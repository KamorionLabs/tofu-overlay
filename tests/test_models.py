"""Tests for tofu_overlay.models: exit codes, key builders, model helpers."""

from __future__ import annotations

import re

import pytest

from tests.conftest import BASE_KEY, BUCKET, ME, OTHER
from tofu_overlay import models
from tofu_overlay.models import (
    BackendConfig,
    ClaimKind,
    ExitCode,
    FrozenError,
    NotAllowedError,
    OverlayError,
    PlanSummary,
    PolicyError,
    PolicyResult,
    RegistryDoc,
    RegistryError,
    StaleError,
    Status,
    ToolError,
    Violation,
)


class TestExitCodes:
    def test_values(self) -> None:
        assert ExitCode.OK == 0
        assert ExitCode.ERROR == 1
        assert ExitCode.CHANGES == 2
        assert ExitCode.POLICY == 3
        assert ExitCode.STALE == 4
        assert ExitCode.REGISTRY == 5
        assert ExitCode.NOT_ALLOWED == 6
        assert ExitCode.FROZEN == 7

    @pytest.mark.parametrize(
        ("cls", "code"),
        [
            (OverlayError, ExitCode.ERROR),
            (ToolError, ExitCode.ERROR),
            (PolicyError, ExitCode.POLICY),
            (StaleError, ExitCode.STALE),
            (RegistryError, ExitCode.REGISTRY),
            (NotAllowedError, ExitCode.NOT_ALLOWED),
            (FrozenError, ExitCode.FROZEN),
        ],
    )
    def test_error_exit_codes(self, cls: type[OverlayError], code: ExitCode) -> None:
        err = cls("boom")
        assert isinstance(err, OverlayError)
        assert err.exit_code == code
        assert "boom" in str(err)


class TestBackendConfigKeys:
    def test_plain_key_builders(self) -> None:
        cfg = BackendConfig(bucket=BUCKET, key=BASE_KEY)
        assert cfg.workspace == "default"
        assert cfg.state_path() == BASE_KEY
        assert cfg.overlay_key("feat-1a2b3c") == f"{BASE_KEY}@feat-1a2b3c"
        assert cfg.registry_key() == f"{BASE_KEY}.overlays.json"
        assert (
            cfg.archive_key("feat-1a2b3c", Status.MERGED, "20260908T101500Z")
            == f"{BASE_KEY}@feat-1a2b3c.merged-20260908T101500Z"
        )

    def test_extension_aware_key_builders(self) -> None:
        cfg = BackendConfig(bucket=BUCKET, key="a/b/terraform.tfstate")
        assert cfg.overlay_key("feat-1a2b3c") == "a/b/terraform@feat-1a2b3c.tfstate"
        assert cfg.registry_key() == "a/b/terraform.overlays.json"
        assert (
            cfg.archive_key("feat-1a2b3c", Status.ABANDONED, "20260908T101500Z")
            == "a/b/terraform@feat-1a2b3c.abandoned-20260908T101500Z.tfstate"
        )

    def test_overlay_prefix_with_empty_name(self) -> None:
        cfg = BackendConfig(bucket=BUCKET, key=BASE_KEY)
        assert cfg.overlay_key("") == f"{BASE_KEY}@"
        assert cfg.overlay_key("x").startswith(cfg.overlay_key(""))

    def test_dotted_directory_is_not_an_extension(self) -> None:
        cfg = BackendConfig(bucket=BUCKET, key="acme.io/webshop/dev")
        assert cfg.overlay_key("n") == "acme.io/webshop/dev@n"
        assert cfg.registry_key() == "acme.io/webshop/dev.overlays.json"

    def test_lock_ids(self) -> None:
        cfg = BackendConfig(bucket=BUCKET, key=BASE_KEY, dynamodb_table="acme-tflock")
        assert cfg.lock_id(BASE_KEY) == f"{BUCKET}/{BASE_KEY}"
        assert cfg.md5_lock_id(BASE_KEY) == f"{BUCKET}/{BASE_KEY}-md5"
        ov = cfg.overlay_key("n")
        assert cfg.md5_lock_id(ov) == f"{BUCKET}/{ov}-md5"

    def test_defaults(self) -> None:
        cfg = BackendConfig(bucket=BUCKET, key=BASE_KEY)
        assert cfg.encrypt is True
        assert cfg.use_lockfile is False
        assert cfg.region is None
        assert cfg.profile is None
        assert cfg.dynamodb_table is None
        assert cfg.kms_key_id is None
        assert cfg.backend_config_files == []


