import asyncio
import json
import uuid
from datetime import datetime, timezone
from typing import Annotated, Any

import numpy as np
import talib
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app import warrant_availability
from app.db import executions_collection, quant_systems_collection
from app.indicators import supertrend_bands
from app.models.market import Ticker
from app.orchestrator import get_pipeline
from app.policies.trend_detection import (
    TrendDetectionPolicyConfig,
    bar_indicator_values,
    build_trend_indicator_series,
    passes_rule_group,
)
from app.tools.finhub import FinHubTool
from app.tools.yfinance import YFinanceTool


def _to_series(dates: list[str], arr: Any, decimals: int = 4) -> list[dict]:
    return [{"time": dates[i], "value": round(float(arr[i]), decimals)}
            for i in range(len(arr)) if not np.isnan(arr[i])]


def _compute_sma(closes: list[float], dates: list[str], period: int) -> list[dict]:
    return _to_series(dates, talib.SMA(np.array(closes, dtype=float), timeperiod=period))


def _compute_ema(closes: list[float], dates: list[str], period: int) -> list[dict]:
    return _to_series(dates, talib.EMA(np.array(closes, dtype=float), timeperiod=period))


def _compute_adx(bars: list[Any], period: int = 14) -> tuple[list[dict], list[dict], list[dict]]:
    highs  = np.array([float(b.high)  for b in bars])
    lows   = np.array([float(b.low)   for b in bars])
    closes = np.array([float(b.close) for b in bars])
    dates  = [b.date.isoformat() for b in bars]
    return (
        _to_series(dates, talib.ADX(highs, lows, closes, timeperiod=period), 2),
        _to_series(dates, talib.PLUS_DI(highs, lows, closes, timeperiod=period), 2),
        _to_series(dates, talib.MINUS_DI(highs, lows, closes, timeperiod=period), 2),
    )


def _compute_supertrend(bars: list[Any], period: int = 10, multiplier: float = 3.0) -> list[dict]:
    highs  = np.array([float(b.high)  for b in bars])
    lows   = np.array([float(b.low)   for b in bars])
    closes = np.array([float(b.close) for b in bars])
    dates  = [b.date.isoformat() for b in bars]

    final_upper, final_lower = supertrend_bands(highs, lows, closes, period, multiplier)

    result: list[dict] = []
    trend = 1
    started = False
    for i in range(len(closes)):
        if np.isnan(final_upper[i]):
            continue
        if not started:
            started = True
        elif trend == 1 and closes[i] < final_lower[i]:
            trend = -1
        elif trend == -1 and closes[i] > final_upper[i]:
            trend = 1
        val = round(float(final_lower[i] if trend == 1 else final_upper[i]), 4)
        result.append({"time": dates[i], "value": val, "bull": trend == 1})
    return result

def _compute_signal_markers(
    bars: list[Any],
    policy_cfg: TrendDetectionPolicyConfig,
) -> list[dict]:
    """Return NEW/BREAK transition markers across the full bar history."""
    n = len(bars)
    if n < 70:
        return []

    dates  = [b.date.isoformat() for b in bars]
    series = build_trend_indicator_series(bars, policy_cfg)

    entry_rules = policy_cfg.entry_enabled_rules()
    exit_rules = policy_cfg.exit_enabled_rules()

    passes_new   = np.zeros(n, dtype=bool)
    passes_break = np.zeros(n, dtype=bool)
    for i in range(n):
        c = bar_indicator_values(i, series, policy_cfg, 60, 20)
        passes_new[i] = passes_rule_group(c, entry_rules, policy_cfg.new_min_true)
        passes_break[i] = passes_rule_group(c, exit_rules, policy_cfg.break_min_true)

    # State machine: OUT -[NEW]-> IN_TREND -[BREAK]-> OUT
    state = "OUT"
    markers: list[dict] = []
    for i in range(1, n):
        if state == "OUT" and passes_new[i] and not passes_new[i - 1]:
            markers.append({"time": dates[i], "position": "belowBar", "color": "#26a69a", "shape": "arrowUp", "text": "NEW"})
            state = "IN_TREND"
        elif state == "IN_TREND" and passes_break[i] and not passes_break[i - 1]:
            markers.append({"time": dates[i], "position": "aboveBar", "color": "#ef5350", "shape": "arrowDown", "text": "BREAK"})
            state = "OUT"
    return markers


