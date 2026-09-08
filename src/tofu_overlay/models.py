"""Pydantic models, enums, exit codes and error types shared by every module.

This module has no dependency on the rest of the package; everything else
imports from here. Keep it free of I/O.
"""

from __future__ import annotations

import posixpath
import re
import uuid
from datetime import UTC, datetime
from enum import IntEnum, StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# --------------------------------------------------------------------------- #
# Exit codes and errors
# --------------------------------------------------------------------------- #


class ExitCode(IntEnum):
    """Process exit codes shared by every command (see DESIGN §6)."""

    OK = 0
    ERROR = 1
    CHANGES = 2
    POLICY = 3
    STALE = 4
    REGISTRY = 5
    NOT_ALLOWED = 6
    FROZEN = 7


class OverlayError(Exception):
    """Base class of every error raised by the tool; carries the exit code."""

    exit_code: ExitCode = ExitCode.ERROR

    def __init__(self, message: str = "", *, exit_code: ExitCode | None = None) -> None:
        super().__init__(message)
        self.message = message
        if exit_code is not None:
            self.exit_code = exit_code

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.message or self.__class__.__name__


class ToolError(OverlayError):
    """Tool or tofu/git failure (exit 1)."""

    exit_code = ExitCode.ERROR


class PolicyError(OverlayError):
    """Policy violation detected on a plan (exit 3)."""

    exit_code = ExitCode.POLICY


class StaleError(OverlayError):
    """Overlay is stale or the branch is behind the trunk (exit 4)."""

    exit_code = ExitCode.STALE


class RegistryError(OverlayError):
    """Registry conflict, unreachable or invalid document (exit 5)."""

    exit_code = ExitCode.REGISTRY


class NotAllowedError(OverlayError):
    """Base key not allowed by policy, or overlay not found (exit 6)."""

    exit_code = ExitCode.NOT_ALLOWED


class FrozenError(OverlayError):
    """Overlay is frozen in status `merging` (exit 7)."""

    exit_code = ExitCode.FROZEN


# --------------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------------- #


class Status(StrEnum):
    """Overlay lifecycle status (DESIGN §5)."""

    CREATING = "creating"
    ACTIVE = "active"
    APPLYING = "applying"
    DIRTY = "dirty"
    MERGING = "merging"
    MERGED = "merged"
    ABANDONED = "abandoned"
    NEEDS_REVIEW = "needs-review"


LIVE_STATUSES: frozenset[Status] = frozenset(
    {Status.CREATING, Status.ACTIVE, Status.APPLYING, Status.DIRTY, Status.MERGING}
)


class ClaimKind(StrEnum):
    """Kind of claim an overlay holds on a resource address."""

    CREATE = "create"
    UPDATE = "update"


# --------------------------------------------------------------------------- #
# Backend
# --------------------------------------------------------------------------- #

ARCHIVE_KEY_RE = re.compile(
    r"@(?P<name>[a-z0-9-]+)\.(?P<status>merged|abandoned|rebase)-\d{8}T\d{6}Z"
)


