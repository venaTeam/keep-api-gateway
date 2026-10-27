import json
import logging
import os
import re

from src.config.core import config


class Parser:
    def __init__(self):
        self.logger = logging.getLogger(__name__)

    def _parse_providers_from_env(self, context_manager):
        """
        Parse providers from the KEEP_PROVIDERS environment variables.
            Either KEEP_PROVIDERS to load multiple providers or KEEP_PROVIDER_<provider_name> can be used.

        KEEP_PROVIDERS is a JSON string of the providers config.
            (e.g. {"slack-prod": {"authentication": {"webhook_url": "https://hooks.slack.com/services/..."}}})
        """
        providers_json = os.environ.get("KEEP_PROVIDERS")

        # check if env var is absolute or relative path to a providers json file
        if providers_json and re.compile(r"^(\/|\.\/|\.\.\/).*\.json$").match(
            providers_json
        ):
            with open(file=providers_json, mode="r", encoding="utf8") as file:
                providers_json = file.read()

        if providers_json:
            try:
                self.logger.debug(
                    "Parsing providers from KEEP_PROVIDERS environment variable"
                )
                providers_dict = json.loads(providers_json)
                self._inject_env_variables(providers_dict)
                context_manager.providers_context.update(providers_dict)
                self.logger.debug(
                    "Providers parsed successfully from KEEP_PROVIDERS environment variable"
                )
            except json.JSONDecodeError:
                self.logger.error(
                    "Error parsing providers from KEEP_PROVIDERS environment variable"
                )

        for env in os.environ.keys():
            if env.startswith("KEEP_PROVIDER_"):
                # KEEP_PROVIDER_SLACK_PROD
                provider_name = (
                    env.replace("KEEP_PROVIDER_", "").replace("_", "-").lower()
                )
                try:
                    self.logger.debug(f"Parsing provider {provider_name} from {env}")
                    # {'authentication': {'webhook_url': 'https://hooks.slack.com/services/...'}}
                    provider_config = json.loads(os.environ.get(env))
                    self._inject_env_variables(provider_config)
                    context_manager.providers_context[provider_name] = provider_config
                    self.logger.debug(
                        f"Provider {provider_name} parsed successfully from {env}"
                    )
                except json.JSONDecodeError:
                    self.logger.error(
                        f"Error parsing provider config from environment variable {env}"
                    )

    def _inject_env_variables(self, config_obj):
        """
        Recursively inject environment variables into the config.
        """
        if isinstance(config_obj, dict):
            for key, value in config_obj.items():
                config_obj[key] = self._inject_env_variables(value)
        elif isinstance(config_obj, list):
            return [self._inject_env_variables(item) for item in config_obj]
        elif (
            isinstance(config_obj, str)
            and config_obj.startswith("$(")
            and config_obj.endswith(")")
        ):
            env_var = config_obj[2:-1]
            env_var_val = os.environ.get(env_var)
            if not env_var_val:
                self.logger.warning(
                    f"Environment variable {env_var} not found while injecting into config"
                )
                return config_obj
            return env_var_val
        return config_obj
