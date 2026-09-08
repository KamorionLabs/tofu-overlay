"""End-to-end lifecycle of an overlay through OverlayService, MergeService and the CLI.

AWS is moto, git is a throwaway repository, and tofu is replaced by a fake runner
that keeps the states in the moto bucket and derives plans from a desired
configuration. This covers the glue that the unit suites do not exercise:
create -> plan -> apply -> check -> merge -> finalize, abandon, gc, doctor and
the CLI plumbing (exit codes, JSON output, allowed_base_keys).
"""

from __future__ import annotations

import io
import json
import uuid
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from tests.conftest import BASE_KEY, BUCKET, LOCK_TABLE, REGION, git
from tofu_overlay import cli, config
from tofu_overlay import state as statemod
from tofu_overlay.merge import IMPORTS_FILENAME, MergeService, read_imports_addresses
from tofu_overlay.models import (
    ExitCode,
    FrozenError,
    PolicyError,
    RegistryError,
    StaleError,
    Status,
    ToolError,
)
from tofu_overlay.output import Console
from tofu_overlay.overlay import REMOTE_KEYS_VAR, OverlayService

BRANCH = "feature/ABC-12-reports"
NEW_ADDRESS = "aws_s3_bucket.reports"
NEW_BUCKET = "acme-reports-overlay"
PROVIDER = 'provider["registry.opentofu.org/hashicorp/aws"]'
EKS_KEY = "acme/webshop/eks/dev"
LAMBDA_KEY = "acme/webshop/lambda/dev"


