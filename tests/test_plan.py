"""Tests for tofu_overlay.plan: parsing, policy evaluation (DESIGN §7), verify (§8), guard."""

from __future__ import annotations

import copy
import re
from collections.abc import Iterable
from typing import Any

import pytest

from tests.conftest import ME, OTHER
from tofu_overlay import plan
from tofu_overlay.models import (
    ClaimKind,
    PlanSummary,
    PolicyResult,
    RegistryDoc,
    ResourceChange,
    Status,
    Violation,
)

BASE_ADDRESSES = {
    "aws_iam_role.app",
    "aws_s3_bucket.assets",
    "aws_s3_bucket.logs",
    'aws_ssm_parameter.flags["a"]',
    'aws_ssm_parameter.flags["b"]',
    "aws_lambda_function.worker",
    "aws_sqs_queue.events",
    "aws_route53_record.www",
    "aws_instance.bastion[0]",
    "aws_instance.bastion[1]",
    "module.net.aws_security_group.app",
}

CREATES = {
    "aws_s3_bucket.reports",
    'aws_ssm_parameter.flags["c"]',
    "aws_ssm_parameter.token",
    "module.net.aws_security_group.reports",
    "aws_sqs_queue.dynamic",
}
OWN_OK = {"aws_cloudwatch_log_group.reports", "aws_sns_topic.reports"}
IGNORED = {"aws_instance.bastion[0]", "data.aws_caller_identity.current"}
ALLOWED = CREATES | OWN_OK | IGNORED | {"aws_iam_role.app"}
DENIED = {
    "aws_s3_bucket.logs",
    "aws_lambda_function.worker",
    "aws_s3_bucket.assets",
    "aws_sqs_queue.events",
    "aws_s3_bucket.adopted",
}
MOVED = {"aws_route53_record.www_v2", "aws_route53_record.www"}


def mk_change(
    address: str,
    actions: list[str],
    *,
    type_: str | None = None,
    before: Any = None,
    after: Any = None,
    after_unknown: Any = None,
    after_sensitive: Any = None,
    replace_paths: list | None = None,
    importing: dict | None = None,
    previous_address: str | None = None,
    deposed: str | None = None,
    module_address: str | None = None,
    index: Any = None,
    action_reason: str | None = None,
) -> ResourceChange:
    """Build a ResourceChange with every field set explicitly."""
    bare = address.split(".")
    if bare[0] == "module":
        bare = bare[2:]
    if bare[0] == "data":
        bare = bare[1:]
    rtype = type_ or bare[0]
    name = re.sub(r"\[.*$", "", bare[1])
    return ResourceChange(
        address=address,
        previous_address=previous_address,
        module_address=module_address,
        type=rtype,
        name=name,
        index=index,
        deposed=deposed,
        actions=actions,
        before=before,
        after=after,
        after_unknown={} if after_unknown is None else after_unknown,
        after_sensitive={} if after_sensitive is None else after_sensitive,
        replace_paths=replace_paths or [],
        importing=importing,
        action_reason=action_reason,
    )


def subset(changes: Iterable[ResourceChange], addresses: set[str]) -> list[ResourceChange]:
    return [c for c in changes if c.address in addresses]


def violated(result: PolicyResult) -> set[str]:
    return {v.address for v in result.violations}


@pytest.fixture
def parsed(plan_synthetic: dict) -> tuple[list[ResourceChange], list[dict], PlanSummary]:
    return plan.parse_plan(plan_synthetic)


@pytest.fixture
def evaluate(registry_doc: RegistryDoc, knowledge, registry):
    """Bind evaluate() to the fixture registry document, overriding what a test needs."""

    def _run(
        changes: list[ResourceChange],
        drift: list[dict] | None = None,
        *,
        doc: RegistryDoc | None = None,
        base_identities: set[str] | None = None,
        trunk_drift: dict[str, list[str]] | None = None,
    ) -> PolicyResult:
        d = doc or registry_doc
        return plan.evaluate(
            changes,
            drift or [],
            base_addresses=set(BASE_ADDRESSES),
            base_identities=base_identities or set(),
            me=d.overlays[ME],
            doc=d,
            knowledge=knowledge,
            registry=registry,
            trunk_drift=trunk_drift,
        )

    return _run


