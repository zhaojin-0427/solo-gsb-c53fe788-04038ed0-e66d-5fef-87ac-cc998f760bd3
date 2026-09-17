from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .. import services
from ..deps import current_user
from ..services import ServiceError

router = APIRouter(prefix="/api", tags=["temperatures"])


class ReadingIn(BaseModel):
    recorded_at: str
    temperature: float
    barcode: Optional[str] = None


class ImportIn(BaseModel):
    batch_code: str
    readings: list[ReadingIn] = Field(min_length=1)
    filename: Optional[str] = None


@router.post("/temperature/import", status_code=201)
def import_temperatures(body: ImportIn, user: dict = Depends(current_user)):
    try:
        return services.import_temperatures(
            actor=user["username"],
            batch_code=body.batch_code,
            readings=[r.model_dump() for r in body.readings],
            filename=body.filename,
        )
    except ServiceError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)
