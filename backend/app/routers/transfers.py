from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import services
from ..deps import current_user
from ..services import ServiceError

router = APIRouter(prefix="/api", tags=["transfers"])


class TransferIn(BaseModel):
    barcode: str
    expected_from_holder: str
    to_holder: str
    location: str
    idempotency_key: str
    scanned_at: Optional[str] = None


@router.post("/transfers", status_code=201)
def create_transfer(body: TransferIn, user: dict = Depends(current_user)):
    try:
        return services.create_transfer(
            actor=user["username"],
            barcode=body.barcode,
            expected_from_holder=body.expected_from_holder,
            to_holder=body.to_holder,
            location=body.location,
            idempotency_key=body.idempotency_key,
            scanned_at=body.scanned_at,
        )
    except ServiceError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)
