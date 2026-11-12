# Archive Backup/Restore — Separate IAM Execution Identities (Design)

**Status:** DESIGN + runtime wiring. No IAM roles, policies, KMS keys or AWS resources are created or
modified here (those are still provisioned out of band). The **ops-script wiring IS implemented** on
this branch — the three scripts now select their per-role AWS profile via a shared helper (see
§3 "Implementation status"). Demo only. Branch `feature/archive-iam-execution`.

Implements the confirmed decisions: **(2)** backup-write and restore-read run under **separate AWS
principals**; **(3)** a **dedicated customer-managed KMS CMK** for archive backups. Companion to
`ARCHIVE_DEMO_ROLLOUT_CHECKLIST.md` §6a.

---

## 1. Baseline and the AWS calls each script makes

Originally all three scripts obtained AWS credentials **ambiently** — no `--profile`, no
`aws sts assume-role`, no access keys — so they ran as whatever identity the process already had (on
the Demo EC2, the **instance role**). The AWS call each one makes:

| Script | AWS call (verified) | Needs |
|---|---|---|
| `backend/ops/archive_backup.sh` | `aws s3 cp "$f" s3://$BUCKET/$PREFIX/… --sse aws:kms` | **write** + KMS encrypt (multipart ⇒ also KMS **decrypt**, see §2) |
| `backend/ops/archive_restore_verify.sh` | `aws s3 cp --recursive "$BACKUP_PREFIX" "$WORK/dl/"` | **read** + KMS decrypt |
| `backend/ops/archive_monitor.sh` | `aws s3 ls s3://$BUCKET/archive-backups/ --recursive` | **list** (read) |

**Problem:** a single ambient role would hold the union of write+read+KMS-encrypt+KMS-decrypt — the
opposite of the decision. The design below splits them, and the scripts now **select the correct
per-role profile at runtime** (§3 "Implementation status") instead of using the ambient role.

---

## 2. Two principals + exact permissions and resource scopes

Let `BK = arn:aws:s3:::clari5pay-demo-archive-backup`, `PFX = archive-backups/*`,
`CMK = arn:aws:kms:ap-south-1:<acct>:key/<archive-backup-cmk-id>`.

### Principal (i) — `clari5pay-demo-archive-backup-write`  (used ONLY by `archive_backup.sh --confirm`)
```json
{ "Version": "2012-10-17", "Statement": [
  { "Sid": "PutBackupObjects", "Effect": "Allow",
    "Action": ["s3:PutObject", "s3:AbortMultipartUpload"],
    "Resource": "BK/archive-backups/*" },
  { "Sid": "KmsForMultipartSseUpload", "Effect": "Allow",
    "Action": ["kms:GenerateDataKey", "kms:Decrypt", "kms:DescribeKey"],
    "Resource": "CMK" }
]}
```
- `s3:PutObject` authorizes CreateMultipartUpload / UploadPart / CompleteMultipartUpload (the volume
  tar uploads via multipart); `s3:AbortMultipartUpload` cleans up an interrupted upload.
- **`kms:GenerateDataKey` AND `kms:Decrypt` are both required for the multipart SSE-KMS upload.** The
  AWS CLI switches to multipart above its 8 MB `multipart_threshold`, and the archive volume tar is
  far larger, so every real backup is multipart. For SSE-KMS, `kms:GenerateDataKey` produces the data
  key that encrypts each part, and **`kms:Decrypt` is also needed on `UploadPart` /
  `CompleteMultipartUpload`** because Amazon S3 must decrypt the per-part data key to read and
  assemble the encrypted parts before it finalizes the object. A single-part `PutObject` would need
  only `GenerateDataKey`; the multipart path additionally requires `Decrypt`. (AWS: UploadPart API
  reference; "large file + KMS" Knowledge Center article.)
- **No** `s3:GetObject`, `s3:ListBucket`, `s3:DeleteObject`/`DeleteObjectVersion`. Role separation is
  preserved **at the S3 layer**: although this identity now holds `kms:Decrypt` on the CMK, it has no
  way to *fetch* a backup object (no GetObject/ListBucket), so it still **cannot read back or delete**
  what it wrote — `kms:Decrypt` alone decrypts a data key, not an S3 object.

