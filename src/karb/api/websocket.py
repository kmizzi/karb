"""WebSocket client for real-time Polymarket orderbook streaming."""

import asyncio
import json
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable, Optional

import websockets
from websockets.client import WebSocketClientProtocol

from karb.utils.logging import get_logger

log = get_logger(__name__)

# Polymarket WebSocket endpoints
WS_MARKET_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
MAX_ASSETS_PER_CONNECTION = 500


@dataclass
class BookLevel:
    """A single price level in the orderbook."""
    price: Decimal
    size: Decimal


@dataclass
class OrderBookUpdate:
    """Real-time orderbook update from WebSocket."""
    asset_id: str
    market_id: str
    bids: list[BookLevel]
    asks: list[BookLevel]
    timestamp: str

    @property
    def best_bid(self) -> Optional[Decimal]:
        if self.bids:
            return max(b.price for b in self.bids)
        return None

    @property
    def best_ask(self) -> Optional[Decimal]:
        if self.asks:
            return min(a.price for a in self.asks)
        return None


@dataclass
class PriceChange:
    """Price change event from WebSocket."""
    asset_id: str
    market_id: str
    price: Decimal
    size: Decimal
    side: str  # BUY or SELL
    best_bid: Optional[Decimal]
    best_ask: Optional[Decimal]


@dataclass
class LastTradePrice:
    """Last trade price event from WebSocket."""
    asset_id: str
    price: Decimal
    size: Decimal
    side: str
    timestamp: str


# Callback types
BookCallback = Callable[[OrderBookUpdate], None]
PriceCallback = Callable[[PriceChange], None]
TradeCallback = Callable[[LastTradePrice], None]


