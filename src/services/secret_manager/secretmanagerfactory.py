import enum

from src.config.core import config
from src.services.context_manager import ContextManager
from src.services.secret_manager.secretmanager import BaseSecretManager


class SecretManagerTypes(enum.Enum):
    FILE = "file"
    GCP = "gcp"
    K8S = "k8s"
    VAULT = "vault"
    AWS = "aws"
    DB = "db"


class SecretManagerFactory:
    @staticmethod
    def get_secret_manager(
        context_manager: ContextManager,
        secret_manager_type: SecretManagerTypes = None,
        **kwargs,
    ) -> BaseSecretManager:
        if not secret_manager_type:
            secret_manager_type = SecretManagerTypes[
                config("SECRET_MANAGER_TYPE", default="FILE").upper()
            ]
        if secret_manager_type == SecretManagerTypes.FILE:
            from src.services.secret_manager.filesecretmanager import FileSecretManager

            return FileSecretManager(context_manager, **kwargs)
        elif secret_manager_type == SecretManagerTypes.GCP:
            from src.services.secret_manager.gcpsecretmanager import GcpSecretManager

            return GcpSecretManager(context_manager, **kwargs)
        elif secret_manager_type == SecretManagerTypes.K8S:
            from src.services.secret_manager.kubernetessecretmanager import (
                KubernetesSecretManager,
            )

            return KubernetesSecretManager(context_manager, **kwargs)
        elif secret_manager_type == SecretManagerTypes.VAULT:
            from src.services.secret_manager.vaultsecretmanager import VaultSecretManager

            return VaultSecretManager(context_manager, **kwargs)
        elif secret_manager_type == SecretManagerTypes.AWS:
            from src.services.secret_manager.awssecretmanager import AwsSecretManager

            return AwsSecretManager(context_manager, **kwargs)
        elif secret_manager_type == SecretManagerTypes.DB:
            from src.services.secret_manager.dbsecretmanager import DbSecretManager

            return DbSecretManager(context_manager, **kwargs)

        raise NotImplementedError(
            f"Secret manager type {str(secret_manager_type)} not implemented"
        )