class TestParsePlan:
    def test_synthetic_shape(self, parsed) -> None:
        changes, drift, summary = parsed
        by_addr = {(c.address, c.deposed): c for c in changes}
        assert len(changes) == 17
        c = by_addr[("aws_s3_bucket.reports", None)]
        assert c.actions == ["create"]
        assert c.type == "aws_s3_bucket"
        assert c.after["bucket"] == "s3-acme-dev-reports"
        assert c.after_unknown["id"] is True
        c = by_addr[('aws_ssm_parameter.flags["c"]', None)]
        assert c.index == "c"
        assert c.after_sensitive == {"value": True}
        c = by_addr[("module.net.aws_security_group.reports", None)]
        assert c.module_address == "module.net"
        c = by_addr[("aws_lambda_function.worker", None)]
        assert c.actions == ["delete", "create"]
        assert c.replace_paths == [["function_name"]]
        assert c.action_reason == "replace_because_cannot_update"
        c = by_addr[("aws_route53_record.www_v2", None)]
        assert c.previous_address == "aws_route53_record.www"
        c = by_addr[("aws_s3_bucket.adopted", None)]
        assert c.importing == {"id": "s3-acme-dev-adopted"}
        c = by_addr[("aws_cloudwatch_log_group.reports", "8a2f1c9e")]
        assert c.actions == ["delete"]
        assert drift and drift[0]["address"] == "aws_iam_role.app"
        assert summary.create == 5
        assert summary.replace == 2
        assert summary.import_ == 1
        assert summary.update >= 2
        assert summary.delete >= 1
        assert summary.no_op >= 2

    def test_real_terraform_data_plan(self, plan_terraform_data: dict) -> None:
        changes, drift, summary = plan.parse_plan(plan_terraform_data)
        assert drift == []
        assert len(changes) == 5
        imported = next(c for c in changes if c.address == "terraform_data.imported")
        assert imported.importing == {"id": "abc"}
        assert imported.actions == ["no-op"]
        many_a = next(c for c in changes if c.address == 'terraform_data.many["a"]')
        assert many_a.index == "a"
        assert many_a.after_unknown == {"id": True, "output": True}
        inner = next(c for c in changes if c.address == "module.m.terraform_data.inner")
        assert inner.module_address == "module.m"
        assert summary.create == 4
        assert summary.no_op == 1
        assert summary.import_ == 1

    def test_missing_sections(self) -> None:
        changes, drift, summary = plan.parse_plan({"format_version": "1.2"})
        assert changes == [] and drift == []
        assert summary == PlanSummary()

    def test_is_base_address(self) -> None:
        assert plan.is_base_address("aws_iam_role.app", BASE_ADDRESSES) is True
        assert plan.is_base_address("aws_s3_bucket.reports", BASE_ADDRESSES) is False

    def test_render_summary(self) -> None:
        text = plan.render_summary(PlanSummary(create=3, update=1, delete=0, replace=2))
        assert "3" in text
        assert "create" in text.lower() or "add" in text.lower()
        assert "2" in text


