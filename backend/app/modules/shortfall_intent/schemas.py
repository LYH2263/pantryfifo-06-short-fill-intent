from pydantic import BaseModel

class ConfirmIn(BaseModel):
    expiry: str | None = None
