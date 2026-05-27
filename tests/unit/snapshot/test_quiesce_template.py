"""The snapshot cron template renders both with and without quiesce."""

from __future__ import annotations

from pgforge.remote.bootstrap import render_script


def _ctx(**overrides):
    base = dict(
        instance_name="demo",
        provider="hetzner",
        volume_id="42",
        luks_uuid="abc",
        pgforge_version="0.1.0",
        retention="7d",
        credential_path="/root/.pgforge/cred-demo.env",
        container_name="pg-demo",
        quiesce="false",
    )
    base.update(overrides)
    return base


def test_quiesce_off_does_only_checkpoint():
    body = render_script("snapshot_cron.sh.j2", **_ctx(quiesce="false"))
    assert "QUIESCE=\"false\"" in body
    assert "pg_backup_start" in body  # the conditional block is still present
    assert "CHECKPOINT" in body


def test_quiesce_on_emits_both_branches():
    body = render_script("snapshot_cron.sh.j2", **_ctx(quiesce="true"))
    assert "QUIESCE=\"true\"" in body
    assert "pg_backup_start" in body
    assert "pg_backup_stop" in body
