# Local KMS backend (`--kms local`)

The default. Generates a 64-byte random key locally, writes it to
`~/.config/pgforge/keys/<label>-<suffix>.key` (mode 0600), and uploads it
to the server as `/root/.pgforge/keys/<name>.key`.

## When to use

- Single-operator setups.
- Local development.
- Anywhere a cloud KMS or Vault would be operational overhead without
  meaningful benefit (e.g. the operator's laptop is already in the same
  trust boundary as the data).

## When **not** to use

- Multi-operator teams. There's no central recovery story: every operator
  who needs to manage an instance needs a copy of `state.json` *and* the
  matching keyfiles.
- Threat models where laptop theft is in scope. The keys are on disk,
  protected only by filesystem permissions and (optionally) FileVault /
  LUKS at the OS level.

## Recovery

If you lose the local keyfile and the server-side copy is still intact
(the typical case after an operator-machine wipe), recover with:

```sh
ssh root@<server> 'cat /root/.pgforge/keys/<name>.key' > recovered.key
# then point the InstanceState's kms.key_id at the recovered path.
```

If both copies are gone, the data is unrecoverable. That is the intended
property of encryption.

`pgforge key export <name> --to <path>` exports the local key to a file you
can store offline; treat that file like a copy of the data itself.
