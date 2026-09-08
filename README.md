# tofu-overlay

Copy-on-write state overlays for OpenTofu/Terraform: apply a git branch on a
shared environment without writing into the trunk state, with cross-overlay
conflict detection and resource claims.

Status: **v1 (alpha)**. Target: OpenTofu >= 1.7 with the `s3` backend
(DynamoDB lock and/or `use_lockfile`). Terraform >= 1.5 is best effort.
Full design: [docs/DESIGN.md](docs/DESIGN.md). Hard limits:
[docs/LIMITS.md](docs/LIMITS.md). CI wiring: [docs/CI.md](docs/CI.md).
Multi-stack branches: [docs/MULTI-STACK.md](docs/MULTI-STACK.md).
Deferred work and other backends: [docs/ROADMAP.md](docs/ROADMAP.md).

## The problem: one sandbox, many branches

Several teams work in parallel on the same root modules ("stacks"). Each
stack has one state per environment, stored in S3 with a DynamoDB lock. The
integration environment is a shared sandbox.

Today the only way to test a branch on that sandbox is to run `apply` from
the branch against the shared state. That has three well-known consequences:

- the branch's resources land in the shared state, and the next trunk apply
  (which does not know them) destroys them;
- two branches touching the same resource silently revert each other;
- nobody can tell who is changing what in the sandbox.

`tofu-overlay` keeps the shared state untouched and gives every branch its
own forked state, while a small shared registry makes sure two branches never
create the same resource or update the same base resource at the same time.

## How it works

An **overlay** is a copy-on-write fork of a base state, bound to a git
branch. Three mechanisms cooperate:

1. **Fork.** `tofu-overlay create` pulls the base state, gives the copy a new
   lineage and serial 0, and pushes it to `<key>@<name>` in the same bucket.
   From then on `plan`/`apply` run the regular `tofu` binary against that
   overlay key (in a dedicated `TF_DATA_DIR`), so the plan only shows the
   branch's delta. The base state is never written by the tool.

2. **Claims.** A registry document (`<key>.overlays.json`, one per base
   state, written with S3 conditional requests) records every overlay and its
   claims: resources it *creates* (address + physical identity) and base
   resources it *updates* (exclusive). Every `plan`/`apply` is gated by policy
   checks computed from the plan JSON: additive changes only, no destroy /
   replace / move / import of base resources, no address or identity already
   claimed by another live overlay, no identity already present in the base.
   Claims live until the overlay is finalized or abandoned.

3. **Imports.** `tofu-overlay merge` hands the overlay's created resources to
   the trunk through a generated `zz_overlay_<name>.imports.tf` file
   (`import {}` blocks) committed with the branch. The file is verified with a
   plan of the branch config against the base state: every created resource
   must import as a no-op. Once the trunk pipeline has applied the imports,
   `finalize` archives the overlay state and releases the claims. `abandon`
   destroys only the overlay's own resources.

The cloud has no branches: two overlays can run in parallel only when their
cloud changes are disjoint at resource granularity. The registry enforces
exactly that, nothing more.

## Install

- Linux CI agent (no Python needed): `curl -fsSL https://raw.githubusercontent.com/KamorionLabs/tofu-overlay/main/scripts/install.sh | sh -s -- 0.1.1`
- With uv: `uv tool install git+https://github.com/KamorionLabs/tofu-overlay@v0.1.1` (or `uv tool install kmr-tofu-overlay` once published on PyPI)
- With pipx: `pipx install kmr-tofu-overlay`

Every release ships a wheel, an sdist, a `tofu-overlay-linux-amd64` binary and `SHA256SUMS`. See [docs/RELEASING.md](docs/RELEASING.md).

## Quick start

