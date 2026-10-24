"""FastAPI app: API routes and the static frontend."""

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from backend import config

app = FastAPI(title="StockLens")
app.mount("/static", StaticFiles(directory=config.FRONTEND_DIR), name="static")


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "mode": config.MODE}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(config.FRONTEND_DIR / "index.html")
