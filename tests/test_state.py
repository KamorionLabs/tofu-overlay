"""Tests for tofu_overlay.state: addressing, fork/inject/remove, serial, sensitive stripping."""

from __future__ import annotations

import copy
import re

import pytest

from tests.conftest import BASE_LINEAGE
from tofu_overlay import state
from tofu_overlay.models import ExitCode, OverlayError, ToolError

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
OVERLAY_ONLY = {
    'aws_ssm_parameter.flags["c"]',
    "aws_s3_bucket.reports",
    "aws_cloudwatch_log_group.reports",
    "aws_sns_topic.reports",
    "module.net.aws_security_group.reports",
}


def entry(doc: dict, type_: str, name: str, module: str | None = None) -> dict:
    for r in doc["resources"]:
        if (
            r["type"] == type_
            and r["name"] == name
            and r.get("module") == module
            and r["mode"] == "managed"
        ):
            return r
    raise KeyError((module, type_, name))


class TestAddresses:
    def test_address_of_root(self) -> None:
        res = {"mode": "managed", "type": "aws_iam_role", "name": "app"}
        assert state.address_of(res, {"schema_version": 1}) == "aws_iam_role.app"

    def test_address_of_for_each(self) -> None:
        res = {"mode": "managed", "type": "aws_ssm_parameter", "name": "flags"}
        assert state.address_of(res, {"index_key": "a"}) == 'aws_ssm_parameter.flags["a"]'

    def test_address_of_count(self) -> None:
        res = {"mode": "managed", "type": "aws_instance", "name": "bastion"}
        assert state.address_of(res, {"index_key": 0}) == "aws_instance.bastion[0]"
        assert state.address_of(res, {"index_key": 1}) == "aws_instance.bastion[1]"

    def test_address_of_module(self) -> None:
        res = {
            "module": "module.net",
            "mode": "managed",
            "type": "aws_security_group",
            "name": "app",
        }
        assert state.address_of(res, {}) == "module.net.aws_security_group.app"
        nested = dict(res, module='module.net.module.child["eu"]')
        assert state.address_of(nested, {"index_key": "x"}) == (
            'module.net.module.child["eu"].aws_security_group.app["x"]'
        )

    def test_address_of_data_source(self) -> None:
        res = {"mode": "data", "type": "aws_caller_identity", "name": "current"}
        assert state.address_of(res, {}) == "data.aws_caller_identity.current"

    def test_address_of_ignores_deposed(self) -> None:
        res = {"mode": "managed", "type": "aws_sqs_queue", "name": "events"}
        assert state.address_of(res, {"deposed": "3c4d5e6f"}) == "aws_sqs_queue.events"

    def test_addresses_and_index(self, state_base: dict, state_overlay: dict) -> None:
        managed = {a for a in state.addresses(state_base) if not a.startswith("data.")}
        assert managed == BASE_ADDRESSES
        overlay_managed = {a for a in state.addresses(state_overlay) if not a.startswith("data.")}
        assert overlay_managed == BASE_ADDRESSES | OVERLAY_ONLY
        idx = state.index_instances(state_overlay)
        res, inst = idx['aws_ssm_parameter.flags["c"]']
        assert res["type"] == "aws_ssm_parameter"
        assert inst["index_key"] == "c"
        assert inst["attributes"]["name"] == "/acme/dev/flags/c"
        res, inst = idx["module.net.aws_security_group.reports"]
        assert res["module"] == "module.net"
        assert inst["attributes"]["id"] == "sg-0abcdef1234567890"


