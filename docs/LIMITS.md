# Hard limits (v1)

This file lists what `tofu-overlay` v1 does **not** do, why, and what would be
needed to lift each limit. Every item is either detected and refused by the
tool, or documented here because the tool cannot detect it. Read it before
adopting the tool on a stack.

## 1. Conflicts are checked at resource granularity

The registry prevents two overlays from creating the same resource
(address or physical identity) or updating the same base resource. It says
nothing about *attribute*-level conflicts inside a resource, nor about
semantic coupling between distinct resources (an IAM policy attached to a
role created by another overlay, a security group rule referencing a group
another overlay is about to change, an alias whose target another overlay
recreates).

Consequence: two overlays with disjoint resource sets can still produce a
broken sandbox. The tool guarantees the *states* stay consistent, not that
the *cloud* makes sense. Reviewers and `status` remain the place to spot
coupled changes.

## 2. A trunk apply reverts overlay updates

An overlay may update a base resource under an exclusive `update` claim. The
trunk configuration does not know that update; the next trunk `apply` writes
the trunk's view back to the cloud. The overlay is not protected: the tool
detects the situation afterwards (drift on the address recorded at apply as
`after_hash`, and a base ETag change) and asks for a `rebase`.

`guard` gives the trunk pipeline a signal only for destructive actions
(delete/replace of an address under an `update` claim); it does not block a
trunk update on the same address, because the trunk's update is legitimate.

Mitigation: keep `update` claims short-lived, and prefer creating new
resources over updating shared ones in a branch.

## 3. Singleton resources serialize overlays

Some resources exist at most once per account, region or parent: an S3
bucket policy, a public access block, an account-level setting, a
`kubernetes_namespace` by name, a `helm_release` in a namespace, a Route53
record by (zone, name, type). Two overlays needing the same singleton cannot
run in parallel: the second is refused with an identity conflict (exit 3)
until the first is finalized or abandoned.

This is by design (the cloud has no branches). It is also why physical
names must be deterministic (see 9): a random suffix would hide the
singleton conflict instead of surfacing it.

## 4. `s3` backend only

The tool understands the `s3` backend and its lock protocol (DynamoDB lock
and `-md5` digest items, or `use_lockfile` `.tflock` objects). Anything else
(`azurerm`, `gcs`, `http`, `local`, `remote`, `cloud`) is detected during
backend resolution and refused with the backend type in the message.

The architecture is backend-agnostic: everything above the
`store.StateStore` contract (registry, claims, policy, merge, CLI) never
touches S3, and `store.make_store` is the only place that picks an
implementation. Supporting another backend means implementing that contract
(head, list, copy, delete, conditional JSON writes, digest and lock
bookkeeping) and, when the object layout differs, its key builders. What
each candidate backend needs is detailed in [ROADMAP.md](ROADMAP.md).

## 5. Default workspace only

Overlay keys are derived from the base key (`<key>@<name>`). Non-default
workspaces change the object path (`env:/<workspace>/<key>`) and are not
modelled. `TF_WORKSPACE` set to anything but `default`, or a
`.terraform/environment` file naming another workspace, is refused during
backend resolution.

## 6. No state encryption surgery

OpenTofu state encryption produces documents where the payload is under
`encrypted_data`. The tool never reads or writes such documents directly:
every state read is `tofu state pull` (which decrypts) and every write is
`tofu state push` (which encrypts) with the stack's own encryption
configuration. `rebase` builds the new document from two *pulled* (clear)
documents and pushes it; it never merges ciphertext.

What is not done: changing encryption settings, key rotation, or reading a
state whose encryption configuration is not available in the working
directory. `is_encrypted()` on a raw document is only used by `doctor` to
report it.

## 7. Cross-stack `terraform_remote_state` follows the overlay only under a contract

A stack that reads another stack's outputs with `terraform_remote_state`
points at the other stack's **base** key. The tool does not rewrite that key:
it exports `TF_VAR_tofu_overlay_keys` (`base key -> overlay key` for every
`s3` reference whose base holds a live overlay of the same name, i.e. the
same branch) and the configuration must opt in with
`key = lookup(var.tofu_overlay_keys, "<base key>", "<base key>")`. Trunk
pipelines never set the variable and keep reading the base. See
[MULTI-STACK.md](MULTI-STACK.md).

Caveats:

- a stack that does not adopt the contract keeps reading the base and sees
  the producer's new outputs only after the producer is merged and applied
  by the trunk;
- a reference whose key or bucket is an expression the tool cannot evaluate
  is reported and cannot be mapped;
- the producer overlay must be applied before the consumer plans, and
  finalized before the consumer (the consumer's next plan falls back to the
  base automatically; `finalize` on the producer warns, best effort, about
  live consumer overlays of the repository);
- only `terraform_remote_state` with the `s3` backend and the default
  workspace is understood; data sources by identity need nothing.

Stacked overlays on the *same* stack (`create --base-overlay`) remain deferred,
see [SYNC-PROPOSAL.md](SYNC-PROPOSAL.md).

## 8. Non-importable resource types cannot be merged

