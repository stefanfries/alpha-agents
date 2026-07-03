import asyncio
import logging
import re
import traceback
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from app import warrant_availability
from app.agents.execution import TradeExecutionAgent
from app.agents.monitoring import MonitoringAgent, MonitoringInput, WarrantSnapshot
from app.agents.portfolio import PortfolioConstructionAgent
from app.agents.research import ResearchAgent, ResearchInput
from app.agents.risk import RiskAgent
from app.agents.screening import SecuritySelectionAgent
from app.agents.universe import UniverseAgent, UniverseInput
from app.agents.warrant_selection import WarrantSelectionAgent
from app.config import MonitoringSettings, resolve_warrant_selection_settings, settings
from app.db import (
    executions_collection,
    finance_db,
    quant_systems_collection,
    update_stage_progress,
    virtual_depot_snapshots_collection,
    virtual_depot_transactions_collection,
    warrant_underlying_map_collection,
)
from app.models.market import Position, Ticker
from app.models.signals import (
    ExecutionPlan,
    MonitoringResult,
    PortfolioProposal,
    ResearchResult,
    RiskAssessment,
    SelectionResult,
    UniverseResult,
    WarrantSelectionResult,
)
from app.tools.finhub import FinHubTool
from app.tools.wikipedia import WikipediaIndexTool
from app.tools.yfinance import YFinanceTool

logger = logging.getLogger(__name__)


