"""Gamma API client for market discovery."""

import json
from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

import aiohttp

from karb.api.models import Market, Token
from karb.config import get_settings
from karb.utils.logging import get_logger

log = get_logger(__name__)


class GammaClient:
    """Client for Polymarket Gamma API (market discovery)."""

    def __init__(self, base_url: Optional[str] = None) -> None:
        self.base_url = (base_url or get_settings().gamma_base_url).rstrip("/")
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create aiohttp session."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"Accept": "application/json"},
                timeout=aiohttp.ClientTimeout(total=30),
            )
        return self._session

    async def close(self) -> None:
        """Close the HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def _get(self, endpoint: str, params: Optional[dict[str, Any]] = None) -> Any:
        """Make a GET request to the API."""
        session = await self._get_session()
        url = f"{self.base_url}{endpoint}"

        try:
            async with session.get(url, params=params) as response:
                response.raise_for_status()
                return await response.json()
        except aiohttp.ClientError as e:
            log.error("Gamma API request failed", url=url, error=str(e))
            raise

    async def get_markets(
        self,
        active: bool = True,
        closed: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """
        Fetch markets from the Gamma API.

        Args:
            active: Only return active markets
            closed: Include closed markets
            limit: Maximum number of markets to return
            offset: Pagination offset

        Returns:
            List of market dictionaries
        """
        params: dict[str, Any] = {
            "limit": limit,
            "offset": offset,
        }
        if active:
            params["active"] = "true"
        # Explicitly set closed parameter
        params["closed"] = "true" if closed else "false"

        data = await self._get("/markets", params)

        # API may return list directly or nested
        if isinstance(data, list):
            return data
        elif isinstance(data, dict) and "markets" in data:
            return data["markets"]
        return []

    async def get_market(self, market_id: str) -> Optional[dict[str, Any]]:
        """
        Fetch a single market by ID.

        Args:
            market_id: The market ID or condition ID

        Returns:
            Market dictionary or None if not found
        """
        try:
            return await self._get(f"/markets/{market_id}")
        except aiohttp.ClientResponseError as e:
            if e.status == 404:
                return None
            raise

    async def get_markets_by_slug(self, slug: str) -> list[dict[str, Any]]:
        """Fetch markets matching a slug pattern."""
        params = {"slug": slug}
        data = await self._get("/markets", params)
        if isinstance(data, list):
            return data
        return []

    def parse_market(self, data: dict[str, Any]) -> Optional[Market]:
        """
        Parse a market dictionary into a Market object.

        Args:
            data: Raw market data from API

        Returns:
            Market object or None if parsing fails
        """
        try:
            # Extract token info - API sometimes returns JSON strings
            tokens = data.get("tokens", [])
            if isinstance(tokens, str):
                try:
                    tokens = json.loads(tokens)
                except json.JSONDecodeError:
                    tokens = []
            tokens = tokens or []

            clob_token_ids = data.get("clobTokenIds", [])
            if isinstance(clob_token_ids, str):
                try:
                    clob_token_ids = json.loads(clob_token_ids)
                except json.JSONDecodeError:
                    clob_token_ids = []
            clob_token_ids = clob_token_ids or []

            # Only support binary markets (exactly 2 outcomes)
            if len(clob_token_ids) != 2:
                log.debug(
                    "Skipping non-binary market",
                    market_id=data.get("id"),
                    outcomes=len(clob_token_ids),
                    question=data.get("question", "")[:50],
                )
                return None

            yes_token = Token(token_id="", outcome="Yes")
            no_token = Token(token_id="", outcome="No")

            # Parse tokens from various response formats
            if tokens:
                for token in tokens:
                    outcome = (
                        token.get("outcome", "")
                        or token.get("name", "")
                        or ""
                    ).lower()
                    token_id = (
                        token.get("token_id")
                        or token.get("tokenId")
                        or token.get("id", "")
                    )

                    if "yes" in outcome:
                        yes_token = Token(
                            token_id=str(token_id),
                            outcome="Yes",
                            price=Decimal(str(token.get("price", "0"))),
                        )
                    elif "no" in outcome:
                        no_token = Token(
                            token_id=str(token_id),
                            outcome="No",
                            price=Decimal(str(token.get("price", "0"))),
                        )

            # Fallback to clobTokenIds array [yes_id, no_id]
            # (we already verified len == 2 above)
            if clob_token_ids:
                if not yes_token.token_id:
                    yes_token.token_id = str(clob_token_ids[0])
                if not no_token.token_id:
                    no_token.token_id = str(clob_token_ids[1])

            # Parse prices from outcomePrices if available
            outcome_prices = data.get("outcomePrices", [])
            if isinstance(outcome_prices, str):
                try:
                    outcome_prices = json.loads(outcome_prices)
                except json.JSONDecodeError:
                    outcome_prices = []
            outcome_prices = outcome_prices or []
            if outcome_prices and len(outcome_prices) >= 2:
                try:
                    price_str = str(outcome_prices[0]).strip()
                    if price_str and price_str not in ("", "null", "None"):
                        yes_token.price = Decimal(price_str)
                except Exception:
                    pass
                try:
                    price_str = str(outcome_prices[1]).strip()
                    if price_str and price_str not in ("", "null", "None"):
                        no_token.price = Decimal(price_str)
                except Exception:
                    pass

            # Skip markets without token IDs
            if not yes_token.token_id or not no_token.token_id:
                log.debug(
                    "Skipping market without token IDs",
                    market_id=data.get("id"),
                    question=data.get("question", "")[:50],
                )
                return None

            # Parse end_date from various possible field names
            end_date = None
            end_date_raw = (
                data.get("endDate")
                or data.get("end_date_iso")
                or data.get("end_date")
                or data.get("resolutionDate")
                or data.get("resolution_date")
            )
            if end_date_raw:
                try:
                    # Handle various ISO formats
                    if isinstance(end_date_raw, str):
                        # Remove trailing Z and add UTC timezone if needed
                        if end_date_raw.endswith("Z"):
                            end_date_raw = end_date_raw[:-1] + "+00:00"
                        end_date = datetime.fromisoformat(end_date_raw)
                except (ValueError, TypeError) as e:
                    log.debug("Failed to parse end_date", raw=end_date_raw, error=str(e))

            return Market(
                id=str(data.get("id", "")),
                condition_id=str(data.get("conditionId", data.get("condition_id", ""))),
                question=data.get("question", ""),
                slug=data.get("slug", ""),
                yes_token=yes_token,
                no_token=no_token,
                volume=Decimal(str(data.get("volume", "0") or "0")),
                liquidity=Decimal(str(data.get("liquidity", "0") or "0")),
                active=data.get("active", True),
                closed=data.get("closed", False),
                end_date=end_date,
            )

        except (KeyError, ValueError, TypeError) as e:
            log.warning(
                "Failed to parse market",
                market_id=data.get("id"),
                error=str(e),
            )
            return None

    async def fetch_all_active_markets(
        self,
        min_volume: float = 0,
        min_liquidity: float = 0,
        max_days_until_resolution: Optional[int] = None,
    ) -> list[Market]:
        """
        Fetch all active markets with optional filtering.

        Args:
            min_volume: Minimum volume in USD
            min_liquidity: Minimum liquidity in USD
            max_days_until_resolution: Maximum days until market resolves (None = no limit)

        Returns:
            List of Market objects
        """
        from datetime import datetime, timezone

        markets: list[Market] = []
        offset = 0
        limit = 100
        now = datetime.now(timezone.utc)

        while True:
            raw_markets = await self.get_markets(
                active=True,
                closed=False,
                limit=limit,
                offset=offset,
            )

            if not raw_markets:
                break

            for raw in raw_markets:
                market = self.parse_market(raw)
                if market is None:
                    continue

                # Apply filters
                if market.volume < Decimal(str(min_volume)):
                    continue
                if market.liquidity < Decimal(str(min_liquidity)):
                    continue

                # Filter by resolution date
                if max_days_until_resolution is not None and market.end_date:
                    end_date = market.end_date
                    if end_date.tzinfo is None:
                        end_date = end_date.replace(tzinfo=timezone.utc)
                    days_until = (end_date - now).days
                    if days_until > max_days_until_resolution:
                        continue

                markets.append(market)

            # Check if we got fewer than limit (last page)
            if len(raw_markets) < limit:
                break

            offset += limit

        log.info(
            "Fetched active markets",
            total=len(markets),
            min_volume=min_volume,
            min_liquidity=min_liquidity,
            max_days=max_days_until_resolution,
        )

        return markets

    async def __aenter__(self) -> "GammaClient":
        """Async context manager entry."""
        return self

    async def __aexit__(self, *args: Any) -> None:
        """Async context manager exit."""
        await self.close()
