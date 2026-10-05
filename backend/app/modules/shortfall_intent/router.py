from fastapi import APIRouter

from app.modules.shortfall_intent import service
from app.modules.shortfall_intent.schemas import ConfirmIn

router = APIRouter(prefix="/api/intents", tags=["shortfall-intents"])

@router.get("")
def list_intents(status: str | None = None):
    return service.list_intents(status)

@router.post("/{intent_id}/confirm")
def confirm_intent(intent_id: int, body: ConfirmIn | None = None):
    expiry = body.expiry if body else None
    return service.confirm_intent(intent_id, expiry)
