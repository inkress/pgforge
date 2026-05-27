# DigitalOcean (provider name: `digitalocean`)

## Prerequisites

- `doctl` ≥ 1.100, authenticated (`doctl auth init`)
- DigitalOcean Personal Access Token (PAT)

## Quick start

```sh
export DIGITALOCEAN_ACCESS_TOKEN=dop_v1_...

pgforge provision myapp \
  --provider digitalocean \
  --server 123456789 \
  --size 50 \
  --location nyc3 \
  --kms local
```

The server id is a numeric droplet id. Names are also accepted.

## Provider quirks

- **Device path:** `/dev/disk/by-id/scsi-0DO_Volume_<volume-name>` — based
  on the volume *name*, not the volume id. DigitalOcean restricts volume
  names to lowercase letters, digits and dashes, ≤64 chars.
- **Metrics:** **not exposed** via `doctl` to a useful degree. pgforge
  returns ``unavailable_reason`` everywhere. Run node_exporter on the
  droplet for in-guest IOPS.
- **Snapshots:** standard `doctl compute snapshot`, tagged with
  `pgforge-instance-<name>` (DO doesn't allow `key=value` tags so we encode
  the relationship in the tag string).

## Server-side credentials

DigitalOcean has **no API for minting per-volume PATs**. The only option is
to upload the operator's PAT (or a separate `PGFORGE_DIGITALOCEAN_SNAPSHOT_TOKEN`)
to the server. **The token has whole-account blast radius.** Document this
in your operations runbook before shipping pgforge against DO.

`pgforge destroy --purge-key` cannot revoke the PAT (no API). Rotate it
manually in the DigitalOcean Control Panel → API → Tokens.
