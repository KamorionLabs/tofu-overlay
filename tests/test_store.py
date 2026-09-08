"""Tests for tofu_overlay.store: the StateStore contract and the make_store factory."""

from __future__ import annotations

import pytest

from tests.conftest import BASE_KEY, BUCKET, REGION
from tofu_overlay import s3state, store
from tofu_overlay.models import BackendConfig, ExitCode, RegistryError, ToolError
from tofu_overlay.s3state import S3State
from tofu_overlay.store import CasConflict, StateStore, make_store


def _cfg(backend_type: str) -> BackendConfig:
    return BackendConfig(backend_type=backend_type, bucket=BUCKET, key=BASE_KEY, region=REGION)


class TestMakeStore:
    def test_s3_returns_s3state(self, boto_session) -> None:
        built = make_store(_cfg("s3"), session=boto_session)
        assert isinstance(built, S3State)
        assert isinstance(built, StateStore)
        assert built.cfg.bucket == BUCKET
        # The injected session is the one the store uses (no new boto3 session).
        assert built.session is boto_session

    def test_default_backend_type_is_s3(self) -> None:
        cfg = BackendConfig(bucket=BUCKET, key=BASE_KEY)
        assert cfg.backend_type == "s3"
        assert isinstance(make_store(cfg), S3State)

    @pytest.mark.parametrize("backend_type", ["azurerm", "gcs", "http", "local", "remote"])
    def test_unsupported_backend_raises_tool_error(self, backend_type: str) -> None:
        with pytest.raises(ToolError) as exc:
            make_store(_cfg(backend_type))
        assert f"backend '{backend_type}' is not supported yet, see docs/ROADMAP.md" in str(
            exc.value
        )
        assert exc.value.exit_code == ExitCode.ERROR

    def test_azurerm_message_carries_hint(self) -> None:
        message = store.unsupported_backend_message("azurerm")
        assert message.startswith("backend 'azurerm' is not supported yet, see docs/ROADMAP.md")
        assert "lease" in message


class TestContract:
    def test_s3state_exposes_every_protocol_method(self) -> None:
        for name in (
            "head", "exists", "list_prefix", "copy", "delete", "get_json", "put_json",
            "digest_item_exists", "delete_digest_item", "lock_info", "delete_lock_marker",
        ):
            assert callable(getattr(S3State, name)), name

    def test_cas_conflict_is_registry_error_and_reexported(self) -> None:
        assert issubclass(CasConflict, RegistryError)
        assert s3state.CasConflict is CasConflict
        assert CasConflict().exit_code == ExitCode.REGISTRY

    def test_supported_backends(self) -> None:
        assert store.SUPPORTED_BACKENDS == ("s3",)
