from fastapi import APIRouter, Depends

from ..db import read, tx
from ..schemas import TempImportIn
from ..security import get_current_user, require_role
from ..services import import_temperatures

router = APIRouter(tags=["temperature"])


@router.post("/temperature/import")
def import_readings(data: TempImportIn, user: dict = Depends(require_role("staff"))):
    """导入温度记录：内容级幂等（reading_uid），超限自动生成事件并冻结批次。"""
    with tx() as conn:
        return import_temperatures(conn, data, user["username"])


@router.get("/temperature/readings")
def list_readings(batch_code: str = "", user: dict = Depends(get_current_user)):
    with read() as conn:
        if batch_code:
            rows = conn.execute(
                "SELECT r.*, b.batch_code FROM temperature_readings r"
                " JOIN batches b ON b.id=r.batch_id WHERE b.batch_code=?"
                " ORDER BY r.id DESC LIMIT 100",
                (batch_code,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT r.*, b.batch_code FROM temperature_readings r"
                " JOIN batches b ON b.id=r.batch_id ORDER BY r.id DESC LIMIT 100"
            ).fetchall()
        return {"readings": [dict(r) for r in rows]}
