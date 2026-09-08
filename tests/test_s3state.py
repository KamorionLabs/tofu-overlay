"""Tests for tofu_overlay.s3state against moto (S3 conditional writes, SSE, DynamoDB items)."""

from __future__ import annotations

import json

import pytest

from tests.conftest import BASE_KEY, BUCKET, LOCK_TABLE, REGION
from tofu_overlay.models import BackendConfig, ExitCode, RegistryError
from tofu_overlay.s3state import CasConflict, S3State

KMS_ARN = "arn:aws:kms:eu-west-1:123456789012:key/0f1e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5b"


class TestObjects:
    def test_head_missing(self, s3state: S3State) -> None:
        assert s3state.head("acme/nope") is None
        assert s3state.exists("acme/nope") is False

    def test_head_present(self, s3state: S3State, s3_client) -> None:
        body = b'{"version": 4}'
        put = s3_client.put_object(Bucket=BUCKET, Key=BASE_KEY, Body=body)
        info = s3state.head(BASE_KEY)
        assert info is not None
        assert info["etag"] == put["ETag"]
        assert info["size"] == len(body)
        assert info["last_modified"]
        assert s3state.exists(BASE_KEY) is True

    def test_list_prefix(self, s3state: S3State, s3_client, backend_cfg: BackendConfig) -> None:
        for name in ("a-111111", "b-222222"):
            s3_client.put_object(Bucket=BUCKET, Key=backend_cfg.overlay_key(name), Body=b"{}")
        s3_client.put_object(Bucket=BUCKET, Key=BASE_KEY, Body=b"{}")
        s3_client.put_object(Bucket=BUCKET, Key=backend_cfg.registry_key(), Body=b"{}")
        keys = s3state.list_prefix(backend_cfg.overlay_key(""))
        assert sorted(keys) == [
            backend_cfg.overlay_key("a-111111"),
            backend_cfg.overlay_key("b-222222"),
        ]
        assert s3state.list_prefix("acme/webshop/nothing/") == []

    def test_copy_reapplies_sse_aes256(self, s3state: S3State, s3_client) -> None:
        s3_client.put_object(Bucket=BUCKET, Key=BASE_KEY, Body=b'{"serial": 1}')
        dst = f"{BASE_KEY}@n.merged-20260908T101500Z"
        s3state.copy(BASE_KEY, dst)
        head = s3_client.head_object(Bucket=BUCKET, Key=dst)
        assert head["ServerSideEncryption"] == "AES256"
        assert s3_client.get_object(Bucket=BUCKET, Key=dst)["Body"].read() == b'{"serial": 1}'

    def test_copy_reapplies_sse_kms(self, boto_session, s3_client) -> None:
        cfg = BackendConfig(bucket=BUCKET, key=BASE_KEY, region=REGION, kms_key_id=KMS_ARN)
        state = S3State(cfg, session=boto_session)
        s3_client.put_object(Bucket=BUCKET, Key=BASE_KEY, Body=b"{}")
        state.copy(BASE_KEY, f"{BASE_KEY}@n.abandoned-20260908T101500Z")
        head = s3_client.head_object(Bucket=BUCKET, Key=f"{BASE_KEY}@n.abandoned-20260908T101500Z")
        assert head["ServerSideEncryption"] == "aws:kms"
        assert head["SSEKMSKeyId"] == KMS_ARN

    def test_copy_missing_source_is_error(self, s3state: S3State) -> None:
        with pytest.raises(Exception):  # noqa: B017 - boto ClientError or OverlayError both acceptable
            s3state.copy("acme/nope", "acme/nope.copy")

    def test_delete(self, s3state: S3State, s3_client) -> None:
        s3_client.put_object(Bucket=BUCKET, Key=BASE_KEY, Body=b"{}")
        s3state.delete(BASE_KEY)
        assert s3state.exists(BASE_KEY) is False
        # deleting an absent key is not an error
        s3state.delete(BASE_KEY)


