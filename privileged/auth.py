"""Operator authentication for the privileged domain.

This is a second, independent authority. An API token that is valid for
``POST /tasks`` is worth nothing here: operator credentials live in their own
configuration key, are stored only as scrypt hashes, and are checked with a
constant-time comparison.

It is deliberately boring. For production you would put a real IdP, hardware
tokens, or an out-of-band channel in front of this — see the README's
"Known limitations".
"""

from __future__ import annotations

import hmac
import secrets
from hashlib import scrypt

#: Interactive-login parameters. Roughly 100 ms per verification on a laptop.
_N = 2**14
_R = 8
_P = 1
_DKLEN = 32
_PREFIX = "scrypt"


class Operator:
    """An authenticated human. Never constructed from agent input."""

    def __init__(self, operator_id: str) -> None:
        self.operator_id = operator_id

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"Operator({self.operator_id!r})"


def hash_secret(secret: str, *, salt: bytes | None = None) -> str:
    if not secret:
        raise ValueError("operator secret must not be empty")
    salt = salt or secrets.token_bytes(16)
    derived = scrypt(secret.encode("utf-8"), salt=salt, n=_N, r=_R, p=_P, dklen=_DKLEN)
    return f"{_PREFIX}${_N}${_R}${_P}${salt.hex()}${derived.hex()}"


def verify_secret(secret: str, stored: str) -> bool:
    """Constant-time verification. Returns False rather than raising."""
    try:
        prefix, n_raw, r_raw, p_raw, salt_hex, hash_hex = stored.split("$")
        if prefix != _PREFIX:
            return False
        derived = scrypt(
            secret.encode("utf-8"),
            salt=bytes.fromhex(salt_hex),
            n=int(n_raw),
            r=int(r_raw),
            p=int(p_raw),
            dklen=len(hash_hex) // 2,
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(derived.hex(), hash_hex)


class OperatorAuthenticator:
    """Checks operator credentials against the configured hash table."""

    def __init__(self, operators: dict[str, str]) -> None:
        #: operator_id -> scrypt hash. Empty means nobody can approve anything,
        #: which is the correct default for a system nobody has configured yet.
        self._operators = dict(operators)

    @property
    def configured(self) -> bool:
        return bool(self._operators)

    def authenticate(self, operator_id: str, secret: str) -> Operator | None:
        stored = self._operators.get(operator_id)
        if stored is None:
            # Spend the same work on an unknown id so timing does not reveal
            # which operator ids exist.
            verify_secret(secret, hash_secret("decoy-value"))
            return None
        if not verify_secret(secret, stored):
            return None
        return Operator(operator_id)
