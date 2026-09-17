from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .. import services
from ..deps import current_user
from ..services import ServiceError

router = APIRouter(prefix="/api", tags=["batches"])


def _call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except ServiceError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


class BatchIn(BaseModel):
    code: str
    name: str
    temp_min: float
    temp_max: float


class SamplesIn(BaseModel):
    batch_code: str
    barcodes: list[str] = Field(min_length=1)
    initial_holder: str
    location: str


@router.get("/batches")
def list_batches(user: dict = Depends(current_user)):
    return _call(services.list_batches)


@router.post("/batches", status_code=201)
def create_batch(body: BatchIn, user: dict = Depends(current_user)):
    return _call(services.create_batch, user["username"],
                 body.code, body.name, body.temp_min, body.temp_max)


@router.get("/batches/{code}")
def get_batch(code: str, user: dict = Depends(current_user)):
    return _call(services.get_batch, code)


@router.post("/samples", status_code=201)
def register_samples(body: SamplesIn, user: dict = Depends(current_user)):
    return _call(services.register_samples, user["username"],
                 body.batch_code, body.barcodes, body.initial_holder, body.location)


@router.get("/samples/by-barcode/{barcode}")
def get_sample(barcode: str, user: dict = Depends(current_user)):
    return _call(services.get_sample_by_barcode, barcode)


@router.get("/samples/{sample_id}/timeline")
def get_timeline(sample_id: int, user: dict = Depends(current_user)):
    return _call(services.get_timeline, sample_id)


@router.get("/samples/{sample_id}/verify")
def verify_sample(sample_id: int, user: dict = Depends(current_user)):
    return _call(services.verify_sample_chain, sample_id)