class TestEvaluateActions:
    def test_full_synthetic_plan(self, parsed, evaluate) -> None:
        changes, drift, _ = parsed
        result = evaluate(changes, drift)
        assert result.ok is False
        bad = violated(result)
        assert DENIED <= bad
        assert bad & MOVED
        assert not (bad & ALLOWED)
        assert bad <= DENIED | MOVED

    def test_creates_are_claimed(self, parsed, evaluate) -> None:
        changes, _, _ = parsed
        result = evaluate(subset(changes, CREATES))
        assert result.ok is True
        claims = result.claims
        assert CREATES <= set(claims)
        for addr in CREATES:
            assert claims[addr].kind == ClaimKind.CREATE
        assert claims["aws_s3_bucket.reports"].type == "aws_s3_bucket"
        assert claims["aws_s3_bucket.reports"].identity == {"bucket": "s3-acme-dev-reports"}
        assert claims['aws_ssm_parameter.flags["c"]'].identity == {"name": "/acme/dev/flags/c"}
        sg_identity = claims["module.net.aws_security_group.reports"].identity
        assert sg_identity["name"] == "nsg-acme-dev-reports"
        assert set(sg_identity) <= {"name", "vpc_id"}
        assert claims["aws_s3_bucket.reports"].claimed_at

    def test_sensitive_attributes_never_reach_claims(self, parsed, evaluate) -> None:
        changes, _, _ = parsed
        result = evaluate(subset(changes, {"aws_ssm_parameter.token"}))
        claim = result.claims["aws_ssm_parameter.token"]
        assert claim.identity == {"name": "/acme/dev/token"}
        assert "s3cr3t" not in claim.model_dump_json()

    def test_unknown_identity_is_address_only_claim_with_warning(self, parsed, evaluate) -> None:
        changes, _, _ = parsed
        result = evaluate(subset(changes, {"aws_sqs_queue.dynamic"}))
        assert result.ok is True
        assert result.claims["aws_sqs_queue.dynamic"].identity == {}
        assert any("aws_sqs_queue.dynamic" in w for w in result.warnings)

    def test_update_on_base_takes_update_claim(self, parsed, evaluate) -> None:
        changes, _, _ = parsed
        result = evaluate(subset(changes, {"aws_iam_role.app"}))
        assert result.ok is True
        claim = result.claims["aws_iam_role.app"]
        assert claim.kind == ClaimKind.UPDATE
        assert claim.type == "aws_iam_role"
        assert claim.identity == {"name": "iam-acme-dev-app"}
        assert claim.after_hash and re.fullmatch(r"[0-9a-f]{64}", claim.after_hash)
        assert claim.after_hash != "0" * 64

    def test_update_hash_is_deterministic(self, parsed, evaluate) -> None:
        changes, _, _ = parsed
        a = evaluate(subset(changes, {"aws_iam_role.app"})).claims["aws_iam_role.app"].after_hash
        b = (
            evaluate(subset(copy.deepcopy(changes), {"aws_iam_role.app"}))
            .claims["aws_iam_role.app"]
            .after_hash
        )
        assert a == b

    def test_update_on_own_resource_needs_no_claim(self, parsed, evaluate) -> None:
        changes, _, _ = parsed
        own = [
            c
            for c in changes
            if c.address == "aws_cloudwatch_log_group.reports" and c.deposed is None
        ]
        result = evaluate(own)
        assert result.ok is True
        claim = result.claims.get("aws_cloudwatch_log_group.reports")
        assert claim is None or claim.kind == ClaimKind.CREATE

    def test_existing_own_claims_are_kept(self, parsed, evaluate) -> None:
        changes, _, _ = parsed
        result = evaluate(subset(changes, {"aws_s3_bucket.reports"}))
        assert "aws_cloudwatch_log_group.reports" in result.claims
        assert "aws_sns_topic.reports" in result.claims
        assert result.claims["aws_sns_topic.reports"].id is not None

    def test_noop_and_read_ignored(self, parsed, evaluate) -> None:
        changes, _, _ = parsed
        result = evaluate(subset(changes, IGNORED))
        assert result.ok is True
        assert not (set(result.claims) & IGNORED)

    @pytest.mark.parametrize(
        "address",
        ["aws_s3_bucket.logs", "aws_lambda_function.worker", "aws_s3_bucket.assets"],
    )
    def test_destructive_on_base_denied(self, parsed, evaluate, address: str) -> None:
        changes, _, _ = parsed
        result = evaluate(subset(changes, {address}))
        assert result.ok is False
        assert violated(result) == {address}
        assert address not in result.claims

    def test_replace_variants_on_base_denied(self, evaluate) -> None:
        for actions in (
            ["create", "delete"],
            ["delete", "create"],
            ["forget", "create"],
            ["forget"],
        ):
            change = mk_change(
                "aws_s3_bucket.logs",
                actions,
                before={"bucket": "s3-acme-dev-logs"},
                after={"bucket": "s3-acme-dev-logs-v2"},
            )
            result = evaluate([change])
            assert result.ok is False, actions
            assert violated(result) == {"aws_s3_bucket.logs"}

    def test_replace_of_own_resource_allowed(self, parsed, evaluate) -> None:
        changes, _, _ = parsed
        result = evaluate(subset(changes, {"aws_sns_topic.reports"}))
        assert result.ok is True
        assert result.claims["aws_sns_topic.reports"].kind == ClaimKind.CREATE

    def test_moved_from_base_denied(self, parsed, evaluate) -> None:
        changes, _, _ = parsed
        result = evaluate(subset(changes, {"aws_route53_record.www_v2"}))
        assert result.ok is False
        assert violated(result) & MOVED

    def test_importing_denied(self, parsed, evaluate) -> None:
        changes, _, _ = parsed
        result = evaluate(subset(changes, {"aws_s3_bucket.adopted"}))
        assert result.ok is False
        assert violated(result) == {"aws_s3_bucket.adopted"}
        assert "aws_s3_bucket.adopted" not in result.claims

    def test_deposed_on_own_create_claim_allowed(self, parsed, evaluate) -> None:
        changes, _, _ = parsed
        deposed = [
            c for c in changes if c.address == "aws_cloudwatch_log_group.reports" and c.deposed
        ]
        assert deposed
        assert evaluate(deposed).ok is True

    def test_deposed_on_base_denied(self, parsed, evaluate) -> None:
        changes, _, _ = parsed
        result = evaluate(subset(changes, {"aws_sqs_queue.events"}))
        assert result.ok is False
        assert violated(result) == {"aws_sqs_queue.events"}

    def test_deposed_on_unclaimed_address_denied(self, evaluate) -> None:
        change = mk_change(
            "aws_sqs_queue.orphan", ["delete"], deposed="9e8d7c6b", before={"name": "x"}
        )
        result = evaluate([change])
        assert result.ok is False
        assert violated(result) == {"aws_sqs_queue.orphan"}

    def test_violation_shape(self, parsed, evaluate) -> None:
        changes, _, _ = parsed
        result = evaluate(subset(changes, {"aws_s3_bucket.logs"}))
        v = result.violations[0]
        assert isinstance(v, Violation)
        assert v.address == "aws_s3_bucket.logs"
        assert v.rule
        assert v.message
        assert v.other_overlay is None


