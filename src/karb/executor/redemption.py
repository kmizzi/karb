"""
Auto-redemption module for resolved Polymarket positions.

Uses polymarket-apis package to redeem winning positions back to USDC.
"""

import asyncio
from typing import Optional

import httpx

from karb.config import get_settings
from karb.utils.logging import get_logger

log = get_logger(__name__)

# Data API for fetching positions
DATA_API_URL = "https://data-api.polymarket.com"


async def get_redeemable_positions(wallet_address: str) -> list[dict]:
    """Fetch all redeemable positions for a wallet."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{DATA_API_URL}/positions?user={wallet_address}")
        if resp.status_code != 200:
            log.error("Failed to fetch positions", status=resp.status_code)
            return []

        positions = resp.json()
        redeemable = [p for p in positions if p.get("redeemable")]

        log.info(
            "Found redeemable positions",
            total_positions=len(positions),
            redeemable=len(redeemable),
        )
        return redeemable


def redeem_position_sync(
    private_key: str,
    condition_id: str,
    size: float,
    outcome_index: int,
    neg_risk: bool = False,
) -> Optional[dict]:
    """
    Redeem a single position synchronously.

    Args:
        private_key: Wallet private key
        condition_id: Market condition ID
        size: Number of shares to redeem
        outcome_index: 0 for first outcome, 1 for second
        neg_risk: Whether this is a negative risk market

    Returns:
        Transaction receipt or None if failed
    """
    try:
        from polymarket_apis import PolymarketWeb3Client

        # signature_type: 0=EOA, 1=Poly proxy, 2=Safe
        # We use 0 since we're trading with EOA wallet directly
        client = PolymarketWeb3Client(
            private_key=private_key,
            signature_type=0,  # EOA
            chain_id=137,  # Polygon mainnet
        )

        # Build amounts array based on outcome index
        # [first_outcome_shares, second_outcome_shares]
        if outcome_index == 0:
            amounts = [size, 0.0]
        else:
            amounts = [0.0, size]

        log.info(
            "Redeeming position",
            condition_id=condition_id[:20] + "...",
            amounts=amounts,
            neg_risk=neg_risk,
        )

        receipt = client.redeem_position(
            condition_id=condition_id,
            amounts=amounts,
            neg_risk=neg_risk,
        )

        log.info(
            "Redemption successful",
            tx_hash=receipt.transaction_hash if hasattr(receipt, 'transaction_hash') else str(receipt),
        )

        return receipt

    except Exception as e:
        log.error("Redemption failed", error=str(e))
        return None


async def redeem_all_positions() -> dict:
    """
    Redeem all redeemable positions for the configured wallet.

    Returns:
        Summary of redemption results
    """
    settings = get_settings()

    if not settings.private_key:
        log.error("No private key configured")
        return {"error": "No private key configured", "redeemed": 0}

    if not settings.wallet_address:
        log.error("No wallet address configured")
        return {"error": "No wallet address configured", "redeemed": 0}

    # Get redeemable positions
    positions = await get_redeemable_positions(settings.wallet_address)

    if not positions:
        log.info("No positions to redeem")
        return {"redeemed": 0, "total_value": 0, "positions": []}

    results = []
    total_value = 0

    # Redeem each position
    for pos in positions:
        condition_id = pos.get("conditionId")
        size = float(pos.get("size", 0))
        outcome_index = pos.get("outcomeIndex", 0)
        neg_risk = pos.get("negativeRisk", False)
        current_value = float(pos.get("currentValue", 0))
        title = pos.get("title", "Unknown")

        if not condition_id or size <= 0:
            continue

        # Skip positions with $0 value (losses) - they still need redemption
        # but won't return any USDC

        log.info(
            "Processing redemption",
            market=title[:50],
            size=size,
            value=current_value,
        )

        # Run redemption in thread pool to not block
        loop = asyncio.get_event_loop()
        receipt = await loop.run_in_executor(
            None,
            redeem_position_sync,
            settings.private_key.get_secret_value(),
            condition_id,
            size,
            outcome_index,
            neg_risk,
        )

        results.append({
            "market": title,
            "size": size,
            "value": current_value,
            "success": receipt is not None,
            "tx_hash": str(receipt.transaction_hash) if receipt and hasattr(receipt, 'transaction_hash') else None,
        })

        if receipt:
            total_value += current_value

        # Small delay between redemptions to avoid rate limits
        await asyncio.sleep(1)

    summary = {
        "redeemed": sum(1 for r in results if r["success"]),
        "failed": sum(1 for r in results if not r["success"]),
        "total_value": total_value,
        "positions": results,
    }

    log.info("Redemption complete", **summary)
    return summary


async def check_and_redeem() -> dict:
    """
    Check for redeemable positions and redeem them.
    This is the main entry point for the auto-redemption task.
    """
    settings = get_settings()

    # Only redeem in live mode
    if settings.dry_run:
        log.debug("Skipping redemption check in dry run mode")
        return {"skipped": True, "reason": "dry_run"}

    return await redeem_all_positions()