class TestLineageAndFork:
    def test_is_encrypted(self, state_base: dict) -> None:
        assert state.is_encrypted(state_base) is False
        assert state.is_encrypted({"encrypted_data": "AAAA", "meta": {}}) is True

    def test_new_lineage(self) -> None:
        a, b = state.new_lineage(), state.new_lineage()
        assert re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", a)
        assert a != b

    def test_fork(self, state_base: dict) -> None:
        snapshot = copy.deepcopy(state_base)
        forked = state.fork(state_base)
        assert forked["lineage"] != BASE_LINEAGE
        assert forked["serial"] == 0
        assert forked["version"] == 4
        assert forked["resources"] == state_base["resources"]
        assert forked["outputs"] == state_base["outputs"]
        # deep copy: the source is untouched by later mutations
        forked["resources"][0]["name"] = "mutated"
        assert state_base == snapshot

    def test_bump_serial(self, state_base: dict) -> None:
        assert state.bump_serial(copy.deepcopy(state_base), 3)["serial"] == 413
        assert state.bump_serial(copy.deepcopy(state_base), 412)["serial"] == 413
        assert state.bump_serial(copy.deepcopy(state_base), 900)["serial"] == 901
        assert state.bump_serial({"serial": 0}, 0)["serial"] == 1


class TestInjectRemove:
    def test_inject_overlay_creates_into_base(self, state_base: dict, state_overlay: dict) -> None:
        merged = state.inject_instances(copy.deepcopy(state_base), state_overlay, OVERLAY_ONLY)
        managed = {a for a in state.addresses(merged) if not a.startswith("data.")}
        assert managed == BASE_ADDRESSES | OVERLAY_ONLY
        # for_each instance merged into the existing resource entry, not duplicated
        flags_entries = [
            r
            for r in merged["resources"]
            if r["type"] == "aws_ssm_parameter" and r["name"] == "flags"
        ]
        assert len(flags_entries) == 1
        assert sorted(i["index_key"] for i in flags_entries[0]["instances"]) == ["a", "b", "c"]
        # base content untouched (the overlay's update on the role is not injected)
        assert (
            entry(merged, "aws_iam_role", "app")["instances"][0]["attributes"]["description"]
            == "Application role"
        )
        # module resource lands in the module entry with dependencies preserved
        sg = entry(merged, "aws_security_group", "reports", module="module.net")
        assert (
            sg["provider"]
            == entry(state_base, "aws_security_group", "app", module="module.net")["provider"]
        )
        assert sg["instances"][0]["dependencies"] == ["module.net.aws_security_group.app"]

    def test_inject_refuses_existing_address(self, state_base: dict, state_overlay: dict) -> None:
        with pytest.raises(ToolError) as exc:
            state.inject_instances(copy.deepcopy(state_base), state_overlay, {"aws_iam_role.app"})
        assert exc.value.exit_code == ExitCode.ERROR
        assert "aws_iam_role.app" in str(exc.value)

    def test_inject_refuses_provider_mismatch(self, state_base: dict, state_overlay: dict) -> None:
        src = copy.deepcopy(state_overlay)
        entry(src, "aws_ssm_parameter", "flags")["provider"] = (
            'provider["registry.opentofu.org/acme/aws"]'
        )
        with pytest.raises(ToolError) as exc:
            state.inject_instances(copy.deepcopy(state_base), src, {'aws_ssm_parameter.flags["c"]'})
        assert "provider" in str(exc.value).lower()

    def test_inject_refuses_newer_schema_version(
        self, state_base: dict, state_overlay: dict
    ) -> None:
        src = copy.deepcopy(state_overlay)
        entry(src, "aws_security_group", "reports", module="module.net")["instances"][0][
            "schema_version"
        ] = 2
        with pytest.raises(ToolError) as exc:
            state.inject_instances(
                copy.deepcopy(state_base), src, {"module.net.aws_security_group.reports"}
            )
        assert "schema" in str(exc.value).lower()

    def test_inject_new_type_with_any_schema_version(
        self, state_base: dict, state_overlay: dict
    ) -> None:
        src = copy.deepcopy(state_overlay)
        entry(src, "aws_sns_topic", "reports")["instances"][0]["schema_version"] = 5
        merged = state.inject_instances(copy.deepcopy(state_base), src, {"aws_sns_topic.reports"})
        assert "aws_sns_topic.reports" in state.addresses(merged)

    def test_remove_addresses(self, state_overlay: dict) -> None:
        doc = state.remove_addresses(copy.deepcopy(state_overlay), BASE_ADDRESSES)
        managed = {a for a in state.addresses(doc) if not a.startswith("data.")}
        assert managed == OVERLAY_ONLY
        flags = entry(doc, "aws_ssm_parameter", "flags")
        assert [i["index_key"] for i in flags["instances"]] == ["c"]
        assert not any(len(r["instances"]) == 0 for r in doc["resources"])

    def test_remove_unknown_address_is_noop(self, state_base: dict) -> None:
        doc = state.remove_addresses(copy.deepcopy(state_base), {"aws_s3_bucket.nope"})
        assert state.addresses(doc) == state.addresses(state_base)

    def test_inject_then_remove_roundtrip(self, state_base: dict, state_overlay: dict) -> None:
        merged = state.inject_instances(copy.deepcopy(state_base), state_overlay, OVERLAY_ONLY)
        back = state.remove_addresses(merged, OVERLAY_ONLY)
        assert state.addresses(back) == state.addresses(state_base)


