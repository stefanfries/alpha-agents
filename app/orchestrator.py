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

_LEGACY_POSITION_FIELDS = frozenset({"purchase_price", "buy_price_at_entry"})


class Pipeline:
    @staticmethod
    def _assert_canonical_position_schema(position: dict[str, Any]) -> None:
        legacy_present = sorted(_LEGACY_POSITION_FIELDS.intersection(position.keys()))
        if legacy_present:
            raise RuntimeError(
                "Legacy position fields detected in snapshot: "
                f"{', '.join(legacy_present)}. Expected canonical fields "
                "average_purchase_price and purchase_price_at_entry."
            )

    @staticmethod
    def _decimal_from_amount_field(container: dict[str, Any], field_name: str) -> Decimal:
        amount_obj = container.get(field_name)
        raw = amount_obj.get("value") if isinstance(amount_obj, dict) else amount_obj
        if raw in (None, ""):
            return Decimal("0")
        try:
            return Decimal(str(raw))
        except Exception:
            return Decimal("0")

    @staticmethod
    def _parse_snapshot_held_since(raw: Any) -> date | None:
        if raw in (None, ""):
            return None
        if isinstance(raw, datetime):
            return raw.date()
        if isinstance(raw, date):
            return raw
        if isinstance(raw, str):
            try:
                return date.fromisoformat(raw)
            except ValueError:
                return None
        return None

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

            async def on_universe_progress(done: int, total: int) -> None:
                await update_stage_progress(execution_id, "universe", {
                    "step": "instruments",
                    "done": done,
                    "total": total,
                    "message": "Resolving instrument mappings…",
                })

            result = await UniverseAgent(
                finhub=finhub,
                wikipedia=wikipedia,
                on_progress=on_universe_progress,
            ).run(
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
        result = await SecuritySelectionAgent(
            settings=screening_cfg,
        ).run(research_with_bars)
        return result

    async def _run_warrant_selection(self, run: dict) -> WarrantSelectionResult:
        execution_id = run["execution_id"]
        monitoring_data = run["stages"]["monitoring"]["result"]
        monitoring = MonitoringResult.model_validate(monitoring_data)
        candidates = monitoring.entry_candidates

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
                min_score=ws_cfg.min_score,
                atm_band_fallback=ws_cfg.atm_band_fallback,
                isin_overrides=overrides,
                on_progress=on_progress,
            ).run(SelectionResult(selected=candidates, scores={t.symbol: 1.0 for t in candidates}, rationale={}))

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
        held_since_map = await self._fetch_held_since(run)
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
            policy_results=screening.policy_results,
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
        """Resolve {warrant_isin -> underlying_symbol} live via FinHub /instruments."""
        result: dict[str, str] = {}

        if holdings:
            async with FinHubTool() as finhub:
                for pos in holdings:
                    resolved = await self._resolve_warrant_underlying_via_instruments(finhub, pos)
                    if not resolved:
                        continue
                    underlying_symbol = resolved["underlying_symbol"]
                    warrant_isin = resolved.get("warrant_isin")
                    if warrant_isin:
                        result[warrant_isin] = underlying_symbol
                    await self._persist_warrant_underlying_mapping(
                        warrant_isin=warrant_isin,
                        underlying_symbol=underlying_symbol,
                        underlying_isin=resolved.get("underlying_isin"),
                        underlying_name=resolved.get("underlying_name"),
                        source="finhub_instruments",
                        resolved_from="isin",
                    )

        return result

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
                underlying_symbol = warrant_underlying_map.get(pos.ticker.isin) if pos.ticker.isin else None
                if not underlying_symbol or underlying_symbol in result:
                    continue

                inst: dict | None = None
                try:
                    if pos.ticker.isin:
                        inst = await finhub.get_instrument(pos.ticker.isin)
                except Exception:
                    inst = None

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

    async def _persist_warrant_underlying_mapping(
        self,
        warrant_isin: str | None,
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
            "underlying_symbol": underlying_symbol,
            "underlying_isin": underlying_isin,
            "underlying_name": underlying_name,
            "source": source,
            "resolved_from": resolved_from,
            "checked_at": now,
        }
        if warrant_isin:
            await coll.update_one({"_id": warrant_isin}, {"$set": payload}, upsert=True)

    async def _resolve_warrant_underlying_via_instruments(
        self,
        finhub: FinHubTool,
        pos: Position,
    ) -> dict[str, str] | None:
        """Resolve a held warrant via FinHub /instruments by ISIN.

        Returns mapping payload on success, else None.
        """
        if not pos.ticker.isin:
            return None

        try:
            inst = await finhub.get_instrument(pos.ticker.isin)
        except Exception:
            return None
        if not inst:
            return None

        details = inst.get("details") or {}
        underlying_link = details.get("underlying_link") or ""
        match = re.search(r"([A-Z]{2}[A-Z0-9]{10})$", underlying_link)
        underlying_isin = match.group(1) if match else None
        if not underlying_isin:
            return None

        try:
            underlying_inst = await finhub.get_instrument(underlying_isin)
        except Exception:
            return None
        if not underlying_inst:
            return None

        gids = underlying_inst.get("global_identifiers") or {}
        underlying_symbol = gids.get("symbol_yfinance")
        if not underlying_symbol:
            return None

        underlying_name = (
            details.get("underlying_name")
            or underlying_inst.get("name")
            or (underlying_inst.get("global_identifiers") or {}).get("name_openfigi")
        )

        warrant_gids = inst.get("global_identifiers") or {}
        return {
            "warrant_isin": inst.get("isin") or warrant_gids.get("isin") or pos.ticker.isin or "",
            "underlying_symbol": underlying_symbol,
            "underlying_isin": underlying_isin,
            "underlying_name": underlying_name,
            "resolved_from": "isin",
        }

    async def _fetch_held_since(self, run: dict) -> dict[str, date]:
        """Return {warrant_wkn -> held_since_date} using latest snapshot per depot.

        For virtual depots only, missing held_since_date values fall back to BUY
        transactions when available.
        """
        qs_id = run.get("quant_system_id")
        if not qs_id:
            return {}
        qs = await quant_systems_collection().find_one({"quant_system_id": qs_id})
        if not qs or not qs.get("depot_id"):
            return {}
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

        held_since: dict[str, date] = {}
        snapshot_wkns: set[str] = set()

        if snapshot:
            for pos in snapshot.get("positions", []):
                if not isinstance(pos, dict):
                    continue
                self._assert_canonical_position_schema(pos)
                wkn = pos.get("wkn")
                if not wkn:
                    continue
                snapshot_wkns.add(wkn)
                held = self._parse_snapshot_held_since(pos.get("held_since_date"))
                if held and wkn not in held_since:
                    held_since[wkn] = held

        if depot_type == "virtual":
            transactions = await virtual_depot_transactions_collection().find(
                {"depot_id": depot_id, "transaction_type": "BUY"},
                sort=[("booking_date", -1)],
            ).to_list()
            for txn in transactions:
                wkn = txn.get("wkn")
                if not wkn or wkn in held_since:
                    continue
                if snapshot_wkns and wkn not in snapshot_wkns:
                    continue
                bd = txn.get("booking_date")
                if isinstance(bd, datetime):
                    held_since[wkn] = bd.date()
                elif isinstance(bd, date):
                    held_since[wkn] = bd

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
            if not isinstance(pos, dict):
                continue
            self._assert_canonical_position_schema(pos)
            isin = pos.get("isin")
            wkn = pos.get("wkn") or isin or ""
            quantity = self._decimal_from_amount_field(pos, "quantity")
            if quantity <= 0:
                continue
            avg_cost = self._decimal_from_amount_field(pos, "average_purchase_price")
            holdings.append(Position(
                ticker=Ticker(symbol=wkn, isin=isin, name=pos.get("instrument_name")),
                quantity=quantity,
                avg_cost=avg_cost,
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
