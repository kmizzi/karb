"""Order execution for arbitrage trades."""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Optional

import aiohttp

from karb.api.models import ArbitrageOpportunity
from karb.config import get_settings
from karb.executor.signer import OrderSide, OrderSigner, SignedOrder
from karb.tracking.trades import Trade, TradeLog
from karb.utils.logging import get_logger

log = get_logger(__name__)


class ExecutionStatus(Enum):
    """Status of an execution attempt."""

    PENDING = "pending"
    SUBMITTED = "submitted"
    PARTIAL = "partial"
    FILLED = "filled"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass
class OrderResult:
    """Result of a single order submission."""

    token_id: str
    side: OrderSide
    price: Decimal
    size: Decimal
    status: ExecutionStatus
    order_id: Optional[str] = None
    filled_size: Decimal = Decimal("0")
    error: Optional[str] = None
    timestamp: datetime = field(default_factory=datetime.utcnow)


@dataclass
class ExecutionResult:
    """Result of an arbitrage execution (both orders)."""

    opportunity: ArbitrageOpportunity
    yes_order: OrderResult
    no_order: OrderResult
    status: ExecutionStatus
    total_cost: Decimal = Decimal("0")
    expected_profit: Decimal = Decimal("0")
    timestamp: datetime = field(default_factory=datetime.utcnow)

    @property
    def is_successful(self) -> bool:
        """Check if both orders filled successfully."""
        return (
            self.yes_order.status == ExecutionStatus.FILLED
            and self.no_order.status == ExecutionStatus.FILLED
        )


@dataclass
class ExecutorStats:
    """Statistics for the executor."""

    total_attempts: int = 0
    successful: int = 0
    partial: int = 0
    failed: int = 0
    total_volume: Decimal = Decimal("0")
    total_profit: Decimal = Decimal("0")


