"""Cross-stack overlay links: OverlayService.remote_overlay_keys and the TF_VAR export.

Two bases live in the same moto bucket: the consumer stack (BASE_KEY, the
env dir under test) reads the producer stack (PRODUCER_KEY) through a
``terraform_remote_state`` block written with the MULTI-STACK.md contract.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest

from tests.conftest import BASE_KEY, BUCKET, REGION
from tofu_overlay import __version__
from tofu_overlay.models import BackendConfig, Status, ToolConfig
from tofu_overlay.output import Console
from tofu_overlay.overlay import NAME_VAR, REMOTE_KEYS_VAR, OverlayService
from tofu_overlay.registry import Registry
from tofu_overlay.store import make_store

NAME = "abc-12-reports-3f9a1c"
PRODUCER_KEY = "acme/webshop/eks/dev"


def _remote_state_tf(key: str, *, bucket: str = BUCKET, literal: bool = False) -> str:
    key_expr = f'"{key}"' if literal else f'lookup(var.tofu_overlay_keys, "{key}", "{key}")'
    return (
        'variable "tofu_overlay_keys" {\n  type    = map(string)\n  default = {}\n}\n\n'
        'data "terraform_remote_state" "eks" {\n  backend = "s3"\n  config = {\n'
        f'    bucket = "{bucket}"\n    key    = {key_expr}\n    region = "{REGION}"\n  }}\n}}\n'
    )


@pytest.fixture
def consumer_dir(tmp_path: Path) -> Path:
    d = tmp_path / "stacks" / "lambda" / "env" / "dev"
    d.mkdir(parents=True)
    (d / "remote.tf").write_text(_remote_state_tf(PRODUCER_KEY))
    return d


@pytest.fixture
def producer_cfg(backend_cfg: BackendConfig) -> BackendConfig:
    return backend_cfg.model_copy(update={"key": PRODUCER_KEY})


@pytest.fixture
def console() -> Console:
    c = Console(no_color=True)
    c.stdout = io.StringIO()
    c.stderr = io.StringIO()
    return c


def _service(env_dir: Path, backend_cfg, boto_session, console) -> OverlayService:
    return OverlayService(
        env_dir, ToolConfig(), backend_cfg, console, name=NAME, session=boto_session
    )


def _register(
    boto_session, cfg: BackendConfig, make_overlay, *, status: Status, with_object: bool = True
):
    """Register overlay NAME on the base of ``cfg`` (and push a dummy state object)."""
    ov = make_overlay(name=NAME, status=status, state_key=cfg.overlay_key(NAME))
    Registry(make_store(cfg, session=boto_session), cfg, __version__).update(
        lambda d: d.overlays.__setitem__(NAME, ov)
    )
    if with_object:
        boto_session.client("s3").put_object(
            Bucket=cfg.bucket, Key=cfg.overlay_key(NAME), Body=b'{"version": 4}'
        )
    return ov


class TestRemoteOverlayKeys:
    def test_no_refs_gives_empty_map(self, tmp_path, backend_cfg, boto_session, console):
        (tmp_path / "main.tf").write_text('resource "aws_s3_bucket" "b" {}\n')
        svc = _service(tmp_path, backend_cfg, boto_session, console)
        assert svc.remote_overlay_keys(NAME) == {}

    def test_missing_registry_is_no_overlay(self, consumer_dir, backend_cfg, boto_session, console):
        svc = _service(consumer_dir, backend_cfg, boto_session, console)
        assert svc.remote_overlay_keys(NAME) == {}
        assert svc._remote_warnings == []

    def test_live_overlay_is_mapped(
        self, consumer_dir, backend_cfg, producer_cfg, boto_session, console, make_overlay
    ):
        ov = _register(boto_session, producer_cfg, make_overlay, status=Status.ACTIVE)
        svc = _service(consumer_dir, backend_cfg, boto_session, console)
        assert svc.remote_overlay_keys(NAME) == {PRODUCER_KEY: ov.state_key}
        assert ov.state_key == f"{PRODUCER_KEY}@{NAME}"
        assert svc.status()["remote_overlays"] == {PRODUCER_KEY: ov.state_key}

    def test_literal_key_is_mapped_too(
        self, tmp_path, backend_cfg, producer_cfg, boto_session, console, make_overlay
    ):
        (tmp_path / "remote.tf").write_text(_remote_state_tf(PRODUCER_KEY, literal=True))
        ov = _register(boto_session, producer_cfg, make_overlay, status=Status.DIRTY)
        svc = _service(tmp_path, backend_cfg, boto_session, console)
        assert svc.remote_overlay_keys(NAME) == {PRODUCER_KEY: ov.state_key}

    @pytest.mark.parametrize("status", [Status.MERGED, Status.ABANDONED, Status.NEEDS_REVIEW])
    def test_non_live_overlay_is_not_mapped(
        self, consumer_dir, backend_cfg, producer_cfg, boto_session, console, make_overlay, status
    ):
        _register(boto_session, producer_cfg, make_overlay, status=status)
        svc = _service(consumer_dir, backend_cfg, boto_session, console)
        assert svc.remote_overlay_keys(NAME) == {}

    def test_other_name_is_not_mapped(
        self, consumer_dir, backend_cfg, producer_cfg, boto_session, console, make_overlay
    ):
        _register(boto_session, producer_cfg, make_overlay, status=Status.ACTIVE)
        svc = _service(consumer_dir, backend_cfg, boto_session, console)
        assert svc.remote_overlay_keys("another-branch-1a2b3c") == {}

    def test_merging_overlay_is_mapped_and_check_warns(
        self, consumer_dir, backend_cfg, producer_cfg, boto_session, console, make_overlay
    ):
        ov = _register(boto_session, producer_cfg, make_overlay, status=Status.MERGING)
        svc = _service(consumer_dir, backend_cfg, boto_session, console)
        assert svc.remote_overlay_keys(NAME) == {PRODUCER_KEY: ov.state_key}
        errors: list[str] = []
        warnings: list[str] = []
        svc._check_remote_links(errors, warnings)
        assert errors == []
        assert len(warnings) == 1 and "merging" in warnings[0] and "eks" in warnings[0]

    def test_missing_object_is_not_mapped_and_check_errors(
        self, consumer_dir, backend_cfg, producer_cfg, boto_session, console, make_overlay
    ):
        ov = _register(
            boto_session, producer_cfg, make_overlay, status=Status.ACTIVE, with_object=False
        )
        svc = _service(consumer_dir, backend_cfg, boto_session, console)
        assert svc.remote_overlay_keys(NAME) == {}
        errors: list[str] = []
        warnings: list[str] = []
        svc._check_remote_links(errors, warnings)
        assert len(errors) == 1 and ov.state_key in errors[0] and "no longer exists" in errors[0]

    def test_ref_to_own_base_is_ignored(
        self, tmp_path, backend_cfg, boto_session, console, make_overlay
    ):
        (tmp_path / "remote.tf").write_text(_remote_state_tf(BASE_KEY))
        _register(boto_session, backend_cfg, make_overlay, status=Status.ACTIVE)
        svc = _service(tmp_path, backend_cfg, boto_session, console)
        assert svc.remote_overlay_keys(NAME) == {}

    def test_unresolved_ref_is_reported(self, tmp_path, backend_cfg, boto_session, console):
        (tmp_path / "remote.tf").write_text(
            'data "terraform_remote_state" "eks" {\n  backend = "s3"\n  config = {\n'
            f'    bucket = "{BUCKET}"\n    key    = "acme/webshop/${{var.stack}}/dev"\n  }}\n}}\n'
        )
        svc = _service(tmp_path, backend_cfg, boto_session, console)
        assert svc.remote_overlay_keys(NAME) == {}
        assert svc._remote_warnings and "eks" in svc._remote_warnings[0]
        assert "not a literal" in svc._remote_warnings[0]

    def test_registry_error_is_a_warning_not_an_error(
        self, consumer_dir, backend_cfg, producer_cfg, boto_session, console
    ):
        boto_session.client("s3").put_object(
            Bucket=BUCKET, Key=producer_cfg.registry_key(), Body=b"{oops"
        )
        svc = _service(consumer_dir, backend_cfg, boto_session, console)
        assert svc.remote_overlay_keys(NAME) == {}
        assert svc._remote_warnings and "skipped" in svc._remote_warnings[0]

    def test_ref_bucket_overrides_the_current_backend(
        self, tmp_path, backend_cfg, boto_session, console, make_overlay
    ):
        other_bucket = "acme-tfstate-shared"
        boto_session.client("s3").create_bucket(
            Bucket=other_bucket, CreateBucketConfiguration={"LocationConstraint": REGION}
        )
        (tmp_path / "remote.tf").write_text(_remote_state_tf(PRODUCER_KEY, bucket=other_bucket))
        cfg = backend_cfg.model_copy(update={"bucket": other_bucket, "key": PRODUCER_KEY})
        ov = _register(boto_session, cfg, make_overlay, status=Status.ACTIVE)
        svc = _service(tmp_path, backend_cfg, boto_session, console)
        assert svc.remote_overlay_keys(NAME) == {PRODUCER_KEY: ov.state_key}
        # nothing of that name in the current bucket: it must not be mapped from there
        assert not make_store(backend_cfg, session=boto_session).exists(ov.state_key)


class RecordingRunner:
    """Minimal runner: records the environment it was built with."""

    envs: list[dict[str, str]] = []

    def __init__(self, binary: str, cwd: Path, data_dir: Path, env=None, stream=None) -> None:
        self.data_dir = Path(data_dir)
        RecordingRunner.envs.append(dict(env or {}))

    def needs_init(self, key: str) -> bool:
        return False

    def init(self, cfg: Any, key: str, *, reconfigure: bool = True) -> None:
        pass

    def ensure_backend_key(self, key: str) -> None:
        pass


class TestEnvExport:
    @pytest.fixture(autouse=True)
    def _reset(self):
        RecordingRunner.envs = []

    def test_runner_gets_json_map(
        self, consumer_dir, backend_cfg, producer_cfg, boto_session, console, make_overlay
    ):
        ov = _register(boto_session, producer_cfg, make_overlay, status=Status.ACTIVE)
        svc = OverlayService(
            consumer_dir, ToolConfig(), backend_cfg, console, name=NAME,
            session=boto_session, runner_factory=RecordingRunner,
        )
        svc._overlay_runner()
        (env,) = RecordingRunner.envs
        assert env[NAME_VAR] == NAME
        assert json.loads(env[REMOTE_KEYS_VAR]) == {PRODUCER_KEY: ov.state_key}

    def test_runner_gets_empty_object_without_links(
        self, consumer_dir, backend_cfg, boto_session, console
    ):
        svc = OverlayService(
            consumer_dir, ToolConfig(), backend_cfg, console, name=NAME,
            session=boto_session, runner_factory=RecordingRunner,
        )
        svc._overlay_runner()
        (env,) = RecordingRunner.envs
        assert env[REMOTE_KEYS_VAR] == "{}"
