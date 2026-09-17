from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import services
from ..deps import current_user, require_approver
from ..services import ServiceError

router = APIRouter(prefix="/api", tags=["exceptions"])


class DispositionIn(BaseModel):
    action: str  # release | correct
    reason: str


class AckIn(BaseModel):
    note: str = ""


@router.get("/exceptions")
def list_exceptions(all: bool = False, user: dict = Depends(current_user)):
    return services.list_exceptions(include_resolved=all)


@router.post("/exceptions/{excursion_id}/dispositions", status_code=201)
def dispose(excursion_id: int, body: DispositionIn, user: dict = Depends(require_approver)):
    try:
        return services.dispose_excursion(user["username"], excursion_id, body.action, body.reason)
    except ServiceError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.post("/conflicts/{conflict_id}/acknowledge")
def acknowledge(conflict_id: int, body: AckIn, user: dict = Depends(require_approver)):
    try:
        return services.acknowledge_conflict(user["username"], conflict_id, body.note)
    except ServiceError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)