router = APIRouter(prefix="/quant-systems")
templates = Jinja2Templates(directory="app/templates")

STAGES = ["universe", "research", "screening", "monitoring", "warrant_selection", "portfolio", "risk", "execution"]

STAGE_LABELS: dict[str, str] = {
    "universe": "Universe",
    "research": "Research",
    "screening": "Screening",
    "monitoring": "Monitoring",
    "warrant_selection": "Warrant Selection",
    "portfolio": "Portfolio",
    "risk": "Risk",
    "execution": "Execution",
}

_NO_ID = {"_id": 0}

# Keep task references alive to prevent GC before completion
_bg_tasks: set[asyncio.Task] = set()


def _fire(coro) -> None:
    task = asyncio.create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


def _stage_ctx(execution: dict, current_stage: str) -> dict:
    s_data = execution.get("stages", {}).get(current_stage, {})
    return {
        "execution": execution,
        "execution_id": execution["execution_id"],
        "qs_id": execution["quant_system_id"],
        "current_stage": current_stage,
        "stages": STAGES,
        "stage_labels": STAGE_LABELS,
        "stage_status": s_data.get("status", "pending"),
        "stage_result": s_data.get("result"),
        "stage_error": s_data.get("error"),
        "stage_progress": s_data.get("progress"),
    }


# ---------------------------------------------------------------------------
# Per-QS execution list
# ---------------------------------------------------------------------------

@router.get("/{qs_id}/executions", response_class=HTMLResponse)
async def list_qs_executions(request: Request, qs_id: str) -> HTMLResponse:
    qs = await quant_systems_collection().find_one({"quant_system_id": qs_id}, _NO_ID)
    executions = await executions_collection().find({"quant_system_id": qs_id}, _NO_ID).sort("created_at", -1).to_list()
    return templates.TemplateResponse(request, "executions/list.html", {"executions": executions, "qs": qs})


# ---------------------------------------------------------------------------
# New execution — reads config from parent QuantSystem
# ---------------------------------------------------------------------------

@router.post("/{qs_id}/executions", response_class=RedirectResponse)
async def create_execution(
    qs_id: str,
    hitl_mode: Annotated[bool, Form()] = True,
) -> RedirectResponse:
    qs = await quant_systems_collection().find_one({"quant_system_id": qs_id}, _NO_ID)
    if qs is None:
        return RedirectResponse(url=f"/quant-systems/{qs_id}/executions", status_code=303)
    execution_id = uuid.uuid4().hex[:6]
    execution_doc = {
        "execution_id": execution_id,
        "quant_system_id": qs_id,
        "created_at": datetime.now(timezone.utc),
        "indices": qs["indices"],
        "capital_eur": qs["capital_eur"],
        "hitl_mode": hitl_mode,
        "config_overrides": dict(qs.get("config_overrides", {})),
        "current_stage": STAGES[0],
        "status": "running",
        "stages": {s: {"status": "pending"} for s in STAGES},
    }
    execution_doc["stages"][STAGES[0]]["status"] = "running"
    await executions_collection().insert_one(execution_doc)
    _fire(get_pipeline().run_stage(execution_id, STAGES[0]))
    return RedirectResponse(url=f"/quant-systems/{qs_id}/executions/{execution_id}", status_code=303)


# ---------------------------------------------------------------------------
# Execution detail — redirect to current stage
# ---------------------------------------------------------------------------

