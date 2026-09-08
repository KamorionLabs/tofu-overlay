"""Raw S3 and DynamoDB access for the ``s3`` backend (the ``StateStore`` implementation).

State objects are only ever *read* here (``HEAD``, ``ListObjects``) or moved
around (``CopyObject`` to an archive key, ``DeleteObject`` at finalize/abandon);
every state write goes through tofu (see DESIGN §3). The registry document is a
plain JSON object written with S3 conditional requests (``IfMatch`` /
``IfNoneMatch: *``) so concurrent writers never clobber each other. The digest
and lock methods of the contract map to the DynamoDB ``-md5`` item, the
DynamoDB lock item and the ``.tflock`` object of ``use_lockfile``.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from tofu_overlay.models import BackendConfig, RegistryError, ToolError
from tofu_overlay.store import CasConflict

__all__ = ["S3State", "CasConflict"]

LOCKFILE_SUFFIX = ".tflock"
NOT_FOUND_CODES = frozenset({"404", "NoSuchKey", "NotFound"})
CAS_CODES = frozenset({"PreconditionFailed", "ConditionalRequestConflict", "412", "409"})
_RETRY_CONFIG = Config(retries={"mode": "standard", "max_attempts": 5})


def _error_code(exc: ClientError) -> str:
    error = exc.response.get("Error", {})
    code = str(error.get("Code", ""))
    status = str(exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", ""))
    return code or status


def _is_not_found(exc: ClientError) -> bool:
    code = _error_code(exc)
    status = str(exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", ""))
    return code in NOT_FOUND_CODES or status == "404"


def _is_cas_conflict(exc: ClientError) -> bool:
    code = _error_code(exc)
    status = str(exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", ""))
    return code in CAS_CODES or status in ("412", "409")


def _message(exc: ClientError) -> str:
    error = exc.response.get("Error", {})
    return f"{error.get('Code', 'error')}: {error.get('Message', str(exc))}"


class S3State:
    """Thin wrapper over the S3 bucket and DynamoDB lock table of a backend.

    Implements :class:`tofu_overlay.store.StateStore`; build it through
    :func:`tofu_overlay.store.make_store`. Pass a preconfigured ``boto3.Session``
    for tests (moto) or to reuse credentials; otherwise one is built from
    ``cfg.profile`` / ``cfg.region``. Clients are created lazily so constructing
    the object never hits the network.
    """

    def __init__(self, cfg: BackendConfig, session: boto3.Session | None = None) -> None:
        self.cfg = cfg
        self.bucket = cfg.bucket
        self._session = session
        self._s3: Any = None
        self._ddb: Any = None

    # ----------------------------------------------------------------- clients

    @property
    def session(self) -> boto3.Session:
        if self._session is None:
            self._session = boto3.Session(
                profile_name=self.cfg.profile, region_name=self.cfg.region
            )
        return self._session

    @property
    def s3(self) -> Any:
        """Lazily created S3 client (tests may set ``_s3`` directly)."""
        if self._s3 is None:
            self._s3 = self.session.client("s3", config=_RETRY_CONFIG)
        return self._s3

    @property
    def dynamodb(self) -> Any:
        """Lazily created DynamoDB client; ``ToolError`` without a lock table."""
        if not self.cfg.dynamodb_table:
            raise ToolError("backend has no dynamodb_table; DynamoDB operations unavailable")
        if self._ddb is None:
            self._ddb = self.session.client("dynamodb", config=_RETRY_CONFIG)
        return self._ddb

    def _uri(self, key: str) -> str:
        return f"s3://{self.bucket}/{key}"

    def _sse_args(self) -> dict[str, str]:
        """Server-side encryption arguments mirroring the backend settings."""
        if not self.cfg.encrypt:
            return {}
        if self.cfg.kms_key_id:
            return {"ServerSideEncryption": "aws:kms", "SSEKMSKeyId": self.cfg.kms_key_id}
        return {"ServerSideEncryption": "AES256"}

    def _call(self, op: str, key: str, fn: Callable[[], Any], error_cls: type = ToolError) -> Any:
        """Run one S3 call, translating unexpected ``ClientError`` into ``error_cls``."""
        try:
            return fn()
        except ClientError as exc:
            raise error_cls(f"{op} {self._uri(key)} failed: {_message(exc)}") from exc

    # ---------------------------------------------------------------- objects

    def head(self, key: str) -> dict | None:
        """``HEAD`` an object: ``{"etag", "size", "last_modified"}`` or ``None`` if absent."""
        try:
            resp = self.s3.head_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            if _is_not_found(exc):
                return None
            raise ToolError(f"HEAD {self._uri(key)} failed: {_message(exc)}") from exc
        last_modified = resp.get("LastModified")
        return {
            "etag": resp.get("ETag", ""),
            "size": int(resp.get("ContentLength", 0)),
            "last_modified": last_modified.isoformat() if last_modified else "",
        }

    def exists(self, key: str) -> bool:
        """True when the object exists (a missing key is never an empty state)."""
        return self.head(key) is not None

    def list_prefix(self, prefix: str) -> list[str]:
        """Every object key under ``prefix`` (paginated)."""

        def run() -> list[str]:
            keys: list[str] = []
            paginator = self.s3.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
                keys.extend(obj["Key"] for obj in page.get("Contents", []))
            return keys

        return self._call("LIST", prefix, run)

    def copy(self, src: str, dst: str) -> None:
        """Server-side copy ``src`` to ``dst``, re-applying the backend's SSE settings."""
        self._call(
            "COPY",
            dst,
            lambda: self.s3.copy_object(
                Bucket=self.bucket,
                Key=dst,
                CopySource={"Bucket": self.bucket, "Key": src},
                **self._sse_args(),
            ),
        )

    def delete(self, key: str) -> None:
        """Delete one object (no error when it is already gone)."""
        self._call("DELETE", key, lambda: self.s3.delete_object(Bucket=self.bucket, Key=key))

    # --------------------------------------------------------------- registry

    def get_json(self, key: str) -> tuple[dict, str] | None:
        """Read a JSON document: ``(doc, etag)`` or ``None`` when the object is absent."""
        try:
            resp = self.s3.get_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            if _is_not_found(exc):
                return None
            raise RegistryError(f"GET {self._uri(key)} failed: {_message(exc)}") from exc
        body = resp["Body"].read()
        try:
            doc = json.loads(body)
        except ValueError as exc:
            raise RegistryError(f"{self._uri(key)} is not valid JSON: {exc}") from exc
        if not isinstance(doc, dict):
            raise RegistryError(f"{self._uri(key)} is not a JSON object")
        return doc, resp.get("ETag", "")

    def put_json(
        self,
        key: str,
        doc: dict,
        *,
        if_match: str | None,
        if_none_match: bool = False,
    ) -> str:
        """Conditionally write a JSON document and return its new ETag.

        ``if_match`` sends ``IfMatch: <etag>`` (update); ``if_none_match`` sends
        ``IfNoneMatch: *`` (creation). A 412 (PreconditionFailed) or 409
        (ConditionalRequestConflict) raises :class:`CasConflict`.
        """
        args: dict[str, Any] = {
            "Bucket": self.bucket,
            "Key": key,
            "Body": json.dumps(doc, indent=2, ensure_ascii=False).encode("utf-8"),
            "ContentType": "application/json",
            **self._sse_args(),
        }
        if if_match:
            args["IfMatch"] = if_match
        if if_none_match:
            args["IfNoneMatch"] = "*"
        try:
            resp = self.s3.put_object(**args)
        except ClientError as exc:
            if _is_cas_conflict(exc):
                raise CasConflict(
                    f"PUT {self._uri(key)} lost a concurrent write ({_error_code(exc)})"
                ) from exc
            raise RegistryError(f"PUT {self._uri(key)} failed: {_message(exc)}") from exc
        return resp.get("ETag", "")

    # --------------------------------------------------------------- dynamodb

    def _get_item(self, lock_id: str) -> dict | None:
        try:
            resp = self.dynamodb.get_item(
                TableName=self.cfg.dynamodb_table,
                Key={"LockID": {"S": lock_id}},
                ConsistentRead=True,
            )
        except ClientError as exc:
            raise ToolError(
                f"DynamoDB GetItem {self.cfg.dynamodb_table}/{lock_id} failed: {_message(exc)}"
            ) from exc
        return resp.get("Item")

    def _delete_item(self, lock_id: str) -> None:
        try:
            self.dynamodb.delete_item(
                TableName=self.cfg.dynamodb_table, Key={"LockID": {"S": lock_id}}
            )
        except ClientError as exc:
            raise ToolError(
                f"DynamoDB DeleteItem {self.cfg.dynamodb_table}/{lock_id} failed: {_message(exc)}"
            ) from exc

    def digest_item_exists(self, path: str) -> bool:
        """True when the ``<bucket>/<path>-md5`` digest item exists (False without a table)."""
        if not self.cfg.dynamodb_table:
            return False
        return self._get_item(self.cfg.md5_lock_id(path)) is not None

    def delete_digest_item(self, path: str) -> None:
        """Delete the ``<bucket>/<path>-md5`` digest item (no-op without a table)."""
        if not self.cfg.dynamodb_table:
            return
        self._delete_item(self.cfg.md5_lock_id(path))

    def lock_info(self, path: str) -> dict | None:
        """Current tofu lock info for ``LockID=<bucket>/<path>``, or ``None`` when unlocked."""
        if not self.cfg.dynamodb_table:
            return None
        item = self._get_item(self.cfg.lock_id(path))
        if item is None:
            return None
        info = item.get("Info", {}).get("S")
        if not info:
            return {}
        try:
            parsed = json.loads(info)
        except ValueError:
            return {"Info": info}
        return parsed if isinstance(parsed, dict) else {"Info": parsed}

    # --------------------------------------------------------------- lockfile

    def delete_lock_marker(self, path: str) -> None:
        """Delete the S3 ``<path>.tflock`` object left by ``use_lockfile`` if present."""
        key = f"{path}{LOCKFILE_SUFFIX}"
        if self.exists(key):
            self.delete(key)
