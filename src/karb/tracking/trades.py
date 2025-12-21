"""Trade logging and history."""

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Optional

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
    """Persistent trade logging."""

    def __init__(self, log_path: Optional[Path] = None) -> None:
        if log_path is None:
            log_path = Path.home() / ".karb" / "trades.jsonl"

        self.log_path = log_path
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def log_trade(self, trade: Trade) -> None:
        """Append a trade to the log."""
        with open(self.log_path, "a") as f:
            f.write(json.dumps(asdict(trade)) + "\n")

        log.info(
            "Trade logged",
            platform=trade.platform,
            market=trade.market_name[:30],
            side=trade.side,
            outcome=trade.outcome,
            price=f"${trade.price:.3f}",
            size=f"${trade.size:.2f}",
        )

    def get_trades(
        self,
        limit: int = 100,
        platform: Optional[str] = None,
        since: Optional[datetime] = None,
    ) -> list[Trade]:
        """Get recent trades."""
        if not self.log_path.exists():
            return []

        trades = []
        with open(self.log_path) as f:
            for line in f:
                try:
                    data = json.loads(line.strip())
                    trade = Trade(**data)

                    if platform and trade.platform != platform:
                        continue

                    if since:
                        trade_time = datetime.fromisoformat(trade.timestamp)
                        if trade_time < since:
                            continue

                    trades.append(trade)
                except (json.JSONDecodeError, TypeError):
                    continue

        # Return most recent first
        return sorted(trades, key=lambda t: t.timestamp, reverse=True)[:limit]

    def get_daily_summary(self, date: Optional[datetime] = None) -> dict:
        """Get summary for a specific day."""
        if date is None:
            date = datetime.now()

        day_start = date.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start.replace(hour=23, minute=59, second=59)

        trades = self.get_trades(limit=1000, since=day_start)
        trades = [t for t in trades if datetime.fromisoformat(t.timestamp) <= day_end]

        total_cost = sum(t.cost for t in trades)
        total_expected_profit = sum(t.profit_expected or 0 for t in trades)

        by_platform = {}
        for t in trades:
            if t.platform not in by_platform:
                by_platform[t.platform] = {"count": 0, "cost": 0.0}
            by_platform[t.platform]["count"] += 1
            by_platform[t.platform]["cost"] += t.cost

        return {
            "date": day_start.strftime("%Y-%m-%d"),
            "trade_count": len(trades),
            "total_cost": total_cost,
            "expected_profit": total_expected_profit,
            "by_platform": by_platform,
        }

    def get_all_time_summary(self) -> dict:
        """Get all-time trading summary."""
        trades = self.get_trades(limit=10000)

        if not trades:
            return {
                "trade_count": 0,
                "total_cost": 0.0,
                "expected_profit": 0.0,
                "first_trade": None,
                "last_trade": None,
            }

        total_cost = sum(t.cost for t in trades)
        total_expected_profit = sum(t.profit_expected or 0 for t in trades)

        return {
            "trade_count": len(trades),
            "total_cost": total_cost,
            "expected_profit": total_expected_profit,
            "first_trade": trades[-1].timestamp if trades else None,
            "last_trade": trades[0].timestamp if trades else None,
        }
