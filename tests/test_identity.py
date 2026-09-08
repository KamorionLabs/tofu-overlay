"""Tests for tofu_overlay.identity: type knowledge, identities, import ids, overrides."""

from __future__ import annotations

import pytest

from tofu_overlay.identity import TypeKnowledge
from tofu_overlay.models import PolicyConfig, ToolConfig

WAF_ARN = "arn:aws:wafv2:eu-west-1:123456789012:regional/webacl/w/1"
ALB_ARN = "arn:aws:elasticloadbalancing:eu-west-1:123456789012:loadbalancer/app/x/1"


@pytest.fixture
def overridden() -> TypeKnowledge:
    cfg = ToolConfig(
        policy=PolicyConfig(),
        identity={"aws_s3_bucket": ["arn"], "acme_widget": ["widget_name"]},
        import_ids={"aws_s3_bucket": "{arn}", "acme_widget": "{widget_name}"},
        virtual_attributes={"aws_s3_bucket": ["tags"], "acme_widget": ["force"]},
        non_importable=["acme_ephemeral_*"],
        replace_prone=["acme_widget"],
    )
    return TypeKnowledge.load(cfg)


class TestPackageData:
    @pytest.mark.parametrize(
        "type_",
        [
            "aws_s3_bucket",
            "aws_iam_role",
            "aws_iam_policy",
            "aws_iam_role_policy",
            "aws_iam_role_policy_attachment",
            "aws_lambda_function",
            "aws_lambda_permission",
            "aws_route53_record",
            "aws_route53_zone",
            "aws_cloudwatch_log_group",
            "aws_cloudwatch_event_target",
            "aws_scheduler_schedule",
            "aws_sqs_queue",
            "aws_sns_topic",
            "aws_dynamodb_table",
            "aws_ecr_repository",
            "aws_secretsmanager_secret",
            "aws_ssm_parameter",
            "aws_kms_key",
            "aws_kms_alias",
            "aws_security_group",
            "aws_lb",
            "aws_lb_listener_rule",
            "aws_db_instance",
            "aws_rds_cluster",
            "aws_eks_cluster",
            "aws_eks_pod_identity_association",
            "aws_cloudfront_distribution",
            "aws_acm_certificate",
            "aws_wafv2_web_acl",
            "aws_wafv2_web_acl_association",
            "aws_api_gateway_rest_api",
            "aws_cognito_user_pool",
            "aws_s3_object",
            "aws_route",
            "aws_route_table_association",
            "aws_efs_file_system",
            "aws_kms_grant",
            "aws_cloudwatch_log_subscription_filter",
            "kubernetes_namespace",
            "kubernetes_service_account_v1",
            "helm_release",
        ],
    )
    def test_import_format_known(self, knowledge: TypeKnowledge, type_: str) -> None:
        assert knowledge.known(type_) is True

    def test_unknown_type(self, knowledge: TypeKnowledge) -> None:
        assert knowledge.known("acme_widget") is False

    @pytest.mark.parametrize(
        "type_",
        [
            "random_id",
            "random_password",
            "random_pet",
            "null_resource",
            "terraform_data",
            "time_sleep",
            "tls_private_key",
            "local_file",
            "archive_file",
            "aws_lambda_invocation",
            "aws_iam_policy_attachment",
            "aws_lb_target_group_attachment",
            "aws_dynamodb_table_item",
            "aws_iam_access_key",
            "aws_acm_certificate_validation",
            "kubernetes_manifest",
        ],
    )
    def test_non_importable(self, knowledge: TypeKnowledge, type_: str) -> None:
        assert knowledge.is_importable(type_) is False

    @pytest.mark.parametrize(
        "type_", ["aws_s3_bucket", "aws_iam_role", "helm_release", "acme_widget"]
    )
    def test_importable(self, knowledge: TypeKnowledge, type_: str) -> None:
        assert knowledge.is_importable(type_) is True

    def test_replace_prone(self, knowledge: TypeKnowledge) -> None:
        assert knowledge.is_replace_prone("aws_lambda_layer_version") is True
        assert knowledge.is_replace_prone("aws_s3_bucket") is False

    def test_virtual_attrs(self, knowledge: TypeKnowledge) -> None:
        assert {
            "filename",
            "source_code_hash",
            "publish",
            "skip_destroy",
        } <= knowledge.virtual_attrs("aws_lambda_function")
        assert "force_destroy" in knowledge.virtual_attrs("aws_s3_bucket")
        assert {
            "deletion_window_in_days",
            "bypass_policy_lockout_safety_check",
        } <= knowledge.virtual_attrs("aws_kms_key")
        assert {"master_password", "skip_final_snapshot"} <= knowledge.virtual_attrs(
            "aws_db_instance"
        )
        assert {"values", "set", "wait"} <= knowledge.virtual_attrs("helm_release")
        assert "wait_for_rollout" in knowledge.virtual_attrs("kubernetes_deployment_v1")
        assert knowledge.virtual_attrs("acme_widget") == set()

    def test_fallback_attrs_constant(self) -> None:
        assert TypeKnowledge.FALLBACK_IDENTITY_ATTRS[0] == "name"
        assert "bucket" in TypeKnowledge.FALLBACK_IDENTITY_ATTRS


