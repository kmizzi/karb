"""LLM-powered market matcher for cross-platform arbitrage."""

import json
import os
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from karb.api.kalshi import KalshiMarket
from karb.api.models import Market as PolyMarket
from karb.utils.logging import get_logger

log = get_logger(__name__)


@dataclass
class LLMMatch:
    """A match identified by the LLM."""
    polymarket_question: str
    kalshi_title: str
    confidence: str  # "high", "medium", "low"
    reasoning: str


@dataclass
class MatchedEvent:
    """A matched event across Polymarket and Kalshi."""
    polymarket: PolyMarket
    kalshi: KalshiMarket
    confidence: float
    match_type: str = "llm"
    reasoning: str = ""

    @property
    def poly_yes_ask(self) -> Optional[Decimal]:
        return self.polymarket.yes_price

    @property
    def kalshi_yes_ask(self) -> Optional[Decimal]:
        return self.kalshi.yes_ask


MATCH_PROMPT = """Find prediction markets from two platforms that ask about THE IDENTICAL event.

## Polymarket:
{polymarket_list}

## Kalshi:
{kalshi_list}

## Rules:
1. ONLY match if asking about the EXACT SAME specific outcome
2. Same person + same position + same timeframe = match
3. Different numbers/teams/people/amounts = NOT a match

Examples of MATCHES:
- "Weed rescheduled 2025" + "Marijuana rescheduled 2025" = MATCH (synonym)
- "Kamala Harris 2028 Dem nomination" + "Kamala Harris Democratic nominee 2028" = MATCH

Examples of NON-MATCHES (do NOT include these):
- "Bitcoin $100k" vs "Bitcoin $200k" = DIFFERENT price targets
- "Colts win Super Bowl" vs "Bills win Super Bowl" = DIFFERENT teams
- "Fed cuts 25bps" vs "Fed cuts 50bps" = DIFFERENT amounts
- "Trump wins" vs "Republican wins" = DIFFERENT questions

Output ONLY a JSON array of exact matches. If no matches, output [].

```json
[{{"polymarket": "exact text", "kalshi": "exact text", "reasoning": "why identical"}}]
```"""


