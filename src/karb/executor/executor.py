"""Order execution for arbitrage trades."""

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from karb.api.models import ArbitrageOpportunity
from karb.config import get_settings
from karb.data.repositories import ClosedPositionRepository, ExecutionRepository
from karb.executor.async_clob import AsyncClobClient, create_async_clob_client

# Order monitoring settings
ORDER_FILL_TIMEOUT_SECONDS = 10  # Max time to wait for order fills
ORDER_CHECK_INTERVAL_SECONDS = 0.5  # How often to check order status

# Shared state file for dashboard (kept for backward compatibility during transition)
ORDERS_FILE = Path.home() / ".karb" / "orders.json"
from karb.executor.signer import OrderSide, OrderSigner
from karb.notifications.slack import get_notifier
from karb.tracking.trades import Trade, TradeLog
from karb.utils.logging import get_logger

log = get_logger(__name__)


def _configure_proxy_for_clob() -> bool:
    """Configure SOCKS5 proxy for CLOB API calls via environment variables.

    This must be called BEFORE importing py_clob_client so that httpx
    picks up the proxy settings.

    We exclude WebSocket endpoints from the proxy since:
    - WebSocket (price feeds) should be direct for low latency
    - Only order placement (CLOB REST API) needs the proxy for geo-bypass

    Returns True if proxy was configured.
    """
    settings = get_settings()
    if not settings.is_proxy_enabled():
        return False

    proxy_url = settings.get_socks5_proxy_url()
    if proxy_url:
        # Set proxy for HTTP/HTTPS requests
        os.environ["HTTP_PROXY"] = proxy_url
        os.environ["HTTPS_PROXY"] = proxy_url

        # Exclude WebSocket endpoints from proxy (they should be direct)
        # This allows price feeds to connect directly while order placement uses proxy
        no_proxy = "ws-subscriptions-clob.polymarket.com,gamma-api.polymarket.com"
        os.environ["NO_PROXY"] = no_proxy

        log.info("Configured SOCKS5 proxy for CLOB API",
                 proxy_host=settings.socks5_proxy_host,
                 proxy_port=settings.socks5_proxy_port)
        return True
    return False


# Configure proxy before importing py_clob_client
_proxy_enabled = _configure_proxy_for_clob()

# Import Polymarket client (after proxy configuration)
try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import ApiCreds, OrderArgs, OrderType
    CLOB_CLIENT_AVAILABLE = True
