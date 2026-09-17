import csv
import io
import json

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from ..db import read
from ..security import get_current_user
from ..services import verify_audit_chain

router = APIRouter(tags=["audit"])

COLUMNS = ["seq", "created_at", "actor", "action", "entity", "entity_id", "payload", "prev_hash", "hash"]


@router.get("/audit")
def list_audit(limit: int = 200, user: dict = Depends(get_current_user)):
    limit = max(1, min(limit, 1000))
    with read() as conn:
        rows = conn.execute(
            "SELECT seq, actor, action, entity, entity_id, payload, prev_hash, hash, created_at"
            " FROM audit_log ORDER BY seq DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return {"entries": [dict(r) for r in rows]}


@router.get("/audit/verify")
def verify(user: dict = Depends(get_current_user)):
    with read() as conn:
        return verify_audit_chain(conn)


@router.get("/audit/export")
def export(format: str = "csv", user: dict = Depends(get_current_user)):
    with read() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT seq, actor, action, entity, entity_id, payload, prev_hash, hash, created_at"
            " FROM audit_log ORDER BY seq"
        ).fetchall()]
    if format == "json":
        body = json.dumps(rows, ensure_ascii=False, indent=2)
        return StreamingResponse(
            iter([body]), media_type="application/json",
            headers={"Content-Disposition": "attachment; filename=audit_log.json"},
        )
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=COLUMNS, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return StreamingResponse(
        iter([buf.getvalue()]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=audit_log.csv"},
    )