@router.get("/{qs_id}/executions/{execution_id}", response_class=RedirectResponse)
async def execution_detail(qs_id: str, execution_id: str) -> RedirectResponse:
    execution = await executions_collection().find_one({"execution_id": execution_id}, _NO_ID)
    stage = execution["current_stage"] if execution else STAGES[0]
    return RedirectResponse(url=f"/quant-systems/{qs_id}/executions/{execution_id}/stages/{stage}")


# ---------------------------------------------------------------------------
# Stage review pages
# ---------------------------------------------------------------------------

@router.get("/{qs_id}/executions/{execution_id}/stages/{stage}", response_class=HTMLResponse)
async def stage_review(request: Request, qs_id: str, execution_id: str, stage: str) -> HTMLResponse:
    execution = await executions_collection().find_one({"execution_id": execution_id}, _NO_ID)
    ctx = _stage_ctx(execution, stage)
    if stage == "universe" and ctx.get("stage_result"):
        adr_isins = ctx["stage_result"].get("adr_isins", [])
        ctx["availability"] = await warrant_availability.availability_map(adr_isins)
    return templates.TemplateResponse(request, f"stages/{stage}.html", ctx)


# ---------------------------------------------------------------------------
# Approve — triggers the next stage
# ---------------------------------------------------------------------------

@router.post("/{qs_id}/executions/{execution_id}/stages/{stage}/approve", response_class=RedirectResponse)
async def approve_stage(
    qs_id: str,
    execution_id: str,
    stage: str,
    kept: Annotated[list[str] | None, Form()] = None,
) -> RedirectResponse:
    idx = STAGES.index(stage)
    if idx + 1 < len(STAGES):
        next_stage = STAGES[idx + 1]
        await executions_collection().update_one(
            {"execution_id": execution_id},
            {"$set": {
                f"stages.{stage}.status": "approved",
                "current_stage": next_stage,
                f"stages.{next_stage}.status": "running",
                "status": "running",
            }},
        )
        _fire(get_pipeline().run_stage(execution_id, next_stage))
        return RedirectResponse(url=f"/quant-systems/{qs_id}/executions/{execution_id}/stages/{next_stage}", status_code=303)

    await executions_collection().update_one(
        {"execution_id": execution_id},
        {"$set": {f"stages.{stage}.status": "approved", "status": "complete"}},
    )
    return RedirectResponse(url=f"/quant-systems/{qs_id}/executions/{execution_id}/stages/{stage}", status_code=303)


# ---------------------------------------------------------------------------
# Restart — re-runs from the chosen stage
# ---------------------------------------------------------------------------