### Principal (ii) — `clari5pay-demo-archive-restore-read`  (used by `archive_restore_verify.sh` + `archive_monitor.sh`)
```json
{ "Version": "2012-10-17", "Statement": [
  { "Sid": "GetBackupObjects", "Effect": "Allow",
    "Action": "s3:GetObject", "Resource": "BK/archive-backups/*" },
  { "Sid": "ListBackupPrefix", "Effect": "Allow",
    "Action": "s3:ListBucket", "Resource": "BK",
    "Condition": { "StringLike": { "s3:prefix": "archive-backups/*" } } },
  { "Sid": "KmsDecryptOnly", "Effect": "Allow",
    "Action": ["kms:Decrypt", "kms:DescribeKey"], "Resource": "CMK" }
]}
```
- `s3:ListBucket` (prefix-conditioned) is required for `cp --recursive` and `ls --recursive`.
- **No** PutObject, AbortMultipartUpload, GenerateDataKey, Delete.

### NOT granted to EITHER principal
`s3:DeleteObject` / `s3:DeleteObjectVersion` (anywhere), any action on the **source** bucket
`clari5pay-demo-storage`, and any `kms:*` beyond the actions named per principal. Backup-write also
has **no** `s3:GetObject`/`s3:ListBucket`; restore-read has **no** `s3:PutObject`/
`s3:AbortMultipartUpload`/`kms:GenerateDataKey`.

### Dedicated CMK key policy (mirrors the split)
- Principal (i) → `kms:GenerateDataKey`, `kms:Decrypt`, `kms:DescribeKey`. `Decrypt` here serves the
  multipart-upload assembly only (see Principal (i) note); the identity still cannot retrieve any S3
  object, so it is not a backup-read path.
- Principal (ii) → `kms:Decrypt`, `kms:DescribeKey`.
- Both KMS grants are scoped **exclusively to the dedicated archive-backup CMK** — no other key.
- Key admins (humans) → key administration only; the CMK is used for **archive backups only**, no
  other workload, so compromise of one job cannot read/write unrelated encrypted data.

---

## 3. Safest execution method — how each script assumes its intended identity

Ranked safest-first. **Recommended: Option A.**

### Option A (recommended) — STS AssumeRole via named CLI profiles, instance role as broker
The Demo **EC2 instance role** holds **no** S3/KMS archive permission — only
`sts:AssumeRole` on the two role ARNs. Each script runs under an `AWS_PROFILE` whose `role_arn` is
its task role and whose `credential_source = Ec2InstanceMetadata`; the CLI fetches short-lived STS
credentials automatically. No long-lived keys anywhere.
```
# ~/.aws/config (no secrets — just role ARNs)
[profile archive-backup-write]
role_arn = arn:aws:iam::<acct>:role/clari5pay-demo-archive-backup-write
credential_source = Ec2InstanceMetadata

[profile archive-restore-read]
role_arn = arn:aws:iam::<acct>:role/clari5pay-demo-archive-restore-read
credential_source = Ec2InstanceMetadata
```
Each role's **trust policy** allows only the instance role to assume it:
```json
{ "Effect": "Allow",
  "Principal": { "AWS": "arn:aws:iam::<acct>:role/<demo-ec2-instance-role>" },
  "Action": "sts:AssumeRole",
  "Condition": { "StringEquals": { "sts:ExternalId": "clari5pay-demo-archive" } } }
```
**Profile selection — IMPLEMENTED on this branch.** A shared helper,
`backend/ops/_archive_aws_profile.sh`, resolves, validates and exports `AWS_PROFILE` for each job.
The profile name is resolved from **two independent per-role environment variables**, each with its
own default — there is deliberately **no single shared variable**, so one override can never collapse
both roles onto one identity:

| Role | Variable | Default profile |
|---|---|---|
| backup-write | `ARCHIVE_BACKUP_AWS_PROFILE` | `archive-backup-write` |
| restore-read | `ARCHIVE_RESTORE_AWS_PROFILE` | `archive-restore-read` |

