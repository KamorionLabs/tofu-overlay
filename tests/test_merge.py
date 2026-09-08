"""Tests for tofu_overlay.merge helpers: imports rendering, parsing back, safe writing."""

from __future__ import annotations

import re
from pathlib import Path

import hcl2
import pytest

from tests.conftest import BASE_ETAG, BASE_KEY, BUCKET, ME, REGION
from tofu_overlay import merge
from tofu_overlay.merge import IMPORTS_FILENAME
from tofu_overlay.models import BackendConfig, ExitCode, Status, ToolError

COMMIT = "89abcdef0123456789abcdef0123456789abcdef"
GENERATED_AT = "2026-09-08T09:15:42Z"

EXPECTED_IMPORTS = {
    "aws_s3_bucket.reports": "s3-acme-dev-reports",
    'aws_ssm_parameter.flags["c"]': "/acme/dev/flags/c",
    "module.net.aws_security_group.reports": "sg-0abcdef1234567890",
    "aws_iam_role_policy.reports": "iam-acme-dev-app:reports",
}


@pytest.fixture
def base() -> BackendConfig:
    return BackendConfig(bucket=BUCKET, key=BASE_KEY, region=REGION)


@pytest.fixture
def overlay(make_overlay, make_claim):
    # deliberately unsorted insertion order
    claims = {
        "module.net.aws_security_group.reports": make_claim(
            "create",
            "aws_security_group",
            {"name": "nsg-acme-dev-reports"},
            id_="sg-0abcdef1234567890",
            import_id="sg-0abcdef1234567890",
        ),
        "aws_iam_role.app": make_claim(
            "update", "aws_iam_role", {"name": "iam-acme-dev-app"}, after_hash="ab" * 32
        ),
        'aws_ssm_parameter.flags["c"]': make_claim(
            "create",
            "aws_ssm_parameter",
            {"name": "/acme/dev/flags/c"},
            id_="/acme/dev/flags/c",
            import_id="/acme/dev/flags/c",
        ),
        "aws_iam_role_policy.reports": make_claim(
            "create",
            "aws_iam_role_policy",
            {"role": "iam-acme-dev-app", "name": "reports"},
            id_="iam-acme-dev-app:reports",
            import_id="iam-acme-dev-app:reports",
        ),
        "aws_s3_bucket.reports": make_claim(
            "create",
            "aws_s3_bucket",
            {"bucket": "s3-acme-dev-reports"},
            id_="s3-acme-dev-reports",
            import_id="s3-acme-dev-reports",
        ),
    }
    return make_overlay(status=Status.ACTIVE, claims=claims, applied_commit=COMMIT)


@pytest.fixture
def rendered(overlay, base) -> str:
    return merge.render_imports(
        overlay, base=base, base_etag=BASE_ETAG, commit=COMMIT, generated_at=GENERATED_AT
    )


def import_blocks(text: str) -> list[tuple[str, str]]:
    """(to, id) pairs in file order."""
    return re.findall(r'import\s*\{\s*to\s*=\s*(\S+)\s*id\s*=\s*"([^"]*)"\s*\}', text)


