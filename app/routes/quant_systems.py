import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.db import (
    finance_db,
    quant_systems_collection,
    virtual_depots_collection,
)

router = APIRouter(prefix="/quant-systems")
templates = Jinja2Templates(directory="app/templates")

_NO_ID = {"_id": 0}

INDICES = ["DAX", "MDAX", "SDAX", "TecDAX", "EuroStoxx50", "NASDAQ100", "SP500", "FTSE100"]


async def _real_depots() -> list[dict]:
    """Return distinct real depot ids from the latest finance.depot_snapshots."""
    try:
        db = finance_db()
        # Distinct depot_ids from depot_snapshots; for each get the latest doc's name
        depot_ids: list[str] = await db["depot_snapshots"].distinct("depot_id")
        result = []
        for depot_id in depot_ids:
            doc = await db["depot_snapshots"].find_one(
                {"depot_id": depot_id},
                {"depot_id": 1, "account_name": 1, "display_name": 1, "_id": 0},
                sort=[("recorded_at", -1)],
            )
            if doc:
                result.append(doc)
        return result
    except Exception:
        return []


async def _resolve_depot_type(depot_id: str, depot_type: str) -> str:
    """Resolve depot_type defensively when the form did not set it."""
    normalized = (depot_type or "").strip().lower()
    if normalized in {"real", "virtual"}:
        return normalized
    vd = await virtual_depots_collection().find_one({"depot_id": depot_id}, {"_id": 1})
    return "virtual" if vd else "real"


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------

@router.get("", response_class=HTMLResponse)
async def list_quant_systems(request: Request) -> HTMLResponse:
    qs_list = await quant_systems_collection().find({}, _NO_ID).sort("created_at", -1).to_list()
    return templates.TemplateResponse(request, "quant_systems/list.html", {"qs_list": qs_list})


# ---------------------------------------------------------------------------
# New — GET renders wizard, POST saves
# ---------------------------------------------------------------------------

@router.get("/new", response_class=HTMLResponse)
async def new_quant_system(request: Request) -> HTMLResponse:
    real_depots = await _real_depots()
    virtual_depots = await virtual_depots_collection().find({}, _NO_ID).sort("name", 1).to_list()
    return templates.TemplateResponse(request, "quant_systems/new.html", {
        "real_depots": real_depots,
        "virtual_depots": virtual_depots,
        "indices": INDICES,
    })


@router.post("", response_class=RedirectResponse)
async def create_quant_system(
    name: Annotated[str, Form()],
    depot_id: Annotated[str, Form()],
    depot_type: Annotated[str, Form()],
    indices: Annotated[list[str], Form()],
    capital_eur: Annotated[float, Form()],
    max_positions: Annotated[int, Form()] = 15,
) -> RedirectResponse:
    now = datetime.now(timezone.utc)
    qs_id = uuid.uuid4().hex[:6]
    safe_max_positions = max(1, min(max_positions, 100))
    resolved_depot_type = await _resolve_depot_type(depot_id=depot_id, depot_type=depot_type)
    await quant_systems_collection().insert_one({
        "quant_system_id": qs_id,
        "name": name.strip(),
        "depot_id": depot_id,
        "depot_type": resolved_depot_type,
        "indices": indices,
        "capital_eur": capital_eur,
        "status": "draft",
        "config_overrides": {
            "portfolio": {
                "max_positions": safe_max_positions,
            },
        },
        "created_at": now,
        "updated_at": now,
    })
    return RedirectResponse(url=f"/quant-systems/{qs_id}", status_code=303)


# ---------------------------------------------------------------------------
# Depot capital helper — must be before /{qs_id} wildcard
# ---------------------------------------------------------------------------

