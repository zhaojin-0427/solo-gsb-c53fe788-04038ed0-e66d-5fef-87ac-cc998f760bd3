from fastapi import APIRouter, Depends

from ..db import read, tx
from ..schemas import HandoverIn
from ..security import get_current_user, require_role
from ..services import err, handover, verify_custody_chain

router = APIRouter(tags=["custody"])


@router.post("/handovers", status_code=200)
def create_handover(data: HandoverIn, user: dict = Depends(require_role("staff"))):
    """交接登记：幂等（idempotency_key），校验前一持有人，冻结批次拒绝交接。"""
    with tx() as conn:
        link, replay = handover(conn, data, user["username"])
        return {"replay": replay, "link": link}


@router.get("/handovers/recent")
def recent(user: dict = Depends(get_current_user)):
    with read() as conn:
        rows = conn.execute(
            "SELECT cl.*, s.barcode FROM custody_links cl"
            " JOIN samples s ON s.id=cl.sample_id"
            " ORDER BY cl.id DESC LIMIT 20"
        ).fetchall()
        return {"links": [dict(r) for r in rows]}


@router.get("/samples/{barcode}")
def sample_state(barcode: str, user: dict = Depends(get_current_user)):
    with read() as conn:
        row = conn.execute(
            "SELECT s.id AS sample_id, s.barcode, b.batch_code, b.frozen, b.temp_min, b.temp_max,"
            " (SELECT COUNT(*) FROM events e WHERE e.batch_id=b.id AND e.type='OUT_OF_RANGE' AND e.status='OPEN') AS open_events"
            " FROM samples s JOIN batches b ON b.id=s.batch_id WHERE s.barcode=?",
            (barcode,),
        ).fetchone()
        if not row:
            err(404, "SAMPLE_NOT_FOUND", f"条码 {barcode} 未登记")
        last = conn.execute(
            "SELECT to_holder, seq, scanned_at, location FROM custody_links"
            " WHERE sample_id=? ORDER BY seq DESC LIMIT 1",
            (row["sample_id"],),
        ).fetchone()
        return {
            "barcode": row["barcode"], "batch_code": row["batch_code"],
            "frozen": bool(row["frozen"]), "open_events": row["open_events"],
            "temp_min": row["temp_min"], "temp_max": row["temp_max"],
            "current_holder": last["to_holder"] if last else None,
            "seq": last["seq"] if last else None,
            "last_location": last["location"] if last else None,
            "last_scanned_at": last["scanned_at"] if last else None,
        }


@router.get("/samples/{barcode}/timeline")
def timeline(barcode: str, user: dict = Depends(get_current_user)):
    with read() as conn:
        sample = conn.execute(
            "SELECT s.id AS sample_id, s.barcode, b.id AS batch_id, b.batch_code, b.frozen"
            " FROM samples s JOIN batches b ON b.id=s.batch_id WHERE s.barcode=?",
            (barcode,),
        ).fetchone()
        if not sample:
            err(404, "SAMPLE_NOT_FOUND", f"条码 {barcode} 未登记")
        links = conn.execute(
            "SELECT * FROM custody_links WHERE sample_id=? ORDER BY seq", (sample["sample_id"],)
        ).fetchall()
        events = conn.execute(
            "SELECT e.*, r.barcode AS reading_barcode, r.temp AS reading_temp, r.recorded_at AS reading_recorded_at"
            " FROM events e LEFT JOIN temperature_readings r ON r.id=e.reading_id"
            " WHERE e.batch_id=? ORDER BY e.id",
            (sample["batch_id"],),
        ).fetchall()
        return {
            "barcode": sample["barcode"], "batch_code": sample["batch_code"],
            "frozen": bool(sample["frozen"]),
            "links": [dict(r) for r in links],
            "events": [dict(r) for r in events],
        }


@router.get("/samples/{barcode}/verify")
def verify(barcode: str, user: dict = Depends(get_current_user)):
    with read() as conn:
        return verify_custody_chain(conn, barcode)
