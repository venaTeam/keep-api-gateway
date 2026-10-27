import asyncio
from src.repositories.db_utils import get_session_sync
from src.models.db.alert import Alert
from sqlalchemy import inspect
from src.core.db import engine

insp = inspect(engine)
columns = insp.get_columns('alert')
for col in columns:
    print(col['name'])
