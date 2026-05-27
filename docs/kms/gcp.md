# GCP KMS backend (`--kms gcp-kms`)

Uses `gcloud kms encrypt` / `decrypt` against a Cloud KMS key. Envelopes a
64-byte DEK; ciphertext sits in `state.json`.

## Config

```sh
pgforge provision myapp ... --kms gcp-kms \
  --kms-config key=projects/my-proj/locations/global/keyRings/pgforge/cryptoKeys/main
# or:
export PGFORGE_GCP_KMS_KEY=projects/my-proj/locations/global/keyRings/pgforge/cryptoKeys/main
```

## Required IAM

Operator:
- `cloudkms.cryptoKeyEncrypterDecrypter` on the key

VM (runtime-unlock mode):
- Same role on the same key, attached via a VM service account or
  workload identity

## Notes

- pgforge uses Cloud KMS for **envelope encryption only**. The DEK is a
  64-byte random and the KMS key encrypts it; the KMS key itself never
  leaves the cloud.
- Static mode mirrors the bash baseline. Runtime mode requires a service
  account on the VM with the role above.
