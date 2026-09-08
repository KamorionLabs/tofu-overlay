"""Tests for tofu_overlay.backend: HCL/file/cached parsing and resolution precedence."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.conftest import BASE_KEY, BUCKET, LOCK_TABLE, REGION
from tofu_overlay import backend
from tofu_overlay.backend import BackendResolutionError
from tofu_overlay.models import BackendConfig, ExitCode, OverlayError, ToolError

S3_BLOCK = f"""
terraform {{
  required_version = ">= 1.7"
  backend "s3" {{
    bucket         = "{BUCKET}"
    key            = "{BASE_KEY}"
    region         = "{REGION}"
    dynamodb_table = "{LOCK_TABLE}"
    encrypt        = true
    use_lockfile   = true
  }}
}}
"""

AZURERM_BLOCK = """
terraform {
  backend "azurerm" {
    resource_group_name  = "rg-acme-tfstate"
    storage_account_name = "acmetfstate"
    container_name       = "tfstate"
    key                  = "acme.tfstate"
  }
}
"""


@pytest.fixture
def env_dir(tmp_path: Path) -> Path:
    d = tmp_path / "stacks" / "storage" / "env" / "dev"
    d.mkdir(parents=True)
    (d / "main.tf").write_text('resource "aws_s3_bucket" "b" {\n  bucket = "x"\n}\n')
    return d


@pytest.fixture(autouse=True)
def _no_workspace_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TF_WORKSPACE", raising=False)


class TestParseHclBackend:
    def test_quoted_strings_are_stripped(self, env_dir: Path) -> None:
        (env_dir / "versions.tf").write_text(S3_BLOCK, encoding="utf-8")
        attrs = backend.parse_hcl_backend(env_dir)
        assert attrs is not None
        assert attrs["bucket"] == BUCKET
        assert attrs["key"] == BASE_KEY
        assert attrs["region"] == REGION
        assert attrs["dynamodb_table"] == LOCK_TABLE
        assert attrs["encrypt"] is True
        assert attrs["use_lockfile"] is True
        assert "__is_block__" not in attrs
        assert not any(str(v).startswith('"') for v in attrs.values())

    def test_no_backend_block(self, env_dir: Path) -> None:
        assert backend.parse_hcl_backend(env_dir) is None

    def test_empty_directory(self, tmp_path: Path) -> None:
        assert backend.parse_hcl_backend(tmp_path) is None

    def test_azurerm_refused(self, env_dir: Path) -> None:
        (env_dir / "backend.tf").write_text(AZURERM_BLOCK, encoding="utf-8")
        with pytest.raises(BackendResolutionError) as exc:
            backend.parse_hcl_backend(env_dir)
        assert "azurerm" in str(exc.value)
        assert isinstance(exc.value, ToolError)
        assert exc.value.exit_code == ExitCode.ERROR

    def test_interpolated_values_kept_raw(self, env_dir: Path) -> None:
        (env_dir / "backend.tf").write_text(
            'terraform {\n  backend "s3" {\n    bucket = "acme-${var.env}"\n'
            '    key = "acme/dev"\n  }\n}\n',
            encoding="utf-8",
        )
        attrs = backend.parse_hcl_backend(env_dir)
        assert attrs is not None
        assert "${" in attrs["bucket"]


class TestBackendConfigFiles:
    def test_key_value_file(self, tmp_path: Path) -> None:
        f = tmp_path / "dev.s3.tfbackend"
        f.write_text(
            f'bucket = "{BUCKET}"\nkey = "{BASE_KEY}"\nregion = "{REGION}"\n'
            f'dynamodb_table = "{LOCK_TABLE}"\nencrypt = true\n',
            encoding="utf-8",
        )
        attrs = backend.parse_backend_config_files([f])
        assert attrs["bucket"] == BUCKET
        assert attrs["key"] == BASE_KEY
        assert attrs["dynamodb_table"] == LOCK_TABLE
        assert attrs["encrypt"] in (True, "true")

    def test_later_file_wins(self, tmp_path: Path) -> None:
        a = tmp_path / "a.tfbackend"
        b = tmp_path / "b.tfbackend"
        a.write_text(f'bucket = "{BUCKET}"\nkey = "first"\n', encoding="utf-8")
        b.write_text('key = "second"\n', encoding="utf-8")
        attrs = backend.parse_backend_config_files([a, b])
        assert attrs["bucket"] == BUCKET
        assert attrs["key"] == "second"

    def test_missing_file_is_error(self, tmp_path: Path) -> None:
        with pytest.raises(OverlayError):
            backend.parse_backend_config_files([tmp_path / "nope.tfbackend"])


class TestCachedBackend:
    def test_read_cached_backend(self, tmp_path: Path) -> None:
        data_dir = tmp_path / ".terraform"
        data_dir.mkdir()
        (data_dir / "terraform.tfstate").write_text(
            json.dumps(
                {
                    "version": 3,
                    "backend": {
                        "type": "s3",
                        "config": {
                            "bucket": BUCKET,
                            "key": BASE_KEY,
                            "region": REGION,
                            "dynamodb_table": LOCK_TABLE,
                            "encrypt": True,
                        },
                        "hash": 1234,
                    },
                }
            ),
            encoding="utf-8",
        )
        attrs = backend.read_cached_backend(data_dir)
        assert attrs is not None
        assert attrs["bucket"] == BUCKET
        assert attrs["key"] == BASE_KEY

    def test_missing_cache(self, tmp_path: Path) -> None:
        assert backend.read_cached_backend(tmp_path / ".terraform") is None


class TestResolveBackend:
    def test_hcl_only(self, env_dir: Path) -> None:
        (env_dir / "versions.tf").write_text(S3_BLOCK, encoding="utf-8")
        cfg = backend.resolve_backend(env_dir, overrides={}, backend_config_files=[], data_dir=None)
        assert isinstance(cfg, BackendConfig)
        assert cfg.bucket == BUCKET
        assert cfg.key == BASE_KEY
        assert cfg.region == REGION
        assert cfg.dynamodb_table == LOCK_TABLE
        assert cfg.use_lockfile is True
        assert cfg.encrypt is True
        assert cfg.workspace == "default"

    def test_precedence_overrides_files_cached_hcl(self, env_dir: Path, tmp_path: Path) -> None:
        (env_dir / "versions.tf").write_text(S3_BLOCK, encoding="utf-8")
        data_dir = env_dir / ".terraform"
        data_dir.mkdir()
        (data_dir / "terraform.tfstate").write_text(
            json.dumps(
                {
                    "backend": {
                        "type": "s3",
                        "config": {
                            "bucket": "cached-bucket",
                            "key": "cached/key",
                            "region": REGION,
                        },
                    }
                }
            ),
            encoding="utf-8",
        )
        f = tmp_path / "dev.s3.tfbackend"
        f.write_text('bucket = "file-bucket"\n', encoding="utf-8")

        cfg = backend.resolve_backend(
            env_dir, overrides={}, backend_config_files=[], data_dir=data_dir
        )
        assert (cfg.bucket, cfg.key) == ("cached-bucket", "cached/key")

        cfg = backend.resolve_backend(
            env_dir, overrides={}, backend_config_files=[f], data_dir=data_dir
        )
        assert (cfg.bucket, cfg.key) == ("file-bucket", "cached/key")
        assert [Path(p).name for p in cfg.backend_config_files] == ["dev.s3.tfbackend"]

        cfg = backend.resolve_backend(
            env_dir,
            overrides={"bucket": "flag-bucket", "key": "flag/key"},
            backend_config_files=[f],
            data_dir=data_dir,
        )
        assert (cfg.bucket, cfg.key) == ("flag-bucket", "flag/key")

    def test_files_only(self, env_dir: Path, tmp_path: Path) -> None:
        f = tmp_path / "dev.s3.tfbackend"
        f.write_text(
            f'bucket = "{BUCKET}"\nkey = "{BASE_KEY}"\nregion = "{REGION}"\n', encoding="utf-8"
        )
        cfg = backend.resolve_backend(
            env_dir, overrides={}, backend_config_files=[f], data_dir=None
        )
        assert cfg.bucket == BUCKET
        assert cfg.key == BASE_KEY

    def test_missing_bucket_is_error(self, env_dir: Path) -> None:
        with pytest.raises(BackendResolutionError):
            backend.resolve_backend(
                env_dir, overrides={"key": BASE_KEY}, backend_config_files=[], data_dir=None
            )

    def test_missing_key_is_error(self, env_dir: Path) -> None:
        with pytest.raises(BackendResolutionError):
            backend.resolve_backend(
                env_dir, overrides={"bucket": BUCKET}, backend_config_files=[], data_dir=None
            )

    def test_unresolved_interpolation_is_error(self, env_dir: Path) -> None:
        with pytest.raises(BackendResolutionError) as exc:
            backend.resolve_backend(
                env_dir,
                overrides={"bucket": "acme-${var.env}", "key": BASE_KEY},
                backend_config_files=[],
                data_dir=None,
            )
        assert "${" in str(exc.value) or "unresolved" in str(exc.value).lower()

    def test_workspace_env_refused(self, env_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TF_WORKSPACE", "staging")
        with pytest.raises(BackendResolutionError) as exc:
            backend.resolve_backend(
                env_dir,
                overrides={"bucket": BUCKET, "key": BASE_KEY},
                backend_config_files=[],
                data_dir=None,
            )
        assert "workspace" in str(exc.value).lower()

    def test_workspace_file_refused(self, env_dir: Path) -> None:
        (env_dir / ".terraform").mkdir()
        (env_dir / ".terraform" / "environment").write_text("staging", encoding="utf-8")
        with pytest.raises(BackendResolutionError):
            backend.resolve_backend(
                env_dir,
                overrides={"bucket": BUCKET, "key": BASE_KEY},
                backend_config_files=[],
                data_dir=None,
            )

    def test_default_workspace_file_accepted(self, env_dir: Path) -> None:
        (env_dir / ".terraform").mkdir()
        (env_dir / ".terraform" / "environment").write_text("default", encoding="utf-8")
        cfg = backend.resolve_backend(
            env_dir,
            overrides={"bucket": BUCKET, "key": BASE_KEY},
            backend_config_files=[],
            data_dir=None,
        )
        assert cfg.workspace == "default"

    def test_azurerm_hcl_refused_on_resolve(self, env_dir: Path) -> None:
        (env_dir / "backend.tf").write_text(AZURERM_BLOCK, encoding="utf-8")
        with pytest.raises(BackendResolutionError) as exc:
            backend.resolve_backend(env_dir, overrides={}, backend_config_files=[], data_dir=None)
        assert "azurerm" in str(exc.value)


class TestDescribe:
    def test_describe_mentions_tuple(self) -> None:
        cfg = BackendConfig(
            bucket=BUCKET,
            key=BASE_KEY,
            region=REGION,
            profile="acme-dev",
            dynamodb_table=LOCK_TABLE,
            use_lockfile=True,
        )
        text = backend.describe(cfg)
        assert text.startswith(f"s3://{BUCKET}/{BASE_KEY}")
        for part in (REGION, "acme-dev", LOCK_TABLE, "lockfile"):
            assert part in text