class BackendConfig(BaseModel):
    """Resolved backend of a stack plus key builders for derived objects.

    ``backend_type`` names the tofu backend (``s3`` is the only one with a
    :class:`~tofu_overlay.store.StateStore` implementation today; the other
    attributes are the ``s3`` ones). Every derived object key (overlay state,
    registry document, archive, lock ids) is built here and nowhere else, so a
    future backend that needs its own key layout (flat blob names, a container
    instead of a bucket, generation tokens...) only has to change these
    builders, see docs/ROADMAP.md.

    Key builders are extension-aware: when the base key carries an extension
    (``a/b/terraform.tfstate``) the suffix is inserted before it
    (``a/b/terraform@NAME.tfstate``, ``a/b/terraform.overlays.json``).
    """

    backend_type: str = "s3"
    bucket: str
    key: str
    region: str | None = None
    profile: str | None = None
    dynamodb_table: str | None = None
    use_lockfile: bool = False
    encrypt: bool = True
    kms_key_id: str | None = None
    workspace: str = "default"
    workspace_key_prefix: str = "env:"
    backend_config_files: list[str] = Field(default_factory=list)

    # -- internals ---------------------------------------------------------- #

    def _split_key(self) -> tuple[str, str]:
        """Return ``(stem, ext)`` where ``ext`` is the basename extension or ``""``."""
        head, base = posixpath.split(self.key)
        stem, ext = posixpath.splitext(base)
        if not stem or not ext[1:].isalnum():
            # Hidden file or odd suffix: treat the whole basename as the stem.
            stem, ext = base, ""
        return (posixpath.join(head, stem) if head else stem), ext

    # -- public helpers ----------------------------------------------------- #

    def state_path(self) -> str:
        """Object key of the state for the configured workspace.

        Equals ``key`` for the default workspace; otherwise the `s3` backend
        layout ``<workspace_key_prefix>/<workspace>/<key>``.
        """
        if self.workspace == "default":
            return self.key
        return f"{self.workspace_key_prefix}/{self.workspace}/{self.key}"

    def overlay_prefix(self) -> str:
        """Prefix common to every overlay key of this base (``<stem>@``)."""
        stem, _ = self._split_key()
        return f"{stem}@"

    def overlay_key(self, name: str) -> str:
        """Key of the overlay state ``<key>@<name>`` (extension-aware).

        An empty ``name`` returns the bare prefix (``<stem>@``, no extension),
        which is what a ``ListObjects`` scan for overlay objects needs.
        """
        stem, ext = self._split_key()
        if not name:
            return f"{stem}@"
        return f"{stem}@{name}{ext}"

    def registry_key(self) -> str:
        """Key of the registry document ``<key>.overlays.json`` (extension-aware)."""
        stem, _ = self._split_key()
        return f"{stem}.overlays.json"

    def archive_key(self, name: str, status: Status | str, ts: str) -> str:
        """Archive key ``<key>@<name>.<status>-<ts>`` used by finalize/abandon/rebase."""
        stem, ext = self._split_key()
        status_str = status.value if isinstance(status, Status) else str(status)
        return f"{stem}@{name}.{status_str}-{ts}{ext}"

    def is_archive_key(self, key: str) -> bool:
        """True when ``key`` has the archive layout produced by :meth:`archive_key`."""
        return ARCHIVE_KEY_RE.search(key) is not None

    def lock_id(self, path: str) -> str:
        """DynamoDB ``LockID`` of a state object: ``<bucket>/<path>``."""
        return f"{self.bucket}/{path}"

    def md5_lock_id(self, path: str) -> str:
        """DynamoDB ``LockID`` of the digest item: ``<bucket>/<path>-md5``."""
        return f"{self.bucket}/{path}-md5"


class RemoteStateRef(BaseModel):
    """One ``data "terraform_remote_state"`` block with an ``s3`` backend (MULTI-STACK.md).

    ``key`` is the literal base key the block reads (or the map key of the
    ``lookup(var.tofu_overlay_keys, ...)`` contract). ``unresolved`` is set when
    the key or bucket is an expression the tool cannot evaluate; ``key`` is then
    ``None``. ``bucket``/``region`` are ``None`` when absent or not literal
    (the current backend's values apply).
    """

    name: str
    bucket: str | None = None
    key: str | None = None
    region: str | None = None
    unresolved: bool = False


# --------------------------------------------------------------------------- #
# Registry document
# --------------------------------------------------------------------------- #


class Claim(BaseModel):
    """A claim held by an overlay on one resource address (DESIGN §5)."""

    kind: ClaimKind
    type: str
    identity: dict[str, Any] = Field(default_factory=dict)
    id: str | None = None
    import_id: str | None = None
    after_hash: str | None = None
    dependencies: list[str] = Field(default_factory=list)
    claimed_at: str
    updated_at: str


class Overlay(BaseModel):
    """One overlay entry of the registry document."""

    name: str
    state_key: str
    lineage: str | None = None
    branch: str
    owners: list[str] = Field(default_factory=list)
    caller_arn: str | None = None
    binary: str = "tofu"
    tofu_version: str | None = None
    created_at: str
    updated_at: str
    base_serial: int | None = None
    base_etag: str | None = None
    trunk_commit: str | None = None
    status: Status
    applied_commit: str | None = None
    run_id: str | None = None
    applying_since: str | None = None
    claims: dict[str, Claim] = Field(default_factory=dict)
    pending_revert: list[str] = Field(default_factory=list)
    last_apply: dict[str, Any] | None = None

    def is_live(self) -> bool:
        """True when the overlay still holds its claims (DESIGN §3.4)."""
        return self.status in LIVE_STATUSES

    def create_claims(self) -> dict[str, Claim]:
        """Claims of kind ``create``, keyed by address."""
        return {a: c for a, c in self.claims.items() if c.kind is ClaimKind.CREATE}

    def update_claims(self) -> dict[str, Claim]:
        """Claims of kind ``update``, keyed by address."""
        return {a: c for a, c in self.claims.items() if c.kind is ClaimKind.UPDATE}