class TestEvaluateConflicts:
    def test_cross_overlay_address_conflict(
        self, parsed, evaluate, registry_doc, make_claim
    ) -> None:
        changes, _, _ = parsed
        doc = copy.deepcopy(registry_doc)
        doc.overlays[OTHER].claims['aws_ssm_parameter.flags["c"]'] = make_claim(
            "create", "aws_ssm_parameter", {"name": "/acme/dev/flags/c"}
        )
        result = evaluate(subset(changes, CREATES), doc=doc)
        assert result.ok is False
        assert violated(result) == {'aws_ssm_parameter.flags["c"]'}
        assert result.violations[0].other_overlay == OTHER

    def test_cross_overlay_update_address_conflict(
        self, parsed, evaluate, registry_doc, make_claim
    ) -> None:
        changes, _, _ = parsed
        doc = copy.deepcopy(registry_doc)
        doc.overlays[OTHER].claims["aws_iam_role.app"] = make_claim(
            "update", "aws_iam_role", {"name": "iam-acme-dev-app"}, after_hash="ab" * 32
        )
        result = evaluate(subset(changes, {"aws_iam_role.app"}), doc=doc)
        assert result.ok is False
        assert violated(result) == {"aws_iam_role.app"}
        assert result.violations[0].other_overlay == OTHER

    def test_cross_overlay_identity_conflict(
        self, parsed, evaluate, registry_doc, make_claim
    ) -> None:
        changes, _, _ = parsed
        doc = copy.deepcopy(registry_doc)
        doc.overlays[OTHER].claims["aws_s3_bucket.audit"] = make_claim(
            "create", "aws_s3_bucket", {"bucket": "s3-acme-dev-reports"}
        )
        result = evaluate(subset(changes, CREATES), doc=doc)
        assert result.ok is False
        assert violated(result) == {"aws_s3_bucket.reports"}
        assert result.violations[0].other_overlay == OTHER

    def test_dead_overlay_does_not_conflict(
        self, parsed, evaluate, registry_doc, make_claim
    ) -> None:
        changes, _, _ = parsed
        doc = copy.deepcopy(registry_doc)
        doc.overlays[OTHER].claims["aws_s3_bucket.reports"] = make_claim(
            "create", "aws_s3_bucket", {"bucket": "s3-acme-dev-reports"}
        )
        doc.overlays[OTHER].status = Status.ABANDONED
        assert evaluate(subset(changes, CREATES), doc=doc).ok is True

    def test_frozen_overlay_still_conflicts(
        self, parsed, evaluate, registry_doc, make_claim
    ) -> None:
        changes, _, _ = parsed
        doc = copy.deepcopy(registry_doc)
        doc.overlays[OTHER].claims["aws_s3_bucket.reports"] = make_claim(
            "create", "aws_s3_bucket", {"bucket": "s3-acme-dev-reports"}
        )
        doc.overlays[OTHER].status = Status.MERGING
        assert evaluate(subset(changes, CREATES), doc=doc).ok is False

    def test_base_identity_conflict(self, parsed, evaluate, knowledge) -> None:
        changes, _, _ = parsed
        key = knowledge.identity_key("aws_s3_bucket", {"bucket": "s3-acme-dev-reports"})
        assert key
        result = evaluate(subset(changes, CREATES), base_identities={key})
        assert result.ok is False
        assert violated(result) == {"aws_s3_bucket.reports"}
        assert result.violations[0].other_overlay is None

    def test_own_previous_claims_do_not_conflict_with_self(self, parsed, evaluate) -> None:
        changes, _, _ = parsed
        assert evaluate(subset(changes, {"aws_sns_topic.reports", "aws_iam_role.app"})).ok is True


class TestEvaluateDrift:
    def test_drift_on_own_update_warns(self, parsed, evaluate) -> None:
        changes, drift, _ = parsed
        result = evaluate(subset(changes, {"aws_iam_role.app"}), drift)
        assert result.ok is True
        assert any("aws_iam_role.app" in w and "rebase" in w.lower() for w in result.warnings)

    def test_drift_on_unclaimed_resource_is_silent(self, parsed, evaluate) -> None:
        changes, drift, _ = parsed
        other_drift = copy.deepcopy(drift)
        other_drift[0]["address"] = "aws_s3_bucket.assets"
        result = evaluate(subset(changes, {"aws_s3_bucket.reports"}), other_drift)
        assert not any("aws_s3_bucket.assets" in w for w in result.warnings)

    def test_drift_matching_recorded_hash_is_silent(self, parsed, evaluate, registry_doc) -> None:
        changes, drift, _ = parsed
        first = evaluate(subset(changes, {"aws_iam_role.app"}))
        doc = copy.deepcopy(registry_doc)
        doc.overlays[ME].claims["aws_iam_role.app"].after_hash = first.claims[
            "aws_iam_role.app"
        ].after_hash
        same_drift = copy.deepcopy(drift)
        same_drift[0]["change"]["after"] = subset(changes, {"aws_iam_role.app"})[0].after
        result = evaluate(subset(changes, {"aws_iam_role.app"}), same_drift, doc=doc)
        assert not any("aws_iam_role.app" in w for w in result.warnings)


LAMBDA_BEFORE = {
    "function_name": "fct-acme-dev-reports",
    "filename": None,
    "source_code_hash": "old==",
    "memory_size": 256,
    "publish": False,
}


@pytest.fixture
def merge_me(make_overlay, make_claim):
    return make_overlay(
        status=Status.ACTIVE,
        claims={
            "aws_s3_bucket.reports": make_claim(
                "create",
                "aws_s3_bucket",
                {"bucket": "s3-acme-dev-reports"},
                id_="s3-acme-dev-reports",
                import_id="s3-acme-dev-reports",
            ),
            "aws_lambda_function.reports": make_claim(
                "create",
                "aws_lambda_function",
                {"function_name": "fct-acme-dev-reports"},
                id_="fct-acme-dev-reports",
                import_id="fct-acme-dev-reports",
            ),
            "aws_iam_role.app": make_claim(
                "update", "aws_iam_role", {"name": "iam-acme-dev-app"}, after_hash="ab" * 32
            ),
        },
    )


def importing_noop(address: str, id_: str, values: dict) -> ResourceChange:
    return mk_change(address, ["no-op"], before=values, after=values, importing={"id": id_})


