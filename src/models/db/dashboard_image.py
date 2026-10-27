"""Images uploaded for dashboard IMAGE widgets.

A row is pending while `dashboard_id` is NULL. The dashboard save that
references it claims it (src/repositories/dashboard_images.py), and deleting
that dashboard deletes it.
"""

from datetime import datetime
from typing import Optional
from uuid import uuid4

from sqlalchemy import Column, ForeignKey, LargeBinary
from sqlalchemy.dialects import mysql
from sqlmodel import Field, SQLModel
from sqlmodel.sql.sqltypes import AutoString


class DashboardImage(SQLModel, table=True):
    id: str = Field(default_factory=lambda: str(uuid4()), primary_key=True)
    tenant_id: str = Field(foreign_key="tenant.id", index=True)
    dashboard_id: Optional[str] = Field(
        default=None,
        sa_column=Column(
            AutoString,
            ForeignKey("dashboard.id", ondelete="CASCADE"),
            nullable=True,
            index=True,
        ),
    )
    name: str
    content_type: str
    size_bytes: int
    image_blob: bytes = Field(
        sa_column=Column(
            LargeBinary().with_variant(mysql.LONGBLOB(), "mysql"), nullable=False
        )
    )
    created_by: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.utcnow)
