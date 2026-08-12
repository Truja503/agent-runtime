"""Authentication for the *normal* API.

Bearer tokens compared in constant time against a configured table. This is
adequate for a single-tenant MVP and is not a replacement for an identity
provider — see "Known limitations" in the README.

What matters architecturally is what this authentication does **not** unlock:
no token issued here can approve a privileged action. That requires an operator
credential from a different configuration key, checked by a different component
(:mod:`privileged.auth`), and — by default — a different process.
"""

from __future__ import annotations

import hmac

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from app.config import ApiPrincipalConfig

bearer_scheme = HTTPBearer(auto_error=False)


class ApiCaller(BaseModel):
    name: str
    role: str

    @property
    def is_operator(self) -> bool:
        return self.role == "operator"


class ApiAuthenticator:
    def __init__(self, principals: tuple[ApiPrincipalConfig, ...]) -> None:
        self._principals = principals

    def authenticate(self, token: str) -> ApiCaller | None:
        if not token:
            return None
        matched: ApiCaller | None = None
        # Every entry is checked so that response time does not reveal how much
        # of a token was correct.
        for principal in self._principals:
            if hmac.compare_digest(token, principal.token.get_secret_value()):
                matched = ApiCaller(name=principal.name, role=principal.role)
        return matched


async def current_caller(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> ApiCaller:
    authenticator: ApiAuthenticator = request.app.state.authenticator
    caller = (
        authenticator.authenticate(credentials.credentials) if credentials else None
    )
    if caller is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="a valid bearer token is required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return caller


async def require_operator(
    caller: ApiCaller = Depends(current_caller),
) -> ApiCaller:
    if not caller.is_operator:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="this endpoint requires the 'operator' role",
        )
    return caller


router = APIRouter(prefix="/auth", tags=["auth"])


@router.get("/whoami", response_model=ApiCaller)
async def whoami(caller: ApiCaller = Depends(current_caller)) -> ApiCaller:
    """Echo the caller's identity. Never echoes the token."""
    return caller
