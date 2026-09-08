"""Tests for tofu_overlay.trunk: trunk sha, shared-clone export, env dir mapping, cache."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from tests.conftest import git
from tofu_overlay import trunk
from tofu_overlay.models import ToolError


@pytest.fixture
def stack_repo(git_repo_with_remote: Path) -> Path:
    """Repo on ``main`` with an env dir, a modules dir and a symlink between them, pushed."""
    repo = git_repo_with_remote
    (repo / "modules" / "bucket").mkdir(parents=True)
    (repo / "modules" / "bucket" / "main.tf").write_text('resource "aws_s3_bucket" "b" {}\n')
    env_dir = repo / "stacks" / "storage" / "env" / "dev"
    env_dir.mkdir(parents=True)
    (env_dir / "main.tf").write_text('module "bucket" { source = "./bucket" }\n')
    os.symlink("../../../../modules/bucket", env_dir / "bucket")
    (repo / ".gitignore").write_text(".tofu-overlay/\n")
    git("add", ".", cwd=repo)
    git("commit", "-q", "-m", "stack", cwd=repo)
    git("push", "-q", "origin", "main", cwd=repo)
    return repo


def _commit_on_trunk(repo: Path, name: str) -> str:
    """Add a file on main and push it; return the new sha of origin/main."""
    branch = git("branch", "--show-current", cwd=repo)
    git("checkout", "-q", "main", cwd=repo)
    (repo / name).write_text("x\n")
    git("add", ".", cwd=repo)
    git("commit", "-q", "-m", name, cwd=repo)
    git("push", "-q", "origin", "main", cwd=repo)
    git("checkout", "-q", branch, cwd=repo)
    return git("rev-parse", "origin/main", cwd=repo)


def _real_git(argv: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True, check=False)


class TestTrunkSha:
    def test_known_and_unknown_refs(self, stack_repo: Path, git_repo: Path) -> None:
        assert trunk.trunk_sha(stack_repo, "origin/main") == git(
            "rev-parse", "origin/main", cwd=stack_repo
        )
        assert trunk.trunk_sha(stack_repo, "origin/nope") is None


class TestExportTrunkTree:
    def test_export_is_a_real_checkout_with_symlinks(
        self, stack_repo: Path, tmp_path: Path
    ) -> None:
        dest = tmp_path / "export"
        sha = trunk.export_trunk_tree(stack_repo, "origin/main", dest)
        assert sha == git("rev-parse", "origin/main", cwd=stack_repo)
        assert (dest / ".git").is_dir()  # a real repository: not a worktree, not an archive
        assert (dest / ".git" / "HEAD").is_file()
        assert git("rev-parse", "HEAD", cwd=dest) == sha
        assert git("branch", "--show-current", cwd=dest) == ""  # detached
        assert git("status", "--porcelain", cwd=dest) == ""
        env_dir = dest / "stacks" / "storage" / "env" / "dev"
        assert (env_dir / "main.tf").read_text() == 'module "bucket" { source = "./bucket" }\n'
        link = env_dir / "bucket"
        assert link.is_symlink()
        assert os.readlink(link) == "../../../../modules/bucket"
        assert (link / "main.tf").is_file()  # resolves inside the export
        assert not (dest / ".tofu-overlay").exists()

    def test_objects_are_shared_and_no_worktree_is_registered(
        self, stack_repo: Path, tmp_path: Path
    ) -> None:
        dest = tmp_path / "export"
        trunk.export_trunk_tree(stack_repo, "origin/main", dest)
        alternates = (dest / ".git" / "objects" / "info" / "alternates").read_text().strip()
        assert Path(alternates).resolve() == (stack_repo / ".git" / "objects").resolve()
        worktrees = git("worktree", "list", "--porcelain", cwd=stack_repo)
        assert worktrees.count("worktree ") == 1  # only the main working tree

    def test_origin_url_is_copied_from_the_repository(
        self, stack_repo: Path, tmp_path: Path
    ) -> None:
        dest = tmp_path / "export"
        trunk.export_trunk_tree(stack_repo, "origin/main", dest)
        assert git("remote", "get-url", "origin", cwd=dest) == git(
            "remote", "get-url", "origin", cwd=stack_repo
        )

    def test_without_origin_the_clone_points_at_the_repository(
        self, git_repo: Path, tmp_path: Path
    ) -> None:
        dest = tmp_path / "export"
        sha = trunk.export_trunk_tree(git_repo, "main", dest)
        assert git("rev-parse", "HEAD", cwd=dest) == sha
        assert Path(git("remote", "get-url", "origin", cwd=dest)).resolve() == git_repo.resolve()

    def test_export_reflects_the_ref_not_the_working_tree(
        self, stack_repo: Path, tmp_path: Path
    ) -> None:
        git("checkout", "-q", "-b", "feature/x", cwd=stack_repo)
        (stack_repo / "stacks" / "storage" / "env" / "dev" / "extra.tf").write_text("# branch\n")
        git("add", ".", cwd=stack_repo)
        git("commit", "-q", "-m", "branch only", cwd=stack_repo)
        (stack_repo / "README.md").write_text("dirty\n")  # uncommitted change
        dest = tmp_path / "export"
        trunk.export_trunk_tree(stack_repo, "origin/main", dest)
        assert not (dest / "stacks" / "storage" / "env" / "dev" / "extra.tf").exists()
        assert (dest / "README.md").read_text() == "acme\n"

    def test_existing_clone_is_reused_and_moved_to_the_new_sha(
        self, stack_repo: Path, tmp_path: Path
    ) -> None:
        dest = tmp_path / "export"
        first = trunk.export_trunk_tree(stack_repo, "origin/main", dest)
        stamp = (dest / ".git" / "config").stat().st_mtime_ns
        new_sha = _commit_on_trunk(stack_repo, "trunk.txt")
        assert new_sha != first
        assert trunk.export_trunk_tree(stack_repo, "origin/main", dest) == new_sha
        assert git("rev-parse", "HEAD", cwd=dest) == new_sha
        assert (dest / "trunk.txt").exists()
        assert (dest / ".git" / "config").stat().st_mtime_ns == stamp  # not re-cloned

    def test_existing_non_repository_dest_is_replaced(
        self, stack_repo: Path, tmp_path: Path
    ) -> None:
        dest = tmp_path / "export"
        dest.mkdir()
        (dest / "junk").write_text("x")
        trunk.export_trunk_tree(stack_repo, "origin/main", dest)
        assert not (dest / "junk").exists()
        assert (dest / ".git").is_dir()

    def test_unknown_ref_is_tool_error(self, stack_repo: Path, tmp_path: Path) -> None:
        with pytest.raises(ToolError, match="unknown locally"):
            trunk.export_trunk_tree(stack_repo, "origin/nope", tmp_path / "export")
        assert not (tmp_path / "export").exists()

    def test_git_failure_is_tool_error(self, stack_repo: Path, tmp_path: Path) -> None:
        def failing(argv: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
            if argv[1] == "clone":
                return subprocess.CompletedProcess(argv, 128, "", "fatal: boom")
            return _real_git(argv, cwd)

        with pytest.raises(ToolError, match="git clone .* failed: fatal: boom"):
            trunk.export_trunk_tree(stack_repo, "origin/main", tmp_path / "export", runner=failing)

    def test_all_git_calls_go_through_the_runner(self, stack_repo: Path, tmp_path: Path) -> None:
        seen: list[list[str]] = []

        def spy(argv: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
            seen.append(argv)
            return _real_git(argv, cwd)

        trunk.export_trunk_tree(stack_repo, "origin/main", tmp_path / "export", runner=spy)
        assert [argv[1] for argv in seen] == ["rev-parse", "clone", "remote", "remote", "checkout"]


class TestTrunkEnvDir:
    def test_maps_relative_path(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        cwd = repo / "stacks" / "storage" / "env" / "dev"
        assert trunk.trunk_env_dir(tmp_path / "export", repo, cwd) == (
            tmp_path / "export" / "stacks" / "storage" / "env" / "dev"
        )

    def test_tolerates_resolved_root(self, tmp_path: Path) -> None:
        real = tmp_path / "real"
        (real / "stacks" / "x").mkdir(parents=True)
        link = tmp_path / "link"
        link.symlink_to(real, target_is_directory=True)
        got = trunk.trunk_env_dir(tmp_path / "export", real.resolve(), link / "stacks" / "x")
        assert got == tmp_path / "export" / "stacks" / "x"

    def test_outside_repo_is_tool_error(self, tmp_path: Path) -> None:
        with pytest.raises(ToolError, match="not inside"):
            trunk.trunk_env_dir(tmp_path / "export", tmp_path / "repo", tmp_path / "elsewhere")


class TestEnsureTrunkExport:
    def test_cached_per_sha_and_pruned(self, stack_repo: Path, tmp_path: Path) -> None:
        cache = tmp_path / "cache"
        export_dir, sha = trunk.ensure_trunk_export(stack_repo, "origin/main", cache)
        assert export_dir == cache / sha
        assert (export_dir / trunk.EXPORT_MARKER).read_text().strip() == sha
        assert (export_dir / ".git").is_dir()
        stamp = (export_dir / "README.md").stat().st_mtime_ns
        again, _ = trunk.ensure_trunk_export(stack_repo, "origin/main", cache)
        assert again == export_dir
        assert (export_dir / "README.md").stat().st_mtime_ns == stamp  # reused, no checkout

        new_sha = _commit_on_trunk(stack_repo, "trunk.txt")
        assert new_sha != sha
        newer, got = trunk.ensure_trunk_export(stack_repo, "origin/main", cache)
        assert got == new_sha and newer == cache / new_sha
        assert (newer / "trunk.txt").exists()
        assert git("rev-parse", "HEAD", cwd=newer) == new_sha
        assert sorted(p.name for p in cache.iterdir()) == [new_sha]  # only the latest sha

    def test_interrupted_export_without_git_is_recloned(
        self, stack_repo: Path, tmp_path: Path
    ) -> None:
        cache = tmp_path / "cache"
        sha = git("rev-parse", "origin/main", cwd=stack_repo)
        partial = cache / sha
        partial.mkdir(parents=True)
        (partial / "junk").write_text("x")
        export_dir, _ = trunk.ensure_trunk_export(stack_repo, "origin/main", cache)
        assert not (export_dir / "junk").exists()
        assert (export_dir / trunk.EXPORT_MARKER).exists()
        assert (export_dir / ".git").is_dir()
        assert (export_dir / "stacks" / "storage" / "env" / "dev" / "main.tf").exists()

    def test_interrupted_checkout_is_rerun_on_the_existing_clone(
        self, stack_repo: Path, tmp_path: Path
    ) -> None:
        cache = tmp_path / "cache"
        export_dir, sha = trunk.ensure_trunk_export(stack_repo, "origin/main", cache)
        (export_dir / trunk.EXPORT_MARKER).unlink()  # marker missing = interrupted
        (export_dir / "README.md").unlink()  # half-written tree
        stamp = (export_dir / ".git" / "config").stat().st_mtime_ns
        again, got = trunk.ensure_trunk_export(stack_repo, "origin/main", cache)
        assert (again, got) == (export_dir, sha)
        assert (export_dir / "README.md").read_text() == "acme\n"
        assert (export_dir / trunk.EXPORT_MARKER).read_text().strip() == sha
        assert (export_dir / ".git" / "config").stat().st_mtime_ns == stamp  # not re-cloned

    def test_marker_without_git_is_recloned(self, stack_repo: Path, tmp_path: Path) -> None:
        cache = tmp_path / "cache"
        sha = git("rev-parse", "origin/main", cwd=stack_repo)
        stale = cache / sha
        stale.mkdir(parents=True)
        (stale / trunk.EXPORT_MARKER).write_text(sha + "\n")  # e.g. an old archive export
        export_dir, _ = trunk.ensure_trunk_export(stack_repo, "origin/main", cache)
        assert (export_dir / ".git").is_dir()
        assert git("rev-parse", "HEAD", cwd=export_dir) == sha

    def test_unknown_trunk_is_tool_error(self, git_repo: Path, tmp_path: Path) -> None:
        with pytest.raises(ToolError, match="unknown locally"):
            trunk.ensure_trunk_export(git_repo, "origin/main", tmp_path / "cache")