@router.post("/{qs_id}/executions/{execution_id}/stages/{stage}/restart", response_class=RedirectResponse)
async def restart_stage(
    qs_id: str,
    execution_id: str,
    stage: str,
    from_stage: Annotated[str, Form()],
    policies_submitted: Annotated[str | None, Form()] = None,
    policy_supertrend: Annotated[str | None, Form()] = None,
    policy_ema20_rising: Annotated[str | None, Form()] = None,
    policy_adx_above: Annotated[str | None, Form()] = None,
    policy_adx_rising: Annotated[str | None, Form()] = None,
    policy_price_above_ema50: Annotated[str | None, Form()] = None,
    policy_tq60_above: Annotated[str | None, Form()] = None,
    policy_tq20_above: Annotated[str | None, Form()] = None,
    policy_tq60_min: Annotated[str | None, Form()] = None,
    policy_tq20_min: Annotated[str | None, Form()] = None,
    new_min_true: Annotated[str | None, Form()] = None,
    policy_supertrend_break: Annotated[str | None, Form()] = None,
    policy_ema20_falling_break: Annotated[str | None, Form()] = None,
    policy_adx_below_break: Annotated[str | None, Form()] = None,
    policy_adx_falling_break: Annotated[str | None, Form()] = None,
    policy_price_below_ema50_break: Annotated[str | None, Form()] = None,
    break_min_true: Annotated[str | None, Form()] = None,
) -> RedirectResponse:
    idx = STAGES.index(from_stage)
    updates: dict = {}
    for s in STAGES[idx:]:
        updates[f"stages.{s}.status"] = "pending"
        updates[f"stages.{s}.result"] = None
        updates[f"stages.{s}.error"] = None
    updates["current_stage"] = from_stage
    updates[f"stages.{from_stage}.status"] = "running"
    updates["status"] = "running"

    if policies_submitted is not None:
        parsed_new_min: int | None = None
        parsed_break_min: int | None = None
        parsed_tq60_min: float = 0.05
        parsed_tq20_min: float = 0.0
        try:
            parsed_new_min = int(new_min_true) if new_min_true not in (None, "") else None
        except ValueError:
            parsed_new_min = None
        try:
            parsed_break_min = int(break_min_true) if break_min_true not in (None, "") else None
        except ValueError:
            parsed_break_min = None
        try:
            parsed_tq60_min = float(policy_tq60_min) if policy_tq60_min not in (None, "") else 0.05
        except (ValueError, TypeError):
            parsed_tq60_min = 0.05
        try:
            parsed_tq20_min = float(policy_tq20_min) if policy_tq20_min not in (None, "") else 0.0
        except (ValueError, TypeError):
            parsed_tq20_min = 0.0
        if not np.isfinite(parsed_tq60_min):
            parsed_tq60_min = 0.05
        if not np.isfinite(parsed_tq20_min):
            parsed_tq20_min = 0.0
        parsed_tq60_min = max(0.0, min(parsed_tq60_min, 1.0))
        parsed_tq20_min = max(0.0, min(parsed_tq20_min, 1.0))
        new_selected = sum(
            [
                policy_supertrend is not None,
                policy_ema20_rising is not None,
                policy_adx_above is not None,
                policy_adx_rising is not None,
                policy_price_above_ema50 is not None,
                policy_tq60_above is not None,
                policy_tq20_above is not None,
            ]
        )
        break_selected = sum(
            [
                policy_supertrend_break is not None,
                policy_ema20_falling_break is not None,
                policy_adx_below_break is not None,
                policy_adx_falling_break is not None,
                policy_price_below_ema50_break is not None,
            ]
        )
        safe_new_min = None
        safe_break_min = None
        if parsed_new_min is not None and new_selected > 0:
            safe_new_min = max(1, min(parsed_new_min, new_selected))
        if parsed_break_min is not None and break_selected > 0:
            safe_break_min = max(1, min(parsed_break_min, break_selected))
        updates["config_overrides.screening"] = {
            "policy_supertrend": policy_supertrend is not None,
            "policy_ema20_rising": policy_ema20_rising is not None,
            "policy_adx_above": policy_adx_above is not None,
            "policy_adx_rising": policy_adx_rising is not None,
            "policy_price_above_ema50": policy_price_above_ema50 is not None,
            "policy_tq60_above": policy_tq60_above is not None,
            "policy_tq20_above": policy_tq20_above is not None,
            "policy_tq60_min": parsed_tq60_min,
            "policy_tq20_min": parsed_tq20_min,
            "new_min_true": safe_new_min,
            "policy_supertrend_break": policy_supertrend_break is not None,
            "policy_ema20_falling_break": policy_ema20_falling_break is not None,
            "policy_adx_below_break": policy_adx_below_break is not None,
            "policy_adx_falling_break": policy_adx_falling_break is not None,
            "policy_price_below_ema50_break": policy_price_below_ema50_break is not None,
            "break_min_true": safe_break_min,
        }

    await executions_collection().update_one({"execution_id": execution_id}, {"$set": updates})
    _fire(get_pipeline().run_stage(execution_id, from_stage))
    return RedirectResponse(url=f"/quant-systems/{qs_id}/executions/{execution_id}/stages/{from_stage}", status_code=303)


# ---------------------------------------------------------------------------
# Warrant availability — manual ISIN override / re-check (global, by ISIN)
# ---------------------------------------------------------------------------