class TestAttributesAndIdentity:
    def test_attribute_path(self) -> None:
        inst = {
            "schema_version": 0,
            "attributes": {
                "metadata": [{"name": "reports", "namespace": "acme"}],
                "id": "acme/reports",
            },
        }
        assert state.attribute(inst, "metadata.0.name") == "reports"
        assert state.attribute(inst, "metadata.0.namespace") == "acme"
        assert state.attribute(inst, "id") == "acme/reports"
        assert state.attribute(inst, "metadata.1.name") is None
        assert state.attribute(inst, "missing.key") is None

    def test_identity_from_state(self, state_overlay: dict, knowledge) -> None:
        idx = state.index_instances(state_overlay)
        _, bucket = idx["aws_s3_bucket.reports"]
        assert state.identity_from_state(bucket["attributes"], "aws_s3_bucket", knowledge) == {
            "bucket": "s3-acme-dev-reports"
        }
        _, record = idx["aws_route53_record.www"]
        ident = state.identity_from_state(record["attributes"], "aws_route53_record", knowledge)
        assert ident["zone_id"] == "Z0123456789ABCDEFGHIJ"
        assert ident["name"] == "www.acme.example"
        assert ident["type"] == "A"


class TestStripSensitive:
    def test_flat(self) -> None:
        after = {"name": "/acme/dev/token", "type": "SecureString", "value": "s3cr3t"}
        assert state.strip_sensitive(after, {"value": True}) == {
            "name": "/acme/dev/token",
            "type": "SecureString",
        }

    def test_nested_and_lists(self) -> None:
        after = {
            "config": {"user": "u", "password": "p"},
            "items": [{"id": 1, "secret": "a"}, {"id": 2, "secret": "b"}],
            "tags": {"env": "dev"},
        }
        sensitive = {
            "config": {"password": True},
            "items": [{"secret": True}, {"secret": True}],
            "tags": {},
        }
        assert state.strip_sensitive(after, sensitive) == {
            "config": {"user": "u"},
            "items": [{"id": 1}, {"id": 2}],
            "tags": {"env": "dev"},
        }

    def test_nothing_sensitive(self) -> None:
        after = {"bucket": "s3-acme-dev-reports", "tags": {"env": "dev"}}
        assert state.strip_sensitive(after, {}) == after
        assert state.strip_sensitive(after, False) == after
        assert state.strip_sensitive(None, False) is None

    def test_does_not_mutate_input(self) -> None:
        after = {"name": "x", "value": "s"}
        snapshot = copy.deepcopy(after)
        state.strip_sensitive(after, {"value": True})
        assert after == snapshot

    def test_error_type(self) -> None:
        assert issubclass(ToolError, OverlayError)
