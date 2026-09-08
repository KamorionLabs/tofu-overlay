# tofu-overlay — design (v1)

Status: v1 spec, 2026-09-08, after a three-lens adversarial review of the v0 draft (Terraform semantics, concurrency/safety, operability). Target: OpenTofu >= 1.7 with the `s3` backend (DynamoDB lock and/or `use_lockfile`). Terraform >= 1.5 best effort. Workspaces other than `default`, backends other than `s3` and state encryption surgery are out of scope for v1 (detected and refused). The store layer is backend-agnostic by construction; see [ROADMAP.md](ROADMAP.md) for what another backend needs.

## 1. Problem

Several teams work in parallel on the same OpenTofu root modules ("stacks"), each stack having one state per environment (S3 + DynamoDB lock). The integration environment is a shared sandbox. Today the only way to test a branch on the sandbox is to run `apply` from that branch against the shared state, which writes the branch's resources into the shared state (the next trunk apply destroys them), lets two branches revert each other's changes on shared resources, and gives no visibility on who is changing what in the sandbox.

## 2. Idea

An **overlay** is a copy-on-write fork of a base state, bound to a git branch:

- `create` forks the base state into `<key>@<name>` (new lineage, same content), so the overlay plans only the branch's delta.
- `plan`/`apply` run the regular OpenTofu binary against the overlay state, after **policy checks** computed from the plan JSON and a shared **registry**.
- The registry records, per base state, every overlay and its **claims**: resources it creates (address + physical identity) and base resources it updates. Claims are held until the overlay is finalized or abandoned, so two overlays can never create the same resource nor update the same base resource.
- `merge` hands the overlay's created resources to the trunk through generated `import {}` blocks committed with the branch, verified with a plan against the base state.
- `abandon` destroys only the overlay's own resources.

Scope is **additive**: an overlay may create resources and, with an exclusive claim, update base resources. It may not destroy, replace, move, forget or import base resources.

The cloud has no branches: two overlays can run in parallel only when their cloud changes are disjoint at resource granularity. The registry enforces that. The tool does not protect an overlay's *updates* from a trunk apply (the trunk config does not know the overlay); `check` detects it and asks for a rebase.

## 3. Invariants (the safety core)

