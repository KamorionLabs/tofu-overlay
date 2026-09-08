"""Tests for tofu_overlay.registry: load, CAS update loop, claims, status machine, tombstones."""

from __future__ import annotations

import copy
import json
import time
from datetime import UTC, datetime, timedelta

import pytest

from tests.conftest import BASE_ETAG, BASE_KEY, BUCKET, ME, OTHER
from tofu_overlay import registry as registry_module
from tofu_overlay.models import (
    ExitCode,
    FrozenError,
    NotAllowedError,
    OverlayError,
    PolicyError,
    RegistryDoc,
    RegistryError,
    Status,
)
from tofu_overlay.registry import Registry
from tofu_overlay.s3state import CasConflict, S3State


def iso(delta: timedelta) -> str:
    return (datetime.now(UTC) + delta).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the CAS backoff instantaneous."""
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    monkeypatch.setattr(registry_module, "sleep", lambda _s: None, raising=False)


@pytest.fixture
def seeded(registry: Registry, s3state: S3State, registry_json: dict) -> Registry:
    """A registry whose S3 document is the registry.json fixture."""
    s3state.put_json(registry.key, registry_json, if_match=None, if_none_match=True)
    return registry


class TestLoad:
    def test_key(self, registry: Registry, backend_cfg) -> None:
        assert registry.key == backend_cfg.registry_key()

    def test_empty(self, registry: Registry) -> None:
        doc, etag = registry.load()
        assert etag is None
        assert isinstance(doc, RegistryDoc)
        assert doc.overlays == {}
        assert doc.base["bucket"] == BUCKET
        assert doc.base["key"] == BASE_KEY

    def test_missing_with_overlay_objects(self, registry: Registry, s3_client, backend_cfg) -> None:
        s3_client.put_object(Bucket=BUCKET, Key=backend_cfg.overlay_key("ghost-1a2b3c"), Body=b"{}")
        with pytest.raises(RegistryError) as exc:
            registry.load()
        assert exc.value.exit_code == ExitCode.REGISTRY
        assert "doctor" in str(exc.value)

    def test_seeded(self, seeded: Registry) -> None:
        doc, etag = seeded.load()
        assert etag
        assert set(doc.overlays) == {ME, OTHER}
        assert doc.overlays[ME].claims["aws_iam_role.app"].kind == "update"

    def test_invalid_json(self, registry: Registry, s3_client) -> None:
        s3_client.put_object(Bucket=BUCKET, Key=registry.key, Body=b"{oops")
        with pytest.raises(RegistryError):
            registry.load()

    def test_newer_version_refused(
        self, registry: Registry, s3state: S3State, registry_json
    ) -> None:
        doc = dict(registry_json, version=99)
        s3state.put_json(registry.key, doc, if_match=None, if_none_match=True)
        with pytest.raises(RegistryError):
            registry.load()

    def test_invalid_shape_refused(self, registry: Registry, s3state: S3State) -> None:
        s3state.put_json(
            registry.key, {"version": 1, "overlays": "nope"}, if_match=None, if_none_match=True
        )
        with pytest.raises(RegistryError):
            registry.load()


    def test_overlay_state_key_must_derive_from_name(
        self, registry: Registry, s3state: S3State, registry_json: dict, backend_cfg
    ) -> None:
        """An entry pointing deletes at the base key (or anywhere else) is refused."""
        for bad_key in (BASE_KEY, backend_cfg.overlay_key("someone-else-000000")):
            doc = copy.deepcopy(registry_json)
            doc["overlays"][ME]["state_key"] = bad_key
            s3state.put_json(registry.key, doc, if_match=None)
            with pytest.raises(RegistryError) as exc:
                registry.load()
            assert exc.value.exit_code == ExitCode.REGISTRY
            assert ME in str(exc.value) and "doctor" in str(exc.value)


class TestUpdate:
    def test_empty_etag_refuses_unconditional_write(
        self, seeded: Registry, s3state: S3State, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        original = s3state.get_json
        monkeypatch.setattr(s3state, "get_json", lambda key: (original(key)[0], ""))
        puts: list = []
        monkeypatch.setattr(s3state, "put_json", lambda *a, **k: puts.append((a, k)))

        with pytest.raises(RegistryError, match="empty ETag"):
            seeded.set_status(ME, Status.DIRTY)
        assert puts == []

    def test_creates_document(self, registry: Registry, s3state: S3State, make_overlay) -> None:
        def fn(doc: RegistryDoc) -> None:
            doc.overlays[ME] = make_overlay(status=Status.CREATING)

        result = registry.update(fn)
        assert result.overlays[ME].status == Status.CREATING
        stored = s3state.get_json(registry.key)
        assert stored is not None
        assert stored[0]["overlays"][ME]["status"] == "creating"
        assert stored[0]["version"] == 1
        assert stored[0]["tool_version"] == "0.1.0"
        assert stored[0]["base"]["key"] == BASE_KEY

    def test_retries_on_cas_conflict(
        self, seeded: Registry, s3state: S3State, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        original = s3state.put_json
        calls = {"put": 0, "fn": 0}

        def flaky_put(key, doc, *, if_match=None, if_none_match=False):
            calls["put"] += 1
            if calls["put"] == 1:
                # simulate a concurrent writer that bumped the ETag in between
                original(
                    key,
                    {**doc, "tool_version": "0.0.9"},
                    if_match=if_match,
                    if_none_match=if_none_match,
                )
                raise CasConflict("precondition failed")
            return original(key, doc, if_match=if_match, if_none_match=if_none_match)

        monkeypatch.setattr(s3state, "put_json", flaky_put)

        def fn(doc: RegistryDoc) -> None:
            calls["fn"] += 1
            doc.overlays[ME].status = Status.MERGING

        result = seeded.update(fn)
        assert result.overlays[ME].status == Status.MERGING
        assert calls["put"] == 2
        assert calls["fn"] >= 2
        stored = s3state.get_json(seeded.key)
        assert stored is not None
        assert stored[0]["overlays"][ME]["status"] == "merging"

    def test_exhaustion_succeeds_when_mutation_already_present(
        self, seeded: Registry, s3state: S3State, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        original = s3state.put_json

        def always_conflict(key, doc, *, if_match=None, if_none_match=False):
            # the "other writer" lands exactly our mutation, then we lose the race
            original(key, doc, if_match=if_match, if_none_match=if_none_match)
            raise CasConflict("precondition failed")

        monkeypatch.setattr(s3state, "put_json", always_conflict)

        def fn(doc: RegistryDoc) -> None:
            doc.overlays[ME].status = Status.MERGING

        result = seeded.update(fn, attempts=2)
        assert result.overlays[ME].status == Status.MERGING

    def test_exhaustion_fails_when_mutation_absent(
        self, seeded: Registry, s3state: S3State, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def always_conflict(key, doc, *, if_match=None, if_none_match=False):
            raise CasConflict("precondition failed")

        monkeypatch.setattr(s3state, "put_json", always_conflict)

        def fn(doc: RegistryDoc) -> None:
            doc.overlays[ME].status = Status.MERGING

        with pytest.raises(RegistryError) as exc:
            seeded.update(fn, attempts=2)
        assert exc.value.exit_code == ExitCode.REGISTRY
        stored = s3state.get_json(seeded.key)
        assert stored is not None
        assert stored[0]["overlays"][ME]["status"] == "active"

    def test_update_uses_if_match(self, seeded: Registry, s3state: S3State, registry_json) -> None:
        # a write that happens before update() re-reads must not be clobbered
        before = s3state.get_json(seeded.key)
        assert before is not None
        concurrent = copy.deepcopy(registry_json)
        concurrent["overlays"][OTHER]["status"] = "dirty"
        s3state.put_json(seeded.key, concurrent, if_match=before[1])

        def fn(doc: RegistryDoc) -> None:
            doc.overlays[ME].status = Status.DIRTY

        result = seeded.update(fn)
        assert result.overlays[ME].status == Status.DIRTY
        assert result.overlays[OTHER].status == Status.DIRTY


class TestLookup:
    def test_get_overlay(self, seeded: Registry) -> None:
        doc, _ = seeded.load()
        assert seeded.get_overlay(doc, ME).branch == "feature/ABC-12-reports"
        with pytest.raises(NotAllowedError) as exc:
            seeded.get_overlay(doc, "unknown-000000")
        assert exc.value.exit_code == ExitCode.NOT_ALLOWED


class TestConflictsFor:
    def test_address_conflict_any_kind(self, seeded: Registry, make_claim) -> None:
        doc, _ = seeded.load()
        wanted = {
            "aws_s3_bucket.audit": make_claim(
                "update", "aws_s3_bucket", {"bucket": "s3-acme-dev-audit"}
            )
        }
        violations = seeded.conflicts_for(doc, ME, wanted)
        assert len(violations) == 1
        assert violations[0].address == "aws_s3_bucket.audit"
        assert violations[0].other_overlay == OTHER

    def test_identity_conflict(self, seeded: Registry, make_claim) -> None:
        doc, _ = seeded.load()
        wanted = {
            "aws_s3_bucket.audit_copy": make_claim(
                "create", "aws_s3_bucket", {"bucket": "s3-acme-dev-audit"}
            )
        }
        violations = seeded.conflicts_for(doc, ME, wanted)
        assert [v.address for v in violations] == ["aws_s3_bucket.audit_copy"]
        assert violations[0].other_overlay == OTHER

    def test_own_claims_and_dead_overlays_ignored(self, seeded: Registry, make_claim) -> None:
        doc, _ = seeded.load()
        wanted = {
            "aws_sns_topic.reports": make_claim(
                "create", "aws_sns_topic", {"name": "sns-acme-dev-reports"}
            ),
            "aws_s3_bucket.reports": make_claim(
                "create", "aws_s3_bucket", {"bucket": "s3-acme-dev-reports"}
            ),
        }
        assert seeded.conflicts_for(doc, ME, wanted) == []
        doc.overlays[OTHER].status = Status.MERGED
        wanted = {
            "aws_s3_bucket.audit": make_claim(
                "create", "aws_s3_bucket", {"bucket": "s3-acme-dev-audit"}
            )
        }
        assert seeded.conflicts_for(doc, ME, wanted) == []

    def test_frozen_overlay_still_holds_claims(self, seeded: Registry, make_claim) -> None:
        doc, _ = seeded.load()
        doc.overlays[OTHER].status = Status.MERGING
        wanted = {
            "aws_s3_bucket.audit": make_claim(
                "create", "aws_s3_bucket", {"bucket": "s3-acme-dev-audit"}
            )
        }
        assert len(seeded.conflicts_for(doc, ME, wanted)) >= 1


class TestAcquireClaims:
    def test_acquire(self, seeded: Registry, make_claim) -> None:
        wanted = {
            "aws_s3_bucket.reports": make_claim(
                "create", "aws_s3_bucket", {"bucket": "s3-acme-dev-reports"}
            ),
            "aws_iam_role.app": make_claim(
                "update", "aws_iam_role", {"name": "iam-acme-dev-app"}, after_hash="ab" * 32
            ),
        }
        doc = seeded.acquire_claims(ME, wanted, "0123456789ab", BASE_ETAG)
        ov = doc.overlays[ME]
        assert ov.status == Status.APPLYING
        assert ov.run_id == "0123456789ab"
        assert ov.applying_since
        assert ov.claims["aws_s3_bucket.reports"].identity == {"bucket": "s3-acme-dev-reports"}
        assert ov.claims["aws_iam_role.app"].after_hash == "ab" * 32
        # previously held claims survive
        assert "aws_cloudwatch_log_group.reports" in ov.claims
        # idempotent re-acquire
        again = seeded.acquire_claims(ME, wanted, "0123456789ab", BASE_ETAG)
        assert again.overlays[ME].claims.keys() == ov.claims.keys()

    def test_conflict_is_policy_error(self, seeded: Registry, make_claim, s3state) -> None:
        wanted = {
            "aws_s3_bucket.audit": make_claim(
                "create", "aws_s3_bucket", {"bucket": "s3-acme-dev-audit"}
            )
        }
        with pytest.raises(PolicyError) as exc:
            seeded.acquire_claims(ME, wanted, "0123456789ab", BASE_ETAG)
        assert exc.value.exit_code == ExitCode.POLICY
        stored = s3state.get_json(seeded.key)
        assert stored is not None
        assert stored[0]["overlays"][ME]["status"] == "active"

    def test_stale_base_refused(self, seeded: Registry, make_claim) -> None:
        wanted = {
            "aws_s3_bucket.reports": make_claim(
                "create", "aws_s3_bucket", {"bucket": "s3-acme-dev-reports"}
            )
        }
        with pytest.raises(OverlayError) as exc:
            seeded.acquire_claims(ME, wanted, "0123456789ab", '"moved-etag"')
        assert exc.value.exit_code == ExitCode.STALE

    def test_frozen_refused(self, seeded: Registry, make_claim) -> None:
        seeded.set_status(ME, Status.MERGING)
        wanted = {
            "aws_s3_bucket.reports": make_claim(
                "create", "aws_s3_bucket", {"bucket": "s3-acme-dev-reports"}
            )
        }
        with pytest.raises(FrozenError) as exc:
            seeded.acquire_claims(ME, wanted, "0123456789ab", BASE_ETAG)
        assert exc.value.exit_code == ExitCode.FROZEN

    def test_dead_overlay_refused(self, seeded: Registry, make_claim) -> None:
        seeded.set_status(ME, Status.MERGED)
        wanted = {
            "aws_s3_bucket.reports": make_claim(
                "create", "aws_s3_bucket", {"bucket": "s3-acme-dev-reports"}
            )
        }
        with pytest.raises(OverlayError):
            seeded.acquire_claims(ME, wanted, "0123456789ab", BASE_ETAG)

    def test_unknown_overlay(self, seeded: Registry, make_claim) -> None:
        with pytest.raises(NotAllowedError):
            seeded.acquire_claims("nope-000000", {}, "0123456789ab", BASE_ETAG)


class TestFinishApply:
    def test_success(self, seeded: Registry, make_claim) -> None:
        wanted = {
            "aws_s3_bucket.reports": make_claim(
                "create", "aws_s3_bucket", {"bucket": "s3-acme-dev-reports"}
            )
        }
        seeded.acquire_claims(ME, wanted, "0123456789ab", BASE_ETAG)
        filled = {
            "aws_s3_bucket.reports": make_claim(
                "create",
                "aws_s3_bucket",
                {"bucket": "s3-acme-dev-reports"},
                id_="s3-acme-dev-reports",
                import_id="s3-acme-dev-reports",
            )
        }
        doc = seeded.finish_apply(
            ME,
            ok=True,
            claims=filled,
            applied_commit="f" * 40,
            caller_arn="arn:aws:sts::123456789012:assumed-role/acme-dev-admin/dev.one",
            tofu_version="1.11.1",
            base_etag_after=BASE_ETAG,
            summary={"create": 1, "update": 0},
        )
        ov = doc.overlays[ME]
        assert ov.status == Status.ACTIVE
        assert ov.run_id is None
        assert ov.applied_commit == "f" * 40
        assert ov.caller_arn.endswith("dev.one")
        assert ov.tofu_version == "1.11.1"
        assert ov.claims["aws_s3_bucket.reports"].id == "s3-acme-dev-reports"
        assert ov.claims["aws_s3_bucket.reports"].import_id == "s3-acme-dev-reports"
        assert ov.last_apply is not None
        assert ov.last_apply["summary"] == {"create": 1, "update": 0}
        assert ov.last_apply["at"]

    def test_success_drops_claims_absent_from_state(self, seeded: Registry, make_claim) -> None:
        """Own resources deleted by the plan lose their claim once the apply succeeded."""
        wanted = {
            "aws_sns_topic.reports": make_claim(
                "create", "aws_sns_topic", {"name": "sns-acme-dev-reports"}
            )
        }
        seeded.acquire_claims(ME, wanted, "0123456789ab", BASE_ETAG)
        doc = seeded.finish_apply(
            ME,
            ok=True,
            claims=wanted,
            applied_commit="f" * 40,
            caller_arn=None,
            tofu_version="1.11.1",
            base_etag_after=BASE_ETAG,
            summary={"delete": 1},
            run_id="0123456789ab",
            released={"aws_cloudwatch_log_group.reports"},
        )
        ov = doc.overlays[ME]
        assert ov.status == Status.ACTIVE
        assert "aws_cloudwatch_log_group.reports" not in ov.claims
        assert "aws_sns_topic.reports" in ov.claims
        assert "aws_iam_role.app" in ov.claims
        reloaded, _ = seeded.load()
        assert "aws_cloudwatch_log_group.reports" not in reloaded.overlays[ME].claims

    def test_failure_keeps_released_claims(self, seeded: Registry, make_claim) -> None:
        seeded.acquire_claims(ME, {}, "0123456789ab", BASE_ETAG)
        doc = seeded.finish_apply(
            ME,
            ok=False,
            claims={},
            applied_commit=None,
            caller_arn=None,
            tofu_version=None,
            base_etag_after=BASE_ETAG,
            summary={},
            run_id="0123456789ab",
            released={"aws_cloudwatch_log_group.reports"},
        )
        assert doc.overlays[ME].status == Status.DIRTY
        assert "aws_cloudwatch_log_group.reports" in doc.overlays[ME].claims

    def test_finish_apply_from_superseded_run_is_refused(
        self, seeded: Registry, s3state: S3State, make_claim
    ) -> None:
        """A stale apply taken over by a newer run cannot flip the status back."""
        seeded.set_status(ME, Status.APPLYING, run_id="r2", applying_since=iso(timedelta()))
        before = s3state.get_json(seeded.key)
        assert before is not None
        with pytest.raises(RegistryError) as exc:
            seeded.finish_apply(
                ME,
                ok=True,
                claims={},
                applied_commit="f" * 40,
                caller_arn=None,
                tofu_version=None,
                base_etag_after=BASE_ETAG,
                summary={},
                run_id="r1",
            )
        assert exc.value.exit_code == ExitCode.REGISTRY
        assert "superseded" in str(exc.value)
        after = s3state.get_json(seeded.key)
        assert after is not None
        assert after[0] == before[0]
        assert after[0]["overlays"][ME]["status"] == "applying"
        assert after[0]["overlays"][ME]["run_id"] == "r2"

    def test_failure_is_dirty_and_keeps_claims(self, seeded: Registry, make_claim) -> None:
        wanted = {
            "aws_s3_bucket.reports": make_claim(
                "create", "aws_s3_bucket", {"bucket": "s3-acme-dev-reports"}
            )
        }
        seeded.acquire_claims(ME, wanted, "0123456789ab", BASE_ETAG)
        doc = seeded.finish_apply(
            ME,
            ok=False,
            claims=wanted,
            applied_commit=None,
            caller_arn=None,
            tofu_version="1.11.1",
            base_etag_after=BASE_ETAG,
            summary={},
        )
        ov = doc.overlays[ME]
        assert ov.status == Status.DIRTY
        assert "aws_s3_bucket.reports" in ov.claims
        assert "aws_cloudwatch_log_group.reports" in ov.claims


class TestStatusAndTombstones:
    def test_set_status_with_fields(self, seeded: Registry) -> None:
        doc = seeded.set_status(ME, Status.MERGING, applied_commit="e" * 40)
        assert doc.overlays[ME].status == Status.MERGING
        assert doc.overlays[ME].applied_commit == "e" * 40
        reloaded, _ = seeded.load()
        assert reloaded.overlays[ME].status == Status.MERGING

    def test_set_status_refuses_changed_status(self, seeded: Registry) -> None:
        seeded.set_status(ME, Status.APPLYING, run_id="r2")
        with pytest.raises(RegistryError) as exc:
            seeded.set_status(ME, Status.MERGING, expect_status={Status.ACTIVE})
        assert exc.value.exit_code == ExitCode.REGISTRY
        reloaded, _ = seeded.load()
        assert reloaded.overlays[ME].status == Status.APPLYING

    def test_set_status_refuses_frozen_entry(self, seeded: Registry) -> None:
        seeded.set_status(ME, Status.MERGING)
        with pytest.raises(FrozenError):
            seeded.set_status(ME, Status.ACTIVE, expect_status={Status.ACTIVE, Status.DIRTY})

    def test_set_status_refuses_changed_entry(self, seeded: Registry) -> None:
        doc, _ = seeded.load()
        loaded = doc.overlays[ME]
        seeded.set_status(ME, Status.ACTIVE, applied_commit="d" * 40)  # someone else wrote
        with pytest.raises(RegistryError, match="changed since it was loaded"):
            seeded.set_status(
                ME, Status.MERGING, expect_status={Status.ACTIVE}, expect_entry=loaded
            )
        reloaded, _ = seeded.load()
        assert reloaded.overlays[ME].status == Status.ACTIVE
        # unchanged entry: the write goes through
        doc = seeded.set_status(
            ME, Status.MERGING, expect_status={Status.ACTIVE}, expect_entry=reloaded.overlays[ME]
        )
        assert doc.overlays[ME].status == Status.MERGING

    def test_set_status_merges_claims(self, seeded: Registry, make_claim) -> None:
        """Claims acquired by a concurrent apply survive a status change."""
        concurrent = {
            "aws_sqs_queue.new": make_claim("create", "aws_sqs_queue", {"name": "q"}, id_="q")
        }
        seeded.acquire_claims(ME, concurrent, "r9", BASE_ETAG)
        seeded.finish_apply(
            ME, ok=True, claims=concurrent, applied_commit=None, caller_arn=None,
            tofu_version=None, base_etag_after=BASE_ETAG, summary={}, run_id="r9",
        )
        refreshed = {
            "aws_sns_topic.reports": make_claim(
                "create", "aws_sns_topic", {"name": "sns-acme-dev-reports"}, id_="arn:sns"
            )
        }
        doc = seeded.set_status(ME, Status.MERGING, claims=refreshed)
        ov = doc.overlays[ME]
        assert ov.status == Status.MERGING
        assert ov.claims["aws_sns_topic.reports"].id == "arn:sns"
        assert "aws_sqs_queue.new" in ov.claims
        assert "aws_cloudwatch_log_group.reports" in ov.claims

    def test_release_overlay(self, seeded: Registry) -> None:
        doc = seeded.release_overlay(ME, Status.MERGED)
        assert ME not in doc.overlays
        assert doc.tombstones[ME].status == Status.MERGED
        assert doc.tombstones[ME].branch == "feature/ABC-12-reports"
        assert doc.tombstones[ME].at
        assert OTHER in doc.overlays
        reloaded, _ = seeded.load()
        assert ME in reloaded.tombstones
        assert seeded.tombstoned(reloaded, ME, 14) is True

    def test_tombstoned_window(self, seeded: Registry, registry_doc: RegistryDoc) -> None:
        name = "feature-old-1-cleanup-1a2b3c"
        registry_doc.tombstones[name].at = iso(timedelta(days=-3))
        assert seeded.tombstoned(registry_doc, name, 14) is True
        registry_doc.tombstones[name].at = iso(timedelta(days=-30))
        assert seeded.tombstoned(registry_doc, name, 14) is False
        assert seeded.tombstoned(registry_doc, "never-seen-000000", 14) is False

    def test_stale_applying(self, seeded: Registry, make_overlay) -> None:
        old = make_overlay(
            status=Status.APPLYING, applying_since=iso(timedelta(hours=-3)), run_id="abc"
        )
        recent = make_overlay(
            status=Status.APPLYING, applying_since=iso(timedelta(minutes=-5)), run_id="abc"
        )
        active = make_overlay(status=Status.ACTIVE)
        assert seeded.stale_applying(old, 90) is True
        assert seeded.stale_applying(recent, 90) is False
        assert seeded.stale_applying(active, 90) is False


class TestRelease:
    def test_release_overlay_carries_pending_revert(self, seeded: Registry) -> None:
        doc = seeded.release_overlay(
            ME, Status.ABANDONED, pending_revert=["aws_iam_role.app", "aws_iam_role.app"]
        )
        tomb = doc.tombstones[ME]
        assert tomb.status == Status.ABANDONED
        assert tomb.pending_revert == ["aws_iam_role.app"]
        reloaded, _ = seeded.load()
        assert reloaded.tombstones[ME].pending_revert == ["aws_iam_role.app"]
        # idempotent: a second release keeps the tombstone
        again = seeded.release_overlay(ME, Status.ABANDONED)
        assert again.tombstones[ME].pending_revert == ["aws_iam_role.app"]

    def test_release_overlay_defaults_to_entry_pending_revert(self, seeded: Registry) -> None:
        seeded.set_status(ME, Status.ACTIVE, pending_revert=["aws_s3_bucket.logs"])
        doc = seeded.release_overlay(ME, Status.ABANDONED)
        assert doc.tombstones[ME].pending_revert == ["aws_s3_bucket.logs"]

    def test_release_overlay_precondition(self, seeded: Registry) -> None:
        with pytest.raises(RegistryError):
            seeded.release_overlay(ME, Status.MERGED, expect_status={Status.MERGING})
        reloaded, _ = seeded.load()
        assert ME in reloaded.overlays


class TestDocumentOnS3:
    def test_document_is_json_and_versioned(self, seeded: Registry, s3_client) -> None:
        seeded.set_status(ME, Status.DIRTY)
        raw = s3_client.get_object(Bucket=BUCKET, Key=seeded.key)["Body"].read()
        doc = json.loads(raw)
        assert doc["version"] == 1
        assert doc["overlays"][ME]["status"] == "dirty"
        assert doc["overlays"][ME]["claims"]["aws_iam_role.app"]["kind"] == "update"
