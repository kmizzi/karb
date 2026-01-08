"""Repository classes for database operations.

All repository methods are async to avoid blocking the trading loop.
"""

from datetime import datetime, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

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

    @staticmethod
    async def get_daily_counts(days: int = 7) -> list[dict[str, Any]]:
        """Get daily alert counts for charts."""
        async with get_async_db() as conn:
            cursor = await conn.execute(
                """
                SELECT
                    DATE(timestamp) as date,
                    COUNT(*) as count
                FROM alerts
                WHERE timestamp >= DATE('now', ? || ' days')
                GROUP BY DATE(timestamp)
                ORDER BY date ASC
                """,
                (f"-{days}",),
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    @staticmethod
    async def update_duration(market: str, duration_secs: float) -> bool:
        """Update duration_secs for the most recent alert of a market.

        Called when an opportunity closes to record actual window lifetime.
        Returns True if an alert was updated.
        """
        async with get_async_db() as conn:
            # Find and update the most recent alert for this market that has no duration
            cursor = await conn.execute(
                """
                UPDATE alerts SET duration_secs = ?
                WHERE id = (
                    SELECT id FROM alerts
                    WHERE market = ? AND (duration_secs IS NULL OR duration_secs = 0)
                    ORDER BY id DESC LIMIT 1
                )
                """,
                (round(duration_secs, 3), market),
            )
            return cursor.rowcount > 0

    @staticmethod
    async def get_window_stats() -> dict[str, Any]:
        """Get arb window duration statistics (how long opportunities last)."""
        async with get_async_db() as conn:
            cursor = await conn.execute(
                """
                SELECT
                    COUNT(*) as total_alerts,
                    COUNT(duration_secs) as alerts_with_duration,
                    AVG(duration_secs) as avg_duration_secs,
                    MIN(duration_secs) as min_duration_secs,
                    MAX(duration_secs) as max_duration_secs,
                    AVG(CASE WHEN duration_secs < 60 THEN duration_secs END) as avg_under_60s,
                    COUNT(CASE WHEN duration_secs < 1 THEN 1 END) as under_1s,
                    COUNT(CASE WHEN duration_secs >= 1 AND duration_secs < 5 THEN 1 END) as between_1_5s,
                    COUNT(CASE WHEN duration_secs >= 5 AND duration_secs < 30 THEN 1 END) as between_5_30s,
                    COUNT(CASE WHEN duration_secs >= 30 AND duration_secs < 60 THEN 1 END) as between_30_60s,
                    COUNT(CASE WHEN duration_secs >= 60 THEN 1 END) as over_60s
                FROM alerts
                WHERE duration_secs IS NOT NULL
                """
            )
            row = await cursor.fetchone()
            if row:
                return {
                    "total_alerts": row["total_alerts"] or 0,
                    "alerts_with_duration": row["alerts_with_duration"] or 0,
                    "avg_duration_secs": round(row["avg_duration_secs"] or 0, 1),
                    "min_duration_secs": round(row["min_duration_secs"] or 0, 1),
                    "max_duration_secs": round(row["max_duration_secs"] or 0, 1),
                    "avg_under_60s": round(row["avg_under_60s"] or 0, 1),
                    "distribution": {
                        "under_1s": row["under_1s"] or 0,
                        "1_to_5s": row["between_1_5s"] or 0,
                        "5_to_30s": row["between_5_30s"] or 0,
                        "30_to_60s": row["between_30_60s"] or 0,
                        "over_60s": row["over_60s"] or 0,
                    },
                }
            return {
                "total_alerts": 0,
                "alerts_with_duration": 0,
                "avg_duration_secs": 0,
                "min_duration_secs": 0,
                "max_duration_secs": 0,
                "avg_under_60s": 0,
                "distribution": {
                    "under_1s": 0,
                    "1_to_5s": 0,
                    "5_to_30s": 0,
                    "30_to_60s": 0,
                    "over_60s": 0,
                },
            }


class ExecutionRepository:
    """Repository for order executions."""

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
        yes_liquidity: Optional[float] = None,
        no_liquidity: Optional[float] = None,
    ) -> int:
        """Insert a new execution record."""
        async with get_async_db() as conn:
            cursor = await conn.execute(
                """
                INSERT INTO executions (
                    timestamp, market, status,
                    yes_order_id, yes_status, yes_price, yes_size, yes_filled_size, yes_error,
                    no_order_id, no_status, no_price, no_size, no_filled_size, no_error,
                    total_cost, expected_profit, profit_pct, market_liquidity, timing_data,
                    yes_liquidity, no_liquidity
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    timestamp, market, status,
                    yes_order_id, yes_status, yes_price, yes_size, yes_filled_size, yes_error,
                    no_order_id, no_status, no_price, no_size, no_filled_size, no_error,
                    total_cost, expected_profit, profit_pct, market_liquidity, timing_data,
                    yes_liquidity, no_liquidity
                ),
            )
            exec_id = cursor.lastrowid or 0

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
    async def get_recent(limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        """Get recent executions with pagination."""
        async with get_async_db() as conn:
            cursor = await conn.execute(
                "SELECT * FROM executions ORDER BY id DESC LIMIT ? OFFSET ?",
                (limit, offset),
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

    # PST timezone for daily reset
    PST = ZoneInfo("America/Los_Angeles")

    @staticmethod
    def _get_pst_date() -> str:
        """Get current date in PST timezone as YYYY-MM-DD string."""
        return datetime.now(StatsRepository.PST).strftime("%Y-%m-%d")

    @staticmethod
    async def check_daily_reset(
        current_price_updates: int = 0,
        current_arbitrage_alerts: int = 0,
    ) -> bool:
        """Check if we need to reset baselines for a new day (PST). Returns True if reset was performed."""
        async with get_async_db() as conn:
            cursor = await conn.execute(
                "SELECT last_reset_date, price_updates, arbitrage_alerts FROM scanner_stats WHERE id = 1"
            )
            row = await cursor.fetchone()
            current_date = StatsRepository._get_pst_date()

            if row and row["last_reset_date"] != current_date:
                # New day - update baselines to current cumulative values
                # This way: daily = cumulative - baseline
                await conn.execute(
                    """
                    UPDATE scanner_stats SET
                        price_updates_baseline = ?,
                        arbitrage_alerts_baseline = ?,
                        last_reset_date = ?
                    WHERE id = 1
                    """,
                    (current_price_updates, current_arbitrage_alerts, current_date),
                )
                log.info("Daily stats baseline reset (PST)", date=current_date)
                return True
            elif row and row["last_reset_date"] is None:
                # First time - set baselines and date
                await conn.execute(
                    """
                    UPDATE scanner_stats SET
                        price_updates_baseline = ?,
                        arbitrage_alerts_baseline = ?,
                        last_reset_date = ?
                    WHERE id = 1
                    """,
                    (current_price_updates, current_arbitrage_alerts, current_date),
                )
            return False

    @staticmethod
    async def update(
        markets: int = 0,
        price_updates: int = 0,
        arbitrage_alerts: int = 0,
        ws_connected: bool = False,
        ws_connections: str = "",
        subscribed_tokens: int = 0,
    ) -> None:
        """Update scanner stats (upsert). Checks for daily reset first."""
        # Check for daily reset first (pass current values to set as new baselines)
        await StatsRepository.check_daily_reset(price_updates, arbitrage_alerts)

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
        """Get current scanner stats with daily values calculated."""
        async with get_async_db() as conn:
            cursor = await conn.execute(
                "SELECT * FROM scanner_stats WHERE id = 1"
            )
            row = await cursor.fetchone()
            if row:
                stats = dict(row)
                stats["ws_connected"] = bool(stats.get("ws_connected", 0))

                # Calculate daily values (cumulative - baseline)
                baseline_updates = stats.get("price_updates_baseline") or 0
                baseline_alerts = stats.get("arbitrage_alerts_baseline") or 0
                stats["price_updates_today"] = max(0, stats.get("price_updates", 0) - baseline_updates)
                stats["arbitrage_alerts_today"] = max(0, stats.get("arbitrage_alerts", 0) - baseline_alerts)

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

    # PST timezone for daily aggregation
    PST = ZoneInfo("America/Los_Angeles")

    @staticmethod
    async def get_daily_balances(days: int = 30) -> list[dict[str, Any]]:
        """Get daily balance snapshots (last snapshot per day in PST).

        Returns the last balance reading for each day, which represents
        the end-of-day balance. Includes USDC + positions value.
        """
        async with get_async_db() as conn:
            # SQLite doesn't have native timezone support, so we need to
            # adjust the timestamp to PST (-8 or -7 depending on DST).
            # For simplicity, use -8 hours (PST standard).
            # This groups by PST date and takes the last (max) snapshot per day.
            cursor = await conn.execute(
                """
                SELECT
                    DATE(timestamp, '-8 hours') as date,
                    MAX(id) as last_snapshot_id
                FROM portfolio_snapshots
                WHERE timestamp >= DATE('now', ? || ' days')
                GROUP BY DATE(timestamp, '-8 hours')
                ORDER BY date ASC
                """,
                (f"-{days}",),
            )
            date_ids = await cursor.fetchall()

            if not date_ids:
                return []

            # Fetch the actual snapshot data for each day's last snapshot
            result = []
            for row in date_ids:
                snapshot_id = row["last_snapshot_id"]
                cursor = await conn.execute(
                    """
                    SELECT timestamp, polymarket_usdc, total_usd, positions_value
                    FROM portfolio_snapshots WHERE id = ?
                    """,
                    (snapshot_id,),
                )
                snapshot = await cursor.fetchone()
                if snapshot:
                    result.append({
                        "date": row["date"],
                        "timestamp": snapshot["timestamp"],
                        "usdc": float(snapshot["polymarket_usdc"] or 0),
                        "positions_value": float(snapshot["positions_value"] or 0),
                        "total": float(snapshot["polymarket_usdc"] or 0) + float(snapshot["positions_value"] or 0),
                    })

            return result


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


class StatsHistoryRepository:
    """Repository for historical stats (hourly snapshots for charting)."""

    MAX_HOURS = 168  # Keep last 7 days of hourly data

    @staticmethod
    async def insert(
        timestamp: str,
        hour: str,
        markets: int = 0,
        price_updates: int = 0,
        arbitrage_alerts: int = 0,
        executions_attempted: int = 0,
        executions_filled: int = 0,
        ws_connected: bool = True,
    ) -> int:
        """Insert a new hourly stats snapshot."""
        async with get_async_db() as conn:
            # Check if we already have an entry for this hour
            cursor = await conn.execute(
                "SELECT id FROM stats_history WHERE hour = ?",
                (hour,),
            )
            existing = await cursor.fetchone()

            if existing:
                # Update existing entry
                await conn.execute(
                    """
                    UPDATE stats_history SET
                        timestamp = ?,
                        markets = ?,
                        price_updates = price_updates + ?,
                        arbitrage_alerts = arbitrage_alerts + ?,
                        executions_attempted = executions_attempted + ?,
                        executions_filled = executions_filled + ?,
                        ws_connected = ?
                    WHERE hour = ?
                    """,
                    (
                        timestamp, markets, price_updates, arbitrage_alerts,
                        executions_attempted, executions_filled,
                        1 if ws_connected else 0, hour
                    ),
                )
                return existing["id"]
            else:
                # Insert new entry
                cursor = await conn.execute(
                    """
                    INSERT INTO stats_history (
                        timestamp, hour, markets, price_updates, arbitrage_alerts,
                        executions_attempted, executions_filled, ws_connected
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        timestamp, hour, markets, price_updates, arbitrage_alerts,
                        executions_attempted, executions_filled, 1 if ws_connected else 0
                    ),
                )
                new_id = cursor.lastrowid or 0

                # Cleanup old entries (keep last MAX_HOURS)
                await conn.execute(
                    """
                    DELETE FROM stats_history WHERE id NOT IN (
                        SELECT id FROM stats_history ORDER BY hour DESC LIMIT ?
                    )
                    """,
                    (StatsHistoryRepository.MAX_HOURS,),
                )

                return new_id

    @staticmethod
    async def get_hourly(hours: int = 24, include_current: bool = True) -> list[dict[str, Any]]:
        """Get hourly stats for the last N hours (for charts).

        If include_current is True, appends the current scanner_stats as
        the most recent data point for live updates.
        """
        async with get_async_db() as conn:
            cursor = await conn.execute(
                """
                SELECT * FROM stats_history
                ORDER BY hour DESC
                LIMIT ?
                """,
                (hours,),
            )
            rows = await cursor.fetchall()
            result = [dict(row) for row in reversed(rows)]

            # Include current live stats as the latest data point
            if include_current:
                cursor = await conn.execute(
                    "SELECT * FROM scanner_stats WHERE id = 1"
                )
                current = await cursor.fetchone()
                if current:
                    from datetime import datetime, timezone
                    now = datetime.now(timezone.utc)
                    current_hour = now.strftime("%Y-%m-%d %H:00")

                    # Check if we already have this hour in history
                    if result and result[-1].get("hour") == current_hour:
                        # Update the last entry with current stats
                        result[-1]["markets"] = current["markets"]
                        result[-1]["price_updates"] = current["price_updates"]
                        result[-1]["ws_connected"] = current["ws_connected"]
                    else:
                        # Add as new entry for current hour
                        result.append({
                            "hour": current_hour,
                            "markets": current["markets"],
                            "price_updates": current["price_updates"],
                            "arbitrage_alerts": 0,
                            "executions_attempted": 0,
                            "executions_filled": 0,
                            "ws_connected": current["ws_connected"],
                        })

            return result

    @staticmethod
    async def get_daily_executions(days: int = 30) -> list[dict[str, Any]]:
        """Get daily execution stats from the executions table."""
        async with get_async_db() as conn:
            cursor = await conn.execute(
                """
                SELECT
                    DATE(timestamp) as date,
                    COUNT(*) as total_executions,
                    SUM(CASE WHEN status = 'filled' THEN 1 ELSE 0 END) as filled,
                    SUM(CASE WHEN status = 'partial' THEN 1 ELSE 0 END) as partial,
                    SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failed,
                    SUM(total_cost) as total_volume,
                    SUM(CASE WHEN status = 'filled' THEN expected_profit ELSE 0 END) as total_profit
                FROM executions
                WHERE timestamp >= DATE('now', ? || ' days')
                GROUP BY DATE(timestamp)
                ORDER BY date ASC
                """,
                (f"-{days}",),
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


class MinuteStatsRepository:
    """Repository for minute-level price update stats (for real-time charting)."""

    MAX_MINUTES = 120  # Keep last 2 hours of minute data

    @staticmethod
    async def insert(
        timestamp: str,
        minute: str,
        price_updates: int = 0,
        ws_connected: bool = True,
    ) -> int:
        """Insert or update minute stats."""
        async with get_async_db() as conn:
            # Upsert: insert or replace
            cursor = await conn.execute(
                """
                INSERT INTO price_updates_minute (timestamp, minute, price_updates, ws_connected)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(minute) DO UPDATE SET
                    timestamp = excluded.timestamp,
                    price_updates = excluded.price_updates,
                    ws_connected = excluded.ws_connected
                """,
                (timestamp, minute, price_updates, 1 if ws_connected else 0),
            )
            minute_id = cursor.lastrowid or 0

            # Cleanup old entries (keep last MAX_MINUTES)
            await conn.execute(
                """
                DELETE FROM price_updates_minute WHERE id NOT IN (
                    SELECT id FROM price_updates_minute ORDER BY minute DESC LIMIT ?
                )
                """,
                (MinuteStatsRepository.MAX_MINUTES,),
            )

            return minute_id

    @staticmethod
    async def get_recent(minutes: int = 60) -> list[dict[str, Any]]:
        """Get recent minute stats for charting."""
        async with get_async_db() as conn:
            # Filter out anomalous values (>20k/min suggests cumulative bug)
            cursor = await conn.execute(
                """
                SELECT * FROM price_updates_minute
                WHERE price_updates < 20000
                ORDER BY minute DESC
                LIMIT ?
                """,
                (minutes,),
            )
            rows = await cursor.fetchall()
            # Return in chronological order
            return [dict(row) for row in reversed(rows)]

    @staticmethod
    async def get_recent_with_current(minutes: int = 60) -> list[dict[str, Any]]:
        """Get recent minute stats including current live stats as latest point."""
        result = await MinuteStatsRepository.get_recent(minutes)

        # Add current scanner stats as the latest data point
        async with get_async_db() as conn:
            cursor = await conn.execute(
                "SELECT * FROM scanner_stats WHERE id = 1"
            )
            current = await cursor.fetchone()
            if current:
                from datetime import datetime, timezone
                now = datetime.now(timezone.utc)
                current_minute = now.strftime("%Y-%m-%d %H:%M")

                # Check if we already have this minute
                if result and result[-1].get("minute") == current_minute:
                    # Update the last entry with current live stats
                    result[-1]["price_updates"] = current["price_updates"]
                    result[-1]["ws_connected"] = current["ws_connected"]
                else:
                    # Add as new entry
                    result.append({
                        "minute": current_minute,
                        "price_updates": current["price_updates"],
                        "ws_connected": current["ws_connected"],
                    })

        return result


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

    @staticmethod
    async def get_daily_counts(days: int = 7) -> list[dict[str, Any]]:
        """Get daily near-miss alert counts for charts."""
        async with get_async_db() as conn:
            cursor = await conn.execute(
                """
                SELECT
                    DATE(timestamp) as date,
                    COUNT(*) as count
                FROM near_miss_alerts
                WHERE timestamp >= DATE('now', ? || ' days')
                GROUP BY DATE(timestamp)
                ORDER BY date ASC
                """,
                (f"-{days}",),
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]