class TestOverlayHelpers:
    @pytest.mark.parametrize(
        ("status", "live"),
        [
            (Status.CREATING, True),
            (Status.ACTIVE, True),
            (Status.APPLYING, True),
            (Status.DIRTY, True),
            (Status.MERGING, True),
            (Status.MERGED, False),
            (Status.ABANDONED, False),
            (Status.NEEDS_REVIEW, False),
        ],
    )
    def test_is_live(self, make_overlay, status: Status, live: bool) -> None:
        assert make_overlay(status=status).is_live() is live

    def test_claim_partitions(self, make_overlay, make_claim) -> None:
        ov = make_overlay(
            claims={
                "aws_s3_bucket.reports": make_claim("create", "aws_s3_bucket"),
                "aws_iam_role.app": make_claim("update", "aws_iam_role"),
            }
        )
        assert set(ov.create_claims()) == {"aws_s3_bucket.reports"}
        assert set(ov.update_claims()) == {"aws_iam_role.app"}
        assert ov.create_claims()["aws_s3_bucket.reports"].kind == ClaimKind.CREATE

    def test_status_is_str_enum(self) -> None:
        assert Status.ACTIVE == "active"
        assert Status("merging") is Status.MERGING
        assert ClaimKind.UPDATE == "update"


class TestRegistryDoc:
    def test_fixture_roundtrip(self, registry_json: dict) -> None:
        doc = RegistryDoc.model_validate(registry_json)
        assert doc.version == 1
        assert doc.base["bucket"] == BUCKET
        assert set(doc.overlays) == {ME, OTHER}
        assert doc.overlays[ME].status == Status.ACTIVE
        assert doc.overlays[ME].claims["aws_iam_role.app"].kind == ClaimKind.UPDATE
        assert "feature-old-1-cleanup-1a2b3c" in doc.tombstones
        assert doc.tombstones["feature-old-1-cleanup-1a2b3c"].status == Status.MERGED
        dumped = doc.model_dump(mode="json")
        assert RegistryDoc.model_validate(dumped) == doc

    def test_live_overlays_excludes_finished(self, registry_doc: RegistryDoc) -> None:
        registry_doc.overlays[OTHER].status = Status.MERGED
        assert set(registry_doc.live_overlays()) == {ME}

    def test_empty_doc_defaults(self) -> None:
        doc = RegistryDoc(tool_version="0.1.0", base={"bucket": BUCKET, "key": BASE_KEY})
        assert doc.overlays == {}
        assert doc.tombstones == {}
        assert doc.live_overlays() == {}


class TestSmallModels:
    def test_plan_summary_import_alias(self) -> None:
        s = PlanSummary.model_validate({"create": 2, "import": 1})
        assert s.import_ == 1
        assert s.create == 2
        assert s.no_op == 0
        assert PlanSummary().model_dump(by_alias=True)["import"] == 0

    def test_policy_result_ok(self) -> None:
        ok = PolicyResult(violations=[], warnings=["w"], claims={})
        assert ok.ok is True
        bad = PolicyResult(
            violations=[Violation(address="a.b", rule="delete", message="m", other_overlay=None)],
            warnings=[],
            claims={},
        )
        assert bad.ok is False

    def test_utcnow_iso(self) -> None:
        ts = models.utcnow_iso()
        assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", ts)
        assert ts.endswith("Z") or "+00:00" in ts

    def test_new_run_id(self) -> None:
        a, b = models.new_run_id(), models.new_run_id()
        assert re.fullmatch(r"[0-9a-f]{12}", a)
        assert a != b