@router.post("/{qs_id}/executions/{execution_id}/warrant-availability/override", response_class=RedirectResponse)
async def set_warrant_override(
    qs_id: str,
    execution_id: str,
    original_isin: Annotated[str, Form()],
    override_isin: Annotated[str, Form()] = "",
) -> RedirectResponse:
    override = override_isin.strip().upper()
    async with FinHubTool() as finhub:
        if override:
            await warrant_availability.set_override(finhub, original_isin, override)
        else:
            await warrant_availability.clear_override(original_isin)
    return RedirectResponse(
        url=f"/quant-systems/{qs_id}/executions/{execution_id}/stages/universe", status_code=303
    )


# ---------------------------------------------------------------------------
# Chart fragments (stubs)
# ---------------------------------------------------------------------------

@router.get("/{qs_id}/executions/{execution_id}/charts/screening/{ticker}", response_class=HTMLResponse)
async def chart_screening(qs_id: str, execution_id: str, ticker: str) -> HTMLResponse:
    execution = await executions_collection().find_one({"execution_id": execution_id}, _NO_ID)
    scr_cfg: dict[str, Any] = {}
    if execution:
        scr_cfg = execution.get("config_overrides", {}).get("screening", {})
    policy_cfg = TrendDetectionPolicyConfig.from_mapping(scr_cfg)
    try:
        t = Ticker(symbol=ticker)
        async with YFinanceTool() as yf:
            bars_map = await yf.fetch_ohlcv_batch([t], lookback_days=1460)
    except Exception as exc:
        return HTMLResponse(f"<p class='text-danger small mt-2'>Chart error: {exc}</p>")

    bars = bars_map.get(ticker, [])
    if not bars:
        return HTMLResponse(f"<p class='text-muted text-center small mt-4'>No data for {ticker}</p>")

    dates  = [b.date.isoformat() for b in bars]
    closes = [float(b.close)     for b in bars]
    ohlcv  = [
        {"time": d, "open": float(b.open), "high": float(b.high),
         "low": float(b.low), "close": float(b.close)}
        for d, b in zip(dates, bars)
    ]
    adx_data, plus_di, minus_di = _compute_adx(bars)
    signal_markers = _compute_signal_markers(bars, policy_cfg)
    chart_data = json.dumps({
        "ticker":         ticker,
        "ohlcv":          ohlcv,
        "ema20":          _compute_ema(closes, dates, 20),
        "ema50":          _compute_ema(closes, dates, 50),
        "sma200":         _compute_sma(closes, dates, 200),
        "adx":            adx_data,
        "plus_di":        plus_di,
        "minus_di":       minus_di,
        "supertrend":     _compute_supertrend(bars),
        "min_adx":        policy_cfg.min_adx,
        "signal_markers": signal_markers,
    })
    return HTMLResponse(
        f"<div class='d-flex flex-column gap-1' data-chart='{chart_data}'>"
        f"  <div class='d-flex justify-content-between align-items-center flex-wrap gap-1 mb-1'>"
        f"    <span class='small fw-semibold'>{ticker}</span>"
        f"    <div class='d-flex gap-1 flex-wrap'>"
        f"      <div class='btn-group btn-group-sm'>"
        f"        <button class='btn btn-outline-secondary' data-range='3M'>3M</button>"
        f"        <button class='btn btn-outline-secondary' data-range='6M'>6M</button>"
        f"        <button class='btn btn-outline-secondary active' data-range='1Y'>1Y</button>"
        f"        <button class='btn btn-outline-secondary' data-range='3Y'>3Y</button>"
        f"      </div>"
        f"      <div class='btn-group btn-group-sm'>"
        f"        <button class='btn btn-outline-secondary active' data-indicator='ema20'>EMA 20</button>"
        f"        <button class='btn btn-outline-secondary' data-indicator='ema50'>EMA 50</button>"
        f"        <button class='btn btn-outline-secondary' data-indicator='sma200'>SMA 200</button>"
        f"        <button class='btn btn-outline-secondary' data-indicator='supertrend'>SuperTrend</button>"
        f"        <button class='btn btn-outline-secondary active' data-indicator='adx'>ADX</button>"
        f"      </div>"
        f"    </div>"
        f"  </div>"
        f"  <div class='lw-price' style='height:380px'></div>"
        f"  <div class='lw-adx' style='height:120px'></div>"
        f"</div>"
    )