@router.get("/depot-capital/{depot_id}", response_class=JSONResponse)
async def depot_capital(depot_id: str) -> JSONResponse:
    """Return estimated capital (EUR) for a real depot.

    Capital = sum(position.current_value) + latest cash account balance.
    depot_snapshots is keyed by depot_id; account_balances is keyed by account_name
    (same account_name appears in both collections).
    """
    db = finance_db()

    # Latest depot snapshot — gives us positions and the account_name for the cash lookup
    snapshot = await db["depot_snapshots"].find_one(
        {"depot_id": depot_id},
        {"positions": 1, "account_name": 1, "_id": 0},
        sort=[("recorded_at", -1)],
    )

    positions_value = Decimal("0")
    account_name: str | None = None
    if snapshot:
        account_name = snapshot.get("account_name")
        for pos in snapshot.get("positions", []):
            cv = pos.get("current_value") or {}
            raw = cv.get("value") if isinstance(cv, dict) else cv
            try:
                positions_value += Decimal(str(raw)) if raw else Decimal("0")
            except InvalidOperation:
                pass

    # Latest cash balance — joined via account_name
    cash_value = Decimal("0")
    if account_name:
        bal_doc = await db["account_balances"].find_one(
            {"account_name": account_name},
            {"balance": 1, "_id": 0},
            sort=[("recorded_at", -1)],
        )
        if bal_doc:
            bal = bal_doc.get("balance") or {}
            raw = bal.get("value") if isinstance(bal, dict) else bal
            try:
                cash_value = Decimal(str(raw)) if raw else Decimal("0")
            except InvalidOperation:
                pass

    total = float(positions_value + cash_value)
    return JSONResponse({"capital_eur": total})


# ---------------------------------------------------------------------------
# Detail redirect
# ---------------------------------------------------------------------------

@router.get("/{qs_id}", response_class=RedirectResponse)
async def quant_system_detail(qs_id: str) -> RedirectResponse:
    return RedirectResponse(url=f"/quant-systems/{qs_id}/edit")


# ---------------------------------------------------------------------------
# Edit — GET renders form, POST saves
# ---------------------------------------------------------------------------

@router.get("/{qs_id}/edit", response_class=HTMLResponse)
async def edit_quant_system(request: Request, qs_id: str) -> HTMLResponse:
    qs = await quant_systems_collection().find_one({"quant_system_id": qs_id}, _NO_ID)
    if qs is None:
        return HTMLResponse("Quant System not found", status_code=404)
    real_depots = await _real_depots()
    virtual_depots = await virtual_depots_collection().find({}, _NO_ID).sort("name", 1).to_list()
    return templates.TemplateResponse(request, "quant_systems/edit.html", {
        "qs": qs,
        "real_depots": real_depots,
        "virtual_depots": virtual_depots,
        "indices": INDICES,
    })


@router.post("/{qs_id}", response_class=RedirectResponse)
async def save_quant_system(
    qs_id: str,
    name: Annotated[str, Form()],
    depot_id: Annotated[str, Form()],
    depot_type: Annotated[str, Form()],
    indices: Annotated[list[str], Form()],
    capital_eur: Annotated[float, Form()],
    max_positions: Annotated[int, Form()] = 15,
    status: Annotated[str, Form()] = "draft",
) -> RedirectResponse:
    safe_max_positions = max(1, min(max_positions, 100))
    resolved_depot_type = await _resolve_depot_type(depot_id=depot_id, depot_type=depot_type)
    await quant_systems_collection().update_one(
        {"quant_system_id": qs_id},
        {"$set": {
            "name": name.strip(),
            "depot_id": depot_id,
            "depot_type": resolved_depot_type,
            "indices": indices,
            "capital_eur": capital_eur,
            "config_overrides.portfolio.max_positions": safe_max_positions,
            "status": status,
            "updated_at": datetime.now(timezone.utc),
        }},
    )
    return RedirectResponse(url=f"/quant-systems/{qs_id}/edit", status_code=303)


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------

@router.post("/{qs_id}/delete", response_class=RedirectResponse)
async def delete_quant_system(qs_id: str) -> RedirectResponse:
    await quant_systems_collection().delete_one({"quant_system_id": qs_id})
    return RedirectResponse(url="/quant-systems", status_code=303)


# ---------------------------------------------------------------------------
# Virtual depot management (inline from depot picker)
# ---------------------------------------------------------------------------

@router.post("/depots/virtual", response_class=HTMLResponse)
async def create_virtual_depot(
    request: Request,
    name: Annotated[str, Form()],
    starting_capital: Annotated[float, Form()] = 100_000.0,
) -> HTMLResponse:
    now = datetime.now(timezone.utc)
    depot_id = uuid.uuid4().hex[:8]
    await virtual_depots_collection().insert_one({
        "depot_id": depot_id,
        "name": name.strip(),
        "starting_capital": starting_capital,
        "created_at": now,
        "updated_at": now,
    })
    # Return updated virtual depot options fragment for HTMX swap
    virtual_depots = await virtual_depots_collection().find({}, _NO_ID).sort("name", 1).to_list()
    return templates.TemplateResponse(request, "quant_systems/partials/virtual_depot_options.html", {
        "virtual_depots": virtual_depots,
        "selected_depot_id": depot_id,
    })
