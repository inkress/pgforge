"""Single chokepoint for every subprocess invocation pgforge makes locally.

Every provider's CLI call goes through :func:`run` so we have one place to:

* enforce dry-run mode
* attach structured logging
* parse JSON output safely
* retry on rate-limits (HTTP 429-ish) with exponential backoff
* version-pin each CLI

Concrete providers stay declarative: they build an argv list and call
``run(argv, parse_json=True)``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from pgforge.errors import (
    ProviderAPIError,
    ProviderAuthError,
    ProviderCLIMissing,
    ProviderCLITooOld,
)
from pgforge.logging import get_logger

log = get_logger(__name__)

# Module-level toggle. Set by cli.py when --dry-run is passed.
_DRY_RUN = False


def set_dry_run(value: bool) -> None:
    global _DRY_RUN
    _DRY_RUN = value


def is_dry_run() -> bool:
    return _DRY_RUN


@dataclass
class CommandResult:
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str
    duration_ms: int
    parsed: Any = None
    """JSON-decoded stdout if ``parse_json=True``."""

    extra: dict[str, Any] = field(default_factory=dict)


# Rate-limit patterns that warrant retry (per provider).
_RATE_LIMIT_PATTERNS = re.compile(
    r"(rate limit|too many requests|throttl|429)", re.IGNORECASE
)

# Authentication-failure patterns (mapped to ProviderAuthError).
_AUTH_FAIL_PATTERNS = re.compile(
    r"(unauthorized|authentication failed|not logged in|credentials .* not found|"
    r"please run .*(login|auth|configure)|InvalidClientTokenId|AuthorizationFailed)",
    re.IGNORECASE,
)


def require_binary(name: str) -> str:
    """Resolve a CLI binary on $PATH, raising :class:`ProviderCLIMissing` if absent."""
    found = shutil.which(name)
    if not found:
        raise ProviderCLIMissing(
            f"required CLI {name!r} not found on PATH. Install it and try again."
        )
    return found


def check_min_version(actual: str, minimum: str, *, name: str) -> None:
    """Best-effort version comparison.

    Treats versions as tuples of integers (``X.Y.Z``). Falls through silently
    on non-numeric versions rather than blocking the user — we'd rather have a
    false negative than block on weird upstream version strings.
    """
    actual_t = _parse_version(actual)
    minimum_t = _parse_version(minimum)
    if actual_t is None or minimum_t is None:
        log.debug("could not compare %s versions %r vs %r", name, actual, minimum)
        return
    if actual_t < minimum_t:
        raise ProviderCLITooOld(
            f"{name} {actual} is older than required minimum {minimum}. Please upgrade."
        )


def _parse_version(s: str) -> tuple[int, ...] | None:
    m = re.search(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?", s or "")
    if not m:
        return None
    return tuple(int(x) if x else 0 for x in m.groups())


def run(
    argv: Iterable[str],
    *,
    parse_json: bool = False,
    check: bool = True,
    input: str | None = None,
    env: dict[str, str] | None = None,
    timeout: float = 120.0,
    retries: int = 3,
    backoff: float = 1.5,
    dry_run_result: Any = None,
    sensitive: bool = False,
) -> CommandResult:
    """Run a local command.

    Parameters
    ----------
    argv:
        Command and arguments. First element must exist on PATH (callers
        typically pass ``require_binary("hcloud")`` as the first arg).
    parse_json:
        If True, parse stdout as JSON into :attr:`CommandResult.parsed`.
    check:
        Raise :class:`ProviderAPIError` on non-zero exit (after retries).
    retries:
        Maximum attempts when stderr matches a rate-limit pattern.
    dry_run_result:
        Value to use for ``CommandResult.parsed`` in dry-run mode.
    sensitive:
        If True, omit argv and stdout from log output.
    """
    argv_list = [str(x) for x in argv]
    display = "*****" if sensitive else " ".join(argv_list)
    log.debug("$ %s", display)

    if _DRY_RUN:
        log.info("[dry-run] would run: %s", display)
        return CommandResult(
            argv=argv_list,
            returncode=0,
            stdout="",
            stderr="",
            duration_ms=0,
            parsed=dry_run_result,
        )

    full_env = {**os.environ, **(env or {})}
    last_result: CommandResult | None = None

    for attempt in range(1, retries + 1):
        t0 = time.monotonic()
        try:
            cp = subprocess.run(
                argv_list,
                input=input,
                capture_output=True,
                text=True,
                env=full_env,
                timeout=timeout,
            )
        except FileNotFoundError as e:
            raise ProviderCLIMissing(
                f"binary not found: {argv_list[0]!r}. Install it and try again."
            ) from e

        duration_ms = int((time.monotonic() - t0) * 1000)
        result = CommandResult(
            argv=argv_list,
            returncode=cp.returncode,
            stdout=cp.stdout,
            stderr=cp.stderr,
            duration_ms=duration_ms,
        )
        last_result = result

        if cp.returncode == 0:
            if parse_json and cp.stdout.strip():
                try:
                    result.parsed = json.loads(cp.stdout)
                except json.JSONDecodeError as e:
                    raise ProviderAPIError(
                        f"{argv_list[0]} returned non-JSON output: {e}\nstdout: {cp.stdout[:500]}"
                    ) from e
            log.debug("ok (%dms)", duration_ms)
            return result

        # Non-zero. Decide retry vs. raise.
        if _AUTH_FAIL_PATTERNS.search(cp.stderr):
            raise ProviderAuthError(
                f"{argv_list[0]} is not authenticated. "
                f"Run its login command and try again.\nstderr: {cp.stderr.strip()}"
            )
        if _RATE_LIMIT_PATTERNS.search(cp.stderr) and attempt < retries:
            sleep_for = backoff ** attempt
            log.warning(
                "rate limit on %s (attempt %d/%d), sleeping %.1fs",
                argv_list[0], attempt, retries, sleep_for,
            )
            time.sleep(sleep_for)
            continue
        break

    assert last_result is not None
    if check:
        raise ProviderAPIError(
            f"{argv_list[0]} failed (exit {last_result.returncode}): "
            f"{last_result.stderr.strip()[:1000]}"
        )
    return last_result
