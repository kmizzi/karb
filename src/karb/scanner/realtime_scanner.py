"""Real-time market scanner using WebSocket streaming."""

import asyncio
import json
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Callable, Optional

from karb.api.gamma import GammaClient
from karb.api.models import Market
from karb.api.websocket import (
    OrderBookUpdate,
    PriceChange,
    WebSocketClient,
)
from karb.config import get_settings
from karb.utils.logging import get_logger

log = get_logger(__name__)

# Shared state files for dashboard
STATS_FILE = Path.home() / ".karb" / "scanner_stats.json"
ALERTS_FILE = Path.home() / ".karb" / "scanner_alerts.json"

# Maximum assets per WebSocket connection
MAX_ASSETS_PER_WS = 500


@dataclass
class MarketPrices:
    """Tracks current prices for a market's YES and NO tokens."""
    market: Market
    yes_best_bid: Optional[Decimal] = None
    yes_best_ask: Optional[Decimal] = None
    no_best_bid: Optional[Decimal] = None
    no_best_ask: Optional[Decimal] = None

    @property
    def combined_ask(self) -> Optional[Decimal]:
        """Cost to buy both YES and NO at best ask."""
        if self.yes_best_ask is None or self.no_best_ask is None:
            return None
        return self.yes_best_ask + self.no_best_ask

    @property
    def arbitrage_profit(self) -> Optional[Decimal]:
        """Potential profit from arbitrage (1 - combined_ask)."""
        combined = self.combined_ask
        if combined is None:
            return None
        return Decimal("1") - combined

    @property
    def has_arbitrage(self) -> bool:
        """Check if arbitrage opportunity exists."""
        profit = self.arbitrage_profit
        if profit is None:
            return False
        settings = get_settings()
        return profit > Decimal(str(settings.min_profit_threshold))


@dataclass
class ArbitrageAlert:
    """Alert for detected arbitrage opportunity."""
    market: Market
    yes_ask: Decimal
    no_ask: Decimal
    combined_cost: Decimal
    profit_pct: Decimal
    timestamp: float


# Callback type for arbitrage alerts
ArbitrageCallback = Callable[[ArbitrageAlert], None]


