from fastapi import APIRouter, Depends

from ..db import read, tx
from ..schemas import ReasonIn
from ..security import get_current_user, require_role
from ..services import add_correction, release_event

router = APIRouter(tags=["events"])


@router.get("/events")
def list_events(status: str = "ALL", user: dict = Depends(get_current_user)):
    sql = (
        "SELECT e.*, b.batch_code, b.temp_min, b.temp_max,"
        " r.barcode AS reading_barcode, r.temp AS reading_temp, r.recorded_at AS reading_recorded_at"
        " FROM events e JOIN batches b ON b.id=e.batch_id"
        " LEFT JOIN temperature_readings r ON r.id=e.reading_id"
    )
    params: tuple = ()
    if status in ("OPEN", "RESOLVED", "CLOSED"):
        sql += " WHERE e.status=?"
        params = (status,)
    sql += " ORDER BY e.id DESC LIMIT 200"
    with read() as conn:
        rows = conn.execute(sql, params).fetchall()
        return {"events": [dict(r) for r in rows]}


@router.post("/events/{event_id}/release")
def release(event_id: int, data: ReasonIn, user: dict = Depends(require_role("supervisor"))):
    """解除隔离：仅授权人员（supervisor），必须填写理由。"""
    with tx() as conn:
        return release_event(conn, event_id, data.reason, user["username"])


@router.post("/events/{event_id}/corrections", status_code=201)
def correct(event_id: int, data: ReasonIn, user: dict = Depends(require_role("supervisor"))):
    """纠正事件：仅授权人员（supervisor），必须填写理由。"""
    with tx() as conn:
        return add_correction(conn, event_id, data.reason, user["username"])
