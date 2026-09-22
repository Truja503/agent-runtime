"""Error types shared across the runtime.

Every error carries a message that is safe to log and to return to a caller:
no credentials, no raw provider payloads, no Authorization headers.
"""

from __future__ import annotations


class RuntimeConfigError(Exception):
    """Configuration is invalid or incomplete. Raised at startup, not at request time."""


class MissingCredentialError(RuntimeConfigError):
    """A provider was selected but its credential is absent."""


class ModelError(Exception):
    """Base class for model provider failures."""


class ModelUnavailableError(ModelError):
    """The provider could not be reached, or returned a retryable failure."""


class ModelRefusalError(ModelError):
    """The provider declined to answer. Not a transport failure."""


class ModelResponseError(ModelError):
    """The provider answered, but the answer could not be interpreted."""


class ModelOutputLimitError(ModelResponseError):
    """The output budget ended before a usable answer was produced."""


class ToolError(Exception):
    """Base class for tool failures."""


class ToolNotFoundError(ToolError):
    """No tool is registered under that name."""


class ToolArgumentError(ToolError):
    """Arguments failed schema validation before the handler was reached."""


class ToolExecutionError(ToolError):
    """The tool handler ran and failed."""


class PermissionDeniedError(Exception):
    """The policy engine refused the action. Never raised by an agent itself."""


class TaskNotFoundError(Exception):
    """No task with that id."""


class InvalidTaskTransitionError(Exception):
    """The requested status transition is not allowed from the current status."""