`merge` hands created resources to the trunk through `import {}` blocks. A
resource whose provider has no importer, or whose import is not
round-trippable, cannot be adopted:

- `random_*`, `null_resource`, `terraform_data`, `time_sleep`,
  `tls_private_key`, `local_file`, `archive_file`, `aws_lambda_invocation`,
  `aws_iam_policy_attachment`, `aws_lb_target_group_attachment`,
  `aws_dynamodb_table_item`, `aws_iam_access_key`,
  `aws_acm_certificate_validation`, `kubernetes_manifest` (in v1);
- `replace_prone` types (`aws_lambda_layer_version`, `aws_security_group_rule`
  in its multi-value form) are refused up-front because an import followed by
  a plan typically shows a replacement.

`merge` lists them and refuses, unless the address is passed with
`--accept-recreate ADDR,...`: the resource is then left out of the imports
file and the trunk will create its own copy (the overlay's copy is destroyed
at `abandon`, or orphaned with `--keep-resources`). For `random_*` and
`terraform_data` that recreation is usually harmless; for anything with a
physical footprint, review carefully.

Both lists are extendable in `.tofu-overlay.yaml` (top-level
`non_importable:` and `replace_prone:` keys, added to the packaged lists).

## 9. Physical names must be deterministic

Identity conflicts (checks 4 and 5 of the policy) compare the physical
identity computed from the plan's `after` values. A name that is unknown at
plan time (`(known after apply)`, random suffix, `name_prefix`) yields an
address-only claim with a warning: the tool cannot tell whether two overlays
or the base already hold that object, and `merge` verification has to rely
on the id read back from the overlay state.

Recommended: derive names from stable inputs (`var.env`, the module path,
optionally `var.tofu_overlay_name`) and avoid `name_prefix` and
`random_id`-based names for resources that overlays are expected to create.

## 10. The base state is never written in v1

`create` reads the base, `rebase` reads the base, `finalize` reads the base
to verify the trunk adopted the resources. Nothing writes to it. The trunk
adopts overlay resources through `import {}` blocks committed with the
branch and applied by the trunk pipeline, under the trunk's own lock.

This is a deliberate choice: the base state's lineage, serial, `-md5` digest,
lock and encryption stay entirely under the trunk pipeline's control, and a
reviewer sees the imports in the PR diff. The price is one extra trunk apply
between `merge` and `finalize`, and the limits of import (8).

## 11. `merge --strategy state` is deferred

Direct injection of the overlay's created instances into the base state
(pull base → inject → push base under the tofu lock) would remove the import
round-trip and the non-importable limit. It is not implemented in v1 because
it violates invariant 2 and because a safe implementation needs, at minimum:

- **lineage check**: the pulled base lineage must equal `base.lineage` in the
  registry, else refuse (the base was recreated or migrated);
- **serial check**: the pulled base serial must equal the overlay's
  `base_serial` and the ETag its `base_etag` (no trunk apply since the last
  rebase), else require `rebase` first;
- **provider address equality** per resource entry: the overlay instance's
  `provider` string must equal the base entry's for the same
  (module, mode, type, name); aliased or differently-versioned provider
  addresses are refused, not rewritten;
- **schema version check**: an instance whose `schema_version` is greater
  than the one used by the same type already present in the base means the
  overlay ran a newer provider; pushing it makes the trunk's provider fail to
  decode the state. Refuse until the trunk upgrades;
- **dependency closure**: `dependencies` of injected instances must resolve
  in the merged document (either base addresses or other injected
  instances);
- **lock discipline**: the push must happen while holding the tofu lock on
  the base key (`state push` does this) with `-force` never used, so a
  concurrent trunk apply fails cleanly instead of racing;
- **encryption**: the push goes through `tofu state push` in the base data
  dir so the base encryption settings apply (6);
- **audit trail**: archive the pre-injection base state to an archive key,
  and record the injected addresses in the registry entry before the push so
  a failed push is recoverable.

`state.inject_instances()` and `state.bump_serial()` already implement the
document-level part (they are used by `rebase`, in the overlay direction).
The missing pieces are the checks above and the base-side write path.

## 12. Other deferred items

- `--destructive exclusive`: allow a single overlay to replace or delete a
  base resource, with an exclusive claim and a `guard` rule on the trunk.
  Needs a way to express "the trunk must not touch this address until the
  overlay is finalized" and a merge path that removes the base instance.
- Stacked overlays (`create --base-overlay`): see
  [SYNC-PROPOSAL.md](SYNC-PROPOSAL.md).
- `registry repair`: rebuild a registry document from the `<key>@*` objects
  and their pulled states. `doctor` reports; nothing repairs.
- DynamoDB registry backend: the registry is a JSON object in S3 with
  conditional writes; a table would give per-overlay items and finer
  contention. Not needed at the current scale.
- Identity-based `import {}` blocks (`identity = {...}`, OpenTofu >= 1.10):
  would replace the `import_ids.yaml` formats for providers that support
  them.
- Non-default workspaces, `azurerm` backend, state encryption surgery: see
  4, 5, 6.