@router.get("/{qs_id}/executions/{execution_id}/charts/warrant_selection/{ticker}", response_class=HTMLResponse)
async def chart_warrant(qs_id: str, execution_id: str, ticker: str, strike: float | None = None, maturity: str | None = None, chart_symbol: str | None = None) -> HTMLResponse:
    # `chart_symbol` overrides the underlying charted (e.g. an ADR's warrants are
    # written on the EUR-listed stock); it keeps candles in the strike currency.
    plot_symbol = chart_symbol or ticker
    try:
        t = Ticker(symbol=plot_symbol)
        async with YFinanceTool() as yf:
            bars_map = await yf.fetch_ohlcv_batch([t], lookback_days=1460)
    except Exception as exc:
        return HTMLResponse(f"<p class='text-danger small mt-2'>Chart error: {exc}</p>")

    bars = bars_map.get(plot_symbol, [])
    if not bars:
        return HTMLResponse(f"<p class='text-muted text-center small mt-4'>No data for {plot_symbol}</p>")

    dates  = [b.date.isoformat() for b in bars]
    closes = [float(b.close)     for b in bars]
    ohlcv  = [
        {"time": d, "open": float(b.open), "high": float(b.high),
         "low": float(b.low), "close": float(b.close)}
        for d, b in zip(dates, bars)
    ]
    chart_data = json.dumps({
        "ticker":     plot_symbol,
        "ohlcv":      ohlcv,
        "ema20":      _compute_ema(closes, dates, 20),
        "ema50":      _compute_ema(closes, dates, 50),
        "sma200":     _compute_sma(closes, dates, 200),
        "supertrend": _compute_supertrend(bars),
        "adx":        [],
        "plus_di":    [],
        "minus_di":   [],
        "strike":     strike,
        "maturity":   maturity,
    })
    return HTMLResponse(
        f"<div class='d-flex flex-column gap-1' data-chart='{chart_data}'>"
        f"  <div class='d-flex justify-content-between align-items-center flex-wrap gap-1 mb-1'>"
        f"    <span class='small fw-semibold'>{plot_symbol}"
        + (f" — strike <strong>{strike:.2f}</strong>" if strike else "")
        + (f" — expires <strong>{maturity}</strong>" if maturity else "")
        + "</span>"
        "    <div class='btn-group btn-group-sm'>"
        "      <button class='btn btn-outline-secondary' data-range='3M'>3M</button>"
        "      <button class='btn btn-outline-secondary' data-range='6M'>6M</button>"
        "      <button class='btn btn-outline-secondary active' data-range='1Y'>1Y</button>"
        "      <button class='btn btn-outline-secondary' data-range='3Y'>3Y</button>"
        "    </div>"
        "  </div>"
        "  <div class='lw-price' style='height:340px'></div>"
        "</div>"
    )


@router.get("/{qs_id}/executions/{execution_id}/charts/portfolio", response_class=HTMLResponse)
async def chart_portfolio(qs_id: str, execution_id: str) -> HTMLResponse:
    return HTMLResponse("<p class='text-muted'>Portfolio weight chart — not yet implemented</p>")


@router.get("/{qs_id}/executions/{execution_id}/charts/risk", response_class=HTMLResponse)
async def chart_risk(qs_id: str, execution_id: str) -> HTMLResponse:
    return HTMLResponse("<p class='text-muted'>Risk weight chart — not yet implemented</p>")
