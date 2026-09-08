"""Trunk baseline helpers: export the trunk tree and locate the env dir inside it.

The trunk baseline (DESIGN §7.8) plans the **trunk configuration** against the
**base state** to learn which base resources would change under a trunk apply.
The trunk tree comes from ``git archive <ref>`` extracted into a cache
directory, never from a worktree: some modules probe for a real ``.git/HEAD``
and a worktree would also register itself in the repository.

Git calls go through the injectable runners of :mod:`tofu_overlay.config`
(text) and a local bytes runner for the archive stream.
"""

from __future__ import annotations

import io
import shutil
import subprocess
import tarfile
from collections.abc import Callable
from pathlib import Path

from tofu_overlay.config import Runner, _git
from tofu_overlay.models import ToolError

EXPORT_MARKER = ".tofu-overlay-export"
"""File written at the root of a finished export, holding the commit sha."""

BytesRunner = Callable[[list[str], Path], "subprocess.CompletedProcess[bytes]"]
"""Signature of the bytes-capturing runner used for ``git archive``."""


def _default_bytes_runner(argv: list[str], cwd: Path) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(argv, cwd=str(cwd), capture_output=True, check=False)


def trunk_sha(repo_root: Path, trunk_ref: str, runner: Runner | None = None) -> str | None:
    """Commit sha of ``trunk_ref`` (e.g. ``origin/main``), or ``None`` when unknown locally."""
    proc = _git(["rev-parse", "--verify", "--quiet", f"{trunk_ref}^{{commit}}"], repo_root, runner)
    if proc.returncode != 0:
        return None
    sha = proc.stdout.strip()
    return sha or None


def _extract(archive: bytes, dest: Path) -> None:
    """Extract a tar stream into ``dest`` keeping symlinks (``filter="tar"`` when available)."""
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as tar:
        if hasattr(tarfile, "tar_filter"):
            tar.extractall(dest, filter="tar")
        else:  # pragma: no cover - Python < 3.11.4
            tar.extractall(dest)


def export_trunk_tree(
    repo_root: Path,
    trunk_ref: str,
    dest: Path,
    *,
    runner: Runner | None = None,
    bytes_runner: BytesRunner | None = None,
) -> str:
    """Export the tree of ``trunk_ref`` into ``dest`` (``git archive`` + tar extraction).

    Returns the commit sha of the ref. ``dest`` is created; an existing
    directory is reused (files are overwritten, extra files are left alone).
    Symlinks are preserved as symlinks. Submodules come out as empty
    directories (``git archive`` semantics). ``ToolError`` when the ref is
    unknown or the archive fails.
    """
    sha = trunk_sha(repo_root, trunk_ref, runner)
    if sha is None:
        raise ToolError(f"git: {trunk_ref} is unknown locally (fetch the trunk first)")
    run = bytes_runner or _default_bytes_runner
    try:
        proc = run(["git", "archive", "--format=tar", sha], repo_root)
    except OSError as exc:
        raise ToolError(f"cannot run git archive: {exc}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or b"").decode("utf-8", "replace").strip()
        raise ToolError(f"git archive {trunk_ref} failed: {detail or proc.returncode}")
    try:
        _extract(proc.stdout or b"", dest)
    except (tarfile.TarError, OSError) as exc:
        raise ToolError(f"cannot extract the trunk archive into {dest}: {exc}") from exc
    return sha


def _relative_to_root(cwd: Path, repo_root: Path) -> Path:
    """``cwd`` relative to ``repo_root``, tolerant to one side being resolved."""
    candidates = (
        (Path(cwd), Path(repo_root)),
        (Path(cwd).resolve(), Path(repo_root).resolve()),
    )
    for child, root in candidates:
        try:
            return child.relative_to(root)
        except ValueError:
            continue
    raise ToolError(f"{cwd} is not inside the repository {repo_root}")


def trunk_env_dir(dest: Path, repo_root: Path, cwd: Path) -> Path:
    """Path of the current env dir (``cwd`` under ``repo_root``) inside the export ``dest``."""
    return Path(dest) / _relative_to_root(cwd, repo_root)


def _prune(cache_root: Path, keep: str) -> None:
    for entry in cache_root.iterdir():
        if entry.name != keep:
            shutil.rmtree(entry, ignore_errors=True)


def ensure_trunk_export(
    repo_root: Path,
    trunk_ref: str,
    cache_root: Path,
    *,
    runner: Runner | None = None,
    bytes_runner: BytesRunner | None = None,
) -> tuple[Path, str]:
    """Export ``trunk_ref`` under ``cache_root/<sha>/`` once per sha; keep only that sha.

    A finished export carries an :data:`EXPORT_MARKER` file; a directory
    without it (interrupted export) is rebuilt. Returns ``(export_dir, sha)``.
    """
    sha = trunk_sha(repo_root, trunk_ref, runner)
    if sha is None:
        raise ToolError(f"git: {trunk_ref} is unknown locally (fetch the trunk first)")
    cache_root = Path(cache_root)
    cache_root.mkdir(parents=True, exist_ok=True)
    final = cache_root / sha
    if (final / EXPORT_MARKER).is_file():
        _prune(cache_root, sha)
        return final, sha
    if final.exists():
        shutil.rmtree(final, ignore_errors=True)
    export_trunk_tree(repo_root, trunk_ref, final, runner=runner, bytes_runner=bytes_runner)
    (final / EXPORT_MARKER).write_text(sha + "\n", encoding="utf-8")
    _prune(cache_root, sha)
    return final, sha


__all__ = [
    "EXPORT_MARKER",
    "ensure_trunk_export",
    "export_trunk_tree",
    "trunk_env_dir",
    "trunk_sha",
]