Each variable independently overrides only its own role (for local/test/non-prod runs) and leaves the
other role untouched. Short-lived creds, strong isolation, auditable via CloudTrail `AssumeRole`
events. Temp creds are never printed.

### Option B — explicit `aws sts assume-role` wrapper
A small wrapper calls `aws sts assume-role --role-arn … --role-session-name …`, parses the JSON, and
exports `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`/`AWS_SESSION_TOKEN` for the single command, then
unsets them. Functionally equivalent to A but moves credential handling into shell (more surface for
a leak — must never log the JSON, must `set +x`). Use only if named profiles are unavailable.

### Option C — separate execution environments / principals
Run the **write** job and the **read** jobs under genuinely different principals: e.g. the backup
job on an instance/cron-user with instance-role (i), and restore/monitor on a different host or a
dedicated IAM user (keys in an isolated profile) with policy (ii). Strongest isolation, highest
operational cost; justified only if a single box must not hold both capabilities even transiently.

**Avoid:** one instance role carrying both policies (defeats the decision); long-lived IAM user
access keys on the Demo box (credential-at-rest risk) unless Option C requires it and they are
stored in a locked-down profile outside the repo.

### Implementation status (branch `feature/archive-iam-execution`)

Option A is implemented at the script layer (no AWS resources created):

- **Shared profile helper implemented** — `backend/ops/_archive_aws_profile.sh` resolves + validates
  the per-role profile and exports `AWS_PROFILE`.
- **Backup uses backup-write** — `archive_backup.sh` selects `ARCHIVE_BACKUP_AWS_PROFILE`
  (default `archive-backup-write`) before its `aws s3 cp` upload.
- **Restore uses restore-read** — `archive_restore_verify.sh` selects `ARCHIVE_RESTORE_AWS_PROFILE`
  (default `archive-restore-read`) before its `aws s3 cp --recursive` download.
- **Monitor uses restore-read** — `archive_monitor.sh` selects `ARCHIVE_RESTORE_AWS_PROFILE` for its
  read-only `aws s3 ls` inspection only.
- **Missing profiles fail closed** — if the named profile is not configured (or the aws CLI is
  absent), the helper returns non-zero and **does not export `AWS_PROFILE`**; backup/restore abort
  before any AWS call and the monitor alerts and skips. There is **no silent fallback to the ambient
  instance role**.
- **No static AWS credentials** — identities are reached only via STS AssumeRole named profiles using
  EC2 instance-metadata credentials; no access keys are stored, read, or exported.
- Verified by `backend/ops/tests/test_archive_aws_profile.sh` (14 cases) + `bash -n` syntax checks.

**Still required out of band (not created here):** the two IAM roles and their policies, the
dedicated CMK and its key policy, the trust policies, and the `~/.aws/config` profiles above. Until
those exist, the scripts correctly refuse to run (fail closed) rather than using the instance role.

---

## 4. Residual risks / open items
- **AWS resources still to be provisioned out of band** — the two roles + policies, the dedicated
  CMK + key policy, trust policies, and the `~/.aws/config` profiles. The script wiring is done and
  fails closed until these exist; separation is only *enforced in AWS* once the policies above are
  attached exactly as written (in particular, backup-write must get `kms:Decrypt` on the CMK or the
  multipart upload will fail — see §2).
- **Optional future improvement — a list-only monitoring role.** `archive_monitor.sh` only runs
  `aws s3 ls` (ListBucket); it needs neither `s3:GetObject` nor `kms:Decrypt`. Today it shares the
  restore-read identity, which is slightly broader than monitoring requires. A tighter future design
  would add a third principal, e.g. `clari5pay-demo-archive-monitor-list`, granting only
  `s3:ListBucket` (prefix-conditioned to `archive-backups/*`) with no GetObject/KMS, and point the
  monitor at an `ARCHIVE_MONITOR_AWS_PROFILE`. **Not implemented — the current monitor behaviour is
  unchanged.**
- **Trust-policy `ExternalId`** and exact account id are placeholders — fill at provisioning.
- **CMK multi-Region / rotation** — rotation on; multi-Region only if backups are replicated
  cross-region (see checklist §1 durability).
- No credentials, key material, or account numbers are included in this document.
