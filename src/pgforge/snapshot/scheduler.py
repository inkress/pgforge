"""Install / remove server-side cron entries that drive snapshot schedules."""

from __future__ import annotations

import shlex
from pgforge import __version__
from pgforge.logging import get_logger
from pgforge.remote.bootstrap import render_script
from pgforge.remote.ssh import RemoteHost

log = get_logger(__name__)


def cron_file_path(instance_name: str) -> str:
    return f"/etc/cron.d/pgforge-{instance_name}"


def runner_path(instance_name: str) -> str:
    return f"/usr/local/sbin/pgforge-snapshot-{instance_name}"


def credential_path(instance_name: str) -> str:
    return f"/root/.pgforge/cred-{instance_name}.env"


def install_schedule(
    host: RemoteHost,
    *,
    instance_name: str,
    provider: str,
    volume_id: str,
    luks_uuid: str,
    container_name: str,
    cron_expression: str,
    retention: str,
    credential_content: str | None,
    quiesce: bool = False,
) -> tuple[str, str]:
    """Render the runner script, upload it, write the cron file, optionally
    install the credentials file.

    Returns ``(cron_file_path, runner_path)``.
    """
    cred_path = credential_path(instance_name)
    if credential_content is not None:
        host.upload(credential_content, cred_path, mode=0o400)

    runner = render_script(
        "snapshot_cron.sh.j2",
        instance_name=instance_name,
        provider=provider,
        volume_id=volume_id,
        luks_uuid=luks_uuid,
        pgforge_version=__version__,
        retention=retention,
        credential_path=cred_path,
        container_name=container_name,
        quiesce="true" if quiesce else "false",
    )
    runner_p = runner_path(instance_name)
    host.upload(runner, runner_p, mode=0o700)

    cron_p = cron_file_path(instance_name)
    cron_body = (
        f"# pgforge: instance={instance_name} provider={provider}\n"
        f"SHELL=/bin/bash\n"
        f"PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin\n"
        f"TZ=UTC\n"
        f"{cron_expression} root {shlex.quote(runner_p)}\n"
    )
    host.upload(cron_body, cron_p, mode=0o644)
    log.info("installed cron schedule on %s for instance %s", host.host, instance_name)
    return cron_p, runner_p


def remove_schedule(host: RemoteHost, instance_name: str) -> None:
    """Best-effort tear-down of cron + runner + credential files."""
    for p in (cron_file_path(instance_name), runner_path(instance_name), credential_path(instance_name)):
        host.run(f"rm -f {shlex.quote(p)}", check=False)