class RealtimeScanner:
    """
    Real-time market scanner using WebSocket streaming.

    Instead of polling, this scanner:
    1. Loads markets from Gamma API
    2. Subscribes to WebSocket for real-time price updates
    3. Triggers callbacks instantly when arbitrage is detected
    """

    def __init__(
        self,
        on_arbitrage: Optional[ArbitrageCallback] = None,
        min_liquidity: float = 1000.0,
        max_markets: int = 250,  # 250 markets * 2 tokens = 500 (WebSocket limit)
    ) -> None:
        settings = get_settings()

        self.gamma = GammaClient()
        self.ws_client = WebSocketClient(
            on_book=self._on_book_update,
            on_price_change=self._on_price_change,
        )

        self._on_arbitrage = on_arbitrage
        self.min_liquidity = min_liquidity or settings.min_liquidity_usd
        self.max_markets = max_markets

        # State
        self._markets: dict[str, Market] = {}  # market_id -> Market
        self._token_to_market: dict[str, str] = {}  # token_id -> market_id
        self._market_prices: dict[str, MarketPrices] = {}  # market_id -> MarketPrices
        self._running = False

        # Stats
        self._price_updates = 0
        self._arbitrage_alerts = 0

    async def load_markets(self) -> list[Market]:
        """Load active markets from Gamma API."""
        log.info("Loading markets from Gamma API...")

        markets = await self.gamma.fetch_all_active_markets(
            min_liquidity=self.min_liquidity,
        )

        # Sort by liquidity and take top N
        markets.sort(key=lambda m: m.liquidity, reverse=True)
        markets = markets[:self.max_markets]

        # Build lookup tables
        self._markets = {}
        self._token_to_market = {}
        self._market_prices = {}

        for market in markets:
            self._markets[market.id] = market
            self._token_to_market[market.yes_token.token_id] = market.id
            self._token_to_market[market.no_token.token_id] = market.id
            self._market_prices[market.id] = MarketPrices(market=market)

        log.info(
            "Markets loaded",
            count=len(markets),
            min_liquidity=self.min_liquidity,
        )

        return markets

    async def subscribe_to_markets(self) -> None:
        """Subscribe to WebSocket updates for all loaded markets."""
        # Clear old subscriptions for fresh start
        self.ws_client._subscribed_assets.clear()

        # Collect all token IDs (YES and NO for each market)
        token_ids = []
        for market in self._markets.values():
            token_ids.append(market.yes_token.token_id)
            token_ids.append(market.no_token.token_id)

        log.info("Subscribing to tokens", count=len(token_ids))
        await self.ws_client.subscribe(token_ids)

    def _on_book_update(self, update: OrderBookUpdate) -> None:
        """Handle orderbook snapshot update."""
        self._update_prices(
            update.asset_id,
            update.best_bid,
            update.best_ask,
        )

    def _on_price_change(self, change: PriceChange) -> None:
        """Handle real-time price change."""
        self._price_updates += 1
        self._update_prices(
            change.asset_id,
            change.best_bid,
            change.best_ask,
        )

    def _update_prices(
        self,
        token_id: str,
        best_bid: Optional[Decimal],
        best_ask: Optional[Decimal],
    ) -> None:
        """Update prices for a token and check for arbitrage."""
        market_id = self._token_to_market.get(token_id)
        if not market_id:
            return

        market = self._markets.get(market_id)
        if not market:
            return

        prices = self._market_prices.get(market_id)
        if not prices:
            return

        # Update the appropriate side
        if token_id == market.yes_token.token_id:
            prices.yes_best_bid = best_bid
            prices.yes_best_ask = best_ask
        elif token_id == market.no_token.token_id:
            prices.no_best_bid = best_bid
            prices.no_best_ask = best_ask

        # Check for arbitrage
        self._check_arbitrage(prices)

    def _check_arbitrage(self, prices: MarketPrices) -> None:
        """Check if market has arbitrage opportunity and trigger alert."""
        if not prices.has_arbitrage:
            return

        # We have an opportunity!
        combined = prices.combined_ask
        profit = prices.arbitrage_profit

        if combined is None or profit is None:
            return

        self._arbitrage_alerts += 1

        alert = ArbitrageAlert(
            market=prices.market,
            yes_ask=prices.yes_best_ask or Decimal("0"),
            no_ask=prices.no_best_ask or Decimal("0"),
            combined_cost=combined,
            profit_pct=profit,
            timestamp=asyncio.get_event_loop().time(),
        )

        log.info(
            "ARBITRAGE DETECTED",
            market=prices.market.question[:50],
            yes_ask=f"${float(alert.yes_ask):.4f}",
            no_ask=f"${float(alert.no_ask):.4f}",
            combined=f"${float(alert.combined_cost):.4f}",
            profit=f"{float(alert.profit_pct) * 100:.2f}%",
        )

        # Save alert to file for dashboard
        self._save_alert(alert)

        # Trigger callback
        if self._on_arbitrage:
            try:
                result = self._on_arbitrage(alert)
                if asyncio.iscoroutine(result):
                    asyncio.create_task(result)
            except Exception as e:
                log.error("Arbitrage callback error", error=str(e))

    async def run(self) -> None:
        """Run the real-time scanner."""
        self._running = True

        log.info("Starting real-time scanner")

        # Load markets
        await self.load_markets()

        # Run WebSocket with auto-reconnection, plus periodic tasks
        await asyncio.gather(
            self._run_websocket_with_reconnect(),
            self._periodic_market_refresh(),
            self._periodic_stats(),
        )

    async def _run_websocket_with_reconnect(self) -> None:
        """Run WebSocket with automatic reconnection."""
        while self._running:
            try:
                # Connect
                await self.ws_client.connect()

                # Subscribe to all markets
                await self.subscribe_to_markets()

                # Listen until disconnected
                await self.ws_client.listen()

            except Exception as e:
                log.error("WebSocket error", error=str(e))

            if not self._running:
                break

            # Reconnect with backoff
            delay = min(self.ws_client._reconnect_delay, 30)
            log.info("Reconnecting WebSocket", delay=delay)
            await asyncio.sleep(delay)
            self.ws_client._reconnect_delay = min(delay * 2, 60)

    async def _periodic_market_refresh(self, interval: float = 600) -> None:
        """Periodically refresh market list (every 10 min)."""
        while self._running:
            await asyncio.sleep(interval)

            try:
                log.info("Refreshing market list...")
                old_count = len(self._markets)
                await self.load_markets()
                new_count = len(self._markets)

                # Only resubscribe if markets changed significantly
                if abs(new_count - old_count) > 10:
                    log.info("Market list changed, reconnecting WebSocket")
                    # Clear subscriptions and let reconnect loop handle it
                    self.ws_client._subscribed_assets.clear()
                    if self.ws_client._ws:
                        await self.ws_client._ws.close()
            except Exception as e:
                log.error("Market refresh error", error=str(e))

    async def _periodic_stats(self, interval: float = 60) -> None:
        """Log periodic statistics and write to shared state file."""
        while self._running:
            await asyncio.sleep(interval)

            stats = self.get_stats()
            log.info(
                "Scanner stats",
                markets=stats["markets"],
                price_updates=stats["price_updates"],
                arbitrage_alerts=stats["arbitrage_alerts"],
                ws_connected=stats["ws_connected"],
            )

            # Write stats to file for dashboard
            self._write_stats_file(stats)

    def _write_stats_file(self, stats: dict) -> None:
        """Write stats to shared file for dashboard."""
        try:
            STATS_FILE.parent.mkdir(parents=True, exist_ok=True)
            stats["last_update"] = asyncio.get_event_loop().time()
            with open(STATS_FILE, "w") as f:
                json.dump(stats, f)
        except Exception as e:
            log.debug("Failed to write stats file", error=str(e))

    def _save_alert(self, alert: ArbitrageAlert) -> None:
        """Save arbitrage alert to file for dashboard."""
        try:
            ALERTS_FILE.parent.mkdir(parents=True, exist_ok=True)

            # Load existing alerts
            alerts = []
            if ALERTS_FILE.exists():
                with open(ALERTS_FILE) as f:
                    alerts = json.load(f)

            # Add new alert
            from datetime import datetime
            alerts.append({
                "market": alert.market.question[:60],
                "yes_ask": float(alert.yes_ask),
                "no_ask": float(alert.no_ask),
                "combined": float(alert.combined_cost),
                "profit": float(alert.profit_pct),
                "timestamp": datetime.now().isoformat(),
            })

            # Keep only last 50 alerts
            alerts = alerts[-50:]

            with open(ALERTS_FILE, "w") as f:
                json.dump(alerts, f)
        except Exception as e:
            log.debug("Failed to save alert", error=str(e))

    def stop(self) -> None:
        """Stop the scanner."""
        log.info("Stopping real-time scanner")
        self._running = False

    async def close(self) -> None:
        """Close all connections."""
        self.stop()
        await self.ws_client.close()
        await self.gamma.close()

    def get_stats(self) -> dict:
        """Get scanner statistics."""
        return {
            "markets": len(self._markets),
            "price_updates": self._price_updates,
            "arbitrage_alerts": self._arbitrage_alerts,
            "ws_connected": self.ws_client.is_connected,
            "subscribed_tokens": self.ws_client.subscribed_count,
        }

    async def __aenter__(self) -> "RealtimeScanner":
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()
