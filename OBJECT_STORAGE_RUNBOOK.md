# Object Storage — Operations Runbook

Operating the platform once uploaded proof/bank images live in S3 instead of the database.

Companion to `S3_IMAGE_MIGRATION.md`, which explains *why* the migration exists and how the code
works. **This document is the one to follow while doing something**, including at 3am.

Status as of 2026-07-19: **Demo migrated and validated. Production NOT migrated** — it still runs
`STORAGE_BACKEND=db` with base64 in the database.

---

## 1. Configuration

Set in the repo-root `.env` on each host, read by the backend container.

| Setting | Default | Meaning |
|---|---|---|
| `STORAGE_BACKEND` | `db` | `db` = uploads stay base64 in the column (historic behaviour). `s3` = uploads go to the bucket, the column keeps `storage://<key>`. |
| `S3_BUCKET` | *(empty)* | Bucket name. **Required** when `STORAGE_BACKEND=s3`. |
| `S3_REGION` | *(empty)* | Bucket region. Falls back to `AWS_REGION`, whose default `eu-north-1` is **wrong for this deployment** — always set this explicitly to `ap-south-1`. |
| `S3_PREFIX` | `uploads` | Key namespace inside the bucket. |
| `S3_URL_TTL` | `900` | Presigned GET lifetime, seconds. |

`STORAGE_BACKEND` defaults to `db`, so deploying the code changes nothing until an operator opts
in. Credentials are **never** configured here — they come from the EC2 instance role.

Demo, for reference:

```
STORAGE_BACKEND=s3
S3_BUCKET=clari5pay-demo-storage
S3_REGION=ap-south-1
```

Changes require the backend container to be recreated:

```bash
cd ~/Clari5Pay_Platform
sudo docker compose -f docker-compose.demo.yml -f docker-compose.https.demo.yml \
  up -d --force-recreate backend
```

(Production uses `-f docker-compose.prod.yml -f docker-compose.https.yml`.)

Verify what the process actually loaded — not what the file says:

```bash
sudo docker exec -w /app -e PYTHONPATH=/app clari5pay_demo_api python -c \
 "from app.core import storage; from app.core.config import settings; \
  print(settings.STORAGE_BACKEND, settings.S3_BUCKET, settings.S3_REGION, storage.is_enabled())"
```

---

## 2. Bucket and IAM

### Bucket

One bucket **per environment** — never shared. Region `ap-south-1`.

* **Block all public access: ON.** These are payment slips, bank details and KYC photos. Access is
  only ever granted through a short-lived presigned URL, minted after the API has already
  authorised the caller.
* **Versioning: enabled** (recovery from an accidental overwrite).
* **Default encryption: SSE-S3.** The application also sends `ServerSideEncryption=AES256`.

### IAM policy

Attach to the EC2 instance role. Substitute the bucket name in both ARNs:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {"Effect": "Allow", "Action": "s3:ListBucket",
     "Resource": "arn:aws:s3:::BUCKET-NAME"},
    {"Effect": "Allow", "Action": ["s3:GetObject", "s3:PutObject"],
     "Resource": "arn:aws:s3:::BUCKET-NAME/uploads/*"}
  ]
}
```

`ListBucket` acts on the bucket; the object actions on the prefix. **No `s3:DeleteObject`** — the
application never deletes, and withholding it means no bug or compromised credential can destroy
an uploaded document. Do not add it "for cleanup"; do lifecycle expiry instead.

Attach the role to the instance (the step most often missed — the policy is inert without it):

```bash
aws ec2 associate-iam-instance-profile --region ap-south-1 \
  --instance-id i-XXXX --iam-instance-profile Name=ROLE-NAME
```

Verify from the box, not the console:

```bash
TOK=$(curl -s -X PUT 'http://169.254.169.254/latest/api/token' \
      -H 'X-aws-ec2-metadata-token-ttl-seconds: 60')
curl -s -H "X-aws-ec2-metadata-token: $TOK" \
  http://169.254.169.254/latest/meta-data/iam/security-credentials/
```

### Preflight

Confirms every permission the app needs, and that delete is denied:

```bash
sudo docker exec -w /app -e PYTHONPATH=/app clari5pay_api python - <<'PY'
import boto3
B='BUCKET-NAME'; K='uploads/_preflight/probe.txt'
c=boto3.client('s3', region_name='ap-south-1',
               endpoint_url='https://s3.ap-south-1.amazonaws.com')
def t(l,f):
    try: f(); print('  PASS', l)
    except Exception as e: print('  FAIL', l, type(e).__name__)
