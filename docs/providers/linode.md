# Linode (provider name: `linode`)

## Prerequisites

- `linode-cli` ≥ 5.0, authenticated (`linode-cli configure`)
- Linode (compute) exists in the chosen region

## Quick start

```sh
export LINODE_CLI_TOKEN=...

pgforge provision myapp \
  --provider linode \
  --server 12345678 \
  --size 50 \
  --location us-east \
  --kms local
```

## Provider quirks

- **Device path:** `/dev/disk/by-id/scsi-0Linode_Volume_<volume-label>`.
- **Snapshots = clones.** Linode's block storage product has no native
  snapshot primitive that pgforge can use; we implement `create_snapshot`
  via `linode-cli volumes clone`. The clone is a new volume in the same
  region with the snapshot's name. `pgforge snapshot ls` filters by name
  prefix (`pgforge-`).

  **Cost note:** clones are billed as full volumes. Tune retention
  aggressively.
- **Metrics:** **not exposed** via `linode-cli`. Returns
  ``unavailable_reason``. Use Linode Longview or in-guest node_exporter.

## Server-side credentials

Linode has no API for minting scoped tokens. pgforge uploads
`LINODE_CLI_TOKEN` (or `PGFORGE_LINODE_SNAPSHOT_TOKEN`) to the server's
credential file. **Account-wide scope.** Document in your runbook.

`pgforge destroy --purge-key` cannot revoke the token. Rotate via the
Cloud Manager → Profile → API Tokens.