class TestIdentityFor:
    def test_bucket(self, knowledge: TypeKnowledge) -> None:
        attrs = {
            "bucket": "s3-acme-dev-reports",
            "arn": "arn:aws:s3:::s3-acme-dev-reports",
            "tags": {},
        }
        assert knowledge.identity_for("aws_s3_bucket", attrs) == {"bucket": "s3-acme-dev-reports"}

    def test_route53_record_normalised(self, knowledge: TypeKnowledge) -> None:
        attrs = {
            "zone_id": "Z0123456789ABCDEFGHIJ",
            "name": "WWW.Acme.Example.",
            "type": "A",
            "set_identifier": None,
            "ttl": 300,
        }
        ident = knowledge.identity_for("aws_route53_record", attrs)
        assert ident["zone_id"] == "Z0123456789ABCDEFGHIJ"
        assert ident["name"] == "www.acme.example"
        assert ident["type"] == "A"
        with_set = knowledge.identity_for("aws_route53_record", dict(attrs, set_identifier="eu"))
        assert with_set["set_identifier"] == "eu"
        assert knowledge.identity_key("aws_route53_record", ident) != knowledge.identity_key(
            "aws_route53_record", with_set
        )

    def test_route53_equal_after_normalisation(self, knowledge: TypeKnowledge) -> None:
        a = knowledge.identity_for(
            "aws_route53_record",
            {"zone_id": "Z1", "name": "www.acme.example.", "type": "A", "set_identifier": None},
        )
        b = knowledge.identity_for(
            "aws_route53_record",
            {"zone_id": "Z1", "name": "WWW.ACME.EXAMPLE", "type": "A", "set_identifier": None},
        )
        assert knowledge.identity_key("aws_route53_record", a) == knowledge.identity_key(
            "aws_route53_record", b
        )

    def test_kubernetes_namespace_path(self, knowledge: TypeKnowledge) -> None:
        attrs = {"metadata": [{"name": "acme-reports", "labels": {}}], "id": "acme-reports"}
        assert knowledge.identity_for("kubernetes_namespace", attrs) == {
            "metadata.0.name": "acme-reports"
        }

    def test_fallback_order(self, knowledge: TypeKnowledge) -> None:
        assert knowledge.identity_for("acme_widget", {"bucket": "b", "name": "n"}) == {"name": "n"}
        assert knowledge.identity_for("acme_widget", {"function_name": "f", "key": "k"}) == {
            "function_name": "f"
        }
        assert knowledge.identity_for("acme_widget", {"id": "only-id"}) == {}

    def test_missing_attributes_give_empty(self, knowledge: TypeKnowledge) -> None:
        assert knowledge.identity_for("aws_s3_bucket", {"bucket": None, "tags": {}}) == {}
        assert knowledge.identity_for("aws_s3_bucket", {}) == {}

    def test_identity_key(self, knowledge: TypeKnowledge) -> None:
        assert knowledge.identity_key("aws_s3_bucket", {"bucket": "s3-acme-dev-reports"}) == (
            "aws_s3_bucket|bucket=s3-acme-dev-reports"
        )
        assert knowledge.identity_key("aws_s3_bucket", {}) is None
        key = knowledge.identity_key(
            "aws_route53_record", {"zone_id": "Z1", "name": "www.acme.example", "type": "A"}
        )
        assert key is not None
        parts = key.split("|")
        assert parts[0] == "aws_route53_record"
        assert set(parts[1:]) == {"zone_id=Z1", "name=www.acme.example", "type=A"}
        # canonical: independent of dict insertion order
        assert key == knowledge.identity_key(
            "aws_route53_record", {"type": "A", "name": "www.acme.example", "zone_id": "Z1"}
        )
        assert knowledge.identity_key("aws_s3_bucket", {"bucket": "x"}) != knowledge.identity_key(
            "aws_s3_bucket_policy", {"bucket": "x"}
        )