t('head_bucket', lambda: c.head_bucket(Bucket=B))
t('put_object',  lambda: c.put_object(Bucket=B,Key=K,Body=b'x',ServerSideEncryption='AES256'))
t('get_object',  lambda: c.get_object(Bucket=B,Key=K)['Body'].read())
t('list',        lambda: c.list_objects_v2(Bucket=B,Prefix='uploads/'))
try: c.delete_object(Bucket=B,Key=K); print('  WARN delete SUCCEEDED — policy too wide')
except Exception: print('  PASS delete correctly denied')
PY
```

The probe object cannot be deleted by the app (by design). Remove it from the console if you care.

> **Endpoint note.** The client pins `endpoint_url` to the regional host and forces SigV4. Without
> both, boto3 signs for the configured region while addressing the global
> `<bucket>.s3.amazonaws.com` host; S3 checks the signature against the host it received, they
> disagree, and **every presigned URL 403s with `SignatureDoesNotMatch`**. The SDK's own calls
> survive it by following S3's redirect, so uploads look healthy while nothing renders. This was a
> real defect (fixed in `75cbba6f`); if presigned URLs start failing, check this first.

---

## 3. Bucket lifecycle recommendations

Not configured yet. Recommended:

| Rule | Setting | Why |
|---|---|---|
| Expire noncurrent versions | 30 days | Versioning is on for recovery, not archival. Without this, every overwrite is retained forever. Keys are content-addressed, so overwrites are rare — this is cheap insurance. |
| Abort incomplete multipart uploads | 7 days | An interrupted backfill can leave unbilled-but-charged parts. |
| Transition to Infrequent Access | 90 days | Proofs are read at transaction time, then almost never. Optional; only worth it once volume is material. |

**Do not** add an expiry rule on current versions. These are financial records with retention
obligations, and the database will still reference them.

---

## 4. Backup and restore

**The database is no longer self-contained.** This is the single biggest operational change.

Before: `pg_dump` captured everything, images included.
Now: the dump holds only `storage://<key>` references. A database restored without its bucket
gives every proof image as a broken link.

### Backup

Both, as a pair:

```bash
# 1. Database
aws rds create-db-snapshot --region ap-south-1 \
  --db-instance-identifier INSTANCE --db-snapshot-identifier NAME-$(date +%Y%m%d)

# 2. Bucket (only if you need a point-in-time copy; versioning covers overwrites)
aws s3 sync s3://BUCKET/uploads/ s3://BACKUP-BUCKET/uploads/
```

### Restore

Restore the database, then confirm the bucket still holds what it references:

```bash
sudo docker exec -w /app -e PYTHONPATH=/app clari5pay_api python - <<'PY'
import asyncio
from sqlalchemy import select
from app.db.session import AsyncSessionLocal
from app.models.models import TransactionAttachment
from app.core import storage
async def main():
    missing=0; total=0
    async with AsyncSessionLocal() as s:
        for a in (await s.execute(select(TransactionAttachment))).scalars().all():
            total+=1
            if not storage.object_exists(a.object_key): missing+=1; print('MISSING', a.object_key)
    print(f'{total} attachments, {missing} missing objects')
asyncio.run(main())
PY
```

Restoring a database from **before** the migration into an environment configured with
`STORAGE_BACKEND=s3` is safe: those rows hold base64, and the read path serves both formats.

---

## 5. Prod → Demo sync

`sync_production_to_demo.sh` clones the Production **database** into Demo. It does **not** copy S3
objects, and it now checks before running (`check_storage_compat`):

| Production | Behaviour |
|---|---|
| `STORAGE_BACKEND=db` | Proceeds. The dump carries the base64, so the clone is complete. |
| `STORAGE_BACKEND=s3` | **Refuses**, with instructions. |
| Same `S3_BUCKET` on both | **Refuses unconditionally.** No override exists. |

Once Production is migrated, choose deliberately:

1. **Copy objects into Demo's own bucket**, then `--assume-objects-synced`:
   ```bash
   aws s3 sync s3://PROD-BUCKET/uploads/ s3://DEMO-BUCKET/uploads/
   ```
   Those are real customer documents. This puts production PII on the demo site — the same
   exposure the sync already creates for user records, but for images too. Deliberate, or not
   at all.
2. **Accept broken images** — `--assume-objects-synced` with nothing copied. Fine when you only
   need the data, not the files.
3. **Don't sync.** Seed Demo instead: `backend/seed_storage_migration_test.py`.

Never point Demo at Production's bucket. The script refuses, and so should you: any demo login
would then read real payment slips, bank details and KYC photos.

---

## 6. Migrating an environment

Full sequence, with the irreversible step isolated. Demo has been through all of it.

```bash
# 0. Deploy the code with the flag OFF. Nothing changes.
./deploy_safe.sh          # or ./deploy_demo.sh

# 1. Configure and enable
#    (edit .env: STORAGE_BACKEND=s3, S3_BUCKET=…, S3_REGION=ap-south-1)
sudo docker compose $F up -d --force-recreate backend

# 2. Dry run — reports what would move, writes nothing
sudo docker exec -w /app -e PYTHONPATH=/app clari5pay_api \
  python migrate_images_to_storage.py --dry-run

# 3. Upload, keeping the base64 in place (reversible)
sudo docker exec -w /app -e PYTHONPATH=/app clari5pay_api \
  python migrate_images_to_storage.py

# 4. Verify before doing anything irreversible — see §7

# 5. SNAPSHOT, and wait for it to be `available`
aws rds create-db-snapshot --region ap-south-1 \
  --db-instance-identifier INSTANCE --db-snapshot-identifier NAME
aws rds wait db-snapshot-available --region ap-south-1 --db-snapshot-identifier NAME

# 6. Clear the base64 — THE IRREVERSIBLE STEP
sudo docker exec -w /app -e PYTHONPATH=/app clari5pay_api \
  python migrate_images_to_storage.py --clear-source

# 7. Verify again (§7), then reclaim space
#    VACUUM (ANALYZE) is safe any time; VACUUM FULL needs a maintenance window.
```

