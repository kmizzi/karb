"""FastAPI dashboard application."""

import secrets
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates

from karb.config import get_settings
from karb.data.database import init_async_db
from karb.data.repositories import (
    AlertRepository,
    ClosedPositionRepository,
    ExecutionRepository,
    MinuteStatsRepository,
    PortfolioRepository,
    StatsHistoryRepository,
    StatsRepository,
    TradeRepository,
)
from karb.executor.async_clob import create_async_clob_client
from karb.tracking.portfolio import PortfolioTracker
from karb.utils.logging import get_logger

log = get_logger(__name__)

# Template directory
TEMPLATE_DIR = Path(__file__).parent / "templates"
TEMPLATE_DIR.mkdir(exist_ok=True)

security = HTTPBasic(auto_error=False)


def verify_credentials(
    credentials: Optional[HTTPBasicCredentials] = Depends(security),
) -> Optional[str]:
    """Verify dashboard credentials if authentication is enabled."""
    settings = get_settings()

    # If no password configured, allow anonymous access
    if not settings.dashboard_password:
        return "anonymous"

    # If password is configured but no credentials provided, require auth
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Basic"},
        )

    correct_username = secrets.compare_digest(
        credentials.username.encode("utf8"),
        (settings.dashboard_username or "admin").encode("utf8"),
    )
    correct_password = secrets.compare_digest(
        credentials.password.encode("utf8"),
        settings.dashboard_password.encode("utf8"),
    )

    if not (correct_username and correct_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )

    return credentials.username


