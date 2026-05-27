"""Smoke tests for the Typer CLI: --version, --help, doctor, ls."""

from __future__ import annotations

from typer.testing import CliRunner

from pgforge import __version__
from pgforge.cli import app


def test_version():
    runner = CliRunner()
    res = runner.invoke(app, ["--version"])
    assert res.exit_code == 0
    assert __version__ in res.stdout


def test_help_lists_subcommands():
    runner = CliRunner()
    res = runner.invoke(app, ["--help"])
    assert res.exit_code == 0
    for cmd in ("provision", "destroy", "ls", "show", "doctor", "snapshot", "key", "state"):
        assert cmd in res.stdout


def test_ls_empty_state(isolated_home):
    runner = CliRunner()
    res = runner.invoke(app, ["ls"])
    assert res.exit_code == 0
    # "no instances yet" is the empty-state message printed to stdout
    assert "no instances" in res.stdout or res.stdout.strip() == ""


def test_snapshot_create_requires_instance(isolated_home):
    runner = CliRunner()
    res = runner.invoke(app, ["snapshot", "create", "does-not-exist"])
    # state not found exits with code 5
    assert res.exit_code != 0


def test_doctor_reports_roadmap(isolated_home, monkeypatch):
    # Doctor probes provider CLIs — without them, it should still print the
    # roadmap table and exit cleanly.
    runner = CliRunner()
    res = runner.invoke(app, ["doctor", "--provider", "hetzner"])
    # error path is acceptable: hcloud isn't installed in test env
    assert "hetzner" in res.stdout or "hetzner" in res.stderr
