
import pytest



@pytest.mark.asyncio
async def test_restart_stage_persists_screening_policy_form_values(monkeypatch):
    from app.routes import pipeline as pipeline_module

    class FakeCollection:
        def __init__(self) -> None:
            self.calls: list[tuple[dict, dict]] = []

        async def update_one(self, selector: dict, update: dict) -> None:
            self.calls.append((selector, update))

    class FakePipeline:
        async def run_stage(self, execution_id: str, from_stage: str) -> None:
            return None

    fake_collection = FakeCollection()

    monkeypatch.setattr(pipeline_module, "executions_collection", lambda: fake_collection)
    monkeypatch.setattr(pipeline_module, "get_pipeline", lambda: FakePipeline())
    monkeypatch.setattr(pipeline_module, "_fire", lambda coro: coro.close())

    response = await pipeline_module.restart_stage(
        qs_id="qs1",
        execution_id="exec1",
        stage="screening",
        from_stage="screening",
        policies_submitted="1",
        ranking_metric="tq20",
        top_n="7",
        market_gate_supertrend="on",
        market_gate_red_regime="on",
        policy_supertrend="on",
        policy_ema20_rising="on",
        policy_adx_above=None,
        policy_adx_rising="on",
        policy_price_above_ema50="on",
        policy_tq60_above="on",
        policy_tq20_above=None,
        policy_tq60_min="0.07",
        policy_tq20_min="0.02",
        policy_tsi_above="on",
        policy_tsi_rising="on",
        policy_tsi_new_min="30",
        new_min_true="99",
        policy_supertrend_break="on",
        policy_ema20_falling_break=None,
        policy_adx_below_break="on",
        policy_adx_falling_break=None,
        policy_price_below_ema50_break=None,
        policy_tsi_falling_break="on",
        policy_tsi_below_break=None,
        policy_tsi_break_max="15",
        break_min_true="0",
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/quant-systems/qs1/executions/exec1/stages/screening"

    assert len(fake_collection.calls) == 1
    selector, update = fake_collection.calls[0]
    assert selector == {"execution_id": "exec1"}

    screening_cfg = update["$set"]["config_overrides.screening"]
    assert screening_cfg == {
        "top_n": 7,
        "ranking_metric": "tq20",
        "market_gate_supertrend": True,
        "market_gate_red_regime": True,
        "policy_supertrend": True,
        "policy_ema20_rising": True,
        "policy_adx_above": False,
        "policy_adx_rising": True,
        "policy_price_above_ema50": True,
        "policy_tq60_above": True,
        "policy_tq20_above": False,
        "policy_tq60_min": 0.07,
        "policy_tq20_min": 0.02,
        "policy_tsi_above": True,
        "policy_tsi_rising": True,
        "policy_tsi_new_min": 30.0,
        "new_min_true": 7,
        "policy_supertrend_break": True,
        "policy_ema20_falling_break": False,
        "policy_adx_below_break": True,
        "policy_adx_falling_break": False,
        "policy_price_below_ema50_break": False,
        "policy_tsi_falling_break": True,
        "policy_tsi_below_break": False,
        "policy_tsi_break_max": 15.0,
        "break_min_true": 1,
    }


@pytest.mark.asyncio
async def test_restart_stage_clamps_tq_thresholds_and_handles_invalid(monkeypatch):
    from app.routes import pipeline as pipeline_module

    class FakeCollection:
        def __init__(self) -> None:
            self.calls: list[tuple[dict, dict]] = []

        async def update_one(self, selector: dict, update: dict) -> None:
            self.calls.append((selector, update))

    class FakePipeline:
        async def run_stage(self, execution_id: str, from_stage: str) -> None:
            return None

    fake_collection = FakeCollection()

    monkeypatch.setattr(pipeline_module, "executions_collection", lambda: fake_collection)
    monkeypatch.setattr(pipeline_module, "get_pipeline", lambda: FakePipeline())
    monkeypatch.setattr(pipeline_module, "_fire", lambda coro: coro.close())

    await pipeline_module.restart_stage(
        qs_id="qs1",
        execution_id="exec2",
        stage="screening",
        from_stage="screening",
        policies_submitted="1",
        ranking_metric="unsupported",
        top_n="invalid",
        policy_supertrend="on",
        policy_ema20_rising="on",
        policy_adx_above="on",
        policy_adx_rising="on",
        policy_price_above_ema50="on",
        policy_tq60_above="on",
        policy_tq20_above="on",
        policy_tq60_min="9.9",
        policy_tq20_min="invalid",
        policy_tsi_above="on",
        policy_tsi_new_min="invalid",
        new_min_true="2",
        policy_supertrend_break="on",
        policy_ema20_falling_break="on",
        policy_adx_below_break="on",
        policy_adx_falling_break="on",
        policy_price_below_ema50_break="on",
        policy_tsi_below_break="on",
        policy_tsi_break_max="invalid",
        break_min_true="2",
    )

    _, update = fake_collection.calls[0]
    screening_cfg = update["$set"]["config_overrides.screening"]

    assert screening_cfg["top_n"] == 20
    assert screening_cfg["ranking_metric"] == "tsi"
    assert screening_cfg["policy_tq60_min"] == 1.0
    assert screening_cfg["policy_tq20_min"] == 0.0


@pytest.mark.asyncio
async def test_screening_stage_context_includes_config_defaults_and_run_overrides(monkeypatch):
    from starlette.requests import Request

    from app.routes import pipeline as pipeline_module

    execution = {
        "execution_id": "exec1",
        "quant_system_id": "qs1",
        "stages": {"screening": {"status": "awaiting_review"}},
        "config_overrides": {"screening": {"top_n": 5}},
    }

    class FakeCollection:
        async def find_one(self, selector: dict, projection: dict) -> dict:
            assert selector == {"execution_id": "exec1"}
            return execution

    class FakeTemplates:
        @staticmethod
        def TemplateResponse(request: Request, template_name: str, context: dict) -> dict:
            assert template_name == "stages/screening.html"
            return context

    monkeypatch.setattr(pipeline_module, "executions_collection", lambda: FakeCollection())
    monkeypatch.setattr(pipeline_module, "templates", FakeTemplates())
    request = Request({
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [],
        "query_string": b"",
        "scheme": "http",
        "server": ("test", 80),
        "client": ("test", 80),
    })

    context = await pipeline_module.stage_review(request, "qs1", "exec1", "screening")

    assert context["screening_cfg"]["top_n"] == 5
    assert context["screening_cfg"]["ranking_metric"] == "tsi"
    assert context["screening_cfg"]["min_market_cap_eur"] == 500_000_000


@pytest.mark.asyncio
@pytest.mark.parametrize("position_group", ["positions_to_sell", "positions_to_keep", "positions_to_roll"])
@pytest.mark.parametrize("has_break_event", [True, False])
async def test_monitoring_refreshes_saved_degraded_status_from_screening(
    monkeypatch, position_group, has_break_event,
):
    from copy import deepcopy

    from starlette.requests import Request

    from app.routes import pipeline as pipeline_module

    screening = {
        "selected": [], "scores": {}, "rationale": {},
        "trend_signals": {"A": "BREAK"},
        "last_break_age_bars": {"A": 2},
        "policy_results": {"A": {
            "tsi_falling": True, "adx_below": True, "ema20_falling": False,
        }},
    }
    if has_break_event:
        screening["break_policy_results"] = {"A": {
            "tsi_falling": True, "adx_below": True, "ema20_falling": True,
        }}
    saved_result = {
        position_group: [{
            "underlying_symbol": "A", "trend_status": "trend degraded",
            "trend_status_detail": "trend degraded: BREAK reasons unavailable",
            "decision_reason": "trend break",
        }],
    }
    execution = {
        "execution_id": "exec1", "quant_system_id": "qs1",
        "stages": {
            "screening": {"result": screening},
            "monitoring": {"status": "awaiting_review", "result": saved_result},
        },
        "config_overrides": {"screening": {
            "policy_tsi_falling_break": True,
            "policy_adx_below_break": False,
            "policy_ema20_falling_break": True,
        }},
    }
    original = deepcopy(execution)

    class FakeCollection:
        async def find_one(self, selector, projection):
            return execution

    class FakeTemplates:
        @staticmethod
        def TemplateResponse(request, template_name, context):
            assert template_name == "stages/monitoring.html"
            return context

    monkeypatch.setattr(pipeline_module, "executions_collection", lambda: FakeCollection())
    monkeypatch.setattr(pipeline_module, "templates", FakeTemplates())
    request = Request({"type": "http", "method": "GET", "path": "/", "headers": []})

    context = await pipeline_module.stage_review(request, "qs1", "exec1", "monitoring")

    position = context["stage_result"][position_group][0]
    if has_break_event:
        assert position["trend_status"] == "trend degraded: EMA20 falling (+1)"
        assert position["trend_status_detail"] == "trend degraded:\n- EMA20 falling\n- TSI falling"
    else:
        assert position["trend_status"] == "trend degraded: TSI falling"
        assert position["trend_status_detail"] == (
            "trend degraded:\n- TSI falling"
            "\nLatest screening conditions; BREAK-event details were not saved."
        )
    assert position["decision_reason"] == "trend break"
    assert execution == original


@pytest.mark.asyncio
async def test_restart_stage_persists_warrant_maturity_range(monkeypatch):
    from app.routes import pipeline as pipeline_module

    class FakeCollection:
        def __init__(self) -> None:
            self.calls: list[tuple[dict, dict]] = []

        async def update_one(self, selector: dict, update: dict) -> None:
            self.calls.append((selector, update))

    class FakePipeline:
        async def run_stage(self, execution_id: str, from_stage: str) -> None:
            return None

    fake_collection = FakeCollection()

    monkeypatch.setattr(pipeline_module, "executions_collection", lambda: fake_collection)
    monkeypatch.setattr(pipeline_module, "get_pipeline", lambda: FakePipeline())
    monkeypatch.setattr(pipeline_module, "_fire", lambda coro: coro.close())

    await pipeline_module.restart_stage(
        qs_id="qs1",
        execution_id="exec3",
        stage="warrant_selection",
        from_stage="warrant_selection",
        maturity_range_submitted="1",
        ws_min_months="9",
        ws_max_months="15",
        ws_strike_min_factor="0.95",
        ws_strike_max_factor="1.00",
        ws_min_score="0.62",
        ws_spread_max_pct="2.25",
    )

    _, update = fake_collection.calls[0]
    ws_cfg = update["$set"]["config_overrides.warrant_selection"]

    assert ws_cfg == {
        "min_days_to_expiry": 270,
        "max_days_to_expiry": 450,
        "strike_min_factor": 0.95,
        "strike_max_factor": 1.0,
        "min_score": 0.62,
        "spread_max_pct": 2.25,
    }


@pytest.mark.asyncio
async def test_restart_stage_persists_risk_limits(monkeypatch):
    from app.routes import pipeline as pipeline_module

    class FakeCollection:
        def __init__(self) -> None:
            self.calls: list[tuple[dict, dict]] = []

        async def update_one(self, selector: dict, update: dict) -> None:
            self.calls.append((selector, update))

    class FakePipeline:
        async def run_stage(self, execution_id: str, from_stage: str) -> None:
            return None

    fake_collection = FakeCollection()
    monkeypatch.setattr(pipeline_module, "executions_collection", lambda: fake_collection)
    monkeypatch.setattr(pipeline_module, "get_pipeline", lambda: FakePipeline())
    monkeypatch.setattr(pipeline_module, "_fire", lambda coro: coro.close())

    await pipeline_module.restart_stage(
        qs_id="qs1",
        execution_id="exec4",
        stage="risk",
        from_stage="risk",
        risk_limits_submitted="1",
        risk_max_position_multiple="2.5",
        risk_max_sector_pct="30",
    )

    _, update = fake_collection.calls[0]
    assert update["$set"]["config_overrides.risk"] == {
        "max_position_multiple": 2.5,
        "max_sector_weight": 0.3,
    }


@pytest.mark.asyncio
async def test_restart_clears_progress_for_rerun_stages(monkeypatch):
    import app.routes.pipeline as pipeline_module

    class FakeExecutionsCollection:
        update = None

        async def update_one(self, _query, update):
            self.update = update

    class FakePipeline:
        async def run_stage(self, _execution_id, _stage):
            return None

    collection = FakeExecutionsCollection()
    scheduled = []
    monkeypatch.setattr(pipeline_module, "executions_collection", lambda: collection)
    monkeypatch.setattr(pipeline_module, "get_pipeline", FakePipeline)
    monkeypatch.setattr(pipeline_module, "_fire", scheduled.append)

    await pipeline_module.restart_stage("qs1", "exec1", "warrant_selection", "warrant_selection")
    await scheduled[0]

    updates = collection.update["$set"]
    assert updates["stages.warrant_selection.progress"] is None
    assert updates["stages.portfolio.progress"] is None
