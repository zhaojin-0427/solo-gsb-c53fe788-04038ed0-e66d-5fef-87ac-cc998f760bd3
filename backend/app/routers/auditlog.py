import csv
import io
import json
from typing import Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response

from .. import services
from ..deps import current_user

router = APIRouter(prefix="/api", tags=["audit"])

_EXPORT_FIELDS = ["id", "ts", "actor", "action", "entity", "entity_id", "detail", "prev_hash", "hash"]


@router.get("/audit")
def list_audit(limit: int = Query(default=200, ge=1, le=1000),
               offset: int = Query(default=0, ge=0),
               action: Optional[str] = None,
               user: dict = Depends(current_user)):
    return services.list_audit(limit=limit, offset=offset, action=action)


@router.get("/audit/verify")
def verify_audit(user: dict = Depends(current_user)):
    return services.verify_audit_chain()


@router.get("/audit/export")
def export_audit(format: str = "csv", user: dict = Depends(current_user)):
    rows = services.export_audit_rows()
    if format == "json":
        return Response(
            content=json.dumps(rows, ensure_ascii=False, indent=2),
            media_type="application/json",
            headers={"Content-Disposition": "attachment; filename=audit_export.json"},
        )
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=_EXPORT_FIELDS)
    writer.writeheader()
    for row in rows:
        writer.writerow({k: row.get(k, "") for k in _EXPORT_FIELDS})
    return Response(
        content="\ufeff" + buf.getvalue(),  # BOM 便于 Excel 识别中文
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=audit_export.csv"},
    )