except ImportError:
    CLOB_CLIENT_AVAILABLE = False
    log.warning("py-clob-client not installed, order execution will fail")


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
class ExecutionTiming:
    """Timing data for execution flow analysis."""

    # All times stored as epoch ms for easy math
    opportunity_detected: Optional[float] = None  # When scanner found the arb
    execute_start: Optional[float] = None  # When execute() was called
    neg_risk_lookup_start: Optional[float] = None  # Start of neg_risk API call
    neg_risk_lookup_end: Optional[float] = None  # End of neg_risk API call
    order_signing_start: Optional[float] = None  # Start signing orders
    order_signing_end: Optional[float] = None  # Done signing orders
    yes_submit_start: Optional[float] = None  # Yes order HTTP request start
    yes_submit_end: Optional[float] = None  # Yes order HTTP response received
    no_submit_start: Optional[float] = None  # No order HTTP request start
    no_submit_end: Optional[float] = None  # No order HTTP response received
    execute_end: Optional[float] = None  # When execute() returned

    # Detailed breakdown from submit_orders_parallel (ms)
    neg_risk_ms: Optional[int] = None  # Time for neg_risk API lookups
    sign_ms: Optional[int] = None  # Time for order signing (CPU)
    submit_ms: Optional[int] = None  # Time for HTTP submission

    def to_dict(self) -> dict:
        """Convert to dict with computed deltas."""
        data = {
            "opportunity_detected": self.opportunity_detected,
            "execute_start": self.execute_start,
            "execute_end": self.execute_end,
        }
        # Compute deltas (ms between steps)
        deltas = {}
        if self.opportunity_detected and self.execute_start:
            deltas["detection_to_execute_ms"] = round(self.execute_start - self.opportunity_detected, 1)
        if self.execute_start and self.neg_risk_lookup_start:
            deltas["execute_to_neg_risk_ms"] = round(self.neg_risk_lookup_start - self.execute_start, 1)
        if self.neg_risk_lookup_start and self.neg_risk_lookup_end:
            deltas["neg_risk_lookup_ms"] = round(self.neg_risk_lookup_end - self.neg_risk_lookup_start, 1)
        if self.order_signing_start and self.order_signing_end:
            deltas["order_signing_ms"] = round(self.order_signing_end - self.order_signing_start, 1)
        if self.yes_submit_start and self.yes_submit_end:
            deltas["yes_submit_ms"] = round(self.yes_submit_end - self.yes_submit_start, 1)
        if self.no_submit_start and self.no_submit_end:
            deltas["no_submit_ms"] = round(self.no_submit_end - self.no_submit_start, 1)
        if self.execute_start and self.execute_end:
            deltas["total_execute_ms"] = round(self.execute_end - self.execute_start, 1)
        if self.opportunity_detected and self.execute_end:
            deltas["total_latency_ms"] = round(self.execute_end - self.opportunity_detected, 1)
        # Include detailed breakdown from submit_orders_parallel
        if self.neg_risk_ms is not None:
            deltas["neg_risk_ms"] = self.neg_risk_ms
        if self.sign_ms is not None:
            deltas["sign_ms"] = self.sign_ms
        if self.submit_ms is not None:
            deltas["submit_ms"] = self.submit_ms
        data["deltas"] = deltas
        return data

    @staticmethod
    def now_ms() -> float:
        """Get current time in epoch milliseconds."""
        return time.time() * 1000


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
    timing: Optional[ExecutionTiming] = None  # Execution timing data

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
    cancelled: int = 0
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
        self._clob_client: Optional[ClobClient] = None
        self._async_client: Optional[AsyncClobClient] = None
        self._execution_history: list[ExecutionResult] = []
        self._trade_log = TradeLog()
        self._active_orders: dict[str, dict] = {}  # Track active orders for dashboard

        # Initialize CLOB client if credentials available
        if CLOB_CLIENT_AVAILABLE and settings.poly_api_key and settings.poly_api_secret:
            try:
                private_key = settings.private_key.get_secret_value() if settings.private_key else None
                creds = ApiCreds(
                    api_key=settings.poly_api_key,
                    api_secret=settings.poly_api_secret.get_secret_value(),
                    api_passphrase=settings.poly_api_passphrase.get_secret_value() if settings.poly_api_passphrase else "",
                )
                self._clob_client = ClobClient(
                    host=self.clob_base_url,
                    key=private_key,
                    chain_id=settings.chain_id,
                    creds=creds,
                    signature_type=0,  # EOA wallet
                    funder=settings.wallet_address,
                )
                log.info("CLOB client initialized with L2 credentials", wallet=settings.wallet_address)
            except Exception as e:
                log.error("Failed to initialize CLOB client", error=str(e))
                self._clob_client = None

    async def _ensure_async_client(self) -> Optional[AsyncClobClient]:
        """Lazily initialize the async CLOB client."""
        if self._async_client is None:
            self._async_client = await create_async_clob_client()
        return self._async_client

    async def close(self) -> None:
        """Close resources."""
        if self._async_client:
            await self._async_client.close()
            self._async_client = None

    def _save_orders_state(self) -> None:
        """Save current orders state to file for dashboard."""
        try:
            ORDERS_FILE.parent.mkdir(parents=True, exist_ok=True)

            # Build orders list from active orders and recent history
            orders_data = {
                "active_orders": list(self._active_orders.values()),
                "recent_executions": [
                    {
                        "timestamp": r.timestamp.isoformat(),
                        "market": r.opportunity.market.question[:60],
                        "status": r.status.value,
                        "yes_order": {
                            "order_id": r.yes_order.order_id,
                            "status": r.yes_order.status.value,
                            "price": float(r.yes_order.price),
                            "size": float(r.yes_order.size),
                            "filled_size": float(r.yes_order.filled_size),
                        },
                        "no_order": {
                            "order_id": r.no_order.order_id,
                            "status": r.no_order.status.value,
                            "price": float(r.no_order.price),
                            "size": float(r.no_order.size),
                            "filled_size": float(r.no_order.filled_size),
                        },
                        "total_cost": float(r.total_cost),
                        "expected_profit": float(r.expected_profit),
                    }
                    for r in self._execution_history[-20:]  # Last 20 executions
                ],
                "stats": self.get_stats(),
                "updated_at": datetime.utcnow().isoformat(),
            }

            with open(ORDERS_FILE, "w") as f:
                json.dump(orders_data, f)
        except Exception as e:
            log.debug("Failed to save orders state", error=str(e))

    def _track_order(
        self,
        order_id: str,
        market: str,
        side: str,
        outcome: str,
        price: float,
        size: float,
        status: str,
    ) -> None:
        """Track an order for dashboard visibility."""
        self._active_orders[order_id] = {
            "order_id": order_id,
            "market": market[:60],
            "side": side,
            "outcome": outcome,
            "price": price,
            "size": size,
            "status": status,
            "created_at": datetime.utcnow().isoformat(),
            "updated_at": datetime.utcnow().isoformat(),
        }
        self._save_orders_state()

    def _update_order_status(self, order_id: str, status: str, filled_size: float = 0) -> None:
        """Update order status for dashboard."""
        if order_id in self._active_orders:
            self._active_orders[order_id]["status"] = status
            self._active_orders[order_id]["filled_size"] = filled_size
            self._active_orders[order_id]["updated_at"] = datetime.utcnow().isoformat()

            # Remove from active if terminal state
            if status in ("filled", "cancelled", "failed"):
                del self._active_orders[order_id]

            self._save_orders_state()

    async def _submit_order_async(self, token_id: str, side: str, price: float, size: float, neg_risk: Optional[bool] = None) -> dict[str, Any]:
        """
        Submit an order using the async CLOB client (low-latency).

        Args:
            token_id: The token ID to trade
            side: "BUY" or "SELL"
            price: Price per token
            size: Number of tokens
            neg_risk: Whether this is a neg_risk market. If None, auto-detects.

        Returns:
            API response
        """
        async_client = await self._ensure_async_client()
        if not async_client:
            raise RuntimeError("Async CLOB client not initialized - check API credentials")

        try:
            response = await async_client.submit_order(
                token_id=token_id,
                side=side,
                price=price,
                size=size,
                neg_risk=neg_risk,
            )
            log.info("Order submitted (async)", token_id=token_id[:10], side=side, response=response)
            return response
        except Exception as e:
            log.error("Async order submission error", error=str(e))
            raise

    def _submit_order_sync(self, token_id: str, side: str, price: float, size: float) -> dict[str, Any]:
        """
        Submit an order using the official CLOB client (fallback).

        Args:
            token_id: The token ID to trade
            side: "BUY" or "SELL"
            price: Price per token
            size: Number of tokens

        Returns:
            API response
        """
        if not self._clob_client:
            raise RuntimeError("CLOB client not initialized - check API credentials")

        try:
            order_args = OrderArgs(
                token_id=token_id,
                price=price,
                size=size,
                side=side,
            )
            response = self._clob_client.create_and_post_order(order_args)
            log.info("Order submitted", token_id=token_id[:10], side=side, response=response)
            return response
        except Exception as e:
            log.error("Order submission error", error=str(e))
            raise

    def _cancel_order_sync(self, order_id: str) -> dict[str, Any]:
        """
        Cancel an order.

        Args:
            order_id: The order ID to cancel

        Returns:
            API response with canceled/not_canceled lists
        """
        if not self._clob_client:
            raise RuntimeError("CLOB client not initialized")

        try:
            response = self._clob_client.cancel(order_id)
            log.info("Order cancelled", order_id=order_id[:20], response=response)
            return response
        except Exception as e:
            log.error("Order cancellation error", order_id=order_id[:20], error=str(e))
            raise

    def _cancel_all_orders_sync(self) -> dict[str, Any]:
        """Cancel all open orders."""
        if not self._clob_client:
            raise RuntimeError("CLOB client not initialized")

        try:
            response = self._clob_client.cancel_all()
            log.info("All orders cancelled", response=response)
            return response
        except Exception as e:
            log.error("Cancel all orders error", error=str(e))
            raise

    def _get_order_sync(self, order_id: str) -> dict[str, Any]:
        """
        Get order status.

        Args:
            order_id: The order ID to check

        Returns:
            Order details including status
        """
        if not self._clob_client:
            raise RuntimeError("CLOB client not initialized")

        try:
            response = self._clob_client.get_order(order_id)
            return response
        except Exception as e:
            log.error("Get order error", order_id=order_id[:20], error=str(e))
            raise

    async def _attempt_unwind(
        self,
        token_id: str,
        side: str,
        size: float,
        buy_price: float,
        market_name: str,
    ) -> bool:
        """
        Attempt to immediately unwind a position after a partial fill.

        Uses aggressive pricing (sell slightly below best bid) to ensure fill.
        This accepts a small loss to exit an unhedged position immediately.

        Args:
            token_id: Token to sell
            side: Should be "SELL"
            size: Size to sell
            buy_price: Price we bought at (for loss calculation)
            market_name: Market name for logging

        Returns:
            True if unwind succeeded
        """
        async_client = self._async_client

        if not async_client:
            log.error("Cannot unwind - no async client available")
            return False

        # Sell at a discount to ensure fill (accept 2-3% loss to exit immediately)
        # This is better than holding an unhedged directional position
        unwind_price = buy_price * 0.97  # Sell 3% below what we paid

        log.warning(
            "Attempting emergency unwind",
            token_id=token_id[:16],
            size=size,
            buy_price=buy_price,
            unwind_price=unwind_price,
            expected_loss=f"${(buy_price - unwind_price) * size:.2f}",
        )

        try:
            # Use GTC order for unwind (FOK might not fill if liquidity is thin)
            response = await async_client.submit_order(
                token_id=token_id,
                side=side,
                price=unwind_price,
                size=size,
                neg_risk=None,  # Auto-detect
                order_type="GTC",  # GTC for better fill chance
            )

            status = response.get("status", "").lower()
            if status in ("filled", "matched", "delayed", "live"):
                # For GTC orders, "live" or "delayed" means submitted - check if it filled
                actual_exit_price = unwind_price
                loss = (buy_price - actual_exit_price) * size
                cost_basis = buy_price * size
                realized_value = actual_exit_price * size

                log.info(
                    "Unwind successful",
                    token_id=token_id[:16],
                    status=status,
                    loss=f"${loss:.2f}",
                )

                # Record closed position in database
                try:
                    asyncio.create_task(ClosedPositionRepository.insert(
                        timestamp=datetime.now(timezone.utc).isoformat(),
                        market_title=market_name[:100],
                        outcome="unknown",  # We don't have YES/NO info here
                        token_id=token_id,
                        size=size,
                        avg_price=buy_price,
                        exit_price=actual_exit_price,
                        cost_basis=cost_basis,
                        realized_value=realized_value,
                        realized_pnl=-loss,
                        status="SOLD",
                        redeemed=False,
                    ))
                except Exception as e:
                    log.debug("Failed to record closed position", error=str(e))

                # Send notification about unwind
                try:
                    notifier = get_notifier()
                    asyncio.create_task(notifier.send_message(
                        f"🔄 Emergency unwind: Sold {size:.2f} tokens at ${unwind_price:.4f} "
                        f"(bought at ${buy_price:.4f}). Loss: ${loss:.2f}\n"
                        f"Market: {market_name[:50]}"
                    ))
                except Exception:
                    pass

                return True
            else:
                log.warning(
                    "Unwind order submitted but not immediately filled",
                    token_id=token_id[:16],
                    status=status,
                    order_id=response.get("orderID", "")[:20],
                )
                return False

        except Exception as e:
            log.error(
                "Unwind failed",
                token_id=token_id[:16],
                error=str(e),
            )

            # Send alert about failed unwind
            try:
                notifier = get_notifier()
                asyncio.create_task(notifier.send_message(
                    f"⚠️ FAILED UNWIND: Could not sell {size:.2f} tokens\n"
                    f"Token: {token_id[:16]}...\n"
                    f"Error: {str(e)}\n"
                    f"Market: {market_name[:50]}\n"
                    f"MANUAL INTERVENTION REQUIRED"
                ))
            except Exception:
                pass

            return False

    async def _wait_for_fills(
        self,
        yes_order_id: Optional[str],
        no_order_id: Optional[str],
        timeout: float = ORDER_FILL_TIMEOUT_SECONDS,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """
        Wait for orders to fill, with timeout and cancellation.

        Args:
            yes_order_id: YES order ID
            no_order_id: NO order ID
            timeout: Max seconds to wait

        Returns:
            Tuple of (yes_order_status, no_order_status)
        """
        start_time = time.time()

        yes_status: dict[str, Any] = {}
        no_status: dict[str, Any] = {}
        yes_filled = False
        no_filled = False

        # Use async client if available for faster status checks
        async_client = self._async_client

        while time.time() - start_time < timeout:
            # Check order statuses - use async client if available
            try:
                if async_client:
                    # Native async - check both orders in parallel
                    tasks = []
                    if yes_order_id and not yes_filled:
                        tasks.append(async_client.get_order(yes_order_id))
                    if no_order_id and not no_filled:
                        tasks.append(async_client.get_order(no_order_id))

                    if tasks:
                        results = await asyncio.gather(*tasks, return_exceptions=True)
                        idx = 0
                        if yes_order_id and not yes_filled:
                            if not isinstance(results[idx], Exception):
                                yes_status = results[idx]
                                status_str = yes_status.get("status", "").lower()
                                yes_filled = status_str in ("filled", "matched")
                                log.debug("YES order status check", status=status_str, filled=yes_filled)
                                # If order is cancelled/expired, it won't fill - stop waiting
                                if status_str in ("canceled", "cancelled", "expired", "not_matched"):
                                    log.info("YES order cancelled/expired, won't fill", status=status_str)
                            else:
                                log.warning("YES order status check failed", error=str(results[idx]))
                            idx += 1
                        if no_order_id and not no_filled and idx < len(results):
                            if not isinstance(results[idx], Exception):
                                no_status = results[idx]
                                status_str = no_status.get("status", "").lower()
                                no_filled = status_str in ("filled", "matched")
                                log.debug("NO order status check", status=status_str, filled=no_filled)
                                # If order is cancelled/expired, it won't fill - stop waiting
                                if status_str in ("canceled", "cancelled", "expired", "not_matched"):
                                    log.info("NO order cancelled/expired, won't fill", status=status_str)
                            else:
                                log.warning("NO order status check failed", error=str(results[idx]))
                else:
                    # Fallback to sync with executor
                    loop = asyncio.get_event_loop()
                    if yes_order_id and not yes_filled:
                        yes_status = await loop.run_in_executor(
                            None, self._get_order_sync, yes_order_id
                        )
                        yes_filled = yes_status.get("status", "").lower() in ("filled", "matched")

                    if no_order_id and not no_filled:
                        no_status = await loop.run_in_executor(
                            None, self._get_order_sync, no_order_id
                        )
                        no_filled = no_status.get("status", "").lower() in ("filled", "matched")

                # Both filled - success!
                if yes_filled and no_filled:
                    log.info("Both orders filled successfully")
                    return yes_status, no_status

            except Exception as e:
                log.warning("Error checking order status", error=str(e))

            await asyncio.sleep(ORDER_CHECK_INTERVAL_SECONDS)

        # Timeout reached - cancel unfilled orders
        log.warning(
            "Order fill timeout reached",
            yes_filled=yes_filled,
            no_filled=no_filled,
            timeout=timeout,
        )

        # Cancel any unfilled orders
        orders_to_cancel = []
        if yes_order_id and not yes_filled:
            orders_to_cancel.append(("YES", yes_order_id))
        if no_order_id and not no_filled:
            orders_to_cancel.append(("NO", no_order_id))

        for label, order_id in orders_to_cancel:
            try:
                if async_client:
                    await async_client.cancel_order(order_id)
                else:
                    loop = asyncio.get_event_loop()
                    await loop.run_in_executor(None, self._cancel_order_sync, order_id)
                log.info(f"Cancelled unfilled {label} order", order_id=order_id[:20])
            except Exception as e:
                log.error(f"Failed to cancel {label} order", order_id=order_id[:20], error=str(e))

        return yes_status, no_status

    async def cancel_order(self, order_id: str) -> bool:
        """
        Cancel a specific order.

        Args:
            order_id: The order ID to cancel

        Returns:
            True if cancelled successfully
        """
        # Try async client first
        if self._async_client:
            try:
                result = await self._async_client.cancel_order(order_id)
                return order_id in result.get("canceled", [])
            except Exception as e:
                log.error("Failed to cancel order (async)", order_id=order_id[:20], error=str(e))
                return False

        # Fallback to sync client
        if not self._clob_client:
            log.error("CLOB client not initialized")
            return False

        try:
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(None, self._cancel_order_sync, order_id)
            return order_id in result.get("canceled", [])
        except Exception as e:
            log.error("Failed to cancel order", order_id=order_id[:20], error=str(e))
            return False

    async def cancel_all_orders(self) -> dict[str, Any]:
        """
        Cancel all open orders.

        Returns:
            Cancellation result with canceled/not_canceled lists
        """
        # Try async client first
        if self._async_client:
            try:
                return await self._async_client.cancel_all()
            except Exception as e:
                log.error("Failed to cancel all orders (async)", error=str(e))
                return {"canceled": [], "not_canceled": {}, "error": str(e)}

        # Fallback to sync client
        if not self._clob_client:
            log.error("CLOB client not initialized")
            return {"canceled": [], "not_canceled": {}}

        try:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(None, self._cancel_all_orders_sync)
        except Exception as e:
            log.error("Failed to cancel all orders", error=str(e))
            return {"canceled": [], "not_canceled": {}, "error": str(e)}

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

        # Save to database (non-blocking)
        asyncio.create_task(self._save_execution_to_db(result))

        # Log trades
        self._log_trades(result)

        # Send Slack notification for dry run
        try:
            notifier = get_notifier()
            asyncio.create_task(notifier.notify_trade(
                platform="polymarket",
                market=opportunity.market.question,
                side="buy",
                outcome="yes+no",
                price=opportunity.combined_cost,
                size=opportunity.max_trade_size,
                status="simulated",
            ))
        except Exception:
            pass

        return result

    async def _save_execution_to_db(self, result: "ExecutionResult") -> None:
        """Save execution record to database asynchronously."""
        try:
            opp = result.opportunity
            # Store individual liquidity values for each side
            yes_liquidity = float(opp.yes_size_available)
            no_liquidity = float(opp.no_size_available)
            # Also keep combined for backward compatibility
            market_liquidity = yes_liquidity + no_liquidity
            # Serialize timing data to JSON
            timing_data = json.dumps(result.timing.to_dict()) if result.timing else None

            await ExecutionRepository.insert(
                timestamp=result.timestamp.replace(tzinfo=timezone.utc).isoformat(),
                market=opp.market.question[:60],
                status=result.status.value,
                yes_order_id=result.yes_order.order_id,
                yes_status=result.yes_order.status.value,
                yes_price=float(result.yes_order.price),
                yes_size=float(result.yes_order.size),
                yes_filled_size=float(result.yes_order.filled_size),
                yes_error=result.yes_order.error,
                no_order_id=result.no_order.order_id,
                no_status=result.no_order.status.value,
                no_price=float(result.no_order.price),
                no_size=float(result.no_order.size),
                no_filled_size=float(result.no_order.filled_size),
                no_error=result.no_order.error,
                total_cost=float(result.total_cost),
                expected_profit=float(result.expected_profit),
                profit_pct=float(opp.profit_pct),
                market_liquidity=market_liquidity,
                timing_data=timing_data,
                yes_liquidity=yes_liquidity,
                no_liquidity=no_liquidity,
            )
        except Exception as e:
            log.debug("Failed to save execution to database", error=str(e))

    def _log_trades(self, result: ExecutionResult) -> None:
        """Log trades to persistent storage."""
        opp = result.opportunity
        timestamp = result.timestamp.isoformat()

        # Only log trades that were actually FILLED (not just submitted)
        if result.yes_order.status == ExecutionStatus.FILLED:
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

        # Only log trades that were actually FILLED
        if result.no_order.status == ExecutionStatus.FILLED:
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

    async def execute(
        self,
        opportunity: ArbitrageOpportunity,
        detection_timestamp_ms: Optional[float] = None,
    ) -> ExecutionResult:
        """
        Execute an arbitrage opportunity.

        Places BUY orders for both YES and NO tokens.

        Args:
            opportunity: The arbitrage opportunity to execute
            detection_timestamp_ms: When the opportunity was detected (epoch ms)

        Returns:
            Execution result
        """
        # Initialize timing tracker
        timing = ExecutionTiming(
            opportunity_detected=detection_timestamp_ms,
            execute_start=ExecutionTiming.now_ms(),
        )

        self.stats.total_attempts += 1

        # Dry run mode
        if self.dry_run:
            return await self.execute_dry_run(opportunity)

        # Ensure async client is available (preferred) or fall back to sync client
        async_client = await self._ensure_async_client()
        if not async_client and not self._clob_client:
            log.error("No CLOB client initialized, cannot execute orders")
            timing.execute_end = ExecutionTiming.now_ms()
            return ExecutionResult(
                opportunity=opportunity,
                yes_order=OrderResult(
                    token_id=opportunity.market.yes_token.token_id,
                    side=OrderSide.BUY,
                    price=opportunity.yes_ask,
                    size=opportunity.max_trade_size,
                    status=ExecutionStatus.FAILED,
                    error="CLOB client not initialized - check POLY_API_KEY/SECRET",
                ),
                no_order=OrderResult(
                    token_id=opportunity.market.no_token.token_id,
                    side=OrderSide.BUY,
                    price=opportunity.no_ask,
                    size=opportunity.max_trade_size,
                    status=ExecutionStatus.FAILED,
                    error="CLOB client not initialized - check POLY_API_KEY/SECRET",
                ),
                status=ExecutionStatus.FAILED,
                timing=timing,
            )

        log.info(
            "Executing arbitrage",
            market=opportunity.market.question[:50],
            yes_price=float(opportunity.yes_ask),
            no_price=float(opportunity.no_ask),
            size=float(opportunity.max_trade_size),
        )

        # Let async client auto-detect neg_risk from API (pass None)
        # This ensures correct exchange contract is used for signing

        try:
            if async_client:
                # Use optimized parallel execution: batch neg_risk + sign + submit
                timing.order_signing_start = ExecutionTiming.now_ms()
                orders = [
                    (
                        opportunity.market.yes_token.token_id,
                        "BUY",
                        float(opportunity.yes_ask),
                        float(opportunity.max_trade_size),
                        None,  # Auto-detect neg_risk
                    ),
                    (
                        opportunity.market.no_token.token_id,
                        "BUY",
                        float(opportunity.no_ask),
                        float(opportunity.max_trade_size),
                        None,  # Auto-detect neg_risk
                    ),
                ]
                responses, order_timing = await async_client.submit_orders_parallel(orders)
                yes_response, no_response = responses[0], responses[1]
                timing.order_signing_end = ExecutionTiming.now_ms()

                # Store detailed timing breakdown
                timing.neg_risk_ms = order_timing.get("neg_risk_ms", 0)
                timing.sign_ms = order_timing.get("sign_ms", 0)
                timing.submit_ms = order_timing.get("submit_ms", 0)
            else:
                # Fallback to sync client wrapped in executor
                timing.order_signing_start = ExecutionTiming.now_ms()
                loop = asyncio.get_event_loop()
                yes_response, no_response = await asyncio.gather(
                    loop.run_in_executor(
                        None,
                        self._submit_order_sync,
                        opportunity.market.yes_token.token_id,
                        "BUY",
                        float(opportunity.yes_ask),
                        float(opportunity.max_trade_size),
                    ),
                    loop.run_in_executor(
                        None,
                        self._submit_order_sync,
                        opportunity.market.no_token.token_id,
                        "BUY",
                        float(opportunity.no_ask),
                        float(opportunity.max_trade_size),
                    ),
                    return_exceptions=True,
                )
                timing.order_signing_end = ExecutionTiming.now_ms()
        except Exception as e:
            log.error("Order submission failed", error=str(e))
            self.stats.failed += 1
            timing.execute_end = ExecutionTiming.now_ms()
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
                timing=timing,
            )

        # Parse initial responses
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

        # Track orders for dashboard visibility
        market_name = opportunity.market.question[:60]
        if yes_result.order_id:
            self._track_order(
                order_id=yes_result.order_id,
                market=market_name,
                side="BUY",
                outcome="YES",
                price=float(opportunity.yes_ask),
                size=float(opportunity.max_trade_size),
                status=yes_result.status.value,
            )
        if no_result.order_id:
            self._track_order(
                order_id=no_result.order_id,
                market=market_name,
                side="BUY",
                outcome="NO",
                price=float(opportunity.no_ask),
                size=float(opportunity.max_trade_size),
                status=no_result.status.value,
            )

        # FOK orders should resolve immediately - if status is SUBMITTED (live/delayed),
        # that's unexpected for FOK. Cancel immediately instead of waiting 10 seconds.
        yes_unexpected_fok = yes_result.status == ExecutionStatus.SUBMITTED and yes_result.order_id
        no_unexpected_fok = no_result.status == ExecutionStatus.SUBMITTED and no_result.order_id

        if yes_unexpected_fok or no_unexpected_fok:
            log.warning(
                "FOK orders returned unexpected 'live' status - cancelling immediately",
                yes_status=yes_result.status.value if yes_result.order_id else None,
                no_status=no_result.status.value if no_result.order_id else None,
            )

            # Cancel any orders that are unexpectedly live (shouldn't happen with FOK)
            async_client = self._async_client
            if yes_unexpected_fok and yes_result.order_id:
                try:
                    if async_client:
                        await async_client.cancel_order(yes_result.order_id)
                    log.info("Cancelled unexpected live YES order", order_id=yes_result.order_id[:20])
                    yes_result.status = ExecutionStatus.CANCELLED
                    yes_result.filled_size = Decimal("0")
                    self._update_order_status(yes_result.order_id, "cancelled", 0)
                except Exception as e:
                    log.error("Failed to cancel YES order", error=str(e))

            if no_unexpected_fok and no_result.order_id:
                try:
                    if async_client:
                        await async_client.cancel_order(no_result.order_id)
                    log.info("Cancelled unexpected live NO order", order_id=no_result.order_id[:20])
                    no_result.status = ExecutionStatus.CANCELLED
                    no_result.filled_size = Decimal("0")
                    self._update_order_status(no_result.order_id, "cancelled", 0)
                except Exception as e:
                    log.error("Failed to cancel NO order", error=str(e))

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
        elif yes_result.status == ExecutionStatus.CANCELLED and no_result.status == ExecutionStatus.CANCELLED:
            # Both cancelled (timeout) - no position taken
            status = ExecutionStatus.CANCELLED
            self.stats.cancelled += 1
            total_cost = Decimal("0")
            log.warning("Both orders cancelled due to timeout - no position taken")
        else:
            # Partial fill or mixed status - IMMEDIATELY TRY TO UNWIND
            status = ExecutionStatus.PARTIAL
            self.stats.partial += 1
            # Calculate partial cost
            yes_cost = yes_result.filled_size * opportunity.yes_ask
            no_cost = no_result.filled_size * opportunity.no_ask
            total_cost = yes_cost + no_cost

            # Attempt immediate unwind of the filled side
            if yes_result.status == ExecutionStatus.FILLED and no_result.status != ExecutionStatus.FILLED:
                log.error(
                    "UNHEDGED POSITION: YES filled but NO did not! Attempting immediate unwind...",
                    yes_size=float(yes_result.filled_size),
                    no_status=no_result.status.value,
                )
                # Try to sell the YES position immediately
                await self._attempt_unwind(
                    token_id=opportunity.market.yes_token.token_id,
                    side="SELL",
                    size=float(yes_result.filled_size),
                    buy_price=float(opportunity.yes_ask),
                    market_name=opportunity.market.question,
                )
            elif no_result.status == ExecutionStatus.FILLED and yes_result.status != ExecutionStatus.FILLED:
                log.error(
                    "UNHEDGED POSITION: NO filled but YES did not! Attempting immediate unwind...",
                    no_size=float(no_result.filled_size),
                    yes_status=yes_result.status.value,
                )
                # Try to sell the NO position immediately
                await self._attempt_unwind(
                    token_id=opportunity.market.no_token.token_id,
                    side="SELL",
                    size=float(no_result.filled_size),
                    buy_price=float(opportunity.no_ask),
                    market_name=opportunity.market.question,
                )

        timing.execute_end = ExecutionTiming.now_ms()
        result = ExecutionResult(
            opportunity=opportunity,
            yes_order=yes_result,
            no_order=no_result,
            status=status,
            total_cost=total_cost,
            expected_profit=opportunity.expected_profit_usd if status == ExecutionStatus.FILLED else Decimal("0"),
            timing=timing,
        )

        self._execution_history.append(result)

        # Save to database (non-blocking)
        asyncio.create_task(self._save_execution_to_db(result))

        # Log trades
        self._log_trades(result)

        # Send Slack notification
        try:
            notifier = get_notifier()
            trade_status = "executed" if status == ExecutionStatus.FILLED else status.value
            asyncio.create_task(notifier.notify_trade(
                platform="polymarket",
                market=opportunity.market.question,
                side="buy",
                outcome="yes+no",
                price=opportunity.combined_cost,
                size=opportunity.max_trade_size,
                status=trade_status,
            ))
        except Exception:
            pass

        log.info(
            "Execution complete",
            status=status.value,
            yes_status=yes_result.status.value,
            no_status=no_result.status.value,
        )

        # Save final orders state for dashboard
        self._save_orders_state()

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
            error_msg = response.get("error") or response.get("errorMsg") or response.get("message") or ""
            if error_msg:
                # FOK orders that can't fill return a specific error - treat as CANCELLED not FAILED
                if "FOK" in error_msg.upper() or "NOT_FILLED" in error_msg.upper():
                    log.info("FOK order not filled - cancelled", error=error_msg)
                    return OrderResult(
                        token_id=token_id,
                        side=side,
                        price=price,
                        size=size,
                        status=ExecutionStatus.CANCELLED,
                        error=error_msg,
                    )
                return OrderResult(
                    token_id=token_id,
                    side=side,
                    price=price,
                    size=size,
                    status=ExecutionStatus.FAILED,
                    error=error_msg,
                )

            # Parse success response
            order_id = response.get("orderID") or response.get("id")
            status_str = response.get("status", "").lower()

            log.info(
                "Order API response",
                order_id=order_id[:20] if order_id else None,
                status=status_str,
                raw_response=str(response)[:200],
            )

            if status_str in ("filled", "matched"):
                status = ExecutionStatus.FILLED
                filled_size = size
            elif status_str in ("open", "live", "pending"):
                status = ExecutionStatus.SUBMITTED
                filled_size = Decimal("0")
            elif status_str in ("canceled", "cancelled", "not_matched", "expired"):
                # FOK order that couldn't be filled
                status = ExecutionStatus.CANCELLED
                filled_size = Decimal("0")
            else:
                log.warning("Unknown order status", status=status_str)
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
            "cancelled": self.stats.cancelled,
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
