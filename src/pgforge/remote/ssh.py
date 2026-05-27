"""SSH client wrapper.

A thin :class:`RemoteHost` around paramiko. Two operations matter:

* ``run(command)`` — execute a shell command, capture stdout/stderr, raise on
  non-zero. ``sudo`` wrapping is automatic when the SSH user isn't root.
* ``upload(content, remote_path, mode)`` — write a string to a remote file
  via SFTP, then chmod. Idempotent: rewrites the file each time.

The class is a context manager so the SSH connection is released promptly
even if a step in provisioning raises.
"""

from __future__ import annotations

import io
import shlex
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import PurePosixPath

import paramiko

from pgforge.errors import RemoteCommandError, SSHError
from pgforge.logging import get_logger
from pgforge.providers._shell import is_dry_run

log = get_logger(__name__)


class RemoteHost:
    """SSH/SCP wrapper. Lazy connection — call :meth:`connect` or use as ctx mgr."""

    def __init__(
        self,
        host: str,
        *,
        user: str = "root",
        port: int = 22,
        key_filename: str | None = None,
        sudo_password: str | None = None,
        connect_timeout: float = 15.0,
    ):
        self.host = host
        self.user = user
        self.port = port
        self.key_filename = key_filename
        self.sudo_password = sudo_password
        self.connect_timeout = connect_timeout
        self._client: paramiko.SSHClient | None = None
        self._sftp: paramiko.SFTPClient | None = None

    # ---- lifecycle ----

    def connect(self) -> None:
        if self._client is not None:
            return
        if is_dry_run():
            log.info("[dry-run] would connect %s@%s:%d", self.user, self.host, self.port)
            return
        client = paramiko.SSHClient()
        # Accept new host keys, but warn the user. Stricter policy is opt-in.
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(
                hostname=self.host,
                port=self.port,
                username=self.user,
                key_filename=self.key_filename,
                timeout=self.connect_timeout,
                allow_agent=True,
                look_for_keys=True,
            )
        except (paramiko.SSHException, OSError) as e:
            raise SSHError(
                f"could not SSH to {self.user}@{self.host}:{self.port}: {e}"
            ) from e
        self._client = client
        log.debug("connected ssh %s@%s:%d", self.user, self.host, self.port)

    def close(self) -> None:
        if self._sftp is not None:
            try:
                self._sftp.close()
            except Exception:
                pass
            self._sftp = None
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None

    def __enter__(self) -> "RemoteHost":
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # ---- exec ----

    def run(
        self,
        command: str,
        *,
        check: bool = True,
        input: str | None = None,
        timeout: float = 600.0,
        log_command: bool = True,
        sudo: bool | None = None,
    ) -> tuple[int, str, str]:
        """Execute ``command`` on the remote host. Returns ``(returncode, stdout, stderr)``."""
        wrapped = self._maybe_sudo(command, sudo)
        if log_command:
            log.debug("remote$ %s", wrapped)
        if is_dry_run():
            log.info("[dry-run] would run remote: %s", wrapped)
            return 0, "", ""
        self.connect()
        assert self._client is not None
        try:
            stdin, stdout, stderr = self._client.exec_command(wrapped, timeout=timeout)
            if self.sudo_password and (sudo is True or (sudo is None and self.user != "root")):
                # paramiko's exec_command runs each command in its own shell, so
                # we just feed the password on stdin if sudo asked for it.
                stdin.write(self.sudo_password + "\n")
                stdin.flush()
            if input is not None:
                stdin.write(input)
                stdin.flush()
            stdin.channel.shutdown_write()
            out = stdout.read().decode("utf-8", errors="replace")
            err = stderr.read().decode("utf-8", errors="replace")
            rc = stdout.channel.recv_exit_status()
        except paramiko.SSHException as e:
            raise SSHError(f"ssh exec failed: {e}") from e
        if check and rc != 0:
            raise RemoteCommandError(command=wrapped, returncode=rc, stdout=out, stderr=err)
        return rc, out, err

    def upload(self, content: str | bytes, remote_path: str, *, mode: int = 0o644) -> None:
        """Write ``content`` to ``remote_path`` via SFTP, then chmod."""
        if isinstance(content, str):
            data = content.encode("utf-8")
        else:
            data = content
        if is_dry_run():
            log.info("[dry-run] would upload %d bytes to %s (mode %o)", len(data), remote_path, mode)
            return
        sftp = self._get_sftp()
        # Ensure parent dir exists.
        parent = str(PurePosixPath(remote_path).parent)
        if parent and parent != "/":
            self.run(f"mkdir -p {shlex.quote(parent)}", check=True, log_command=False)
        with sftp.file(remote_path, "wb") as fh:
            fh.write(data)
        sftp.chmod(remote_path, mode)
        log.debug("uploaded %s (%d bytes, mode %o)", remote_path, len(data), mode)

    def exists(self, remote_path: str) -> bool:
        rc, _, _ = self.run(f"test -e {shlex.quote(remote_path)}", check=False)
        return rc == 0

    # ---- internals ----

    def _maybe_sudo(self, command: str, sudo: bool | None) -> str:
        if sudo is False:
            return command
        if sudo is True or self.user != "root":
            return f"sudo -n -- bash -c {shlex.quote(command)}"
        return command

    def _get_sftp(self) -> paramiko.SFTPClient:
        self.connect()
        if self._sftp is None:
            assert self._client is not None
            self._sftp = self._client.open_sftp()
        return self._sftp


@contextmanager
def connect(
    host: str,
    *,
    user: str = "root",
    port: int = 22,
    key_filename: str | None = None,
) -> Iterator[RemoteHost]:
    """Convenience context manager for short-lived connections."""
    rh = RemoteHost(host=host, user=user, port=port, key_filename=key_filename)
    try:
        rh.connect()
        yield rh
    finally:
        rh.close()


def quote_host(host: str) -> str:
    """Wrap IPv6 addresses in brackets for ``ssh user@[host]:port`` style use."""
    return f"[{host}]" if ":" in host else host


def stdin_redirect(content: str) -> io.StringIO:
    """Stub: build an in-memory buffer for ``run(input=...)`` callers."""
    return io.StringIO(content)