class FakeRunner:
    """TofuRunner stand-in: states live in the moto bucket, plans come from `desired`."""

    session: Any = None
    desired: dict[str, dict[str, Any]] = {}
    calls: list[list[str]] = []
    envs: list[dict[str, str]] = []

    def __init__(self, binary: str, cwd: Path, data_dir: Path, env=None, stream=None) -> None:
        self.binary = binary
        self.cwd = Path(cwd)
        self.data_dir = Path(data_dir)
        self.env = dict(env or {})
        self.stream = stream
        self.data_dir.mkdir(parents=True, exist_ok=True)
        FakeRunner.envs.append(self.env)

    # ------------------------------------------------------------ backend
    @property
    def _marker(self) -> Path:
        return self.data_dir / "terraform.tfstate"

    @property
    def key(self) -> str:
        return json.loads(self._marker.read_text())["backend"]["config"]["key"]

    def needs_init(self, key: str) -> bool:
        return not self._marker.exists() or self.key != key

    def init(self, cfg, key: str, *, reconfigure: bool = True) -> None:
        FakeRunner.calls.append(["init", key])
        self._marker.write_text(
            json.dumps({"backend": {"type": "s3", "config": {"bucket": cfg.bucket, "key": key}}})
        )

    def ensure_backend_key(self, key: str) -> None:
        assert self.key == key, f"data dir bound to {self.key}, expected {key}"

    def version(self) -> str:
        return "1.11.1"

    # ------------------------------------------------------------ state
    def _s3(self):
        return FakeRunner.session.client("s3")

    def state_pull(self) -> dict:
        body = self._s3().get_object(Bucket=BUCKET, Key=self.key)["Body"].read()
        return json.loads(body)

    def state_push(self, doc: dict, *, force: bool = False) -> None:
        """Mimic `tofu state push`: without -force an empty remote gets a fresh lineage
        and serial 1, a different lineage is refused, and the serial follows the remote."""
        FakeRunner.calls.append(["state push", self.key])
        doc = dict(doc)
        if not force:
            try:
                remote = self.state_pull()
            except Exception:  # noqa: BLE001 - missing object == empty remote
                remote = None
            if not remote or not remote.get("lineage"):
                doc["lineage"] = str(uuid.uuid4())
                doc["serial"] = 1
            else:
                if remote.get("lineage") != doc.get("lineage"):
                    raise ToolError("lineage mismatch: use -force")
                if int(doc.get("serial") or 0) < int(remote.get("serial") or 0):
                    raise ToolError("serial too low: use -force")
                doc["serial"] = int(remote.get("serial") or 0) + 1
        self._s3().put_object(Bucket=BUCKET, Key=self.key, Body=json.dumps(doc).encode())

    def state_rm(self, addresses: list[str]) -> None:
        FakeRunner.calls.append(["state rm", *addresses])
        doc = statemod.remove_addresses(self.state_pull(), set(addresses))
        doc["serial"] = int(doc.get("serial") or 0) + 1
        self.state_push(doc)

    # ------------------------------------------------------------ plan/apply
    def _imports(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for path in self.cwd.glob("zz_overlay_*.imports.tf"):
            out.update(read_imports_addresses(path))
        return out

    @staticmethod
    def _entry(address: str, entry: dict, inst: dict, actions: list[str], **extra) -> dict:
        attrs = inst.get("attributes") or {}
        change = {
            "actions": actions,
            "before": None if actions == ["create"] else attrs,
            "after": None if actions == ["delete"] else attrs,
            "after_unknown": {},
            "after_sensitive": {},
            "replace_paths": [],
            "importing": None,
        }
        change.update(extra)
        return {
            "address": address,
            "module_address": entry.get("module"),
            "mode": entry.get("mode", "managed"),
            "type": entry["type"],
            "name": entry["name"],
            "index": inst.get("index_key"),
            "change": change,
        }

    def plan(self, out: Path, *, extra=None, destroy=False, targets=None, refresh=True) -> int:
        FakeRunner.calls.append(["plan", self.key, "destroy" if destroy else "", *(extra or [])])
        present = statemod.index_instances(self.state_pull())
        imports = self._imports()
        changes = []
        if destroy:
            for address, (entry, inst) in present.items():
                if entry.get("mode") == "managed":
                    changes.append(self._entry(address, entry, inst, ["delete"]))
        else:
            for address, spec in FakeRunner.desired.items():
                if address in present:
                    entry, inst = present[address]
                    changes.append(self._entry(address, entry, inst, ["no-op"]))
                    continue
                entry = {"type": spec["type"], "name": spec["name"], "mode": "managed"}
                inst = {"attributes": spec["attrs"]}
                if address in imports:
                    inst = {"attributes": {**spec["attrs"], "id": imports[address]}}
                    changes.append(
                        self._entry(
                            address, entry, inst, ["no-op"], importing={"id": imports[address]}
                        )
                    )
                else:
                    changes.append(
                        self._entry(address, entry, inst, ["create"], after_unknown={"id": True})
                    )
            for address, (entry, inst) in present.items():
                if entry.get("mode") == "managed" and address not in FakeRunner.desired:
                    changes.append(self._entry(address, entry, inst, ["delete"]))
        out.write_text(
            json.dumps({"format_version": "1.2", "resource_changes": changes, "resource_drift": []})
        )
        return 2 if any(c["change"]["actions"] != ["no-op"] for c in changes) else 0

    def show_json(self, planfile: Path) -> dict:
        return json.loads(Path(planfile).read_text())

    def apply(self, planfile: Path, *, auto_approve: bool) -> None:
        FakeRunner.calls.append(["apply", self.key])
        plan = self.show_json(planfile)
        doc = self.state_pull()
        for change in plan["resource_changes"]:
            actions = change["change"]["actions"]
            if actions == ["delete"]:
                doc = statemod.remove_addresses(doc, {change["address"]})
            elif actions == ["create"]:
                attrs = dict(change["change"]["after"])
                attrs["id"] = attrs.get("bucket") or f"id-{change['name']}"
                doc["resources"].append(
                    {
                        "mode": "managed",
                        "type": change["type"],
                        "name": change["name"],
                        "provider": PROVIDER,
                        "instances": [
                            {"schema_version": 0, "attributes": attrs, "sensitive_attributes": []}
                        ],
                    }
                )
        doc["serial"] = int(doc.get("serial") or 0) + 1
        self.state_push(doc)


# ---------------------------------------------------------------------- fixtures


@pytest.fixture
def env_repo(git_repo_with_remote: Path) -> Path:
    """Repo with a stack env dir, the tool config, .gitignore, on a feature branch."""
    repo = git_repo_with_remote
    env_dir = repo / "stacks" / "storage" / "env" / "dev"
    env_dir.mkdir(parents=True)
    (env_dir / "main.tf").write_text('resource "aws_s3_bucket" "reports" {}\n')
    (repo / ".gitignore").write_text(".tofu-overlay/\n")
    (repo / ".tofu-overlay.yaml").write_text(
        "policy:\n  allowed_base_keys:\n    - 'acme/webshop/*/dev'\n  trunk_branch: main\n"
    )
    git("add", ".", cwd=repo)
    git("commit", "-q", "-m", "stack", cwd=repo)
    git("push", "-q", "origin", "main", cwd=repo)
    git("checkout", "-q", "-b", BRANCH, cwd=repo)
    return env_dir


@pytest.fixture
def base_in_s3(s3_client, state_base: dict) -> dict:
    s3_client.put_object(Bucket=BUCKET, Key=BASE_KEY, Body=json.dumps(state_base).encode())
    return state_base


@pytest.fixture
def fake_runner(boto_session, base_in_s3: dict):
    FakeRunner.session = boto_session
    FakeRunner.calls = []
    FakeRunner.envs = []
    desired: dict[str, dict[str, Any]] = {}
    for address, (entry, _inst) in statemod.index_instances(base_in_s3).items():
        if entry.get("mode") == "managed":
            desired[address] = {"type": entry["type"], "name": entry["name"], "attrs": {}}
    desired[NEW_ADDRESS] = {
        "type": "aws_s3_bucket",
        "name": "reports",
        "attrs": {"bucket": NEW_BUCKET, "force_destroy": False},
    }
    FakeRunner.desired = desired
    yield FakeRunner
    FakeRunner.session = None
    FakeRunner.desired = {}


@pytest.fixture
def console() -> Console:
    c = Console(no_color=True)
    c.stdout = io.StringIO()
    c.stderr = io.StringIO()
    return c


@pytest.fixture
def service(env_repo: Path, backend_cfg, boto_session, fake_runner, console):
    cfg = config.load_config(env_repo)
    return OverlayService(
        env_repo, cfg, backend_cfg, console, session=boto_session, runner_factory=fake_runner
    )


def _other_service(env_repo: Path, backend_cfg, boto_session, fake_runner, console, name: str):
    cfg = config.load_config(env_repo)
    return OverlayService(
        env_repo, cfg, backend_cfg, console, name=name,
        session=boto_session, runner_factory=fake_runner,
    )


def _adopt_in_base(
    s3_client,
    overlay_key: str,
    address: str,
    attrs: dict[str, Any] | None = None,
    base_key: str = BASE_KEY,
) -> None:
    """Simulate the trunk applying the imports: copy the instance into the base state.

    ``attrs`` overrides instance attributes (a different ``id`` simulates the
    trunk creating its own object instead of importing).
    """
    base = json.loads(s3_client.get_object(Bucket=BUCKET, Key=base_key)["Body"].read())
    overlay = json.loads(s3_client.get_object(Bucket=BUCKET, Key=overlay_key)["Body"].read())
    entry, inst = statemod.index_instances(overlay)[address]
    inst = {**inst, "attributes": {**inst.get("attributes", {}), **(attrs or {})}}
    base["resources"].append({**entry, "instances": [inst]})
    base["serial"] += 1
    s3_client.put_object(Bucket=BUCKET, Key=base_key, Body=json.dumps(base).encode())


def _move_base(s3_client, base_in_s3: dict) -> dict:
    """Bump the base serial so the overlay becomes stale."""
    moved = dict(base_in_s3, serial=base_in_s3["serial"] + 1)
    s3_client.put_object(Bucket=BUCKET, Key=BASE_KEY, Body=json.dumps(moved).encode())
    return moved


def _created_and_applied(service):
    service.create()
    return service.apply(auto_approve=True, allow_stale=False, allow_behind=False)


def _merged(service) -> Path:
    return MergeService(service).merge(
        allow_import_updates=False, accept_recreate=[], allow_unapplied=False, yes=True
    )


def _status_of(service) -> Status:
    doc, _ = service.registry.load()
    return doc.overlays[service.name].status


# ---------------------------------------------------------------------- service


class TestImportStrategyLifecycle:
    def test_create_plan_apply_merge_finalize(self, service, s3_client, env_repo, console):
        name = config.overlay_name_for(BRANCH)
        assert service.name == name

        ov = service.create()
        assert ov.status == Status.ACTIVE
        assert ov.branch == BRANCH
        assert ov.base_etag
        assert service.store.exists(service.overlay_key)
        forked = json.loads(
            s3_client.get_object(Bucket=BUCKET, Key=service.overlay_key)["Body"].read()
        )
        assert forked["lineage"] == ov.lineage != json.loads(
            s3_client.get_object(Bucket=BUCKET, Key=BASE_KEY)["Body"].read()
        )["lineage"]
        with pytest.raises(RegistryError):
            service.create()

        policy, summary, planfile, stale = service.plan()
        assert policy.ok and not stale
        assert summary.create == 1
        assert set(policy.claims) == {NEW_ADDRESS}
        assert planfile.exists()

        ov = service.apply(auto_approve=True, allow_stale=False, allow_behind=False)
        assert ov.status == Status.ACTIVE
        assert ov.applied_commit == config.head_commit(env_repo)
        claim = ov.claims[NEW_ADDRESS]
        assert claim.id == NEW_BUCKET
        assert claim.import_id == NEW_BUCKET
        assert claim.identity == {"bucket": NEW_BUCKET}
        assert ov.last_apply and ov.last_apply["ok"]
        # The base state was never written (invariant 3.2).
        base = json.loads(s3_client.get_object(Bucket=BUCKET, Key=BASE_KEY)["Body"].read())
        assert NEW_ADDRESS not in statemod.addresses(base)

        ok, errors, warnings = service.check()
        assert ok, errors
        assert service.list()[0]["claims"] == 1
        assert service.status()["current"] == name

        merged = MergeService(service).merge(
            allow_import_updates=False, accept_recreate=[], allow_unapplied=False, yes=True
        )
        assert merged == env_repo / IMPORTS_FILENAME.format(name=name)
        assert read_imports_addresses(merged) == {NEW_ADDRESS: NEW_BUCKET}
        assert service.registry.get_overlay(service.registry.load()[0], name).status == (
            Status.MERGING
        )

        policy, _summary, _planfile, _stale = service.plan()
        assert policy.ok, policy.violations
        with pytest.raises(FrozenError):
            service.apply(auto_approve=True, allow_stale=False, allow_behind=False)
        with pytest.raises(PolicyError, match="not adopted"):
            service.finalize(purge=False, yes=True)

        _adopt_in_base(s3_client, service.overlay_key, NEW_ADDRESS)
        service.finalize(purge=False, yes=True)
        assert not service.store.exists(service.overlay_key)
        archives = [k for k in service.store.list_prefix(f"{BASE_KEY}@") if ".merged-" in k]
        assert len(archives) == 1
        doc, _ = service.registry.load()
        assert name not in doc.overlays
        assert doc.tombstones[name].status == Status.MERGED
        assert not service.data_dir.exists()

        with pytest.raises(RegistryError, match="force-name"):
            service.create()
        assert "never" not in console.stdout.getvalue()

    def test_conflicting_overlay_is_refused(
        self, service, env_repo, backend_cfg, boto_session, fake_runner, console
    ):
        service.create()
        service.apply(auto_approve=True, allow_stale=False, allow_behind=False)
        other = _other_service(env_repo, backend_cfg, boto_session, fake_runner, console, "other")
        other.create()
        policy, _summary, _planfile, _stale = other.plan()
        assert not policy.ok
        assert {v.rule for v in policy.violations} <= {"address-conflict", "identity-conflict"}
        assert all(v.other_overlay == service.name for v in policy.violations)
        with pytest.raises(PolicyError):
            other.apply(auto_approve=True, allow_stale=False, allow_behind=False)

    def test_stale_overlay_needs_rebase(self, service, s3_client, base_in_s3):
        service.create()
        moved = _move_base(s3_client, base_in_s3)
        ok, errors, _warnings = service.check()
        assert not ok and any(e.startswith("stale:") for e in errors)
        with pytest.raises(StaleError):
            service.apply(auto_approve=True, allow_stale=False, allow_behind=False)
        ov = service.rebase(yes=True)
        assert ov.base_serial == moved["serial"]
        ok, errors, _warnings = service.check()
        assert ok, errors


    def test_deleting_own_resource_releases_claim(self, service, fake_runner):
        _created_and_applied(service)
        fake_runner.desired.pop(NEW_ADDRESS)
        policy, summary, _planfile, _stale = service.plan()
        assert policy.ok, policy.violations
        assert summary.delete == 1
        assert NEW_ADDRESS not in policy.claims
        assert any("claim is released after apply" in w for w in policy.warnings)
        ov = service.apply(auto_approve=True, allow_stale=False, allow_behind=False)
        assert ov.status == Status.ACTIVE
        assert NEW_ADDRESS not in ov.claims
        doc, _ = service.registry.load()
        assert NEW_ADDRESS not in doc.overlays[service.name].claims
        ok, errors, _warnings = service.check()
        assert ok, errors
        # and the overlay can still be merged (no create claim without instance)
        assert _merged(service).exists()

    def test_apply_without_auto_approve_requires_confirmation(self, service, console):
        service.create()
        console.ask = lambda _prompt: "n"
        FakeRunner.calls.clear()
        with pytest.raises(ToolError, match="apply aborted"):
            service.apply(auto_approve=False, allow_stale=False, allow_behind=False)
        assert ["apply", service.overlay_key] not in FakeRunner.calls
        assert _status_of(service) == Status.ACTIVE
        assert not any(a for a in service.data_dir.glob("tfplan.*"))

        console.ask = lambda _prompt: "y"
        ov = service.apply(auto_approve=False, allow_stale=False, allow_behind=False)
        assert ov.status == Status.ACTIVE
        assert NEW_ADDRESS in ov.claims
        assert ["apply", service.overlay_key] in FakeRunner.calls

    def test_apply_refused_in_ci_without_yes(self, service, monkeypatch):
        service.create()
        service.console.ci = True
        with pytest.raises(ToolError, match="apply aborted"):
            service.apply(auto_approve=False, allow_stale=False, allow_behind=False)
        ov = service.apply(auto_approve=False, allow_stale=False, allow_behind=False, yes=True)
        assert NEW_ADDRESS in ov.claims

    def test_plan_files_do_not_accumulate(self, service):
        service.create()
        for _ in range(3):
            policy, _summary, planfile, _stale = service.plan()
            assert policy.ok and planfile.exists()
        assert len(list(service.data_dir.glob("tfplan.*"))) == 1
        service.apply(auto_approve=True, allow_stale=False, allow_behind=False)
        assert list(service.data_dir.glob("tfplan.*")) == []
        _merged(service)
        assert list(service.base_data_dir.glob("tfplan.*")) == []

    def test_claim_records_dependencies(self, service, make_claim):
        doc = {
            "version": 4, "serial": 1, "lineage": "x",
            "resources": [{
                "mode": "managed", "type": "aws_lambda_function", "name": "worker",
                "provider": PROVIDER,
                "instances": [{
                    "schema_version": 0,
                    "attributes": {"id": "fn", "function_name": "fn"},
                    "sensitive_attributes": [],
                    "dependencies": ["aws_iam_role.app", "module.net.aws_security_group.app"],
                }],
            }],
        }
        claims = {"aws_lambda_function.worker": make_claim(type_="aws_lambda_function")}
        filled = service._claims_from_state(claims, doc)
        assert filled["aws_lambda_function.worker"].dependencies == [
            "aws_iam_role.app", "module.net.aws_security_group.app",
        ]


class TestRebase:
    def test_rebase_fresh_overlay_is_refused(self, service):
        service.create()
        with pytest.raises(PolicyError, match="nothing to rebase"):
            service.rebase(yes=True)

    def test_rebase_archives_previous_state(self, service, s3_client, base_in_s3):
        _created_and_applied(service)
        _move_base(s3_client, base_in_s3)
        ov = service.rebase(yes=True)
        assert ov.status == Status.ACTIVE
        archives = [k for k in service.store.list_prefix(f"{BASE_KEY}@") if ".rebase-" in k]
        assert len(archives) == 1
        rebased = json.loads(
            s3_client.get_object(Bucket=BUCKET, Key=service.overlay_key)["Body"].read()
        )
        assert NEW_ADDRESS in statemod.addresses(rebased)
        assert rebased["lineage"] == ov.lineage

    def test_rebase_refuses_claimed_address_in_new_base_with_policy_error(
        self, service, s3_client
    ):
        _created_and_applied(service)
        _adopt_in_base(s3_client, service.overlay_key, NEW_ADDRESS)
        with pytest.raises(PolicyError, match="conflicts with create claims") as exc:
            service.rebase(yes=True)
        assert exc.value.exit_code == ExitCode.POLICY
        assert NEW_ADDRESS in str(exc.value)
        assert not any(".rebase-" in k for k in service.store.list_prefix(f"{BASE_KEY}@"))


class TestAbandon:
    def test_abandon_merging_requires_keep_resources(self, service):
        _created_and_applied(service)
        _merged(service)
        with pytest.raises(FrozenError):
            service.abandon(keep_resources=False, dry_run=False, yes=True)
        assert service.store.exists(service.overlay_key)
        FakeRunner.calls.clear()
        service.abandon(keep_resources=True, dry_run=False, yes=True)
        assert not [c for c in FakeRunner.calls if c[0] == "apply"]
        assert not service.store.exists(service.overlay_key)
        doc, _ = service.registry.load()
        assert doc.tombstones[service.name].status == Status.ABANDONED

    def test_abandon_refuses_claims_already_in_base(self, service, s3_client):
        _created_and_applied(service)
        _adopt_in_base(s3_client, service.overlay_key, NEW_ADDRESS)
        with pytest.raises(PolicyError, match="already merged"):
            service.abandon(keep_resources=False, dry_run=False, yes=True)
        assert service.store.exists(service.overlay_key)
        assert _status_of(service) == Status.ACTIVE

    def test_abandon_destroy_plan_touching_base_is_refused(self, service, monkeypatch):
        _created_and_applied(service)
        original = FakeRunner.plan

        def plan_with_base_delete(self, out, **kwargs):
            rc = original(self, out, **kwargs)
            if kwargs.get("destroy"):
                doc = json.loads(out.read_text())
                doc["resource_changes"].append(
                    FakeRunner._entry(
                        "aws_s3_bucket.logs",
                        {"type": "aws_s3_bucket", "name": "logs", "mode": "managed"},
                        {"attributes": {"bucket": "s3-acme-dev-logs"}},
                        ["delete"],
                    )
                )
                out.write_text(json.dumps(doc))
            return rc

        monkeypatch.setattr(FakeRunner, "plan", plan_with_base_delete)
        FakeRunner.calls.clear()
        with pytest.raises(PolicyError, match="non-owned"):
            service.abandon(keep_resources=False, dry_run=False, yes=True)
        assert not [c for c in FakeRunner.calls if c[0] == "apply"]
        assert service.store.exists(service.overlay_key)
        assert _status_of(service) == Status.ACTIVE

    def test_abandon_typed_confirmation_mismatch_aborts(self, service, console):
        _created_and_applied(service)
        console.ask = lambda _prompt: "nope"
        FakeRunner.calls.clear()
        with pytest.raises(ToolError, match="abandon aborted"):
            service.abandon(keep_resources=False, dry_run=False, yes=False)
        assert not [c for c in FakeRunner.calls if c[0] == "apply"]
        assert service.store.exists(service.overlay_key)
        assert _status_of(service) == Status.ACTIVE

    def test_abandon_keeps_pending_revert_for_update_claims(self, service, make_claim):
        _created_and_applied(service)

        def add_update_claim(doc):
            doc.overlays[service.name].claims["aws_iam_role.app"] = make_claim(
                "update", "aws_iam_role", {"name": "iam-acme-dev-app"}
            )

        service.registry.update(add_update_claim)
        service.abandon(keep_resources=False, dry_run=False, yes=True)
        doc, _ = service.registry.load()
        assert service.name not in doc.overlays
        assert doc.tombstones[service.name].pending_revert == ["aws_iam_role.app"]
        assert service.status()["tombstones"][service.name]["pending_revert"] == [
            "aws_iam_role.app"
        ]
        report = service.doctor()
        assert any(
            f["code"] == "pending-revert" and "aws_iam_role.app" in f["message"] for f in report
        )

    def test_abandon_destroys_only_own_resources(self, service, s3_client, base_in_s3):
        service.create()
        service.apply(auto_approve=True, allow_stale=False, allow_behind=False)
        service.abandon(keep_resources=False, dry_run=True, yes=True)
        assert service.store.exists(service.overlay_key)

        FakeRunner.calls.clear()
        service.abandon(keep_resources=False, dry_run=False, yes=True)
        destroyed = [c for c in FakeRunner.calls if c[0] == "apply"]
        assert destroyed == [["apply", service.overlay_key]]
        assert not service.store.exists(service.overlay_key)
        assert any(".abandoned-" in k for k in service.store.list_prefix(f"{BASE_KEY}@"))
        base = json.loads(s3_client.get_object(Bucket=BUCKET, Key=BASE_KEY)["Body"].read())
        assert statemod.addresses(base) == statemod.addresses(base_in_s3)
        doc, _ = service.registry.load()
        assert doc.tombstones[service.name].status == Status.ABANDONED

    def test_gc_and_doctor(self, service, env_repo):
        service.create()
        findings = service.gc(purge=False, yes=True)
        assert any(f["kind"] == "orphan-branch" for f in findings)  # branch never pushed
        report = service.doctor()
        assert not [f for f in report if f["level"] == "error"], report


class TestFinalize:
    def test_finalize_refuses_different_id_in_base(self, service, s3_client):
        _created_and_applied(service)
        _merged(service)
        _adopt_in_base(s3_client, service.overlay_key, NEW_ADDRESS, {"id": "trunk-made-its-own"})
        with pytest.raises(PolicyError, match="created its own objects"):
            service.finalize(purge=False, yes=True)
        assert service.store.exists(service.overlay_key)
        assert _status_of(service) == Status.MERGING

    def test_finalize_accepts_recreated_address_absent_from_imports_file(
        self, service, s3_client, env_repo, console
    ):
        _created_and_applied(service)
        path = MergeService(service).merge(
            allow_import_updates=False,
            accept_recreate=[NEW_ADDRESS],
            allow_unapplied=False,
            yes=True,
        )
        assert read_imports_addresses(path) == {}
        assert _status_of(service) == Status.MERGING
        assert "recreated by the trunk" in console.stderr.getvalue()
        # verify mode (plan while merging) agrees
        policy, _summary, _planfile, _stale = service.plan()
        assert policy.ok, policy.violations
        _adopt_in_base(s3_client, service.overlay_key, NEW_ADDRESS, {"id": "trunk-made-its-own"})
        service.finalize(purge=False, yes=True)
        doc, _ = service.registry.load()
        assert doc.tombstones[service.name].status == Status.MERGED
        assert "orphaned" in console.stderr.getvalue()

    def test_finalize_purge_deletes_overlay_only(self, service, s3_client):
        _created_and_applied(service)
        _merged(service)
        _adopt_in_base(s3_client, service.overlay_key, NEW_ADDRESS)
        service.finalize(purge=True, yes=True)
        assert not service.store.exists(service.overlay_key)
        assert service.store.exists(BASE_KEY)
        assert not any(".merged-" in k for k in service.store.list_prefix(f"{BASE_KEY}@"))


class TestMerge:
    def test_merge_verification_failure_removes_imports_file(
        self, service, env_repo, fake_runner
    ):
        _created_and_applied(service)
        fake_runner.desired.pop("aws_s3_bucket.logs")  # the verify plan now deletes a base resource
        with pytest.raises(PolicyError, match="merge verification failed"):
            _merged(service)
        assert not (env_repo / IMPORTS_FILENAME.format(name=service.name)).exists()
        assert _status_of(service) == Status.ACTIVE
        assert list(service.base_data_dir.glob("tfplan.*")) == []

    def test_merge_undo_returns_to_active(self, service, env_repo):
        _created_and_applied(service)
        path = _merged(service)
        assert path.exists()
        MergeService(service).undo(yes=True)
        assert not path.exists()
        assert _status_of(service) == Status.ACTIVE
        with pytest.raises(PolicyError, match="nothing to undo"):
            MergeService(service).undo(yes=True)
        assert _merged(service).exists()

    def test_merge_refuses_entry_changed_since_load(self, service, env_repo, monkeypatch):
        """A CI apply landing during the verify plan must not be overwritten."""
        _created_and_applied(service)
        merge_service = MergeService(service)
        original = merge_service.verify

        def verify_after_concurrent_apply(**kwargs):
            service.registry.set_status(service.name, Status.ACTIVE, applied_commit="e" * 40)
            return original(**kwargs)

        monkeypatch.setattr(merge_service, "verify", verify_after_concurrent_apply)
        with pytest.raises(RegistryError, match="changed since it was loaded"):
            merge_service.merge(
                allow_import_updates=False, accept_recreate=[], allow_unapplied=False, yes=True
            )
        assert not (env_repo / IMPORTS_FILENAME.format(name=service.name)).exists()
        assert _status_of(service) == Status.ACTIVE


# ---------------------------------------------------------------------- multi-stack


def _backend_block(key: str) -> str:
    return (
        f'terraform {{\n  backend "s3" {{\n    bucket         = "{BUCKET}"\n'
        f'    key            = "{key}"\n    region         = "{REGION}"\n'
        f'    dynamodb_table = "{LOCK_TABLE}"\n  }}\n}}\n\n'
    )


REMOTE_STATE_BLOCK = f"""
variable "tofu_overlay_keys" {{
  type    = map(string)
  default = {{}}
}}

data "terraform_remote_state" "eks" {{
  backend = "s3"
  config = {{
    bucket = "{BUCKET}"
    key    = lookup(var.tofu_overlay_keys, "{EKS_KEY}", "{EKS_KEY}")
    region = "{REGION}"
  }}
}}
"""


@pytest.fixture
def multi_stack(env_repo, s3_client, base_in_s3, backend_cfg, boto_session, fake_runner, console):
    """Producer stack ``eks`` and consumer stack ``lambda`` (reads eks through remote_state).

    Returns a factory building a fresh service for ``"eks"`` or ``"lambda"``; a
    fresh instance per step mirrors one CLI invocation (links are cached per
    instance).
    """
    repo = env_repo.parents[3]
    dirs = {"eks": repo / "stacks" / "eks" / "env" / "dev",
            "lambda": repo / "stacks" / "lambda" / "env" / "dev"}
    keys = {"eks": EKS_KEY, "lambda": LAMBDA_KEY}
    for kind, d in dirs.items():
        d.mkdir(parents=True)
        body = 'resource "aws_s3_bucket" "reports" {}\n' if kind == "eks" else REMOTE_STATE_BLOCK
        (d / "main.tf").write_text(_backend_block(keys[kind]) + body)
        s3_client.put_object(Bucket=BUCKET, Key=keys[kind], Body=json.dumps(base_in_s3).encode())
    git("add", ".", cwd=repo)
    git("commit", "-q", "-m", "eks and lambda stacks", cwd=repo)
    cfg = config.load_config(env_repo)

    def make(kind: str) -> OverlayService:
        return OverlayService(
            dirs[kind], cfg, backend_cfg.model_copy(update={"key": keys[kind]}), console,
            session=boto_session, runner_factory=fake_runner,
        )

    return make


def _plan_env(service) -> dict[str, str]:
    """Run a plan and return the env of the runner that executed it."""
    FakeRunner.envs.clear()
    policy, _summary, _planfile, _stale = service.plan()
    assert policy.ok, policy.violations
    plan_env = [e for e in FakeRunner.envs if REMOTE_KEYS_VAR in e]
    assert plan_env, "no runner received the overlay variables"
    assert all(e == plan_env[0] for e in plan_env)
    return plan_env[0]


class TestMultiStack:
    def test_consumer_reads_producer_overlay_until_finalize(
        self, multi_stack, s3_client, console
    ):
        name = config.overlay_name_for(BRANCH)
        # No producer overlay yet: the consumer reads the base.
        consumer = multi_stack("lambda")
        consumer.create()
        assert json.loads(_plan_env(consumer)[REMOTE_KEYS_VAR]) == {}

        producer = multi_stack("eks")
        _created_and_applied(producer)
        producer_key = producer.overlay_key
        assert producer_key == f"{EKS_KEY}@{name}"

        consumer = multi_stack("lambda")
        env = _plan_env(consumer)
        assert json.loads(env[REMOTE_KEYS_VAR]) == {EKS_KEY: producer_key}
        assert f"remote state eks: reading overlay {producer_key}" in console.stderr.getvalue()
        assert consumer.status()["remote_overlays"] == {EKS_KEY: producer_key}
        ok, errors, warnings = consumer.check()
        assert ok, errors
        assert not any("merging" in w for w in warnings)

        # The producer freezes: the consumer still reads it, check says so.
        _merged(producer)
        consumer = multi_stack("lambda")
        ok, errors, warnings = consumer.check()
        assert ok, errors
        assert any("merging" in w and producer_key in w for w in warnings)
        assert json.loads(_plan_env(consumer)[REMOTE_KEYS_VAR]) == {EKS_KEY: producer_key}

        # Finalize warns about the consumer overlay, then the consumer falls back to the base.
        _adopt_in_base(s3_client, producer_key, NEW_ADDRESS, base_key=EKS_KEY)
        before = len(console.stderr.getvalue())
        producer.finalize(purge=False, yes=True)
        finalize_output = console.stderr.getvalue()[before:]
        assert "re-plan it after finalize" in finalize_output
        assert LAMBDA_KEY in finalize_output and "remote state eks" in finalize_output
        assert "stacks/lambda/env/dev" in finalize_output

        consumer = multi_stack("lambda")
        assert json.loads(_plan_env(consumer)[REMOTE_KEYS_VAR]) == {}
        ok, errors, warnings = consumer.check()
        assert ok, errors

    def test_consumer_check_errors_when_producer_object_vanished(
        self, multi_stack, s3_client
    ):
        producer = multi_stack("eks")
        producer.create()
        consumer = multi_stack("lambda")
        consumer.create()
        s3_client.delete_object(Bucket=BUCKET, Key=producer.overlay_key)
        consumer = multi_stack("lambda")
        assert consumer.remote_overlay_keys(consumer.name) == {}
        ok, errors, _warnings = consumer.check()
        assert not ok
        assert any("no longer exists" in e and producer.overlay_key in e for e in errors)

    def test_finalize_without_consumer_overlay_does_not_warn(
        self, multi_stack, s3_client, console
    ):
        producer = multi_stack("eks")
        _created_and_applied(producer)
        _merged(producer)
        _adopt_in_base(s3_client, producer.overlay_key, NEW_ADDRESS, base_key=EKS_KEY)
        producer.finalize(purge=False, yes=True)
        assert "re-plan it after finalize" not in console.stderr.getvalue()


# ---------------------------------------------------------------------- cli


@pytest.fixture
def cli_env(env_repo: Path, boto_session, fake_runner, monkeypatch):
    monkeypatch.setitem(cli.INJECT, "session", boto_session)
    monkeypatch.setitem(cli.INJECT, "runner_factory", fake_runner)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("TF_BUILD", raising=False)
    return env_repo


def _invoke(env_dir: Path, *args: str, key: str = BASE_KEY):
    runner = CliRunner()
    return runner.invoke(
        cli.app,
        [
            "-C", str(env_dir), "--bucket", BUCKET, "--key", key, "--region", REGION,
            "--dynamodb-table", LOCK_TABLE, "--no-color", *args,
        ],
    )


class TestCli:
    def test_print_backend(self, cli_env):
        result = _invoke(cli_env, "--print-backend")
        assert result.exit_code == 0, result.output
        assert f"s3://{BUCKET}/{BASE_KEY}" in result.output

    def test_base_key_not_allowed(self, cli_env):
        result = _invoke(cli_env, "create", key="acme/webshop/storage/prod")
        assert result.exit_code == int(ExitCode.NOT_ALLOWED), result.output

    def test_plan_without_overlay_is_not_found(self, cli_env):
        result = _invoke(cli_env, "plan")
        assert result.exit_code == int(ExitCode.NOT_ALLOWED), result.output
        result = _invoke(cli_env, "check")
        assert result.exit_code == 0, result.output

    def test_forbidden_passthrough(self, cli_env):
        result = _invoke(cli_env, "plan", "--", "-target=aws_s3_bucket.reports")
        assert result.exit_code == int(ExitCode.ERROR), result.output

    def test_json_outputs_are_single_documents(self, cli_env):
        assert _invoke(cli_env, "create").exit_code == 0
        result = _invoke(cli_env, "--json", "plan", "--detailed-exitcode")
        assert result.exit_code == int(ExitCode.CHANGES), result.output
        payload = json.loads(result.stdout)
        assert payload["schema"] == 1 and payload["summary"]["create"] == 1

        result = _invoke(cli_env, "--json", "status", "--repo")
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["schema"] == 1 and len(payload["bases"]) == 1

        result = _invoke(cli_env, "--json", "status")
        assert json.loads(result.stdout)["current"] == config.overlay_name_for(BRANCH)

        result = _invoke(cli_env, "--yes", "apply", "--auto-approve")
        assert result.exit_code == 0, result.output
        result = _invoke(cli_env, "--json", "check")
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["ok"] is True

    def test_json_plan_and_status_carry_remote_overlays(self, cli_env, multi_stack):
        producer = multi_stack("eks")
        producer.create()
        lambda_dir = cli_env.parents[3] / "stacks" / "lambda" / "env" / "dev"
        assert _invoke(lambda_dir, "create", key=LAMBDA_KEY).exit_code == 0
        result = _invoke(lambda_dir, "--json", "plan", key=LAMBDA_KEY)
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["remote_overlays"] == {EKS_KEY: producer.overlay_key}
        assert f"reading overlay {producer.overlay_key}" in result.output
        result = _invoke(lambda_dir, "--json", "status", key=LAMBDA_KEY)
        assert json.loads(result.stdout)["remote_overlays"] == {EKS_KEY: producer.overlay_key}
        result = _invoke(lambda_dir, "status", key=LAMBDA_KEY)
        assert result.exit_code == 0, result.output
        assert f"remote state {EKS_KEY}: reading overlay {producer.overlay_key}" in result.output

    def test_auto_approve_requires_yes(self, cli_env):
        assert _invoke(cli_env, "create").exit_code == 0
        result = _invoke(cli_env, "apply", "--auto-approve")
        assert result.exit_code == int(ExitCode.POLICY), result.output

    def test_apply_in_ci_without_yes_is_refused(self, cli_env, monkeypatch):
        assert _invoke(cli_env, "create").exit_code == 0
        monkeypatch.setenv("CI", "true")
        FakeRunner.calls.clear()
        result = _invoke(cli_env, "apply")
        assert result.exit_code == int(ExitCode.ERROR), result.output
        assert "apply aborted" in result.output
        assert not [c for c in FakeRunner.calls if c[0] == "apply"]
        result = _invoke(cli_env, "--yes", "apply", "--auto-approve")
        assert result.exit_code == 0, result.output

    def test_json_apply_merge_doctor_guard_single_document(self, cli_env, tmp_path):
        assert _invoke(cli_env, "create").exit_code == 0
        result = _invoke(cli_env, "--json", "--yes", "apply", "--auto-approve")
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["schema"] == 1 and payload["overlay"]["status"] == "active"

        result = _invoke(cli_env, "--json", "--yes", "merge")
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["imports_file"].endswith(".imports.tf")

        result = _invoke(cli_env, "--json", "doctor")
        assert result.exit_code == 0, result.output
        assert "findings" in json.loads(result.stdout)

        plan_json = tmp_path / "trunk-plan.json"
        plan_json.write_text(json.dumps({"format_version": "1.2", "resource_changes": []}))
        result = _invoke(cli_env, "--json", "guard", str(plan_json))
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["ok"] is True

        plan_json.write_text(json.dumps({
            "format_version": "1.2",
            "resource_changes": [{
                "address": "aws_s3_bucket.trunk_reports", "mode": "managed",
                "type": "aws_s3_bucket", "name": "trunk_reports",
                "change": {"actions": ["create"], "before": None,
                           "after": {"bucket": NEW_BUCKET}},
            }],
        }))
        result = _invoke(cli_env, "--json", "guard", str(plan_json))
        assert result.exit_code == int(ExitCode.POLICY), result.output
        payload = json.loads(result.stdout)
        assert payload["ok"] is False
        assert payload["violations"][0]["rule"] == "identity-claimed"

    def test_exit_codes_for_stale_frozen_registry(self, cli_env, s3_client, base_in_s3):
        assert _invoke(cli_env, "create").exit_code == 0
        _move_base(s3_client, base_in_s3)
        result = _invoke(cli_env, "--yes", "apply", "--auto-approve")
        assert result.exit_code == int(ExitCode.STALE), result.output
        assert _invoke(cli_env, "--yes", "rebase").exit_code == 0
        assert _invoke(cli_env, "--yes", "apply", "--auto-approve").exit_code == 0
        assert _invoke(cli_env, "--yes", "merge").exit_code == 0
        result = _invoke(cli_env, "--yes", "apply", "--auto-approve")
        assert result.exit_code == int(ExitCode.FROZEN), result.output
        result = _invoke(cli_env, "--yes", "abandon")
        assert result.exit_code == int(ExitCode.FROZEN), result.output
        registry_key = f"{BASE_KEY}.overlays.json"
        s3_client.put_object(Bucket=BUCKET, Key=registry_key, Body=b"{oops")
        result = _invoke(cli_env, "status")
        assert result.exit_code == int(ExitCode.REGISTRY), result.output
        result = _invoke(cli_env, "--yes", "apply", "--auto-approve")
        assert result.exit_code == int(ExitCode.REGISTRY), result.output

    def test_chdir_with_relative_backend_config(self, cli_env, monkeypatch):
        repo = cli_env.parents[3]
        (repo / "dev.s3.tfbackend").write_text(
            f'bucket = "{BUCKET}"\nkey = "{BASE_KEY}"\nregion = "{REGION}"\n'
            f'dynamodb_table = "{LOCK_TABLE}"\n'
        )
        monkeypatch.chdir(repo)
        result = CliRunner().invoke(
            cli.app,
            ["-C", "stacks/storage/env/dev", "--backend-config", "dev.s3.tfbackend", "--json",
             "--print-backend"],
        )
        assert result.exit_code == 0, result.output
        backend = json.loads(result.stdout)["backend"]
        assert backend["bucket"] == BUCKET and backend["key"] == BASE_KEY
        assert backend["backend_config_files"] == [str((repo / "dev.s3.tfbackend").resolve())]
        # an inline key=value entry is kept verbatim
        result = CliRunner().invoke(
            cli.app,
            ["-C", "stacks/storage/env/dev", "--backend-config", "dev.s3.tfbackend",
             "--backend-config", "region=eu-west-3", "--json", "--print-backend"],
        )
        assert result.exit_code == 0, result.output
        backend = json.loads(result.stdout)["backend"]
        assert backend["region"] == "eu-west-3"
        assert backend["backend_config_files"][1] == "region=eu-west-3"

    def test_merge_refuses_symlinked_env_dir(self, cli_env, service):
        _created_and_applied(service)
        link = cli_env.parent / "dev-link"
        link.symlink_to(cli_env, target_is_directory=True)
        result = _invoke(link, "--yes", "merge")
        assert result.exit_code == int(ExitCode.ERROR), result.output
        assert "symlink" in result.output
        assert not list(cli_env.glob("zz_overlay_*.imports.tf"))
        assert _status_of(service) == Status.ACTIVE
        assert _invoke(link, "create").exit_code == int(ExitCode.ERROR)
        # read-only commands still work through the link
        assert _invoke(link, "status").exit_code == 0
        result = _invoke(link, "--json", "status", "--repo")
        assert result.exit_code == 0, result.output
        assert len(json.loads(result.stdout)["bases"]) == 1  # the symlink is not a second env


class TestClaimsFromState:
    def test_sensitive_paths_never_reach_claims(self, service, make_claim):
        """State v4 flags sensitive values as paths; they must not feed identity/import ids."""
        doc = {
            "version": 4,
            "serial": 1,
            "lineage": "x",
            "resources": [
                {
                    "mode": "managed",
                    "type": "aws_ssm_parameter",
                    "name": "secret",
                    "provider": PROVIDER,
                    "instances": [
                        {
                            "schema_version": 0,
                            "attributes": {"id": "/acme/secret", "name": "/acme/secret",
                                           "value": "hunter2"},
                            "sensitive_attributes": [[{"type": "get_attr", "value": "value"}]],
                        }
                    ],
                }
            ],
        }
        claims = {"aws_ssm_parameter.secret": make_claim(type_="aws_ssm_parameter")}
        filled = service._claims_from_state(claims, doc)
        claim = filled["aws_ssm_parameter.secret"]
        assert claim.id == "/acme/secret"
        assert "hunter2" not in json.dumps(claim.model_dump())


class TestLineageRegression:
    """Regression for the first real run: `tofu state push` without -force into an empty
    key generates its own lineage; the registry must record the stored one."""

    def test_create_records_the_stored_lineage(self, service, s3_client):
        ov = service.create()
        stored = json.loads(
            s3_client.get_object(Bucket=BUCKET, Key=service.overlay_key)["Body"].read()
        )
        assert stored["lineage"] == ov.lineage
        assert any(call[0] == "state push" for call in FakeRunner.calls)
        # A fresh service validates the overlay (lineage check) before planning.
        policy, _summary, _planfile, _stale = service.plan()
        assert policy.ok

    def test_doctor_reports_lineage_mismatch(self, service):
        service.create()
        name = service.name

        def tamper(d):
            d.overlays[name] = d.overlays[name].model_copy(update={"lineage": "bogus"})

        service.registry.update(tamper)
        codes = {f["code"] for f in service.doctor()}
        assert "lineage-mismatch" in codes
        with pytest.raises(RegistryError):
            service.plan()