class TestRenderImports:
    def test_filename_template(self) -> None:
        assert IMPORTS_FILENAME.format(name=ME) == f"zz_overlay_{ME}.imports.tf"

    def test_blocks_sorted_by_address(self, rendered: str) -> None:
        blocks = import_blocks(rendered)
        assert [to for to, _ in blocks] == sorted(EXPECTED_IMPORTS)
        assert dict(blocks) == EXPECTED_IMPORTS

    def test_update_claims_not_rendered(self, rendered: str) -> None:
        assert "aws_iam_role.app" not in rendered

    def test_header(self, rendered: str) -> None:
        header = [line for line in rendered.splitlines() if line.startswith("#")]
        joined = "\n".join(header)
        for needle in (ME, BASE_KEY, BASE_ETAG, COMMIT, GENERATED_AT):
            assert needle in joined
        first_import = rendered.index("import")
        assert rendered[:first_import].strip().startswith("#")

    def test_identity_comments(self, rendered: str) -> None:
        assert re.search(r"#\s*identity:.*bucket=s3-acme-dev-reports", rendered)
        assert re.search(r"#\s*identity:.*name=/acme/dev/flags/c", rendered)
        ident = re.search(r"#\s*identity:.*role=iam-acme-dev-app.*", rendered)
        assert ident and "name=reports" in ident.group(0)

    def test_deterministic(self, overlay, base, rendered: str) -> None:
        again = merge.render_imports(
            overlay, base=base, base_etag=BASE_ETAG, commit=COMMIT, generated_at=GENERATED_AT
        )
        assert again == rendered
        reordered = overlay.model_copy(deep=True)
        reordered.claims = dict(sorted(overlay.claims.items(), reverse=True))
        assert (
            merge.render_imports(
                reordered, base=base, base_etag=BASE_ETAG, commit=COMMIT, generated_at=GENERATED_AT
            )
            == rendered
        )

    def test_valid_hcl(self, rendered: str) -> None:
        parsed = hcl2.loads(rendered)
        blocks = parsed["import"]
        assert len(blocks) == len(EXPECTED_IMPORTS)
        ids = [b["id"].strip('"') for b in blocks]
        assert ids == [EXPECTED_IMPORTS[a] for a in sorted(EXPECTED_IMPORTS)]

    def test_ends_with_newline(self, rendered: str) -> None:
        assert rendered.endswith("\n")


class TestReadImportsAddresses:
    def test_roundtrip(self, rendered: str, tmp_path: Path) -> None:
        path = tmp_path / IMPORTS_FILENAME.format(name=ME)
        path.write_text(rendered, encoding="utf-8")
        assert merge.read_imports_addresses(path) == EXPECTED_IMPORTS

    def test_hand_written_layout(self, tmp_path: Path) -> None:
        path = tmp_path / "imports.tf"
        path.write_text(
            "# comment\n"
            "import {\n"
            "  to   = aws_s3_bucket.reports\n"
            '  id   = "s3-acme-dev-reports"\n'
            "}\n\n"
            "import {\n"
            "\tto = aws_sns_topic.reports\n"
            '\tid = "arn:aws:sns:eu-west-1:123456789012:sns-acme-dev-reports"\n'
            "}\n",
            encoding="utf-8",
        )
        assert merge.read_imports_addresses(path) == {
            "aws_s3_bucket.reports": "s3-acme-dev-reports",
            "aws_sns_topic.reports": "arn:aws:sns:eu-west-1:123456789012:sns-acme-dev-reports",
        }

    def test_empty_file(self, tmp_path: Path) -> None:
        path = tmp_path / "imports.tf"
        path.write_text("# nothing here\n", encoding="utf-8")
        assert merge.read_imports_addresses(path) == {}


class TestWriteImports:
    def test_writes_file(self, tmp_path: Path, rendered: str) -> None:
        env_dir = tmp_path / "stacks" / "storage" / "env" / "dev"
        env_dir.mkdir(parents=True)
        path = merge.write_imports(env_dir, ME, rendered)
        assert path == env_dir / f"zz_overlay_{ME}.imports.tf"
        assert path.read_text(encoding="utf-8") == rendered
        # rewriting an existing regular file is allowed (re-merge)
        path2 = merge.write_imports(env_dir, ME, rendered + "# again\n")
        assert path2 == path
        assert path.read_text(encoding="utf-8").endswith("# again\n")

    def test_refuses_symlinked_env_dir(self, tmp_path: Path, rendered: str) -> None:
        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real, target_is_directory=True)
        with pytest.raises(ToolError) as exc:
            merge.write_imports(link, ME, rendered)
        assert exc.value.exit_code == ExitCode.ERROR
        assert list(real.iterdir()) == []

    def test_refuses_symlinked_target(self, tmp_path: Path, rendered: str) -> None:
        env_dir = tmp_path / "env"
        env_dir.mkdir()
        elsewhere = tmp_path / "elsewhere.tf"
        elsewhere.write_text("# do not touch\n", encoding="utf-8")
        (env_dir / f"zz_overlay_{ME}.imports.tf").symlink_to(elsewhere)
        with pytest.raises(ToolError):
            merge.write_imports(env_dir, ME, rendered)
        assert elsewhere.read_text(encoding="utf-8") == "# do not touch\n"
