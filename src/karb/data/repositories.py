"""Repository classes for database operations.

All repository methods are async to avoid blocking the trading loop.
"""

from datetime import datetime, timezone
from typing import Any, Optional

from karb.data.database import get_async_db, get_db
from karb.utils.logging import get_logger

log = get_logger(__name__)


class TradeRepository:
    """Repository for trade records."""

    @staticmethod
    async def insert(
        timestamp: str,
        platform: str,
        side: str,
        outcome: str,
        price: float,
        size: float,
        cost: float,
        market_id: Optional[str] = None,
        market_name: Optional[str] = None,
        order_id: Optional[str] = None,
        strategy: Optional[str] = None,
        profit_expected: Optional[float] = None,
        notes: Optional[str] = None,
    ) -> int:
        """Insert a new trade record."""
        async with get_async_db() as conn:
            cursor = await conn.execute(
                """
                INSERT INTO trades (
                    timestamp, platform, market_id, market_name, side, outcome,
                    price, size, cost, order_id, strategy, profit_expected, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    timestamp, platform, market_id, market_name, side, outcome,
                    price, size, cost, order_id, strategy, profit_expected, notes
                ),
            )
            return cursor.lastrowid or 0

    @staticmethod
    async def get_recent(
        limit: int = 50,
        offset: int = 0,
        platform: Optional[str] = None,
        since: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """Get recent trades with optional filtering."""
        async with get_async_db() as conn:
            query = "SELECT * FROM trades WHERE 1=1"
            params: list[Any] = []

            if platform:
                query += " AND platform = ?"
                params.append(platform)
            if since:
                query += " AND timestamp >= ?"
                params.append(since)

            query += " ORDER BY timestamp DESC LIMIT ? OFFSET ?"
            params.extend([limit, offset])

            cursor = await conn.execute(query, params)
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    @staticmethod
    async def get_total_count(
        platform: Optional[str] = None,
        since: Optional[str] = None,
    ) -> int:
        """Get total count of trades."""
        async with get_async_db() as conn:
            query = "SELECT COUNT(*) as count FROM trades WHERE 1=1"
            params: list[Any] = []

            if platform:
                query += " AND platform = ?"
                params.append(platform)
            if since:
                query += " AND timestamp >= ?"
                params.append(since)

            cursor = await conn.execute(query, params)
            row = await cursor.fetchone()
            return row["count"] if row else 0

    @staticmethod
    async def get_daily_summary(date: str) -> dict[str, Any]:
        """Get summary stats for a specific date (YYYY-MM-DD)."""
        async with get_async_db() as conn:
            cursor = await conn.execute(
                """
                SELECT
                    COUNT(*) as trade_count,
                    SUM(cost) as total_volume,
                    SUM(CASE WHEN side = 'buy' THEN cost ELSE 0 END) as buy_volume,
                    SUM(CASE WHEN side = 'sell' THEN cost ELSE 0 END) as sell_volume
                FROM trades
                WHERE timestamp LIKE ?
                """,
                (f"{date}%",),
            )
            row = await cursor.fetchone()
            return dict(row) if row else {}

    @staticmethod
    async def get_all_time_summary() -> dict[str, Any]:
        """Get all-time summary stats."""
        async with get_async_db() as conn:
            cursor = await conn.execute(
                """
                SELECT
                    COUNT(*) as total_trades,
                    SUM(cost) as total_volume,
                    COUNT(DISTINCT market_id) as unique_markets,
                    MIN(timestamp) as first_trade,
                    MAX(timestamp) as last_trade
                FROM trades
                """
            )
            row = await cursor.fetchone()
            return dict(row) if row else {}


class AlertRepository:
    """Repository for arbitrage alerts."""

    MAX_ALERTS = 100  # Keep last 100 alerts

    @staticmethod
    async def insert(
        market: str,
        yes_ask: float,
        no_ask: float,
        combined: float,
        profit: float,
        timestamp: str,
        platform: str = "polymarket",
        days_until_resolution: Optional[int] = None,
        resolution_date: Optional[str] = None,
        first_seen: Optional[str] = None,
        duration_secs: Optional[float] = None,
    ) -> int:
        """Insert a new alert and cleanup old ones."""
        async with get_async_db() as conn:
            cursor = await conn.execute(
                """
                INSERT INTO alerts (
                    market, yes_ask, no_ask, combined, profit, timestamp,
                    platform, days_until_resolution, resolution_date,
                    first_seen, duration_secs
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    market, yes_ask, no_ask, combined, profit, timestamp,
                    platform, days_until_resolution, resolution_date,
                    first_seen, duration_secs
                ),
            )
            alert_id = cursor.lastrowid or 0

            # Cleanup old alerts (keep last MAX_ALERTS)
            await conn.execute(
                """
                DELETE FROM alerts WHERE id NOT IN (
                    SELECT id FROM alerts ORDER BY id DESC LIMIT ?
                )
                """,
                (AlertRepository.MAX_ALERTS,),
            )

            return alert_id

    @staticmethod
    async def get_recent(limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        """Get recent alerts with pagination."""
        async with get_async_db() as conn:
            cursor = await conn.execute(
                "SELECT * FROM alerts ORDER BY id DESC LIMIT ? OFFSET ?",
                (limit, offset),
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    @staticmethod
    async def get_total_count() -> int:
        """Get total count of alerts."""
        async with get_async_db() as conn:
            cursor = await conn.execute("SELECT COUNT(*) as count FROM alerts")
            row = await cursor.fetchone()
            return row["count"] if row else 0


class ExecutionRepository:
    """Repository for order executions."""

    MAX_EXECUTIONS = 50  # Keep last 50 executions

    @staticmethod
    async def insert(
        timestamp: str,
        market: str,
        status: str,
        yes_order_id: Optional[str] = None,
        yes_status: Optional[str] = None,
        yes_price: Optional[float] = None,
        yes_size: Optional[float] = None,
        yes_filled_size: Optional[float] = None,
        yes_error: Optional[str] = None,
        no_order_id: Optional[str] = None,
        no_status: Optional[str] = None,
        no_price: Optional[float] = None,
        no_size: Optional[float] = None,
        no_filled_size: Optional[float] = None,
        no_error: Optional[str] = None,
        total_cost: float = 0.0,
        expected_profit: float = 0.0,
        profit_pct: Optional[float] = None,
        market_liquidity: Optional[float] = None,
        timing_data: Optional[str] = None,
    ) -> int:
        """Insert a new execution record."""
        async with get_async_db() as conn:
            cursor = await conn.execute(
                """
                INSERT INTO executions (
                    timestamp, market, status,
                    yes_order_id, yes_status, yes_price, yes_size, yes_filled_size, yes_error,
                    no_order_id, no_status, no_price, no_size, no_filled_size, no_error,
                    total_cost, expected_profit, profit_pct, market_liquidity, timing_data
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    timestamp, market, status,
                    yes_order_id, yes_status, yes_price, yes_size, yes_filled_size, yes_error,
                    no_order_id, no_status, no_price, no_size, no_filled_size, no_error,
                    total_cost, expected_profit, profit_pct, market_liquidity, timing_data
                ),
            )
            exec_id = cursor.lastrowid or 0

            # Cleanup old executions
            await conn.execute(
                """
                DELETE FROM executions WHERE id NOT IN (
                    SELECT id FROM executions ORDER BY id DESC LIMIT ?
                )
                """,
                (ExecutionRepository.MAX_EXECUTIONS,),
            )

            # Update execution stats
            await conn.execute(
                """
                UPDATE execution_stats SET
                    total_attempts = total_attempts + 1,
                    successful = successful + CASE WHEN ? = 'filled' THEN 1 ELSE 0 END,
                    partial = partial + CASE WHEN ? = 'partial' THEN 1 ELSE 0 END,
                    failed = failed + CASE WHEN ? = 'failed' THEN 1 ELSE 0 END,
                    cancelled = cancelled + CASE WHEN ? = 'cancelled' THEN 1 ELSE 0 END,
                    total_volume = total_volume + ?,
                    total_profit = total_profit + CASE WHEN ? = 'filled' THEN ? ELSE 0 END
                WHERE id = 1
                """,
                (status, status, status, status, total_cost, status, expected_profit),
            )

            return exec_id

    @staticmethod
    async def get_recent(limit: int = 20) -> list[dict[str, Any]]:
        """Get recent executions."""
        async with get_async_db() as conn:
            cursor = await conn.execute(
                "SELECT * FROM executions ORDER BY id DESC LIMIT ?",
                (limit,),
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    @staticmethod
    async def get_stats() -> dict[str, Any]:
        """Get execution statistics."""
        async with get_async_db() as conn:
            cursor = await conn.execute(
                "SELECT * FROM execution_stats WHERE id = 1"
            )
            row = await cursor.fetchone()
            if row:
                stats = dict(row)
                total = stats.get("total_attempts", 0)
                stats["success_rate"] = (
                    stats.get("successful", 0) / total if total > 0 else 0.0
                )
                return stats
            return {}


class StatsRepository:
    """Repository for scanner stats."""

    @staticmethod
    async def update(
        markets: int = 0,
        price_updates: int = 0,
        arbitrage_alerts: int = 0,
        ws_connected: bool = False,
        ws_connections: str = "",
        subscribed_tokens: int = 0,
    ) -> None:
        """Update scanner stats (upsert)."""
        async with get_async_db() as conn:
            await conn.execute(
                """
                UPDATE scanner_stats SET
                    markets = ?,
                    price_updates = ?,
                    arbitrage_alerts = ?,
                    ws_connected = ?,
                    ws_connections = ?,
                    subscribed_tokens = ?,
                    last_update = ?
                WHERE id = 1
                """,
                (
                    markets, price_updates, arbitrage_alerts,
                    1 if ws_connected else 0, ws_connections,
                    subscribed_tokens, datetime.now(timezone.utc).timestamp()
                ),
            )

    @staticmethod
    async def get() -> dict[str, Any]:
        """Get current scanner stats."""
        async with get_async_db() as conn:
            cursor = await conn.execute(
                "SELECT * FROM scanner_stats WHERE id = 1"
            )
            row = await cursor.fetchone()
            if row:
                stats = dict(row)
                stats["ws_connected"] = bool(stats.get("ws_connected", 0))
                return stats
            return {}


class PortfolioRepository:
    """Repository for portfolio snapshots."""

    @staticmethod
    async def insert(
        timestamp: str,
        polymarket_usdc: float,
        total_usd: float,
        positions_value: float = 0.0,
    ) -> int:
        """Insert a new portfolio snapshot."""
        async with get_async_db() as conn:
            cursor = await conn.execute(
                """
                INSERT INTO portfolio_snapshots (
                    timestamp, polymarket_usdc, total_usd, positions_value
                ) VALUES (?, ?, ?, ?)
                """,
                (timestamp, polymarket_usdc, total_usd, positions_value),
            )
            return cursor.lastrowid or 0

    @staticmethod
    async def get_recent(limit: int = 100) -> list[dict[str, Any]]:
        """Get recent portfolio snapshots."""
        async with get_async_db() as conn:
            cursor = await conn.execute(
                "SELECT * FROM portfolio_snapshots ORDER BY id DESC LIMIT ?",
                (limit,),
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    @staticmethod
    async def get_profit_loss(since: str) -> dict[str, Any]:
        """Calculate profit/loss since a given timestamp."""
        async with get_async_db() as conn:
            # Get earliest snapshot after 'since'
            cursor = await conn.execute(
                """
                SELECT * FROM portfolio_snapshots
                WHERE timestamp >= ?
                ORDER BY timestamp ASC LIMIT 1
                """,
                (since,),
            )
            start_row = await cursor.fetchone()

            # Get latest snapshot
            cursor = await conn.execute(
                "SELECT * FROM portfolio_snapshots ORDER BY id DESC LIMIT 1"
            )
            end_row = await cursor.fetchone()

            if start_row and end_row:
                start = dict(start_row)
                end = dict(end_row)
                return {
                    "start_balance": start.get("total_usd", 0),
                    "end_balance": end.get("total_usd", 0),
                    "profit_loss": end.get("total_usd", 0) - start.get("total_usd", 0),
                    "start_time": start.get("timestamp"),
                    "end_time": end.get("timestamp"),
                }
            return {}


class ClosedPositionRepository:
    """Repository for closed position history."""

    @staticmethod
    async def insert(
        timestamp: str,
        market_title: Optional[str] = None,
        outcome: Optional[str] = None,
        token_id: Optional[str] = None,
        condition_id: Optional[str] = None,
        size: float = 0.0,
        avg_price: Optional[float] = None,
        exit_price: Optional[float] = None,
        cost_basis: Optional[float] = None,
        realized_value: Optional[float] = None,
        realized_pnl: Optional[float] = None,
        status: Optional[str] = None,
        redeemed: bool = False,
    ) -> int:
        """Insert a closed position record."""
        async with get_async_db() as conn:
            cursor = await conn.execute(
                """
                INSERT INTO closed_positions (
                    timestamp, market_title, outcome, token_id, condition_id,
                    size, avg_price, exit_price, cost_basis, realized_value,
                    realized_pnl, status, redeemed
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    timestamp, market_title, outcome, token_id, condition_id,
                    size, avg_price, exit_price, cost_basis, realized_value,
                    realized_pnl, status, 1 if redeemed else 0
                ),
            )
            return cursor.lastrowid or 0

    @staticmethod
    async def exists(token_id: str) -> bool:
        """Check if a closed position already exists."""
        async with get_async_db() as conn:
            cursor = await conn.execute(
                "SELECT 1 FROM closed_positions WHERE token_id = ? LIMIT 1",
                (token_id,),
            )
            row = await cursor.fetchone()
            return row is not None

    @staticmethod
    async def get_recent(
        limit: int = 50,
        offset: int = 0,
        redeemed: Optional[bool] = None,
    ) -> list[dict[str, Any]]:
        """Get recent closed positions with pagination and optional redeemed filter."""
        async with get_async_db() as conn:
            if redeemed is not None:
                cursor = await conn.execute(
                    "SELECT * FROM closed_positions WHERE redeemed = ? ORDER BY timestamp DESC LIMIT ? OFFSET ?",
                    (1 if redeemed else 0, limit, offset),
                )
            else:
                cursor = await conn.execute(
                    "SELECT * FROM closed_positions ORDER BY timestamp DESC LIMIT ? OFFSET ?",
                    (limit, offset),
                )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    @staticmethod
    async def get_total_count(redeemed: Optional[bool] = None) -> int:
        """Get total count of closed positions with optional redeemed filter."""
        async with get_async_db() as conn:
            if redeemed is not None:
                cursor = await conn.execute(
                    "SELECT COUNT(*) as count FROM closed_positions WHERE redeemed = ?",
                    (1 if redeemed else 0,),
                )
            else:
                cursor = await conn.execute("SELECT COUNT(*) as count FROM closed_positions")
            row = await cursor.fetchone()
            return row["count"] if row else 0

    @staticmethod
    async def get_profit_summary() -> dict[str, Any]:
        """Get summary of realized P&L from closed positions."""
        async with get_async_db() as conn:
            cursor = await conn.execute(
                """
                SELECT
                    COUNT(*) as total_positions,
                    SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as winning_positions,
                    SUM(CASE WHEN realized_pnl < 0 THEN 1 ELSE 0 END) as losing_positions,
                    SUM(realized_pnl) as total_realized_pnl,
                    SUM(cost_basis) as total_cost_basis,
                    SUM(realized_value) as total_realized_value
                FROM closed_positions
                """
            )
            row = await cursor.fetchone()
            return dict(row) if row else {}


class NearMissAlertRepository:
    """Repository for near-miss (illiquid) arbitrage alerts."""

    MAX_ALERTS = 100  # Keep last 100 near-miss alerts

    @staticmethod
    async def insert(
        timestamp: str,
        market: str,
        yes_ask: float,
        no_ask: float,
        combined: float,
        profit_pct: float,
        yes_liquidity: float,
        no_liquidity: float,
        min_required: float,
        reason: str = "insufficient_liquidity",
    ) -> int:
        """Insert a new near-miss alert and cleanup old ones."""
        async with get_async_db() as conn:
            cursor = await conn.execute(
                """
                INSERT INTO near_miss_alerts (
                    timestamp, market, yes_ask, no_ask, combined, profit_pct,
                    yes_liquidity, no_liquidity, min_required, reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    timestamp, market, yes_ask, no_ask, combined, profit_pct,
                    yes_liquidity, no_liquidity, min_required, reason
                ),
            )
            alert_id = cursor.lastrowid or 0

            # Cleanup old alerts (keep last MAX_ALERTS)
            await conn.execute(
                """
                DELETE FROM near_miss_alerts WHERE id NOT IN (
                    SELECT id FROM near_miss_alerts ORDER BY id DESC LIMIT ?
                )
                """,
                (NearMissAlertRepository.MAX_ALERTS,),
            )

            return alert_id

    @staticmethod
    async def get_recent(limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        """Get recent near-miss alerts with pagination."""
        async with get_async_db() as conn:
            cursor = await conn.execute(
                "SELECT * FROM near_miss_alerts ORDER BY id DESC LIMIT ? OFFSET ?",
                (limit, offset),
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    @staticmethod
    async def get_total_count() -> int:
        """Get total count of near-miss alerts."""
        async with get_async_db() as conn:
            cursor = await conn.execute("SELECT COUNT(*) as count FROM near_miss_alerts")
            row = await cursor.fetchone()
            return row["count"] if row else 0