class WebSocketClient:
    """
    WebSocket client for real-time Polymarket data.

    Connects to the CLOB WebSocket and streams orderbook updates
    for subscribed markets.
    """

    def __init__(
        self,
        on_book: Optional[BookCallback] = None,
        on_price_change: Optional[PriceCallback] = None,
        on_trade: Optional[TradeCallback] = None,
    ) -> None:
        self._ws: Optional[WebSocketClientProtocol] = None
        self._running = False
        self._subscribed_assets: set[str] = set()
        self._reconnect_delay = 1.0
        self._max_reconnect_delay = 60.0

        # Callbacks
        self._on_book = on_book
        self._on_price_change = on_price_change
        self._on_trade = on_trade

        # State tracking
        self._orderbooks: dict[str, OrderBookUpdate] = {}
        self._best_prices: dict[str, tuple[Optional[Decimal], Optional[Decimal]]] = {}

    async def connect(self) -> None:
        """Connect to the WebSocket server."""
        log.info("Connecting to Polymarket WebSocket", url=WS_MARKET_URL)

        try:
            self._ws = await websockets.connect(
                WS_MARKET_URL,
                ping_interval=30,
                ping_timeout=10,
                close_timeout=5,
            )
            self._running = True
            self._reconnect_delay = 1.0  # Reset on successful connect
            log.info("WebSocket connected")
            # Don't auto-resubscribe here - caller will call subscribe()

        except Exception as e:
            log.error("WebSocket connection failed", error=str(e))
            raise

    async def subscribe(self, asset_ids: list[str]) -> None:
        """
        Subscribe to orderbook updates for given assets.

        Args:
            asset_ids: List of token IDs to subscribe to
        """
        if len(asset_ids) > MAX_ASSETS_PER_CONNECTION:
            log.warning(
                "Too many assets for single connection",
                requested=len(asset_ids),
                max=MAX_ASSETS_PER_CONNECTION,
            )
            asset_ids = asset_ids[:MAX_ASSETS_PER_CONNECTION]

        new_assets = set(asset_ids) - self._subscribed_assets
        if not new_assets:
            return

        self._subscribed_assets.update(new_assets)

        if self._ws and self.is_connected:
            await self._send_subscription(list(new_assets))

    async def _send_subscription(self, asset_ids: list[str]) -> None:
        """Send subscription message to WebSocket in batches."""
        if not self._ws or not self.is_connected:
            return

        # Subscribe in batches to avoid overwhelming the server
        batch_size = 100
        for i in range(0, len(asset_ids), batch_size):
            batch = asset_ids[i:i + batch_size]
            message = {
                "assets_ids": batch,
                "type": "market",
            }
            await self._ws.send(json.dumps(message))
            log.debug("Subscribed batch", batch_num=i // batch_size + 1, count=len(batch))
            await asyncio.sleep(0.1)  # Small delay between batches

        log.info("Subscribed to assets", count=len(asset_ids))

    async def listen(self) -> None:
        """Listen for messages from the WebSocket."""
        if not self._ws:
            raise RuntimeError("Not connected")

        try:
            async for message in self._ws:
                try:
                    await self._handle_message(message)
                except Exception as e:
                    log.error("Error handling message", error=str(e))

        except websockets.ConnectionClosed as e:
            log.warning("WebSocket connection closed", code=e.code, reason=e.reason)
            self._running = False
        except Exception as e:
            log.error("WebSocket error", error=str(e))
            self._running = False

    async def _handle_message(self, raw_message: str) -> None:
        """Handle incoming WebSocket message."""
        # Ignore empty messages (heartbeats, etc.)
        if not raw_message or not raw_message.strip():
            return

        try:
            data = json.loads(raw_message)
        except json.JSONDecodeError:
            # Log but don't crash on malformed messages
            log.debug("Received non-JSON message", length=len(raw_message))
            return

        # Messages can come as arrays (batch) or single objects
        if isinstance(data, list):
            for item in data:
                await self._process_event(item)
            return

        await self._process_event(data)

    async def _process_event(self, data: dict) -> None:
        """Process a single event from WebSocket."""
        if not isinstance(data, dict):
            log.debug("Skipping non-dict message", type=type(data).__name__)
            return

        # Handle different event types
        event_type = data.get("event_type")

        if event_type == "book":
            await self._handle_book_message(data)
        elif event_type == "price_change":
            await self._handle_price_change(data)
        elif event_type == "last_trade_price":
            await self._handle_last_trade(data)
        elif event_type == "tick_size_change":
            # Ignore tick size changes for now
            pass
        else:
            log.debug("Unknown event type", event_type=event_type)

    async def _handle_book_message(self, data: dict[str, Any]) -> None:
        """Handle orderbook snapshot message."""
        try:
            bids = [
                BookLevel(
                    price=Decimal(str(b.get("price", "0"))),
                    size=Decimal(str(b.get("size", "0"))),
                )
                for b in data.get("bids", [])
            ]

            asks = [
                BookLevel(
                    price=Decimal(str(a.get("price", "0"))),
                    size=Decimal(str(a.get("size", "0"))),
                )
                for a in data.get("asks", [])
            ]

            update = OrderBookUpdate(
                asset_id=data.get("asset_id", ""),
                market_id=data.get("market", ""),
                bids=bids,
                asks=asks,
                timestamp=data.get("timestamp", ""),
            )

            # Update state
            self._orderbooks[update.asset_id] = update
            self._best_prices[update.asset_id] = (update.best_bid, update.best_ask)

            # Invoke callback
            if self._on_book:
                result = self._on_book(update)
                if asyncio.iscoroutine(result):
                    await result

        except Exception as e:
            log.error("Error parsing book message", error=str(e))

    async def _handle_price_change(self, data: dict[str, Any]) -> None:
        """Handle price change message."""
        try:
            market_id = data.get("market", "")

            for change in data.get("price_changes", []):
                price_change = PriceChange(
                    asset_id=change.get("asset_id", ""),
                    market_id=market_id,
                    price=Decimal(str(change.get("price", "0"))),
                    size=Decimal(str(change.get("size", "0"))),
                    side=change.get("side", ""),
                    best_bid=Decimal(str(change["best_bid"])) if change.get("best_bid") else None,
                    best_ask=Decimal(str(change["best_ask"])) if change.get("best_ask") else None,
                )

                # Update best prices
                self._best_prices[price_change.asset_id] = (
                    price_change.best_bid,
                    price_change.best_ask,
                )

                # Invoke callback
                if self._on_price_change:
                    result = self._on_price_change(price_change)
                    if asyncio.iscoroutine(result):
                        await result

        except Exception as e:
            log.error("Error parsing price change", error=str(e))

    async def _handle_last_trade(self, data: dict[str, Any]) -> None:
        """Handle last trade price message."""
        try:
            trade = LastTradePrice(
                asset_id=data.get("asset_id", ""),
                price=Decimal(str(data.get("price", "0"))),
                size=Decimal(str(data.get("size", "0"))),
                side=data.get("side", ""),
                timestamp=data.get("timestamp", ""),
            )

            if self._on_trade:
                result = self._on_trade(trade)
                if asyncio.iscoroutine(result):
                    await result

        except Exception as e:
            log.error("Error parsing last trade", error=str(e))

    def get_best_prices(self, asset_id: str) -> tuple[Optional[Decimal], Optional[Decimal]]:
        """Get cached best bid/ask for an asset."""
        return self._best_prices.get(asset_id, (None, None))

    def get_orderbook(self, asset_id: str) -> Optional[OrderBookUpdate]:
        """Get cached orderbook for an asset."""
        return self._orderbooks.get(asset_id)

    async def run_forever(self) -> None:
        """Run the WebSocket client with automatic reconnection."""
        while True:
            try:
                await self.connect()
                await self.listen()
            except Exception as e:
                log.error("WebSocket error, reconnecting", error=str(e))

            if not self._running:
                break

            # Exponential backoff for reconnection
            log.info("Reconnecting", delay=self._reconnect_delay)
            await asyncio.sleep(self._reconnect_delay)
            self._reconnect_delay = min(
                self._reconnect_delay * 2,
                self._max_reconnect_delay,
            )

    async def close(self) -> None:
        """Close the WebSocket connection."""
        self._running = False
        if self._ws:
            await self._ws.close()
            self._ws = None
        log.info("WebSocket closed")

    @property
    def is_connected(self) -> bool:
        """Check if WebSocket is connected."""
        if self._ws is None:
            return False
        # websockets v12+ uses .state, older uses .open
        try:
            from websockets.protocol import State
            return self._ws.state == State.OPEN
        except (ImportError, AttributeError):
            # Fallback for older versions or different API
            return getattr(self._ws, 'open', False)

    @property
    def subscribed_count(self) -> int:
        """Number of subscribed assets."""
        return len(self._subscribed_assets)
