# Hetzner Cloud (provider name: `hetzner`)

## Prerequisites

- `hcloud` CLI ≥ 1.40, on PATH on your operator machine
- `hcloud context create <name>` or `HCLOUD_TOKEN` environment variable
- The target server must exist already (`hcloud server create` it first)

## Quick start

```sh
pgforge provision myapp \
  --provider hetzner \
  --server pg-host-1 \
  --size 50 \
  --kms local
```

`--location` is optional; pgforge infers it from the server's datacenter.

## Provider quirks

- **Device path:** `/dev/disk/by-id/scsi-0HC_Volume_<id>`.
- **Snapshots:** Hetzner exposes volume snapshots via
  `hcloud volume create-snapshot` on accounts where the feature is enabled.
  pgforge surfaces a clear error if the API rejects the call.
- **Metrics:** **not exposed** via `hcloud`. `pgforge metrics` returns
  ``unavailable_reason`` on every series for Hetzner instances. Use
  node_exporter or the Hetzner Cloud console for IOPS visibility.

## Server-side credentials

Hetzner cannot mint per-volume tokens. `pgforge snapshot schedule` will
require either `HCLOUD_TOKEN` (broad) or a separate
`PGFORGE_HETZNER_SNAPSHOT_TOKEN` (still account-wide, but managed
independently from the operator's main token). The chosen token is uploaded
to `/root/.pgforge/cred-<name>.env` on the server, mode 0400.

`pgforge destroy --purge-key` will *not* revoke the token (no API); rotate
it manually in the Hetzner Cloud Console.
