"""Main bot orchestration."""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Optional

from karb.analyzer.arbitrage import ArbitrageAnalyzer
from karb.api.models import ArbitrageOpportunity
from karb.config import get_settings
from karb.executor.executor import ExecutionResult, ExecutionStatus, OrderExecutor
from karb.notifications.slack import get_notifier
from karb.scanner.market_scanner import MarketScanner, MarketSnapshot
from karb.utils.logging import get_logger, setup_logging

log = get_logger(__name__)


@dataclass
class BotStats:
    """Runtime statistics for the bot."""

    started_at: datetime = field(default_factory=datetime.utcnow)
    scan_cycles: int = 0
    markets_scanned: int = 0
    opportunities_found: int = 0
    trades_executed: int = 0
    trades_successful: int = 0
    total_profit: Decimal = Decimal("0")


class ArbitrageBot:
    """
    Main arbitrage bot that coordinates scanning, analysis, and execution.

    Flow:
    1. Scanner polls markets for orderbook data
    2. Analyzer checks each market for arbitrage opportunities
    3. Executor places trades for profitable opportunities
    """

    def __init__(
        self,
        scanner: Optional[MarketScanner] = None,
        analyzer: Optional[ArbitrageAnalyzer] = None,
        executor: Optional[OrderExecutor] = None,
    ) -> None:
        settings = get_settings()

        # Initialize components
        self.scanner = scanner or MarketScanner(
            min_liquidity=settings.min_liquidity_usd,
        )
        self.analyzer = analyzer or ArbitrageAnalyzer()
        self.executor = executor or OrderExecutor()

        self.stats = BotStats()
        self._running = False
        self._pending_opportunities: list[ArbitrageOpportunity] = []

    async def process_snapshot(self, snapshot: MarketSnapshot) -> None:
        """
        Process a single market snapshot.

        Called by the scanner for each market update.
        """
        # Analyze for arbitrage
        opportunity = self.analyzer.analyze(snapshot)

        if opportunity is not None:
            self.stats.opportunities_found += 1
            self._pending_opportunities.append(opportunity)

    async def execute_opportunities(self) -> list[ExecutionResult]:
        """Execute all pending arbitrage opportunities."""
        results: list[ExecutionResult] = []

        if not self._pending_opportunities:
            return results

        # Sort by profit (best first)
        self._pending_opportunities.sort(key=lambda x: x.profit_pct, reverse=True)

        log.info(
            "Executing opportunities",
            count=len(self._pending_opportunities),
        )

        for opportunity in self._pending_opportunities:
            try:
                result = await self.executor.execute(opportunity)
                results.append(result)

                self.stats.trades_executed += 1
                if result.status == ExecutionStatus.FILLED:
                    self.stats.trades_successful += 1
                    self.stats.total_profit += result.expected_profit

            except Exception as e:
                log.error(
                    "Execution error",
                    market=opportunity.market.question[:30],
                    error=str(e),
                )

        # Clear pending
        self._pending_opportunities = []

        return results

    async def run_cycle(self) -> None:
        """Run a single scan/analyze/execute cycle."""
        self.stats.scan_cycles += 1

        # Scan all markets
        snapshots = await self.scanner.run_once()
        self.stats.markets_scanned += len(snapshots)

        # Analyze each snapshot
        for snapshot in snapshots:
            await self.process_snapshot(snapshot)

        # Execute any found opportunities
        if self._pending_opportunities:
            await self.execute_opportunities()

    async def run(self) -> None:
        """Run the bot continuously."""
        settings = get_settings()
        self._running = True

        mode = "DRY RUN" if settings.dry_run else "LIVE"
        log.info(
            f"Starting arbitrage bot [{mode}]",
            poll_interval=settings.poll_interval_seconds,
            min_profit=f"{settings.min_profit_threshold * 100:.1f}%",
            max_position=f"${settings.max_position_size}",
        )

        if not settings.dry_run and not self.executor.signer.is_configured:
            log.warning(
                "Trading credentials not configured. "
                "Set PRIVATE_KEY and WALLET_ADDRESS in .env for live trading."
            )

        try:
            while self._running:
                try:
                    await self.run_cycle()

                    # Log periodic stats
                    if self.stats.scan_cycles % 10 == 0:
                        self._log_stats()

                except Exception as e:
                    log.error("Cycle error", error=str(e))

                await asyncio.sleep(settings.poll_interval_seconds)

        except asyncio.CancelledError:
            log.info("Bot cancelled")
        finally:
            await self.shutdown()

    def stop(self) -> None:
        """Stop the bot."""
        log.info("Stopping bot...")
        self._running = False

    async def shutdown(self) -> None:
        """Clean shutdown of all components."""
        log.info("Shutting down...")
        self.stop()
        await self.scanner.close()
        await self.executor.close()
        self._log_stats()

    def _log_stats(self) -> None:
        """Log current statistics."""
        runtime = datetime.utcnow() - self.stats.started_at
        hours = runtime.total_seconds() / 3600

        log.info(
            "Bot statistics",
            runtime=f"{hours:.1f}h",
            cycles=self.stats.scan_cycles,
            markets=self.stats.markets_scanned,
            opportunities=self.stats.opportunities_found,
            trades=self.stats.trades_executed,
            successful=self.stats.trades_successful,
            profit=f"${float(self.stats.total_profit):.2f}",
        )

    def get_stats(self) -> dict:
        """Get current statistics."""
        runtime = datetime.utcnow() - self.stats.started_at

        return {
            "runtime_seconds": runtime.total_seconds(),
            "scan_cycles": self.stats.scan_cycles,
            "markets_scanned": self.stats.markets_scanned,
            "opportunities_found": self.stats.opportunities_found,
            "trades_executed": self.stats.trades_executed,
            "trades_successful": self.stats.trades_successful,
            "total_profit": float(self.stats.total_profit),
            "analyzer_stats": self.analyzer.get_stats(),
            "executor_stats": self.executor.get_stats(),
        }

    async def __aenter__(self) -> "ArbitrageBot":
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.shutdown()


