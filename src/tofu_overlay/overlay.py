"""Overlay orchestration: create/plan/apply/status/check/rebase/abandon/finalize/gc/doctor.

This module wires the lower layers together following DESIGN.md sections 3, 5
and 6. It never writes a state object itself: every state read is a
``tofu state pull`` and every state write is a ``tofu state push`` or an
``apply`` run in a dedicated data directory (invariant 3.1). The only raw S3
operations are HEAD, ListObjects, CopyObject to an archive key and the deletes
performed at finalize/abandon.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tofu_overlay import __version__, config
from tofu_overlay import plan as planmod
from tofu_overlay import state as statemod
from tofu_overlay.backend import parse_remote_state_refs, resolve_backend
from tofu_overlay.identity import TypeKnowledge
from tofu_overlay.models import (
    BackendConfig,
    Claim,
    ClaimKind,
    FrozenError,
    NotAllowedError,
    Overlay,
    OverlayError,
    PlanSummary,
    PolicyError,
    PolicyResult,
    RegistryDoc,
    RegistryError,
    RemoteStateRef,
    StaleError,
    Status,
    ToolConfig,
    ToolError,
    Violation,
    new_run_id,
    utcnow_iso,
)
from tofu_overlay.output import Console
from tofu_overlay.registry import Registry
from tofu_overlay.s3state import S3State
from tofu_overlay.tofu import TofuRunner, validate_passthrough

DATA_DIR_NAME = ".tofu-overlay"
BASE_DIR_NAME = "_base"
BASE_CACHE_FILE = "base_addresses.json"
ARCHIVE_RE = re.compile(r"@(?P<name>[a-z0-9-]+)\.(?P<status>merged|abandoned|rebase)-\d{8}T\d{6}Z")
IMPORTS_GLOB = "zz_overlay_*.imports.tf"
PLAN_GLOB = "tfplan.*"
NAME_VAR = "TF_VAR_tofu_overlay_name"
REMOTE_KEYS_VAR = "TF_VAR_tofu_overlay_keys"

# Statuses that count as "live" in conflict checks (DESIGN 3.4).
_LIVE = {Status.CREATING, Status.ACTIVE, Status.APPLYING, Status.DIRTY, Status.MERGING}


def _archive_timestamp() -> str:
    """Timestamp suffix used in archive keys (UTC, second precision)."""
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _age_days(iso: str | None) -> float | None:
    """Age in days of an ISO timestamp, or None when unparsable."""
    if not iso:
        return None
    try:
        then = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=UTC)
    return round((datetime.now(UTC) - then).total_seconds() / 86400, 1)


def _has_changes(summary: PlanSummary) -> bool:
    """True when the plan summary reports at least one non-noop action."""
    total = summary.create + summary.update + summary.delete + summary.replace + summary.import_
    return total > 0


def _sensitive_keys(inst: dict) -> set[str]:
    """Top-level attribute names flagged sensitive in a state instance.

    State v4 stores ``sensitive_attributes`` as paths (``[[{"type": "get_attr",
    "value": "password"}]]``); older or hand-written documents may use plain
    names. A nested sensitive path drops its whole top-level attribute.
    """
    keys: set[str] = set()
    for entry in inst.get("sensitive_attributes") or []:
        if isinstance(entry, str):
            keys.add(entry)
        elif isinstance(entry, list) and entry:
            first = entry[0]
            if isinstance(first, dict) and first.get("type") == "get_attr":
                keys.add(str(first.get("value")))
    return keys


def _claim_dict(claim: Claim) -> dict[str, Any]:
    """Compact JSON-able view of a claim."""
    return {
        "kind": str(claim.kind),
        "type": claim.type,
        "id": claim.id,
        "import_id": claim.import_id,
        "identity": claim.identity,
    }


def _prune_plan_files(directory: Path, keep: Path | None = None) -> None:
    """Delete saved plan files (they embed sensitive values) except ``keep``."""
    if not directory.is_dir():
        return
    for path in directory.glob(PLAN_GLOB):
        if keep is not None and path == keep:
            continue
        path.unlink(missing_ok=True)


class OverlayService:
    """Orchestrates one overlay of one base state from a stack env directory."""

    def __init__(
        self,
        cwd: Path,
        cfg: ToolConfig,
        backend: BackendConfig,
        console: Console,
        *,
        name: str | None = None,
        session: Any = None,
        runner_factory: Callable[..., TofuRunner] | None = None,
    ) -> None:
        # Logical path (not resolved): a symlinked env dir must stay visible as such.
        self.cwd = Path(os.path.normpath(Path(cwd).absolute()))
        self.cfg = cfg
        self.backend = backend
        self.console = console
        self._explicit_name = name
        self._name: str | None = None
        self._session = session
        self._runner_factory = runner_factory or TofuRunner
        self._s3: S3State | None = None
        self._registry: Registry | None = None
        self._knowledge: TypeKnowledge | None = None
        self._repo_root: Path | None = None
        # Cross-stack remote_state links, computed once per command (MULTI-STACK.md).
        self._remote_refs_cache: list[RemoteStateRef] | None = None
        self._remote_links_cache: list[dict[str, Any]] | None = None
        self._remote_warnings: list[str] = []
        # Injectable subprocess entry point for the few git calls config.py does not cover.
        self.git_run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run

    # ------------------------------------------------------------------ properties

    @property
    def name(self) -> str:
        """Overlay name: --name > TOFU_OVERLAY_NAME > derived from the current branch."""
        if self._name is None:
            self._name = config.resolve_overlay_name(self._explicit_name, self.cwd)
        return self._name

    @property
    def explicit_name(self) -> bool:
        """True when the name was given explicitly (branch check is then skipped)."""
        return self._explicit_name is not None

    @property
    def s3(self) -> S3State:
        """S3/DynamoDB access for the resolved backend."""
        if self._s3 is None:
            self._s3 = S3State(self.backend, session=self._session)
        return self._s3

    @property
    def registry(self) -> Registry:
        """Registry document accessor for this base."""
        if self._registry is None:
            self._registry = Registry(self.s3, self.backend, __version__)
            self._registry.apply_timeout_min = self.cfg.policy.apply_timeout_min
        return self._registry

    @property
    def knowledge(self) -> TypeKnowledge:
        """Type knowledge (identity attributes, import formats)."""
        if self._knowledge is None:
            self._knowledge = TypeKnowledge.load(self.cfg)
        return self._knowledge

    @property
    def repo_root(self) -> Path:
        """Git repository root (falls back to cwd)."""
        if self._repo_root is None:
            self._repo_root = config.find_repo_root(self.cwd) or self.cwd
        return self._repo_root

    @property
    def data_dir(self) -> Path:
        """TF_DATA_DIR of the overlay."""
        return self.cwd / DATA_DIR_NAME / self.name

    @property
    def base_data_dir(self) -> Path:
        """TF_DATA_DIR used for read-only operations on the base."""
        return self.cwd / DATA_DIR_NAME / BASE_DIR_NAME

    @property
    def overlay_key(self) -> str:
        """S3 key of the overlay state."""
        return self.backend.overlay_key(self.name)

    @property
    def base_key(self) -> str:
        """S3 key of the base state."""
        return self.backend.state_path()

    def refuse_symlinked_env(self) -> None:
        """Mutating commands refuse an env dir reached through a symlink below the repo.

        Files written there (imports file, data dirs) would land in the target
        directory, i.e. in every env that points at it.
        """
        link = config.symlinked_ancestor(self.cwd)
        if link is not None:
            raise ToolError(f"{link} is a symlink: refusing to work in {self.cwd}")

    def _state_key(self, ov: Overlay) -> str:
        """Overlay state key derived from the name, checked against the registry entry.

        Every delete/copy targets this key; it can never be the base key.
        """
        key = self.backend.overlay_key(ov.name)
        if ov.state_key != key or key == self.base_key:
            raise RegistryError(
                f"overlay '{ov.name}' state_key {ov.state_key!r} does not match {key!r}; "
                "run `doctor`"
            )
        return key

    # ------------------------------------------------------------------ runners

    def _env(self) -> dict[str, str]:
        """Overlay name and remote overlay keys exported to every tofu run (DESIGN 6)."""
        try:
            name = self.name
        except OverlayError:
            return {}
        keys = json.dumps(self.remote_overlay_keys(name), sort_keys=True)
        return {NAME_VAR: name, REMOTE_KEYS_VAR: keys}

    def _runner(self, data_dir: Path) -> TofuRunner:
        """Build a tofu runner bound to a data dir (injectable through runner_factory)."""
        data_dir.mkdir(parents=True, exist_ok=True)
        return self._runner_factory(
            self.cfg.binary, self.cwd, data_dir, env=self._env(), stream=self.console.stream
        )

    def _overlay_runner(self) -> TofuRunner:
        """Runner whose backend points at the overlay key (init when needed)."""
        runner = self._runner(self.data_dir)
        if runner.needs_init(self.overlay_key):
            self.console.info(f"init overlay data dir ({self.overlay_key})")
            runner.init(self.backend, self.overlay_key)
        runner.ensure_backend_key(self.overlay_key)
        return runner

    def _base_runner(self) -> TofuRunner:
        """Runner whose backend points at the base key, read-only use."""
        runner = self._runner(self.base_data_dir)
        if runner.needs_init(self.base_key):
            self.console.info(f"init base data dir ({self.base_key})")
            runner.init(self.backend, self.base_key)
        runner.ensure_backend_key(self.base_key)
        return runner

    # ------------------------------------------------------------------ git helpers

    def _git(self, *args: str) -> str | None:
        try:
            proc = self.git_run(
                ["git", *args], cwd=str(self.cwd), capture_output=True, text=True, check=False
            )
        except OSError:
            return None
        if proc.returncode != 0:
            return None
        return proc.stdout.strip() or None

    def _trunk_commit(self) -> str | None:
        return self._git("rev-parse", f"origin/{self.cfg.policy.trunk_branch}")

    def _head_commit(self) -> str | None:
        try:
            return config.head_commit(self.cwd)
        except OverlayError:
            return None

    def _check_ancestry(self, *, allow_behind: bool) -> None:
        """DESIGN 3.8: the branch must contain the trunk unless --allow-behind (never in CI)."""
        trunk = self.cfg.policy.trunk_branch
        if allow_behind and config.is_ci():
            raise PolicyError("--allow-behind is refused in CI")
        contains = config.branch_contains_trunk(self.cwd, trunk)
        if contains is None:
            self.console.warn(f"origin/{trunk} is unknown locally; ancestry not checked")
            return
        if contains:
            return
        if allow_behind:
            self.console.warn(f"branch does not contain origin/{trunk} (--allow-behind)")
            return
        raise StaleError(f"branch is behind origin/{trunk}; rebase your branch (or --allow-behind)")

    def _caller_arn(self) -> str | None:
        try:
            session = self._session
            if session is None:
                import boto3

                session = boto3.Session(
                    profile_name=self.backend.profile, region_name=self.backend.region
                )
            return session.client("sts").get_caller_identity().get("Arn")
        except Exception:  # noqa: BLE001 - informative field only
            return None

    # ------------------------------------------------------------------ registry helpers

    def _load(self) -> tuple[RegistryDoc, Overlay]:
        doc, _ = self.registry.load()
        return doc, self.registry.get_overlay(doc, self.name)

    def _effective_status(self, ov: Overlay) -> Status:
        """`applying` older than the timeout is treated as `dirty` (DESIGN 5)."""
        if ov.status == Status.APPLYING and self.registry.stale_applying(
            ov, self.cfg.policy.apply_timeout_min
        ):
            return Status.DIRTY
        return ov.status

    def _gate(self, ov: Overlay, allowed: set[Status], command: str) -> Status:
        """Refuse `command` unless the effective status is in `allowed`."""
        status = self._effective_status(ov)
        if status in allowed:
            return status
        name = ov.name
        if status == Status.MERGING:
            raise FrozenError(
                f"overlay '{name}' is frozen (merging): `{command}` refused; "
                "use `merge --undo` or `finalize`"
            )
        if status == Status.CREATING:
            raise RegistryError(f"overlay '{name}' is still being created; re-run `create`")
        if status == Status.APPLYING:
            raise RegistryError(
                f"overlay '{name}' has an apply in progress "
                f"(run {ov.run_id} since {ov.applying_since}); `{command}` refused"
            )
        if status in (Status.MERGED, Status.ABANDONED):
            raise NotAllowedError(f"overlay '{name}' is {status}: `{command}` refused")
        if status == Status.NEEDS_REVIEW:
            raise RegistryError(f"overlay '{name}' needs review (base lineage changed)")
        raise PolicyError(f"overlay '{name}' is {status}: `{command}` refused")

    def _check_base_lineage(self, doc: RegistryDoc, base_doc: dict) -> None:
        """DESIGN 5: the registry's base lineage must match the pulled base state."""
        pulled = base_doc.get("lineage")
        recorded = doc.base.get("lineage")
        if recorded and pulled and recorded != pulled:
            raise RegistryError(
                f"base lineage changed ({recorded} -> {pulled}); every overlay of this base "
                "needs review"
            )

    # ------------------------------------------------------------------ remote state links

    def _remote_refs(self) -> list[RemoteStateRef]:
        if self._remote_refs_cache is None:
            self._remote_refs_cache = parse_remote_state_refs(self.cwd)
        return self._remote_refs_cache

    def _remote_backend(self, ref: RemoteStateRef) -> BackendConfig:
        """Backend of the base a ref reads: current backend with the ref's bucket/key/region."""
        return self.backend.model_copy(
            update={
                "bucket": ref.bucket or self.backend.bucket,
                "key": ref.key,
                "region": ref.region or self.backend.region,
                "backend_config_files": [],
            }
        )

    def _is_own_base(self, cfg: BackendConfig) -> bool:
        return cfg.bucket == self.backend.bucket and cfg.state_path() == self.base_key

    def _remote_overlay(self, cfg: BackendConfig, name: str) -> Overlay | None:
        """Live overlay ``name`` registered on another base (``None`` when absent).

        A missing registry means no overlays; registry errors bubble up.
        """
        s3 = S3State(cfg, session=self._session)
        doc, _etag = Registry(s3, cfg, __version__).load()
        ov = doc.overlays.get(name)
        if ov is None or ov.status not in _LIVE:
            return None
        return ov

    def _remote_link(self, ref: RemoteStateRef, name: str) -> dict[str, Any] | None:
        cfg = self._remote_backend(ref)
        if self._is_own_base(cfg):
            return None
        ov = self._remote_overlay(cfg, name)
        if ov is None:
            return None
        exists = S3State(cfg, session=self._session).head(ov.state_key) is not None
        return {
            "ref": ref.name,
            "bucket": cfg.bucket,
            "key": cfg.key,
            "overlay_key": ov.state_key,
            "status": str(ov.status),
            "exists": exists,
        }

    def _remote_links(self, name: str) -> list[dict[str, Any]]:
        """Refs of this stack that resolve to a live overlay ``name`` on another base (cached)."""
        if self._remote_links_cache is not None:
            return self._remote_links_cache
        links: list[dict[str, Any]] = []
        for ref in self._remote_refs():
            if ref.unresolved:
                self._remote_warnings.append(
                    f"remote state {ref.name}: key is not a literal, it cannot be mapped to an "
                    "overlay (the base is read)"
                )
                continue
            try:
                link = self._remote_link(ref, name)
            except OverlayError as exc:
                self._remote_warnings.append(f"remote state {ref.name}: skipped ({exc})")
                continue
            if link is not None:
                links.append(link)
        self._remote_links_cache = links
        return links

    def remote_overlay_keys(self, name: str) -> dict[str, str]:
        """``{base key -> overlay key}`` for refs whose base holds a live overlay ``name``.

        Read-only. Only overlays whose state object exists are mapped; refs to
        this stack's own base, unresolved refs and unreadable registries are
        skipped (the latter two are reported as warnings by ``plan``/``check``).
        """
        return {
            link["key"]: link["overlay_key"] for link in self._remote_links(name) if link["exists"]
        }

    def _report_remote_overlays(self) -> None:
        """One line per mapped ref plus the warnings collected while resolving them."""
        for warning in self._remote_warnings:
            self.console.warn(warning)
        for link in self._remote_links(self.name):
            if link["exists"]:
                self.console.info(
                    f"remote state {link['ref']}: reading overlay {link['overlay_key']}"
                )
            else:
                self.console.warn(
                    f"remote state {link['ref']}: overlay object {link['overlay_key']} is "
                    "missing, the base is read"
                )

    def _check_remote_links(self, errors: list[str], warnings: list[str]) -> None:
        links = self._remote_links(self.name)
        warnings += self._remote_warnings
        for link in links:
            where = f"remote state {link['ref']} ({link['key']})"
            if not link["exists"]:
                errors.append(f"{where}: overlay object {link['overlay_key']} no longer exists")
            elif link["status"] == Status.MERGING:
                warnings.append(
                    f"{where}: reads overlay {link['overlay_key']} which is merging and will "
                    "disappear at its finalize; re-plan this overlay afterwards"
                )

    def _sibling_env_dirs(self, root: Path) -> list[Path]:
        me = self.cwd.resolve()
        return sorted(
            p
            for p in root.glob(self.cfg.policy.env_dir_glob)
            if p.is_dir() and p.resolve() != me and config.symlinked_ancestor(p) is None
        )

    def _refs_to_own_base(self, env_dir: Path) -> list[RemoteStateRef]:
        try:
            refs = parse_remote_state_refs(env_dir)
        except OverlayError:
            return []
        return [
            r
            for r in refs
            if not r.unresolved
            and r.key == self.base_key
            and (r.bucket or self.backend.bucket) == self.backend.bucket
        ]

    def _consumer_overlay(self, env_dir: Path, name: str) -> tuple[BackendConfig, Overlay] | None:
        """Live overlay ``name`` of the stack in ``env_dir``; best effort, ``None`` on error."""
        try:
            cfg = resolve_backend(
                env_dir, overrides={}, backend_config_files=[], data_dir=env_dir / ".terraform"
            )
            if self._is_own_base(cfg):
                return None
            ov = self._remote_overlay(cfg, name)
        except OverlayError:
            return None
        return None if ov is None else (cfg, ov)

    def _warn_consumers(self, ov: Overlay) -> None:
        """finalize: warn about live overlays of sibling stacks reading this base (best effort)."""
        root = config.find_repo_root(self.cwd)
        if root is None:
            return
        for env_dir in self._sibling_env_dirs(root):
            refs = self._refs_to_own_base(env_dir)
            if not refs:
                continue
            found = self._consumer_overlay(env_dir, ov.name)
            if found is None:
                continue
            cfg, _consumer = found
            names = ", ".join(r.name for r in refs)
            self.console.warn(
                f"overlay '{ov.name}' on s3://{cfg.bucket}/{cfg.key} "
                f"({os.path.relpath(env_dir, root)}) reads this base through remote state "
                f"{names}; re-plan it after finalize (it falls back to the base automatically)"
            )

    # ------------------------------------------------------------------ validation (3.7)

    def _validate_overlay(self, ov: Overlay) -> dict:
        """DESIGN 3.7: live entry, key exists, lineage and branch match. Returns the state."""
        if ov.status not in _LIVE:
            raise NotAllowedError(f"overlay '{ov.name}' is {ov.status}")
        if not self.explicit_name:
            branch = config.current_branch(self.cwd)
            if branch != ov.branch:
                raise PolicyError(
                    f"overlay '{ov.name}' belongs to branch '{ov.branch}', "
                    f"current branch is '{branch}' (use --name to force)"
                )
        if self.s3.head(ov.state_key) is None:
            raise RegistryError(
                f"overlay state object {ov.state_key} is missing; a missing key is never "
                "treated as an empty state (run `doctor`)"
            )
        doc = self._overlay_runner().state_pull()
        if statemod.is_encrypted(doc):
            raise ToolError("encrypted states are out of scope in v1")
        if ov.lineage and doc.get("lineage") != ov.lineage:
            raise RegistryError(
                f"overlay state lineage {doc.get('lineage')} differs from the registry "
                f"({ov.lineage}); run `doctor`"
            )
        return doc

    def _freshness(self, ov: Overlay) -> tuple[bool, str]:
        """DESIGN 3.8: stale when the base ETag differs from the recorded one."""
        head = self.s3.head(self.base_key)
        if head is None:
            raise ToolError(f"base state object {self.base_key} is missing")
        etag = head["etag"]
        return etag != ov.base_etag, etag

    # ------------------------------------------------------------------ base cache

    def _base_cache_path(self) -> Path:
        return self.base_data_dir / BASE_CACHE_FILE

    def _pull_base(self) -> dict:
        """Pull the base state through tofu in the base data dir (read-only)."""
        doc = self._base_runner().state_pull()
        if statemod.is_encrypted(doc):
            raise ToolError("encrypted states are out of scope in v1")
        return doc

    def _identities_of(self, doc: dict) -> set[str]:
        keys: set[str] = set()
        for _addr, (entry, inst) in statemod.index_instances(doc).items():
            if entry.get("mode") != "managed":
                continue
            type_ = entry["type"]
            attrs = inst.get("attributes") or {}
            identity = statemod.identity_from_state(attrs, type_, self.knowledge)
            key = self.knowledge.identity_key(type_, identity)
            if key:
                keys.add(key)
        return keys

    def _write_base_cache(self, doc: dict, etag: str | None) -> dict:
        payload = {
            "etag": etag,
            "serial": doc.get("serial"),
            "lineage": doc.get("lineage"),
            "addresses": sorted(statemod.addresses(doc)),
            "identities": sorted(self._identities_of(doc)),
        }
        self.base_data_dir.mkdir(parents=True, exist_ok=True)
        self._base_cache_path().write_text(json.dumps(payload, indent=1))
        return payload

    def _base_cache(self, refresh: bool) -> dict:
        path = self._base_cache_path()
        if not refresh and path.exists():
            try:
                return json.loads(path.read_text())
            except json.JSONDecodeError:
                pass
        head = self.s3.head(self.base_key)
        return self._write_base_cache(self._pull_base(), head["etag"] if head else None)

    def _base_addresses(self, refresh: bool = False) -> set[str]:
        """Addresses of the base state, cached under the base data dir."""
        return set(self._base_cache(refresh)["addresses"])

    def _base_identities(self, refresh: bool = False) -> set[str]:
        """Identity keys of the base state, cached alongside the addresses."""
        return set(self._base_cache(refresh)["identities"])

    # ------------------------------------------------------------------ claims from state

    def _claims_from_state(self, claims: dict[str, Claim], doc: dict) -> dict[str, Claim]:
        """Invariant 3.3: recompute id/import_id/identity of create claims from the state."""
        index = statemod.index_instances(doc)
        out: dict[str, Claim] = {}
        now = utcnow_iso()
        for address, claim in claims.items():
            if claim.kind != ClaimKind.CREATE or address not in index:
                out[address] = claim
                continue
            entry, inst = index[address]
            attrs = dict(inst.get("attributes") or {})
            for sensitive in _sensitive_keys(inst):
                attrs.pop(sensitive, None)
            type_ = entry.get("type", claim.type)
            identity = statemod.identity_from_state(attrs, type_, self.knowledge) or claim.identity
            dependencies = sorted(
                {str(d) for d in inst.get("dependencies") or [] if isinstance(d, str)}
            )
            out[address] = claim.model_copy(
                update={
                    "type": type_,
                    "id": attrs.get("id", claim.id),
                    "import_id": self.knowledge.import_id_for(type_, attrs) or claim.import_id,
                    "identity": identity,
                    "dependencies": dependencies,
                    "updated_at": now,
                }
            )
        return out

    def _missing_create_claims(self, ov: Overlay, doc: dict) -> list[str]:
        present = statemod.addresses(doc)
        return sorted(a for a in ov.create_claims() if a not in present)

    def _claims_in_base(self, ov: Overlay, base_doc: dict) -> tuple[list[str], list[str]]:
        """(addresses, identities) of create claims that exist in the base state."""
        base_addresses = statemod.addresses(base_doc)
        base_identities = self._identities_of(base_doc)
        by_address = sorted(a for a in ov.create_claims() if a in base_addresses)
        by_identity = []
        for address, claim in sorted(ov.create_claims().items()):
            key = self.knowledge.identity_key(claim.type, claim.identity)
            if key and key in base_identities:
                by_identity.append(f"{address} ({key})")
        return by_address, by_identity

    # ------------------------------------------------------------------ archive / delete

    def _overlay_prefix(self) -> str:
        key = self.backend.overlay_key("")
        return key[: key.index("@") + 1]

    def _archive(self, ov: Overlay, status: str, *, dry_run: bool = False) -> str:
        """Copy the overlay object to an archive key, then delete object, md5 item and tflock."""
        key = self._state_key(ov)
        archive_key = self.backend.archive_key(ov.name, status, _archive_timestamp())
        steps = [
            f"s3 CopyObject {key} -> {archive_key}",
            f"s3 DeleteObject {key}",
            f"dynamodb DeleteItem {self.backend.md5_lock_id(key)}",
            f"s3 DeleteObject {key}.tflock (if present)",
        ]
        for step in steps:
            self.console.info(("[dry-run] " if dry_run else "") + step)
        if dry_run:
            return archive_key
        self.s3.copy(key, archive_key)
        self._delete_state_object(key)
        return archive_key

    def _delete_state_object(self, key: str) -> None:
        """Delete an overlay state object with its digest item and lock file (never the base)."""
        if key == self.base_key:
            raise RegistryError(f"refusing to delete the base state {key}")
        self.s3.delete(key)
        if self.backend.dynamodb_table:
            self.s3.delete_md5_item(key)
        self.s3.delete_lockfile(key)

    def _remove_data_dir(self) -> None:
        if self.data_dir.exists():
            shutil.rmtree(self.data_dir, ignore_errors=True)

    def _confirm_typed(self, action: str, *, yes: bool) -> None:
        prompt = f"Type the overlay name to confirm `{action}` of '{self.name}'"
        if not self.console.confirm_typed(self.name, prompt, yes=yes):
            raise ToolError(f"{action} aborted")

    # ------------------------------------------------------------------ create

    def create(self, *, force_name: bool = False) -> Overlay:
        """Fork the base state into the overlay key and register it (resumes `creating`)."""
        name = self.name
        branch = config.current_branch(self.cwd) if not self.explicit_name else None
        doc, _etag = self.registry.load()
        existing = doc.overlays.get(name)
        resuming = existing is not None and existing.status == Status.CREATING
        self._refuse_create(doc, existing, force_name=force_name)
        base_head = self.s3.head(self.base_key)
        if base_head is None:
            raise ToolError(
                f"base state object {self.base_key} does not exist (no --empty-base in v1)"
            )
        if not resuming:
            self._refuse_unregistered_overlay_object()
        self.console.info(f"{'resume' if resuming else 'register'} overlay '{name}' (creating)")
        doc = self.registry.update(lambda d: self._register_creating(d, name, branch))
        ov = doc.overlays[name]

        base_doc = self._pull_base()
        self._check_base_lineage(doc, base_doc)
        self._write_base_cache(base_doc, base_head["etag"])
        forked = statemod.fork(base_doc)
        lineage = self._push_forked(ov, forked)

        fields = {
            "lineage": lineage,
            "base_etag": base_head["etag"],
            "base_serial": base_doc.get("serial"),
            "trunk_commit": self._trunk_commit(),
            "caller_arn": self._caller_arn(),
            "tofu_version": self._safe_version(),
            "updated_at": utcnow_iso(),
        }
        base_lineage = base_doc.get("lineage")

        def flip(d: RegistryDoc) -> None:
            if not d.base.get("lineage") and base_lineage:
                d.base["lineage"] = base_lineage
            entry = d.overlays[name]
            d.overlays[name] = entry.model_copy(update={**fields, "status": Status.ACTIVE})

        doc = self.registry.update(flip)
        self.console.success(f"overlay '{name}' active on {self.overlay_key}")
        return doc.overlays[name]

    def _refuse_create(
        self, doc: RegistryDoc, existing: Overlay | None, *, force_name: bool
    ) -> None:
        if existing is not None and existing.status != Status.CREATING:
            raise RegistryError(
                f"overlay '{self.name}' already exists with status {existing.status}"
            )
        if existing is None and not force_name:
            if self.registry.tombstoned(doc, self.name, self.cfg.policy.tombstone_days):
                raise RegistryError(
                    f"name '{self.name}' was recently {doc.tombstones[self.name].status}; "
                    "use --force-name to reuse it"
                )

    def _refuse_unregistered_overlay_object(self) -> None:
        if self.s3.exists(self.overlay_key):
            raise RegistryError(
                f"object {self.overlay_key} already exists outside the registry; run `doctor`"
            )
        if self.backend.dynamodb_table and self.s3.md5_item_exists(self.overlay_key):
            raise RegistryError(
                f"DynamoDB digest item for {self.overlay_key} exists outside the registry; "
                "run `doctor`"
            )

    def _register_creating(self, d: RegistryDoc, name: str, branch: str | None) -> None:
        if name in d.overlays:
            return
        now = utcnow_iso()
        d.overlays[name] = Overlay(
            name=name,
            state_key=self.overlay_key,
            lineage=None,
            branch=branch or self._git("branch", "--show-current") or "",
            owners=[config.git_user_email(self.cwd)],
            caller_arn=None,
            binary=self.cfg.binary,
            tofu_version=None,
            created_at=now,
            updated_at=now,
            base_serial=None,
            base_etag=None,
            trunk_commit=None,
            status=Status.CREATING,
            applied_commit=None,
            run_id=None,
            applying_since=None,
            claims={},
            pending_revert=[],
            last_apply=None,
        )

    def _push_forked(self, ov: Overlay, forked: dict) -> str:
        """Push the forked document to the overlay key; skip when a resume already did."""
        runner = self._overlay_runner()
        if self.s3.exists(self.overlay_key):
            current = runner.state_pull()
            if ov.lineage and current.get("lineage") == ov.lineage:
                self.console.info("overlay state already pushed, skipping")
                return ov.lineage
            self.console.warn("overlay object left by an interrupted create, overwriting")
            runner.state_push(forked, force=True)
        else:
            runner.state_push(forked)
        return forked["lineage"]

    def _safe_version(self) -> str | None:
        try:
            return self._runner(self.data_dir).version()
        except OverlayError:
            return None

    # ------------------------------------------------------------------ plan

    def plan(
        self,
        *,
        extra: list[str] | None = None,
        allow_behind: bool = False,
        detailed_exitcode: bool = False,
    ) -> tuple[PolicyResult, PlanSummary, Path, bool]:
        """Validate, plan against the overlay state and evaluate the policy (read-only)."""
        extra = list(extra or [])
        validate_passthrough(extra)
        doc, ov = self._load()
        status = self._gate(ov, {Status.ACTIVE, Status.DIRTY, Status.MERGING}, "plan")
        self._report_remote_overlays()
        if status == Status.MERGING:
            return self._verify_mode(ov)
        self._validate_overlay(ov)
        stale, _etag = self._freshness(ov)
        if stale:
            self.console.warn("overlay is stale: the base state moved since the fork (rebase)")
        self._check_ancestry(allow_behind=allow_behind)
        runner = self._overlay_runner()
        _prune_plan_files(self.data_dir)
        planfile = self.data_dir / f"tfplan.{new_run_id()}"
        runner.plan(planfile, extra=extra)
        changes, drift, summary = planmod.parse_plan(runner.show_json(planfile))
        policy = planmod.evaluate(
            changes,
            drift,
            base_addresses=self._base_addresses(),
            base_identities=self._base_identities(),
            me=ov,
            doc=doc,
            knowledge=self.knowledge,
            registry=self.registry,
        )
        self._report_policy(policy, summary)
        return policy, summary, planfile, stale

    def _verify_mode(self, ov: Overlay) -> tuple[PolicyResult, PlanSummary, Path, bool]:
        """`plan` in status merging re-runs the merge verification (DESIGN 8)."""
        from tofu_overlay.merge import MergeService

        self.console.info(f"overlay '{ov.name}' is merging: running verify mode")
        stale, _ = self._freshness(ov)
        ok, errors, warnings = MergeService(self).verify()
        violations = [
            Violation(address="", rule="merge-verify", message=e, other_overlay=None)
            for e in errors
        ]
        policy = PolicyResult(violations=violations, warnings=warnings, claims={})
        planfile = self.base_data_dir / "tfplan.verify"
        summary = PlanSummary()
        self._report_policy(policy, summary)
        return policy, summary, planfile, stale

    def _report_policy(self, policy: PolicyResult, summary: PlanSummary) -> None:
        self.console.info(planmod.render_summary(summary))
        for warning in policy.warnings:
            self.console.warn(warning)
        for v in policy.violations:
            other = f" (overlay {v.other_overlay})" if v.other_overlay else ""
            self.console.error(f"{v.rule}: {v.address} {v.message}{other}")
        if policy.ok:
            self.console.success("policy checks passed")

    # ------------------------------------------------------------------ apply

    def apply(
        self,
        *,
        auto_approve: bool,
        allow_stale: bool,
        allow_behind: bool,
        extra: list[str] | None = None,
        yes: bool = False,
    ) -> Overlay:
        """Plan, confirm, acquire claims atomically, apply, then record ids from the state.

        A saved plan never prompts in tofu, so the confirmation is the tool's
        own: skipped with ``--auto-approve`` or ``--yes``, refused in CI without
        them.
        """
        if config.is_ci() and (allow_stale or allow_behind):
            raise PolicyError("--allow-stale/--allow-behind are refused in CI")
        _doc, ov = self._load()
        self._gate(ov, {Status.ACTIVE, Status.DIRTY}, "apply")
        policy, summary, planfile, stale = self.plan(extra=extra, allow_behind=allow_behind)
        try:
            return self._apply_planned(
                ov, policy, summary, planfile, stale,
                allow_stale=allow_stale, confirmed=auto_approve or yes,
            )
        finally:
            planfile.unlink(missing_ok=True)

    def _apply_planned(
        self,
        ov: Overlay,
        policy: PolicyResult,
        summary: PlanSummary,
        planfile: Path,
        stale: bool,
        *,
        allow_stale: bool,
        confirmed: bool,
    ) -> Overlay:
        if stale and not allow_stale:
            raise StaleError("overlay is stale; run `rebase` (or --allow-stale outside CI)")
        if not policy.ok:
            raise PolicyError(f"{len(policy.violations)} policy violation(s), apply refused")
        if not _has_changes(summary):
            self.console.info("no changes, nothing to apply")
            return ov
        prompt = f"Apply {planmod.render_summary(summary)[:-1]} on overlay '{self.name}'?"
        if not self.console.confirm(prompt, yes=confirmed):
            raise ToolError("apply aborted")
        run_id = planfile.name.split(".", 1)[1]
        _stale, current_etag = self._freshness(ov)
        etag_for_cas = ov.base_etag if allow_stale else current_etag
        released = set(ov.claims) - set(policy.claims)
        self.console.info(f"acquiring {len(policy.claims)} claim(s), run {run_id}")
        doc = self.registry.acquire_claims(self.name, policy.claims, run_id, etag_for_cas or "")
        claims = {
            a: c for a, c in doc.overlays[self.name].claims.items() if a not in released
        }
        ok = False
        error: Exception | None = None
        try:
            self._overlay_runner().apply(planfile, auto_approve=True)
            ok = True
        except Exception as exc:  # noqa: BLE001 - recorded then re-raised
            error = exc
        finally:
            doc = self._finish_apply(ok, claims, summary, run_id=run_id, released=released)
        if error is not None:
            raise error
        final = doc.overlays[self.name]
        self.console.success(f"apply done, overlay '{self.name}' is {final.status}")
        return final

    def _finish_apply(
        self,
        ok: bool,
        claims: dict[str, Claim],
        summary: PlanSummary,
        *,
        run_id: str | None = None,
        released: set[str] | None = None,
    ) -> RegistryDoc:
        filled = claims
        to_release = set(released or ())
        try:
            state = self._overlay_runner().state_pull()
        except OverlayError as exc:
            self.console.warn(f"could not refresh claims from the overlay state: {exc}")
            to_release = set()  # unverified: keep the claims, `check` will report them
        else:
            filled = self._claims_from_state(claims, state)
            to_release -= statemod.addresses(state)  # only release what is really gone
        head = self.s3.head(self.base_key)
        return self.registry.finish_apply(
            self.name,
            ok=ok,
            claims=filled,
            applied_commit=self._head_commit(),
            caller_arn=self._caller_arn(),
            tofu_version=self._safe_version(),
            base_etag_after=head["etag"] if head else None,
            summary=summary.model_dump(by_alias=True),
            run_id=run_id,
            released=to_release,
        )

    # ------------------------------------------------------------------ status / list

    def _overlay_row(self, ov: Overlay, base_etag: str | None) -> dict[str, Any]:
        return {
            "name": ov.name,
            "branch": ov.branch,
            "owners": ov.owners,
            "status": str(self._effective_status(ov)),
            "fresh": bool(base_etag) and ov.base_etag == base_etag,
            "base_serial": ov.base_serial,
            "age_days": _age_days(ov.created_at),
            "updated_at": ov.updated_at,
            "applied_commit": ov.applied_commit,
            "run_id": ov.run_id,
            "claims": {a: _claim_dict(c) for a, c in sorted(ov.claims.items())},
            "pending_revert": ov.pending_revert,
            "last_apply": ov.last_apply,
        }

    def status(self) -> dict[str, Any]:
        """Overlays of this base with freshness, claims and pending reverts (read-only)."""
        doc, _ = self.registry.load()
        head = self.s3.head(self.base_key)
        base_etag = head["etag"] if head else None
        current: str | None
        try:
            current = self.name
        except OverlayError:
            current = None
        return {
            "base": {**self.backend.model_dump(include={"bucket", "key"}), "etag": base_etag,
                     "lineage": doc.base.get("lineage")},
            "registry_key": self.registry.key,
            "current": current if current in doc.overlays else None,
            "remote_overlays": self.remote_overlay_keys(current) if current else {},
            "overlays": [
                self._overlay_row(ov, base_etag) for _, ov in sorted(doc.overlays.items())
            ],
            "tombstones": {n: t.model_dump() for n, t in doc.tombstones.items()},
        }

    def list(self) -> list[dict[str, Any]]:
        """Compact rows for every overlay of this base (read-only)."""
        rows = []
        for row in self.status()["overlays"]:
            rows.append(
                {
                    "name": row["name"],
                    "branch": row["branch"],
                    "owners": row["owners"],
                    "status": row["status"],
                    "fresh": row["fresh"],
                    "age_days": row["age_days"],
                    "claims": len(row["claims"]),
                    "pending_revert": len(row["pending_revert"]),
                }
            )
        return rows

    # ------------------------------------------------------------------ check

    def check(self) -> tuple[bool, list[str], list[str]]:
        """CI gate (read-only): freshness, ancestry, claims vs states, conflicts, imports file."""
        errors: list[str] = []
        warnings: list[str] = []
        doc, _ = self.registry.load()
        ov = doc.overlays.get(self.name)
        if ov is None or ov.status not in _LIVE:
            return True, [], [f"no overlay for branch (name {self.name})"]
        stale, _ = self._freshness(ov)
        if stale:
            errors.append("stale: the base state moved since the fork, run `rebase`")
        contains = config.branch_contains_trunk(self.cwd, self.cfg.policy.trunk_branch)
        if contains is False:
            errors.append(f"behind: branch does not contain origin/{self.cfg.policy.trunk_branch}")
        elif contains is None:
            warnings.append("origin trunk unknown locally, ancestry not checked")
        try:
            overlay_doc = self._validate_overlay(ov)
        except OverlayError as exc:
            return False, [f"invalid overlay: {exc}"], warnings
        for address in self._missing_create_claims(ov, overlay_doc):
            errors.append(f"create claim {address} has no instance in the overlay state")
        by_address, by_identity = self._claims_in_base(ov, self._pull_base())
        errors += [f"base now contains claimed address {a}" for a in by_address]
        errors += [f"base now contains claimed identity {i}" for i in by_identity]
        for v in self.registry.conflicts_for(doc, self.name, ov.claims):
            errors.append(f"conflict: {v.address} {v.message} (overlay {v.other_overlay})")
        errors += self._check_imports_file(ov)
        self._check_remote_links(errors, warnings)
        return not errors, errors, warnings

    def _check_imports_file(self, ov: Overlay) -> list[str]:
        from tofu_overlay.merge import IMPORTS_FILENAME, read_imports_addresses

        path = self.cwd / IMPORTS_FILENAME.format(name=ov.name)
        if not path.exists():
            return []
        imported = read_imports_addresses(path)
        errors = []
        for address, claim in sorted(ov.create_claims().items()):
            if address not in imported:
                errors.append(f"imports file lacks {address}")
            elif claim.import_id and imported[address] != claim.import_id:
                errors.append(f"imports file id for {address} differs from the claim")
        for address in sorted(set(imported) - set(ov.create_claims())):
            errors.append(f"imports file has {address} which is not a create claim")
        return errors

    # ------------------------------------------------------------------ rebase

    def rebase(self, *, yes: bool) -> Overlay:
        """Rebuild the overlay on the current base (local merge, single push)."""
        doc, ov = self._load()
        self._gate(ov, {Status.ACTIVE}, "rebase")
        stale, base_etag = self._freshness(ov)
        if not stale:
            raise PolicyError("overlay is fresh, nothing to rebase")
        self._check_ancestry(allow_behind=False)
        overlay_doc = self._validate_overlay(ov)
        base_doc = self._pull_base()
        self._check_base_lineage(doc, base_doc)
        by_address, by_identity = self._claims_in_base(ov, base_doc)
        if by_address or by_identity:
            raise PolicyError(
                "new base conflicts with create claims: "
                + ", ".join(by_address + by_identity)
            )
        new_doc = self._build_rebased(ov, overlay_doc, base_doc)
        self.console.info(
            f"rebase '{ov.name}': base serial {ov.base_serial} -> {base_doc.get('serial')}, "
            f"{len(ov.create_claims())} own instance(s) re-injected, one state push"
        )
        self._confirm_typed("rebase", yes=yes)
        archive_key = self.backend.archive_key(ov.name, "rebase", _archive_timestamp())
        self.s3.copy(self._state_key(ov), archive_key)
        self.console.info(f"previous overlay state archived at {archive_key}")
        self._overlay_runner().state_push(new_doc)
        self._write_base_cache(base_doc, base_etag)
        doc = self.registry.set_status(
            ov.name,
            Status.ACTIVE,
            expect_status={Status.ACTIVE},
            expect_entry=ov,
            base_etag=base_etag,
            base_serial=base_doc.get("serial"),
            trunk_commit=self._trunk_commit(),
        )
        self.console.success("rebase done")
        return doc.overlays[ov.name]

    def _build_rebased(self, ov: Overlay, overlay_doc: dict, base_doc: dict) -> dict:
        new_doc = json.loads(json.dumps(base_doc))
        new_doc["lineage"] = ov.lineage or overlay_doc.get("lineage")
        own = set(ov.create_claims()) & statemod.addresses(overlay_doc)
        new_doc = statemod.inject_instances(new_doc, overlay_doc, own)
        return statemod.bump_serial(
            new_doc, max(int(overlay_doc.get("serial") or 0), int(base_doc.get("serial") or 0))
        )

    # ------------------------------------------------------------------ abandon

    def abandon(self, *, keep_resources: bool, dry_run: bool, yes: bool) -> None:
        """Destroy the overlay's own resources (only) and release its claims."""
        doc, ov = self._load()
        allowed = {Status.CREATING, Status.ACTIVE, Status.DIRTY}
        if keep_resources:
            allowed.add(Status.MERGING)
        self._gate(ov, allowed, "abandon")
        overlay_doc = self._overlay_state_or_none(ov)
        if overlay_doc is not None:
            by_address, by_identity = self._claims_in_base(ov, self._pull_base())
            if by_address or by_identity:
                raise PolicyError(
                    "create claims already present in the base (already merged? run `finalize`): "
                    + ", ".join(by_address + by_identity)
                )
        creates = set(ov.create_claims())
        self._describe_abandon(ov, overlay_doc, creates, keep_resources)
        if dry_run:
            self._archive(ov, "abandoned", dry_run=True)
            self.console.info("[dry-run] registry: release overlay -> tombstone abandoned")
            return
        self._confirm_typed("abandon", yes=yes)
        if overlay_doc is not None and not keep_resources:
            self._destroy_own_resources(ov, overlay_doc, creates)
        elif overlay_doc is not None:
            for address, claim in sorted(ov.create_claims().items()):
                self.console.warn(f"orphaned resource kept: {address} id={claim.id}")
        if overlay_doc is not None:
            self._archive(ov, "abandoned")
        pending = sorted(set(ov.pending_revert) | set(ov.update_claims()))
        if pending:
            self.console.warn(
                "update claims were applied on base resources; run the trunk pipeline to "
                "revert: " + ", ".join(pending)
            )
        self._remove_data_dir()
        self.registry.release_overlay(ov.name, Status.ABANDONED, pending_revert=pending)
        self.console.success(f"overlay '{ov.name}' abandoned")

    def _overlay_state_or_none(self, ov: Overlay) -> dict | None:
        if ov.status == Status.CREATING and self.s3.head(ov.state_key) is None:
            return None
        return self._validate_overlay(ov)

    def _describe_abandon(
        self, ov: Overlay, overlay_doc: dict | None, creates: set[str], keep: bool
    ) -> None:
        if overlay_doc is None:
            self.console.info("no overlay state object: registry cleanup only")
            return
        others = sorted(statemod.addresses(overlay_doc) - creates)
        self.console.info(f"state rm {len(others)} base address(es) from the overlay state")
        if keep:
            self.console.info(f"--keep-resources: {len(creates)} own resource(s) left in place")
        else:
            self.console.info(f"plan -destroy then apply on {len(creates)} own resource(s)")

    def _destroy_own_resources(self, ov: Overlay, overlay_doc: dict, creates: set[str]) -> None:
        runner = self._overlay_runner()
        others = sorted(statemod.addresses(overlay_doc) - creates)
        if others:
            runner.state_rm(others)
        planfile = self.data_dir / f"tfplan.destroy.{new_run_id()}"
        try:
            runner.plan(planfile, destroy=True)
            changes, _drift, _summary = planmod.parse_plan(runner.show_json(planfile))
            offenders = [
                f"{c.address} {c.actions}"
                for c in changes
                if c.actions not in (["no-op"], ["read"])
                and (c.actions != ["delete"] or c.address not in creates)
            ]
            if offenders:
                raise PolicyError(
                    "destroy plan touches non-owned resources: " + ", ".join(offenders)
                )
            runner.apply(planfile, auto_approve=True)
        finally:
            planfile.unlink(missing_ok=True)
        remaining = statemod.addresses(runner.state_pull()) & creates
        if remaining:
            raise ToolError(
                "resources still present after destroy: " + ", ".join(sorted(remaining))
            )

    # ------------------------------------------------------------------ finalize

    def finalize(self, *, purge: bool, yes: bool) -> None:
        """After the trunk applied the imports: archive the overlay and tombstone it."""
        _doc, ov = self._load()
        self._gate(ov, {Status.MERGING}, "finalize")
        overlay_doc = self._validate_overlay(ov)
        claims = self._claims_from_state(ov.claims, overlay_doc)
        accepted = self._accepted_recreate(ov)
        base_index = statemod.index_instances(self._pull_base())
        missing, different = [], []
        for address, claim in sorted(claims.items()):
            if claim.kind != ClaimKind.CREATE:
                continue
            if address not in base_index:
                missing.append(address)
                continue
            base_id = (base_index[address][1].get("attributes") or {}).get("id")
            if claim.id is not None and base_id != claim.id:
                if address in accepted:
                    self.console.warn(
                        f"{address} was recreated by the trunk; the overlay's object "
                        f"id={claim.id} is now orphaned and must be deleted by hand"
                    )
                    continue
                different.append(f"{address}: overlay id={claim.id} base id={base_id}")
        if missing:
            raise PolicyError(
                "the trunk has not adopted every create claim yet: " + ", ".join(missing)
            )
        if different:
            raise PolicyError(
                "the trunk created its own objects for: " + "; ".join(different)
            )
        self._warn_consumers(ov)
        self._confirm_typed("finalize", yes=yes)
        if purge:
            self.console.info("--purge: no archive kept")
            self._delete_state_object(self._state_key(ov))
        else:
            self._archive(ov, "merged")
        self._remove_data_dir()
        self.registry.release_overlay(ov.name, Status.MERGED)
        from tofu_overlay.merge import IMPORTS_FILENAME

        self.console.success(f"overlay '{ov.name}' merged")
        self.console.info(f"now run: git rm {IMPORTS_FILENAME.format(name=ov.name)}")

    def _accepted_recreate(self, ov: Overlay) -> set[str]:
        """Create claims absent from the imports file were accepted for recreation at merge."""
        from tofu_overlay.merge import IMPORTS_FILENAME, read_imports_addresses

        path = self.cwd / IMPORTS_FILENAME.format(name=ov.name)
        if not path.exists():
            return set()
        return set(ov.create_claims()) - set(read_imports_addresses(path))

    # ------------------------------------------------------------------ gc

    def _archives(self) -> list[str]:
        keys = self.s3.list_prefix(self._overlay_prefix())
        return sorted(k for k in keys if ARCHIVE_RE.search(k))

    def gc(self, *, purge: bool, yes: bool) -> list[dict[str, Any]]:
        """Report overlays without a remote branch and archived states; --purge deletes archives."""
        doc, _ = self.registry.load()
        findings: list[dict[str, Any]] = []
        for name, ov in sorted(doc.overlays.items()):
            if ov.branch and not config.remote_branch_exists(self.cwd, ov.branch):
                findings.append(
                    {"kind": "orphan-branch", "name": name, "branch": ov.branch,
                     "status": str(ov.status), "message": "remote branch is gone"}
                )
        archives = self._archives()
        for key in archives:
            findings.append({"kind": "archive", "key": key, "message": "archived overlay state"})
        for f in findings:
            self.console.info(f"{f['kind']}: {f.get('name') or f.get('key')} - {f['message']}")
        if purge and archives:
            if not self.console.confirm_typed(
                "purge", f"Type `purge` to delete {len(archives)} archive object(s)", yes=yes
            ):
                raise ToolError("purge aborted")
            for key in archives:
                self.s3.delete(key)
                self.console.info(f"deleted {key}")
        return findings

    # ------------------------------------------------------------------ doctor

    def doctor(self) -> list[dict[str, Any]]:
        """Read-only consistency report between registry, S3 objects, DynamoDB items and repo."""
        findings: list[dict[str, Any]] = []
        doc, _ = self._load_for_doctor(findings)
        keys = self.s3.list_prefix(self._overlay_prefix())
        self._doctor_objects(doc, keys, findings)
        self._doctor_registry(doc, findings)
        self._doctor_repo(doc, findings)
        for f in findings:
            getattr(self.console, {"error": "error", "warning": "warn"}.get(f["level"], "info"))(
                f"{f['code']}: {f['message']}"
            )
        return findings

    def _load_for_doctor(self, findings: list[dict]) -> tuple[RegistryDoc, str | None]:
        try:
            return self.registry.load()
        except OverlayError as exc:
            findings.append({"level": "error", "code": "registry", "message": str(exc)})
            return RegistryDoc(tool_version=__version__, base={"bucket": self.backend.bucket,
                                                                "key": self.backend.key}), None

    def _doctor_objects(self, doc: RegistryDoc, keys: list[str], findings: list[dict]) -> None:
        registered = {ov.state_key for ov in doc.overlays.values()}
        for key in sorted(keys):
            if key.endswith(".tflock"):
                findings.append(
                    {"level": "warning", "code": "tflock", "message": f"lock file {key}"}
                )
            elif ARCHIVE_RE.search(key):
                findings.append({"level": "info", "code": "archive", "message": key})
            elif key not in registered:
                findings.append(
                    {"level": "error", "code": "unregistered-object",
                     "message": f"{key} is not in the registry"}
                )

    def _doctor_registry(self, doc: RegistryDoc, findings: list[dict]) -> None:
        for name, ov in sorted(doc.overlays.items()):
            if ov.is_live() and not self.s3.exists(ov.state_key):
                findings.append(
                    {"level": "error", "code": "missing-object",
                     "message": f"{name} ({ov.status}) has no object at {ov.state_key}"}
                )
                if self.backend.dynamodb_table and self.s3.md5_item_exists(ov.state_key):
                    findings.append({"level": "warning", "code": "orphan-md5",
                                     "message": f"digest item without object for {ov.state_key}"})
            if ov.status == Status.APPLYING and self.registry.stale_applying(
                ov, self.cfg.policy.apply_timeout_min
            ):
                findings.append({"level": "warning", "code": "stale-applying",
                                 "message": f"{name} applying since {ov.applying_since}"})
            if self.backend.dynamodb_table and ov.is_live():
                lock = self.s3.lock_item(ov.state_key)
                if lock:
                    findings.append({"level": "warning", "code": "locked",
                                     "message": f"{name} is locked: {lock}"})
            age = _age_days(ov.created_at)
            if age is not None and age > self.cfg.policy.max_overlay_age_days and ov.is_live():
                findings.append({"level": "warning", "code": "old-overlay",
                                 "message": f"{name} is {age} days old"})
        for name, tomb in sorted(doc.tombstones.items()):
            findings.append({"level": "info", "code": "tombstone",
                             "message": f"{name} {tomb.status} at {tomb.at}"})
            if tomb.pending_revert:
                findings.append({"level": "warning", "code": "pending-revert",
                                 "message": f"{name} ({tomb.status}) modified base resources "
                                            "the trunk still has to revert: "
                                            + ", ".join(tomb.pending_revert)})

    def _doctor_repo(self, doc: RegistryDoc, findings: list[dict]) -> None:
        if not config.ensure_gitignored(self.repo_root, DATA_DIR_NAME + "/"):
            findings.append({"level": "warning", "code": "gitignore",
                             "message": f"{DATA_DIR_NAME}/ is not git-ignored"})
        pinned = self._pinned_tofu_version()
        if pinned:
            actual = self._safe_version_in(self.base_data_dir)
            if actual and actual != pinned:
                findings.append({"level": "warning", "code": "tofu-version",
                                 "message": f"binary {actual} != pinned {pinned}"})
        for path in sorted(self.cwd.glob(IMPORTS_GLOB)):
            name = path.name[len("zz_overlay_"):-len(".imports.tf")]
            ov = doc.overlays.get(name)
            if ov is None or not ov.is_live():
                findings.append({"level": "warning", "code": "stale-imports-file",
                                 "message": f"{path.name}: overlay {name} is not live (git rm it)"})

    def _pinned_tofu_version(self) -> str | None:
        for directory in (self.cwd, self.repo_root):
            path = directory / ".opentofu-version"
            if path.exists():
                return path.read_text().strip() or None
        return None

    def _safe_version_in(self, data_dir: Path) -> str | None:
        try:
            return self._runner(data_dir).version()
        except OverlayError:
            return None
