# pgforge architecture

A 10-minute read for someone diving into the code.

## Layers

```
                              CLI (Typer)
                                  │
                                  ▼
     ┌────────────────────────────────────────────────────────┐
     │                       commands/                        │
     │   provision  destroy  ls/show  doctor  snapshot.*      │
     │   metrics  capacity  key.*  state.*  ssh/psql          │
     └─────────┬───────────────┬──────────────┬───────────────┘
               │               │              │
               ▼               ▼              ▼
       ┌───────────────┐  ┌────────┐   ┌─────────────┐
       │  providers/   │  │  kms/  │   │   state/    │
       │  base.py ABC  │  │  base  │   │  schema +   │
       │  hetzner.py   │  │  local │   │  store      │
       │  aws.py       │  │  *_kms │   │  (locks)    │
       │  gcp.py       │  │  vault │   └─────────────┘
       │  azure.py     │  └────────┘
       │  digitalocean.py│
       │  linode.py    │       │
       │  mock.py      │       │
       └──────┬────────┘       │
              │                │
              ▼                ▼
       ┌───────────────┐   ┌────────────────┐
       │  _shell.py    │   │  remote/       │
       │  (subprocess) │   │  ssh, scripts/ │
       └───────────────┘   └────────────────┘
                                 │
                                 ▼
                  ┌──────────────────────────────┐
                  │  Jinja-rendered bash on the  │
                  │  DB server (LUKS, Docker,    │
                  │  cron, systemd)              │
                  └──────────────────────────────┘
```

## Three abstractions that earn their keep

1. **`Provider` ABC** (`providers/base.py`). Every cloud action goes through
   one of ~20 methods. Concrete subclasses shell out to the cloud's CLI and
   parse JSON. This is the only file that should know about cloud-specific
   quirks (device paths, attach semantics, metrics availability).

2. **`KMSBackend` ABC** (`kms/base.py`). Generate a key, get a handle.
   Fetch material when needed (e.g. to feed `cryptsetup`). Delete. Rotate.
   The `EnvelopeKMSBackend` base (`kms/_envelope.py`) factors out the
   common shape for cloud-KMS backends (random DEK + cloud encrypt).

3. **`StateStore`** (`state/store.py`). One JSON file, two locks: a
   short-lived `flock` on the file for read-modify-write transactions, and
   a per-instance lockfile held across long operations (provision/destroy/
   snapshot). All writes are atomic via `os.replace` of a temp file.

## The provision flow in detail

```
provision <name>
  └─ resolve provider, server (cloud API)
  └─ add InstanceState{phase=PENDING} to state
  ┌──────────────────── per-phase, check-then-act ────────────────────┐
  │ phase < VOLUME_CREATED  → provider.create_volume()                │
  │ phase < VOLUME_ATTACHED → provider.attach_volume()                │
  │ phase < (KMS key exists) → kms_backend.generate_key()             │
  │ phase < LUKS_FORMATTED  → SSH: luks_format.sh.j2                  │
  │ phase < MOUNTED         → SSH: luks_open.sh.j2 + mkfs + mount    │
  │ phase < CRYPTTAB_WRITTEN→ SSH: crypttab_fstab.sh.j2               │
  │ phase < POSTGRES_RUNNING→ SSH: postgres_docker.sh.j2              │
  │ phase < CRON_INSTALLED  → SSH: install_cloud_cli.sh.j2            │
  │ phase ← READY                                                     │
  └────────────────────────────────────────────────────────────────────┘
```

Re-running `pgforge provision --resume` after a failure picks up at the
right phase. Each step uses an "inspect-then-act" check (e.g. is the device
already a LUKS container? is the container already running?) so the bash
template itself is idempotent too.

## Why we shell out to CLIs

Hard constraint from the design: pgforge never imports a cloud SDK. This
trades off polish (worse JSON parsing, version-drift exposure, more
subprocess overhead) against an enormous reduction in dependencies. The
target user is someone who already runs `aws` / `hcloud` / `gcloud` and
wants pgforge to be additive, not require yet another secret store / auth
flow / SDK upgrade matrix.

`providers/_shell.py` is the single subprocess chokepoint: dry-run, retries
on rate limits, version checks, structured logging. Every provider goes
through it.

## The remote scripts

`remote/scripts/*.sh.j2` are Jinja-rendered at runtime and uploaded to the
target server. Keeping them as templates (rather than building shell strings
in Python) means:

- They're independently readable and reviewable.
- They behave identically whether invoked by pgforge or by an operator
  who SSHes in and runs them by hand.
- Their shell-level idempotency (the `isLuks`/`mountpoint`/`docker ps`
  checks) lives where it can actually inspect server state.

| Template | Purpose |
|---|---|
| `install_deps.sh.j2` | apt-get cryptsetup, install Docker. |
| `install_cloud_cli.sh.j2` | Install the cloud CLI for snapshot cron. |
| `luks_format.sh.j2` | `cryptsetup luksFormat` if not already LUKS. |
| `luks_open.sh.j2` | Open LUKS, mkfs, mount. |
| `crypttab_fstab.sh.j2` | Persist auto-unlock across reboot. |
| `luks_rotate.sh.j2` | Add new keyslot, verify, remove old. |
| `postgres_docker.sh.j2` | docker run / start the Postgres container. |
| `snapshot_cron.sh.j2` | Server-side cron runner: snapshot + optional quiesce. |
| `runtime_unlock_agent.sh.j2` | Boot-time KMS decrypt → tmpfs. |
| `runtime_unlock.service.j2` | systemd unit ordering the agent before crypttab. |
| `uninstall.sh.j2` | Teardown: stop container, close LUKS, strip crypttab/fstab, wipe keys. |

## Where state lives

| Thing | Location |
|---|---|
| State file | `$PGFORGE_HOME/state.json` (default `~/.config/pgforge/state.json`) |
| Local KMS keys | `$PGFORGE_HOME/keys/<id>.key` (mode 0600) |
| Per-instance locks | `$PGFORGE_HOME/locks/<name>.lock` |
| Server-side LUKS keyfile (static mode) | `/root/.pgforge/keys/<instance>.key` (mode 0400) |
| Server-side LUKS keyfile (runtime mode) | `/run/pgforge/keys/<instance>.key` (tmpfs, 0400) |
| Server-side cron | `/etc/cron.d/pgforge-<name>` |
| Server-side snapshot runner | `/usr/local/sbin/pgforge-snapshot-<name>` |
| Server-side runtime-unlock agent | `/usr/local/sbin/pgforge-runtime-unlock-<name>` |
| Server-side credential file | `/root/.pgforge/cred-<name>.env` (mode 0400) |
| Server-side logs | `/var/log/pgforge/<name>.log` |
