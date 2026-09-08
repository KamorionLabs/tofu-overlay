"""Shared pytest fixtures: moto-backed S3/DynamoDB, fixture documents, model factories.

Tests never call a real OpenTofu binary nor real AWS: every AWS call goes through
moto's ``mock_aws`` and every subprocess-bound helper is exercised only on the
parts that do not spawn processes (or on throwaway git repositories).
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import boto3
import pytest
from moto import mock_aws

FIXTURES_DIR = Path(__file__).parent / "fixtures"

BUCKET = "acme-tfstate"
BASE_KEY = "acme/webshop/storage/dev"
REGION = "eu-west-1"
LOCK_TABLE = "acme-tflock"
BASE_LINEAGE = "5f1c9d1e-2b7a-4c6e-9a1d-0f3b8e7d6c5a"
OVERLAY_LINEAGE = "a7e2f0c4-6d1b-4f3a-8e5c-2b9d7c1a4e6f"
ME = "feature-abc-12-reports-3f9a1c"
OTHER = "feature-xyz-99-audit-7b2c4d"
BASE_ETAG = '"9c1f0e2d3b4a5968778695a4b3c2d1e0"'


@pytest.fixture(autouse=True)
def aws_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fake credentials so boto3 never reaches a real account."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.delenv("AWS_PROFILE", raising=False)


@pytest.fixture
def aws() -> Iterator[None]:
    """Activate moto for the duration of a test."""
    with mock_aws():
        yield


@pytest.fixture
def load_fixture() -> Callable[[str], Any]:
    """Return a loader for JSON documents under tests/fixtures (fresh copy each call)."""

    def _load(name: str) -> Any:
        with (FIXTURES_DIR / name).open(encoding="utf-8") as fh:
            return json.load(fh)

    return _load


@pytest.fixture
def plan_synthetic(load_fixture: Callable[[str], Any]) -> dict:
    return load_fixture("plan_synthetic.json")


@pytest.fixture
def plan_terraform_data(load_fixture: Callable[[str], Any]) -> dict:
    return load_fixture("plan_terraform_data.json")


@pytest.fixture
def state_base(load_fixture: Callable[[str], Any]) -> dict:
    return load_fixture("state_base.json")


@pytest.fixture
def state_overlay(load_fixture: Callable[[str], Any]) -> dict:
    return load_fixture("state_overlay.json")


@pytest.fixture
def registry_json(load_fixture: Callable[[str], Any]) -> dict:
    return load_fixture("registry.json")


@pytest.fixture
def backend_cfg():
    """A BackendConfig for the acme dev base state (S3 + DynamoDB lock table)."""
    from tofu_overlay.models import BackendConfig

    return BackendConfig(
        bucket=BUCKET,
        key=BASE_KEY,
        region=REGION,
        profile=None,
        dynamodb_table=LOCK_TABLE,
        use_lockfile=False,
        encrypt=True,
        kms_key_id=None,
    )


@pytest.fixture
def boto_session(aws: None) -> boto3.Session:
    """A boto3 session bound to the moto sandbox, with bucket and lock table created."""
    session = boto3.Session(region_name=REGION)
    s3 = session.client("s3")
    s3.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": REGION})
    ddb = session.client("dynamodb")
    ddb.create_table(
        TableName=LOCK_TABLE,
        AttributeDefinitions=[{"AttributeName": "LockID", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "LockID", "KeyType": "HASH"}],
        BillingMode="PAY_PER_REQUEST",
    )
    return session


@pytest.fixture
def s3_client(boto_session: boto3.Session):
    return boto_session.client("s3")


@pytest.fixture
def ddb_client(boto_session: boto3.Session):
    return boto_session.client("dynamodb")


@pytest.fixture
def s3state(backend_cfg, boto_session: boto3.Session):
    """An S3State wired to the moto sandbox."""
    from tofu_overlay.s3state import S3State

    return S3State(backend_cfg, session=boto_session)


@pytest.fixture
def registry(s3state, backend_cfg):
    """A Registry over the moto-backed S3State."""
    from tofu_overlay.registry import Registry

    return Registry(s3state, backend_cfg, "0.1.0")


@pytest.fixture
def tool_config():
    from tofu_overlay.models import PolicyConfig, ToolConfig

    return ToolConfig(
        policy=PolicyConfig(allowed_base_keys=["acme/webshop/*/dev", "acme/*/sandbox"]),
    )


@pytest.fixture
def knowledge(tool_config):
    from tofu_overlay.identity import TypeKnowledge

    return TypeKnowledge.load(tool_config)


@pytest.fixture
def make_claim() -> Callable[..., Any]:
    """Factory for Claim models with sensible defaults."""
    from tofu_overlay.models import Claim, ClaimKind

    def _make(
        kind: str | ClaimKind = ClaimKind.CREATE,
        type_: str = "aws_s3_bucket",
        identity: dict | None = None,
        id_: str | None = None,
        import_id: str | None = None,
        after_hash: str | None = None,
    ) -> Claim:
        return Claim(
            kind=ClaimKind(kind),
            type=type_,
            identity=identity or {},
            id=id_,
            import_id=import_id,
            after_hash=after_hash,
            claimed_at="2026-09-02T10:00:00Z",
            updated_at="2026-09-07T16:30:00Z",
        )

    return _make


@pytest.fixture
def make_overlay() -> Callable[..., Any]:
    """Factory for Overlay models (defaults describe the reference overlay of the fixtures)."""
    from tofu_overlay.models import Overlay, Status

    def _make(
        name: str = ME,
        status: str | Status = Status.ACTIVE,
        claims: dict | None = None,
        branch: str = "feature/ABC-12-reports",
        base_etag: str | None = BASE_ETAG,
        **fields: Any,
    ) -> Overlay:
        data: dict[str, Any] = {
            "name": name,
            "state_key": f"{BASE_KEY}@{name}",
            "lineage": OVERLAY_LINEAGE,
            "branch": branch,
            "owners": ["dev.one@example.com"],
            "caller_arn": None,
            "binary": "tofu",
            "tofu_version": "1.11.1",
            "created_at": "2026-09-01T08:00:00Z",
            "updated_at": "2026-09-07T16:30:00Z",
            "base_serial": 412,
            "base_etag": base_etag,
            "trunk_commit": "0123456789abcdef0123456789abcdef01234567",
            "status": Status(status),
            "applied_commit": None,
            "run_id": None,
            "applying_since": None,
            "claims": claims or {},
            "pending_revert": [],
            "last_apply": None,
        }
        data.update(fields)
        return Overlay(**data)

    return _make


@pytest.fixture
def registry_doc(registry_json: dict):
    """The registry.json fixture parsed as a RegistryDoc."""
    from tofu_overlay.models import RegistryDoc

    return RegistryDoc.model_validate(copy.deepcopy(registry_json))


@pytest.fixture
def me(registry_doc):
    """The reference overlay from the registry fixture."""
    return registry_doc.overlays[ME]


def git(*args: str, cwd: Path) -> str:
    """Run a git command in a throwaway repository and return stdout."""
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", HOME=str(cwd.parent))
    out = subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env=env
    )
    return out.stdout.strip()


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """A local git repository on branch ``main`` with one commit and a user identity."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git("init", "-q", "-b", "main", cwd=repo)
    git("config", "user.email", "dev.one@example.com", cwd=repo)
    git("config", "user.name", "Dev One", cwd=repo)
    git("config", "commit.gpgsign", "false", cwd=repo)
    (repo / "README.md").write_text("acme\n", encoding="utf-8")
    git("add", ".", cwd=repo)
    git("commit", "-q", "-m", "init", cwd=repo)
    return repo


@pytest.fixture
def git_repo_with_remote(git_repo: Path, tmp_path: Path) -> Path:
    """The repo above with a bare ``origin`` remote holding ``main``."""
    remote = tmp_path / "origin.git"
    git("init", "-q", "--bare", "-b", "main", str(remote), cwd=tmp_path)
    git("remote", "add", "origin", str(remote), cwd=git_repo)
    git("push", "-q", "-u", "origin", "main", cwd=git_repo)
    return git_repo
