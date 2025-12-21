"""Slack webhook notifications."""

import asyncio
from datetime import datetime
from decimal import Decimal
from typing import Optional

import aiohttp

from karb.config import get_settings
from karb.utils.logging import get_logger

log = get_logger(__name__)


class SlackNotifier:
    """Send notifications to Slack via webhook."""

    def __init__(self, webhook_url: Optional[str] = None) -> None:
        settings = get_settings()
        self.webhook_url = webhook_url or settings.slack_webhook_url
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def send(self, text: str, blocks: Optional[list] = None) -> bool:
        """Send a message to Slack."""
        if not self.webhook_url:
            log.debug("Slack webhook not configured, skipping notification")
            return False

        try:
            session = await self._get_session()
            payload = {"text": text}
            if blocks:
                payload["blocks"] = blocks

            async with session.post(self.webhook_url, json=payload) as resp:
                if resp.status == 200:
                    log.debug("Slack notification sent")
                    return True
                else:
                    log.warning("Slack notification failed", status=resp.status)
                    return False

        except Exception as e:
            log.error("Failed to send Slack notification", error=str(e))
            return False

    async def notify_arbitrage(
        self,
        market: str,
        yes_ask: Decimal,
        no_ask: Decimal,
        combined: Decimal,
        profit_pct: Decimal,
    ) -> bool:
        """Send arbitrage opportunity notification."""
        blocks = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": "Arbitrage Detected",
                    "emoji": True,
                }
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Market:*\n{market[:80]}"},
                    {"type": "mrkdwn", "text": f"*Profit:*\n+{float(profit_pct) * 100:.2f}%"},
                    {"type": "mrkdwn", "text": f"*YES Ask:*\n${float(yes_ask):.4f}"},
                    {"type": "mrkdwn", "text": f"*NO Ask:*\n${float(no_ask):.4f}"},
                    {"type": "mrkdwn", "text": f"*Combined:*\n${float(combined):.4f}"},
                ]
            },
            {
                "type": "context",
                "elements": [
                    {"type": "mrkdwn", "text": f"_{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}_"}
                ]
            }
        ]

        return await self.send(
            f"Arbitrage: {market[:50]}... +{float(profit_pct) * 100:.2f}%",
            blocks=blocks,
        )

    async def notify_trade(
        self,
        platform: str,
        market: str,
        side: str,
        outcome: str,
        price: Decimal,
        size: Decimal,
        status: str = "executed",
    ) -> bool:
        """Send trade execution notification."""
        emoji = "" if status == "executed" else ""
        color = "good" if status == "executed" else "warning"

        blocks = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f"{emoji} Trade {status.title()}",
                    "emoji": True,
                }
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Platform:*\n{platform}"},
                    {"type": "mrkdwn", "text": f"*Action:*\n{side.upper()} {outcome.upper()}"},
                    {"type": "mrkdwn", "text": f"*Price:*\n${float(price):.4f}"},
                    {"type": "mrkdwn", "text": f"*Size:*\n${float(size):.2f}"},
                    {"type": "mrkdwn", "text": f"*Market:*\n{market[:60]}"},
                ]
            },
            {
                "type": "context",
                "elements": [
                    {"type": "mrkdwn", "text": f"_{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}_"}
                ]
            }
        ]

        return await self.send(
            f"Trade: {side} {outcome} @ ${float(price):.3f} on {platform}",
            blocks=blocks,
        )

    async def notify_error(self, error: str, context: Optional[str] = None) -> bool:
        """Send error notification."""
        blocks = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": "Error Alert",
                    "emoji": True,
                }
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"```{error}```"}
            },
        ]

        if context:
            blocks.append({
                "type": "context",
                "elements": [
                    {"type": "mrkdwn", "text": f"Context: {context}"}
                ]
            })

        blocks.append({
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": f"_{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}_"}
            ]
        })

        return await self.send(f"Error: {error[:100]}", blocks=blocks)

    async def notify_startup(self, mode: str, markets: int = 0) -> bool:
        """Send bot startup notification."""
        settings = get_settings()

        blocks = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": "Karb Bot Started",
                    "emoji": True,
                }
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Mode:*\n{mode}"},
                    {"type": "mrkdwn", "text": f"*Min Profit:*\n{settings.min_profit_threshold * 100:.1f}%"},
                    {"type": "mrkdwn", "text": f"*Max Position:*\n${settings.max_position_size}"},
                    {"type": "mrkdwn", "text": f"*Markets:*\n{markets}"},
                ]
            },
            {
                "type": "context",
                "elements": [
                    {"type": "mrkdwn", "text": f"_{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}_"}
                ]
            }
        ]

        return await self.send(f"Karb bot started in {mode} mode", blocks=blocks)

    async def notify_shutdown(self, reason: str = "normal") -> bool:
        """Send bot shutdown notification."""
        return await self.send(f"Karb bot shutting down: {reason}")

    async def notify_daily_summary(
        self,
        trades: int,
        volume: float,
        profit: float,
        alerts: int,
    ) -> bool:
        """Send daily summary notification."""
        blocks = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": "Daily Summary",
                    "emoji": True,
                }
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Trades:*\n{trades}"},
                    {"type": "mrkdwn", "text": f"*Volume:*\n${volume:.2f}"},
                    {"type": "mrkdwn", "text": f"*Profit:*\n${profit:.2f}"},
                    {"type": "mrkdwn", "text": f"*Alerts:*\n{alerts}"},
                ]
            },
            {
                "type": "context",
                "elements": [
                    {"type": "mrkdwn", "text": f"_{datetime.now().strftime('%Y-%m-%d')}_"}
                ]
            }
        ]

        return await self.send(
            f"Daily: {trades} trades, ${volume:.2f} volume, ${profit:.2f} profit",
            blocks=blocks,
        )


# Global notifier instance
_notifier: Optional[SlackNotifier] = None


def get_notifier() -> SlackNotifier:
    """Get the global Slack notifier instance."""
    global _notifier
    if _notifier is None:
        _notifier = SlackNotifier()
    return _notifier
