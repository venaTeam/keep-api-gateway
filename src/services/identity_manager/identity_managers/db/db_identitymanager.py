import os

import jwt
from fastapi import HTTPException
from fastapi.responses import JSONResponse

from src.repositories.db import create_user as create_user_in_db
from src.repositories.db import delete_user as delete_user_from_db
from src.repositories.db import get_user
from src.repositories.db import get_users as get_users_from_db
from src.models.user import User
from src.services.context_manager import ContextManager
from src.services.identity_manager.identity_managers.db.db_authverifier import DbAuthVerifier
from src.services.identity_manager.identitymanager import BaseIdentityManager


def _record_login_failure(reason: str) -> None:
    """Increment the failed-login counter. Never raises."""
    try:
        from src.repositories.metrics import login_failures_total

        login_failures_total.labels(reason=reason).inc()
    except Exception:
        pass


class DbIdentityManager(BaseIdentityManager):
    def __init__(self, tenant_id, context_manager: ContextManager, **kwargs):
        super().__init__(tenant_id, context_manager, **kwargs)
        self.logger.info("DB Identity Manager initialized")

    def on_start(self, app) -> None:
        """
        Initialize the identity manager.
        """
        # This is a special method that is called when the identity manager is
        # initialized. It is used to set up the identity manager with the FastAPI
        self.logger.info("Adding signin endpoint")

        @app.post("/signin")
        def signin(body: dict):
            # block empty passwords (e.g. user provisioned)
            if not body.get("password"):
                _record_login_failure("empty_password")
                return JSONResponse(
                    status_code=401,
                    content={"message": "Empty password"},
                )

            # validate the user/password
            tenant_id = body.get("tenant_id") or body.get("tenantId") or self.tenant_id
            user = get_user(body.get("username"), body.get("password"), tenant_id=tenant_id)
            if not user:
                _record_login_failure("invalid_credentials")
                return JSONResponse(
                    status_code=401,
                    content={"message": "Invalid username or password"},
                )
            # generate a JWT secret
            jwt_secret = os.environ.get("KEEP_JWT_SECRET")
            if not jwt_secret:
                self.logger.info("missing KEEP_JWT_SECRET environment variable")
                raise HTTPException(status_code=401, detail="Missing JWT secret")
            token = jwt.encode(
                {
                    "email": user.username,
                    "tenant_id": user.tenant_id,
                    "role": user.role,
                },
                jwt_secret,
                algorithm="HS256",
            )
            # return the token
            return {
                "accessToken": token,
                "tenantId": user.tenant_id,
                "email": user.username,
                "role": user.role,
            }

        self.logger.info("Added signin endpoint")

    def get_users(self, tenant_id=None) -> list[User]:
        users = get_users_from_db(tenant_id or self.tenant_id)
        users = [
            User(
                email=f"{user.username}",
                name=user.username,
                role=user.role,
                last_login=str(user.last_sign_in) if user.last_sign_in else None,
                created_at=str(user.created_at),
            )
            for user in users
        ]
        return users

    def create_user(
        self, user_email: str, user_name: str, password: str, role: str, groups: list
    ) -> dict:
        # Username is redundant, but we need it in other auth types
        # Groups: for future use
        try:
            user = create_user_in_db(self.tenant_id, user_email, password, role)
            return User(
                email=user_email,
                name=user_email,
                role=role,
                last_login=None,
                created_at=str(user.created_at),
            )
        except Exception:
            raise HTTPException(status_code=409, detail="User already exists")

    def delete_user(self, user_email: str) -> dict:
        try:
            delete_user_from_db(user_email, tenant_id=self.tenant_id)
            return {"status": "OK"}
        except Exception:
            raise HTTPException(status_code=404, detail="User not found")

    def get_auth_verifier(self, scopes) -> DbAuthVerifier:
        return DbAuthVerifier(scopes)

    def update_user(self, user_email: str, update_data: dict) -> User:
        raise NotImplementedError("DbIdentityManager.update_user")


