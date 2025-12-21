"""Arbitrage opportunity detection and analysis."""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Optional

from karb.api.models import ArbitrageOpportunity
from karb.config import get_settings
from karb.scanner.market_scanner import MarketSnapshot
from karb.utils.logging import get_logger

log = get_logger(__name__)


@dataclass
class AnalyzerConfig:
    """Configuration for the arbitrage analyzer."""

    # Minimum profit threshold (e.g., 0.005 = 0.5%)
    min_profit_threshold: Decimal = Decimal("0.005")

    # Maximum cost threshold (YES + NO must be less than this)
    # Accounts for fees - typically set to 0.99 for ~1% fee buffer
    max_combined_cost: Decimal = Decimal("0.99")

    # Minimum liquidity at the ask prices (in tokens/USD)
    min_liquidity: Decimal = Decimal("100")

    # Maximum position size per trade
    max_position_size: Decimal = Decimal("1000")


@dataclass
class AnalyzerStats:
    """Statistics for the analyzer."""

    opportunities_found: int = 0
    opportunities_rejected: int = 0
    total_scans: int = 0

    # Rejection reasons
    rejected_no_asks: int = 0
    rejected_cost_too_high: int = 0
    rejected_profit_too_low: int = 0
    rejected_liquidity_too_low: int = 0


class ArbitrageAnalyzer:
    """
    Analyzes market snapshots for arbitrage opportunities.

    The core arbitrage strategy:
    - Buy YES at best ask
    - Buy NO at best ask
    - If combined cost < $1.00, guaranteed profit

    Example:
        YES ask: $0.48
        NO ask: $0.49
        Combined: $0.97
        Profit: $0.03 per $1 (3.09% return)
    """

    def __init__(self, config: Optional[AnalyzerConfig] = None) -> None:
        if config is None:
            settings = get_settings()
            config = AnalyzerConfig(
                min_profit_threshold=Decimal(str(settings.min_profit_threshold)),
                max_position_size=Decimal(str(settings.max_position_size)),
                min_liquidity=Decimal(str(settings.min_liquidity_usd)),
            )
        self.config = config
        self.stats = AnalyzerStats()
        self._recent_opportunities: list[ArbitrageOpportunity] = []

    def analyze(self, snapshot: MarketSnapshot) -> Optional[ArbitrageOpportunity]:
        """
        Analyze a market snapshot for arbitrage opportunity.

        Args:
            snapshot: Market snapshot with orderbook data

        Returns:
            ArbitrageOpportunity if found, None otherwise
        """
        self.stats.total_scans += 1

        # Get best ask prices
        yes_ask = snapshot.yes_best_ask
        no_ask = snapshot.no_best_ask

        # Must have asks on both sides
        if yes_ask is None or no_ask is None:
            self.stats.rejected_no_asks += 1
            return None

        # Calculate combined cost
        combined_cost = yes_ask + no_ask

        # Check if arbitrage exists (combined cost < max threshold)
        if combined_cost >= self.config.max_combined_cost:
            self.stats.rejected_cost_too_high += 1
            return None

        # Calculate profit percentage
        profit_pct = (Decimal("1") - combined_cost) / combined_cost

        # Check minimum profit threshold
        if profit_pct < self.config.min_profit_threshold:
            self.stats.rejected_profit_too_low += 1
            return None

        # Check liquidity
        yes_size = snapshot.yes_orderbook.best_ask_size or Decimal("0")
        no_size = snapshot.no_orderbook.best_ask_size or Decimal("0")
        min_size = min(yes_size, no_size)

        if min_size < self.config.min_liquidity:
            self.stats.rejected_liquidity_too_low += 1
            return None

        # Calculate max trade size
        max_trade_size = min(min_size, self.config.max_position_size)

        # Create opportunity
        opportunity = ArbitrageOpportunity(
            market=snapshot.market,
            yes_ask=yes_ask,
            no_ask=no_ask,
            combined_cost=combined_cost,
            profit_pct=profit_pct,
            yes_size_available=yes_size,
            no_size_available=no_size,
            max_trade_size=max_trade_size,
            timestamp=datetime.utcnow(),
        )

        self.stats.opportunities_found += 1
        self._recent_opportunities.append(opportunity)

        # Keep only recent opportunities (last 100)
        if len(self._recent_opportunities) > 100:
            self._recent_opportunities = self._recent_opportunities[-100:]

        log.info(
            "Arbitrage opportunity found!",
            market=snapshot.market.question[:50],
            yes_ask=float(yes_ask),
            no_ask=float(no_ask),
            combined=float(combined_cost),
            profit_pct=f"{float(profit_pct) * 100:.2f}%",
            max_size=float(max_trade_size),
            expected_profit=f"${float(max_trade_size * profit_pct):.2f}",
        )

        return opportunity

    def analyze_batch(
        self, snapshots: list[MarketSnapshot]
    ) -> list[ArbitrageOpportunity]:
        """
        Analyze multiple snapshots for opportunities.

        Args:
            snapshots: List of market snapshots

        Returns:
            List of found opportunities
        """
        opportunities: list[ArbitrageOpportunity] = []

        for snapshot in snapshots:
            opp = self.analyze(snapshot)
            if opp is not None:
                opportunities.append(opp)

        # Sort by profit percentage descending
        opportunities.sort(key=lambda x: x.profit_pct, reverse=True)

        if opportunities:
            log.info(
                "Batch analysis complete",
                markets_scanned=len(snapshots),
                opportunities=len(opportunities),
                best_profit=f"{float(opportunities[0].profit_pct) * 100:.2f}%",
            )

        return opportunities

    def get_stats(self) -> dict[str, int]:
        """Get analyzer statistics."""
        return {
            "opportunities_found": self.stats.opportunities_found,
            "opportunities_rejected": self.stats.opportunities_rejected,
            "total_scans": self.stats.total_scans,
            "rejected_no_asks": self.stats.rejected_no_asks,
            "rejected_cost_too_high": self.stats.rejected_cost_too_high,
            "rejected_profit_too_low": self.stats.rejected_profit_too_low,
            "rejected_liquidity_too_low": self.stats.rejected_liquidity_too_low,
        }

    def get_recent_opportunities(self) -> list[ArbitrageOpportunity]:
        """Get recently found opportunities."""
        return self._recent_opportunities.copy()

    def reset_stats(self) -> None:
        """Reset analyzer statistics."""
        self.stats = AnalyzerStats()
        self._recent_opportunities = []
