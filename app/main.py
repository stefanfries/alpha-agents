from fastapi import FastAPI
from fastapi.responses import RedirectResponse

from app.db import lifespan
from app.routes import pipeline

app = FastAPI(title="Alpha Agents", lifespan=lifespan)
app.include_router(pipeline.router)


@app.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    return RedirectResponse(url="/runs")