class Pipeline:
    def _portfolio_max_positions(self, run: dict) -> int:
        """Resolve max positions from execution config, fallback to global settings."""
        portfolio_cfg = run.get("config_overrides", {}).get("portfolio", {})
        raw = portfolio_cfg.get("max_positions")
        if raw is None:
            return settings.portfolio.max_positions
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return settings.portfolio.max_positions
        return value if value > 0 else settings.portfolio.max_positions

    async def run_stage(self, execution_id: str, stage: str) -> None:
        coll = executions_collection()
        run = await coll.find_one({"execution_id": execution_id})
        if run is None:
            logger.error("run_stage: execution %r not found", execution_id)
            return
        try:
            result = await self._dispatch(stage, run)
            await coll.update_one(
                {"execution_id": execution_id},
                {"$set": {
                    f"stages.{stage}.result": result.model_dump(mode="json"),
                    f"stages.{stage}.status": "awaiting_review",
                    "status": "awaiting_review",
                }},
            )
        except Exception:
            tb = traceback.format_exc()
            logger.exception("Stage %r failed for execution %s", stage, execution_id)
            await coll.update_one(
                {"execution_id": execution_id},
                {"$set": {
                    f"stages.{stage}.status": "error",
                    f"stages.{stage}.error": tb,
                    "status": "error",
                }},
            )

    async def _dispatch(self, stage: str, run: dict) -> Any:
        match stage:
            case "universe":
                return await self._run_universe(run)
            case "research":
                return await self._run_research(run)
            case "screening":
                return await self._run_screening(run)
            case "monitoring":
                return await self._run_monitoring(run)
            case "warrant_selection":
                return await self._run_warrant_selection(run)
            case "portfolio":
                return await self._run_portfolio(run)
            case "risk":
                return await self._run_risk(run)
            case "execution":
                return await self._run_execution(run)
            case _:
                raise ValueError(f"Unknown stage: {stage!r}")

    async def _run_universe(self, run: dict) -> UniverseResult:
        execution_id = run["execution_id"]
        async with FinHubTool() as finhub, WikipediaIndexTool() as wikipedia:
            await self._wake_finhub(execution_id, finhub, "universe")
            result = await UniverseAgent(finhub=finhub, wikipedia=wikipedia).run(
                UniverseInput(indices=run.get("indices", []))
            )

            # Warrant availability is only uncertain for ADRs — regular stocks
            # reliably have warrants at comdirect, so only ADR ISINs are scanned.
            adr_set = set(result.adr_isins)
            adr_tickers = [t for t in result.tickers if t.isin in adr_set]

            async def on_avail_progress(done: int, total: int) -> None:
                await update_stage_progress(execution_id, "universe", {
                    "step": "warrants", "done": done, "total": total,
                })

            await warrant_availability.scan(finhub, adr_tickers, on_avail_progress)
            await update_stage_progress(execution_id, "universe", None)
            return result

    async def _wake_finhub(self, execution_id: str, finhub: FinHubTool, stage: str) -> None:
        """Ping FinHub; if cold-start takes >3 s, surface a progress message."""
        ping_task = asyncio.create_task(finhub.ping())
        await asyncio.sleep(3)
        if not ping_task.done():
            await update_stage_progress(execution_id, stage, {
                "message": "Waking up FinHub API (may take 30–60 s)…",
                "waking_up_since": datetime.now(timezone.utc).isoformat(),
            })
            try:
                await ping_task
            except Exception:
                pass
            await update_stage_progress(execution_id, stage, None)

    async def _run_research(self, run: dict) -> ResearchResult:
        execution_id = run["execution_id"]
        universe = UniverseResult.model_validate(run["stages"]["universe"]["result"])
        total = len(universe.tickers)
        _last: list[int] = [0]
        batch = max(1, total // 20)

        async def on_progress(step: str, done: int, total: int) -> None:
            if step == "ohlcv" or done - _last[0] >= batch or done == total:
                _last[0] = done
                await update_stage_progress(execution_id, "research", {"step": step, "done": done, "total": total})

        async with YFinanceTool() as yf:
            result = await ResearchAgent(tool=yf, on_progress=on_progress).run(
                ResearchInput(tickers=universe.tickers, lookback_days=settings.research.lookback_days)
            )
        # OHLCV bars are excluded from the stored document — 500+ tickers × 365 bars
        # exceeds MongoDB's 16 MB BSON limit. Bars are re-fetched in the screening stage.
        return ResearchResult(tickers=result.tickers, bars={}, fundamentals=result.fundamentals)

    async def _run_screening(self, run: dict) -> SelectionResult:
        research = ResearchResult.model_validate(run["stages"]["research"]["result"])
        async with YFinanceTool() as yf:
            bars = await yf.fetch_ohlcv_batch(research.tickers, settings.research.lookback_days)
        research_with_bars = ResearchResult(
            tickers=research.tickers, bars=bars, fundamentals=research.fundamentals
        )
        overrides = run.get("config_overrides", {}).get("screening", {})
        screening_cfg = settings.screening.model_copy(update=overrides)
        return await SecuritySelectionAgent(
            settings=screening_cfg,
        ).run(research_with_bars)

    async def _run_warrant_selection(self, run: dict) -> WarrantSelectionResult:
        execution_id = run["execution_id"]
        # Use monitoring's entry_candidates if the monitoring stage ran;
        # fall back to the full screening selection for backward compatibility.
        monitoring_data = run.get("stages", {}).get("monitoring", {}).get("result")
        if monitoring_data:
            monitoring = MonitoringResult.model_validate(monitoring_data)
            candidates = monitoring.entry_candidates
        else:
            screening = SelectionResult.model_validate(run["stages"]["screening"]["result"])
            candidates = screening.selected

        research = ResearchResult.model_validate(run["stages"]["research"]["result"])
        prices: dict[str, float] = {
            sym: float(fund["currentPrice"])
            for sym, fund in research.fundamentals.items()
            if fund.get("currentPrice")
        }

        async def on_progress(done: int, total: int, active: list[str]) -> None:
            await update_stage_progress(execution_id, "warrant_selection", {"done": done, "total": total, "active": active})

        overrides = await warrant_availability.overrides_map()
        ws_overrides = run.get("config_overrides", {}).get("warrant_selection", {})
        ws_cfg = resolve_warrant_selection_settings(ws_overrides)
        async with FinHubTool() as finhub:
            await self._wake_finhub(execution_id, finhub, "warrant_selection")
            result = await WarrantSelectionAgent(
                finhub=finhub,
                prices=prices,
                min_days_to_expiry=ws_cfg.min_days_to_expiry,
                max_days_to_expiry=ws_cfg.max_days_to_expiry,
                strike_min_factor=ws_cfg.strike_min_factor,
                strike_max_factor=ws_cfg.strike_max_factor,
                atm_band_fallback=ws_cfg.atm_band_fallback,
                isin_overrides=overrides,
                on_progress=on_progress,
            ).run(SelectionResult(selected=candidates, scores={t.symbol: 1.0 for t in candidates}, rationale={}))

        # Wire monitoring metadata if available
        monitoring_data = run.get("stages", {}).get("monitoring", {}).get("result")
        if monitoring_data:
            monitoring = MonitoringResult.model_validate(monitoring_data)
            result.keep_existing_isins = monitoring.keep_existing_isins or []
            result.roll_underlyings = monitoring.roll_underlyings or []
            result.roll_keep_underlyings = monitoring.roll_keep_underlyings or []

        return result

    async def _run_monitoring(self, run: dict) -> MonitoringResult:
        screening = SelectionResult.model_validate(run["stages"]["screening"]["result"])
        name_source = screening.all_tickers or screening.selected
        universe_names_by_isin = {t.isin: t.name for t in name_source if t.isin and t.name}
        underlying_names = {t.symbol: t.name for t in name_source if t.name}
        screening_symbols = set(screening.trend_signals.keys())
        current_holdings = await self._fetch_holdings(run)
        max_positions = self._portfolio_max_positions(run)

        # No holdings → pass all screening candidates through as entry candidates
        if not current_holdings:
            free = max_positions
            return MonitoringResult(
                positions_to_sell=[],
                positions_to_keep=[],
                positions_to_roll=[],
                entry_candidates=screening.selected[:max_positions],
                free_positions=free,
                excluded_symbols=[],
            )

        warrant_underlying_map = await self._fetch_warrant_underlying_map(run, current_holdings)
        if screening_symbols:
            warrant_underlying_map = {
                key: self._normalize_underlying_symbol_for_screening(value, screening_symbols)
                for key, value in warrant_underlying_map.items()
            }
        warrant_isins = [pos.ticker.isin for pos in current_holdings if pos.ticker.isin]
        warrant_snapshots = await self._fetch_warrant_snapshots(warrant_isins)
        # Prefer canonical universe names by resolving each held warrant to underlying ISIN via /instruments.
        names_from_universe = await self._resolve_underlying_names_from_universe(
            holdings=current_holdings,
            warrant_underlying_map=warrant_underlying_map,
            universe_names_by_isin=universe_names_by_isin,
        )
        underlying_names.update(names_from_universe)
        held_identifiers = self._held_warrant_identifiers(current_holdings)
        if held_identifiers:
            # For symbols not covered by universe names, use cached fallback names.
            for symbol, name in (await self._underlying_names_from_cache(held_identifiers)).items():
                underlying_names.setdefault(symbol, name)
        held_since_map = await self._fetch_held_since(run)
        break_confirmed_symbols = await self._break_confirmed_symbols(run, screening)
        overrides = run.get("config_overrides", {}).get("monitoring", {})
        if overrides:
            base = settings.monitoring.model_dump()
            for k, v in overrides.items():
                if isinstance(v, dict) and isinstance(base.get(k), dict):
                    base[k] = {**base[k], **v}
                else:
                    base[k] = v
            mon_cfg = MonitoringSettings.model_validate(base)
        else:
            mon_cfg = settings.monitoring
        result = await MonitoringAgent(
            settings=mon_cfg,
            max_positions=max_positions,
        ).run(MonitoringInput(
            candidates=screening.selected,
            scores=screening.scores,
            trend_signals=screening.trend_signals,
            underlying_names=underlying_names,
            current_holdings=current_holdings,
            warrant_underlying_map=warrant_underlying_map,
            held_since_map=held_since_map,
            warrant_snapshots=warrant_snapshots,
            break_confirmed_symbols=break_confirmed_symbols,
            max_positions=max_positions,
        ))

        # Monitoring is classification-only. Replacement lookup happens in warrant_selection.
        result.keep_existing_isins = []
        result.roll_underlyings = sorted({p.underlying_symbol for p in result.positions_to_roll if p.underlying_symbol})
        result.roll_keep_underlyings = []

        return result

    @staticmethod
    def _normalize_underlying_symbol_for_screening(
        symbol: str,
        screening_symbols: set[str],
    ) -> str:
        if symbol in screening_symbols:
            return symbol
        if "." in symbol:
            base = symbol.split(".", 1)[0]
            if base in screening_symbols:
                return base
        return symbol

    async def _break_confirmed_symbols(self, run: dict, screening: SelectionResult) -> set[str]:
        """Confirm BREAK on two consecutive closed candles (same-day reruns don't count)."""
        previous_break_candles = await self._previous_break_candle_dates(run)
        confirmed: set[str] = set()
        for symbol, signal in screening.trend_signals.items():
            if signal != "BREAK":
                continue
            previous_candle = screening.previous_candle_dates.get(symbol)
            if previous_candle and previous_break_candles.get(symbol) == previous_candle:
                confirmed.add(symbol)
        return confirmed

    async def _previous_break_candle_dates(self, run: dict) -> dict[str, date]:
        """Return prior execution BREAK candle dates by symbol for this quant system."""
        qs_id = run.get("quant_system_id")
        if not qs_id:
            return {}
        previous_run = await executions_collection().find_one(
            {
                "quant_system_id": qs_id,
                "execution_id": {"$ne": run["execution_id"]},
                "stages.screening.result": {"$exists": True},
            },
            sort=[("created_at", -1)],
        )
        if not previous_run:
            return {}
        screening_data = previous_run.get("stages", {}).get("screening", {}).get("result")
        if not screening_data:
            return {}
        previous_screening = SelectionResult.model_validate(screening_data)
        return {
            symbol: candle_date
            for symbol, candle_date in previous_screening.latest_candle_dates.items()
            if previous_screening.trend_signals.get(symbol) == "BREAK"
        }

    async def _fetch_warrant_snapshots(self, warrant_isins: list[str]) -> dict[str, WarrantSnapshot]:
        """Fetch current warrant snapshot data for monitoring health checks."""
        if not warrant_isins:
            return {}

        unique_isins = list(dict.fromkeys(x for x in warrant_isins if x))
        snapshots: dict[str, WarrantSnapshot] = {}
        today = date.today()

        async with FinHubTool() as finhub:
            for isin in unique_isins:
                try:
                    detail = await finhub.get_warrant_detail(isin)
                except Exception:
                    logger.warning("Monitoring snapshots: detail fetch failed for %s", isin)
                    continue
                if not detail:
                    logger.warning("Monitoring snapshots: no detail for %s", isin)
                    continue

                md = detail.get("market_data") or {}
                an = detail.get("analytics") or {}
                rd = detail.get("reference_data") or {}

                spread_pct = self._as_float(md.get("spread_percent"))
                leverage = self._as_float(an.get("leverage"))
                delta = self._as_float(an.get("delta"))
                days_to_maturity = self._days_to_maturity(rd.get("maturity_date"), today)

                bid = self._as_float(md.get("bid"))
                ask = self._as_float(md.get("ask"))
                bid_ask_midprice = (bid + ask) / 2.0 if bid is not None and ask is not None else None

                if all(
                    value is None
                    for value in (spread_pct, leverage, delta, days_to_maturity, bid_ask_midprice)
                ):
                    logger.warning("Monitoring snapshots: empty metrics for %s", isin)
                    continue

                snapshots[isin] = WarrantSnapshot(
                    warrant_isin=isin,
                    spread_pct=spread_pct,
                    leverage=leverage,
                    days_to_maturity=days_to_maturity,
                    delta=delta,
                    bid_ask_midprice=bid_ask_midprice,
                )

        return snapshots

    @staticmethod
    def _as_float(value: Any) -> float | None:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _days_to_maturity(value: Any, today: date) -> int | None:
        if value is None:
            return None
        if isinstance(value, date):
            maturity_date = value
        elif isinstance(value, datetime):
            maturity_date = value.date()
        elif isinstance(value, str):
            raw = value.strip()
            if raw.endswith("Z"):
                raw = raw[:-1] + "+00:00"
            try:
                maturity_date = datetime.fromisoformat(raw).date()
            except ValueError:
                try:
                    maturity_date = date.fromisoformat(raw)
                except ValueError:
                    return None
        else:
            return None
        return (maturity_date - today).days

    async def _fetch_warrant_underlying_map(self, run: dict, holdings: list[Position]) -> dict[str, str]:
        """Resolve map for held warrants using: previous run -> cache -> FinHub fallback.

        Returns a key-flexible mapping that may contain both warrant ISIN and WKN keys.
        """
        result: dict[str, str] = {}

        # 1) Baseline from the last approved warrant_selection stage.
        result.update(await self._map_from_last_approved_warrant_selection(run))

        # 2) Add cached resolutions for currently held identifiers.
        identifiers = self._held_warrant_identifiers(holdings)
        missing_name_identifiers: set[str] = set()
        if identifiers:
            result.update(await self._map_from_cache(identifiers))
            missing_name_identifiers = await self._identifiers_missing_underlying_name_from_cache(identifiers)

        # 3) Resolve remaining holdings live via FinHub /instruments and persist.
        # Also refresh legacy cache entries where symbol exists but underlying_name is missing.
        needs_refresh: list[Position] = []
        for pos in holdings:
            has_symbol = bool(self._resolve_underlying_symbol_for_position(pos, result))
            has_missing_name = bool(
                (pos.ticker.isin and pos.ticker.isin in missing_name_identifiers)
                or (pos.ticker.symbol and pos.ticker.symbol in missing_name_identifiers)
            )
            if not has_symbol or has_missing_name:
                needs_refresh.append(pos)

        if needs_refresh:
            async with FinHubTool() as finhub:
                for pos in needs_refresh:
                    resolved = await self._resolve_warrant_underlying_via_instruments(finhub, pos)
                    if not resolved:
                        continue
                    underlying_symbol = resolved["underlying_symbol"]
                    warrant_isin = resolved.get("warrant_isin")
                    warrant_wkn = resolved.get("warrant_wkn")
                    if warrant_isin:
                        result[warrant_isin] = underlying_symbol
                    if warrant_wkn:
                        result[warrant_wkn] = underlying_symbol
                    await self._persist_warrant_underlying_mapping(
                        warrant_isin=warrant_isin,
                        warrant_wkn=warrant_wkn,
                        underlying_symbol=underlying_symbol,
                        underlying_isin=resolved.get("underlying_isin"),
                        underlying_name=resolved.get("underlying_name"),
                        source="finhub_instruments_fallback",
                        resolved_from=resolved.get("resolved_from"),
                    )

        return result

    async def _identifiers_missing_underlying_name_from_cache(self, identifiers: set[str]) -> set[str]:
        if not identifiers:
            return set()
        try:
            coll = warrant_underlying_map_collection()
        except RuntimeError:
            return set()
        docs = coll.find({"_id": {"$in": list(identifiers)}})
        missing: set[str] = set()
        async for d in docs:
            if d.get("underlying_symbol") and not d.get("underlying_name"):
                key = d.get("_id")
                if key:
                    missing.add(key)
        return missing

    async def _map_from_last_approved_warrant_selection(self, run: dict) -> dict[str, str]:
        """Return {warrant_isin → underlying_symbol} from the last approved execution."""
        qs_id = run.get("quant_system_id")
        if not qs_id:
            return {}
        last_run = await executions_collection().find_one(
            {
                "quant_system_id": qs_id,
                "execution_id": {"$ne": run["execution_id"]},
                "stages.warrant_selection.status": "approved",
            },
            sort=[("created_at", -1)],
        )
        if not last_run:
            return {}
        ws_data = last_run.get("stages", {}).get("warrant_selection", {}).get("result")
        if not ws_data:
            return {}
        ws = WarrantSelectionResult.model_validate(ws_data)
        return {w.warrant_isin: w.underlying.symbol for w in ws.selected}

    @staticmethod
    def _held_warrant_identifiers(holdings: list[Position]) -> set[str]:
        identifiers: set[str] = set()
        for pos in holdings:
            if pos.ticker.isin:
                identifiers.add(pos.ticker.isin)
            if pos.ticker.symbol:
                identifiers.add(pos.ticker.symbol)
        return identifiers

    @staticmethod
    def _resolve_underlying_symbol_for_position(pos: Position, mapping: dict[str, str]) -> str | None:
        return (
            (mapping.get(pos.ticker.isin) if pos.ticker.isin else None)
            or (mapping.get(pos.ticker.symbol) if pos.ticker.symbol else None)
        )

    async def _resolve_underlying_names_from_universe(
        self,
        holdings: list[Position],
        warrant_underlying_map: dict[str, str],
        universe_names_by_isin: dict[str, str],
    ) -> dict[str, str]:
        """Resolve held underlying names via warrant /instruments -> underlying ISIN -> universe name."""
        if not holdings or not universe_names_by_isin:
            return {}

        result: dict[str, str] = {}
        async with FinHubTool() as finhub:
            for pos in holdings:
                underlying_symbol = self._resolve_underlying_symbol_for_position(pos, warrant_underlying_map)
                if not underlying_symbol or underlying_symbol in result:
                    continue

                inst: dict | None = None
                for identifier in (pos.ticker.isin, pos.ticker.symbol):
                    if not identifier:
                        continue
                    try:
                        inst = await finhub.get_instrument(identifier)
                    except Exception:
                        inst = None
                    if inst:
                        break

                if not inst:
                    continue

                details = inst.get("details") or {}
                underlying_link = details.get("underlying_link") or ""
                match = re.search(r"([A-Z]{2}[A-Z0-9]{10})$", underlying_link)
                if not match:
                    continue

                underlying_isin = match.group(1)
                universe_name = universe_names_by_isin.get(underlying_isin)
                if universe_name:
                    result[underlying_symbol] = universe_name

        return result

    async def _map_from_cache(self, identifiers: set[str]) -> dict[str, str]:
        if not identifiers:
            return {}
        coll = warrant_underlying_map_collection()
        docs = coll.find({"_id": {"$in": list(identifiers)}})
        return {
            d["_id"]: d["underlying_symbol"]
            async for d in docs
            if d.get("underlying_symbol")
        }

    async def _underlying_names_from_cache(self, identifiers: set[str]) -> dict[str, str]:
        if not identifiers:
            return {}
        try:
            coll = warrant_underlying_map_collection()
        except RuntimeError:
            return {}
        docs = coll.find({"_id": {"$in": list(identifiers)}})
        names: dict[str, str] = {}
        async for d in docs:
            symbol = d.get("underlying_symbol")
            name = d.get("underlying_name")
            if symbol and name:
                names[symbol] = name
        return names

    async def _persist_warrant_underlying_mapping(
        self,
        warrant_isin: str | None,
        warrant_wkn: str | None,
        underlying_symbol: str,
        underlying_isin: str | None,
        underlying_name: str | None,
        source: str,
        resolved_from: str | None,
    ) -> None:
        coll = warrant_underlying_map_collection()
        now = datetime.now(timezone.utc)
        payload = {
            "warrant_isin": warrant_isin,
            "warrant_wkn": warrant_wkn,
            "underlying_symbol": underlying_symbol,
            "underlying_isin": underlying_isin,
            "underlying_name": underlying_name,
            "source": source,
            "resolved_from": resolved_from,
            "checked_at": now,
        }
        keys = [k for k in (warrant_isin, warrant_wkn) if k]
        for key in keys:
            await coll.update_one({"_id": key}, {"$set": payload}, upsert=True)

    async def _resolve_warrant_underlying_via_instruments(
        self,
        finhub: FinHubTool,
        pos: Position,
    ) -> dict[str, str] | None:
        """Resolve a held warrant via FinHub /instruments by ISIN or WKN.

        Returns mapping payload on success, else None.
        """
        identifiers = [x for x in (pos.ticker.isin, pos.ticker.symbol) if x]
        if not identifiers:
            return None

        for identifier in identifiers:
            try:
                inst = await finhub.get_instrument(identifier)
            except Exception:
                continue
            if not inst:
                continue

            details = inst.get("details") or {}
            underlying_link = details.get("underlying_link") or ""
            match = re.search(r"([A-Z]{2}[A-Z0-9]{10})$", underlying_link)
            underlying_isin = match.group(1) if match else None
            if not underlying_isin:
                continue

            try:
                underlying_inst = await finhub.get_instrument(underlying_isin)
            except Exception:
                continue
            if not underlying_inst:
                continue

            gids = underlying_inst.get("global_identifiers") or {}
            underlying_symbol = gids.get("symbol_yfinance") or gids.get("symbol_comdirect")
            if not underlying_symbol:
                continue

            underlying_name = (
                details.get("underlying_name")
                or underlying_inst.get("name")
                or (underlying_inst.get("global_identifiers") or {}).get("name_openfigi")
            )

            warrant_gids = inst.get("global_identifiers") or {}
            return {
                "warrant_isin": inst.get("isin") or warrant_gids.get("isin") or pos.ticker.isin or "",
                "warrant_wkn": inst.get("wkn") or warrant_gids.get("wkn") or pos.ticker.symbol or "",
                "underlying_symbol": underlying_symbol,
                "underlying_isin": underlying_isin,
                "underlying_name": underlying_name,
                "resolved_from": "isin" if identifier == pos.ticker.isin else "wkn",
            }
        return None

    async def _fetch_held_since(self, run: dict) -> dict[str, date]:
        """Return {warrant_wkn → most recent BUY booking_date} for the linked virtual depot."""
        qs_id = run.get("quant_system_id")
        if not qs_id:
            return {}
        qs = await quant_systems_collection().find_one({"quant_system_id": qs_id})
        if not qs or qs.get("depot_type") != "virtual" or not qs.get("depot_id"):
            return {}
        depot_id = qs["depot_id"]
        transactions = await virtual_depot_transactions_collection().find(
            {"depot_id": depot_id, "transaction_type": "BUY"},
            sort=[("booking_date", -1)],
        ).to_list()
        held_since: dict[str, date] = {}
        for txn in transactions:
            wkn = txn.get("wkn")
            if wkn and wkn not in held_since:
                bd = txn.get("booking_date")
                held_since[wkn] = bd.date() if hasattr(bd, "date") else bd
        return held_since

    async def _run_portfolio(self, run: dict) -> PortfolioProposal:
        warrant_result = WarrantSelectionResult.model_validate(
            run["stages"]["warrant_selection"]["result"]
        )
        # Build SelectionResult for PortfolioAgent — each position is the warrant instrument
        warrant_tickers = [
            Ticker(symbol=w.warrant_wkn or w.warrant_isin, isin=w.warrant_isin, name=w.underlying.name)
            for w in warrant_result.selected
        ]
        scores = {(w.warrant_wkn or w.warrant_isin): w.score for w in warrant_result.selected}
        selection = SelectionResult(selected=warrant_tickers, scores=scores, rationale={})

        current_holdings = await self._fetch_holdings(run)
        # Warrant ISINs that monitoring decided to keep — excluded from close_positions
        kept_warrant_isins: set[str] = set()
        monitoring_data = run.get("stages", {}).get("monitoring", {}).get("result")
        if monitoring_data:
            monitoring = MonitoringResult.model_validate(monitoring_data)
            kept_warrant_isins = {p.warrant_isin for p in monitoring.positions_to_keep if p.warrant_isin}
            kept_warrant_isins.update(p.warrant_isin for p in monitoring.positions_to_roll if p.warrant_isin)
        return await PortfolioConstructionAgent(
            capital_eur=run.get("capital_eur", settings.portfolio.capital_eur),
            current_holdings=current_holdings,
            sizing_method=settings.portfolio.sizing_method,
            max_position_weight=settings.portfolio.max_position_weight,
            kept_warrant_isins=kept_warrant_isins,
        ).run(selection)

    async def _fetch_holdings(self, run: dict) -> list[Position]:
        """Return current holdings from the depot linked to the QuantSystem."""
        qs_id = run.get("quant_system_id")
        if not qs_id:
            return []
        qs = await quant_systems_collection().find_one({"quant_system_id": qs_id})
        if not qs or not qs.get("depot_id"):
            return []

        depot_id = qs["depot_id"]
        depot_type = qs.get("depot_type", "virtual")

        if depot_type == "real":
            snapshot = await finance_db()["depot_snapshots"].find_one(
                {"depot_id": depot_id}, sort=[("recorded_at", -1)]
            )
        else:
            snapshot = await virtual_depot_snapshots_collection().find_one(
                {"depot_id": depot_id}, sort=[("recorded_at", -1)]
            )

        if not snapshot:
            return []

        holdings: list[Position] = []
        for pos in snapshot.get("positions", []):
            isin = pos.get("isin")
            wkn = pos.get("wkn") or isin or ""
            qty_raw = pos.get("quantity", {})
            qty = qty_raw.get("value") if isinstance(qty_raw, dict) else qty_raw
            try:
                quantity = Decimal(str(qty)) if qty else Decimal("0")
            except Exception:
                quantity = Decimal("0")
            if quantity <= 0:
                continue
            holdings.append(Position(
                ticker=Ticker(symbol=wkn, isin=isin, name=pos.get("instrument_name")),
                quantity=quantity,
                avg_cost=Decimal("0"),
            ))
        return holdings

    async def _run_risk(self, run: dict) -> RiskAssessment:
        proposal = PortfolioProposal.model_validate(run["stages"]["portfolio"]["result"])
        return await RiskAgent(
            max_position_weight=settings.risk.max_position_weight,
            max_positions=settings.risk.max_positions,
        ).run(proposal)

    async def _run_execution(self, run: dict) -> ExecutionPlan:
        assessment = RiskAssessment.model_validate(run["stages"]["risk"]["result"])
        return await TradeExecutionAgent(
            dry_run=settings.execution.dry_run,
            min_trade_eur=settings.execution.min_trade_eur,
            order_type=settings.execution.order_type,
        ).run(assessment)


_pipeline = Pipeline()


def get_pipeline() -> Pipeline:
    return _pipeline
