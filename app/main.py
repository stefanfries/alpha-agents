import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse, RedirectResponse

from app.config import settings
from app.db import lifespan
from app.routes import pipeline, quant_systems

app = FastAPI(title="Alpha Agents", lifespan=lifespan)
app.include_router(pipeline.router)         # /quant-systems/{qs_id}/executions/...
app.include_router(quant_systems.router)    # /quant-systems CRUD + /quant-systems/depots/virtual


@app.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    return RedirectResponse(url="/quant-systems")


@app.get("/api/finhub/health", include_in_schema=False)
async def finhub_health_proxy() -> JSONResponse:
    """Proxy the FinHub /health check so the browser avoids CORS and the API URL stays server-side."""
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            r = await client.get(f"{settings.finhub.base_url}/health")
            r.raise_for_status()
            return JSONResponse({"status": "ok"})
    except Exception:
        return JSONResponse(status_code=503, content={"status": "error"})
