# TODO - refactor context manager to support multitenancy in a more robust way
import logging
from typing import Any, TypedDict

import click
import json5


from src.config.core import config
from src.repositories.db import get_session_sync
from src.models.alert import AlertDto
from src.models.incident import IncidentDto






class ContextManager:
    def __init__(
        self,
        tenant_id,
    ):
        self.logger = logging.getLogger(__name__)
        self.tenant_id = tenant_id
        self.providers_context = {}
        self.event_context: AlertDto = {}
        self.incident_context: IncidentDto | None = None
        self.consts_context = {}
        self.secret_context = {}
        # cli context
        try:
            self.click_context = click.get_current_context()
        except RuntimeError:
            self.click_context = {}
        self.aliases = {}
        # dependencies are used so iohandler will be able to use the output class of the providers
        # e.g. let's say bigquery_provider results are google.cloud.bigquery.Row
        #     and we want to use it in iohandler, we need to import it before the eval
        self.dependencies = set()
        self._api_key = None
        self.__loggers = {}

    @property
    def api_url(self):
        """
        The URL of the Keep API
        """
        return config("KEEP_API_URL")

    @property
    def api_key(self):
        # avoid circular import
        from utils.tenant_utils import get_or_create_api_key

        if self._api_key is None:
            with get_session_sync() as session:
                self._api_key = get_or_create_api_key(
                    session=session,
                    created_by="system",
                    tenant_id=self.tenant_id,
                    unique_api_key_id="webhook",
                )
        return self._api_key



    def set_event_context(self, event):
        self.event_context = event

    def set_incident_context(self, incident):
        self.incident_context = incident

    def set_consts_context(self, consts):
        self.consts_context = consts



    def set_secret_context(self):
        """
        Set the secret context.
        If no secret is provided, attempt to load it from the secret manager.
        """
        from secretmanager.secretmanagerfactory import SecretManagerFactory

        secret_manager = SecretManagerFactory.get_secret_manager(self)

        secret_key = f"{self.tenant_id}_secrets"
        try:
            secret = secret_manager.read_secret(secret_name=secret_key, is_json=True)
            self.secret_context = secret or {}
        except Exception:
            self.logger.warning(
                "Could not load secrets",
                extra={"tenant_id": self.tenant_id},
            )
            self.secret_context = {}

    def get_full_context(self, exclude_providers=False, exclude_env=False):
        """
        Gets full context

        Returns:
            dict: dictinoary contains all context
                  providers - all context about providers (configuration, etc)
                  steps - all context about steps (output, conditions, etc)
                  foreach - all context about the current 'foreach'
                            foreach can be in two modes:
                                1. "step foreach" - for step result
                                2. "condition foreach" - for each condition result
                            whereas in (2), the {{ foreach.value }} contains (1), in the (1) case, we need to explicitly put in under (value)
                            anyway, this should be refactored to something more structured
        """
        full_context = {
            "event": self.event_context,

            "consts": self.consts_context,
            "secrets": self.secret_context,
        }

        if not exclude_providers:
            full_context["providers"] = self.providers_context

        full_context.update(self.aliases)
        return full_context