class TestImportIdFor:
    @pytest.mark.parametrize(
        ("type_", "attrs", "expected"),
        [
            (
                "aws_s3_bucket",
                {"id": "s3-acme-dev-reports", "bucket": "s3-acme-dev-reports"},
                "s3-acme-dev-reports",
            ),
            (
                "aws_iam_role_policy",
                {"role": "iam-acme-dev-app", "name": "inline"},
                "iam-acme-dev-app:inline",
            ),
            (
                "aws_iam_role_policy_attachment",
                {
                    "role": "iam-acme-dev-app",
                    "policy_arn": "arn:aws:iam::aws:policy/ReadOnlyAccess",
                },
                "iam-acme-dev-app/arn:aws:iam::aws:policy/ReadOnlyAccess",
            ),
            (
                "aws_lambda_permission",
                {"function_name": "fct-acme-dev-reports", "statement_id": "AllowS3"},
                "fct-acme-dev-reports/AllowS3",
            ),
            (
                "aws_scheduler_schedule",
                {"group_name": "default", "name": "sch-acme-dev-nightly"},
                "default/sch-acme-dev-nightly",
            ),
            (
                "aws_cloudwatch_event_target",
                {"event_bus_name": "default", "rule": "r", "target_id": "t"},
                "default/r/t",
            ),
            (
                "aws_s3_object",
                {"bucket": "s3-acme-dev-reports", "key": "reports/2026/x.csv"},
                "s3-acme-dev-reports/reports/2026/x.csv",
            ),
            (
                "aws_route",
                {"route_table_id": "rtb-0123", "destination_cidr_block": "10.1.0.0/16"},
                "rtb-0123_10.1.0.0/16",
            ),
            (
                "aws_route_table_association",
                {"subnet_id": "subnet-0123", "route_table_id": "rtb-0123"},
                "subnet-0123/rtb-0123",
            ),
            ("aws_kms_grant", {"key_id": "k", "grant_id": "g"}, "k:g"),
            (
                "aws_cloudwatch_log_subscription_filter",
                {"log_group_name": "/aws/lambda/x", "name": "f"},
                "/aws/lambda/x|f",
            ),
            (
                "aws_wafv2_web_acl",
                {"id": "0f1e2d3c", "name": "waf-acme-dev", "scope": "REGIONAL"},
                "0f1e2d3c/waf-acme-dev/REGIONAL",
            ),
            (
                "aws_wafv2_web_acl_association",
                {"web_acl_arn": WAF_ARN, "resource_arn": ALB_ARN},
                f"{WAF_ARN},{ALB_ARN}",
            ),
            (
                "aws_eks_pod_identity_association",
                {"cluster_name": "k8s-acme-dev", "association_id": "a-0123"},
                "k8s-acme-dev,a-0123",
            ),
            (
                "kubernetes_service_account_v1",
                {"metadata": [{"namespace": "acme", "name": "reports"}]},
                "acme/reports",
            ),
            ("helm_release", {"namespace": "acme", "name": "reports"}, "acme/reports"),
            ("acme_widget", {"id": "w-1", "name": "n"}, "w-1"),
        ],
    )
    def test_formats(
        self, knowledge: TypeKnowledge, type_: str, attrs: dict, expected: str
    ) -> None:
        assert knowledge.import_id_for(type_, attrs) == expected

    def test_route53_record_with_and_without_set_identifier(self, knowledge: TypeKnowledge) -> None:
        attrs = {"zone_id": "Z0123456789ABCDEFGHIJ", "name": "www.acme.example", "type": "A"}
        assert (
            knowledge.import_id_for("aws_route53_record", attrs)
            == "Z0123456789ABCDEFGHIJ_www.acme.example_A"
        )
        assert (
            knowledge.import_id_for("aws_route53_record", dict(attrs, set_identifier="eu"))
            == "Z0123456789ABCDEFGHIJ_www.acme.example_A_eu"
        )

    def test_empty_optional_group_is_dropped(self, knowledge: TypeKnowledge) -> None:
        # SDKv2 stores unset optional strings as "": the optional group must vanish.
        attrs = {"zone_id": "Z1", "name": "www.acme.example", "type": "A", "set_identifier": ""}
        assert knowledge.import_id_for("aws_route53_record", attrs) == "Z1_www.acme.example_A"
        unset = {"function_name": "fn", "qualifier": ""}
        assert knowledge.import_id_for("aws_lambda_function_url", unset) == "fn"
        assert knowledge.import_id_for("aws_lambda_function_event_invoke_config", unset) == "fn"
        live = {"function_name": "fn", "qualifier": "live"}
        assert knowledge.import_id_for("aws_lambda_function_url", live) == "fn/live"

    def test_missing_placeholder_is_none(self, knowledge: TypeKnowledge) -> None:
        assert knowledge.import_id_for("aws_iam_role_policy", {"role": "iam-acme-dev-app"}) is None
        assert knowledge.import_id_for("acme_widget", {"name": "n"}) is None
        assert knowledge.import_id_for("aws_s3_object", {"bucket": "b", "key": None}) is None