class OrderExecutor:
    """
    Executes arbitrage trades on Polymarket.

    Handles:
    - Order creation and signing
    - Submitting both YES and NO orders
    - Monitoring fill status
    - Dry run simulation
    """

    def __init__(
        self,
        signer: Optional[OrderSigner] = None,
        dry_run: Optional[bool] = None,
        clob_base_url: Optional[str] = None,
    ) -> None:
        settings = get_settings()

        self.signer = signer or OrderSigner()
        self.dry_run = dry_run if dry_run is not None else settings.dry_run
        self.clob_base_url = (clob_base_url or settings.clob_base_url).rstrip("/")

        self.stats = ExecutorStats()
        self._session: Optional[aiohttp.ClientSession] = None
        self._execution_history: list[ExecutionResult] = []
        self._trade_log = TradeLog()

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create aiohttp session."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                timeout=aiohttp.ClientTimeout(total=30),
            )
        return self._session

    async def close(self) -> None:
        """Close HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def _submit_order(self, signed_order: SignedOrder) -> dict[str, Any]:
        """
        Submit a signed order to the CLOB API.

        Args:
            signed_order: The signed order to submit

        Returns:
            API response
        """
        session = await self._get_session()
        url = f"{self.clob_base_url}/order"

        payload = self.signer.order_to_api_payload(signed_order)

        try:
            async with session.post(url, json=payload) as response:
                data = await response.json()
                if response.status >= 400:
                    log.error(
                        "Order submission failed",
                        status=response.status,
                        response=data,
                    )
                return data
        except aiohttp.ClientError as e:
            log.error("Order submission error", error=str(e))
            raise

    async def execute_dry_run(
        self, opportunity: ArbitrageOpportunity
    ) -> ExecutionResult:
        """
        Simulate execution without placing real orders.

        Args:
            opportunity: The arbitrage opportunity

        Returns:
            Simulated execution result
        """
        log.info(
            "[DRY RUN] Would execute arbitrage",
            market=opportunity.market.question[:50],
            yes_price=float(opportunity.yes_ask),
            no_price=float(opportunity.no_ask),
            size=float(opportunity.max_trade_size),
            expected_profit=f"${float(opportunity.expected_profit_usd):.2f}",
        )

        # Simulate successful execution
        yes_result = OrderResult(
            token_id=opportunity.market.yes_token.token_id,
            side=OrderSide.BUY,
            price=opportunity.yes_ask,
            size=opportunity.max_trade_size,
            status=ExecutionStatus.FILLED,
            order_id="dry-run-yes",
            filled_size=opportunity.max_trade_size,
        )

        no_result = OrderResult(
            token_id=opportunity.market.no_token.token_id,
            side=OrderSide.BUY,
            price=opportunity.no_ask,
            size=opportunity.max_trade_size,
            status=ExecutionStatus.FILLED,
            order_id="dry-run-no",
            filled_size=opportunity.max_trade_size,
        )

        total_cost = opportunity.max_trade_size * opportunity.combined_cost

        result = ExecutionResult(
            opportunity=opportunity,
            yes_order=yes_result,
            no_order=no_result,
            status=ExecutionStatus.FILLED,
            total_cost=total_cost,
            expected_profit=opportunity.expected_profit_usd,
        )

        # Update stats
        self.stats.total_attempts += 1
        self.stats.successful += 1
        self.stats.total_volume += total_cost
        self.stats.total_profit += opportunity.expected_profit_usd

        self._execution_history.append(result)

        # Log trades
        self._log_trades(result)

        return result

    def _log_trades(self, result: ExecutionResult) -> None:
        """Log trades to persistent storage."""
        opp = result.opportunity
        timestamp = result.timestamp.isoformat()

        # Log YES order if it was submitted
        if result.yes_order.status in (ExecutionStatus.FILLED, ExecutionStatus.SUBMITTED):
            self._trade_log.log_trade(Trade(
                timestamp=timestamp,
                platform="polymarket",
                market_id=opp.market.condition_id,
                market_name=opp.market.question,
                side="buy",
                outcome="yes",
                price=float(result.yes_order.price),
                size=float(result.yes_order.filled_size or result.yes_order.size),
                cost=float(result.yes_order.price * (result.yes_order.filled_size or result.yes_order.size)),
                order_id=result.yes_order.order_id,
                strategy="single_market",
                profit_expected=float(opp.expected_profit_usd) / 2,  # Split between both orders
            ))

        # Log NO order if it was submitted
        if result.no_order.status in (ExecutionStatus.FILLED, ExecutionStatus.SUBMITTED):
            self._trade_log.log_trade(Trade(
                timestamp=timestamp,
                platform="polymarket",
                market_id=opp.market.condition_id,
                market_name=opp.market.question,
                side="buy",
                outcome="no",
                price=float(result.no_order.price),
                size=float(result.no_order.filled_size or result.no_order.size),
                cost=float(result.no_order.price * (result.no_order.filled_size or result.no_order.size)),
                order_id=result.no_order.order_id,
                strategy="single_market",
                profit_expected=float(opp.expected_profit_usd) / 2,
            ))

    async def execute(self, opportunity: ArbitrageOpportunity) -> ExecutionResult:
        """
        Execute an arbitrage opportunity.

        Places BUY orders for both YES and NO tokens.

        Args:
            opportunity: The arbitrage opportunity to execute

        Returns:
            Execution result
        """
        self.stats.total_attempts += 1

        # Dry run mode
        if self.dry_run:
            return await self.execute_dry_run(opportunity)

        # Check signer configuration
        if not self.signer.is_configured:
            log.error("Signer not configured, cannot execute orders")
            return ExecutionResult(
                opportunity=opportunity,
                yes_order=OrderResult(
                    token_id=opportunity.market.yes_token.token_id,
                    side=OrderSide.BUY,
                    price=opportunity.yes_ask,
                    size=opportunity.max_trade_size,
                    status=ExecutionStatus.FAILED,
                    error="Signer not configured",
                ),
                no_order=OrderResult(
                    token_id=opportunity.market.no_token.token_id,
                    side=OrderSide.BUY,
                    price=opportunity.no_ask,
                    size=opportunity.max_trade_size,
                    status=ExecutionStatus.FAILED,
                    error="Signer not configured",
                ),
                status=ExecutionStatus.FAILED,
            )

        log.info(
            "Executing arbitrage",
            market=opportunity.market.question[:50],
            yes_price=float(opportunity.yes_ask),
            no_price=float(opportunity.no_ask),
            size=float(opportunity.max_trade_size),
        )

        # Create orders
        yes_order_data = self.signer.create_order(
            token_id=opportunity.market.yes_token.token_id,
            side=OrderSide.BUY,
            price=opportunity.yes_ask,
            size=opportunity.max_trade_size,
        )

        no_order_data = self.signer.create_order(
            token_id=opportunity.market.no_token.token_id,
            side=OrderSide.BUY,
            price=opportunity.no_ask,
            size=opportunity.max_trade_size,
        )

        # Sign orders
        try:
            yes_signed = self.signer.sign_order(yes_order_data)
            no_signed = self.signer.sign_order(no_order_data)
        except Exception as e:
            log.error("Failed to sign orders", error=str(e))
            self.stats.failed += 1
            return ExecutionResult(
                opportunity=opportunity,
                yes_order=OrderResult(
                    token_id=opportunity.market.yes_token.token_id,
                    side=OrderSide.BUY,
                    price=opportunity.yes_ask,
                    size=opportunity.max_trade_size,
                    status=ExecutionStatus.FAILED,
                    error=f"Signing failed: {e}",
                ),
                no_order=OrderResult(
                    token_id=opportunity.market.no_token.token_id,
                    side=OrderSide.BUY,
                    price=opportunity.no_ask,
                    size=opportunity.max_trade_size,
                    status=ExecutionStatus.FAILED,
                    error=f"Signing failed: {e}",
                ),
                status=ExecutionStatus.FAILED,
            )

        # Submit orders concurrently
        try:
            yes_response, no_response = await asyncio.gather(
                self._submit_order(yes_signed),
                self._submit_order(no_signed),
                return_exceptions=True,
            )
        except Exception as e:
            log.error("Order submission failed", error=str(e))
            self.stats.failed += 1
            return ExecutionResult(
                opportunity=opportunity,
                yes_order=OrderResult(
                    token_id=opportunity.market.yes_token.token_id,
                    side=OrderSide.BUY,
                    price=opportunity.yes_ask,
                    size=opportunity.max_trade_size,
                    status=ExecutionStatus.FAILED,
                    error=str(e),
                ),
                no_order=OrderResult(
                    token_id=opportunity.market.no_token.token_id,
                    side=OrderSide.BUY,
                    price=opportunity.no_ask,
                    size=opportunity.max_trade_size,
                    status=ExecutionStatus.FAILED,
                    error=str(e),
                ),
                status=ExecutionStatus.FAILED,
            )

        # Parse responses
        yes_result = self._parse_order_response(
            yes_response,
            opportunity.market.yes_token.token_id,
            OrderSide.BUY,
            opportunity.yes_ask,
            opportunity.max_trade_size,
        )

        no_result = self._parse_order_response(
            no_response,
            opportunity.market.no_token.token_id,
            OrderSide.BUY,
            opportunity.no_ask,
            opportunity.max_trade_size,
        )

        # Determine overall status
        if yes_result.status == ExecutionStatus.FILLED and no_result.status == ExecutionStatus.FILLED:
            status = ExecutionStatus.FILLED
            self.stats.successful += 1
            total_cost = opportunity.max_trade_size * opportunity.combined_cost
            self.stats.total_volume += total_cost
            self.stats.total_profit += opportunity.expected_profit_usd
        elif yes_result.status == ExecutionStatus.FAILED and no_result.status == ExecutionStatus.FAILED:
            status = ExecutionStatus.FAILED
            self.stats.failed += 1
            total_cost = Decimal("0")
        else:
            status = ExecutionStatus.PARTIAL
            self.stats.partial += 1
            # Calculate partial cost
            yes_cost = yes_result.filled_size * opportunity.yes_ask
            no_cost = no_result.filled_size * opportunity.no_ask
            total_cost = yes_cost + no_cost

        result = ExecutionResult(
            opportunity=opportunity,
            yes_order=yes_result,
            no_order=no_result,
            status=status,
            total_cost=total_cost,
            expected_profit=opportunity.expected_profit_usd if status == ExecutionStatus.FILLED else Decimal("0"),
        )

        self._execution_history.append(result)

        # Log trades
        self._log_trades(result)

        log.info(
            "Execution complete",
            status=status.value,
            yes_status=yes_result.status.value,
            no_status=no_result.status.value,
        )

        return result

    def _parse_order_response(
        self,
        response: Any,
        token_id: str,
        side: OrderSide,
        price: Decimal,
        size: Decimal,
    ) -> OrderResult:
        """Parse API response into OrderResult."""
        if isinstance(response, Exception):
            return OrderResult(
                token_id=token_id,
                side=side,
                price=price,
                size=size,
                status=ExecutionStatus.FAILED,
                error=str(response),
            )

        if isinstance(response, dict):
            # Check for error
            if "error" in response or "message" in response:
                return OrderResult(
                    token_id=token_id,
                    side=side,
                    price=price,
                    size=size,
                    status=ExecutionStatus.FAILED,
                    error=response.get("error") or response.get("message"),
                )

            # Parse success response
            order_id = response.get("orderID") or response.get("id")
            status_str = response.get("status", "").lower()

            if status_str in ("filled", "matched"):
                status = ExecutionStatus.FILLED
                filled_size = size
            elif status_str in ("open", "live", "pending"):
                status = ExecutionStatus.SUBMITTED
                filled_size = Decimal("0")
            else:
                status = ExecutionStatus.SUBMITTED
                filled_size = Decimal("0")

            return OrderResult(
                token_id=token_id,
                side=side,
                price=price,
                size=size,
                status=status,
                order_id=order_id,
                filled_size=filled_size,
            )

        return OrderResult(
            token_id=token_id,
            side=side,
            price=price,
            size=size,
            status=ExecutionStatus.FAILED,
            error="Unknown response format",
        )

    def get_stats(self) -> dict[str, Any]:
        """Get executor statistics."""
        return {
            "total_attempts": self.stats.total_attempts,
            "successful": self.stats.successful,
            "partial": self.stats.partial,
            "failed": self.stats.failed,
            "total_volume": float(self.stats.total_volume),
            "total_profit": float(self.stats.total_profit),
            "success_rate": (
                self.stats.successful / self.stats.total_attempts * 100
                if self.stats.total_attempts > 0
                else 0
            ),
        }

    def get_history(self) -> list[ExecutionResult]:
        """Get execution history."""
        return self._execution_history.copy()

    async def __aenter__(self) -> "OrderExecutor":
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()
