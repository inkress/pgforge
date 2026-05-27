"""The runtime-unlock agent template renders for every supported provider."""

from __future__ import annotations

import pytest

from pgforge.remote.bootstrap import render_script


@pytest.mark.parametrize("provider", ["aws", "gcp", "azure", "vault"])
def test_agent_renders(provider):
    body = render_script(
        "runtime_unlock_agent.sh.j2",
        instance_name="demo",
        provider=provider,
        envelope_b64="dummy==",
        kms_key_ref="ref",
        target_keyfile="/run/pgforge/keys/demo.key",
    )
    assert "PROVIDER=" in body and provider in body
    assert "/run/pgforge/keys/demo.key" in body


def test_unit_file_renders():
    body = render_script("runtime_unlock.service.j2", instance_name="demo")
    assert "pgforge-runtime-unlock-demo" in body
    assert "Before=cryptsetup-pre.target" in body
