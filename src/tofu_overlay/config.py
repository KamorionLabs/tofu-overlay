"""Configuration file loading, CI detection, overlay naming and git helpers.

Every git call goes through an injectable ``runner`` so tests never spawn a
process. The default runner is ``subprocess.run`` on the ``git`` binary.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from tofu_overlay.models import PolicyConfig, ToolConfig, ToolError

CONFIG_FILENAMES: tuple[str, ...] = (".tofu-overlay.yaml", ".tofu-overlay.yml")
OVERLAY_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
SLUG_MAX_LEN = 34
SHA_LEN = 6

Runner = Callable[[list[str], Path], subprocess.CompletedProcess[str]]
"""Signature of an injectable command runner: ``runner(argv, cwd)``."""


# --------------------------------------------------------------------------- #
# Git plumbing
# --------------------------------------------------------------------------- #


def default_runner(argv: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run ``argv`` in ``cwd`` capturing text output; never raises on non-zero exit."""
    return subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True, check=False)


def _git(
    args: list[str], cwd: Path, runner: Runner | None = None
) -> subprocess.CompletedProcess[str]:
    """Run a git command; ``ToolError`` if the git binary itself is unavailable."""
    run = runner or default_runner
    try:
        return run(["git", *args], cwd)
    except FileNotFoundError as exc:
        raise ToolError("git binary not found in PATH") from exc
    except OSError as exc:
        raise ToolError(f"cannot run git: {exc}") from exc


def _git_stdout(args: list[str], cwd: Path, runner: Runner | None, what: str) -> str:
    """Run a git command that must succeed and return its stripped stdout."""
    proc = _git(args, cwd, runner)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise ToolError(f"git: cannot determine {what} in {cwd}: {detail or proc.returncode}")
    return proc.stdout.strip()


# --------------------------------------------------------------------------- #
# Repository and configuration discovery
# --------------------------------------------------------------------------- #