```bash
pip install kmr-tofu-overlay        # provides the `tofu-overlay` command

# Once per repository: allow-list the base keys that may be forked.
cat > .tofu-overlay.yaml <<'EOF'
policy:
  allowed_base_keys: ["acme/webshop/*/dev"]
  trunk_branch: main
  env_dir_glob: "stacks/*/env/*"
EOF
echo ".tofu-overlay/" >> .gitignore

# On a feature branch, from the stack's env directory:
cd stacks/storage/env/dev
git switch -c feature/ABC-12-reports

tofu-overlay create           # fork the base state into <key>@abc-12-reports-3f9a1c
tofu-overlay plan             # regular plan + policy checks, read-only
tofu-overlay apply            # acquire claims, apply on the overlay state
tofu-overlay status           # who holds what on this base

# When the branch is ready:
tofu-overlay merge            # writes zz_overlay_<name>.imports.tf, verifies it
git add zz_overlay_*.imports.tf && git commit -m "feat: reports bucket" && git push
# ... PR review, trunk pipeline applies the imports ...
tofu-overlay finalize         # archive the overlay state, release claims
git rm zz_overlay_*.imports.tf && git commit -m "chore: drop overlay imports"

# Or, to throw the branch away:
tofu-overlay abandon          # destroys only the overlay's own resources
```

The overlay name is derived from the branch on every run
(`<slug(branch)[:34]>-<sha1(branch)[:6]>`); there is no "current overlay"
file. Use `--name` or `TOFU_OVERLAY_NAME` to override.

## Commands

All commands run from the stack's env directory (`-C/--chdir` accepted) and
start by echoing the resolved backend (`s3://bucket/key`, region, profile,
lock table). Read-only commands never write anything, registry included.

| Command | Writes | Effect |
|---|---|---|
| `create [--name N] [--force-name]` | overlay key, registry | Fork the base state into `<key>@<name>`, register the overlay. Refuses a tombstoned name, an existing `<key>@<name>` object outside the registry, a missing base. Re-running resumes a `creating` entry. |
| `plan [--json] [--detailed-exitcode] [-- tofu args]` | none | Validate the overlay, freshness and git ancestry, `tofu plan` against the overlay state, `show -json`, policy checks. In status `merging`: verify mode. |
| `apply [--auto-approve] [--allow-stale]` | overlay key, registry | `plan`, then acquire claims atomically (registry CAS), `tofu apply`, refresh claim ids from the overlay state. Refuses stale overlays. |
| `status [--json] [--repo]` | none | Overlays of this base (or of every base under `env_dir_glob`): owners, branch, freshness, status, claims, age, pending reverts. |
| `list [--json]` | none | Same data as `status`, one line per overlay. `--bucket B --prefix P` scans registries without a checkout. |
| `check [--json] [--repo]` | none | CI gate: overlay exists for the branch (else "no overlay", exit 0), fresh, branch contains trunk, claims match the overlay state, no conflicting overlay, imports file matches the claims. |
| `rebase` | overlay key, registry | Re-fork on the current base while keeping the overlay's own resources. Typed confirmation; the previous overlay state is archived. |
| `merge [--undo] [--allow-import-updates] [--accept-recreate ADDR,...] [--allow-unapplied]` | imports file, registry | Write and verify `zz_overlay_<name>.imports.tf`, status `merging`. `--undo` deletes the file and returns to `active`. |
| `finalize [--purge]` | S3 archive/delete, DynamoDB, registry | After the trunk applied the imports: verify every created resource is in the base with the same id, archive the overlay key, release claims. Prints the `git rm` to run. |
| `abandon [--keep-resources] [--dry-run]` | overlay key, S3 archive/delete, DynamoDB, registry | Destroy the overlay's own resources (base resources are removed from the overlay state first so they can never be destroyed), archive, release claims, record pending reverts for `update` claims. |
| `doctor` | none | Report orphan overlay objects, missing keys, orphan `-md5` items and `.tflock`s, stale `applying`, tombstones, git-ignore, binary version, leftover imports files. |
| `guard PLAN_JSON` | none | For the trunk pipeline: fail if the trunk plan creates an identity claimed by an overlay, deletes/replaces an address under an `update` claim, or deletes an address an overlay's resources depend on. |
| `gc [--purge]` | S3 archives (with `--purge`) | Report overlays whose branch no longer exists on the remote, and archives. `--purge` deletes archive objects only. Never destroys cloud resources. |
| `version` | none | Print the tool version. |

