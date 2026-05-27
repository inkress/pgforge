# HashiCorp Vault backend (`--kms vault`)

Uses Vault's **transit secrets engine** for envelope encryption.

## Config

```sh
export VAULT_ADDR=https://vault.internal:8200
export VAULT_TOKEN=hvs....
# or use another configured auth method (approle, k8s, cloud auth, ...)

pgforge provision myapp ... --kms vault \
  --kms-config mount=transit,key=pgforge
```

`mount` defaults to `transit`; `key` defaults to `pgforge`. The transit key
must already exist (`vault write -f transit/keys/pgforge`).

## Why transit (and not kv-v2)?

The transit engine encrypts the DEK with a key Vault holds; the key never
leaves Vault. kv-v2 would require pgforge to *read* the key out of Vault,
which is less safe.

## Runtime-unlock mode

Works the same way: the VM authenticates to Vault at boot (typically via
AppRole or a cloud auth method) and `vault write transit/decrypt/pgforge`
unwraps the DEK. The `runtime_unlock_agent.sh.j2` template ships the
required scaffolding; you supply the Vault auth.

## Notes

- pgforge stores the Vault `vault:v1:...` ciphertext string verbatim as
  the envelope. It is portable across Vault upgrades and key rotations.
- Vault transit key rotation: `vault write -f transit/keys/pgforge/rotate`.
  Old ciphertexts continue to decrypt against the rotated key.
