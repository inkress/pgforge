# pgforge security model

This document is intended to be read end-to-end before pgforge ever protects
data you care about. It describes what pgforge does, what it *doesn't* do,
and where the trust boundaries live.

## What pgforge guarantees

* **Encryption at rest.** The cloud volume backing your Postgres data
  directory is LUKS2 (AES-256-XTS, 512-bit key, argon2id PBKDF). A detached
  volume, a stolen disk, a leaked snapshot blob — none of these are readable
  without the LUKS key.
* **Restricted exposure.** The Postgres container binds to `127.0.0.1` on
  the server, not to a public interface. Reach it via SSH tunnel.
* **Per-instance key isolation.** Every instance has its own LUKS key. Loss
  of one key compromises one instance.
* **Atomic key rotation.** `pgforge key rotate` adds a new LUKS keyslot,
  verifies it, then removes the old one. A crash mid-rotation leaves the
  old key valid; a success leaves only the new key valid.

## What pgforge does not guarantee

* **It is not zero-trust against the running server.** Anyone with root on
  the running VM can read both the plaintext data (it's mounted and Postgres
  is reading from it) and, in the default ("static") unlock mode, the LUKS
  keyfile itself. This is the same property the bash baseline has.
* **It does not encrypt Postgres credentials in transit by default.** The
  superuser password is generated at provision time and printed once. The
  Docker container is on `127.0.0.1`, so encryption in transit is your
  SSH tunnel's job.
* **It does not back up its own state file.** `~/.config/pgforge/state.json`
  is operator-local. Lose it without `pgforge state export` first, and
  recovery means hand-reconstructing entries from snapshot labels (see
  `pgforge state rebuild --from-cloud`).

## KMS unlock modes

Two modes — the choice is `--unlock-mode=static` (default) or `--unlock-mode=runtime`.

### static (default, all backends)

```
[ Operator machine ] -- LUKS key bytes (local KMS) --> [ DB server: /root/.pgforge/keys/<name>.key ]
                                                              |
                                                              v
                                                       crypttab unlocks at boot
```

- For the **local** KMS backend: the bytes are generated on the operator
  machine, written to `~/.config/pgforge/keys/<id>.key` (mode 0600), and
  uploaded to the server.
- For **cloud-KMS** backends (`aws-kms` / `gcp-kms` / `azure-kv` / `vault`):
  the bytes are still on the server in `/root/.pgforge/keys/<name>.key` —
  KMS only holds a *ciphertext copy* (in `state.json` under
  `kms.envelope_ciphertext_b64`) so the operator can recover the key if
  their local copy is lost. This is **KMS-as-escrow**, not KMS-as-runtime.

**Threat coverage:** physical-disk theft, detached-volume theft, snapshot
leak, accidental key loss on the operator machine (with cloud KMS).

**Threat *not* covered:** an attacker rooting the running server. They can
read the data and the key. To address that, use runtime mode.

### runtime (cloud-KMS only)

```
[ Operator machine ] -- envelope (encrypted DEK) --> state.json
                                                          |
                                                          v
                                            installed in state at provision time
                                                          |
                                                          v
[ DB server: systemd unit `pgforge-runtime-unlock-<name>.service` ]
   |
   | uses instance role / SP / managed identity / Vault auth
   v
   calls cloud KMS decrypt → writes DEK to /run/pgforge/keys/<name>.key (tmpfs)
   |
   v
   crypttab opens LUKS using tmpfs keyfile
```

The plaintext DEK never persists across reboots on the server's disk. An
attacker who powers off the box and exfiltrates the boot disk gets nothing.
An attacker rooting the *running* box can still read tmpfs — runtime mode is
not a defense against runtime root compromise.

**Threat coverage:** static threats + offline boot-disk theft.

## Snapshot security

Snapshots are byte-for-byte block copies of the encrypted volume. They
inherit the LUKS protection of the source volume: a leaked snapshot blob
needs the LUKS key to decrypt. Snapshots created with `--quiesce` are
SQL-consistent (Postgres WAL is flushed via `pg_backup_start` before the
snapshot), while crash-consistent snapshots rely on WAL replay at restore
time.

## Server-side credentials

The scheduled-snapshot cron lives on the DB server and needs to call the
cloud's snapshot API. Different providers expose this differently:

| Provider | Best path | Fallback |
|---|---|---|
| AWS | EC2 instance role with `ec2:CreateSnapshot` scoped to the volume ARN | IAM user access key on disk |
| GCP | VM service account with `compute.disks.createSnapshot` | SA JSON key on disk |
| Azure | Managed identity with `Microsoft.Compute/snapshots/write` on the RG | SP client secret on disk |
| Hetzner | API token on disk (project-scoped, no narrower) | — |
| DigitalOcean | PAT on disk (account-scoped, no narrower) | — |
| Linode | PAT on disk (no narrower) | — |

pgforge's `mint_snapshot_credential` does its best to mint a *new* per-instance
credential where the cloud allows it (AWS, GCP, Azure), so revocation is
per-instance. For Hetzner / DigitalOcean / Linode the token has whole-account
blast radius — this is loudly documented in the per-provider docs.

## What an attacker would actually do

| Attack | Outcome |
|---|---|
| Steal a cold backup tape / disk | Encrypted, useless. |
| Read a snapshot from the cloud account they breached | Encrypted, useless. |
| Steal the operator's laptop with state.json | They get instance metadata + (with local KMS) the LUKS keys. With cloud KMS they get the *ciphertext* of the LUKS keys, which requires KMS access to decrypt. |
| Compromise a server-side cron credential | They can snapshot the volume — but the snapshot is encrypted. |
| Root the running DB server | Game over for static mode (data + keyfile readable). Runtime mode protects against subsequent power-off + exfiltrate-disk only. |
| Compromise the KMS account itself | Game over (KMS decrypts all envelopes). |

## Rotation expectations

- LUKS keyslots: rotate yearly with `pgforge key rotate <name>` (or after any
  suspected operator-machine compromise).
- KMS CMK (cloud KMS only): rotate via the cloud's KMS rotation feature.
  Envelope ciphertexts in state.json continue to decrypt against an old key
  version until the next rotate.
- Server-side cron credentials: rotate after any server-image change or
  after access-credential exposure. Hetzner/DO/Linode require manual
  rotation in the cloud console.
