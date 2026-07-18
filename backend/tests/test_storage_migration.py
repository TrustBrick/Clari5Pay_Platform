"""Tests for the object-storage migration of transaction proof images.

Covers the eight scenarios the migration has to get right:

  1. database mode (the default)      5. missing objects
  2. object-storage mode              6. legacy base64 records
  3. mixed migration mode             7. migrated records
  4. failed uploads                   8. idempotent backfill

S3 is faked with an in-memory dict standing in for the bucket, so the suite needs no AWS
credentials, no network and no bucket — which is the point: none of this logic should depend on
infrastructure to be verifiable.

Run inside the backend container:

    docker exec -w /app -e PYTHONPATH=/app clari5pay_api python -m pytest tests/test_storage_migration.py -v
"""
from __future__ import annotations

import base64
import hashlib
import json

import pytest

from app.core import storage

# A tiny but real 1x1 PNG — decodes cleanly, so nothing here relies on a malformed fixture.
PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c6360000002000100ffff030000060005577bfabd4"
    "00000000049454e44ae426082"
)
PNG_B64 = base64.b64encode(PNG_BYTES).decode()
PNG_DATA_URL = f"data:image/png;base64,{PNG_B64}"


class FakeS3:
    """Minimal stand-in for the boto3 S3 client, recording calls so tests can assert on them."""

    def __init__(self, *, fail_put: bool = False, fail_get: bool = False):
        self.objects: dict[str, bytes] = {}
        self.fail_put = fail_put
        self.fail_get = fail_get
        self.put_calls = 0
        self.sign_calls = 0

    def head_object(self, Bucket: str, Key: str):
        if Key not in self.objects:
            raise RuntimeError("404 NoSuchKey")
        return {"ContentLength": len(self.objects[Key])}

    def put_object(self, Bucket: str, Key: str, Body: bytes, **kw):
        if self.fail_put:
            raise RuntimeError("simulated S3 outage")
        self.put_calls += 1
        self.objects[Key] = Body
        return {}

    def get_object(self, Bucket: str, Key: str):
        if self.fail_get or Key not in self.objects:
            raise RuntimeError("404 NoSuchKey")
        return {"Body": _Body(self.objects[Key])}

    def generate_presigned_url(self, op: str, Params: dict, ExpiresIn: int):
        key = Params["Key"]
        if key not in self.objects:
            # Real S3 signs blindly, but signing a key we know is absent lets the "missing
            # object" test exercise the failure path deterministically.
            raise RuntimeError("no such key")
        self.sign_calls += 1
        return f"https://fake-bucket.s3.amazonaws.com/{key}?sig=abc&expires={ExpiresIn}"


class _Body:
    def __init__(self, data: bytes):
        self._data = data

    def read(self) -> bytes:
        return self._data


@pytest.fixture
def s3(monkeypatch):
    """Enable the s3 backend against a fake client."""
    fake = FakeS3()
    monkeypatch.setattr(storage.settings, "STORAGE_BACKEND", "s3", raising=False)
    monkeypatch.setattr(storage.settings, "S3_BUCKET", "test-bucket", raising=False)
    monkeypatch.setattr(storage.settings, "S3_PREFIX", "uploads", raising=False)
    monkeypatch.setattr(storage.settings, "S3_URL_TTL", 900, raising=False)
    # Drop any cached real client BEFORE swapping in the fake; monkeypatch restores the original
    # attribute on teardown, so clearing again afterwards would hit the fake and fail.
    storage._client.cache_clear()
    monkeypatch.setattr(storage, "_client", lambda: fake)
    yield fake


@pytest.fixture
def db_mode(monkeypatch):
    """The default: object storage disabled."""
    monkeypatch.setattr(storage.settings, "STORAGE_BACKEND", "db", raising=False)


# ── 1. Database mode — the default must change nothing ────────────────────────────────────

def test_db_mode_is_default_and_passes_values_through(db_mode):
    assert storage.is_enabled() is False
    stored, decoded = storage.store_value(PNG_DATA_URL, field="admin_proof")
    assert stored == PNG_DATA_URL, "data URL must be stored verbatim when the flag is off"
    assert decoded is None
    # And reading it back is equally untouched.
    assert storage.resolve_value(PNG_DATA_URL) == PNG_DATA_URL


def test_db_mode_never_calls_storage(db_mode):
    for fn in (lambda: storage.put("k", storage.decode_data_url(PNG_DATA_URL)),
               lambda: storage.presigned_url("k"),
               lambda: storage.get_bytes("k")):
        with pytest.raises(storage.StorageError):
            fn()


# ── 2. Object-storage mode ────────────────────────────────────────────────────────────────

