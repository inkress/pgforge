# AWS KMS backend (`--kms aws-kms`)

Uses `aws kms encrypt` / `aws kms decrypt` against a customer-managed key
(CMK) you create ahead of time. The CMK never leaves AWS; pgforge envelopes
each 64-byte DEK with it and stores the ciphertext in `state.json`.

## Config

```sh
pgforge provision myapp ... --kms aws-kms --kms-config key_id=arn:aws:kms:us-east-1:1234:key/abc
# or use the env var:
export PGFORGE_AWS_KMS_KEY_ID=arn:aws:kms:us-east-1:1234:key/abc
```

A KMS alias (`alias/pgforge`) also works.

## Required IAM permissions

For the operator (running `pgforge provision`):

- `kms:Encrypt` on the CMK
- `kms:GenerateDataKey` (not used directly but recommended for future
  features)

For the server (in runtime-unlock mode):

- `kms:Decrypt` on the CMK

The pgforge envelope embeds an `encryption-context` of
`pgforge-label=<label>` so a leaked ciphertext can't be decrypted against
an unrelated workload's allowed context.

## Static mode (default)

The plaintext DEK is also written to the server. KMS gates *operator-side*
recovery only. See [security-model.md](../security-model.md) for the
threat-model rationale.

## Runtime mode

```sh
pgforge provision myapp ... --kms aws-kms --kms-config key_id=... --unlock-mode runtime
```

The DEK never persists on disk on the server. A systemd unit
(`pgforge-runtime-unlock-<name>.service`) calls `aws kms decrypt` at boot
into a tmpfs keyfile, which crypttab then uses.

## Rotation

To rotate the LUKS DEK:

```sh
pgforge key rotate myapp
```

To rotate the underlying KMS CMK: use the AWS console's "Enable key
rotation" feature. Existing envelope ciphertexts continue to decrypt
against the rotated key.
