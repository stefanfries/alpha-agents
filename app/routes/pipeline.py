import asyncio
import uuid
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.db import runs_collection
from app.orchestrator import get_pipeline

router = APIRouter(prefix="/runs")
templates = Jinja2Templates(directory="app/templates")

STAGES = ["universe", "research", "screening", "warrant_selection", "portfolio", "risk", "execution"]

STAGE_LABELS: dict[str, str] = {
    "universe": "Universe",
    "research": "Research",
    "screening": "Screening",
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


def _stage_ctx(run: dict, current_stage: str) -> dict:
    s_data = run.get("stages", {}).get(current_stage, {})
    return {
        "run": run,
        "run_id": run["run_id"],
        "current_stage": current_stage,
        "stages": STAGES,
        "stage_labels": STAGE_LABELS,
        "stage_status": s_data.get("status", "pending"),
        "stage_result": s_data.get("result"),
        "stage_error": s_data.get("error"),
    }


# ---------------------------------------------------------------------------
# Run list
# ---------------------------------------------------------------------------

@router.get("", response_class=HTMLResponse)
async def list_runs(request: Request) -> HTMLResponse:
    runs = await runs_collection().find({}, _NO_ID).sort("created_at", -1).to_list()
    return templates.TemplateResponse(request, "runs/list.html", {"runs": runs})


# ---------------------------------------------------------------------------
# New run — triggers universe immediately
# ---------------------------------------------------------------------------

@router.post("", response_class=RedirectResponse)
async def create_run(
    indices: Annotated[list[str], Form()],
    capital_eur: Annotated[float, Form()],
    mitl_mode: Annotated[bool, Form()] = True,
) -> RedirectResponse:
    run_id = uuid.uuid4().hex[:6]
    run_doc = {
        "run_id": run_id,
        "created_at": datetime.now(timezone.utc),
        "indices": indices,
        "capital_eur": capital_eur,
        "mitl_mode": mitl_mode,
        "config_overrides": {},
        "current_stage": STAGES[0],
        "status": "running",
        "stages": {s: {"status": "pending"} for s in STAGES},
    }
    run_doc["stages"][STAGES[0]]["status"] = "running"
    await runs_collection().insert_one(run_doc)
    _fire(get_pipeline().run_stage(run_id, STAGES[0]))
    return RedirectResponse(url=f"/runs/{run_id}", status_code=303)


# ---------------------------------------------------------------------------
# Run detail — redirect to current stage
# ---------------------------------------------------------------------------

@router.get("/{run_id}", response_class=RedirectResponse)
async def run_detail(run_id: str) -> RedirectResponse:
    run = await runs_collection().find_one({"run_id": run_id}, _NO_ID)
    stage = run["current_stage"] if run else STAGES[0]
    return RedirectResponse(url=f"/runs/{run_id}/stages/{stage}")


# ---------------------------------------------------------------------------
# Stage review pages
# ---------------------------------------------------------------------------

@router.get("/{run_id}/stages/{stage}", response_class=HTMLResponse)
async def stage_review(request: Request, run_id: str, stage: str) -> HTMLResponse:
    run = await runs_collection().find_one({"run_id": run_id}, _NO_ID)
    if run is None:
        run = {"run_id": run_id, "current_stage": stage, "stages": {}, "indices": []}
    ctx = _stage_ctx(run, stage)
    return templates.TemplateResponse(request, f"stages/{stage}.html", ctx)


# ---------------------------------------------------------------------------
# Approve — triggers the next stage
# ---------------------------------------------------------------------------

@router.post("/{run_id}/stages/{stage}/approve", response_class=RedirectResponse)
async def approve_stage(
    run_id: str,
    stage: str,
    kept: Annotated[list[str] | None, Form()] = None,
) -> RedirectResponse:
    idx = STAGES.index(stage)
    if idx + 1 < len(STAGES):
        next_stage = STAGES[idx + 1]
        await runs_collection().update_one(
            {"run_id": run_id},
            {"$set": {
                f"stages.{stage}.status": "approved",
                "current_stage": next_stage,
                f"stages.{next_stage}.status": "running",
                "status": "running",
            }},
        )
        _fire(get_pipeline().run_stage(run_id, next_stage))
        return RedirectResponse(url=f"/runs/{run_id}/stages/{next_stage}", status_code=303)

    await runs_collection().update_one(
        {"run_id": run_id},
        {"$set": {f"stages.{stage}.status": "approved", "status": "complete"}},
    )
    return RedirectResponse(url=f"/runs/{run_id}/stages/{stage}", status_code=303)


# ---------------------------------------------------------------------------
# Restart — re-runs from the chosen stage
# ---------------------------------------------------------------------------

@router.post("/{run_id}/stages/{stage}/restart", response_class=RedirectResponse)
async def restart_stage(
    run_id: str,
    stage: str,
    from_stage: Annotated[str, Form()],
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
    await runs_collection().update_one({"run_id": run_id}, {"$set": updates})
    _fire(get_pipeline().run_stage(run_id, from_stage))
    return RedirectResponse(url=f"/runs/{run_id}/stages/{from_stage}", status_code=303)


# ---------------------------------------------------------------------------
# Chart fragments (stubs)
# ---------------------------------------------------------------------------

@router.get("/{run_id}/charts/screening/{ticker}", response_class=HTMLResponse)
async def chart_screening(run_id: str, ticker: str) -> HTMLResponse:
    return HTMLResponse(f"<p class='text-muted'>Chart for {ticker} — not yet implemented</p>")


@router.get("/{run_id}/charts/warrant_selection/{isin}", response_class=HTMLResponse)
async def chart_warrant(run_id: str, isin: str) -> HTMLResponse:
    return HTMLResponse(f"<p class='text-muted'>Warrant scoring chart for {isin} — not yet implemented</p>")


@router.get("/{run_id}/charts/portfolio", response_class=HTMLResponse)
async def chart_portfolio(run_id: str) -> HTMLResponse:
    return HTMLResponse("<p class='text-muted'>Portfolio weight chart — not yet implemented</p>")


@router.get("/{run_id}/charts/risk", response_class=HTMLResponse)
async def chart_risk(run_id: str) -> HTMLResponse:
    return HTMLResponse("<p class='text-muted'>Risk weight chart — not yet implemented</p>")
