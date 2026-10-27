from typing import Optional

from pydantic import BaseModel


class CreateOperatorRequest(BaseModel):
    group: str
    tenant_id: str
    name: Optional[str] = None


class OperatorOut(BaseModel):
    id: str
    name: str
    group: str
    tenant_id: str
    apikey: str

    class Config:
        orm_mode = True 