def test_s3_mode_uploads_and_returns_reference(s3):
    stored, decoded = storage.store_value(PNG_DATA_URL, field="admin_proof")
    assert storage.is_ref(stored)
    assert stored.startswith("storage://uploads/admin_proof/")
    assert decoded.size == len(PNG_BYTES)
    assert s3.objects[storage.ref_to_key(stored)] == PNG_BYTES, "exact bytes must be stored"


def test_reference_is_provider_neutral(s3):
    """The stored value must not name a provider — swapping backends must not require a data
    rewrite, so `s3://` (or any vendor scheme) must never reach the column."""
    stored, _ = storage.store_value(PNG_DATA_URL, field="admin_proof")
    assert stored.startswith("storage://")
    assert not stored.startswith("s3://")
    for vendor in ("s3:", "azure:", "gs:", "minio:", "amazonaws"):
        assert vendor not in stored, f"provider detail {vendor!r} leaked into the reference"


def test_legacy_vendor_prefix_still_resolves(s3):
    """Tolerated on read so nothing written during experimentation becomes unreadable — but it
    is never produced."""
    _, decoded = storage.store_value(PNG_DATA_URL, field="admin_proof")
    key = storage.build_key(field="admin_proof", upload=decoded)
    assert storage.is_ref(f"s3://{key}") is True
    assert storage.ref_to_key(f"s3://{key}") == key
    assert storage.key_to_ref(key).startswith("storage://"), "writes must be canonical"


def test_no_base64_reaches_the_column_in_s3_mode(s3):
    stored, _ = storage.store_value(PNG_DATA_URL, field="admin_bank_image")
    assert "base64" not in stored and PNG_B64 not in stored
    assert len(stored) < 200, "the column must hold a short reference, not content"


def test_resolve_returns_presigned_url(s3):
    stored, _ = storage.store_value(PNG_DATA_URL, field="admin_proof")
    url = storage.resolve_value(stored)
    assert url.startswith("https://") and "sig=" in url
    assert s3.sign_calls == 1


def test_client_pins_the_regional_endpoint_and_sigv4(monkeypatch):
    """Regression: presigned URLs must address the bucket's REGIONAL host.

    boto3 will happily sign for the configured region while addressing the global
    ``<bucket>.s3.amazonaws.com`` host. S3 validates the signature against the host it was sent
    to, so the two disagree and EVERY presigned URL fails with SignatureDoesNotMatch. The SDK's
    own calls survive it by following S3's redirect internally, so uploads look fine and only the
    browser-facing read path breaks — which is why this reached a real bucket before being seen.
    """
    captured = {}

    def fake_boto_client(service, **kw):
        captured.update(service=service, **kw)
        return FakeS3()

    monkeypatch.setattr(storage.settings, "STORAGE_BACKEND", "s3", raising=False)
    monkeypatch.setattr(storage.settings, "S3_BUCKET", "test-bucket", raising=False)
    monkeypatch.setattr(storage.settings, "S3_REGION", "ap-south-1", raising=False)
    storage._client.cache_clear()

    import boto3
    monkeypatch.setattr(boto3, "client", fake_boto_client)
    storage._client()

    assert captured["endpoint_url"] == "https://s3.ap-south-1.amazonaws.com", \
        "endpoint must be the regional host, not the global one"
    assert captured["region_name"] == "ap-south-1"
    assert captured["config"].signature_version == "s3v4"
    storage._client.cache_clear()


def test_objects_are_private_by_default(s3):
    """No ACL is ever set — access is only ever granted by a presigned URL."""
    storage.store_value(PNG_DATA_URL, field="admin_proof")
    # FakeS3.put_object would have recorded an ACL kwarg had one been passed.
    assert not hasattr(s3, "last_acl")


# ── 3. Mixed migration mode — legacy and migrated rows side by side ───────────────────────

def test_mixed_values_resolve_independently(s3):
    migrated, _ = storage.store_value(PNG_DATA_URL, field="merchant_proofs")
    legacy = PNG_DATA_URL
    assert storage.resolve_value(migrated).startswith("https://")
    assert storage.resolve_value(legacy) == legacy, "legacy rows must keep working untouched"


def test_http_urls_are_left_alone(s3):
    url = "https://cdn.example.com/x.png"
    stored, decoded = storage.store_value(url, field="admin_proof")
    assert stored == url and decoded is None
    assert storage.resolve_value(url) == url


def test_none_and_empty_are_safe(s3):
    for value in (None, ""):
        assert storage.store_value(value, field="admin_proof") == (value, None)
        assert storage.resolve_value(value) == value


# ── 4. Failed uploads — must raise, never silently fall back ─────────────────────────────

