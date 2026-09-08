# Releasing

Releases are tag-driven. Every `v<major>.<minor>.<patch>` tag builds, on GitHub Actions:

- a wheel and an sdist (`uv build`);
- a single-file Linux/amd64 binary (`PyInstaller`), smoke-tested (`version`, `--help`);
- `SHA256SUMS` covering all three;
- a GitHub release with generated notes and the four files attached;
- optionally a PyPI upload (job `pypi`, trusted publishing, enabled only when the repository variable `PYPI_PUBLISH` is `true`).

## Cut a release

```sh
# 1. bump the version in pyproject.toml and src/tofu_overlay/__init__.py, update CHANGELOG.md
# 2. commit, then tag and push
git tag -a v0.1.0 -m "v0.1.0"
git push origin main v0.1.0
```

The `build` job fails if the tag does not match `pyproject.toml`.

After the release, run the `install-test` workflow (`gh workflow run install-test.yml -f version=<ver>`): it installs the published binary on `ubuntu-22.04` through `scripts/install.sh`. While the repository is private the script needs `GITHUB_TOKEN` and fetches assets through the API; on a public repository plain release URLs are used.

## Enable PyPI (one-time, maintainers)

1. On PyPI, add a *pending* trusted publisher for project `kmr-tofu-overlay`: owner `KamorionLabs`, repository `tofu-overlay`, workflow `release.yml`, environment `pypi`.
2. In the GitHub repository, create the `pypi` environment and set the repository variable `PYPI_PUBLISH=true`.

No API token is stored anywhere.

## Install matrix

| Where | Command |
|---|---|
| Laptop (uv) | `uv tool install kmr-tofu-overlay` (after PyPI is enabled) or `uv tool install git+https://github.com/KamorionLabs/tofu-overlay@v0.1.1` |
| Laptop (pipx) | `pipx install kmr-tofu-overlay` |
| Linux CI agent, no Python required | `curl -fsSL https://raw.githubusercontent.com/KamorionLabs/tofu-overlay/main/scripts/install.sh \| sh -s -- 0.1.1` |
| Private repository | same, with `GITHUB_TOKEN` set (read access to releases) |

The binary needs `tofu` (or `terraform`) on the `PATH`, like the Python package.