### Exit codes

| Code | Meaning |
|---|---|
| 0 | OK. Changes are reported, not signalled. |
| 1 | Tool or tofu error. |
| 2 | `plan --detailed-exitcode` only: changes present. |
| 3 | Policy violation (denied action, conflicting claim, identity already in the base). |
| 4 | Overlay stale (base moved) or branch behind trunk. |
| 5 | Registry conflict, unreachable or invalid. |
| 6 | Base key not allowed by `policy.allowed_base_keys`, or overlay not found. |
| 7 | Overlay frozen (status `merging`). |

Global options: `-C/--chdir`, `--name`, `--backend-config FILE` (repeatable,
the same files the pipeline uses), `--bucket/--key/--region/--profile/--dynamodb-table`,
`--json`, `--no-color`, `--yes`, `--print-backend`, `-v/--verbose`.

Pass-through arguments after `--` on `plan` are forwarded to tofu, except
`-target`, `-replace`, `-refresh-only`, `-destroy`, `-state` and `-lock=false`,
which are rejected.

## Configuration

### Backend resolution

The backend is resolved in this order; the first complete answer wins:

1. flags / environment (`TOFU_OVERLAY_BUCKET`, `_KEY`, `_REGION`, `_PROFILE`,
   `_DYNAMODB_TABLE`);
2. `--backend-config FILE` (repeatable; key=value or HCL, as tofu accepts);
3. the cached backend in `.terraform/terraform.tfstate`;
4. the `backend "s3" {}` block in the directory's `*.tf` files.

Unresolved values (`${...}`), a backend other than `s3` (`backend 'azurerm'
is not supported yet, see docs/ROADMAP.md`) or a non-default workspace
(`TF_WORKSPACE`, `.terraform/environment`) are refused with a clear message.
`--print-backend` prints the resolved tuple and exits.

### `.tofu-overlay.yaml`

Found at the repository root by walking up from the current directory. See
[.tofu-overlay.example.yaml](.tofu-overlay.example.yaml) for a commented
example.

```yaml
policy:
  allowed_base_keys: ["acme/webshop/*/dev", "acme/*/sandbox"]   # globs, default deny
  trunk_branch: main
  env_dir_glob: "stacks/*/env/*"
  tombstone_days: 14
  apply_timeout_min: 90
  max_overlay_age_days: 30
binary: tofu               # or terraform; TOFU_OVERLAY_BINARY overrides
identity: {}               # type -> [attribute paths], extends data/identity.yaml
import_ids: {}             # type -> "{attr}/..." import id format
virtual_attributes: {}     # type -> [write-only attrs ignored at merge verify]
```

### Environment variables

| Variable | Effect |
|---|---|
| `TOFU_OVERLAY_NAME` | Overlay name (overrides the branch-derived name). |
| `TOFU_OVERLAY_BINARY` | `tofu` (default) or `terraform`. |
| `TOFU_OVERLAY_BUCKET`, `_KEY`, `_REGION`, `_PROFILE`, `_DYNAMODB_TABLE` | Backend overrides. |
| `TF_PLUGIN_CACHE_DIR` | Provider cache; defaults to `~/.cache/tofu-overlay/plugins`. |
| `CI=true` / `TF_BUILD=True` | CI mode: no colour, ADO log issues, `--allow-stale`/`--allow-behind` refused, `--yes` accepted for `apply`. |

`TF_VAR_tofu_overlay_name` is exported to every tofu run; declare a
`tofu_overlay_name` variable if the config wants to know it (for example to
suffix a physical name), otherwise it is ignored. `TF_VAR_tofu_overlay_keys`
is exported alongside it for cross-stack reads, see below.

## Multi-stack branches

A branch that adds an output in stack A and reads it in stack B through
`terraform_remote_state` would normally see A's **base** in B. When B's
configuration adopts a small contract:

```hcl
variable "tofu_overlay_keys" {
  type    = map(string)
  default = {}
}

data "terraform_remote_state" "eks" {
  backend = "s3"
  config = {
    bucket = "acme-tfstate"
    key    = lookup(var.tofu_overlay_keys, "acme/webshop/eks/dev", "acme/webshop/eks/dev")
    region = "eu-west-1"
  }
}
```

then B's `plan`/`apply` read A's overlay whenever A's base holds a live
overlay of the same name (same branch), and fall back to the base otherwise;
trunk pipelines never set the variable and are unaffected. Apply the producer
first, finalize it first; `check` and `finalize` help with the ordering.
Details, limits and the alternative by identity: [docs/MULTI-STACK.md](docs/MULTI-STACK.md).

## Safety invariants

These are the properties the tool is built around; see DESIGN.md section 3.

1. **State objects are only written by OpenTofu.** Every read is
   `tofu state pull`, every write is `tofu state push` or `apply`, in a
   dedicated `TF_DATA_DIR`. Lock protocol, `-md5` digest items, lineage/serial
   checks and state encryption stay consistent. Raw S3 calls on state keys are
   limited to `HEAD`, `ListObjects`, `CopyObject` to an archive key and
   `DeleteObject` at finalize/abandon.
2. **The base state is never written by the tool.** The trunk adopts overlay
   resources through `import {}` blocks applied by the trunk pipeline.
3. **The registry stores intention, the overlay state stores reality.** Ids
   and identities are recomputed from the overlay state whenever they matter.
4. **Claims live until `finalize` or `abandon`.** Every live status counts in
   conflict checks.
5. **`plan`, `check`, `status`, `list`, `doctor`, `guard`, `gc` never write
   anything.** Mutating commands print the resolved backend and what they are
   about to do; destructive ones require a typed confirmation.
6. **Default deny on base keys.** Overlays are only allowed on keys matching
   `policy.allowed_base_keys`.
7. **Every tofu run validates the overlay first**: registry entry with a live
   status, overlay object present (a missing key is never treated as an empty
   state), lineage of the pulled overlay equals the registry's, current branch
   equals the overlay's branch (unless `--name` was given).
8. **Freshness is an ETag comparison** between the base object and the
   overlay's recorded `base_etag`. `apply` refuses stale overlays; `plan`
   warns. The branch must also contain the trunk.
9. **Additive scope only.** No destroy, replace, move, forget or import of
   base resources; `-target`, `-replace`, `-refresh-only`, `-destroy`,
   `-state`, `-lock=false` are rejected.

## Status of v1

Implemented in v1:

- `s3` backend, default workspace, DynamoDB lock and/or `use_lockfile`;
- fork / plan / apply with policy checks and atomic claims;
- merge by `import {}` blocks with verification against the base state;
- rebase (own created resources re-injected on a fresh base copy);
- abandon, finalize, doctor, guard, gc, status/list/check with `--json`;
- overlay-aware cross-stack `terraform_remote_state` reads
  ([docs/MULTI-STACK.md](docs/MULTI-STACK.md));
- CI mode (generic and Azure DevOps).

Deferred, tracked in [docs/ROADMAP.md](docs/ROADMAP.md) (details in
[docs/LIMITS.md](docs/LIMITS.md) and [docs/SYNC-PROPOSAL.md](docs/SYNC-PROPOSAL.md)):

- other backends (`azurerm`, `gcs`, `http`, `local`): the store layer is
  backend-agnostic (`store.StateStore`, `store.make_store`), only `s3` is
  implemented;
- `merge --strategy state` (direct state injection);
- `--destructive exclusive` (replacement of base resources by a single overlay);
- stacked overlays (`create --base-overlay`);
- `registry repair`, DynamoDB registry backend, identity-based imports,
  non-default workspaces, state encryption surgery.

## Development

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/ruff check src tests
.venv/bin/pytest -q
```

Tests use `moto` for S3/DynamoDB and a fake runner for tofu; no cloud
credentials are needed.

## License

MIT, see [LICENSE](LICENSE).
