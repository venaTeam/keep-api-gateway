from src.repositories.cel_to_sql.properties_metadata import PropertiesMetadata
from src.repositories.cel_to_sql.sql_providers.base import BaseCelToSqlProvider
from src.repositories.cel_to_sql.sql_providers.mysql import CelToMySqlProvider
from src.repositories.cel_to_sql.sql_providers.postgresql import CelToPostgreSqlProvider
from src.repositories.cel_to_sql.sql_providers.sqlite import CelToSqliteProvider



def get_cel_to_sql_provider(
    properties_metadata: PropertiesMetadata,
) -> BaseCelToSqlProvider:
    from src.repositories.db import engine
    return get_cel_to_sql_provider_for_dialect(engine.dialect.name, properties_metadata)


def get_cel_to_sql_provider_for_dialect(
    dialect_name: str,
    properties_metadata: PropertiesMetadata,
) -> BaseCelToSqlProvider:
    from src.repositories.db import engine
    if dialect_name == "sqlite":
        return CelToSqliteProvider(engine.dialect, properties_metadata)
    elif dialect_name == "mysql":
        return CelToMySqlProvider(engine.dialect, properties_metadata)
    elif dialect_name == "postgresql":
        return CelToPostgreSqlProvider(engine.dialect, properties_metadata)

    else:
        raise ValueError(f"Unsupported dialect: {engine.dialect.name}")
