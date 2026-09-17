from fastapi import APIRouter, Depends

from ..db import read, tx
from ..schemas import BatchIn, SamplesIn
from ..security import get_current_user, require_role
from ..services import create_batch, err, get_batch, register_samples

router = APIRouter(tags=["batches"])


@router.post("/batches", status_code=201)
def create(data: BatchIn, user: dict = Depends(require_role("staff"))):
    with tx() as conn:
        return create_batch(conn, data, user["username"])


@router.get("/batches")
def list_batches(user: dict = Depends(get_current_user)):
    with read() as conn:
        rows = conn.execute(
            "SELECT b.*, "
            " (SELECT COUNT(*) FROM samples s WHERE s.batch_id=b.id) AS sample_count,"
            " (SELECT COUNT(*) FROM events e WHERE e.batch_id=b.id AND e.type='OUT_OF_RANGE' AND e.status='OPEN') AS open_events"
            " FROM batches b ORDER BY b.id DESC"
        ).fetchall()
        return {"batches": [dict(r) for r in rows]}


@router.get("/batches/{batch_code}")
def detail(batch_code: str, user: dict = Depends(get_current_user)):
    with read() as conn:
        return get_batch(conn, batch_code)


@router.post("/batches/{batch_code}/samples", status_code=201)
def add_samples(batch_code: str, data: SamplesIn, user: dict = Depends(require_role("staff"))):
    with tx() as conn:
        return register_samples(conn, batch_code, data.barcodes, user["username"])


@router.get("/batches/{batch_code}/samples")
def list_samples(batch_code: str, user: dict = Depends(get_current_user)):
    with read() as conn:
        batch = conn.execute("SELECT id FROM batches WHERE batch_code=?", (batch_code,)).fetchone()
        if not batch:
            err(404, "BATCH_NOT_FOUND", f"批次 {batch_code} 不存在")
        rows = conn.execute(
            "SELECT s.barcode, s.created_at,"
            " (SELECT cl.to_holder FROM custody_links cl WHERE cl.sample_id=s.id ORDER BY seq DESC LIMIT 1) AS current_holder,"
            " (SELECT cl.seq FROM custody_links cl WHERE cl.sample_id=s.id ORDER BY seq DESC LIMIT 1) AS seq"
            " FROM samples s WHERE s.batch_id=? ORDER BY s.barcode",
            (batch["id"],),
        ).fetchall()
        return {"samples": [dict(r) for r in rows]}
