"""The one list of model modules.

Importing this populates `SQLModel.metadata` with every table this image
declares, deterministically. That last word is the point: the serving process's
metadata is otherwise an accident of which code paths happened to run.
`src/models/db/user.py` is only ever imported function-locally and reaches
workers by fork inheritance; `src/models/db/secret.py` is imported only when
`SECRET_MANAGER_TYPE == "db"`, which is not the default. So the same image would
otherwise declare a different set of tables depending on its configuration --
fine for an ORM, useless for a schema check.

Used by the drift check (`db_on_start.schema_drift`), and by nothing else. The
migrations live in `keep-migrations` and import no models at all -- they are
hand-written by policy, so nothing here has to be importable from a revision.
"""

from sqlmodel import SQLModel

from src.models.db.action import *  # noqa: F401,F403
from src.models.db.ai_external import *  # noqa: F401,F403
from src.models.db.ai_suggestion import *  # noqa: F401,F403
from src.models.db.alert import *  # noqa: F401,F403
from src.models.db.dashboard import *  # noqa: F401,F403
from src.models.db.enrichment_event import *  # noqa: F401,F403
from src.models.db.extraction import *  # noqa: F401,F403
from src.models.db.facet import *  # noqa: F401,F403
from src.models.db.incident import *  # noqa: F401,F403
from src.models.db.maintenance_window import *  # noqa: F401,F403
from src.models.db.mapping import *  # noqa: F401,F403
from src.models.db.operator import *  # noqa: F401,F403
from src.models.db.preset import *  # noqa: F401,F403
from src.models.db.provider import *  # noqa: F401,F403
from src.models.db.provider_image import *  # noqa: F401,F403
from src.models.db.rule import *  # noqa: F401,F403
from src.models.db.secret import *  # noqa: F401,F403
from src.models.db.statistics import *  # noqa: F401,F403
from src.models.db.system import *  # noqa: F401,F403
from src.models.db.tenant import *  # noqa: F401,F403
from src.models.db.tenant_role_grant import *  # noqa: F401,F403
from src.models.db.topology import *  # noqa: F401,F403
from src.models.db.user import *  # noqa: F401,F403


def declared_tables() -> dict[str, set[str]]:
    """{table name -> column names} for every table this image declares."""
    return {
        name: {column.name for column in table.columns}
        for name, table in SQLModel.metadata.tables.items()
    }
