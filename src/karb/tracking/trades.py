"""Trade logging and history."""

import asyncio
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Optional

from karb.data.repositories import TradeRepository
from karb.utils.logging import get_logger

log = get_logger(__name__)


@dataclass
class Trade:
    """A completed trade."""
    timestamp: str
    platform: str  # "polymarket" or "kalshi"
    market_id: str
    market_name: str
    side: str  # "buy" or "sell"
    outcome: str  # "yes" or "no"
    price: float
    size: float
    cost: float
    order_id: Optional[str] = None
    strategy: str = "single_market"  # "single_market" or "cross_platform"
    profit_expected: Optional[float] = None
    notes: Optional[str] = None


class TradeLog:
    """Trade logging using SQLite database."""

    def __init__(self) -> None:
        """Initialize trade log."""
        pass  # No file path needed, uses database

    def log_trade(self, trade: Trade) -> None:
        """Log a trade to the database (non-blocking).

        This schedules an async database write without blocking.
        """
        # Schedule async database write
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._log_trade_async(trade))
        except RuntimeError:
            # No running loop, use sync fallback (shouldn't happen in normal operation)
            log.debug("No event loop, skipping database trade log")

        log.info(
            "Trade logged",
            platform=trade.platform,
            market=trade.market_name[:30],
            side=trade.side,
            outcome=trade.outcome,
            price=f"${trade.price:.3f}",
            size=f"${trade.size:.2f}",
        )

    async def _log_trade_async(self, trade: Trade) -> None:
        """Async implementation of trade logging."""
        try:
            await TradeRepository.insert(
                timestamp=trade.timestamp,
                platform=trade.platform,
                market_id=trade.market_id,
                market_name=trade.market_name,
                side=trade.side,
                outcome=trade.outcome,
                price=trade.price,
                size=trade.size,
                cost=trade.cost,
                order_id=trade.order_id,
                strategy=trade.strategy,
                profit_expected=trade.profit_expected,
                notes=trade.notes,
            )
        except Exception as e:
            log.debug("Failed to log trade to database", error=str(e))

    async def log_trade_async(self, trade: Trade) -> None:
        """Log a trade to the database asynchronously."""
        await self._log_trade_async(trade)
        log.info(
            "Trade logged",
            platform=trade.platform,
            market=trade.market_name[:30],
            side=trade.side,
            outcome=trade.outcome,
            price=f"${trade.price:.3f}",
            size=f"${trade.size:.2f}",
        )

    async def get_trades(
        self,
        limit: int = 100,
        platform: Optional[str] = None,
        since: Optional[datetime] = None,
    ) -> list[Trade]:
        """Get recent trades from the database."""
        since_str = since.isoformat() if since else None
        rows = await TradeRepository.get_recent(
            limit=limit,
            platform=platform,
            since=since_str,
        )
        return [Trade(**row) for row in rows]

    async def get_daily_summary(self, date: Optional[datetime] = None) -> dict[str, Any]:
        """Get summary for a specific day."""
        if date is None:
            date = datetime.now()
        date_str = date.strftime("%Y-%m-%d")
        return await TradeRepository.get_daily_summary(date_str)

    async def get_all_time_summary(self) -> dict[str, Any]:
        """Get all-time trading summary."""
        return await TradeRepository.get_all_time_summary()
