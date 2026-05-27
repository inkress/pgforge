# AWS (provider name: `aws`)

## Prerequisites

- `aws` CLI v2 ≥ 2.0, configured (`aws configure` or env vars)
- The target EC2 instance must exist and be Nitro-based (the standard
  modern instance families — m5, c5, r5, t3 and later all qualify)
- The instance's IAM principal needs `ec2:CreateVolume`, `AttachVolume`,
  `DescribeVolumes`, `CreateSnapshot`, `DescribeSnapshots`, `DeleteSnapshot`
  to provision and snapshot.

## Quick start

```sh
export AWS_REGION=us-east-1

pgforge provision myapp \
  --provider aws \
  --server i-0123abcd \
  --size 50 \
  --location us-east-1a \
  --kms aws-kms \
  --kms-config key_id=arn:aws:kms:us-east-1:123456789:key/abcd...
```

`--location` is an **availability zone**, not a region. Volumes live in a
single AZ; pgforge fails fast if you pass a region instead.

## Provider quirks

- **Device path:** Nitro NVMe — `/dev/disk/by-id/nvme-Amazon_Elastic_Block_Store_<volid-without-dash>`.
  pgforge never relies on the legacy `/dev/sdf` hint.
- **Metrics:** full CloudWatch via `aws cloudwatch get-metric-statistics`.
  IOPS and throughput are computed as `Sum / 300s` of the relevant series.
- **Volume type:** pgforge creates `gp3` volumes by default.

## Server-side credentials

pgforge tries to mint a per-instance IAM user with a policy scoped to the
specific volume ARN (`ec2:CreateSnapshot` on the volume only). This requires
operator-side `iam:CreateUser` + `iam:PutUserPolicy` + `iam:CreateAccessKey`.

If those aren't available, pgforge falls back to writing the operator's own
`AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` to the server. The fallback
is loudly logged; prefer the proper way.

**Best practice:** attach an instance role to the EC2 server before
provisioning, then set `AWS_USE_INSTANCE_ROLE=1` (not yet implemented in
the credential mint — Phase 2). That keeps long-lived credentials off the
server entirely.

`pgforge destroy --purge-key` deletes the IAM user pgforge created
(`pgforge-<name>-<suffix>`); the operator-fallback path requires manual
key rotation.

## Runtime-unlock mode

Works with the `aws-kms` backend. Set up an instance role with
`kms:Decrypt` on the CMK ARN, then provision with:

```sh
pgforge provision myapp \
  --provider aws --server i-0123abcd --size 50 --location us-east-1a \
  --kms aws-kms --kms-config key_id=arn:aws:kms:... \
  --unlock-mode runtime
```

At boot, `pgforge-runtime-unlock-<name>.service` runs before `cryptsetup.target`
and uses the instance role to decrypt the envelope into tmpfs.
