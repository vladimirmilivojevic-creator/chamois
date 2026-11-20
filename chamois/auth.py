"""Bearer-token auth (ARNICA uses `Authorization: Bearer <token>`) and roles."""

import secrets
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from chamois.config import Role, Settings, User

_bearer = HTTPBearer(auto_error=False)


def get_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> User:
    if credentials is not None:
        for token, user in settings.tokens.items():
            # constant-time comparison, so response timing does not leak tokens
            if secrets.compare_digest(credentials.credentials.encode(), token.encode()):
                return user
    raise HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        "missing or invalid bearer token",
        headers={"WWW-Authenticate": "Bearer"},
    )


def require_operator(user: Annotated[User, Depends(current_user)]) -> User:
    if user.role != Role.OPERATOR:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "operator role required")
    return user


CurrentUser = Annotated[User, Depends(current_user)]
Operator = Annotated[User, Depends(require_operator)]
