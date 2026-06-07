from fastapi import FastAPI
from fastapi.responses import RedirectResponse

from app.db import lifespan
from app.routes import pipeline, quant_systems

app = FastAPI(title="Alpha Agents", lifespan=lifespan)
app.include_router(pipeline.router)         # /quant-systems/{qs_id}/executions/...
app.include_router(pipeline.global_router)  # /executions (global list)
app.include_router(quant_systems.router)    # /quant-systems CRUD + /quant-systems/depots/virtual


@app.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    return RedirectResponse(url="/quant-systems")
