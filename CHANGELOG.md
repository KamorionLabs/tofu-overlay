# Changelog

All notable changes to this project are documented in this file. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the
project adheres to [Semantic Versioning](https://semver.org/).

## Unreleased

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