class TestVerifyImportPlan:
    def base_changes(self) -> list[ResourceChange]:
        return [
            importing_noop(
                "aws_s3_bucket.reports",
                "s3-acme-dev-reports",
                {"bucket": "s3-acme-dev-reports", "id": "s3-acme-dev-reports"},
            ),
            importing_noop("aws_lambda_function.reports", "fct-acme-dev-reports", LAMBDA_BEFORE),
            mk_change(
                "aws_s3_bucket.assets",
                ["no-op"],
                before={"bucket": "s3-acme-dev-assets"},
                after={"bucket": "s3-acme-dev-assets"},
            ),
        ]

    def test_all_noop(self, merge_me, knowledge) -> None:
        ok, errors, warnings = plan.verify_import_plan(
            self.base_changes(), me=merge_me, knowledge=knowledge, allow_import_updates=False
        )
        assert ok is True
        assert errors == []
        assert warnings == []

    def test_update_with_only_virtual_attrs_ok(self, merge_me, knowledge) -> None:
        changes = self.base_changes()
        changes[1] = mk_change(
            "aws_lambda_function.reports",
            ["update"],
            before=LAMBDA_BEFORE,
            after=dict(
                LAMBDA_BEFORE, filename="build/reports.zip", source_code_hash="new==", publish=True
            ),
            importing={"id": "fct-acme-dev-reports"},
        )
        ok, errors, _ = plan.verify_import_plan(
            changes, me=merge_me, knowledge=knowledge, allow_import_updates=False
        )
        assert ok is True, errors

    def test_unknown_computed_attrs_are_not_offenders(self, merge_me, knowledge) -> None:
        """after_unknown keys (version, last_modified...) are side-effects of virtual diffs."""
        changes = self.base_changes()
        changes[1] = mk_change(
            "aws_lambda_function.reports",
            ["update"],
            before=dict(LAMBDA_BEFORE, version="1", last_modified="2026-09-01T00:00:00Z"),
            after=dict(LAMBDA_BEFORE, filename="build/reports.zip", source_code_hash="new=="),
            after_unknown={"version": True, "last_modified": True, "qualified_arn": True},
            importing={"id": "fct-acme-dev-reports"},
        )
        ok, errors, _ = plan.verify_import_plan(
            changes, me=merge_me, knowledge=knowledge, allow_import_updates=False
        )
        assert ok is True, errors

    def test_accepted_recreate_expects_plain_create(self, merge_me, knowledge) -> None:
        changes = self.base_changes()
        changes[0] = mk_change(
            "aws_s3_bucket.reports", ["create"], after={"bucket": "s3-acme-dev-reports"}
        )
        ok, errors, _ = plan.verify_import_plan(
            changes, me=merge_me, knowledge=knowledge, allow_import_updates=False
        )
        assert ok is False
        assert any("no import block" in e for e in errors)
        ok, errors, warnings = plan.verify_import_plan(
            changes,
            me=merge_me,
            knowledge=knowledge,
            allow_import_updates=False,
            accepted_recreate={"aws_s3_bucket.reports"},
        )
        assert ok is True, errors
        assert any("recreated by the trunk" in w for w in warnings)
        # an accepted address that still imports, or plans anything else, is an error
        ok, errors, _ = plan.verify_import_plan(
            self.base_changes(),
            me=merge_me,
            knowledge=knowledge,
            allow_import_updates=False,
            accepted_recreate={"aws_s3_bucket.reports"},
        )
        assert ok is False

    def test_managed_resource_in_module_named_data_is_checked(self, merge_me, knowledge) -> None:
        show = {
            "format_version": "1.2",
            "resource_changes": [
                {
                    "address": "module.data.aws_rds_cluster.main",
                    "module_address": "module.data",
                    "mode": "managed",
                    "type": "aws_rds_cluster",
                    "name": "main",
                    "change": {"actions": ["delete"], "before": {"id": "c"}, "after": None},
                },
                {
                    "address": "module.data.data.aws_caller_identity.current",
                    "module_address": "module.data",
                    "mode": "data",
                    "type": "aws_caller_identity",
                    "name": "current",
                    "change": {"actions": ["read"], "before": None, "after": {}},
                },
            ],
        }
        changes, _drift, _summary = plan.parse_plan(show)
        assert [c.mode for c in changes] == ["managed", "data"]
        ok, errors, _ = plan.verify_import_plan(
            changes + self.base_changes(),
            me=merge_me,
            knowledge=knowledge,
            allow_import_updates=False,
        )
        assert ok is False
        assert any("module.data.aws_rds_cluster.main" in e for e in errors)
        assert not any("aws_caller_identity" in e for e in errors)

    def test_update_with_real_attr_fails(self, merge_me, knowledge) -> None:
        changes = self.base_changes()
        changes[1] = mk_change(
            "aws_lambda_function.reports",
            ["update"],
            before=LAMBDA_BEFORE,
            after=dict(LAMBDA_BEFORE, filename="build/reports.zip", memory_size=512),
            importing={"id": "fct-acme-dev-reports"},
        )
        ok, errors, _ = plan.verify_import_plan(
            changes, me=merge_me, knowledge=knowledge, allow_import_updates=True
        )
        assert ok is False
        assert any("aws_lambda_function.reports" in e and "memory_size" in e for e in errors)

    def test_update_with_replace_paths_fails(self, merge_me, knowledge) -> None:
        changes = self.base_changes()
        changes[1] = mk_change(
            "aws_lambda_function.reports",
            ["update"],
            before=LAMBDA_BEFORE,
            after=dict(LAMBDA_BEFORE, filename="x.zip"),
            importing={"id": "fct-acme-dev-reports"},
            replace_paths=[["filename"]],
        )
        ok, errors, _ = plan.verify_import_plan(
            changes, me=merge_me, knowledge=knowledge, allow_import_updates=False
        )
        assert ok is False
        assert any("aws_lambda_function.reports" in e for e in errors)

    def test_replace_fails(self, merge_me, knowledge) -> None:
        changes = self.base_changes()
        changes[1] = mk_change(
            "aws_lambda_function.reports",
            ["delete", "create"],
            before=LAMBDA_BEFORE,
            after=dict(LAMBDA_BEFORE, function_name="fct-acme-dev-reports-v2"),
            importing={"id": "fct-acme-dev-reports"},
            replace_paths=[["function_name"]],
        )
        ok, errors, _ = plan.verify_import_plan(
            changes, me=merge_me, knowledge=knowledge, allow_import_updates=True
        )
        assert ok is False
        assert any("aws_lambda_function.reports" in e for e in errors)

    def test_create_claim_without_importing_fails(self, merge_me, knowledge) -> None:
        changes = self.base_changes()
        changes[0] = mk_change(
            "aws_s3_bucket.reports", ["create"], after={"bucket": "s3-acme-dev-reports"}
        )
        ok, errors, _ = plan.verify_import_plan(
            changes, me=merge_me, knowledge=knowledge, allow_import_updates=False
        )
        assert ok is False
        assert any("aws_s3_bucket.reports" in e for e in errors)

    def test_create_claim_missing_from_plan_fails(self, merge_me, knowledge) -> None:
        changes = self.base_changes()[1:]
        ok, errors, _ = plan.verify_import_plan(
            changes, me=merge_me, knowledge=knowledge, allow_import_updates=False
        )
        assert ok is False
        assert any("aws_s3_bucket.reports" in e for e in errors)

    def test_update_claim_address_may_update(self, merge_me, knowledge) -> None:
        changes = self.base_changes() + [
            mk_change(
                "aws_iam_role.app",
                ["update"],
                before={"description": "a"},
                after={"description": "b"},
            )
        ]
        ok, errors, warnings = plan.verify_import_plan(
            changes, me=merge_me, knowledge=knowledge, allow_import_updates=False
        )
        assert ok is True, errors
        assert warnings == []

    def test_foreign_update_fails_unless_allowed(self, merge_me, knowledge) -> None:
        changes = self.base_changes()
        changes[2] = mk_change(
            "aws_s3_bucket.assets", ["update"], before={"tags": {}}, after={"tags": {"env": "dev"}}
        )
        ok, errors, _ = plan.verify_import_plan(
            changes, me=merge_me, knowledge=knowledge, allow_import_updates=False
        )
        assert ok is False
        assert any("aws_s3_bucket.assets" in e for e in errors)
        ok, errors, warnings = plan.verify_import_plan(
            changes, me=merge_me, knowledge=knowledge, allow_import_updates=True
        )
        assert ok is True
        assert errors == []
        assert any("aws_s3_bucket.assets" in w for w in warnings)

    @pytest.mark.parametrize(
        "actions", [["delete"], ["forget"], ["delete", "create"], ["create", "delete"]]
    )
    def test_destructive_anywhere_fails(self, merge_me, knowledge, actions: list[str]) -> None:
        changes = self.base_changes() + [
            mk_change(
                "aws_s3_bucket.logs", actions, before={"bucket": "s3-acme-dev-logs"}, after=None
            )
        ]
        ok, errors, _ = plan.verify_import_plan(
            changes, me=merge_me, knowledge=knowledge, allow_import_updates=True
        )
        assert ok is False
        assert any("aws_s3_bucket.logs" in e for e in errors)

    def test_unknown_type_strict_noop(self, make_overlay, make_claim, knowledge) -> None:
        me = make_overlay(
            claims={
                "acme_widget.w": make_claim(
                    "create", "acme_widget", {"name": "w"}, id_="w-1", import_id="w-1"
                )
            }
        )
        ok, _, _ = plan.verify_import_plan(
            [importing_noop("acme_widget.w", "w-1", {"id": "w-1", "name": "w"})],
            me=me,
            knowledge=knowledge,
            allow_import_updates=False,
        )
        assert ok is True
        changed = mk_change(
            "acme_widget.w",
            ["update"],
            before={"id": "w-1", "force": False},
            after={"id": "w-1", "force": True},
            importing={"id": "w-1"},
        )
        ok, errors, _ = plan.verify_import_plan(
            [changed], me=me, knowledge=knowledge, allow_import_updates=False
        )
        assert ok is False


