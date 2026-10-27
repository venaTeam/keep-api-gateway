from typing import List

from pydantic import BaseModel, Field


class RoleAssignment(BaseModel):
    # subject = the identifier the token carries: email for a user, group path
    # for a group (VENA-5596 -- we store the token identifier, not the id).
    subject: str
    subject_type: str
    role: str


class CreateTenantRequest(BaseModel):
    name: str
    role_mappings: List[RoleAssignment] = Field(default_factory=list)


class UpdateTenantRequest(BaseModel):
    name: str


class TenantOut(BaseModel):
    id: str
    name: str

    class Config:
        orm_mode = True