class Tombstone(BaseModel):
    """Trace of a merged/abandoned overlay kept for ``policy.tombstone_days``."""

    status: Status
    at: str
    branch: str = ""
    pending_revert: list[str] = Field(default_factory=list)


class RegistryDoc(BaseModel):
    """The per-base registry document ``<key>.overlays.json`` (DESIGN §5)."""

    version: int = 1
    tool_version: str
    base: dict[str, Any] = Field(default_factory=dict)
    overlays: dict[str, Overlay] = Field(default_factory=dict)
    tombstones: dict[str, Tombstone] = Field(default_factory=dict)

    def live_overlays(self) -> dict[str, Overlay]:
        """Overlays whose claims count in conflict checks."""
        return {n: o for n, o in self.overlays.items() if o.is_live()}


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #


class PolicyConfig(BaseModel):
    """``policy:`` section of ``.tofu-overlay.yaml`` (DESIGN §10)."""

    allowed_base_keys: list[str] = Field(default_factory=list)
    trunk_branch: str = "main"
    env_dir_glob: str = "stacks/*/env/*"
    tombstone_days: int = 14
    apply_timeout_min: int = 90
    max_overlay_age_days: int = 30


class ToolConfig(BaseModel):
    """Whole ``.tofu-overlay.yaml`` merged over defaults."""

    policy: PolicyConfig = Field(default_factory=PolicyConfig)
    binary: str = "tofu"
    identity: dict[str, list[str]] = Field(default_factory=dict)
    import_ids: dict[str, str] = Field(default_factory=dict)
    virtual_attributes: dict[str, list[str]] = Field(default_factory=dict)
    non_importable: list[str] = Field(default_factory=list)
    replace_prone: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Plan analysis
# --------------------------------------------------------------------------- #


class ResourceChange(BaseModel):
    """One ``resource_changes[]`` entry of ``tofu show -json`` (flattened)."""

    address: str
    previous_address: str | None = None
    module_address: str | None = None
    mode: str | None = None
    type: str
    name: str
    index: Any = None
    deposed: str | None = None
    actions: list[str] = Field(default_factory=list)
    before: Any = None
    after: Any = None
    after_unknown: Any = None
    before_sensitive: Any = None
    after_sensitive: Any = None
    replace_paths: list[Any] = Field(default_factory=list)
    importing: dict[str, Any] | None = None
    action_reason: str | None = None


class PlanSummary(BaseModel):
    """Counts of actions in a plan; ``import_`` is serialised as ``import``."""

    model_config = ConfigDict(populate_by_name=True)

    create: int = 0
    update: int = 0
    delete: int = 0
    replace: int = 0
    import_: int = Field(0, alias="import")
    no_op: int = 0

    def total_changes(self) -> int:
        """Number of changes that are not no-ops."""
        return self.create + self.update + self.delete + self.replace + self.import_


class Violation(BaseModel):
    """One policy violation reported for a resource address."""

    address: str
    rule: str
    message: str
    other_overlay: str | None = None


class PolicyResult(BaseModel):
    """Outcome of the policy checks on a plan (DESIGN §7)."""

    violations: list[Violation] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    claims: dict[str, Claim] = Field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """True when no violation was found."""
        return not self.violations


class Finding(BaseModel):
    """A ``doctor``/``gc`` finding: ``level`` is ``info``, ``warning`` or ``error``."""

    level: str
    code: str
    message: str


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


def utcnow_iso() -> str:
    """Current UTC time as an ISO-8601 string with a ``Z`` suffix (second precision)."""
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def new_run_id() -> str:
    """Short random identifier for one apply run (12 hex chars)."""
    return uuid.uuid4().hex[:12]