class LLMMatcher:
    """
    Uses an LLM to semantically match markets between platforms.

    Supports Anthropic, OpenAI, and Google Gemini APIs.
    """

    def __init__(
        self,
        provider: str = "anthropic",  # "anthropic", "openai", or "gemini"
        model: Optional[str] = None,
        api_key: Optional[str] = None,
    ):
        self.provider = provider
        self.api_key = api_key or self._get_api_key()

        if provider == "anthropic":
            self.model = model or "claude-3-haiku-20240307"
        elif provider == "gemini":
            self.model = model or "gemini-1.5-flash"
        else:
            self.model = model or "gpt-4o-mini"

        self._client = None

    def _get_api_key(self) -> str:
        """Get API key from environment."""
        if self.provider == "anthropic":
            key = os.environ.get("ANTHROPIC_API_KEY")
            if not key:
                raise ValueError("ANTHROPIC_API_KEY not set")
            return key
        elif self.provider == "gemini":
            key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
            if not key:
                raise ValueError("GOOGLE_API_KEY or GEMINI_API_KEY not set")
            return key
        else:
            key = os.environ.get("OPENAI_API_KEY")
            if not key:
                raise ValueError("OPENAI_API_KEY not set")
            return key

    def _get_client(self):
        """Lazy-load the API client."""
        if self._client is None:
            if self.provider == "anthropic":
                import anthropic
                self._client = anthropic.Anthropic(api_key=self.api_key)
            elif self.provider == "gemini":
                import google.generativeai as genai
                genai.configure(api_key=self.api_key)
                self._client = genai.GenerativeModel(self.model)
            else:
                import openai
                self._client = openai.OpenAI(api_key=self.api_key)
        return self._client

    def _call_llm(self, prompt: str) -> str:
        """Call the LLM and return response text."""
        client = self._get_client()

        if self.provider == "anthropic":
            response = client.messages.create(
                model=self.model,
                max_tokens=4096,
                messages=[{"role": "user", "content": prompt}],
            )
            return response.content[0].text
        elif self.provider == "gemini":
            response = client.generate_content(prompt)
            return response.text
        else:
            response = client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=4096,
            )
            return response.choices[0].message.content

    def find_matches(
        self,
        poly_markets: list[PolyMarket],
        kalshi_markets: list[KalshiMarket],
    ) -> list[LLMMatch]:
        """
        Use LLM to find matching markets between platforms.

        Args:
            poly_markets: List of Polymarket markets
            kalshi_markets: List of Kalshi markets

        Returns:
            List of identified matches
        """
        # Build market lists for the prompt
        poly_list = "\n".join(f"- {m.question}" for m in poly_markets)
        kalshi_list = "\n".join(f"- {m.title}" for m in kalshi_markets)

        prompt = MATCH_PROMPT.format(
            polymarket_list=poly_list,
            kalshi_list=kalshi_list,
        )

        log.info(
            "Calling LLM for market matching",
            provider=self.provider,
            model=self.model,
            poly_count=len(poly_markets),
            kalshi_count=len(kalshi_markets),
        )

        response = self._call_llm(prompt)

        # Parse JSON response
        try:
            # Extract JSON from response (handle markdown code blocks)
            json_str = response
            if "```json" in response:
                json_str = response.split("```json")[1].split("```")[0]
            elif "```" in response:
                json_str = response.split("```")[1].split("```")[0]

            # Find JSON array in response
            json_str = json_str.strip()
            if not json_str.startswith("["):
                # Try to find JSON array in the text
                start = json_str.find("[")
                if start >= 0:
                    json_str = json_str[start:]

            # Handle truncated JSON by trying to fix it
            if not json_str.endswith("]"):
                # Find last complete object
                last_brace = json_str.rfind("}")
                if last_brace > 0:
                    json_str = json_str[:last_brace + 1] + "]"

            matches_data = json.loads(json_str.strip())

            matches = [
                LLMMatch(
                    polymarket_question=m["polymarket"],
                    kalshi_title=m["kalshi"],
                    confidence=m.get("confidence", "medium"),
                    reasoning=m.get("reasoning", ""),
                )
                for m in matches_data
            ]

            log.info("LLM matching complete", matches_found=len(matches))
            return matches

        except (json.JSONDecodeError, KeyError) as e:
            log.error("Failed to parse LLM response", error=str(e), response=response[:500])
            return []

    def match_batch(
        self,
        poly_markets: list[PolyMarket],
        kalshi_markets: list[KalshiMarket],
    ) -> list[MatchedEvent]:
        """
        Match markets and return MatchedEvent objects.

        Compatible with the existing EventMatcher interface.
        """
        # Get LLM matches
        llm_matches = self.find_matches(poly_markets, kalshi_markets)

        # Build lookup dicts
        poly_by_question = {m.question: m for m in poly_markets}
        kalshi_by_title = {m.title: m for m in kalshi_markets}

        # Convert to MatchedEvent objects
        matched_events = []
        for match in llm_matches:
            poly = poly_by_question.get(match.polymarket_question)
            kalshi = kalshi_by_title.get(match.kalshi_title)

            if poly and kalshi:
                confidence_map = {"high": 0.95, "medium": 0.75, "low": 0.55}
                matched_events.append(MatchedEvent(
                    polymarket=poly,
                    kalshi=kalshi,
                    confidence=confidence_map.get(match.confidence, 0.75),
                    match_type="llm",
                    reasoning=match.reasoning,
                ))
            else:
                log.warning(
                    "LLM returned non-existent market",
                    poly_found=poly is not None,
                    kalshi_found=kalshi is not None,
                )

        return matched_events


async def test_llm_matcher():
    """Test the LLM matcher with real data."""
    import asyncio
    from karb.api.gamma import GammaClient
    from karb.api.kalshi import KalshiClient

    # Fetch markets
    gamma = GammaClient()
    kalshi = KalshiClient()

    poly_markets = await gamma.fetch_all_active_markets(min_liquidity=1000.0)
    poly_markets = sorted(poly_markets, key=lambda m: m.liquidity, reverse=True)[:100]

    kalshi_markets = await kalshi.get_markets(status="open", limit=200)

    await gamma.close()
    await kalshi.close()

    # Run matcher
    matcher = LLMMatcher(provider="anthropic")
    matches = matcher.match_batch(poly_markets, kalshi_markets)

    print(f"\nFound {len(matches)} matches:\n")
    for m in matches:
        print(f"Polymarket: {m.polymarket.question[:60]}...")
        print(f"Kalshi:     {m.kalshi.title[:60]}...")
        print(f"Confidence: {m.confidence:.0%} | {m.reasoning}")
        print()


if __name__ == "__main__":
    import asyncio
    asyncio.run(test_llm_matcher())
