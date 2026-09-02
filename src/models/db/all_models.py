"""The one list of model tables this image declares.

The set is built from `MODELS` below -- an explicit tuple -- and never from
`SQLModel.metadata`. That registry is process-global: it holds whatever any
import path happened to register, so reading it back would make the answer
depend on which code ran first, which is exactly what this file exists to
prevent. Today the two agree (44 either way), but only the explicit tuple keeps
agreeing after someone adds a model somewhere unexpected.

The imports are still wildcards because a model has to be *imported* to exist at
all; `MODELS` then says which of them are ours.

Determinism matters here because two of these are conditional in the serving
process: `user.py` is only ever imported function-locally and reaches workers by
fork inheritance, and `secret.py` is imported only when `SECRET_MANAGER_TYPE` is
`db`, which is not the default. Without this file the same image would check a
different set of tables depending on how it was configured -- fine for an ORM,
useless for a schema check.

Used by the drift check (`db_on_start.schema_drift`), and by nothing else. The
migrations live in `keep-migrations` and import no models at all -- they are
hand-written by policy, so nothing here has to be importable from a revision.
"""

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

#: Every table-backed model this image owns. Add a model, add it here -- and
#: `test_declared_tables_matches_the_model_modules` fails until you do.
MODELS = (
    Action,  # noqa: F405
    AIFeedback,  # noqa: F405
    AISuggestion,  # noqa: F405
    Alert,  # noqa: F405
    AlertAudit,  # noqa: F405
    AlertDeduplicationEvent,  # noqa: F405
    AlertDeduplicationRule,  # noqa: F405
    AlertField,  # noqa: F405
    AlertRaw,  # noqa: F405
    AlertToIncident,  # noqa: F405
    CommentMention,  # noqa: F405
    Dashboard,  # noqa: F405
    EnrichmentEvent,  # noqa: F405
    EnrichmentLog,  # noqa: F405
    ExternalAIConfigAndMetadata,  # noqa: F405
    ExtractionRule,  # noqa: F405
    Facet,  # noqa: F405
    Incident,  # noqa: F405
    IncidentEnrichment,  # noqa: F405
    LastAlert,  # noqa: F405
    LastAlertToIncident,  # noqa: F405
    MaintenanceWindowRule,  # noqa: F405
    MappingRule,  # noqa: F405
    Operator,  # noqa: F405
    PMIMatrix,  # noqa: F405
    Preset,  # noqa: F405
    PresetTagLink,  # noqa: F405
    Provider,  # noqa: F405
    ProviderExecutionLog,  # noqa: F405
    ProviderImage,  # noqa: F405
    Rule,  # noqa: F405
    Secret,  # noqa: F405
    System,  # noqa: F405
    Tag,  # noqa: F405
    Tenant,  # noqa: F405
    TenantApiKey,  # noqa: F405
    TenantInstallation,  # noqa: F405
    TenantRoleGrant,  # noqa: F405
    TopologyApplication,  # noqa: F405
    TopologyService,  # noqa: F405
    TopologyServiceApplication,  # noqa: F405
    TopologyServiceDependency,  # noqa: F405
    User,  # noqa: F405
    UserPresetColumnConfig,  # noqa: F405
)


def declared_tables() -> dict[str, set[str]]:
    """{table name -> column names} for every table this image declares."""
    return {
        model.__tablename__: {column.name for column in model.__table__.columns}
        for model in MODELS
    }
