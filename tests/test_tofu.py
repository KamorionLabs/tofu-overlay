"""Tests for tofu_overlay.tofu that need no binary: pass-through validation, data-dir checks."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests.conftest import BASE_KEY, BUCKET, REGION
from tofu_overlay import tofu
from tofu_overlay.models import ExitCode, OverlayError, ToolError
from tofu_overlay.tofu import FORBIDDEN_PASSTHROUGH, TofuRunner


class TestValidatePassthrough:
    def test_constant(self) -> None:
        for flag in ("-target", "-replace", "-refresh-only", "-destroy", "-state", "-lock=false"):
            assert flag in FORBIDDEN_PASSTHROUGH

    def test_allowed_arguments(self) -> None:
        tofu.validate_passthrough([])
        tofu.validate_passthrough(
            [
                "-var",
                "env=dev",
                "-var-file=dev.tfvars",
                "-parallelism=5",
                "-refresh=false",
                "-lock-timeout=5m",
                "-compact-warnings",
            ]
        )

    @pytest.mark.parametrize(
        "arg",
        [
            "-target",
            "-target=aws_s3_bucket.reports",
            "--target=aws_s3_bucket.reports",
            "-replace=aws_s3_bucket.reports",
            "--replace",
            "-refresh-only",
            "-destroy",
            "-state=other.tfstate",
            "--state",
            "-lock=false",
            "-lock=0",
            "-out=tfplan",
            "--out",
        ],
    )
    def test_forbidden(self, arg: str) -> None:
        with pytest.raises(ToolError) as exc:
            tofu.validate_passthrough(["-var", "x=1", arg])
        assert exc.value.exit_code == ExitCode.ERROR
        assert isinstance(exc.value, OverlayError)
        assert arg.split("=")[0].lstrip("-") in str(exc.value)

    def test_value_following_flag_is_not_scanned_as_flag(self) -> None:
        # a variable value that merely contains the word is fine
        tofu.validate_passthrough(["-var", "note=-target"])


@pytest.fixture
def runner(tmp_path: Path) -> TofuRunner:
    cwd = tmp_path / "stacks" / "storage" / "env" / "dev"
    cwd.mkdir(parents=True)
    return TofuRunner("tofu", cwd, tmp_path / ".tofu-overlay" / "feat-1a2b3c")


def write_backend_state(data_dir: Path, key: str) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "terraform.tfstate").write_text(
        json.dumps(
            {
                "version": 3,
                "terraform_version": "1.11.1",
                "backend": {
                    "type": "s3",
                    "config": {"bucket": BUCKET, "key": key, "region": REGION, "encrypt": True},
                    "hash": 42,
                },
            }
        ),
        encoding="utf-8",
    )


class TestDataDir:
    def test_construction_spawns_nothing(self, tmp_path: Path) -> None:
        r = TofuRunner(
            "/nonexistent/acme/tofu",
            tmp_path,
            tmp_path / "dd",
            env={"X": "1"},
            stream=lambda _l: None,
        )
        assert isinstance(r, TofuRunner)

    def test_needs_init_when_missing(self, runner: TofuRunner) -> None:
        assert runner.needs_init(BASE_KEY) is True

    def test_needs_init_on_key_mismatch(self, runner: TofuRunner, tmp_path: Path) -> None:
        write_backend_state(tmp_path / ".tofu-overlay" / "feat-1a2b3c", f"{BASE_KEY}@feat-1a2b3c")
        assert runner.needs_init(BASE_KEY) is True
        # never initialised through the runner: the lock marker is missing
        assert runner.needs_init(f"{BASE_KEY}@feat-1a2b3c") is True

    def test_needs_init_tracks_lock_file(self, runner: TofuRunner, tmp_path: Path) -> None:
        overlay_key = f"{BASE_KEY}@feat-1a2b3c"
        data_dir = tmp_path / ".tofu-overlay" / "feat-1a2b3c"
        write_backend_state(data_dir, overlay_key)
        # what init() records: a digest of the provider lock file (private but stable)
        (data_dir / tofu.LOCK_MARKER_NAME).write_text(runner._lock_digest(), encoding="utf-8")
        assert runner.needs_init(overlay_key) is False
        assert runner.needs_init(BASE_KEY) is True
        (runner.cwd / ".terraform.lock.hcl").write_text(
            'provider "registry.opentofu.org/hashicorp/aws" {\n  version = "6.0.0"\n}\n',
            encoding="utf-8",
        )
        assert runner.needs_init(overlay_key) is True

    def test_needs_init_on_garbage(self, runner: TofuRunner, tmp_path: Path) -> None:
        data_dir = tmp_path / ".tofu-overlay" / "feat-1a2b3c"
        data_dir.mkdir(parents=True)
        (data_dir / "terraform.tfstate").write_text("{not json", encoding="utf-8")
        assert runner.needs_init(BASE_KEY) is True

    def test_ensure_backend_key(self, runner: TofuRunner, tmp_path: Path) -> None:
        overlay_key = f"{BASE_KEY}@feat-1a2b3c"
        write_backend_state(tmp_path / ".tofu-overlay" / "feat-1a2b3c", overlay_key)
        runner.ensure_backend_key(overlay_key)
        with pytest.raises(ToolError) as exc:
            runner.ensure_backend_key(BASE_KEY)
        assert exc.value.exit_code == ExitCode.ERROR

    def test_ensure_backend_key_missing_data_dir(self, runner: TofuRunner) -> None:
        with pytest.raises(ToolError):
            runner.ensure_backend_key(BASE_KEY)


class FakePopen:
    """Records the Popen call and behaves like a finished, silent process."""

    calls: list[dict[str, Any]] = []

    def __init__(self, cmd: list[str], **kwargs: Any) -> None:
        FakePopen.calls.append({"cmd": cmd, **kwargs})
        self.returncode = 0
        self.stdout = iter([])

    def wait(self) -> int:
        return self.returncode

    def communicate(self) -> tuple[str, str]:
        return "", ""


@pytest.fixture
def spawned(runner: TofuRunner) -> type[FakePopen]:
    FakePopen.calls = []
    runner.popen_factory = FakePopen
    return FakePopen


class TestEnvironment:
    def test_tf_cli_args_are_dropped(
        self, runner: TofuRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TF_CLI_ARGS", "-lock=false")
        monkeypatch.setenv("TF_CLI_ARGS_plan", "-target=aws_s3_bucket.reports")
        monkeypatch.setenv("TF_CLI_ARGS_apply", "-refresh-only")
        monkeypatch.setenv("TF_LOG", "info")
        env = runner.environment()
        assert not [k for k in env if k.startswith("TF_CLI_ARGS")]
        assert env["TF_LOG"] == "info"
        assert env["TF_DATA_DIR"] == str(runner.data_dir)
        assert env["TF_IN_AUTOMATION"] == "1"


class TestRunner:
    def test_init_passes_absolute_backend_config_files(
        self, runner: TofuRunner, spawned: type[FakePopen], tmp_path: Path
    ) -> None:
        from tofu_overlay.models import BackendConfig

        config_file = (tmp_path / "dev.s3.tfbackend").resolve()
        config_file.write_text('bucket = "acme-tfstate"\n', encoding="utf-8")
        cfg = BackendConfig(
            bucket=BUCKET, key=BASE_KEY, region=REGION, backend_config_files=[str(config_file)]
        )
        runner.init(cfg, f"{BASE_KEY}@feat-1a2b3c")
        call = spawned.calls[0]
        assert call["cwd"] == str(runner.cwd)
        assert f"-backend-config={config_file}" in call["cmd"]
        assert Path(config_file).is_absolute()
        assert call["cmd"][-1] == f"-backend-config=key={BASE_KEY}@feat-1a2b3c"

    def test_apply_never_inherits_stdio(
        self, runner: TofuRunner, spawned: type[FakePopen], tmp_path: Path
    ) -> None:
        planfile = tmp_path / "tfplan.abc"
        runner.apply(planfile, auto_approve=False)
        call = spawned.calls[0]
        assert call["cmd"][:2] == ["tofu", "apply"]
        assert call["cmd"][-1] == str(planfile)
        assert call["stdin"] is subprocess.DEVNULL
        assert call["stdout"] is subprocess.PIPE
        assert call["stderr"] is subprocess.STDOUT
        assert not [k for k in call["env"] if k.startswith("TF_CLI_ARGS")]