The backfill is **idempotent and resumable**: keys are the SHA-256 of the bytes, so a re-run finds
the object already present and skips it. Interrupting it is safe — each transaction commits
separately. No column is cleared until its object has been re-read and its digest matched.

---

## 7. Verification

Run after step 3 and again after step 6.

```bash
sudo docker exec -w /app -e PYTHONPATH=/app clari5pay_api python - <<'PY'
import asyncio, hashlib, json, urllib.request
from sqlalchemy import select, text
from sqlalchemy.orm import undefer
from app.db.session import AsyncSessionLocal
from app.models.models import Transaction, TransactionAttachment
from app.api.routes.transactions import _t
from app.core import storage
F=['merchant_proof','merchant_proofs','admin_proof','admin_bank_image']
async def main():
    async with AsyncSessionLocal() as s:
        left=(await s.execute(text(
          "SELECT count(*) FROM transactions WHERE merchant_proof LIKE 'data:%' "
          "OR admin_proof LIKE 'data:%' OR admin_bank_image LIKE 'data:%' "
          "OR merchant_proofs LIKE '%data:%'"))).scalar()
        atts=(await s.execute(select(TransactionAttachment))).scalars().all()
        ok=bad=0
        for k in {a.object_key: a for a in atts}.values():
            try:
                d=urllib.request.urlopen(storage.presigned_url(k.object_key),timeout=30).read()
                ok += hashlib.sha256(d).hexdigest()==k.checksum
            except Exception: bad+=1
        rows=(await s.execute(select(Transaction).options(
              *(undefer(getattr(Transaction,f)) for f in F)))).scalars().all()
        leaks=sum(1 for t in rows for kk in ('merchantProof','merchantProofs','adminProof',
                  'adminBankImage') if _t(t,full=False).get(kk) is not None)
    print(f'rows still base64 : {left}')
    print(f'objects verified  : {ok} ok / {bad} failed')
    print(f'list-payload leaks: {leaks}')
PY
```

Expected after step 6: `0` base64 rows, all objects verified, `0` leaks.

Also confirm `Content-Type` is correct — a wrong value renders as a broken image even though the
bytes are fine:

```bash
aws s3api head-object --bucket BUCKET --key uploads/... --query ContentType
```

---

## 8. Monitoring

| Signal | Where | Meaning |
|---|---|---|
| `StorageError` in API logs | `docker logs <api>` | Upload or presign failed. Uploads return **503** — they never silently fall back to base64. |
| `SignatureDoesNotMatch` | API logs / browser console | Endpoint/region misconfiguration. See §2. |
| 403 on image loads | Browser console | Presigned URL expired (raise `S3_URL_TTL`) or IAM changed. |
| Images blank, API 200 | Browser | Object missing. `resolve_value` returns `None` rather than failing the whole response — the `has*` flag still reports presence, so the gap is visible. |
| Bucket object count | CloudWatch / `s3 ls` | Should track transaction volume. Flat = uploads not landing. |
| `transactions` table size | `pg_total_relation_size` | Should stay small. Growth means base64 is being written again — check the flag. |

---

## 9. Rollback

**Before `--clear-source`** — trivial and instant. The base64 is still in the columns:

```bash
# set STORAGE_BACKEND=db in .env
sudo docker compose $F up -d --force-recreate backend
```

Objects already uploaded are harmless; a later re-run finds and reuses them.

**After `--clear-source`** — restore the RDS snapshot taken in step 5. The objects remain in the
bucket (the app has no delete permission), so a restored database plus the intact bucket is a
consistent working state.

**Partial/failed backfill** — nothing to roll back. Per-transaction commits, and no column is
cleared until its object is verified. Re-run to continue.

---

## 10. Known limitations

* **Cross-field de-duplication doesn't happen.** Keys include the column name, so identical bytes
  in two different columns are stored twice. Intentional — it keeps objects browsable and lets a
  lifecycle or access policy target one class of file. Negligible in practice.
* **The detail endpoint presigns on every request.** No caching. Cheap, but it is a per-request
  signing cost.
* **`agent_transaction` is not migrated.** `slip_image`, `account_proof` and `deposit_proof` have
  the same pattern and are still base64. Deliberate — see `S3_IMAGE_MIGRATION.md`.
* **Demo's seeded corpus compresses unrealistically.** Synthetic fixtures shrink under TOAST, so
  demo's size reduction is not representative. Judge reclamation on production figures.
* **Objects live in one region.** Previously images rode along inside RDS backups; bucket
  durability is now a separate concern.