def create_app() -> FastAPI:
    """Create the FastAPI dashboard application."""
    app = FastAPI(
        title="Karb Dashboard",
        description="Polymarket Arbitrage Bot Dashboard",
        version="1.0.0",
    )

    templates = Jinja2Templates(directory=str(TEMPLATE_DIR))

    @app.on_event("startup")
    async def startup():
        """Initialize database on startup."""
        await init_async_db()
        log.info("Dashboard database initialized")

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(
        request: Request,
        username: str = Depends(verify_credentials),
    ):
        """Main dashboard page."""
        return templates.TemplateResponse(
            "dashboard.html",
            {"request": request, "username": username},
        )

    @app.get("/api/status")
    async def get_status(username: str = Depends(verify_credentials)):
        """Get current bot status."""
        settings = get_settings()

        # Read scanner stats from database
        scanner_stats = await StatsRepository.get()
        if not scanner_stats:
            scanner_stats = {
                "markets": 0,
                "price_updates": 0,
                "arbitrage_alerts": 0,
                "ws_connected": False,
                "last_update": None,
            }

        return {
            "bot": {
                "dry_run": settings.dry_run,
                "min_profit": f"{settings.min_profit_threshold * 100:.1f}%",
                "max_position": f"${settings.max_position_size}",
                "wallet": settings.wallet_address[:10] + "..." if settings.wallet_address else None,
            },
            "scanner": scanner_stats,
            "timestamp": datetime.now().isoformat(),
        }

    @app.get("/api/balance")
    async def get_balance(username: str = Depends(verify_credentials)):
        """Get current balances."""
        tracker = PortfolioTracker()
        try:
            balances = await tracker.get_current_balances()
            return balances
        except Exception as e:
            log.error("Failed to get balances", error=str(e))
            return {"error": str(e)}

    @app.get("/api/positions")
    async def get_positions(username: str = Depends(verify_credentials)):
        """Get current positions from Polymarket."""
        try:
            client = await create_async_clob_client()
            if not client:
                return {"positions": [], "error": "CLOB client not available"}

            positions = await client.get_positions()
            await client.close()

            # Format positions for display - Data API format
            formatted = []
            for p in positions:
                # Data API returns: asset, size, avgPrice, curPrice, initialValue, currentValue, cashPnl, etc.
                size = float(p.get("size", 0) or 0)
                if size == 0:
                    continue  # Skip zero positions

                # Determine status
                redeemable = p.get("redeemable", False)
                cur_price = float(p.get("curPrice", 0) or 0)
                if redeemable:
                    if cur_price >= 0.99:
                        status = "WON"
                    elif cur_price <= 0.01:
                        status = "LOST"
                    else:
                        status = "RESOLVED"
                else:
                    status = "OPEN"

                formatted.append({
                    "market": p.get("title", p.get("market", "")),
                    "outcome": p.get("outcome", ""),
                    "size": size,
                    "avg_price": float(p.get("avgPrice", 0) or 0),
                    "current_price": cur_price,
                    "pnl": float(p.get("cashPnl", 0) or 0),
                    "realized_pnl": float(p.get("realizedPnl", 0) or 0),
                    "initial_value": float(p.get("initialValue", 0) or 0),
                    "current_value": float(p.get("currentValue", 0) or 0),
                    "token_id": p.get("asset", ""),
                    "status": status,
                    "redeemable": redeemable,
                    "end_date": p.get("endDate", ""),
                })

            # Split into open and closed positions
            open_positions = [p for p in formatted if p["status"] == "OPEN"]
            closed_positions = [p for p in formatted if p["status"] != "OPEN"]

            # Store closed positions in database for history
            for pos in closed_positions:
                token_id = pos.get("token_id", "")
                if token_id and not await ClosedPositionRepository.exists(token_id):
                    await ClosedPositionRepository.insert(
                        timestamp=datetime.now().isoformat(),
                        market_title=pos.get("market"),
                        outcome=pos.get("outcome"),
                        token_id=token_id,
                        size=pos.get("size", 0),
                        avg_price=pos.get("avg_price"),
                        exit_price=pos.get("current_price"),
                        cost_basis=pos.get("initial_value"),
                        realized_value=pos.get("current_value"),
                        realized_pnl=pos.get("pnl"),
                        status=pos.get("status"),
                        redeemed=pos.get("redeemable", False),
                    )
                    log.info("Stored closed position", market=pos.get("market", "")[:30])

            return {
                "open": open_positions,
                "closed": closed_positions,
                "open_count": len(open_positions),
                "closed_count": len(closed_positions),
            }
        except Exception as e:
            log.error("Failed to get positions", error=str(e))
            return {"positions": [], "error": str(e)}

    @app.get("/api/closed-positions")
    async def get_closed_positions(
        limit: int = 20,
        offset: int = 0,
        redeemed: Optional[bool] = None,
        username: str = Depends(verify_credentials),
    ):
        """Get closed positions history from database with pagination."""
        positions = await ClosedPositionRepository.get_recent(
            limit=limit, offset=offset, redeemed=redeemed
        )
        total = await ClosedPositionRepository.get_total_count(redeemed=redeemed)
        return {
            "positions": [
                {
                    "timestamp": p.get("timestamp", ""),
                    "market": p.get("market_title", ""),
                    "outcome": p.get("outcome", ""),
                    "size": float(p.get("size", 0)),
                    "avg_price": float(p.get("avg_price", 0) or 0),
                    "exit_price": float(p.get("exit_price", 0) or 0),
                    "cost_basis": float(p.get("cost_basis", 0) or 0),
                    "realized_value": float(p.get("realized_value", 0) or 0),
                    "realized_pnl": float(p.get("realized_pnl", 0) or 0),
                    "status": p.get("status", ""),
                    "redeemed": bool(p.get("redeemed", 0)),
                }
                for p in positions
            ],
            "count": len(positions),
            "total": total,
            "offset": offset,
            "has_more": offset + len(positions) < total,
        }

    @app.get("/api/trades")
    async def get_trades(
        limit: int = 20,
        offset: int = 0,
        username: str = Depends(verify_credentials),
    ):
        """Get trades from database with pagination."""
        trades = await TradeRepository.get_recent(limit=limit, offset=offset)
        total = await TradeRepository.get_total_count()
        return {
            "trades": [
                {
                    "timestamp": t.get("timestamp", ""),
                    "platform": t.get("platform", ""),
                    "market": (t.get("market_name", "") or "")[:50],
                    "side": t.get("side", ""),
                    "outcome": t.get("outcome", ""),
                    "price": float(t.get("price", 0)),
                    "size": float(t.get("size", 0)),
                    "cost": float(t.get("cost", 0)),
                }
                for t in trades
            ],
            "count": len(trades),
            "total": total,
            "offset": offset,
            "has_more": offset + len(trades) < total,
        }

    @app.get("/api/pnl")
    async def get_pnl(username: str = Depends(verify_credentials)):
        """Get profit/loss summary."""
        daily = await TradeRepository.get_daily_summary(
            datetime.now().strftime("%Y-%m-%d")
        )
        all_time = await TradeRepository.get_all_time_summary()

        tracker = PortfolioTracker()
        pnl = await tracker.get_profit_loss()

        # Get execution stats for expected profit
        exec_stats = await ExecutionRepository.get_stats()

        # Get realized profit from closed positions
        closed_summary = await ClosedPositionRepository.get_profit_summary()

        return {
            "daily": daily,
            "all_time": all_time,
            "portfolio": pnl,
            "expected_profit": exec_stats.get("total_profit", 0),
            "realized": {
                "total_pnl": closed_summary.get("total_realized_pnl") or 0,
                "winning_positions": closed_summary.get("winning_positions") or 0,
                "losing_positions": closed_summary.get("losing_positions") or 0,
                "total_positions": closed_summary.get("total_positions") or 0,
                "total_cost": closed_summary.get("total_cost_basis") or 0,
                "total_value": closed_summary.get("total_realized_value") or 0,
            },
        }

    @app.get("/api/alerts")
    async def get_alerts(
        limit: int = 10,
        offset: int = 0,
        username: str = Depends(verify_credentials),
    ):
        """Get arbitrage alerts from database with pagination."""
        alerts = await AlertRepository.get_recent(limit=limit, offset=offset)
        total = await AlertRepository.get_total_count()
        return {
            "alerts": alerts,
            "count": len(alerts),
            "total": total,
            "offset": offset,
            "has_more": offset + len(alerts) < total,
        }

    @app.get("/api/near-miss-alerts")
    async def get_near_miss_alerts(
        limit: int = 10,
        offset: int = 0,
        username: str = Depends(verify_credentials),
    ):
        """Get near-miss (illiquid) arbitrage alerts from database with pagination."""
        from karb.data.repositories import NearMissAlertRepository

        alerts = await NearMissAlertRepository.get_recent(limit=limit, offset=offset)
        total = await NearMissAlertRepository.get_total_count()
        return {
            "alerts": [
                {
                    "timestamp": a.get("timestamp", ""),
                    "market": a.get("market", ""),
                    "profit_pct": float(a.get("profit_pct", 0) or 0),
                    "combined": float(a.get("combined", 0) or 0),
                    "yes_ask": float(a.get("yes_ask", 0) or 0),
                    "no_ask": float(a.get("no_ask", 0) or 0),
                    "yes_liquidity": float(a.get("yes_liquidity", 0) or 0),
                    "no_liquidity": float(a.get("no_liquidity", 0) or 0),
                    "min_required": float(a.get("min_required", 0) or 0),
                    "reason": a.get("reason", "insufficient_liquidity"),
                }
                for a in alerts
            ],
            "count": len(alerts),
            "total": total,
            "offset": offset,
            "has_more": offset + len(alerts) < total,
        }

    @app.get("/api/redeemable")
    async def get_redeemable(username: str = Depends(verify_credentials)):
        """Get positions that can be redeemed."""
        from karb.executor.redemption import get_redeemable_positions

        settings = get_settings()
        if not settings.wallet_address:
            return {"error": "No wallet configured", "positions": []}

        try:
            positions = await get_redeemable_positions(settings.wallet_address)
            total_value = sum(float(p.get("currentValue", 0)) for p in positions)

            return {
                "positions": [
                    {
                        "market": p.get("title", "Unknown"),
                        "outcome": p.get("outcome", "?"),
                        "size": p.get("size", 0),
                        "value": float(p.get("currentValue", 0)),
                        "pnl": float(p.get("cashPnl", 0)),
                    }
                    for p in positions
                ],
                "count": len(positions),
                "total_value": total_value,
            }
        except Exception as e:
            log.error("Failed to get redeemable positions", error=str(e))
            return {"error": str(e), "positions": []}

    @app.post("/api/redeem")
    async def redeem_positions(username: str = Depends(verify_credentials)):
        """Trigger redemption of all redeemable positions."""
        from karb.executor.redemption import redeem_all_positions

        settings = get_settings()
        if settings.dry_run:
            return {"error": "Cannot redeem in dry run mode", "redeemed": 0}

        try:
            result = await redeem_all_positions()
            return result
        except Exception as e:
            log.error("Redemption failed", error=str(e))
            return {"error": str(e), "redeemed": 0}

    @app.get("/api/orders")
    async def get_orders(username: str = Depends(verify_credentials)):
        """Get current order status and recent executions from database."""
        executions = await ExecutionRepository.get_recent(limit=20)
        stats = await ExecutionRepository.get_stats()

        # Format executions for dashboard compatibility
        formatted_executions = []
        for e in executions:
            # Parse timing data from JSON if present
            timing = None
            timing_json = e.get("timing_data")
            if timing_json:
                try:
                    import json
                    timing = json.loads(timing_json)
                except Exception:
                    pass

            formatted_executions.append({
                "timestamp": e.get("timestamp", ""),
                "market": e.get("market", ""),
                "status": e.get("status", ""),
                "yes_order": {
                    "order_id": e.get("yes_order_id"),
                    "status": e.get("yes_status", ""),
                    "price": float(e.get("yes_price", 0) or 0),
                    "size": float(e.get("yes_size", 0) or 0),
                    "filled_size": float(e.get("yes_filled_size", 0) or 0),
                    "error": e.get("yes_error"),
                },
                "no_order": {
                    "order_id": e.get("no_order_id"),
                    "status": e.get("no_status", ""),
                    "price": float(e.get("no_price", 0) or 0),
                    "size": float(e.get("no_size", 0) or 0),
                    "filled_size": float(e.get("no_filled_size", 0) or 0),
                    "error": e.get("no_error"),
                },
                "total_cost": float(e.get("total_cost", 0) or 0),
                "expected_profit": float(e.get("expected_profit", 0) or 0),
                "profit_pct": float(e.get("profit_pct", 0) or 0),
                "market_liquidity": float(e.get("market_liquidity", 0) or 0),
                "yes_liquidity": float(e.get("yes_liquidity", 0) or 0),
                "no_liquidity": float(e.get("no_liquidity", 0) or 0),
                "timing": timing,
            })

        return {
            "active_orders": [],  # Active orders are still in-memory
            "recent_executions": formatted_executions,
            "stats": {
                "total_attempts": stats.get("total_attempts", 0),
                "successful": stats.get("successful", 0),
                "partial": stats.get("partial", 0),
                "failed": stats.get("failed", 0),
                "cancelled": stats.get("cancelled", 0),
                "total_volume": stats.get("total_volume", 0),
                "total_profit": stats.get("total_profit", 0),
                "success_rate": stats.get("success_rate", 0),
            },
            "updated_at": datetime.now().isoformat(),
        }

    @app.get("/api/charts/hourly")
    async def get_hourly_chart_data(
        hours: int = 24,
        username: str = Depends(verify_credentials),
    ):
        """Get hourly stats for charting (price updates, market count)."""
        data = await StatsHistoryRepository.get_hourly(hours=hours)
        return {
            "hours": [d.get("hour", "") for d in data],
            "price_updates": [d.get("price_updates", 0) for d in data],
            "markets": [d.get("markets", 0) for d in data],
            "arbitrage_alerts": [d.get("arbitrage_alerts", 0) for d in data],
            "ws_connected": [bool(d.get("ws_connected", 0)) for d in data],
            "count": len(data),
        }

    @app.get("/api/charts/price-updates-minute")
    async def get_price_updates_minute_chart_data(
        minutes: int = 60,
        username: str = Depends(verify_credentials),
    ):
        """Get minute-level price updates for real-time charting."""
        # Use get_recent (recorded deltas) not get_recent_with_current (which mixes cumulative)
        data = await MinuteStatsRepository.get_recent(minutes=minutes)
        return {
            "minutes": [d.get("minute", "") for d in data],
            "price_updates": [d.get("price_updates", 0) for d in data],
            "ws_connected": [bool(d.get("ws_connected", 0)) for d in data],
            "count": len(data),
        }

    @app.get("/api/charts/daily-executions")
    async def get_daily_executions_chart_data(
        days: int = 30,
        username: str = Depends(verify_credentials),
    ):
        """Get daily execution stats for charting."""
        data = await StatsHistoryRepository.get_daily_executions(days=days)
        return {
            "dates": [d.get("date", "") for d in data],
            "total_executions": [d.get("total_executions", 0) for d in data],
            "filled": [d.get("filled", 0) for d in data],
            "partial": [d.get("partial", 0) for d in data],
            "failed": [d.get("failed", 0) for d in data],
            "total_volume": [float(d.get("total_volume", 0) or 0) for d in data],
            "total_profit": [float(d.get("total_profit", 0) or 0) for d in data],
            "count": len(data),
        }

    @app.get("/api/charts/daily-alerts")
    async def get_daily_alerts_chart_data(
        days: int = 7,
        username: str = Depends(verify_credentials),
    ):
        """Get daily arbitrage alert counts for charting."""
        data = await AlertRepository.get_daily_counts(days=days)
        return {
            "dates": [d.get("date", "") for d in data],
            "counts": [d.get("count", 0) for d in data],
            "count": len(data),
        }

    @app.get("/api/charts/daily-near-miss")
    async def get_daily_near_miss_chart_data(
        days: int = 7,
        username: str = Depends(verify_credentials),
    ):
        """Get daily near-miss alert counts for charting."""
        from karb.data.repositories import NearMissAlertRepository

        data = await NearMissAlertRepository.get_daily_counts(days=days)
        return {
            "dates": [d.get("date", "") for d in data],
            "counts": [d.get("count", 0) for d in data],
            "count": len(data),
        }

    @app.get("/api/arb-window-stats")
    async def get_arb_window_stats(
        username: str = Depends(verify_credentials),
    ):
        """Get arbitrage window duration statistics."""
        stats = await AlertRepository.get_window_stats()
        return stats

    @app.get("/api/charts/daily-balance")
    async def get_daily_balance_chart_data(
        days: int = 30,
        username: str = Depends(verify_credentials),
    ):
        """Get daily balance data for charting (USDC + positions value, PST timezone)."""
        data = await PortfolioRepository.get_daily_balances(days=days)
        return {
            "dates": [d.get("date", "") for d in data],
            "usdc": [d.get("usdc", 0) for d in data],
            "positions_value": [d.get("positions_value", 0) for d in data],
            "total": [d.get("total", 0) for d in data],
            "count": len(data),
        }

    return app


def run_dashboard(host: str = "0.0.0.0", port: int = 80):
    """Run the dashboard server."""
    import uvicorn

    log.info("Starting dashboard", host=host, port=port)

    app = create_app()
    uvicorn.run(app, host=host, port=port, log_level="info")
