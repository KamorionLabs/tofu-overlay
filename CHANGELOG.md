# Changelog

All notable changes to this project are documented in this file. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the
project adheres to [Semantic Versioning](https://semver.org/).

## 0.1.1 - 2026-09-08

- `scripts/install.sh`: download release assets through the API when `GITHUB_TOKEN` is set (private repositories); `install-test` workflow.

## 0.1.0 - 2026-09-08

### Added

- Initial design (docs/DESIGN.md) and module contracts (docs/MODULES.md).
- Documentation: README, hard limits (docs/LIMITS.md), CI integration and IAM
  permissions (docs/CI.md), stacked-overlays proposal (docs/SYNC-PROPOSAL.md),
  commented example configuration (.tofu-overlay.example.yaml).
- Package scaffolding, GitHub Actions CI (ruff + pytest on 3.11/3.12), MIT
  license.
- Overlay-aware cross-stack `terraform_remote_state` reads (multi-stack PR):
  `backend.parse_remote_state_refs`, `OverlayService.remote_overlay_keys`,
  `TF_VAR_tofu_overlay_keys` (JSON map `base key -> overlay key`) exported to
  every tofu run of an overlay, `remote_overlays` in `--json` output of
  `plan`/`apply`/`status`, `check` warnings/errors on mapped overlays,
  `finalize` warning about consumer overlays of sibling stacks. Documented in
  docs/MULTI-STACK.md; LIMITS.md §7 rewritten accordingly.

### Changed

- Backend-agnostic store layer: new `store` module with the `StateStore`
  protocol, `CasConflict` (re-exported by `s3state`) and the
  `make_store(cfg, session=None)` factory dispatching on the new
  `BackendConfig.backend_type` field (`s3` only; other types raise
  `backend '<type>' is not supported yet, see docs/ROADMAP.md`).
  `Registry`, `OverlayService` and the CLI are typed against `StateStore`
  and no longer import `S3State`; `OverlayService.s3` is now
  `OverlayService.store`. `S3State` digest/lock methods renamed to
  backend-neutral names (`digest_item_exists`, `delete_digest_item`,
  `lock_info`, `delete_lock_marker`).
- `backend.parse_hcl_backend` and `read_cached_backend` report the backend
  type (`backend_type` entry) instead of refusing non-`s3` blocks;
  `resolve_backend` refuses unsupported types with the message above.
- Overlay prefix and archive-key detection go through `BackendConfig`
  (`overlay_prefix`, `is_archive_key`); no manual `@` key building outside
  the key builders.
- docs/ROADMAP.md: single list of deferred work, per-backend requirements
  (`azurerm`, `gcs`, `http`, `local`) and extension points; linked from the
  README, DESIGN and LIMITS. MODULES.md updated for the new module.

### Fixed

- `apply` asks its own confirmation: a saved plan never prompts in tofu.
- `TF_CLI_ARGS*` are dropped from the tofu environment; `-out` is rejected.
- Claims of own resources deleted by a plan are released after the apply;
  a superseded apply cannot record its outcome (`run_id` checked).
- `merge`/`rebase`/`undo` status changes carry preconditions and merge claims
  instead of replacing them.
- Abandoned overlays keep `pending_revert` in their tombstone (`doctor` warns).
- `guard` dependency rule works from `dependencies` recorded in the claims.
- Registry entries must carry the state key derived from their name; deletes
  never target the base key.
- Plan JSON `mode` is authoritative for data sources; `after_unknown`
  attributes no longer defeat `virtual_attributes`; `--accept-recreate`
  verifies; empty optional import-id groups are dropped.
- `-C` with a relative `--backend-config`, symlinked env dirs refused for
  mutating commands, `rebase` conflicts exit 3, plan files are pruned.
- Release workflow: wheel, sdist, Linux binary, GitHub release, optional PyPI trusted publishing; `scripts/install.sh`.
