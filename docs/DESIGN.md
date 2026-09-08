# tofu-overlay — design

Status: draft v0 (2026-09-08). Target: OpenTofu >= 1.7 (import/removed blocks), Terraform >= 1.5 best effort.

## 1. Problem

Several teams work in parallel on the same OpenTofu root modules ("stacks"), each stack having **one state per environment** (S3 + DynamoDB lock). The integration environment is a shared sandbox. Today the only way to test a branch on the sandbox is to run `apply` from that branch against the shared state, which:

- writes the branch's resources into the shared state, so the next apply from the trunk destroys them;
- lets two branches revert each other's changes on shared resources;
- gives no visibility on "who is currently changing what" in the sandbox.

Trunk-based development with per-environment gating fixes the promotion problem, but not the "I want to apply my branch on the sandbox before merging" problem.

## 2. Idea

An **overlay** is a copy-on-write fork of a base state, bound to a git branch and a name:

- `create` copies the base state object (`<key>`) to `<key>@<overlay>` in the same bucket. Serial and lineage are preserved, so the overlay plans only the branch's delta.
- `plan`/`apply` run the regular OpenTofu binary against the overlay state (backend `key` override + separate `TF_DATA_DIR`), after **policy checks** computed from the plan JSON and a shared **registry**.
- The registry records, per base state, every active overlay and its **claims**: resources it creates (address + planned/actual physical identity) and resources it updates.
- `merge` hands the overlay's created resources to the base state, by default through generated `import` blocks committed with the branch (visible and reviewable in the PR), alternatively by direct state surgery under the DynamoDB lock.
- `abandon` destroys only the overlay's delta and drops the overlay.

Scope is deliberately **additive**: an overlay may *create* resources and, with an exclusive claim, *update* base resources. It may not destroy or replace base resources (policy `destructive: deny`, overridable to `exclusive` which requires that no other overlay exists on the stack).

The cloud has no branches. Two overlays can only run in parallel when their cloud changes are disjoint at **resource granularity** (update APIs take the whole object). The registry + claims enforce that.

## 3. Non-goals

- Attribute-level concurrency on a single resource (CloudFront distribution, WAF ACL, node group...). Those serialize.
- Protecting overlay *updates* from a base apply: the trunk's config does not know the overlay, so a base apply reverts the overlay's updates in the cloud. `check` detects this drift and asks for a rebase.
- Managing environments other than the base (no promotion logic).
- Replacing state locking: OpenTofu's own DynamoDB lock still applies to each state key.

## 4. Vocabulary

- **base**: the trunk state, `bucket/key` (e.g. `acme/webshop/storage/dev`).
- **overlay**: `bucket/key@<name>`; `<name>` = `[a-z0-9][a-z0-9-]{0,40}` (derived from the branch by default: `feature/ABC-12-foo` → `abc-12-foo`).
- **registry**: JSON document `bucket/<key>.overlays.json` (sibling of the base state), updated with S3 conditional writes (`IfMatch` ETag, `IfNoneMatch: *` on creation). No new infrastructure. Optional DynamoDB registry backend later.
- **claim**: an entry in the registry that reserves a resource address (and its physical identity) for one overlay. Kinds: `create`, `update`. `destructive` is only allowed with an exclusive stack claim.
- **freshness**: an overlay records the base `serial` (and ETag) it was copied from. If the base moved, the overlay is *stale*: `plan` warns, `apply` refuses (unless `--allow-stale`, never in CI).

## 5. Registry document

```json
{
  "version": 1,
  "base": {"bucket": "acme-tfstate", "key": "acme/webshop/storage/dev"},
  "overlays": {
    "abc-12-foo": {
      "state_key": "acme/webshop/storage/dev@abc-12-foo",
      "branch": "feature/ABC-12-foo",
      "owner": "jean@example.com",
      "created_at": "2026-09-08T09:00:00Z",
      "updated_at": "2026-09-08T09:30:00Z",
      "base_serial": 412,
      "base_etag": "\"9c1...\"",
      "status": "active",             // active | dirty | merging | merged | abandoned
      "exclusive": false,
      "claims": {
        "aws_s3_bucket.reports": {
          "kind": "create",
          "type": "aws_s3_bucket",
          "identity": {"bucket": "s3-acme-dev-reports"},
          "id": "s3-acme-dev-reports",          // filled after apply
          "import_id": "s3-acme-dev-reports",   // filled after apply
          "claimed_at": "2026-09-08T09:10:00Z"
        },
        "aws_iam_role.reports": {"kind": "update", "type": "aws_iam_role", "identity": {"name": "iam-acme-dev-reports"}, "claimed_at": "..."}
      },
      "last_plan": {"at": "...", "summary": {"create": 3, "update": 1}, "stale": false}
    }
  }
}
```