class TestGuardTrunkPlan:
    def test_clean_trunk_plan(self, registry_doc, knowledge) -> None:
        changes = [
            mk_change("aws_s3_bucket.logs", ["delete"], before={"bucket": "s3-acme-dev-logs"}),
            mk_change("aws_s3_bucket.other", ["create"], after={"bucket": "s3-acme-dev-other"}),
            mk_change(
                "aws_iam_role.app",
                ["update"],
                before={"description": "a"},
                after={"description": "b"},
            ),
        ]
        assert (
            plan.guard_trunk_plan(
                changes, doc=registry_doc, knowledge=knowledge, overlay_states=None
            )
            == []
        )

    def test_create_of_claimed_identity(self, registry_doc, knowledge) -> None:
        changes = [
            mk_change(
                "aws_s3_bucket.audit_trunk",
                ["create"],
                after={"bucket": "s3-acme-dev-audit", "tags": {}},
            ),
            mk_change("aws_sns_topic.trunk", ["create"], after={"name": "sns-acme-dev-reports"}),
        ]
        violations = plan.guard_trunk_plan(
            changes, doc=registry_doc, knowledge=knowledge, overlay_states=None
        )
        by_addr = {v.address: v for v in violations}
        assert set(by_addr) == {"aws_s3_bucket.audit_trunk", "aws_sns_topic.trunk"}
        assert by_addr["aws_s3_bucket.audit_trunk"].other_overlay == OTHER
        assert by_addr["aws_sns_topic.trunk"].other_overlay == ME

    @pytest.mark.parametrize("actions", [["delete"], ["delete", "create"], ["create", "delete"]])
    def test_destroying_address_under_update_claim(
        self, registry_doc, knowledge, actions: list[str]
    ) -> None:
        changes = [
            mk_change("aws_iam_role.app", actions, before={"name": "iam-acme-dev-app"}, after=None)
        ]
        violations = plan.guard_trunk_plan(
            changes, doc=registry_doc, knowledge=knowledge, overlay_states=None
        )
        assert [v.address for v in violations] == ["aws_iam_role.app"]
        assert violations[0].other_overlay == ME

    def test_delete_of_overlay_dependency(
        self, registry_doc, knowledge, state_overlay, make_claim
    ) -> None:
        registry_doc = copy.deepcopy(registry_doc)
        registry_doc.overlays[ME].claims["module.net.aws_security_group.reports"] = make_claim(
            "create",
            "aws_security_group",
            {"name": "nsg-acme-dev-reports", "vpc_id": "vpc-0123456789abcdef0"},
            id_="sg-0abcdef1234567890",
            import_id="sg-0abcdef1234567890",
        )
        changes = [
            mk_change(
                "module.net.aws_security_group.app",
                ["delete"],
                before={"name": "nsg-acme-dev-app"},
                module_address="module.net",
            )
        ]
        assert (
            plan.guard_trunk_plan(
                changes, doc=registry_doc, knowledge=knowledge, overlay_states=None
            )
            == []
        )
        violations = plan.guard_trunk_plan(
            changes, doc=registry_doc, knowledge=knowledge, overlay_states={ME: state_overlay}
        )
        assert [v.address for v in violations] == ["module.net.aws_security_group.app"]
        assert violations[0].other_overlay == ME

    def test_delete_of_dependency_recorded_in_claim(
        self, registry_doc, knowledge, make_claim
    ) -> None:
        """The trunk pipeline only has the registry: dependencies live in the claims."""
        registry_doc = copy.deepcopy(registry_doc)
        claim = make_claim(
            "create",
            "aws_iam_role_policy_attachment",
            {"role": "iam-acme-dev-base", "policy_arn": "arn:aws:iam::aws:policy/ReadOnlyAccess"},
            id_="iam-acme-dev-base-20260901",
        )
        registry_doc.overlays[ME].claims["aws_iam_role_policy_attachment.own"] = claim.model_copy(
            update={"dependencies": ["aws_iam_role.base", "aws_instance.bastion[0]"]}
        )
        changes = [
            mk_change("aws_iam_role.base", ["delete"], before={"name": "iam-acme-dev-base"}),
            mk_change("aws_instance.bastion[1]", ["delete"], before={"id": "i-1"}),
        ]
        violations = plan.guard_trunk_plan(
            changes, doc=registry_doc, knowledge=knowledge, overlay_states=None
        )
        assert [(v.address, v.rule) for v in violations] == [
            ("aws_iam_role.base", "dependency"),
            ("aws_instance.bastion[1]", "dependency"),
        ]
        assert violations[0].other_overlay == ME

    def test_dead_overlays_ignored(self, registry_doc, knowledge) -> None:
        doc = copy.deepcopy(registry_doc)
        doc.overlays[OTHER].status = Status.MERGED
        changes = [
            mk_change(
                "aws_s3_bucket.audit_trunk", ["create"], after={"bucket": "s3-acme-dev-audit"}
            )
        ]
        assert (
            plan.guard_trunk_plan(changes, doc=doc, knowledge=knowledge, overlay_states=None) == []
        )