class TestJsonCas:
    def test_get_json_missing(self, s3state: S3State) -> None:
        assert s3state.get_json("acme/nope.overlays.json") is None

    def test_put_then_get(self, s3state: S3State, backend_cfg: BackendConfig) -> None:
        key = backend_cfg.registry_key()
        etag = s3state.put_json(
            key, {"version": 1, "overlays": {}}, if_match=None, if_none_match=True
        )
        assert etag
        got = s3state.get_json(key)
        assert got is not None
        doc, got_etag = got
        assert doc == {"version": 1, "overlays": {}}
        assert got_etag == etag

    def test_if_none_match_conflict(self, s3state: S3State, backend_cfg: BackendConfig) -> None:
        key = backend_cfg.registry_key()
        s3state.put_json(key, {"version": 1}, if_match=None, if_none_match=True)
        with pytest.raises(CasConflict) as exc:
            s3state.put_json(key, {"version": 1}, if_match=None, if_none_match=True)
        assert isinstance(exc.value, RegistryError)
        assert exc.value.exit_code == ExitCode.REGISTRY

    def test_if_match_conflict_and_success(
        self, s3state: S3State, backend_cfg: BackendConfig
    ) -> None:
        key = backend_cfg.registry_key()
        etag1 = s3state.put_json(key, {"version": 1, "n": 1}, if_match=None, if_none_match=True)
        with pytest.raises(CasConflict):
            s3state.put_json(
                key, {"version": 1, "n": 2}, if_match='"0123456789abcdef0123456789abcdef"'
            )
        etag2 = s3state.put_json(key, {"version": 1, "n": 2}, if_match=etag1)
        assert etag2 != etag1
        got = s3state.get_json(key)
        assert got is not None
        assert got[0]["n"] == 2
        # the stale etag cannot be reused
        with pytest.raises(CasConflict):
            s3state.put_json(key, {"version": 1, "n": 3}, if_match=etag1)

    def test_put_json_is_valid_json_on_s3(self, s3state: S3State, s3_client, backend_cfg) -> None:
        key = backend_cfg.registry_key()
        s3state.put_json(key, {"a": {"b": [1, 2]}}, if_match=None, if_none_match=True)
        raw = s3_client.get_object(Bucket=BUCKET, Key=key)["Body"].read()
        assert json.loads(raw) == {"a": {"b": [1, 2]}}

    def test_get_json_invalid_document(self, s3state: S3State, s3_client, backend_cfg) -> None:
        key = backend_cfg.registry_key()
        s3_client.put_object(Bucket=BUCKET, Key=key, Body=b"{not json")
        with pytest.raises(Exception):  # noqa: B017 - RegistryError or JSON error both acceptable
            s3state.get_json(key)


class TestDynamoDb:
    def test_md5_item(self, s3state: S3State, ddb_client, backend_cfg: BackendConfig) -> None:
        path = backend_cfg.overlay_key("feat-1a2b3c")
        assert s3state.md5_item_exists(path) is False
        ddb_client.put_item(
            TableName=LOCK_TABLE,
            Item={"LockID": {"S": backend_cfg.md5_lock_id(path)}, "Digest": {"S": "abc"}},
        )
        assert s3state.md5_item_exists(path) is True
        s3state.delete_md5_item(path)
        assert s3state.md5_item_exists(path) is False
        # deleting an absent item is not an error
        s3state.delete_md5_item(path)

    def test_lock_item(self, s3state: S3State, ddb_client, backend_cfg: BackendConfig) -> None:
        path = backend_cfg.overlay_key("feat-1a2b3c")
        assert s3state.lock_item(path) is None
        info = {
            "ID": "8c5e5c3a-1234-4bcd-9abc-0123456789ab",
            "Operation": "OperationTypeApply",
            "Who": "dev@acme-laptop",
            "Version": "1.11.1",
            "Created": "2026-09-08T09:00:00Z",
            "Path": f"{BUCKET}/{path}",
        }
        ddb_client.put_item(
            TableName=LOCK_TABLE,
            Item={"LockID": {"S": backend_cfg.lock_id(path)}, "Info": {"S": json.dumps(info)}},
        )
        got = s3state.lock_item(path)
        assert got is not None
        assert got["ID"] == info["ID"] or got.get("Info") == json.dumps(info)

    def test_no_table_configured(self, boto_session) -> None:
        cfg = BackendConfig(bucket=BUCKET, key=BASE_KEY, region=REGION, dynamodb_table=None)
        state = S3State(cfg, session=boto_session)
        assert state.md5_item_exists(BASE_KEY) is False
        assert state.lock_item(BASE_KEY) is None
        state.delete_md5_item(BASE_KEY)

    def test_delete_lockfile(self, s3state: S3State, s3_client, backend_cfg: BackendConfig) -> None:
        path = backend_cfg.overlay_key("feat-1a2b3c")
        s3_client.put_object(Bucket=BUCKET, Key=f"{path}.tflock", Body=b'{"ID": "x"}')
        s3state.delete_lockfile(path)
        assert s3state.exists(f"{path}.tflock") is False
        # absent lockfile is fine
        s3state.delete_lockfile(path)