Registry writes go through `Registry.update(fn)` = get (ETag) → mutate → put with `IfMatch` → retry on 412 with backoff (bounded). Every conflict check runs **inside** that critical section when it leads to a write (claim acquisition), so two overlays cannot both acquire the same claim.

## 6. Commands

All commands run from the stack's env directory (where the `backend "s3"` block lives) and discover the backend from the HCL (`state.tf`/`backend.tf`), from `-backend-config` files, or from flags/env (`TOFU_OVERLAY_BUCKET`, `_KEY`, `_REGION`, `_PROFILE`, `_DYNAMODB_TABLE`).

| Command | What it does |
|---|---|
| `create [NAME] [--branch B] [--base-overlay P]` | Copy base → overlay key (server-side `CopyObject`), register overlay (status `active`), write local `.tofu-overlay/current` with the name. Refuses if an active overlay with that name exists. `--base-overlay` = stacked overlay (see §10), recorded as `parent`. |
| `plan [-- tofu args]` | `init -reconfigure -backend-config=key=<overlay>` in `TF_DATA_DIR=.tofu-overlay/<name>`, `plan -out -detailed-exitcode`, `show -json` → **policy checks** (§7). Prints the plan summary and the verdict. Exit 0 = ok, 2 = changes, 3 = policy violation, 4 = stale. |
| `apply [--auto-approve]` | Re-run checks, **acquire claims atomically** in the registry, `apply tfplan`, then pull the overlay state and fill `id`/`import_id`/actual identity for every `create` claim. On apply failure: status `dirty`, claims kept. |
| `status` / `list` | Show overlays of this base: owner, branch, freshness (base serial vs current), claims, status. `--json`. |
| `check` | Non-mutating CI gate: overlay fresh? claims still valid vs base (no base resource with the same address/identity appeared)? no conflicting overlay? Exit non-zero on problems. Meant for PR build validation. |
| `rebase` | Base moved: re-copy base → overlay key, re-inject the overlay's `create` claims (instances copied from the previous overlay state, or import blocks) into the fresh copy, bump serial, push. Then `plan` must show only the overlay's `update` claims (or nothing). Conflict if the new base contains an address or identity claimed by the overlay. |
| `merge [--strategy import\|state] [--out FILE]` | `import` (default): write `overlay_<name>_imports.tf` in the env dir with one `import {}` block per `create` claim (import id from the mapping in §8), then **verify** with a plan against the *base* state (`-refresh=false` off, real refresh on): every import must be `importing` with no other change; otherwise print the offenders and fall back suggestion. Marks the overlay `merging`. `state`: pull base under a manual DynamoDB lock, insert the overlay's `create` instances (address-disjoint check), bump serial, push, mark `merged`. |
| `finalize` | After the trunk applied the imports: verify base contains every claimed address, delete overlay key, mark `merged`, optionally delete the imports file. |
| `abandon [--keep-resources]` | `destroy -target=<addr>` for every `create` claim in dependency order (tofu handles ordering), release claims, delete overlay key, mark `abandoned`. `update` claims are released without action; the next base apply reverts them (documented). |
| `gc` | Remove overlays whose branch no longer exists on the remote (`--dry-run` default). |

`TF_VAR_tofu_overlay_name` is exported to every tofu run so configs may make cross-stack `terraform_remote_state` keys overlay-aware if they wish.

## 7. Policy checks (from `tofu show -json tfplan`)

For each `resource_changes[]` entry (address, type, `change.actions`, `change.before/after`, `after_unknown`):

1. **Freshness**: current base serial == recorded `base_serial`, else *stale*.
2. **Additive policy**:
   - `["create"]` → allowed. Claim `create`.
   - `["update"]` on an address present in the base state → allowed only with an exclusive `update` claim. Address absent from base (i.e. created earlier by this overlay) → allowed, no new claim.
   - `["delete"]`, `["delete","create"]`, `["create","delete"]` on a base address → **denied** by default. With `--destructive exclusive`: allowed only if this overlay is the *only* active overlay on the stack and it takes the `exclusive` flag. Replacement of a resource created by this overlay → allowed.
   - `["no-op"]`, `["read"]` → ignored.
3. **Address conflict**: another active overlay claims the same address (any kind) → denied.
4. **Identity conflict**: another active overlay's `create` claim has the same physical identity (per-type identity attributes, §8) → denied even if the address differs. Unknown identity (`after_unknown`) → address check only, warning.
5. **Base identity conflict**: a `create` whose identity already exists in the base state → denied (would fail at apply with AlreadyExists, or worse, silently adopt).
6. **Drift on own updates**: for each `update` claim, if the plan shows the resource being changed *back* (before != what the overlay applied last) the base likely reverted it → warning "rebase required".

