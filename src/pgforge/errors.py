"""Typed exception hierarchy for pgforge.

Every failure surfaces as a subclass of :class:`PgforgeError`. The CLI's
top-level handler catches these and turns them into structured exit codes;
``except Exception`` is reserved for genuinely unexpected bugs.
"""

from __future__ import annotations


class PgforgeError(Exception):
    """Base class for every error pgforge raises intentionally."""

    exit_code: int = 1


class ConfigError(PgforgeError):
    """Bad config file, missing env var, malformed flag combination."""

    exit_code = 2


class StateError(PgforgeError):
    """Anything wrong with the local state file."""

    exit_code = 3


class StateLocked(StateError):
    """Per-instance lock is held by another pgforge process."""

    exit_code = 4


class StateNotFound(StateError):
    """Referenced instance does not exist in state."""

    exit_code = 5


class StateConflict(StateError):
    """Tried to create an instance whose name is already taken."""

    exit_code = 6


class ProviderError(PgforgeError):
    """Base for all cloud-provider failures."""

    exit_code = 10


class ProviderCLIMissing(ProviderError):
    """The provider's CLI binary isn't installed (``hcloud``, ``aws`` …)."""

    exit_code = 11


class ProviderCLITooOld(ProviderError):
    """Installed CLI is older than ``min_cli_version``."""

    exit_code = 12


class ProviderAuthError(ProviderError):
    """The provider CLI is installed but unauthenticated."""

    exit_code = 13


class ProviderAPIError(ProviderError):
    """A CLI invocation returned non-zero with no more specific cause."""

    exit_code = 14


class ProviderResourceNotFound(ProviderError):
    """Volume/server/snapshot referenced in state no longer exists in the cloud."""

    exit_code = 15


class ProviderUnsupported(ProviderError):
    """Provider is registered but not yet implemented (Phase 2+ providers)."""

    exit_code = 16


class KMSError(PgforgeError):
    """Base for all KMS backend failures."""

    exit_code = 20


class KMSKeyNotFound(KMSError):
    exit_code = 21


class KMSDecryptionFailed(KMSError):
    exit_code = 22


class SSHError(PgforgeError):
    """Failure reaching the remote DB server."""

    exit_code = 30


class RemoteCommandError(SSHError):
    """A command run over SSH returned non-zero."""

    exit_code = 31

    def __init__(self, command: str, returncode: int, stdout: str, stderr: str):
        self.command = command
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        super().__init__(
            f"remote command failed (exit {returncode}): {command}\n--- stderr ---\n{stderr.strip()}"
        )


class ProvisionError(PgforgeError):
    """Something went wrong during provision orchestration."""

    exit_code = 40


class SnapshotError(PgforgeError):
    exit_code = 50


class UserAbort(PgforgeError):
    """User declined a confirmation prompt."""

    exit_code = 130