1. **State objects are only written by OpenTofu.** Every read of a state is `tofu state pull`, every write is `tofu state push` (or `apply`), run in a dedicated `TF_DATA_DIR` whose backend points at the target key. This keeps the DynamoDB `-md5` digest item, the lock protocol, `use_lockfile`, lineage/serial checks and state encryption consistent. The only raw store operations on state keys are `head` (ETag, existence), `list_prefix` (doctor), `copy` to an archive key and `delete` (+ the digest item and lock marker) at finalize/abandon; they are the `store.StateStore` contract, implemented for `s3` by `s3state.py` (S3 `HEAD`/`ListObjects`/`CopyObject`/`DeleteObject`, DynamoDB `DeleteItem`, `.tflock`).
2. **The base state is never written by the tool** in v1. The trunk adopts overlay resources through `import {}` blocks applied by the trunk pipeline.
3. **The registry stores intention, the overlay state stores reality.** Ids, import ids and identities are recomputed from the overlay state whenever they matter (status, check, merge, finalize, abandon). Registry values are a cache.
4. **Claims live until `finalize` or `abandon`.** Statuses `creating`, `active`, `applying`, `dirty`, `merging` all count in conflict checks.
5. **`plan`, `check`, `status`, `list`, `doctor`, `guard`, `gc` never write anything** (registry included). Only `create`, `apply`, `rebase`, `merge`, `finalize`, `abandon` write, and each prints the resolved backend and what it is about to do.
6. **Overlays are only allowed on base keys matching `policy.allowed_base_keys`** (globs, default deny). Exit code 6 otherwise.
7. **Every tofu run validates the overlay first**: registry entry exists with a live status, `HEAD` of the overlay key succeeds, the lineage pulled from the overlay state equals the registry's `lineage`, the current git branch equals the overlay's `branch` (or `--name` was given explicitly). A missing key is never treated as an empty state.
8. **Freshness** = the base ETag read by `HEAD` equals the overlay's `base_etag` (serial is informative only). `apply` refuses stale overlays; `plan` warns. Additionally the branch must contain the trunk (`git merge-base --is-ancestor origin/<trunk> HEAD`), else `plan`/`apply` refuse unless `--allow-behind` (never in CI).
9. **Pass-through arguments** `-target`, `-replace`, `-refresh-only`, `-destroy`, `-state`, `-lock=false`, `-out` are rejected for `plan`/`apply`; `TF_CLI_ARGS*` variables (tofu's other way to inject arguments) are dropped from the environment of every tofu run. The only `-target` ever passed to tofu is the tool's own, in `apply --only-claims` (§7.9).

## 4. Vocabulary

- **base**: the trunk state `s3://<bucket>/<key>`, default workspace only.
- **overlay key**: `<key>@<name>`; if `<key>` has an extension the suffix goes before it (`terraform@<name>.tfstate`). `<name>` = `<slug(branch)[:34]>-<sha1(branch)[:6]>`, or `--name`/`TOFU_OVERLAY_NAME`. The name is recomputed from the branch on every run; there is no `current` file.
- **registry key**: `<key>.overlays.json` (extension-aware: `terraform.overlays.json`). One JSON document per base, S3 conditional writes (`IfMatch` ETag; `IfNoneMatch: *` on creation, only when no `<key>@*` object exists).
- **archive key**: `<key>@<name>.<status>-<timestamp>` used by `finalize`/`abandon` instead of deleting the overlay state outright (`gc --purge` deletes archives later).
- **data dirs**: `.tofu-overlay/<name>/` (overlay), `.tofu-overlay/_base/` (base, read-only use: `state pull`, verify plans, `trunk_baseline.json` cache) and `.tofu-overlay/_trunk-data/` (base key, read-only: the trunk baseline plan, §7.8). `.tofu-overlay/_trunk/<sha>/` holds the exported trunk tree (a `git clone --shared` of the repository with `origin/<trunk>` checked out detached, latest sha only). `TF_PLUGIN_CACHE_DIR` defaults to `~/.cache/tofu-overlay/plugins`. The tool warns if `.tofu-overlay/` is not git-ignored.

## 5. Registry document

```json
{
  "version": 1,
  "tool_version": "0.1.0",
  "base": {"bucket": "acme-tfstate", "key": "acme/webshop/storage/dev", "lineage": "…"},
  "overlays": {
    "abc-12-reports-3f9a1c": {
      "state_key": "acme/webshop/storage/dev@abc-12-reports-3f9a1c",
      "lineage": "…",
      "branch": "feature/ABC-12-reports",
      "owners": ["jean@example.com"],
      "caller_arn": "arn:aws:sts::123456789012:assumed-role/…",
      "binary": "tofu", "tofu_version": "1.11.1",
      "created_at": "…", "updated_at": "…",
      "base_serial": 412, "base_etag": "\"9c1…\"", "trunk_commit": "…",
      "status": "active",
      "applied_commit": "…", "run_id": null,
      "claims": {
        "aws_s3_bucket.reports": {"kind": "create", "type": "aws_s3_bucket",
          "identity": {"bucket": "s3-acme-dev-reports"}, "id": "s3-acme-dev-reports",
          "import_id": "s3-acme-dev-reports", "claimed_at": "…", "updated_at": "…"},
        "aws_iam_role.reports": {"kind": "update", "type": "aws_iam_role",
          "identity": {"name": "iam-acme-dev-reports"}, "after_hash": "…", "claimed_at": "…"}
      },
      "pending_revert": [],
      "last_apply": {"at": "…", "summary": {"create": 3, "update": 1}}
    }
  },
  "tombstones": {"old-name-1a2b3c": {"status": "merged", "at": "…", "branch": "…", "pending_revert": []}}
}
```

Statuses: `creating` → `active` ⇄ `applying` → `active` | `dirty`; `active`/`dirty` → `merging` → `merged`; `active`/`dirty` → `abandoned`. `merged`/`abandoned` entries move to `tombstones` and stay `policy.tombstone_days` (default 14); `create` refuses a tombstoned name without `--force-name`.

| status | plan | apply | rebase | merge | finalize | abandon |
|---|---|---|---|---|---|---|
| creating | refuse (resume `create`) | refuse | refuse | refuse | refuse | allowed (cleanup) |
| active | yes | yes | yes | yes | refuse | yes |
| applying (younger than `policy.apply_timeout_min`) | refuse | refuse | refuse | refuse | refuse | refuse |
| applying (older) | treated as dirty | | | | | |
| dirty | yes | yes (retry) | refuse | refuse | refuse | yes |
| merging | verify mode only | refuse (exit 7) | refuse | `--undo` only | yes | `--keep-resources` only |
| merged / abandoned | refuse | refuse | refuse | refuse | refuse | refuse |

`Registry.update(fn)`: GET (ETag) → `fn(doc)` (must be idempotent: set-by-key, never append) → PUT `IfMatch` → on 412/409 re-GET and retry with bounded backoff; when the budget is exhausted, re-GET once and succeed if the mutation is already present. Fail-closed on missing (when overlay objects exist), invalid or newer-version documents. `base.lineage` is checked against the pulled base state; a lineage change invalidates every overlay (status `needs-review`, mutating commands refuse).

## 6. Commands and exit codes

All commands run from the stack's env directory (`-C/--chdir` accepted). Backend resolution order: flags/env (`TOFU_OVERLAY_BUCKET`, `_KEY`, `_REGION`, `_PROFILE`, `_DYNAMODB_TABLE`) > `--backend-config FILE` (repeatable, same files the pipeline uses) > cached backend in `.terraform/terraform.tfstate` > HCL `backend "<type>"` block in `*.tf`. The parsers report the backend type; resolution refuses any type without a store implementation (`backend 'azurerm' is not supported yet, see docs/ROADMAP.md`), unresolved values and non-default workspaces (`TF_WORKSPACE`, `.terraform/environment`) with a clear message. The resolved tuple is echoed first; `--print-backend` prints it and exits.

| Command | Effect |
|---|---|
| `create [--name N] [--force-name]` | Register `creating` (CAS), pull base (base data dir), set new lineage, serial 0 → push to the overlay key (overlay data dir, `init -reconfigure -backend-config=key=<overlay>` with the original backend-config files first and `key` last), record `base_etag`/`base_serial`/`trunk_commit`, flip to `active`. Refuses: base key not allowed, name active, tombstoned name, `<key>@<name>` object or `-md5` item already present outside the registry (run `doctor`), base object missing (no `--empty-base` in v1). Re-running resumes a `creating` entry. |
| `plan [--json] [--detailed-exitcode] [-- tofu args]` | Validate overlay (§3.7), freshness and git ancestry, `init` if needed, `plan -out=tfplan.<run_id> -detailed-exitcode`, `show -json`, policy checks (§7). When the plan holds a new `update` on a base address, the trunk baseline (§7.8) is computed (cached) and the drifted addresses are listed separately from the claims (`drift: ADDR` lines; `--json` carries `drift: [...]` and `ignored: [...]`). Read-only. In status `merging`: verify mode (§8). |
| `apply [--auto-approve] [--allow-stale] [--accept-drift \| --only-claims]` | Refuses with exit 4 when `plan` reports trunk drift on base updates, unless `--accept-drift` (in CI it also requires `--yes`): the drifted addresses are then claimed as regular `update`s, with a warning listing them; or `--only-claims` (exclusive with `--accept-drift`, error if both): when the plan carries drift or ignored updates, a second plan targeted at exactly the claim set (`-target` per claim, the tool's own targeting, §7.9) is evaluated with the same inputs, gated, and applied instead — drift and ignored updates stay unapplied, `last_apply.only_claims` is recorded, the human output prints `targeted apply: N address(es)` and `--json` carries `only_claims` and `targets`; without drift or ignored updates the flag is a no-op (single plan). Then `plan` then: mark `applying` + `run_id` (CAS, refuse if base moved), **acquire claims atomically** (re-run checks 3-5 inside the CAS), `apply tfplan`, pull overlay state, fill `id`/`import_id`/identity from state (filtered by `after_sensitive`), record `applied_commit`, `caller_arn`, `tofu_version`, re-read base ETag (moved → stale), status `active` (or `dirty` on failure; claims kept). A saved plan never prompts in tofu, so the tool asks its own yes/no confirmation before acquiring claims (skipped by `--auto-approve` or `--yes`, refused in CI without them); `--auto-approve` only in CI or with `--yes`. Own resources deleted by the plan lose their claim once the apply succeeded and the state confirms they are gone; a superseded apply (stale `applying` taken over by a newer run) cannot record its outcome. |
| `status [--json] [--repo]` / `list [--json]` | Overlays of this base (or every base under `policy.env_dir_glob` with `--repo`), owners, branch, freshness, status, claims, age, pending reverts. Read-only. `list --bucket B --prefix P` scans `*.overlays.json` without a checkout. |
| `check [--json] [--repo]` | CI gate, read-only: overlay exists for the branch (else exit 0 with a "no overlay" line), fresh, branch contains trunk, every `create` claim has an instance in the overlay state, no base resource with a claimed address/identity appeared, no conflicting overlay, imports file (if any) matches the claims. Trunk drift (§7.8) is reported as a warning (`trunk drift: N resource(s) would change under a trunk plan`). |
| `rebase` | Refuse if stale-only is false, status not `active`, or the branch is behind trunk. Pull base and overlay, build the new document locally (base content + overlay's `create` instances merged per resource entry, `provider` equality and `schema_version` checks, overlay lineage kept, serial = max + 1), conflict checks against the new base (address, identity), one `state push` to the overlay key, then update `base_etag`/`base_serial`/`trunk_commit`. Typed confirmation. Previous overlay state archived (`.rebase-<ts>`). |
| `merge [--undo] [--allow-import-updates] [--accept-recreate ADDR,…] [--allow-unapplied]` | Import strategy only in v1. Refuse if dirty, HEAD != `applied_commit` or dirty tree (unless `--allow-unapplied`), or any `create` claim is non-importable (listed, unless accepted for recreation). Write `zz_overlay_<name>.imports.tf` in the env dir (must not be a symlink; sorted; header with overlay, base key/ETag, commit, per-block identity), verify (§8), status `merging`. `--undo` deletes the file and returns to `active`. |
| `finalize [--purge]` | Precondition: the trunk applied the imports. Pull base: every `create` claim must be present with the same `id` (an address present with a different id = the trunk created its own object → refuse with report). Archive the overlay key (copy to archive key, delete object + `-md5` item + `.tflock`), remove local data dir, status `merged` → tombstone. Prints the `git rm` for the imports file (never edits the trunk). Typed confirmation. |
| `abandon [--keep-resources] [--dry-run]` | Refuse in `merging`/`merged` (except `--keep-resources`). Verify no `create` claim identity is present in the base (else "already merged, run finalize"). Sequence: `state rm` every address that is not a `create` claim from the overlay state (so base resources can never be destroyed), `plan -destroy -out`, gate: only `delete` actions on `create` claims (deposed included), `apply`, pull state, verify the addresses are gone, archive key, release claims, record `pending_revert` for `update` claims in the tombstone (shown by `status`, warned by `doctor`) and print the trunk pipeline to run, remove local data dir, status `abandoned`. `--keep-resources` skips the destroy and prints the orphaned ids. Typed confirmation; `--dry-run` prints every S3/DynamoDB/tofu operation. |
| `doctor` | Read-only report: overlay objects `<key>@*` absent from the registry, registry entries whose key is missing, orphan `-md5` items and `.tflock`s, stale `applying`, tombstones, `.tofu-overlay/` not git-ignored, tofu version vs `.opentofu-version`, imports files whose overlay is merged. |
| `guard PLAN_JSON` | For the trunk pipeline, read-only: fails if the trunk plan creates an identity claimed by an overlay, deletes/replaces an address under an `update` claim, or deletes an address that an overlay's `create` instances depend on (the `dependencies` of every created instance are recorded in its claim at apply, so the registry alone is enough). |
| `gc [--purge]` | Report overlays whose branch no longer exists on the remote, and archives. `--purge` deletes archive objects only (typed confirmation). Never destroys cloud resources. |

Exit codes (all commands): 0 ok (changes are reported, not signalled), 1 tool/tofu error, 3 policy violation, 4 stale or behind trunk, 5 registry conflict/unreachable/invalid, 6 base key not allowed or overlay not found, 7 overlay frozen (`merging`). `--detailed-exitcode` on `plan` restores 2 for "changes present". CI = `CI=true` or `TF_BUILD=True`: no colour, `##vso[task.logissue]` lines on ADO, `--allow-stale`/`--allow-behind` refused, `--yes` accepted for `apply`. `--json` output on `plan`, `check`, `status`, `list`, `doctor` (versioned `schema: 1`).

Two variables are exported to every tofu run of an overlay (plan, apply, the destroy plan of `abandon`, the verify plan of `merge`); both are ignored when undeclared:

- `TF_VAR_tofu_overlay_name`: the overlay name;
- `TF_VAR_tofu_overlay_keys`: a JSON object `{"<base key>": "<overlay key>"}` for every `data "terraform_remote_state"` (`s3` backend) of the env dir whose base holds a live overlay of the same name with an existing state object (`{}` otherwise). Refs to the stack's own base, unresolved keys and unreadable registries are skipped and reported. The stack opts in with `key = lookup(var.tofu_overlay_keys, "<base key>", "<base key>")`, see [MULTI-STACK.md](MULTI-STACK.md). `plan`/`apply` print one line per mapped ref and `--json` carries `remote_overlays`; `status` shows the map; `check` warns when a mapped overlay is `merging` and errors when its object is gone; `finalize` warns (best effort, over `policy.env_dir_glob`) about live overlays of sibling stacks that read the finalized base.

## 7. Policy checks (plan JSON)

Input: `resource_changes[]` (address, `previous_address`, `deposed`, type, `mode`, `change.actions`, `before`, `after`, `after_unknown`, `before_sensitive`, `after_sensitive`, `replace_paths`, `importing`, `action_reason`), `resource_drift[]`, the set of base addresses (from the base state pulled at `create`/`rebase`, cached in the overlay data dir and refreshed on demand), the registry, the type knowledge and, when the plan holds a new `update` on a base address, the trunk baseline (item 8).

1. Freshness (§3.8).
2. Actions, explicit allow-list; anything else is denied:
   - `["create"]` → allowed; claim `create` (identity from `after`, minus sensitive attributes; unknown identity → address-only claim + warning).
   - `["update"]` on a base address → items 7 and 8 first; otherwise allowed with an `update` claim (exclusive). On an address created by this overlay → allowed, no claim.
   - `["no-op"]`, `["read"]` → ignored.
   - `["delete"]`, `["delete","create"]`, `["create","delete"]`, `["forget"]`, `["forget","create"]` on a base address → denied. On an address created by this overlay → allowed (replace of own resource).
   - `previous_address` set (moved) on a base address → denied. `importing` set on an address (branch `import {}` block) → denied (an overlay adopts nothing). `deposed` entries → allowed only for `create` claims.
3. Address conflict: another live overlay claims the address (any kind) → denied.
4. Identity conflict: another live overlay's `create` claim has the same identity → denied.
5. Base identity conflict: a `create` whose identity already exists in the base state → denied.
6. Drift on own updates (warning): `resource_drift[]` entry for an `update` claim whose refreshed value differs from `after_hash` recorded at apply → "the trunk (or someone) changed it, rebase".
7. Environment-dependent attributes (noise): an `update` on a base address whose differing top-level attributes (`before` vs `after`, `after_unknown` keys excluded) are all in `ignored_attributes[type]` (globs; `"*"` applies to every type; defaults: `aws_lambda_function: [filename, last_modified]`, `aws_lambda_layer_version: [filename]`, `archive_file: [output_path]`, `"*": [last_modified]`) is **not a branch change**: no claim, listed in `PolicyResult.ignored`, one warning. The change stays in the tofu plan and is applied (tofu cannot skip a change without `-target`); it is harmless by construction (a local path under another `TF_DATA_DIR`, a timestamp).
8. Trunk drift: the **trunk baseline** is a plan of the trunk config (`origin/<trunk>` checked out detached in a `git clone --shared` of the repository under `.tofu-overlay/_trunk/<sha>/` — a real repository with a `.git` directory and the same `origin` URL, so modules that read git metadata work; never `git archive` nor a worktree — planned from the same relative env dir in `.tofu-overlay/_trunk-data/`, backend on the base key, `-refresh` on, no overlay variable exported) against the **base state**: `address -> actions` of every non-no-op managed change, cached in `.tofu-overlay/_base/trunk_baseline.json` keyed by (trunk sha, base ETag). An `update` on a base address that appears in the baseline (and is not already under an `update` claim of this overlay) is **drift**: the base lags the trunk and a trunk plan would produce that update too. No claim, listed in `PolicyResult.drift`, one warning ("N base resources differ because the trunk is not applied on this base: run the trunk pipeline, then `rebase`, or pass --accept-drift to claim them"). `apply` refuses (exit 4) unless `--accept-drift`, which turns them into regular `update` claims. `delete`/replace of base addresses stay denied whatever the baseline says. When `origin/<trunk>` is unknown locally, the export fails or the env dir does not exist on the trunk, the baseline is unavailable (warning) and every base update is claimed as before.

9. Targeted apply (`apply --only-claims`): when the full plan carries drift (8) or ignored updates (7), the tool re-plans with `-target=<addr>` for every address of the claim set (creates and updates) and evaluates that plan with the same inputs (same base addresses, same baseline). It is applied only when every non-no-op managed change is a claim of this overlay (an existing one, or one of the full plan) or an ignored-attributes update: a `drift` address or any other address that tofu pulled in through dependencies refuses the apply (exit 3, "dependency of your changes carries trunk drift; apply the trunk first or use --accept-drift"); deletes/replaces of base addresses stay denied as always. The claims acquired are those of the targeted evaluation (a subset of the full plan's claims plus the untouched existing ones); a full-plan claim absent from the targeted plan is dropped with a warning, not acquired. Why a tool-controlled `-target` is acceptable while pass-through `-target` is refused (§3.9): the target set is not chosen by the user, it is exactly the claim set the policy already accepted, and the resulting plan is gated again before apply, so it can never touch anything the full plan would not have claimed. What it leaves behind is documented in LIMITS.md §15.

Order per base `update`: 7 (ignored attributes), then 8 (drift), then the claim. Checks 3-5 are recomputed inside the registry critical section when claims are acquired.

## 8. Merge verification (import strategy)

The verify plan runs the **branch config** against the **base state** in the base data dir (`init -reconfigure` with the original key). Rule, per `resource_changes[]`:

- every `create` claim address must have `importing` set, and `actions == ["no-op"]`, or `actions == ["update"]` with empty `replace_paths` and every differing attribute in `virtual_attributes[type]` (write-only attributes such as `aws_lambda_function.filename`/`source_code_hash`, `force_destroy`, `deletion_window_in_days`, `skip_final_snapshot`, `helm_release.values`) — otherwise it is a hard failure with the offenders listed;
- addresses under an `update` claim may show `update` (that is the branch's change);
- any other `update` → failure unless `--allow-import-updates` (then a warning);
- any `delete`, replace, `forget` → failure;
- `replace_prone` types (e.g. `aws_lambda_layer_version`) and `non_importable` types are refused up-front (§6 `merge`).

`import { to = ADDRESS  id = "IMPORT_ID" }` blocks use `import_ids.yaml` formats (identity-based imports are a later option). Types absent from the file default to `{id}` with a warning and strict no-op verification.

## 9. Type knowledge

`data/identity.yaml`: `type → [attribute paths]` (e.g. `aws_route53_record: [zone_id, name, type, set_identifier]`, `kubernetes_namespace: [metadata.0.name]`). Fallback: first present among `name, bucket, identifier, function_name, domain_name, cluster_identifier, cluster_id, replication_group_id, key`. Values are normalised (Route53 names lower-cased without trailing dot).

`data/import_ids.yaml`: `formats: type → "{attr}/…"`, `non_importable: […]`, `replace_prone: […]`, `virtual_attributes: type → [attrs]`, `ignored_attributes: type glob → [attrs]` (§7.7). Users extend both with `.tofu-overlay.yaml` (`identity:`, `import_ids:`, `virtual_attributes:`, `ignored_attributes:`, `policy:`).

## 10. Configuration file `.tofu-overlay.yaml` (repo root, found by walking up)

```yaml
policy:
  allowed_base_keys: ["acme/webshop/*/dev", "acme/*/sandbox"]
  trunk_branch: main
  env_dir_glob: "stacks/*/env/*"
  tombstone_days: 14
  apply_timeout_min: 90
  max_overlay_age_days: 30
binary: tofu
identity: {}
import_ids: {}
virtual_attributes: {}
ignored_attributes: {}     # type glob -> [environment-dependent attrs], §7.7
```

## 11. Deferred (documented, not implemented)

Tracked in [ROADMAP.md](ROADMAP.md), the single list of deferred work:

- other state backends (`azurerm`, `gcs`, `http`, `local`) on top of the `store.StateStore` contract;
- `merge --strategy state` (direct injection into the base under the tofu lock protocol), with lineage, provider address and schema-version checks. See LIMITS.md;
- `--destructive exclusive` (single-overlay replacement of base resources);
- stacked overlays (`create --base-overlay`), see SYNC-PROPOSAL.md;
- `registry repair`, DynamoDB registry backend, identity-based `import` blocks, non-default workspaces, state encryption surgery.

## 12. Repository layout

```
src/tofu_overlay/
  __init__.py     version
  cli.py          typer app, exit-code mapping
  config.py       .tofu-overlay.yaml, CI detection, overlay naming, git helpers
  trunk.py        trunk baseline helpers: shared-clone export of the trunk, env dir mapping, per-sha cache
  backend.py      backend resolution (type reported, unsupported types refused), workspace refusal
  store.py        StateStore protocol, CasConflict, make_store(cfg) factory on backend_type
  s3state.py      s3 StateStore: boto3 session, HEAD/list/copy/delete, registry JSON CAS, DynamoDB digest/lock items
  registry.py     Registry document, update(fn), status machine, claims
  tofu.py         runner: init/plan/show/apply/state pull|push|rm, arg validation, streaming
  state.py        state document helpers: addresses, instances, inject/remove, lineage/serial, identity
  identity.py     type knowledge loader (YAML + overrides), identity/import-id/virtual attrs
  plan.py         plan JSON analysis and policy checks (ignored attributes, trunk drift)
  overlay.py      create/plan/apply/status/check/rebase/abandon/finalize/gc/doctor
  merge.py        imports file generation, verify, undo, guard
  output.py       console/CI/JSON output, confirmations
  models.py       pydantic models, enums, exit codes
  data/identity.yaml  data/import_ids.yaml
tests/            pytest + moto; fixtures/ (plan/state JSON)
docs/DESIGN.md LIMITS.md CI.md MULTI-STACK.md SYNC-PROPOSAL.md ROADMAP.md
```