Checks 3-5 are recomputed under the registry critical section at claim time.

## 8. Type knowledge (data-driven, YAML, user-extensible)

`identity.yaml`: `type → [attributes]` used as the physical identity (e.g. `aws_s3_bucket: [bucket]`, `aws_route53_record: [zone_id, name, type, set_identifier]`, `aws_iam_role_policy_attachment: [role, policy_arn]`, `kubernetes_namespace: [metadata.0.name]`). Generic fallback: first present among `name, bucket, identifier, function_name, domain_name, cluster_identifier`.

`import_ids.yaml`: `type → format` used to build the import id from state attributes (default `{id}`; e.g. `aws_iam_role_policy_attachment: "{role}/{policy_arn}"`, `aws_route53_record: "{zone_id}_{name}_{type}"`, `aws_lambda_permission: "{function_name}/{statement_id}"`, `aws_iam_role_policy: "{role}:{name}"`, `aws_scheduler_schedule: "{group_name}/{name}"`, `kubernetes_service_account_v1: "{metadata.0.namespace}/{metadata.0.name}"`, `helm_release: "{namespace}/{name}"`), plus `non_importable: [random_*, null_resource, terraform_data, time_*, tls_private_key, local_file, archive_file, aws_acm_certificate_validation, aws_lambda_invocation]` which force the `state` strategy for those addresses (mixed strategy allowed: imports for the importable, state injection for the rest).

Users extend/override with `.tofu-overlay.yaml` at repo root (`identity:`, `import_ids:`, `policy:`).

## 9. Safety rails

- Never touches the base state except in `merge --strategy state` and `finalize`, both under a manual DynamoDB lock item (`LockID = bucket/key`, same format as OpenTofu's, so a concurrent `tofu` run sees the lock).
- Every mutating command prints what it is about to do and asks for confirmation unless `--yes`/CI.
- `apply` refuses stale overlays; `check` is idempotent and read-only.
- Overlay state keys are namespaced with `@`, which cannot collide with the repo's existing keys, and are enumerable (`list` uses the registry, not S3 listing).
- The tool never stores credentials; it uses the same AWS profile/OIDC as tofu.

## 10. Bonus (design only): syncing overlays

Overlay B may depend on something overlay A created. Injecting A's resources into B's *state* is wrong: B's config does not declare them, so B would plan their destruction. Two sound options:

1. **Data sources**: B reads A's resources by identity (`data "aws_s3_bucket"`). Works today, needs A applied first.
2. **Stacked overlays**: `create B --base-overlay A` copies A's state instead of the base and records `parent: A`. B's freshness tracks A's serial. When A merges, B rebases onto the base. This mirrors stacked PRs and keeps every state with exactly one source of truth. Rebase across a chain is the hard part (A merged → B's `create` claims re-injected on top of the new base, A's claims now in base).

Proposal: implement (2) as `create --base-overlay` + `rebase` awareness, behind an `experimental` flag, after v1.

## 11. CI integration (Azure DevOps / any)

- PR build validation: `tofu-overlay check` + `tofu-overlay plan` (exit 3/4 fail the build) on the branch's overlay.
- Trunk pipeline (after merge): unchanged — the imports file rides with the code; the trunk plan shows N imports; after apply, a scheduled or manual `tofu-overlay finalize` cleans up.
- The overlay's apply is run by the developer (or a manually triggered pipeline on the branch) with the environment's approval as usual.

## 12. Repository layout

```
tofu-overlay/
  pyproject.toml            # hatchling, package kmr-tofu-overlay, CLI tofu-overlay
  src/tofu_overlay/
    __init__.py  cli.py  config.py  backend.py  s3state.py  lock.py
    registry.py  models.py  tofu.py  plan.py  identity.py  imports.py
    overlay.py  merge.py  output.py
    data/identity.yaml  data/import_ids.yaml
  tests/            # pytest + moto (S3/DynamoDB), fixtures: plan JSON, state JSON, registry
  docs/DESIGN.md  docs/LIMITS.md  docs/SYNC-PROPOSAL.md  docs/CI.md
  .github/workflows/ci.yml   # ruff + pytest
  README.md  LICENSE (MIT)  CHANGELOG.md
```

Dependencies: `boto3`, `python-hcl2`, `pydantic`, `typer`, `rich`, `pyyaml`. Dev: `pytest`, `moto[s3,dynamodb]`, `ruff`.
