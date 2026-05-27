# Google Cloud (provider name: `gcp`)

## Prerequisites

- `gcloud` ≥ 400.0, authenticated (`gcloud auth login`)
- `CLOUDSDK_CORE_PROJECT` (or `GOOGLE_CLOUD_PROJECT`) set, or pass via
  the constructor (currently exposed only via `--kms-config` for `gcp-kms`)
- Compute Engine VM exists in the chosen zone

## Quick start

```sh
export CLOUDSDK_CORE_PROJECT=my-proj
export CLOUDSDK_COMPUTE_ZONE=us-central1-a

pgforge provision myapp \
  --provider gcp \
  --server pg-vm-1 \
  --size 50 \
  --location us-central1-a \
  --kms gcp-kms \
  --kms-config key=projects/my-proj/locations/global/keyRings/pgforge/cryptoKeys/main
```

## Provider quirks

- **Device path:** `/dev/disk/by-id/google-<device-name>`. pgforge forces
  `--device-name == --disk` at attach time so the kernel path is
  predictable.
- **Snapshots:** standard `gcloud compute snapshots` resources, labelled
  with `pgforge-instance` and `pgforge-luks-uuid`.
- **Metrics:** Cloud Monitoring via `gcloud monitoring time-series list`.

## Server-side credentials

pgforge tries to create a per-instance service account (`pgf-<name>-<suffix>`)
and bind `roles/compute.storageAdmin` on the *disk* (not project-wide). A
JSON key for the SA is uploaded to `/root/.pgforge/gcp-sa-<name>.json`.

If SA management isn't available, the cron runner falls back to whatever
gcloud is already authenticated as on the VM (typically a VM-attached SA).

**Best practice:** attach a snapshot-scoped service account to the VM at
creation time. Set `GOOGLE_APPLICATION_CREDENTIALS` to point at the VM's
own SA JSON key file (or rely on the metadata server) — pgforge will
not overwrite an existing valid setup.

`pgforge destroy --purge-key` deletes the SA pgforge created.
