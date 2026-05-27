# pgforge

> One command, encrypted Postgres on any cloud.

[![CI](https://github.com/inkress/pgforge/actions/workflows/ci.yml/badge.svg)](https://github.com/inkress/pgforge/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![Status: alpha](https://img.shields.io/badge/status-alpha-orange.svg)](#status)

`pgforge` provisions a fully **encrypted-at-rest Postgres** on a cloud server in one command — across six clouds, with pluggable key management. Under the hood: cloud volume + LUKS2 (AES-256-XTS) + ext4 + Postgres in Docker, bound to `127.0.0.1`. The block device stores only ciphertext.

```sh
pgforge provision myapp --provider hetzner --server pg-host-1 --size 50 --kms local
```

That's it. Five minutes later you have a running, encrypted Postgres, scheduled snapshots configured, and a clear recovery story.

---

## Table of contents

- [Why pgforge?](#why-pgforge)
- [Status](#status)
- [Install](#install)
- [Quickstart](#quickstart)
- [Supported clouds](#supported-clouds)
- [Key management options](#key-management-options)
- [Command reference](#command-reference)
- [Storage layout](#storage-layout)
- [Security model](#security-model)
- [Architecture](#architecture)
- [Development](#development)
- [Contributing](#contributing)
- [License](#license)

---

## Why pgforge?

Encrypting a Postgres data directory at rest on a cloud volume is a solved problem — but the solution is roughly *10 steps you have to remember every time*: create the volume, attach it, `cryptsetup luksFormat`, `cryptsetup luksOpen`, `mkfs`, mount, write `/etc/crypttab` and `/etc/fstab` so it auto-unlocks on boot, run Postgres on top, bind it to localhost, and figure out a story for keys + backups. If you want to do this across more than one cloud, you do it ten different ways with ten different CLIs.

pgforge does it once, predictably, across all six major clouds:

- **One CLI** — `pgforge provision <name> --provider <cloud>` — replaces the "10 steps" everywhere.
- **Idempotent and resumable** — re-running the same command after a network blip resumes from the last good phase. Each step inspects what's on the server before acting.
- **Pluggable key management** — local keyfile, HashiCorp Vault, AWS KMS, GCP KMS, Azure Key Vault. Add your own backend in <150 LOC.
- **Honest snapshots** — crash-consistent by default; `--quiesce` wraps each snapshot in `pg_backup_start`/`pg_backup_stop` for SQL-consistency. Server-side cron means snapshots keep happening even if your laptop is offline.
- **Honest metrics** — provider-side IOPS/throughput/latency where the cloud exposes them; an explicit `unavailable_reason` where it doesn't. No fake numbers.
- **No SDK lock-in** — pgforge shells out to each cloud's official CLI (`hcloud`, `aws`, `gcloud`, `az`, `doctl`, `linode-cli`). Your existing auth and version pinning come along for the ride.
- **Drop-in for the bash baseline** — the reference bash script that started this project still works; pgforge produces a byte-equivalent server-side layout.

If you've ever wished `docker-compose up postgres` did the encrypted-at-rest part for you, this is that, generalized to any cloud you actually pay for.

## Status

**Alpha.** All six providers and all five KMS backends are implemented and pass a 63-test unit suite. The full lifecycle (provision → snapshot → restore → destroy) works end-to-end against the built-in mock provider. The Hetzner path matches the proven bash baseline byte-for-byte. AWS, GCP, Azure, DigitalOcean, and Linode have been implemented against their official CLIs but have not yet had real-cloud integration runs in this repository — early adopters should expect some sharp edges on those.

We'd love issues, traces, and pull requests. See [Contributing](#contributing).

## Install

```sh
pip install pgforge
# or from a checkout
pip install -e .
```

pgforge needs the official CLI for whichever cloud(s) you target:

| Provider | Required CLI | Sanity check |
|---|---|---|
| Hetzner | `hcloud` (≥ 1.40) | `hcloud context list` |
| AWS | `aws` v2 (≥ 2.0) | `aws sts get-caller-identity` |
| GCP | `gcloud` (≥ 400) | `gcloud auth list` |
| Azure | `az` (≥ 2.50) | `az account show` |
| DigitalOcean | `doctl` (≥ 1.100) | `doctl account get` |
| Linode | `linode-cli` (≥ 5.0) | `linode-cli account view` |

Run `pgforge doctor` once everything's installed to verify versions + auth.

## Quickstart

### 1. Provision

```sh
pgforge provision myapp \
  --provider hetzner \
  --server pg-host-1 \
  --size 50 \
  --kms local
```

The end-of-command summary prints the connection string and the initial superuser password. **Store the password in your secrets manager** — pgforge will not print it again.

### 2. Connect

```sh
# from your laptop, via an SSH tunnel:
ssh -L 5432:127.0.0.1:5432 root@<server-ip>
psql "postgresql://postgres:<password>@127.0.0.1:5432/postgres"
```

Or use the built-in shortcut:

```sh
pgforge psql myapp     # opens psql inside the container over SSH
```

### 3. Snapshot on a schedule

Snapshots run from a cron entry **on the database server itself**, so they keep happening when your laptop is asleep.

```sh
pgforge snapshot schedule myapp \
  --cron "0 3 * * *" \
  --retain "7d,4w,3m"        # 7 daily + 4 weekly + 3 monthly

pgforge snapshot health myapp   # alert-friendly staleness check
```

For SQL-consistent snapshots (wraps each in `pg_backup_start`/`pg_backup_stop`):

```sh
pgforge snapshot schedule myapp --cron "0 3 * * *" --retain "7d,4w,3m" --quiesce
```

### 4. Inspect

```sh
pgforge ls                 # all instances, table view
pgforge show myapp         # full record
pgforge metrics myapp      # provider-side IOPS / throughput / latency
pgforge capacity myapp     # filesystem usage + Postgres DB size
```

### 5. Restore from a snapshot

```sh
pgforge snapshot ls myapp                       # find a snapshot id
pgforge snapshot restore <snap-id> \
  --instance myapp \
  --to myapp-restore \
  --postgres-port 5433
```

The restored instance comes up on the same server (override with `--server`) on a different port, ready to inspect.

### 6. Tear down

```sh
pgforge destroy myapp                            # prompts for confirmation
pgforge destroy myapp --force --keep-snapshots   # keep the backups
```

## Supported clouds

| Cloud | Provider name | Metrics (cloud) | Per-instance scoped creds | Notes |
|---|---|---|---|---|
| Hetzner Cloud | `hetzner` | n/a | project-scoped only | Reference implementation; matches the bash baseline byte-for-byte. |
| AWS EC2 + EBS | `aws` | full CloudWatch | per-volume IAM user/policy | Nitro NVMe device paths. |
| Google Cloud | `gcp` | full Cloud Monitoring | per-instance service account | Disk name == device-name forced for predictable kernel paths. |
| Microsoft Azure | `azure` | full Azure Monitor | per-RG service principal or managed identity | Uses LUN-based device paths. |
| DigitalOcean | `digitalocean` | n/a | account-scoped only | No API for token minting. |
| Linode | `linode` | n/a | account-scoped only | "Snapshots" implemented as `linode-cli volumes clone`. |

Per-provider details and quirks are in [`docs/providers/`](docs/providers).

## Key management options

```sh
--kms local               # 64-byte keyfile on operator machine + server
--kms aws-kms             # envelope encryption with an AWS KMS CMK
--kms gcp-kms             # envelope encryption with a GCP KMS key
--kms azure-kv            # envelope encryption with an Azure Key Vault key
--kms vault               # envelope encryption with HashiCorp Vault (transit engine)
```

Pass backend-specific config via `--kms-config key=value` (repeatable):

```sh
pgforge provision myapp ... \
  --kms aws-kms \
  --kms-config key_id=arn:aws:kms:us-east-1:123:key/abc
```

Two unlock modes:

- `--unlock-mode static` (default) — plaintext key file sits on the server at `/root/.pgforge/keys/<name>.key`. crypttab auto-unlocks at boot. Matches the bash baseline.
- `--unlock-mode runtime` (cloud KMS only) — a boot-time systemd agent calls the cloud KMS to unwrap the envelope into tmpfs. The plaintext key never persists on disk across reboots.

Full discussion in [`docs/security-model.md`](docs/security-model.md) and per-backend docs in [`docs/kms/`](docs/kms).

## Command reference

```
pgforge provision <name> --provider … --server … --size … [options]
pgforge destroy <name> [--keep-snapshots] [--keep-volume] [--purge-key]
pgforge ls / show <name>
pgforge doctor [--provider <name>]

pgforge snapshot create <name> [--quiesce] [--label k=v]
pgforge snapshot ls [<name> | --all]
pgforge snapshot delete <snapshot-id> --instance <name>
pgforge snapshot restore <snapshot-id> --instance <name> --to <new-name>
pgforge snapshot schedule <name> --cron "0 3 * * *" --retain "7d,4w,3m" [--quiesce]
pgforge snapshot schedule <name> --disable
pgforge snapshot health <name> [--grace 60]
pgforge snapshot prune <name> [--retain "..."] [--dry-run]

pgforge metrics <name> [--window 24h]
pgforge capacity <name> | --all

pgforge key ls / rotate <name> / export <name> --to <path>

pgforge state validate / repair --reconcile
pgforge state export --to <path> / import --from <path>
pgforge state rebuild --from-cloud [--provider <name>] [--apply]

pgforge ssh <name>
pgforge psql <name>
```

Every command accepts `--json` for machine-readable output, `--dry-run` to print actions without executing, `--verbose` / `-q`, and `--state-file <path>` to override the state location.

## Storage layout

| What | Where |
|---|---|
| State file | `~/.config/pgforge/state.json` (override: `PGFORGE_STATE_FILE` or `--state-file`) |
| Local KMS keys (operator) | `~/.config/pgforge/keys/<id>.key` (mode `0600`) |
| Per-instance locks | `~/.config/pgforge/locks/<name>.lock` |
| LUKS key on server (static mode) | `/root/.pgforge/keys/<instance>.key` (mode `0400`) |
| LUKS key on server (runtime mode) | `/run/pgforge/keys/<instance>.key` (tmpfs, `0400`) |
| Server-side cron | `/etc/cron.d/pgforge-<name>` |
| Server-side log | `/var/log/pgforge/<name>.log` |
| Server-side runner | `/usr/local/sbin/pgforge-snapshot-<name>` |

## Security model

Read this end-to-end before pgforge protects data you care about: [`docs/security-model.md`](docs/security-model.md). One-paragraph summary:

The encrypted volume stores only ciphertext, so a detached volume or snapshot is unreadable without the LUKS key. With the **local** KMS backend, the key is generated on your machine and uploaded to the server so the volume auto-unlocks at boot — root on the running server can still read the data (the same property the bash baseline has). With the **cloud-KMS** backends, pgforge uses envelope encryption: a random data key sits on the server (same posture as `local`), and its ciphertext sits in `state.json`. KMS gates *operator-side* key recovery, not runtime decryption — this is **KMS-as-escrow**. The `--unlock-mode=runtime` mode moves decryption to boot time via instance roles, so the plaintext key never persists across reboots.

## Architecture

A 10-minute read at [`docs/architecture.md`](docs/architecture.md). In one diagram:

```
                              CLI (Typer)
                                  │
                                  ▼
     ┌────────────────────────────────────────────────────────┐
     │                       commands/                        │
     │   provision  destroy  ls/show  doctor  snapshot.*      │
     │   metrics  capacity  key.*  state.*  ssh/psql          │
     └─────────┬───────────────┬──────────────┬───────────────┘
               │               │              │
               ▼               ▼              ▼
       ┌───────────────┐  ┌────────┐   ┌─────────────┐
       │  providers/   │  │  kms/  │   │   state/    │
       │  base.py ABC  │  │  base  │   │  schema +   │
       │  hetzner.py   │  │  local │   │  store      │
       │  aws.py       │  │  *_kms │   │  (locks)    │
       │  gcp.py       │  │  vault │   └─────────────┘
       │  azure.py     │  └────────┘
       │  digitalocean.py│
       │  linode.py    │       │
       │  mock.py      │       │
       └──────┬────────┘       │
              │                │
              ▼                ▼
       ┌───────────────┐   ┌────────────────┐
       │  _shell.py    │   │  remote/       │
       │  (subprocess) │   │  ssh, scripts/ │
       └───────────────┘   └────────────────┘
```

Concrete providers shell out to the official CLI (`hcloud`, `aws`, …). Concrete KMS backends extend `EnvelopeKMSBackend` for cloud-KMS or `KMSBackend` directly for local. Server-side scripts are Jinja templates rendered at runtime so they're independently auditable.

## Development

```sh
git clone https://github.com/inkress/pgforge
cd pgforge
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
pytest                  # 63 tests, < 1 s
ruff check src tests
mypy src                # optional, type-strict in /src
```

Try the full end-to-end flow against the built-in mock provider — no cloud account needed:

```sh
export PGFORGE_HOME=/tmp/pgforge-demo
export PGFORGE_MOCK_STATE=/tmp/pgforge-demo/mock-world.json

pgforge --dry-run provision demo --provider mock --server srv-1 --size 10 --kms local
pgforge snapshot create demo --quiesce
pgforge snapshot ls demo
pgforge ls
pgforge --dry-run destroy demo --force
```

For real-cloud integration tests (gated, costs money):

```sh
PGFORGE_INTEGRATION=1 pytest -m integration
```

## Contributing

PRs welcome. Especially valuable:

- Real-cloud integration test runs for AWS / GCP / Azure / DigitalOcean / Linode.
- Issues with full error output (`pgforge --verbose --verbose <cmd>` to get debug logging).
- New KMS backends — add to `src/pgforge/kms/` extending `EnvelopeKMSBackend`.
- Provider tweaks where the cloud CLI's JSON shape has drifted.

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for development setup, coding conventions, and the PR review bar.

## License

[MIT](LICENSE). Use it, fork it, ship it.
