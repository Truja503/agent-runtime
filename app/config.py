"""Runtime configuration, loaded from the environment.

Secrets are held as ``SecretStr`` so that a stray ``repr()`` in a log line or a
traceback prints ``**********`` instead of the credential.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.errors import RuntimeConfigError


class ProviderKind(StrEnum):
    """Which trust domain a model lives in.

    This is not cosmetic: the policy engine reads it. A CLOUD-backed principal
    can never be granted a privileged capability, regardless of role.
    """

    ANTHROPIC = "anthropic"
    OPENAI = "openai"
    LOCAL = "local"
    SCRIPTED = "scripted"

    @property
    def is_cloud(self) -> bool:
        return self in {ProviderKind.ANTHROPIC, ProviderKind.OPENAI}


class PrivilegedParserKind(StrEnum):
    RULES = "rules"
    LOCAL = "local"


class ApiPrincipalConfig(BaseModel):
    """A caller of the *normal* API. Not an operator; cannot approve anything."""

    name: str
    role: str
    token: SecretStr

    @field_validator("role")
    @classmethod
    def _known_role(cls, value: str) -> str:
        if value not in {"operator", "viewer"}:
            raise ValueError(f"unknown API role: {value!r} (expected operator or viewer)")
        return value


class OperatorConfig(BaseModel):
    """A human who may approve privileged actions. Separate credential namespace."""

    operator_id: str
    secret_hash: str


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        # `model_` is a pydantic-protected prefix; we use our own field names.
        protected_namespaces=(),
    )

    # Model selection
    model_provider: ProviderKind = ProviderKind.SCRIPTED
    model_name: str = ""
    project_toolchain_image: str = ""

    anthropic_api_key: SecretStr | None = None
    openai_api_key: SecretStr | None = None

    local_model_base_url: str = "http://127.0.0.1:8080/v1"
    local_model_api_key: SecretStr = SecretStr("not-needed")
    local_model_name: str = "local-model"

    # Privileged intent parsing (never a cloud model)
    privileged_parser: PrivilegedParserKind = PrivilegedParserKind.RULES
    privileged_model_base_url: str = "http://127.0.0.1:8080/v1"
    privileged_model_api_key: SecretStr = SecretStr("not-needed")
    privileged_model_name: str = "local-model"

    # Storage
    database_path: Path = Path("./data/runtime.db")
    privileged_database_path: Path = Path("./data/privileged.db")

    # Filesystem sandbox
    workspace_root: Path = Path("./workspace")
    internet_access_enabled: bool = False

    # Auth
    api_tokens: str = "dev:operator:dev-token"
    privileged_operators: str = ""
    privileged_api_enabled: bool = False
    privileged_approval_ttl_seconds: int = Field(default=900, ge=30, le=86_400)

    # Agent execution limits
    default_max_steps: int = Field(default=6, ge=1, le=50)
    supervisor_max_steps: int = Field(default=4, ge=1, le=50)
    researcher_max_steps: int = Field(default=10, ge=1, le=50)
    coder_max_steps: int = Field(default=16, ge=1, le=50)
    reviewer_max_steps: int = Field(default=10, ge=1, le=50)
    model_max_tokens: int = Field(default=2048, ge=64, le=128_000)
    model_timeout_seconds: float = Field(default=120, gt=0, le=3600)
    model_retry_count: int = Field(default=2, ge=0, le=5)
    model_profiles_path: Path | None = None
    dev_prompt_inspection: bool = False
    project_name: str = "default"
    supervisor_model: str | None = None
    researcher_model: str | None = None
    coder_model: str | None = None
    reviewer_model: str | None = None
    supervisor_provider: ProviderKind | None = None
    researcher_provider: ProviderKind | None = None
    coder_provider: ProviderKind | None = None
    reviewer_provider: ProviderKind | None = None
    supervisor_max_tokens: int | None = Field(default=None, ge=64, le=128_000)
    researcher_max_tokens: int | None = Field(default=None, ge=64, le=128_000)
    coder_max_tokens: int | None = Field(default=None, ge=64, le=128_000)
    reviewer_max_tokens: int | None = Field(default=None, ge=64, le=128_000)

    log_level: str = "INFO"

    # -- derived -----------------------------------------------------------

    @property
    def api_principals(self) -> tuple[ApiPrincipalConfig, ...]:
        principals: list[ApiPrincipalConfig] = []
        for raw in (chunk.strip() for chunk in self.api_tokens.split(",")):
            if not raw:
                continue
            parts = raw.split(":")
            if len(parts) != 3:
                raise RuntimeConfigError("API_TOKENS entries must look like 'name:role:token'")
            name, role, token = (part.strip() for part in parts)
            if not name or not token:
                raise RuntimeConfigError("API_TOKENS entries need a non-empty name and token")
            principals.append(ApiPrincipalConfig(name=name, role=role, token=SecretStr(token)))
        return tuple(principals)

    @property
    def operators(self) -> tuple[OperatorConfig, ...]:
        operators: list[OperatorConfig] = []
        for raw in (chunk.strip() for chunk in self.privileged_operators.split(",")):
            if not raw:
                continue
            operator_id, _, secret_hash = raw.partition(":")
            if not operator_id.strip() or not secret_hash.strip():
                raise RuntimeConfigError(
                    "PRIVILEGED_OPERATORS entries must look like 'operator_id:scrypt_hash'"
                )
            operators.append(
                OperatorConfig(operator_id=operator_id.strip(), secret_hash=secret_hash.strip())
            )
        return tuple(operators)

    def resolved_model_name(self) -> str:
        """The model id to send, falling back to a sane per-provider default."""
        if self.model_name:
            return self.model_name
        return _DEFAULT_MODEL_NAMES[self.model_provider]


# Defaults chosen so `MODEL_PROVIDER=anthropic` alone is a working config.
_DEFAULT_MODEL_NAMES: dict[ProviderKind, str] = {
    ProviderKind.ANTHROPIC: "claude-opus-5",
    ProviderKind.OPENAI: "gpt-4o",
    ProviderKind.LOCAL: "local-model",
    ProviderKind.SCRIPTED: "scripted",
}


def load_settings(**overrides: object) -> Settings:
    """Build settings, surfacing configuration problems as RuntimeConfigError."""
    try:
        return Settings(**overrides)  # type: ignore[arg-type]
    except RuntimeConfigError:
        raise
    except Exception as exc:  # pydantic ValidationError and friends
        raise RuntimeConfigError(f"invalid configuration: {exc}") from exc
