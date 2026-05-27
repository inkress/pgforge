# Azure (provider name: `azure`)

## Prerequisites

- `az` CLI ≥ 2.50, authenticated (`az login`)
- Resource group + VM exist; the VM is Linux

## Quick start

```sh
export AZURE_RESOURCE_GROUP=my-rg
export AZURE_LOCATION=eastus2

pgforge provision myapp \
  --provider azure \
  --server pg-vm-1 \
  --size 50 \
  --location my-rg/eastus2 \
  --kms azure-kv \
  --kms-config vault=pgforge-vault,key=main
```

`--location` is `<resource-group>/<region>` — both are required because
Azure resources live in an RG, not just a region.

## Provider quirks

- **Device path:** `/dev/disk/azure/scsi1/lun<N>` where N is the LUN chosen
  at attach time. pgforge picks the next free LUN on the VM and remembers
  it in state.
- **Metrics:** Azure Monitor via `az monitor metrics list`. Some series
  (latency in particular) are emitted as both `average` and `total` —
  pgforge prefers `average` and falls through to `total`.
- **Volume SKU:** pgforge creates `Premium_LRS` disks. Override by editing
  the provider or extending the CLI (post-Phase-1).

## Server-side credentials

If the VM has a **system-assigned managed identity** with
`Microsoft.Compute/snapshots/write` scoped to the RG, set
`AZURE_USE_MANAGED_IDENTITY=1` before `pgforge snapshot schedule` and
pgforge will skip the credential file entirely.

Otherwise, pgforge creates a service principal scoped to the RG
(`pgforge-<name>-<suffix>`) with the `Disk Snapshot Contributor` role and
writes its `AZURE_CLIENT_ID` / `AZURE_CLIENT_SECRET` / `AZURE_TENANT_ID` /
`AZURE_SUBSCRIPTION_ID` to the server's `/root/.pgforge/cred-<name>.env`.

`pgforge destroy --purge-key` deletes the SP pgforge created.
