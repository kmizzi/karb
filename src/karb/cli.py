"""Command-line interface for karb."""

import asyncio
import sys
from typing import Optional

import click
from rich.console import Console
from rich.table import Table

from karb import __version__
from karb.config import get_settings, reload_settings
from karb.utils.logging import setup_logging

console = Console()


@click.group()
@click.version_option(version=__version__)
def cli() -> None:
    """Karb - Polymarket arbitrage bot."""
    pass


@cli.command()
@click.option("--dry-run/--live", default=True, help="Dry run mode (no real trades)")
@click.option("--realtime/--polling", default=True, help="Use real-time WebSocket (default) or legacy polling")
@click.option("--poll-interval", type=float, help="Seconds between scans (polling mode only)")
@click.option("--min-profit", type=float, help="Minimum profit threshold (e.g., 0.005 for 0.5%)")
@click.option("--max-position", type=float, help="Maximum position size in USD")
@click.option("--log-level", type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR"]), default="INFO")
def run(
    dry_run: bool,
    realtime: bool,
    poll_interval: Optional[float],
    min_profit: Optional[float],
    max_position: Optional[float],
    log_level: str,
) -> None:
    """Run the arbitrage bot."""
    import os

    # Override settings from CLI
    if dry_run is not None:
        os.environ["DRY_RUN"] = str(dry_run).lower()
    if poll_interval is not None:
        os.environ["POLL_INTERVAL_SECONDS"] = str(poll_interval)
    if min_profit is not None:
        os.environ["MIN_PROFIT_THRESHOLD"] = str(min_profit)
    if max_position is not None:
        os.environ["MAX_POSITION_SIZE"] = str(max_position)
    if log_level:
        os.environ["LOG_LEVEL"] = log_level

    reload_settings()
    setup_logging(log_level)

    settings = get_settings()

    mode = "[yellow]DRY RUN[/yellow]" if settings.dry_run else "[red]LIVE TRADING[/red]"
    engine = "[cyan]REAL-TIME WebSocket[/cyan]" if realtime else "[dim]Legacy Polling[/dim]"
    console.print(f"\n[bold]Karb Arbitrage Bot[/bold] - {mode}")
    console.print(f"[bold]Engine:[/bold] {engine}\n")

    if not settings.dry_run:
        if not settings.private_key or not settings.wallet_address:
            console.print(
                "[red]Error:[/red] Live trading requires PRIVATE_KEY and WALLET_ADDRESS.\n"
                "Set these in your .env file or environment."
            )
            sys.exit(1)

        console.print(f"[dim]Wallet:[/dim] {settings.wallet_address}")

    if not realtime:
        console.print(f"[dim]Poll interval:[/dim] {settings.poll_interval_seconds}s")
    console.print(f"[dim]Min profit:[/dim] {settings.min_profit_threshold * 100:.1f}%")
    console.print(f"[dim]Max position:[/dim] ${settings.max_position_size}")
    console.print()

    try:
        if realtime:
            from karb.bot import run_realtime_bot
            asyncio.run(run_realtime_bot())
        else:
            from karb.bot import run_bot
            asyncio.run(run_bot())
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted[/yellow]")


@cli.command()
def scan() -> None:
    """Scan markets once and show opportunities."""
    setup_logging("INFO")

    async def _scan() -> None:
        from karb.analyzer.arbitrage import ArbitrageAnalyzer
        from karb.scanner.market_scanner import MarketScanner

        console.print("[bold]Scanning markets...[/bold]\n")

        async with MarketScanner() as scanner:
            snapshots = await scanner.run_once()

            console.print(f"Found {len(snapshots)} active markets\n")

            analyzer = ArbitrageAnalyzer()
            opportunities = analyzer.analyze_batch(snapshots)

            if not opportunities:
                console.print("[yellow]No arbitrage opportunities found[/yellow]")
                return

            # Display opportunities
            table = Table(title="Arbitrage Opportunities")
            table.add_column("Market", style="cyan", max_width=40)
            table.add_column("YES Ask", justify="right")
            table.add_column("NO Ask", justify="right")
            table.add_column("Combined", justify="right")
            table.add_column("Profit %", justify="right", style="green")
            table.add_column("Max Size", justify="right")

            for opp in opportunities[:20]:  # Top 20
                table.add_row(
                    opp.market.question[:40],
                    f"${float(opp.yes_ask):.3f}",
                    f"${float(opp.no_ask):.3f}",
                    f"${float(opp.combined_cost):.3f}",
                    f"{float(opp.profit_pct) * 100:.2f}%",
                    f"${float(opp.max_trade_size):.0f}",
                )

            console.print(table)

    asyncio.run(_scan())


@cli.command()
@click.option("--limit", default=30, help="Maximum markets to show")
def markets(limit: int) -> None:
    """List active markets."""
    setup_logging("WARNING")

    async def _markets() -> None:
        from karb.api.gamma import GammaClient

        console.print("[bold]Fetching markets...[/bold]\n")

        async with GammaClient() as client:
            # Just fetch one page of markets
            raw_markets = await client.get_markets(active=True, limit=100)
            markets = []
            for raw in raw_markets:
                m = client.parse_market(raw)
                if m is not None:
                    markets.append(m)

            # Sort by volume
            markets.sort(key=lambda m: m.volume, reverse=True)

            table = Table(title=f"Active Markets (showing {min(limit, len(markets))} of {len(markets)})")
            table.add_column("Market", style="cyan", max_width=50)
            table.add_column("Volume", justify="right")
            table.add_column("Liquidity", justify="right")
            table.add_column("YES", justify="right")
            table.add_column("NO", justify="right")

            for market in markets[:limit]:
                table.add_row(
                    market.question[:50],
                    f"${float(market.volume):,.0f}",
                    f"${float(market.liquidity):,.0f}",
                    f"${float(market.yes_price):.2f}",
                    f"${float(market.no_price):.2f}",
                )

            console.print(table)

    asyncio.run(_markets())


@cli.command()
def config() -> None:
    """Show current configuration."""
    settings = get_settings()

    table = Table(title="Current Configuration")
    table.add_column("Setting", style="cyan")
    table.add_column("Value")

    # Trading
    table.add_row("Dry Run", str(settings.dry_run))
    table.add_row("Min Profit Threshold", f"{settings.min_profit_threshold * 100:.1f}%")
    table.add_row("Max Position Size", f"${settings.max_position_size}")
    table.add_row("Poll Interval", f"{settings.poll_interval_seconds}s")
    table.add_row("Min Liquidity", f"${settings.min_liquidity_usd}")

    # Network
    table.add_row("Polygon RPC", settings.polygon_rpc_url[:50])
    table.add_row("Chain ID", str(settings.chain_id))

    # Credentials
    wallet = settings.wallet_address or "[not set]"
    has_key = "[set]" if settings.private_key else "[not set]"
    table.add_row("Wallet Address", wallet)
    table.add_row("Private Key", has_key)

    # Alerts
    has_telegram = "[set]" if settings.telegram_bot_token else "[not set]"
    table.add_row("Telegram Bot", has_telegram)

    console.print(table)


@cli.command()
@click.argument("token_id")
def orderbook(token_id: str) -> None:
    """Show orderbook for a token."""
    setup_logging("WARNING")

    async def _orderbook() -> None:
        from karb.api.clob import ClobClient

        async with ClobClient() as client:
            ob = await client.get_orderbook(token_id)

            console.print(f"\n[bold]Orderbook for {token_id[:20]}...[/bold]\n")

            # Bids
            bid_table = Table(title="Bids (Buy Orders)")
            bid_table.add_column("Price", justify="right", style="green")
            bid_table.add_column("Size", justify="right")

            for level in sorted(ob.bids, key=lambda x: x.price, reverse=True)[:10]:
                bid_table.add_row(f"${float(level.price):.4f}", f"{float(level.size):,.2f}")

            # Asks
            ask_table = Table(title="Asks (Sell Orders)")
            ask_table.add_column("Price", justify="right", style="red")
            ask_table.add_column("Size", justify="right")

            for level in sorted(ob.asks, key=lambda x: x.price)[:10]:
                ask_table.add_row(f"${float(level.price):.4f}", f"{float(level.size):,.2f}")

            console.print(bid_table)
            console.print()
            console.print(ask_table)

            # Summary
            if ob.best_bid and ob.best_ask:
                spread = ob.best_ask - ob.best_bid
                console.print(f"\n[dim]Best Bid:[/dim] ${float(ob.best_bid):.4f}")
                console.print(f"[dim]Best Ask:[/dim] ${float(ob.best_ask):.4f}")
                console.print(f"[dim]Spread:[/dim] ${float(spread):.4f} ({float(spread / ob.best_ask) * 100:.2f}%)")

    asyncio.run(_orderbook())


def main() -> None:
    """Main entry point."""
    cli()


if __name__ == "__main__":
    main()
