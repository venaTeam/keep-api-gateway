import enum
import importlib
import logging
import os
import threading
import time
from typing import Type

from src.config.core import config
from src.services.context_manager import ContextManager
from src.services.identity_manager.authverifierbase import AuthVerifierBase
from src.services.identity_manager.identitymanager import BaseIdentityManager

logger = logging.getLogger(__name__)

# Per-(auth_type, tenant_id) identity-manager cache. Building a manager (Keycloak
# especially) costs ~3 blocking round-trips in __init__, paid today on every
# /preset, /incidents and /auth/* request just for a permission check. The manager
# is user-agnostic, so it is built once and reused for the process lifetime via
# double-checked locking; a failed build propagates and is not cached.
#
# Caveats:
#  - Keyed by (auth_type, tenant_id) only: the first caller's context_manager/kwargs
#    are baked into the cached instance. Safe today (no manager stores per-request
#    state; all callers pass none) — revisit if a kwarg-sensitive manager is added.
#  - The cached manager (and its KeycloakAdmin / requests.Session) is shared across
#    FastAPI threadpool threads; concurrent use is fine in practice (worst case a
#    redundant token refresh).
#  - No invalidation/TTL: a manager that breaks after init (rotated admin creds,
#    changed KEYCLOAK_URL) stays pinned until process restart, and the cache grows
#    per tenant — add eviction at many-tenant scale.
_manager_cache: dict[str, BaseIdentityManager] = {}
_manager_cache_lock = threading.Lock()


class IdentityManagerTypes(enum.Enum):
    """
    Enum class representing different types of identity managers.
    """

    AUTH0 = "auth0"
    KEYCLOAK = "keycloak"
    OKTA = "okta"
    ONELOGIN = "onelogin"
    DB = "db"
    NOAUTH = "noauth"
    OAUTH2PROXY = "oauth2proxy"


class IdentityManagerFactory:
    """
    Factory class for creating identity managers and authentication verifiers.
    """

    @staticmethod
    def get_identity_manager(
        tenant_id: str = None,
        context_manager: ContextManager = None,
        identity_manager_type: IdentityManagerTypes = None,
        **kwargs,
    ) -> BaseIdentityManager:
        """
        Get an instance of the identity manager based on the specified type.

        Args:
            tenant_id (str, optional): The ID of the tenant.
            context_manager (ContextManager, optional): The context manager instance.
            identity_manager_type (IdentityManagerTypes, optional): The type of identity manager to create.
            **kwargs: Additional keyword arguments to pass to the identity manager.

        Returns:
            BaseIdentityManager: An instance of the specified identity manager.
        """
        if not identity_manager_type:
            identity_manager_type = config(
                "AUTH_TYPE", default=IdentityManagerTypes.NOAUTH.value
            )
        elif isinstance(identity_manager_type, IdentityManagerTypes):
            identity_manager_type = identity_manager_type.value

        # Normalize (lowercase + legacy aliases: single_tenant->db, no_auth->noauth,
        # multi_tenant->auth0) BEFORE building the cache key, so the same effective
        # auth type can't land under two keys (e.g. "KEYCLOAK"/"keycloak",
        # "single_tenant"/"db"). _load_manager applies the same mapping, so passing
        # the already-normalized value through it is an idempotent no-op.
        identity_manager_type = (
            IdentityManagerFactory._backward_compatible_get_identity_manager(
                identity_manager_type
            )
        )

        cache_key = f"{identity_manager_type}:{tenant_id}"
        # Lock-free fast path: a populated entry is never mutated, only added.
        manager = _manager_cache.get(cache_key)
        if manager is not None:
            return manager

        with _manager_cache_lock:
            # Re-check under the lock: a concurrent caller may have built it while
            # we waited. The build below runs only while holding the lock, so
            # exactly one thread ever builds and the rest reuse the result.
            manager = _manager_cache.get(cache_key)
            if manager is None:
                # If _load_manager raises, the exception propagates and we never
                # store the entry -> failed builds are not cached.
                manager = IdentityManagerFactory._load_manager(
                    identity_manager_type,
                    "identitymanager",
                    tenant_id,
                    context_manager,
                    **kwargs,
                )
                _manager_cache[cache_key] = manager
            return manager

    @staticmethod
    def get_auth_verifier(scopes: list[str] = []) -> AuthVerifierBase:
        """
        Get an instance of the authentication verifier.

        Args:
            scopes (list[str], optional): A list of scopes for the auth verifier.

        Returns:
            AuthVerifierBase: An instance of the authentication verifier.
        """
        auth_type = os.environ.get(
            "AUTH_TYPE", IdentityManagerTypes.NOAUTH.value
        ).lower()
        return IdentityManagerFactory._load_manager(auth_type, "authverifier", scopes)

    @staticmethod
    def _load_manager(manager_type: str, manager_class: str, *args, **kwargs):
        """
        Load and instantiate a manager class based on the specified type and class.

        Args:
            manager_type (str): The type of manager to load.
            manager_class (str): The class of manager to load.
            *args: Positional arguments to pass to the manager constructor.
            **kwargs: Keyword arguments to pass to the manager constructor.

        Returns:
            The instantiated manager object.

        Raises:
            NotImplementedError: If the specified manager type or class is not implemented.
        """
        try:
            t = time.time()
            logger.debug(f"Loading {manager_class} for {manager_type}")
            manager_type = (
                IdentityManagerFactory._backward_compatible_get_identity_manager(
                    manager_type
                )
            )
            try:
                module = importlib.import_module(
                    f"src.services.identity_manager.identity_managers.{manager_type}.{manager_type}_{manager_class}"
                )
            except ModuleNotFoundError:
                raise NotImplementedError(
                    f"{manager_class} for {manager_type} not implemented"
                )

            logger.debug(
                f"Loaded {manager_class} for {manager_type} in {time.time() - t} seconds"
            )
            # look for the class that contains the manager_class in its name
            for _attr in dir(module):
                if manager_class in _attr.lower() and "base" not in _attr.lower():
                    class_name = _attr
                    break
            manager_class: Type = getattr(module, class_name)
            resp = manager_class(*args, **kwargs)
            logger.debug(f"Found class {class_name} in {time.time() - t} seconds")
            return resp
        except (ImportError, AttributeError):
            raise NotImplementedError(
                f"{manager_class} for {manager_type} not implemented"
            )

    @staticmethod
    def _backward_compatible_get_identity_manager(
        auth_type: str = None,
    ):
        """
        Map old auth_type to new IdentityManagerTypes enum.
        """
        if auth_type.lower() == "single_tenant":
            return IdentityManagerTypes.DB.value
        elif auth_type.lower() == "no_auth":
            return IdentityManagerTypes.NOAUTH.value
        elif auth_type.lower() == "multi_tenant":
            return IdentityManagerTypes.AUTH0.value
        else:
            return auth_type.lower()

