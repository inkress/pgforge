# Changelog

All notable changes to pgforge are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] — 2026-05-26

Initial public release.

### Added

- **Six cloud providers** that shell out to official CLIs:
  Hetzner (`hcloud`), AWS (`aws`), GCP (`gcloud`), Azure (`az`),
  DigitalOcean (`doctl`), Linode (`linode-cli`).
- **Five KMS backends**: local keyfile, AWS KMS, GCP KMS, Azure Key Vault,
  HashiCorp Vault (transit). Cloud KMS backends use envelope encryption.
- **Provision flow** (`pgforge provision`): creates a cloud volume,
  LUKS2-encrypts it (AES-256-XTS, argon2id), formats, mounts, persists via
  `/etc/crypttab` + `/etc/fstab`, runs `postgres:16` in Docker bound to
  `127.0.0.1`. Phase-driven and idempotent — re-runnable with `--resume`.
- **Two unlock modes**: `static` (matches the bash baseline; key on server)
  and `runtime` (boot-time KMS decrypt into tmpfs, cloud-KMS only).
- **Snapshot subsystem**: ad-hoc `pgforge snapshot create` (with optional
  `--quiesce` for SQL-consistent snapshots via `pg_backup_start/stop`),
  retention-policy pruning (`snapshot prune`), server-side cron scheduling
  (`snapshot schedule`), staleness monitoring (`snapshot health`), and
  full restore (`snapshot restore`).
- **Key rotation** (`pgforge key rotate`) — transactional LUKS keyslot
  add → verify → drop, with KMS handle rotation.
- **Disaster-recovery state rebuild** (`pgforge state rebuild --from-cloud`)
  reconstructs an inventory from snapshot labels when the local state file
  is lost.
- **Capacity + metrics** (`pgforge capacity` / `pgforge metrics`) with
  per-provider "unavailable" reasons rather than silent zero values when
  a cloud doesn't expose IOPS via its CLI.
- **State store** at `~/.config/pgforge/state.json` with `fcntl` locking,
  atomic writes, per-instance lockfiles, and schema migrations.
- **Doctor preflight** (`pgforge doctor`) checks each provider CLI's
  presence, version, and authentication status.
- 63 unit tests covering the state store (including concurrency and
  atomic-write race), retention policy, KMS round-trips, per-provider
  device path conventions, metrics-normalization gaps, and CLI parsing.

### Security

- Threat model documented honestly in [`docs/security-model.md`](docs/security-model.md):
  static-mode KMS is *key-as-escrow*, not runtime decryption. Runtime mode
  upgrades the posture against offline boot-disk theft.

[Unreleased]: https://github.com/inkress/pgforge/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/inkress/pgforge/releases/tag/v0.1.0
