# Azure Key Vault backend (`--kms azure-kv`)

Uses `az keyvault key encrypt` / `decrypt` against a Key Vault key.

## Config

```sh
pgforge provision myapp ... --kms azure-kv \
  --kms-config vault=pgforge-vault,key=main
# or:
export PGFORGE_AZURE_KV_VAULT=pgforge-vault
export PGFORGE_AZURE_KV_KEY=main
```

Algorithm defaults to `RSA-OAEP-256`. Override with `algorithm=...` in
`--kms-config` if you've configured a different key type.

## Required RBAC

Operator:
- `Key Vault Crypto User` (or finer-grained `Microsoft.KeyVault/vaults/keys/encrypt/action`)

VM (runtime-unlock mode):
- Same role; preferably via system-assigned managed identity.

## Notes

- pgforge envelopes the 64-byte DEK with the configured key. The key
  material in Key Vault is never exported.
- The encryption is JWE-style and Key Vault's encrypt response is
  base64-encoded text — pgforge transports it as bytes through the
  envelope abstraction.