class TestIdentityOf:
    def test_before_identity_uses_before_sensitive(self, knowledge) -> None:
        change = ResourceChange(
            address="aws_ssm_parameter.token",
            type="aws_ssm_parameter",
            name="token",
            actions=["update"],
            before={"name": "/acme/token", "value": "old-secret"},
            after={"name": "/acme/token", "value": "new-secret"},
            before_sensitive={"value": True},
            after_sensitive={"value": True, "name": True},
        )
        assert plan._identity_of(change, knowledge, source="before") == {"name": "/acme/token"}
        assert plan._identity_of(change, knowledge, source="after") == {}


# ---------------------------------------------------------------- drift / ignored


LAMBDA_BEFORE = {
    "id": "fct-acme-dev-worker",
    "function_name": "fct-acme-dev-worker",
    "filename": ".terraform/modules/worker/code.zip",
    "last_modified": "2026-09-01T00:00:00Z",
    "memory_size": 256,
}


def _update(address: str, before: dict, after: dict, **kwargs: Any) -> ResourceChange:
    return mk_change(address, ["update"], before=before, after=after, **kwargs)


class TestIgnoredAttributesAndTrunkDrift:
    """DESIGN §7.7 (environment-dependent attributes) and §7.8 (trunk drift)."""

    def test_update_touching_only_ignored_attributes_is_not_claimed(self, evaluate) -> None:
        after = {**LAMBDA_BEFORE, "filename": ".tofu-overlay/x/modules/worker/code.zip"}
        change = _update(
            "aws_lambda_function.worker", LAMBDA_BEFORE, after,
            after_unknown={"last_modified": True},
        )
        result = evaluate([change])
        assert result.ok
        assert result.ignored == ["aws_lambda_function.worker"]
        assert "aws_lambda_function.worker" not in result.claims
        assert result.drift == []
        assert any("environment-dependent" in w for w in result.warnings)

    def test_wildcard_last_modified_applies_to_every_type(self, evaluate) -> None:
        before = {"id": "sqs-acme-dev-events", "name": "sqs-acme-dev-events", "last_modified": "a"}
        after = {**before, "last_modified": "b"}
        result = evaluate([_update("aws_sqs_queue.events", before, after)])
        assert result.ignored == ["aws_sqs_queue.events"]
        assert "aws_sqs_queue.events" not in result.claims

    def test_real_attribute_change_alongside_ignored_ones_is_claimed(self, evaluate) -> None:
        after = {**LAMBDA_BEFORE, "filename": "elsewhere.zip", "memory_size": 512}
        result = evaluate([_update("aws_lambda_function.worker", LAMBDA_BEFORE, after)])
        assert result.ignored == []
        claim = result.claims["aws_lambda_function.worker"]
        assert claim.kind is ClaimKind.UPDATE

    def test_ignored_rule_runs_before_drift(self, evaluate) -> None:
        after = {**LAMBDA_BEFORE, "filename": "elsewhere.zip"}
        change = _update("aws_lambda_function.worker", LAMBDA_BEFORE, after)
        result = evaluate([change], trunk_drift={"aws_lambda_function.worker": ["update"]})
        assert result.ignored == ["aws_lambda_function.worker"]
        assert result.drift == []

    def test_update_in_trunk_drift_is_not_claimed(self, evaluate) -> None:
        before = {"id": "s3-acme-dev-logs", "bucket": "s3-acme-dev-logs", "tags": {}}
        change = _update("aws_s3_bucket.logs", before, {**before, "tags": {"env": "dev"}})
        result = evaluate([change], trunk_drift={"aws_s3_bucket.logs": ["update"]})
        assert result.ok
        assert result.drift == ["aws_s3_bucket.logs"]
        assert "aws_s3_bucket.logs" not in result.claims
        assert any("trunk is not applied" in w and "--accept-drift" in w for w in result.warnings)

    def test_without_baseline_the_update_is_claimed(self, evaluate) -> None:
        before = {"id": "s3-acme-dev-logs", "bucket": "s3-acme-dev-logs", "tags": {}}
        change = _update("aws_s3_bucket.logs", before, {**before, "tags": {"env": "dev"}})
        for trunk_drift in (None, {}, {"aws_s3_bucket.assets": ["update"]}):
            result = evaluate([change], trunk_drift=trunk_drift)
            assert result.drift == []
            assert result.claims["aws_s3_bucket.logs"].kind is ClaimKind.UPDATE

    def test_existing_update_claim_stays_claimed_despite_drift(self, evaluate) -> None:
        before = {"id": "iam-acme-dev-app", "name": "iam-acme-dev-app", "description": "a"}
        change = _update("aws_iam_role.app", before, {**before, "description": "b"})
        result = evaluate([change], trunk_drift={"aws_iam_role.app": ["update"]})
        assert result.drift == []
        assert result.claims["aws_iam_role.app"].kind is ClaimKind.UPDATE

    def test_delete_and_replace_in_drift_stay_denied(self, evaluate) -> None:
        attrs = {"id": "s3-acme-dev-logs", "bucket": "s3-acme-dev-logs"}
        changes = [
            mk_change("aws_s3_bucket.logs", ["delete"], before=attrs),
            mk_change("aws_sqs_queue.events", ["delete", "create"], before=attrs, after=attrs),
        ]
        drift = {"aws_s3_bucket.logs": ["delete"], "aws_sqs_queue.events": ["delete", "create"]}
        result = evaluate(changes, trunk_drift=drift)
        assert violated(result) == {"aws_s3_bucket.logs", "aws_sqs_queue.events"}
        assert {v.rule for v in result.violations} == {"destructive"}
        assert result.drift == []

    def test_own_resource_update_is_never_drift_nor_ignored(self, evaluate) -> None:
        before = {"id": "reports", "name": "reports", "last_modified": "a"}
        change = _update("aws_sns_topic.reports", before, {**before, "last_modified": "b"})
        result = evaluate([change], trunk_drift={"aws_sns_topic.reports": ["update"]})
        assert result.ok and result.drift == [] and result.ignored == []
        assert result.claims["aws_sns_topic.reports"].kind is ClaimKind.CREATE

    def test_result_lists_are_sorted_and_serialised(self, evaluate) -> None:
        before = {"id": "x", "bucket": "x", "tags": {}}
        changes = [
            _update("aws_s3_bucket.logs", before, {**before, "tags": {"a": "1"}}),
            _update("aws_s3_bucket.assets", before, {**before, "tags": {"a": "1"}}),
        ]
        drift = {"aws_s3_bucket.logs": ["update"], "aws_s3_bucket.assets": ["update"]}
        result = evaluate(changes, trunk_drift=drift)
        assert result.drift == ["aws_s3_bucket.assets", "aws_s3_bucket.logs"]
        dumped = result.model_dump(mode="json")
        assert dumped["drift"] == result.drift and dumped["ignored"] == []
