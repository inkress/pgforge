# Contributing to pgforge

Thanks for taking a look. pgforge is small enough that a single PR can meaningfully move it forward — and this doc is the short version of how to do that without surprises.

## TL;DR

1. Fork → branch → PR against `main`.
2. Run `pytest` and `ruff check src tests`. Both must be green.
3. New code that touches an existing abstraction either matches that abstraction or proposes a change to it in the PR description. No leaking provider quirks into the command layer.
4. **No LLM-attribution lines** in commits (no `Co-Authored-By: <bot>`, no "Generated with …" footers). Your real name and email belong on the commit.
5. By contributing, you agree your contributions are licensed under the project's MIT license.

## Setup

```sh
git clone https://github.com/<your-fork>/pgforge
cd pgforge
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
pytest -q
```

`pytest` runs the 63-test unit suite in under a second; everything must pass on any PR.

For real-cloud integration tests (opt-in, costs money):

```sh
export PGFORGE_INTEGRATION=1
export PGFORGE_TEST_HCLOUD_TOKEN=...    # etc., per provider
pytest -m integration
```

These are scaffolded but not yet populated in this repo. PRs adding real test bodies are highly valued — we're explicitly asking for them.

## Coding conventions

- **Python 3.10+**. We use `from __future__ import annotations` everywhere so we can write `str | None` style hints.
- **Type hints are non-optional.** Run `mypy src` before submitting; the bar is "no new errors on touched modules".
- **`ruff check`** is the linter and import sorter. Run it; fix or justify everything.
- **Subprocess goes through `pgforge.providers._shell.run`.** Don't call `subprocess.run` directly from a provider/KMS module. That single chokepoint is where dry-run, retries, version checks, and structured logging live.
- **Remote shell goes in a Jinja template.** Don't build shell strings in Python. `src/pgforge/remote/scripts/*.sh.j2` are the source of truth for anything that runs on the DB server.
- **Errors are typed.** Raise a subclass of `PgforgeError` from `errors.py`. The CLI's top-level handler turns each into a specific exit code; `except Exception` is reserved for genuine bugs.

## Adding a new cloud provider

1. New file `src/pgforge/providers/<name>.py` subclassing `Provider` (from `providers/base.py`). Implement every `@abstractmethod`.
2. Register it in `providers/registry.py`: add to `_LAZY` and `_IMPLEMENTED`.
3. Add the device-path convention test in `tests/unit/providers/test_device_paths.py`.
4. Add a metrics-availability test in `tests/unit/providers/test_metrics_normalization.py`.
5. Write `docs/providers/<name>.md` covering prerequisites, quirks, and the credential model.
6. (Optional but encouraged) add a real-cloud integration test in `tests/integration/` gated by an env var.

## Adding a new KMS backend

1. New file `src/pgforge/kms/<name>.py`. Cloud KMS backends usually extend `EnvelopeKMSBackend` (`kms/_envelope.py`) and only implement `encrypt` / `decrypt`. Local-style backends extend `KMSBackend` directly.
2. Register in `kms/registry.py`.
3. Write a contract test in `tests/unit/kms/` — at minimum the encrypt/decrypt round-trip via a stub.
4. Add `docs/kms/<name>.md` covering config keys, IAM/RBAC requirements, and rotation.

## PR review bar

We optimize for:

- **Small, focused PRs.** One change per PR; one logical commit per PR is ideal.
- **Tests for behavior, not implementation.** If a future refactor would invalidate the test even though behavior is unchanged, the test is too tight.
- **Honest abstractions.** "I had to add a special case in the command layer for provider X" usually means the Provider ABC needs a new method.
- **Clear PR description.** What you changed, why, what could go wrong, how you verified.

## Commit hygiene

- Use your real name and email on every commit.
- **No `Co-Authored-By: <bot>` lines, no "🤖 Generated with …" footers.** Tooling is fine; attribution to humans is required.
- Commit messages: imperative mood, one short subject line, body if useful.
- Avoid `--amend` on commits that have already been pushed to a shared branch.

## Reporting bugs

Open a GitHub issue with:

- `pgforge --version` output
- `pgforge doctor --json` output (redacted as needed)
- The full command you ran (with secrets redacted)
- Stderr + stdout (run with `-vv` for debug logging)
- For provider issues, the relevant CLI's version (`hcloud version`, `aws --version`, etc.)

## Security issues

Don't open a public issue. See [`SECURITY.md`](SECURITY.md).

## Code of conduct

This project follows the [Contributor Covenant 2.1](CODE_OF_CONDUCT.md). Be excellent to each other.