class TestOverrides:
    def test_identity_override_wins(self, overridden: TypeKnowledge) -> None:
        attrs = {"bucket": "s3-acme-dev-reports", "arn": "arn:aws:s3:::s3-acme-dev-reports"}
        assert overridden.identity_for("aws_s3_bucket", attrs) == {
            "arn": "arn:aws:s3:::s3-acme-dev-reports"
        }
        assert overridden.identity_for("acme_widget", {"widget_name": "w", "name": "n"}) == {
            "widget_name": "w"
        }

    def test_import_override_wins(self, overridden: TypeKnowledge) -> None:
        attrs = {"id": "s3-acme-dev-reports", "arn": "arn:aws:s3:::s3-acme-dev-reports"}
        assert (
            overridden.import_id_for("aws_s3_bucket", attrs) == "arn:aws:s3:::s3-acme-dev-reports"
        )
        assert overridden.import_id_for("acme_widget", {"widget_name": "w"}) == "w"
        assert overridden.known("acme_widget") is True

    def test_virtual_attrs_override(self, overridden: TypeKnowledge) -> None:
        assert "tags" in overridden.virtual_attrs("aws_s3_bucket")
        assert overridden.virtual_attrs("acme_widget") == {"force"}

    def test_non_importable_and_replace_prone_override(self, overridden: TypeKnowledge) -> None:
        assert overridden.is_importable("acme_ephemeral_token") is False
        assert overridden.is_importable("random_id") is False
        assert overridden.is_replace_prone("acme_widget") is True
        assert overridden.is_replace_prone("aws_lambda_layer_version") is True

    def test_package_defaults_untouched_for_other_types(self, overridden: TypeKnowledge) -> None:
        assert overridden.identity_for("aws_iam_role", {"name": "iam-acme-dev-app"}) == {
            "name": "iam-acme-dev-app"
        }
        assert overridden.import_id_for("aws_iam_role_policy", {"role": "r", "name": "n"}) == "r:n"


class TestIgnoredAttributes:
    def test_packaged_defaults(self, knowledge: TypeKnowledge) -> None:
        assert {"filename", "last_modified"} <= knowledge.ignored_attrs("aws_lambda_function")
        assert "filename" in knowledge.ignored_attrs("aws_lambda_layer_version")
        assert "output_path" in knowledge.ignored_attrs("archive_file")

    def test_wildcard_applies_to_every_type(self, knowledge: TypeKnowledge) -> None:
        assert knowledge.ignored_attrs("aws_s3_bucket") == {"last_modified"}
        assert knowledge.ignored_attrs("acme_widget") == {"last_modified"}
        assert "filename" not in knowledge.ignored_attrs("aws_s3_bucket")

    def test_user_entries_are_unioned(self) -> None:
        cfg = ToolConfig(
            policy=PolicyConfig(),
            ignored_attributes={"aws_lambda_function": ["s3_key"], "acme_widget": ["stamp"]},
        )
        knowledge = TypeKnowledge.load(cfg)
        assert {"filename", "last_modified", "s3_key"} <= knowledge.ignored_attrs(
            "aws_lambda_function"
        )
        assert knowledge.ignored_attrs("acme_widget") == {"last_modified", "stamp"}

    def test_constructor_default_is_empty(self) -> None:
        knowledge = TypeKnowledge({}, {}, [], [], {})
        assert knowledge.ignored_attrs("aws_lambda_function") == set()
