"""Tests for tofu_overlay.trunk: trunk sha, git-archive export, env dir mapping, cache."""

from __future__ import annotations

import os
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


class TestTrunkSha:
    def test_known_and_unknown_refs(self, stack_repo: Path, git_repo: Path) -> None:
        assert trunk.trunk_sha(stack_repo, "origin/main") == git(
            "rev-parse", "origin/main", cwd=stack_repo
        )
        assert trunk.trunk_sha(stack_repo, "origin/nope") is None


class TestExportTrunkTree:
    def test_export_is_a_plain_tree_with_symlinks(self, stack_repo: Path, tmp_path: Path) -> None:
        dest = tmp_path / "export"
        sha = trunk.export_trunk_tree(stack_repo, "origin/main", dest)
        assert sha == git("rev-parse", "origin/main", cwd=stack_repo)
        env_dir = dest / "stacks" / "storage" / "env" / "dev"
        assert (env_dir / "main.tf").read_text() == 'module "bucket" { source = "./bucket" }\n'
        link = env_dir / "bucket"
        assert link.is_symlink()
        assert os.readlink(link) == "../../../../modules/bucket"
        assert (link / "main.tf").is_file()  # resolves inside the export
        assert not (dest / ".git").exists()  # never a worktree
        assert not (dest / ".tofu-overlay").exists()

    def test_export_reflects_the_ref_not_the_working_tree(
        self, stack_repo: Path, tmp_path: Path
    ) -> None:
        git("checkout", "-q", "-b", "feature/x", cwd=stack_repo)
        (stack_repo / "stacks" / "storage" / "env" / "dev" / "extra.tf").write_text("# branch\n")
        git("add", ".", cwd=stack_repo)
        git("commit", "-q", "-m", "branch only", cwd=stack_repo)
        dest = tmp_path / "export"
        trunk.export_trunk_tree(stack_repo, "origin/main", dest)
        assert not (dest / "stacks" / "storage" / "env" / "dev" / "extra.tf").exists()

    def test_unknown_ref_is_tool_error(self, stack_repo: Path, tmp_path: Path) -> None:
        with pytest.raises(ToolError, match="unknown locally"):
            trunk.export_trunk_tree(stack_repo, "origin/nope", tmp_path / "export")

    def test_archive_failure_is_tool_error(self, stack_repo: Path, tmp_path: Path) -> None:
        import subprocess

        def failing(argv: list[str], cwd: Path) -> subprocess.CompletedProcess[bytes]:
            return subprocess.CompletedProcess(argv, 128, b"", b"fatal: boom")

        with pytest.raises(ToolError, match="boom"):
            trunk.export_trunk_tree(
                stack_repo, "origin/main", tmp_path / "export", bytes_runner=failing
            )


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
        stamp = (export_dir / "README.md").stat().st_mtime_ns
        again, _ = trunk.ensure_trunk_export(stack_repo, "origin/main", cache)
        assert again == export_dir
        assert (export_dir / "README.md").stat().st_mtime_ns == stamp  # reused, not re-extracted

        new_sha = _commit_on_trunk(stack_repo, "trunk.txt")
        assert new_sha != sha
        newer, got = trunk.ensure_trunk_export(stack_repo, "origin/main", cache)
        assert got == new_sha and newer == cache / new_sha
        assert (newer / "trunk.txt").exists()
        assert sorted(p.name for p in cache.iterdir()) == [new_sha]  # only the latest sha

    def test_interrupted_export_is_rebuilt(self, stack_repo: Path, tmp_path: Path) -> None:
        cache = tmp_path / "cache"
        sha = git("rev-parse", "origin/main", cwd=stack_repo)
        partial = cache / sha
        partial.mkdir(parents=True)
        (partial / "junk").write_text("x")
        export_dir, _ = trunk.ensure_trunk_export(stack_repo, "origin/main", cache)
        assert not (export_dir / "junk").exists()
        assert (export_dir / trunk.EXPORT_MARKER).exists()
        assert (export_dir / "stacks" / "storage" / "env" / "dev" / "main.tf").exists()

    def test_unknown_trunk_is_tool_error(self, git_repo: Path, tmp_path: Path) -> None:
        with pytest.raises(ToolError, match="unknown locally"):
            trunk.ensure_trunk_export(git_repo, "origin/main", tmp_path / "cache")
