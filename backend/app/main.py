"""FastAPI 应用入口：API 路由 + 前端静态文件。"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import config, services
from .db import init_db, read_conn
from .routers import auditlog, auth, batches, exceptions, temperatures, transfers
from .services import ServiceError

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("custody")


@asynccontextmanager
async def lifespan(_app):
    init_db()
    if config.SEED_DEMO_DATA:
        try:
            _seed_demo_data()
        except ServiceError as e:
            log.warning("演示数据写入跳过：%s", e.detail)
    yield


app = FastAPI(title=config.APP_TITLE, lifespan=lifespan)

app.include_router(auth.router)
app.include_router(batches.router)
app.include_router(transfers.router)
app.include_router(temperatures.router)
app.include_router(exceptions.router)
app.include_router(auditlog.router)


@app.get("/api/health")
def health():
    return {"status": "ok", "app": config.APP_TITLE}


def _seed_demo_data():
    """可选演示数据：仅在没有任何批次时写入一次。"""
    with read_conn() as conn:
        has_data = conn.execute("SELECT 1 FROM batches LIMIT 1").fetchone()
    if has_data:
        return
    log.info("SEED_DEMO_DATA=true，写入演示数据")
    actor = "admin"
    services.create_batch(actor, "DEMO-2026-001", "演示批次（2~8°C 冷链）", 2.0, 8.0)
    services.register_samples(actor, "DEMO-2026-001",
                              ["DEMO-0001", "DEMO-0002", "DEMO-0003"],
                              initial_holder="张倩", location="样本接收室")
    services.create_transfer(actor, "DEMO-0001", "张倩", "李牧", "冷藏库 A-01",
                             "seed-transfer-0001")
    services.import_temperatures(actor, "DEMO-2026-001", [
        {"recorded_at": "2026-09-16T08:00:00+00:00", "temperature": 4.2, "barcode": "DEMO-0001"},
        {"recorded_at": "2026-09-16T12:00:00+00:00", "temperature": 9.6, "barcode": "DEMO-0002"},
        {"recorded_at": "2026-09-16T12:00:00+00:00", "temperature": 5.1},
    ])
    log.info("演示数据完成：批次 DEMO-2026-001，样本 DEMO-0001~0003，DEMO-0002 触发超限冻结")


# 静态前端挂载在最后，/api/* 路由优先
app.mount("/", StaticFiles(directory=config.STATIC_DIR, html=True), name="static")