async def run_bot() -> None:
    """Entry point for running the bot (legacy polling mode)."""
    settings = get_settings()
    setup_logging(settings.log_level)

    async with ArbitrageBot() as bot:
        await bot.run()


class RealtimeArbitrageBot:
    """
    Real-time arbitrage bot using WebSocket streaming.

    This is the fast version that reacts to price changes instantly
    instead of polling.
    """

    # How often to check for redeemable positions (in seconds)
    REDEMPTION_CHECK_INTERVAL = 300  # 5 minutes

    def __init__(self) -> None:
        from karb.scanner.realtime_scanner import RealtimeScanner

        settings = get_settings()

        self.scanner = RealtimeScanner(
            on_arbitrage=self._on_arbitrage,
            min_liquidity=settings.min_liquidity_usd,
        )
        self.executor = OrderExecutor()

        self.stats = BotStats()
        self._running = False
        self._execution_lock = asyncio.Lock()
        self._redemption_task: Optional[asyncio.Task] = None

    async def _on_arbitrage(self, alert) -> None:
        """Handle arbitrage alert from scanner."""
        from karb.api.models import ArbitrageOpportunity
        from karb.scanner.realtime_scanner import ArbitrageAlert

        alert: ArbitrageAlert = alert
        settings = get_settings()

        self.stats.opportunities_found += 1

        # Convert alert to opportunity
        # Limit trade size to available liquidity on BOTH sides
        max_position = Decimal(str(settings.max_position_size))
        available_size = min(alert.yes_size_available, alert.no_size_available)

        # Skip if insufficient liquidity on either side
        min_required_size = Decimal("10")  # Minimum $10 trade
        if available_size < min_required_size:
            log.warning(
                "Skipping arbitrage - insufficient liquidity",
                market=alert.market.question[:40],
                yes_available=float(alert.yes_size_available),
                no_available=float(alert.no_size_available),
                min_required=float(min_required_size),
            )
            return

        trade_size = min(available_size, max_position)

        opportunity = ArbitrageOpportunity(
            market=alert.market,
            yes_ask=alert.yes_ask,
            no_ask=alert.no_ask,
            combined_cost=alert.combined_cost,
            profit_pct=alert.profit_pct,
            yes_size_available=alert.yes_size_available,
            no_size_available=alert.no_size_available,
            max_trade_size=trade_size,
        )

        log.info(
            "Executing with liquidity-adjusted size",
            market=alert.market.question[:40],
            trade_size=float(trade_size),
            yes_liquidity=float(alert.yes_size_available),
            no_liquidity=float(alert.no_size_available),
        )

        # Execute immediately (with lock to prevent concurrent executions)
        async with self._execution_lock:
            try:
                result = await self.executor.execute(opportunity)

                self.stats.trades_executed += 1
                if result.status == ExecutionStatus.FILLED:
                    self.stats.trades_successful += 1
                    self.stats.total_profit += result.expected_profit

                    log.info(
                        "Trade executed successfully",
                        market=alert.market.question[:30],
                        profit=f"${float(result.expected_profit):.2f}",
                    )

            except Exception as e:
                log.error(
                    "Execution error",
                    market=alert.market.question[:30],
                    error=str(e),
                )

    async def _auto_redemption_loop(self) -> None:
        """Background task that periodically checks for and redeems resolved positions."""
        from karb.executor.redemption import check_and_redeem

        settings = get_settings()

        # Wait a bit before first check to let the bot stabilize
        await asyncio.sleep(60)

        log.info("Auto-redemption task started", interval=f"{self.REDEMPTION_CHECK_INTERVAL}s")

        while self._running:
            try:
                result = await check_and_redeem()

                if result.get("skipped"):
                    log.debug("Redemption skipped", reason=result.get("reason"))
                elif result.get("redeemed", 0) > 0:
                    log.info(
                        "Auto-redemption completed",
                        redeemed=result["redeemed"],
                        total_value=f"${result.get('total_value', 0):.2f}",
                    )

                    # Send notification for successful redemptions
                    try:
                        notifier = get_notifier()
                        await notifier.send_message(
                            f"💰 Auto-redeemed {result['redeemed']} position(s) "
                            f"for ${result.get('total_value', 0):.2f}"
                        )
                    except Exception:
                        pass

                elif result.get("error"):
                    log.error("Auto-redemption error", error=result["error"])

            except Exception as e:
                log.error("Auto-redemption task error", error=str(e))

            # Wait for next check
            await asyncio.sleep(self.REDEMPTION_CHECK_INTERVAL)

    async def run(self) -> None:
        """Run the real-time bot."""
        settings = get_settings()
        self._running = True

        mode = "DRY RUN" if settings.dry_run else "LIVE"
        log.info(
            f"Starting REAL-TIME arbitrage bot [{mode}]",
            min_profit=f"{settings.min_profit_threshold * 100:.1f}%",
            max_position=f"${settings.max_position_size}",
        )

        if not settings.dry_run and not self.executor.signer.is_configured:
            log.warning(
                "Trading credentials not configured. "
                "Set PRIVATE_KEY and WALLET_ADDRESS in .env for live trading."
            )

        # Send startup notification
        try:
            notifier = get_notifier()
            await notifier.notify_startup(mode=mode)
        except Exception as e:
            log.debug("Startup notification failed", error=str(e))

        # Start auto-redemption background task
        if not settings.dry_run:
            self._redemption_task = asyncio.create_task(self._auto_redemption_loop())
            log.info("Auto-redemption task scheduled")

        try:
            await self.scanner.run()
        except asyncio.CancelledError:
            log.info("Bot cancelled")
        finally:
            await self.shutdown()

    def stop(self) -> None:
        """Stop the bot."""
        log.info("Stopping bot...")
        self._running = False
        self.scanner.stop()

    async def shutdown(self) -> None:
        """Clean shutdown."""
        log.info("Shutting down...")
        self.stop()

        # Cancel auto-redemption task
        if self._redemption_task and not self._redemption_task.done():
            self._redemption_task.cancel()
            try:
                await self._redemption_task
            except asyncio.CancelledError:
                pass
            log.info("Auto-redemption task cancelled")

        # Send shutdown notification
        try:
            notifier = get_notifier()
            await notifier.notify_shutdown(reason="normal")
            await notifier.close()
        except Exception:
            pass

        await self.scanner.close()
        await self.executor.close()
        self._log_stats()

    def _log_stats(self) -> None:
        """Log statistics."""
        runtime = datetime.utcnow() - self.stats.started_at
        hours = runtime.total_seconds() / 3600

        scanner_stats = self.scanner.get_stats()

        log.info(
            "Bot statistics",
            runtime=f"{hours:.1f}h",
            markets=scanner_stats.get("markets", 0),
            price_updates=scanner_stats.get("price_updates", 0),
            opportunities=self.stats.opportunities_found,
            trades=self.stats.trades_executed,
            successful=self.stats.trades_successful,
            profit=f"${float(self.stats.total_profit):.2f}",
        )

    async def __aenter__(self) -> "RealtimeArbitrageBot":
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.shutdown()


async def run_realtime_bot() -> None:
    """Entry point for running the real-time bot."""
    settings = get_settings()
    setup_logging(settings.log_level)

    async with RealtimeArbitrageBot() as bot:
        await bot.run()
