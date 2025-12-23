"""SQLite database management for karb with async support.

Uses aiosqlite for non-blocking database operations to avoid
interfering with critical trading execution.
"""

import asyncio
import sqlite3
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import AsyncGenerator, Generator, Optional

import aiosqlite

from karb.utils.logging import get_logger

log = get_logger(__name__)

# Default database path
DEFAULT_DB_PATH = Path.home() / ".karb" / "karb.db"

# Global database path (can be overridden for testing)
_db_path: Optional[Path] = None

# Async connection pool (simple single connection for now)
_async_conn: Optional[aiosqlite.Connection] = None
_async_lock = asyncio.Lock()


def set_db_path(path: Path) -> None:
    """Set the database path (useful for testing)."""
    global _db_path
    _db_path = path


def get_db_path() -> Path:
    """Get the current database path."""
    return _db_path or DEFAULT_DB_PATH


# =============================================================================
# Async API (for use in asyncio contexts - preferred for trading operations)
# =============================================================================

async def get_async_connection() -> aiosqlite.Connection:
    """Get or create the async database connection."""
    global _async_conn

    async with _async_lock:
        if _async_conn is None:
            db_path = get_db_path()
            db_path.parent.mkdir(parents=True, exist_ok=True)
            _async_conn = await aiosqlite.connect(
                str(db_path),
                timeout=30.0,
            )
            # Enable WAL mode for better concurrency
            await _async_conn.execute("PRAGMA journal_mode = WAL")
            await _async_conn.execute("PRAGMA synchronous = NORMAL")
            _async_conn.row_factory = aiosqlite.Row
            log.debug("Async database connection established", path=str(db_path))
        return _async_conn


@asynccontextmanager
async def get_async_db() -> AsyncGenerator[aiosqlite.Connection, None]:
    """Async context manager for database access."""
    conn = await get_async_connection()
    try:
        yield conn
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise


async def close_async_db() -> None:
    """Close the async database connection."""
    global _async_conn
    async with _async_lock:
        if _async_conn is not None:
            await _async_conn.close()
            _async_conn = None
            log.debug("Async database connection closed")


async def init_async_db() -> None:
    """Initialize the database schema asynchronously."""
    async with get_async_db() as conn:
        await conn.executescript(_get_schema())
        log.info("Database schema initialized (async)", path=str(get_db_path()))
    # Run migrations to add any new columns to existing tables
    await run_async_migrations()


# =============================================================================
# Sync API (for use in synchronous contexts like migrations or CLI)
# =============================================================================

@contextmanager
def get_db() -> Generator[sqlite3.Connection, None, None]:
    """Sync context manager for database access."""
    db_path = get_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(
        str(db_path),
        timeout=30.0,
    )
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.row_factory = sqlite3.Row

    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    """Initialize the database schema synchronously."""
    with get_db() as conn:
        conn.executescript(_get_schema())
        log.info("Database schema initialized", path=str(get_db_path()))


# =============================================================================
# Schema
# =============================================================================