def test_upload_failure_raises_and_writes_nothing(monkeypatch):
    fake = FakeS3(fail_put=True)
    monkeypatch.setattr(storage.settings, "STORAGE_BACKEND", "s3", raising=False)
    monkeypatch.setattr(storage.settings, "S3_BUCKET", "test-bucket", raising=False)
    storage._client.cache_clear()
    monkeypatch.setattr(storage, "_client", lambda: fake)

    with pytest.raises(storage.StorageError):
        storage.store_value(PNG_DATA_URL, field="admin_proof")
    assert fake.objects == {}, "a failed upload must leave the bucket untouched"


def test_malformed_and_unsupported_uploads_are_rejected(s3):
    for bad in ("data:image/png;base64,!!!not-base64!!!",   # malformed
                f"data:image/gif;base64,{PNG_B64}",         # unsupported type
                "data:image/png;base64,"):                  # empty payload
        with pytest.raises(storage.StorageError):
            storage.decode_data_url(bad)


# ── 5. Missing objects — degrade, don't explode ───────────────────────────────────────────

def test_missing_object_resolves_to_none_not_an_exception(s3):
    """A deleted or unreachable object must not take down the whole transaction response."""
    dangling = storage.key_to_ref("uploads/admin_proof/ab/deadbeefdeadbeef.png")
    assert storage.resolve_value(dangling) is None


def test_get_bytes_on_missing_object_raises(s3):
    with pytest.raises(storage.StorageError):
        storage.get_bytes("uploads/nope/ab/missing.png")


# ── 6 & 7. Legacy vs migrated classification ─────────────────────────────────────────────

def test_reference_and_data_url_are_distinguishable(s3):
    assert storage.is_ref("storage://uploads/a/b/c.png") is True
    assert storage.is_ref(PNG_DATA_URL) is False
    assert storage.is_ref("https://example.com/a.png") is False
    assert storage.is_data_url(PNG_DATA_URL) is True
    assert storage.is_data_url("storage://uploads/a.png") is False


def test_ref_key_roundtrip():
    key = "uploads/admin_proof/ab/0123456789abcdef.png"
    assert storage.ref_to_key(storage.key_to_ref(key)) == key


# ── 8. Idempotency — the property the backfill depends on ────────────────────────────────

def test_same_bytes_produce_same_key(s3):
    a = storage.decode_data_url(PNG_DATA_URL)
    b = storage.decode_data_url(PNG_DATA_URL)
    assert storage.build_key(field="admin_proof", upload=a) == \
           storage.build_key(field="admin_proof", upload=b)


def test_different_bytes_produce_different_keys(s3):
    other = storage.decode_data_url(
        "data:image/png;base64," + base64.b64encode(b"a different image").decode())
    same = storage.decode_data_url(PNG_DATA_URL)
    assert storage.build_key(field="admin_proof", upload=other) != \
           storage.build_key(field="admin_proof", upload=same)


def test_reupload_is_a_noop(s3):
    """The heart of resumability: running twice must not transfer twice."""
    first, _ = storage.store_value(PNG_DATA_URL, field="admin_proof")
    assert s3.put_calls == 1
    second, _ = storage.store_value(PNG_DATA_URL, field="admin_proof")
    assert second == first, "the same bytes must map to the same reference"
    assert s3.put_calls == 1, "an already-stored object must not be uploaded again"
    assert len(s3.objects) == 1


def test_storing_an_existing_reference_is_a_noop(s3):
    """A row already migrated must be left alone on a re-run."""
    ref, _ = storage.store_value(PNG_DATA_URL, field="admin_proof")
    again, decoded = storage.store_value(ref, field="admin_proof")
    assert again == ref and decoded is None
    assert s3.put_calls == 1


def test_stored_bytes_verify_against_checksum(s3):
    """What the backfill checks before it is willing to clear the source."""
    stored, decoded = storage.store_value(PNG_DATA_URL, field="admin_proof")
    fetched = storage.get_bytes(storage.ref_to_key(stored))
    assert hashlib.sha256(fetched).hexdigest() == decoded.sha256


# ── merchant_proofs is a JSON array of up to 3 files ─────────────────────────────────────

def test_json_array_of_proofs_migrates_per_entry(s3):
    others = [
        "data:image/png;base64," + base64.b64encode(b"proof-two").decode(),
        "data:image/png;base64," + base64.b64encode(b"proof-three").decode(),
    ]
    refs = [storage.store_value(v, field="merchant_proofs")[0]
            for v in [PNG_DATA_URL, *others]]
    assert all(storage.is_ref(r) for r in refs)
    assert len(set(refs)) == 3, "distinct files must not collide"
    assert json.loads(json.dumps(refs)) == refs, "references must survive JSON round-trip"
