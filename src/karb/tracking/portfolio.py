"""Portfolio and balance tracking."""

import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Optional

from karb.config import get_settings
from karb.utils.logging import get_logger

log = get_logger(__name__)


@dataclass
class BalanceSnapshot:
    """A point-in-time balance snapshot."""
    timestamp: str
    polymarket_usdc: float
    kalshi_usd: float
    total_usd: float
    positions_value: float = 0.0  # Value of open positions


class PortfolioTracker:
    """Track balances and portfolio value over time."""

    def __init__(self, data_path: Optional[Path] = None) -> None:
        if data_path is None:
            data_path = Path.home() / ".karb" / "portfolio.jsonl"

        self.data_path = data_path
        self.data_path.parent.mkdir(parents=True, exist_ok=True)

    async def get_current_balances(self) -> dict:
        """Fetch current balances from all platforms."""
        settings = get_settings()
        balances = {
            "timestamp": datetime.now().isoformat(),
            "polymarket_usdc": 0.0,
            "kalshi_usd": 0.0,
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
                total_usdc = 0
                for addr in [USDC_BRIDGED, USDC_NATIVE]:
                    usdc = w3.eth.contract(address=Web3.to_checksum_address(addr), abi=USDC_ABI)
                    balance = usdc.functions.balanceOf(wallet).call()
                    total_usdc += balance / 1e6  # USDC has 6 decimals

                balances["polymarket_usdc"] = total_usdc
            except Exception as e:
                log.debug("Failed to get Polymarket balance", error=str(e))

        # Get Kalshi balance
        if settings.is_kalshi_enabled():
            try:
                from karb.api.kalshi import KalshiClient
                async with KalshiClient() as client:
                    balance = await client.get_balance()
                    balances["kalshi_usd"] = float(balance)
            except Exception as e:
                log.debug("Failed to get Kalshi balance", error=str(e))

        balances["total_usd"] = balances["polymarket_usdc"] + balances["kalshi_usd"]
        return balances

    async def get_positions(self) -> dict:
        """Get open positions on all platforms."""
        settings = get_settings()
        positions = {
            "polymarket": [],
            "kalshi": [],
        }

        # Polymarket positions would require additional API implementation
        # Kalshi positions can be fetched from their portfolio API

        return positions

    def record_snapshot(self, snapshot: BalanceSnapshot) -> None:
        """Record a balance snapshot."""
        with open(self.data_path, "a") as f:
            data = {
                "timestamp": snapshot.timestamp,
                "polymarket_usdc": snapshot.polymarket_usdc,
                "kalshi_usd": snapshot.kalshi_usd,
                "total_usd": snapshot.total_usd,
                "positions_value": snapshot.positions_value,
            }
            f.write(json.dumps(data) + "\n")

    def get_snapshots(self, limit: int = 100) -> list[BalanceSnapshot]:
        """Get recent balance snapshots."""
        if not self.data_path.exists():
            return []

        snapshots = []
        with open(self.data_path) as f:
            for line in f:
                try:
                    data = json.loads(line.strip())
                    snapshots.append(BalanceSnapshot(**data))
                except (json.JSONDecodeError, TypeError):
                    continue

        return sorted(snapshots, key=lambda s: s.timestamp, reverse=True)[:limit]

    def get_profit_loss(self, since: Optional[datetime] = None) -> dict:
        """Calculate profit/loss over a period."""
        snapshots = self.get_snapshots(limit=1000)

        if not snapshots:
            return {"error": "No balance history"}

        if since:
            snapshots = [
                s for s in snapshots
                if datetime.fromisoformat(s.timestamp) >= since
            ]

        if len(snapshots) < 2:
            return {"error": "Not enough data points"}

        # Most recent and oldest in period
        current = snapshots[0]
        starting = snapshots[-1]

        pnl = current.total_usd - starting.total_usd
        pnl_pct = (pnl / starting.total_usd * 100) if starting.total_usd > 0 else 0

        return {
            "starting_balance": starting.total_usd,
            "current_balance": current.total_usd,
            "pnl_usd": pnl,
            "pnl_pct": pnl_pct,
            "period_start": starting.timestamp,
            "period_end": current.timestamp,
        }
