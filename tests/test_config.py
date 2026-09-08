"""Tests for tofu_overlay.config: naming, policy globs, CI detection, git helpers."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

from tests.conftest import git
from tofu_overlay import config
from tofu_overlay.models import ExitCode, OverlayError, PolicyConfig, ToolConfig

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")


class TestOverlayNameFor:
    def test_slug_and_hash_suffix(self) -> None:
        branch = "feature/ABC-12-reports"
        name = config.overlay_name_for(branch)
        digest = hashlib.sha1(branch.encode("utf-8")).hexdigest()[:6]
        assert name == f"feature-abc-12-reports-{digest}"
        assert NAME_RE.match(name)

    def test_deterministic_and_branch_specific(self) -> None:
        a = config.overlay_name_for("feature/x")
        assert a == config.overlay_name_for("feature/x")
        assert a != config.overlay_name_for("feature/X")
        assert a != config.overlay_name_for("feature/y")

    def test_long_branch_is_truncated_to_34_plus_hash(self) -> None:
        branch = "feature/" + "a" * 80
        name = config.overlay_name_for(branch)
        assert len(name) == 34 + 1 + 6
        assert NAME_RE.match(name)
        slug, _, digest = name.rpartition("-")
        assert len(digest) == 6
        assert slug == ("feature-" + "a" * 80)[:34]

    def test_separators_collapsed_and_stripped(self) -> None:
        name = config.overlay_name_for("--Fix__Thing/#42--")
        assert NAME_RE.match(name)
        assert name.startswith("fix-thing-42-")
        assert "--" not in name

    def test_leading_digits_allowed(self) -> None:
        assert NAME_RE.match(config.overlay_name_for("42-hotfix"))


class TestBaseKeyAllowed:
    def test_empty_list_denies(self) -> None:
        assert config.base_key_allowed("acme/webshop/storage/dev", PolicyConfig()) is False

    def test_globs(self) -> None:
        policy = PolicyConfig(allowed_base_keys=["acme/webshop/*/dev", "acme/*/sandbox"])
        assert config.base_key_allowed("acme/webshop/storage/dev", policy) is True
        assert config.base_key_allowed("acme/pim/sandbox", policy) is True
        assert config.base_key_allowed("acme/webshop/storage/prod", policy) is False
        assert config.base_key_allowed("other/webshop/storage/dev", policy) is False

    def test_exact_key(self) -> None:
        policy = PolicyConfig(allowed_base_keys=["a/b/terraform.tfstate"])
        assert config.base_key_allowed("a/b/terraform.tfstate", policy) is True
        assert config.base_key_allowed("a/b/terraform.tfstate@x", policy) is False


class TestCiDetection:
    def test_not_ci(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CI", raising=False)
        monkeypatch.delenv("TF_BUILD", raising=False)
        assert config.is_ci() is False
        assert config.is_ado() is False

    @pytest.mark.parametrize("value", ["true", "True", "TRUE"])
    def test_ci_env(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        monkeypatch.setenv("CI", value)
        monkeypatch.delenv("TF_BUILD", raising=False)
        assert config.is_ci() is True
        assert config.is_ado() is False

    def test_ado_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CI", raising=False)
        monkeypatch.setenv("TF_BUILD", "True")
        assert config.is_ci() is True
        assert config.is_ado() is True

    def test_ci_false_string(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CI", "false")
        monkeypatch.delenv("TF_BUILD", raising=False)
        assert config.is_ci() is False


class TestLoadConfig:
    def test_defaults_without_file(self, git_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TOFU_OVERLAY_BINARY", raising=False)
        cfg = config.load_config(git_repo)
        assert isinstance(cfg, ToolConfig)
        assert cfg.binary == "tofu"
        assert cfg.policy.allowed_base_keys == []
        assert cfg.policy.trunk_branch == "main"
        assert cfg.policy.tombstone_days == 14
        assert cfg.policy.apply_timeout_min == 90

    def test_file_found_by_walking_up(
        self, git_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("TOFU_OVERLAY_BINARY", raising=False)
        (git_repo / ".tofu-overlay.yaml").write_text(
            "policy:\n"
            "  allowed_base_keys: ['acme/webshop/*/dev']\n"
            "  trunk_branch: develop\n"
            "  tombstone_days: 7\n"
            "binary: terraform\n"
            "identity:\n"
            "  acme_widget: [widget_name]\n"
            "import_ids:\n"
            "  acme_widget: '{widget_name}'\n"
            "virtual_attributes:\n"
            "  acme_widget: [force]\n",
            encoding="utf-8",
        )
        env_dir = git_repo / "stacks" / "storage" / "env" / "dev"
        env_dir.mkdir(parents=True)
        cfg = config.load_config(env_dir)
        assert cfg.policy.allowed_base_keys == ["acme/webshop/*/dev"]
        assert cfg.policy.trunk_branch == "develop"
        assert cfg.policy.tombstone_days == 7
        assert cfg.policy.apply_timeout_min == 90
        assert cfg.binary == "terraform"
        assert cfg.identity == {"acme_widget": ["widget_name"]}
        assert cfg.import_ids == {"acme_widget": "{widget_name}"}
        assert cfg.virtual_attributes == {"acme_widget": ["force"]}

    def test_binary_env_override(self, git_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (git_repo / ".tofu-overlay.yaml").write_text("binary: terraform\n", encoding="utf-8")
        monkeypatch.setenv("TOFU_OVERLAY_BINARY", "/opt/acme/bin/tofu")
        assert config.load_config(git_repo).binary == "/opt/acme/bin/tofu"


class TestRepoRoot:
    def test_find_repo_root(self, git_repo: Path) -> None:
        nested = git_repo / "stacks" / "x"
        nested.mkdir(parents=True)
        assert config.find_repo_root(nested).resolve() == git_repo.resolve()

    def test_no_repo(self, tmp_path: Path) -> None:
        lonely = tmp_path / "lonely"
        lonely.mkdir()
        assert config.find_repo_root(lonely) is None

    def test_ensure_gitignored(self, git_repo: Path) -> None:
        assert config.ensure_gitignored(git_repo, ".tofu-overlay/") is False
        (git_repo / ".gitignore").write_text(".tofu-overlay/\n", encoding="utf-8")
        assert config.ensure_gitignored(git_repo, ".tofu-overlay/") is True
        # never edits files
        assert (git_repo / ".gitignore").read_text(encoding="utf-8") == ".tofu-overlay/\n"


class TestGitHelpers:
    def test_current_branch_and_head(self, git_repo: Path) -> None:
        assert config.current_branch(git_repo) == "main"
        git("checkout", "-q", "-b", "feature/ABC-12-reports", cwd=git_repo)
        assert config.current_branch(git_repo) == "feature/ABC-12-reports"
        head = config.head_commit(git_repo)
        assert re.fullmatch(r"[0-9a-f]{40}", head)
        assert head == git("rev-parse", "HEAD", cwd=git_repo)

    def test_detached_head_is_tool_error(self, git_repo: Path) -> None:
        git("checkout", "-q", "--detach", cwd=git_repo)
        with pytest.raises(OverlayError) as exc:
            config.current_branch(git_repo)
        assert exc.value.exit_code == ExitCode.ERROR

    def test_tree_dirty(self, git_repo: Path) -> None:
        assert config.is_tree_dirty(git_repo) is False
        (git_repo / "README.md").write_text("changed\n", encoding="utf-8")
        assert config.is_tree_dirty(git_repo) is True

    def test_git_user_email(self, git_repo: Path) -> None:
        assert config.git_user_email(git_repo) == "dev.one@example.com"

    def test_resolve_overlay_name_precedence(
        self, git_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        git("checkout", "-q", "-b", "feature/ABC-12-reports", cwd=git_repo)
        monkeypatch.delenv("TOFU_OVERLAY_NAME", raising=False)
        assert config.resolve_overlay_name(None, git_repo) == config.overlay_name_for(
            "feature/ABC-12-reports"
        )
        monkeypatch.setenv("TOFU_OVERLAY_NAME", "from-env-1a2b3c")
        assert config.resolve_overlay_name(None, git_repo) == "from-env-1a2b3c"
        assert config.resolve_overlay_name("explicit-9f8e7d", git_repo) == "explicit-9f8e7d"


class TestRemoteHelpers:
    def test_branch_contains_trunk_unknown_remote(self, git_repo: Path) -> None:
        assert config.branch_contains_trunk(git_repo, "main") is None

    def test_branch_contains_trunk(self, git_repo_with_remote: Path) -> None:
        repo = git_repo_with_remote
        git("checkout", "-q", "-b", "feature/ABC-12-reports", cwd=repo)
        assert config.branch_contains_trunk(repo, "main") is True
        # trunk moves forward on the remote: the feature branch is now behind
        git("checkout", "-q", "main", cwd=repo)
        (repo / "trunk.txt").write_text("more\n", encoding="utf-8")
        git("add", ".", cwd=repo)
        git("commit", "-q", "-m", "trunk moves", cwd=repo)
        git("push", "-q", "origin", "main", cwd=repo)
        git("checkout", "-q", "feature/ABC-12-reports", cwd=repo)
        assert config.branch_contains_trunk(repo, "main") is False
        git("merge", "-q", "--no-edit", "origin/main", cwd=repo)
        assert config.branch_contains_trunk(repo, "main") is True

    def test_remote_branch_exists(self, git_repo_with_remote: Path) -> None:
        repo = git_repo_with_remote
        assert config.remote_branch_exists(repo, "main") is True
        assert config.remote_branch_exists(repo, "feature/nope") is False
        git("checkout", "-q", "-b", "feature/ABC-12-reports", cwd=repo)
        git("push", "-q", "-u", "origin", "feature/ABC-12-reports", cwd=repo)
        assert config.remote_branch_exists(repo, "feature/ABC-12-reports") is True
