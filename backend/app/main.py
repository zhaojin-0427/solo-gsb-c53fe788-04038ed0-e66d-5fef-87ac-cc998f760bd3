from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import settings
from .db import init_db
from .routers import audit, auth, batches, custody, events, temperature

app = FastAPI(title="样本链路与隔离处置台", version="1.0.0")

init_db()


@app.exception_handler(ValueError)
async def value_error_handler(_: Request, exc: ValueError):
    return JSONResponse(status_code=400, content={"detail": {"code": "BAD_INPUT", "message": str(exc)}})

app.include_router(auth.router, prefix="/api")
app.include_router(batches.router, prefix="/api")
app.include_router(custody.router, prefix="/api")
app.include_router(temperature.router, prefix="/api")
app.include_router(events.router, prefix="/api")
app.include_router(audit.router, prefix="/api")


@app.get("/api/health")
def health():
    return {"ok": True}


app.mount("/", StaticFiles(directory=settings.static_dir, html=True), name="static")
