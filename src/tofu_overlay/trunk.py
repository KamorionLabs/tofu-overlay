"""Trunk baseline helpers: export the trunk tree and locate the env dir inside it.

The trunk baseline (DESIGN §7.8) plans the **trunk configuration** against the
**base state** to learn which base resources would change under a trunk apply.
The trunk tree is a **real repository checkout**: a ``git clone --shared`` of
the local repository (objects shared through alternates, nothing copied) with
``trunk_ref`` checked out detached. Neither ``git archive`` nor a worktree
would do: common modules (default tags, git metadata) walk up to a
``.git/HEAD`` inside a ``.git`` *directory* and read the remotes through a
git provider; an archive has no ``.git`` and a worktree's ``.git`` is a file
that also registers itself in the repository. The clone's ``origin`` URL is
copied from the repository so a git provider sees the same remote as the
real checkout.

Every git call goes through the injectable runner of :mod:`tofu_overlay.config`.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from tofu_overlay.config import Runner, _git
from tofu_overlay.models import ToolError

EXPORT_MARKER = ".tofu-overlay-export"
"""File written at the root of a finished export, holding the commit sha."""


def trunk_sha(repo_root: Path, trunk_ref: str, runner: Runner | None = None) -> str | None:
    """Commit sha of ``trunk_ref`` (e.g. ``origin/main``), or ``None`` when unknown locally."""
    proc = _git(["rev-parse", "--verify", "--quiet", f"{trunk_ref}^{{commit}}"], repo_root, runner)
    if proc.returncode != 0:
        return None
    sha = proc.stdout.strip()
    return sha or None


def _origin_url(repo_root: Path, runner: Runner | None) -> str | None:
    """URL of the ``origin`` remote of ``repo_root``, or ``None`` when there is none."""
    proc = _git(["remote", "get-url", "origin"], repo_root, runner)
    if proc.returncode != 0:
        return None
    url = proc.stdout.strip()
    return url or None


def _git_checked(args: list[str], cwd: Path, runner: Runner | None, what: str) -> None:
    proc = _git(args, cwd, runner)
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip() or (proc.stdout or "").strip()
        raise ToolError(f"{what} failed: {detail or proc.returncode}")


def export_trunk_tree(
    repo_root: Path,
    trunk_ref: str,
    dest: Path,
    *,
    runner: Runner | None = None,
) -> str:
    """Check out the tree of ``trunk_ref`` into ``dest`` as a shared clone of ``repo_root``.

    ``dest`` becomes (or already is) a repository: ``git clone --shared
    --no-checkout`` of ``repo_root`` when ``dest/.git`` is not a directory
    (an existing non-repository ``dest`` is replaced), with ``origin`` set to
    the URL of ``repo_root``'s ``origin`` when there is one; then
    ``git checkout --force --detach <sha>`` (``--force`` so that a run on a
    half-written clone restores the tree). Objects are shared with
    ``repo_root`` (alternates), so nothing is copied and no fetch is needed.
    Symlinks are preserved by git; submodules are not initialised; untracked
    files in an existing clone are left alone. Returns the commit sha.
    ``ToolError`` when the ref is unknown or git fails.
    """
    sha = trunk_sha(repo_root, trunk_ref, runner)
    if sha is None:
        raise ToolError(f"git: {trunk_ref} is unknown locally (fetch the trunk first)")
    repo_root = Path(repo_root)
    dest = Path(dest)
    if not (dest / ".git").is_dir():
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        dest.parent.mkdir(parents=True, exist_ok=True)
        _git_checked(
            ["clone", "--shared", "--no-checkout", "--quiet", str(repo_root), str(dest)],
            repo_root,
            runner,
            f"git clone of {repo_root}",
        )
        url = _origin_url(repo_root, runner)
        if url is not None:
            _git_checked(
                ["remote", "set-url", "origin", url], dest, runner, "git remote set-url origin"
            )
    _git_checked(
        ["checkout", "--quiet", "--force", "--detach", sha],
        dest,
        runner,
        f"git checkout {trunk_ref} ({sha[:12]})",
    )
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
) -> tuple[Path, str]:
    """Export ``trunk_ref`` under ``cache_root/<sha>/`` once per sha; keep only that sha.

    A finished export is a clone carrying an :data:`EXPORT_MARKER` file. A
    directory without the marker (interrupted export) is rebuilt: the
    checkout runs again on the existing clone, or the directory is cloned
    afresh when it holds no ``.git`` directory. Returns ``(export_dir, sha)``.
    """
    sha = trunk_sha(repo_root, trunk_ref, runner)
    if sha is None:
        raise ToolError(f"git: {trunk_ref} is unknown locally (fetch the trunk first)")
    cache_root = Path(cache_root)
    cache_root.mkdir(parents=True, exist_ok=True)
    final = cache_root / sha
    if (final / EXPORT_MARKER).is_file() and (final / ".git").is_dir():
        _prune(cache_root, sha)
        return final, sha
    export_trunk_tree(repo_root, trunk_ref, final, runner=runner)
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
