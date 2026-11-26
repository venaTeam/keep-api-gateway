"""The base of the assistant's models, with a registry and MetaData of its own.

Not in SQLModel.metadata on purpose: the gateway's tests create every table in there
on SQLite, which has neither jsonb nor identity columns, and its readiness check
(src/models/db/all_models.py) would require tables that only the assistant needs.
"""
from sqlalchemy.orm import registry
from sqlmodel import SQLModel


class AssistantModel(SQLModel, registry=registry()):
    pass