Every deferred item, backends included, is tracked in [ROADMAP.md](ROADMAP.md).

## 13. Trunk drift is detected, not resolved

The overlay plans the branch config against a copy of the base state. When
the base state lags the trunk config (the trunk pipeline has not applied the
latest trunk on that environment), the overlay plan shows `update`s on base
resources that the branch never touched: a trunk plan would show them too.
Claiming and applying them from the overlay would silently do the trunk's
work under the branch's name, and the registry would then block the trunk
(`guard`) or another branch on those addresses.

The tool computes a **trunk baseline**: `origin/<trunk>` checked out detached
in a `git clone --shared` of the repository under `.tofu-overlay/_trunk/<sha>/`
(objects shared, nothing copied) and planned from the same relative env dir against the base state (`.tofu-overlay/_trunk-data/`,
read-only, cached per trunk sha and base ETag). A base `update` that the
baseline also reports is classified as **drift**: not claimed, listed by
`plan` (`drift:` lines, `drift` in `--json`), warned by `check`, and `apply`
refuses with exit 4. The fix is the trunk pipeline (apply the trunk on that
environment) followed by `rebase`; `apply --accept-drift` (with `--yes` in
CI) claims the drifted addresses as regular updates when the branch really
owns that change.

Caveats:

- the baseline is only computed when the overlay plan holds a new `update`
  on a base address (and by `check`), and it is skipped with a warning when
  `origin/<trunk>` is unknown locally, the export fails or the env dir does
  not exist on the trunk: every base update is then claimed as before;
- the baseline runs the trunk config with the same binary, providers and
  credentials as the overlay, without the overlay variables
  (`TF_VAR_tofu_overlay_*`), like the trunk pipeline would. A trunk config
  that cannot plan locally (missing `--backend-config`, provider auth) makes
  `plan` fail; `--accept-drift` degrades that failure to a warning;
- the export is a real repository (a `.git` directory, `origin` set to the
  same URL as the checkout) because common modules walk up to `.git/HEAD`
  and read the remotes through a git provider; `git archive` (no `.git`)
  and worktrees (`.git` is a file) both break them. Submodules are not
  initialised in the export; the clone shares the repository's objects
  (`objects/info/alternates`), so pruning objects of the repository while
  a cached export references them invalidates that export (the next run
  rebuilds it);
- a branch that changes a resource the trunk also changed is classified as
  drift for that resource: `--accept-drift` is the way to claim it;
- `delete`/replace of base resources stay denied whatever the baseline says
  (overlays are additive).

## 14. Environment-dependent attributes are applied, not skipped

Some attributes depend on where tofu runs rather than on the configuration:
`aws_lambda_function.filename` stored in the base state as
`.terraform/modules/x/code.zip` differs from the path the overlay computes
under its own `TF_DATA_DIR`; `archive_file.output_path`, `last_modified`
timestamps. An `update` on a base resource whose differing attributes are
all in `ignored_attributes` for its type (packaged defaults plus
`.tofu-overlay.yaml`) is not a branch change: it is not claimed, `plan`
lists it (`ignored:` lines, `ignored` in `--json`) and warns once.

The change is still part of the tofu plan and gets applied on the overlay:
tofu cannot leave one change out of a plan without `-target`, which the tool
rejects. This is harmless by construction (the attribute is write-only or
informational), but it means the overlay state carries the overlay's local
path for that resource; the trunk state keeps its own. If a real attribute
changes alongside an ignored one, the update is claimed as usual.

`apply --only-claims` (15) is the way to keep such updates out of the
overlay state altogether.

## 15. A targeted apply leaves the trunk's pending changes unapplied

On stacks whose plans carry machine-dependent noise (a module that zips
Lambda code locally, a CloudFormation stack parameterised with `path.cwd`),
every overlay plan shows the branch's creates plus base updates that the
trunk baseline shows too (13) or that only touch ignored attributes (14).
`apply --only-claims` answers that case: it re-plans with `-target` on
exactly the claim set and applies that plan, after gating it again
(DESIGN §7.9). Drift and ignored updates are then *not* applied on the
overlay, which is the point: the overlay state keeps the base's values for
them, and the next `plan` reports the same `drift:`/`ignored:` lines (and
`check` the same warning) until the trunk pipeline applies them and the
overlay is rebased.

Caveats:

- `-target` is tofu's partial-graph mode: a targeted plan can pull in
  dependencies of the claims. The gate refuses any pulled-in address that is
  not a claim or an ignored update, drift included, with exit 3; the way out
  is the trunk pipeline (then `rebase`), or `--accept-drift`;
- a claim of the full plan that the targeted plan does not show (a resource
  whose creation depends on an unapplied change) is dropped, not acquired;
  it is claimed by the next apply;
- the overlay state after a targeted apply is a partial application of the
  branch config. `merge` verification (DESIGN §8) and `check` reason on
  claims and do not care, but do not expect the overlay to reflect the whole
  branch;
- `--only-claims` and `--accept-drift` are mutually exclusive; without drift
  or ignored updates the flag changes nothing (one plan, applied whole).