def find_repo_root(start: Path, runner: Runner | None = None) -> Path | None:
    """Git top-level directory of ``start``, or the nearest ancestor holding ``.git``.

    Returns ``None`` when ``start`` is not inside a repository.
    """
    start = Path(start).resolve()
    cwd = start if start.is_dir() else start.parent
    try:
        proc = _git(["rev-parse", "--show-toplevel"], cwd, runner)
    except ToolError:
        proc = None
    if proc is not None and proc.returncode == 0 and proc.stdout.strip():
        return Path(proc.stdout.strip()).resolve()
    for candidate in (cwd, *cwd.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def _find_config_file(start: Path, repo_root: Path | None) -> Path | None:
    """Nearest ``.tofu-overlay.yaml`` walking up from ``start`` to ``repo_root``."""
    start = Path(start).resolve()
    cwd = start if start.is_dir() else start.parent
    stop = repo_root.resolve() if repo_root else None
    for candidate in (cwd, *cwd.parents):
        for filename in CONFIG_FILENAMES:
            path = candidate / filename
            if path.is_file():
                return path
        if stop is not None and candidate == stop:
            break
    return None


def _read_yaml(path: Path) -> dict[str, Any]:
    """Parse a YAML mapping; empty documents yield ``{}``."""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ToolError(f"cannot read {path}: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ToolError(f"{path}: top level must be a mapping")
    return data


_IMPORT_IDS_SECTIONS = ("non_importable", "replace_prone", "virtual_attributes")


def _normalise_raw(data: dict[str, Any]) -> dict[str, Any]:
    """Accept the ``import_ids.yaml`` layout (``formats:`` + lists) under ``import_ids:``.

    ``import_ids: {formats: {...}, non_importable: [...]}`` is lifted to the flat
    ``ToolConfig`` fields; a plain ``type -> format`` mapping is kept as is.
    """
    raw = dict(data)
    import_ids = raw.get("import_ids")
    if not isinstance(import_ids, dict):
        return raw
    if "formats" not in import_ids and not any(k in import_ids for k in _IMPORT_IDS_SECTIONS):
        return raw
    lifted = dict(import_ids)
    formats = lifted.pop("formats", {}) or {}
    for section in _IMPORT_IDS_SECTIONS:
        if section in lifted:
            value = lifted.pop(section)
            if section == "virtual_attributes":
                merged = dict(raw.get(section) or {})
                merged.update(value or {})
                raw[section] = merged
            else:
                raw[section] = list(raw.get(section) or []) + list(value or [])
    formats.update(lifted)
    raw["import_ids"] = formats
    return raw


def load_config(start: Path, runner: Runner | None = None) -> ToolConfig:
    """Load ``.tofu-overlay.yaml`` (walking up to the repo root) merged over defaults.

    ``TOFU_OVERLAY_BINARY`` in the environment overrides ``binary``.
    """
    repo_root = find_repo_root(start, runner)
    path = _find_config_file(start, repo_root)
    raw: dict[str, Any] = _normalise_raw(_read_yaml(path)) if path else {}
    try:
        cfg = ToolConfig.model_validate(raw)
    except ValidationError as exc:
        where = path or "defaults"
        raise ToolError(f"invalid configuration in {where}: {exc}") from exc
    binary = os.environ.get("TOFU_OVERLAY_BINARY", "").strip()
    if binary:
        cfg.binary = binary
    return cfg


# --------------------------------------------------------------------------- #
# CI detection
# --------------------------------------------------------------------------- #


def _env_true(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() == "true"


def is_ci() -> bool:
    """True under any CI (``CI=true``) or Azure DevOps (``TF_BUILD=True``)."""
    return _env_true("CI") or is_ado()


def is_ado() -> bool:
    """True on an Azure DevOps agent (``TF_BUILD=True``)."""
    return _env_true("TF_BUILD")


# --------------------------------------------------------------------------- #
# Git state helpers
# --------------------------------------------------------------------------- #


def current_branch(cwd: Path, runner: Runner | None = None) -> str:
    """Current branch name; ``ToolError`` when HEAD is detached or not a repository."""
    branch = _git_stdout(["branch", "--show-current"], cwd, runner, "current branch")
    if not branch:
        raise ToolError(
            "HEAD is detached: check out a branch or pass --name / TOFU_OVERLAY_NAME"
        )
    return branch


def head_commit(cwd: Path, runner: Runner | None = None) -> str:
    """Full SHA of ``HEAD``."""
    return _git_stdout(["rev-parse", "HEAD"], cwd, runner, "HEAD commit")


def is_tree_dirty(cwd: Path, runner: Runner | None = None) -> bool:
    """True when the working tree has uncommitted changes (untracked files included)."""
    out = _git_stdout(["status", "--porcelain"], cwd, runner, "working tree status")
    return bool(out)


def branch_contains_trunk(cwd: Path, trunk: str, runner: Runner | None = None) -> bool | None:
    """Whether ``origin/<trunk>`` is an ancestor of ``HEAD``.

    Returns ``None`` when ``origin/<trunk>`` is unknown locally (never fetched).
    """
    ref = f"refs/remotes/origin/{trunk}"
    probe = _git(["rev-parse", "--verify", "--quiet", ref], cwd, runner)
    if probe.returncode != 0:
        return None
    proc = _git(["merge-base", "--is-ancestor", f"origin/{trunk}", "HEAD"], cwd, runner)
    if proc.returncode == 0:
        return True
    if proc.returncode == 1:
        return False
    detail = (proc.stderr or "").strip()
    raise ToolError(f"git merge-base failed: {detail or proc.returncode}")


def remote_branch_exists(cwd: Path, branch: str, runner: Runner | None = None) -> bool:
    """True when ``branch`` exists on ``origin`` (queries the remote)."""
    proc = _git(["ls-remote", "--exit-code", "--heads", "origin", branch], cwd, runner)
    if proc.returncode == 0:
        return bool(proc.stdout.strip())
    if proc.returncode == 2:
        return False
    detail = (proc.stderr or "").strip()
    raise ToolError(f"git ls-remote failed: {detail or proc.returncode}")


def git_user_email(cwd: Path, runner: Runner | None = None) -> str:
    """Configured ``user.email``; falls back to ``GIT_AUTHOR_EMAIL``/``EMAIL``."""
    proc = _git(["config", "--get", "user.email"], cwd, runner)
    email = proc.stdout.strip() if proc.returncode == 0 else ""
    if not email:
        email = os.environ.get("GIT_AUTHOR_EMAIL", "").strip() or os.environ.get("EMAIL", "")
    email = email.strip()
    if not email:
        raise ToolError("git user.email is not set (needed to record the overlay owner)")
    return email


def symlinked_ancestor(path: Path) -> Path | None:
    """First symlink among ``path`` and its ancestors below the enclosing repository root.

    The walk stops at the first directory holding ``.git`` (a repository reached
    through a symlink is fine: every env dir of that repo shares it). The path
    is normalised but never resolved, so the symlink itself is what gets seen.
    """
    current = Path(os.path.normpath(Path(path).absolute()))
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return None
        if candidate.is_symlink():
            return candidate
    return None


def ensure_gitignored(repo_root: Path, entry: str, runner: Runner | None = None) -> bool:
    """True when ``entry`` (e.g. ``.tofu-overlay/``) is git-ignored. Never edits files."""
    proc = _git(["check-ignore", "-q", "--", entry], repo_root, runner)
    return proc.returncode == 0


# --------------------------------------------------------------------------- #
# Overlay naming
# --------------------------------------------------------------------------- #


def _slug(text: str) -> str:
    """Lowercase, non ``[a-z0-9]`` runs to ``-``, trimmed and capped at ``SLUG_MAX_LEN``."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:SLUG_MAX_LEN].rstrip("-")


def validate_overlay_name(name: str) -> str:
    """Return ``name`` if it matches ``^[a-z0-9][a-z0-9-]{0,40}$``, else ``ToolError``."""
    if not OVERLAY_NAME_RE.match(name):
        raise ToolError(
            f"invalid overlay name {name!r}: expected ^[a-z0-9][a-z0-9-]{{0,40}}$"
        )
    return name


def overlay_name_for(branch: str) -> str:
    """Deterministic overlay name ``<slug(branch)[:34]>-<sha1(branch)[:6]>``."""
    digest = hashlib.sha1(branch.encode("utf-8")).hexdigest()[:SHA_LEN]  # noqa: S324
    slug = _slug(branch)
    name = f"{slug}-{digest}" if slug else digest
    return validate_overlay_name(name)


def resolve_overlay_name(
    explicit: str | None, cwd: Path, runner: Runner | None = None
) -> str:
    """``--name`` > ``TOFU_OVERLAY_NAME`` > name derived from the current branch."""
    if explicit:
        return validate_overlay_name(explicit.strip())
    from_env = os.environ.get("TOFU_OVERLAY_NAME", "").strip()
    if from_env:
        return validate_overlay_name(from_env)
    return overlay_name_for(current_branch(cwd, runner))


# --------------------------------------------------------------------------- #
# Policy
# --------------------------------------------------------------------------- #


def base_key_allowed(key: str, policy: PolicyConfig) -> bool:
    """Default-deny glob match of ``key`` against ``policy.allowed_base_keys``.

    Patterns use ``fnmatch`` semantics (``*`` also matches ``/``); an empty
    list allows nothing.
    """
    return any(fnmatch.fnmatchcase(key, pattern) for pattern in policy.allowed_base_keys)