def _get_schema() -> str:
    """Return the database schema as a string."""
    return """
-- Trades table (replaces trades.jsonl)
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    platform TEXT NOT NULL,
    market_id TEXT,
    market_name TEXT,
    side TEXT NOT NULL,
    outcome TEXT NOT NULL,
    price REAL NOT NULL,
    size REAL NOT NULL,
    cost REAL NOT NULL,
    order_id TEXT,
    strategy TEXT,
    profit_expected REAL,
    notes TEXT
);

-- Portfolio snapshots (replaces portfolio.jsonl)
CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    polymarket_usdc REAL NOT NULL,
    total_usd REAL NOT NULL,
    positions_value REAL DEFAULT 0
);

-- Arbitrage alerts (replaces scanner_alerts.json)
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market TEXT NOT NULL,
    yes_ask REAL NOT NULL,
    no_ask REAL NOT NULL,
    combined REAL NOT NULL,
    profit REAL NOT NULL,
    timestamp TEXT NOT NULL,
    platform TEXT DEFAULT 'polymarket',
    days_until_resolution INTEGER,
    resolution_date TEXT,
    first_seen TEXT,
    duration_secs REAL
);

-- Order executions (replaces orders.json recent_executions)
CREATE TABLE IF NOT EXISTS executions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    market TEXT NOT NULL,
    status TEXT NOT NULL,
    yes_order_id TEXT,
    yes_status TEXT,
    yes_price REAL,
    yes_size REAL,
    yes_filled_size REAL,
    yes_error TEXT,
    no_order_id TEXT,
    no_status TEXT,
    no_price REAL,
    no_size REAL,
    no_filled_size REAL,
    no_error TEXT,
    total_cost REAL DEFAULT 0,
    expected_profit REAL DEFAULT 0,
    profit_pct REAL,
    market_liquidity REAL,
    timing_data TEXT
);

-- Scanner stats (replaces scanner_stats.json) - single row, updated in place
CREATE TABLE IF NOT EXISTS scanner_stats (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    markets INTEGER DEFAULT 0,
    price_updates INTEGER DEFAULT 0,
    arbitrage_alerts INTEGER DEFAULT 0,
    ws_connected INTEGER DEFAULT 0,
    ws_connections TEXT,
    subscribed_tokens INTEGER DEFAULT 0,
    last_update REAL
);

-- Execution stats (aggregate stats)
CREATE TABLE IF NOT EXISTS execution_stats (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    total_attempts INTEGER DEFAULT 0,
    successful INTEGER DEFAULT 0,
    partial INTEGER DEFAULT 0,
    failed INTEGER DEFAULT 0,
    cancelled INTEGER DEFAULT 0,
    total_volume REAL DEFAULT 0,
    total_profit REAL DEFAULT 0
);

-- Initialize singleton rows
INSERT OR IGNORE INTO scanner_stats (id) VALUES (1);
INSERT OR IGNORE INTO execution_stats (id) VALUES (1);

-- Closed positions history (for tracking resolved positions)
CREATE TABLE IF NOT EXISTS closed_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    market_title TEXT,
    outcome TEXT,
    token_id TEXT,
    condition_id TEXT,
    size REAL NOT NULL,
    avg_price REAL,
    exit_price REAL,
    cost_basis REAL,
    realized_value REAL,
    realized_pnl REAL,
    status TEXT,
    redeemed INTEGER DEFAULT 0
);

-- Near-miss alerts (good arbs skipped due to insufficient liquidity)
CREATE TABLE IF NOT EXISTS near_miss_alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    market TEXT NOT NULL,
    yes_ask REAL NOT NULL,
    no_ask REAL NOT NULL,
    combined REAL NOT NULL,
    profit_pct REAL NOT NULL,
    yes_liquidity REAL NOT NULL,
    no_liquidity REAL NOT NULL,
    min_required REAL NOT NULL,
    reason TEXT DEFAULT 'insufficient_liquidity'
);

-- Indexes for common queries
CREATE INDEX IF NOT EXISTS idx_trades_timestamp ON trades(timestamp);
CREATE INDEX IF NOT EXISTS idx_trades_platform ON trades(platform);
CREATE INDEX IF NOT EXISTS idx_alerts_timestamp ON alerts(timestamp);
CREATE INDEX IF NOT EXISTS idx_executions_timestamp ON executions(timestamp);
CREATE INDEX IF NOT EXISTS idx_snapshots_timestamp ON portfolio_snapshots(timestamp);
CREATE INDEX IF NOT EXISTS idx_closed_positions_timestamp ON closed_positions(timestamp);
CREATE INDEX IF NOT EXISTS idx_near_miss_alerts_timestamp ON near_miss_alerts(timestamp);
"""


def _get_migrations() -> list[str]:
    """Return migration SQL statements for adding new columns to existing tables.

    These use 'ALTER TABLE ... ADD COLUMN' with error handling since
    SQLite will raise an error if the column already exists.
    """
    return [
        # Add profit_pct to executions (added for arb % visibility)
        "ALTER TABLE executions ADD COLUMN profit_pct REAL",
        # Add market_liquidity to executions (added for liquidity visibility)
        "ALTER TABLE executions ADD COLUMN market_liquidity REAL",
        # Add timing_data to executions (added for latency tracking)
        "ALTER TABLE executions ADD COLUMN timing_data TEXT",
    ]


async def run_async_migrations() -> None:
    """Run migrations to add new columns to existing tables (async)."""
    async with get_async_db() as conn:
        for migration in _get_migrations():
            try:
                await conn.execute(migration)
                log.debug("Migration applied", sql=migration[:50])
            except Exception:
                # Column likely already exists, ignore
                pass


def run_migrations() -> None:
    """Run migrations to add new columns to existing tables (sync)."""
    with get_db() as conn:
        for migration in _get_migrations():
            try:
                conn.execute(migration)
                log.debug("Migration applied", sql=migration[:50])
            except Exception:
                # Column likely already exists, ignore
                pass
