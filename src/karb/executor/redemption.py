"""
Auto-redemption module for resolved Polymarket positions.

Uses polymarket-apis package to redeem winning positions back to USDC.
"""

import asyncio
from typing import Optional, Tuple

import httpx

from karb.config import get_settings
from karb.utils.logging import get_logger

log = get_logger(__name__)

# Data API for fetching positions
DATA_API_URL = "https://data-api.polymarket.com"

# Minimum MATIC balance required to attempt redemptions (in wei)
MIN_MATIC_FOR_REDEMPTION = 0.05  # ~$0.025 USD, enough for 1 transaction


async def get_matic_balance(wallet_address: str) -> float:
    """Get MATIC balance for gas fees."""
    try:
        from web3 import Web3

        settings = get_settings()
        w3 = Web3(Web3.HTTPProvider(settings.polygon_rpc_url))
        wallet = Web3.to_checksum_address(wallet_address)
        balance_wei = w3.eth.get_balance(wallet)
        return balance_wei / 1e18
    except Exception as e:
        log.error("Failed to get MATIC balance", error=str(e))
        return 0.0


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
) -> Tuple[Optional[dict], Optional[str]]:
    """
    Redeem a single position synchronously.

    Args:
        private_key: Wallet private key
        condition_id: Market condition ID
        size: Number of shares to redeem
        outcome_index: 0 for first outcome, 1 for second
        neg_risk: Whether this is a negative risk market

    Returns:
        Tuple of (Transaction receipt or None, error_type or None)
        error_type can be: "insufficient_gas", "contract_error", "other"
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

        log.debug(
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

        return receipt, None

    except Exception as e:
        error_str = str(e).lower()

        # Check for insufficient gas/funds error
        if "insufficient funds" in error_str:
            log.error(
                "Redemption failed - INSUFFICIENT GAS (MATIC)",
                error="Not enough MATIC for gas fees. Send MATIC to wallet.",
            )
            return None, "insufficient_gas"

        # Check for contract revert errors
        if "execution reverted" in error_str or "safemath" in error_str:
            log.warning(
                "Redemption failed - contract error",
                condition_id=condition_id[:20] + "...",
                error=str(e)[:100],
            )
            return None, "contract_error"

        log.error("Redemption failed", error=str(e))
        return None, "other"


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

    # Check gas balance first
    matic_balance = await get_matic_balance(settings.wallet_address)
    if matic_balance < MIN_MATIC_FOR_REDEMPTION:
        log.error(
            "INSUFFICIENT MATIC FOR REDEMPTIONS",
            matic_balance=f"{matic_balance:.6f}",
            required=f"{MIN_MATIC_FOR_REDEMPTION:.4f}",
            wallet=settings.wallet_address,
            action="Send MATIC/POL to wallet for gas fees",
        )
        return {
            "error": "insufficient_gas",
            "redeemed": 0,
            "matic_balance": matic_balance,
            "required": MIN_MATIC_FOR_REDEMPTION,
        }

    # Get redeemable positions
    positions = await get_redeemable_positions(settings.wallet_address)

    if not positions:
        log.debug("No positions to redeem")
        return {"redeemed": 0, "total_value": 0, "positions": []}

    # Calculate total redeemable value
    total_redeemable = sum(float(p.get("currentValue", 0)) for p in positions)
    log.info(
        "Starting redemption",
        positions=len(positions),
        total_value=f"${total_redeemable:.2f}",
        matic_balance=f"{matic_balance:.4f}",
    )

    results = []
    total_value = 0
    insufficient_gas_hit = False

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

        # Skip if we already hit insufficient gas - no point trying more
        if insufficient_gas_hit:
            results.append({
                "market": title,
                "size": size,
                "value": current_value,
                "success": False,
                "error": "skipped_no_gas",
            })
            continue

        log.debug(
            "Processing redemption",
            market=title[:50],
            size=size,
            value=current_value,
        )

        # Run redemption in thread pool to not block
        loop = asyncio.get_event_loop()
        receipt, error_type = await loop.run_in_executor(
            None,
            redeem_position_sync,
            settings.private_key.get_secret_value(),
            condition_id,
            size,
            outcome_index,
            neg_risk,
        )

        # Check if we ran out of gas
        if error_type == "insufficient_gas":
            insufficient_gas_hit = True
            results.append({
                "market": title,
                "size": size,
                "value": current_value,
                "success": False,
                "error": "insufficient_gas",
            })
            continue

        results.append({
            "market": title,
            "size": size,
            "value": current_value,
            "success": receipt is not None,
            "tx_hash": str(receipt.transaction_hash) if receipt and hasattr(receipt, 'transaction_hash') else None,
            "error": error_type,
        })

        if receipt:
            total_value += current_value

        # Small delay between redemptions to avoid rate limits
        await asyncio.sleep(1)

    summary = {
        "redeemed": sum(1 for r in results if r.get("success")),
        "failed": sum(1 for r in results if not r.get("success")),
        "total_value": total_value,
        "positions": results,
    }

    if insufficient_gas_hit:
        summary["error"] = "insufficient_gas"
        log.error(
            "Redemption stopped - OUT OF GAS",
            redeemed=summary["redeemed"],
            failed=summary["failed"],
            wallet=settings.wallet_address,
        )
    elif summary["redeemed"] > 0:
        log.info(
            "Redemption complete",
            redeemed=summary["redeemed"],
            total_value=f"${total_value:.2f}",
        )
    elif summary["failed"] > 0:
        log.warning(
            "Redemption complete with failures",
            redeemed=summary["redeemed"],
            failed=summary["failed"],
        )

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
