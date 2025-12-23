"""Portfolio and balance tracking."""

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from karb.config import get_settings
from karb.data.repositories import PortfolioRepository
from karb.utils.logging import get_logger

log = get_logger(__name__)


@dataclass
class BalanceSnapshot:
    """A point-in-time balance snapshot."""
    timestamp: str
    polymarket_usdc: float
    total_usd: float
    positions_value: float = 0.0  # Value of open positions


class PortfolioTracker:
    """Track balances and portfolio value over time using SQLite."""

    def __init__(self) -> None:
        """Initialize portfolio tracker."""
        pass  # No file path needed, uses database

    async def get_current_balances(self) -> dict[str, Any]:
        """Fetch current balances from Polymarket."""
        settings = get_settings()
        balances: dict[str, Any] = {
            "timestamp": datetime.now().isoformat(),
            "polymarket_usdc": 0.0,
            "total_usd": 0.0,
        }

        # Get Polymarket USDC balance (on-chain on Polygon)
        if settings.wallet_address:
            try:
                from web3 import Web3

                # Both USDC contracts on Polygon
                USDC_BRIDGED = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"  # Bridged USDC
                USDC_NATIVE = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"   # Native USDC
                USDC_ABI = [{"constant":True,"inputs":[{"name":"account","type":"address"}],"name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"type":"function"}]

                w3 = Web3(Web3.HTTPProvider(settings.polygon_rpc_url))
                wallet = Web3.to_checksum_address(settings.wallet_address)

                # Check both USDC contracts
                total_usdc = 0.0
                for addr in [USDC_BRIDGED, USDC_NATIVE]:
                    usdc = w3.eth.contract(address=Web3.to_checksum_address(addr), abi=USDC_ABI)
                    balance = usdc.functions.balanceOf(wallet).call()
                    total_usdc += balance / 1e6  # USDC has 6 decimals

                balances["polymarket_usdc"] = total_usdc
            except Exception as e:
                log.error("Failed to get balances", error=str(e))

        balances["total_usd"] = balances["polymarket_usdc"]
        return balances

    async def get_positions(self) -> dict[str, Any]:
        """Get open positions on Polymarket."""
        positions: dict[str, Any] = {
            "polymarket": [],
        }
        # Polymarket positions would require additional API implementation
        return positions

    def record_snapshot(self, snapshot: BalanceSnapshot) -> None:
        """Record a balance snapshot (non-blocking).

        This schedules an async database write without blocking.
        """
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._record_snapshot_async(snapshot))
        except RuntimeError:
            log.debug("No event loop, skipping database snapshot")

    async def _record_snapshot_async(self, snapshot: BalanceSnapshot) -> None:
        """Async implementation of snapshot recording."""
        try:
            await PortfolioRepository.insert(
                timestamp=snapshot.timestamp,
                polymarket_usdc=snapshot.polymarket_usdc,
                total_usd=snapshot.total_usd,
                positions_value=snapshot.positions_value,
            )
        except Exception as e:
            log.debug("Failed to record snapshot to database", error=str(e))

    async def record_snapshot_async(self, snapshot: BalanceSnapshot) -> None:
        """Record a balance snapshot asynchronously."""
        await self._record_snapshot_async(snapshot)

    async def get_snapshots(self, limit: int = 100) -> list[BalanceSnapshot]:
        """Get recent balance snapshots from database."""
        rows = await PortfolioRepository.get_recent(limit=limit)
        return [
            BalanceSnapshot(
                timestamp=row.get("timestamp", ""),
                polymarket_usdc=row.get("polymarket_usdc", 0.0),
                total_usd=row.get("total_usd", 0.0),
                positions_value=row.get("positions_value", 0.0),
            )
            for row in rows
        ]

    async def get_profit_loss(self, since: Optional[datetime] = None) -> dict[str, Any]:
        """Calculate profit/loss over a period."""
        since_str = since.isoformat() if since else None
        result = await PortfolioRepository.get_profit_loss(since_str or "1970-01-01")

        if not result or not result.get("start_balance"):
            return {"error": "Not enough data points"}

        start_balance = result.get("start_balance", 0)
        end_balance = result.get("end_balance", 0)
        pnl = result.get("profit_loss", 0)
        pnl_pct = (pnl / start_balance * 100) if start_balance > 0 else 0

        return {
            "starting_balance": start_balance,
            "current_balance": end_balance,
            "pnl_usd": pnl,
            "pnl_pct": pnl_pct,
            "period_start": result.get("start_time"),
            "period_end": result.get("end_time"),
        }
