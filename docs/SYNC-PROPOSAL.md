# Proposal: sharing resources between overlays (stacked overlays)

Status: proposal, not implemented in v1. Referenced from DESIGN.md section 11
and LIMITS.md items 7 and 12.

## 1. The need

Two situations come up once overlays are in daily use:

1. **Sequential branches.** Branch B builds on branch A's work before A is
   merged (A created a bucket, B adds a notification on it). B's overlay is
   forked from the base, which does not contain A's bucket, so B's plan wants
   to create it again and is refused with an identity conflict (A holds the
   claim). Today the only path is to wait for A to be merged and applied by
   the trunk, then `rebase`.

2. **Cross-stack outputs on the same branch.** Stack `network` adds an output
   consumed by stack `lambda` through `terraform_remote_state` on the same
   branch. `lambda`'s overlay reads `network`'s **base** state and never sees
   the new output until `network` is merged (LIMITS.md item 7).

Both are "I want to see another overlay's resources from mine". The tempting
shortcut is to copy the other overlay's instances into mine. This document
explains why that shortcut is wrong, and proposes two designs that are not.

## 2. Why state injection into a sibling overlay is wrong

"Sync overlay B from overlay A" = pull A's state, pick A's created instances,
inject them into B's state. It looks like a small extension of what
`rebase` already does (inject own instances on a fresh base copy). It breaks
the model in four ways:

- **Two states now claim the same objects.** A's registry entry holds the
  `create` claims; B's state holds instances for the same addresses. If B is
  abandoned, `abandon` must not destroy them (they are not B's claims), so
  `state rm` them first; fine. But if B *applies* and its config changes one
  of those resources, B updates an object that A owns without an `update`
  claim on it, and A's next plan shows drift it did not cause. The registry
  no longer describes who changes what.
- **Reality diverges silently.** A keeps applying; B's copy of A's instances
  is a snapshot. B's next plan refreshes them from the cloud and may plan
  updates to "restore" attributes A changed on purpose (the same problem as
  a trunk apply on an overlay `update`, LIMITS.md item 2, but between two
  branches that are both moving).
- **Merge order becomes a constraint nobody records.** B's imports file
  would list A's resources too (they are in B's state) unless the tool
  filters them by claim owner. If B is merged first, the trunk imports A's
  objects under B's commit, A becomes unmergeable (`finalize` sees its
  identities already in the base, created by "someone else"), and A's owner
  discovers it after the fact.
- **Invariant 3 is violated.** "The registry stores intention, the overlay
  state stores reality" only holds if every instance in an overlay state is
  either a base instance or one of that overlay's own claims. A third kind
  of instance ("borrowed from A") needs its own bookkeeping in every
  command (`plan` policy, `apply` id refresh, `rebase`, `abandon`,
  `finalize`, `check`, `guard`) and every one of them becomes a place to get
  it wrong.

The right primitive is not "copy A's instances into B" but "B's base is A".

## 3. Design A: stacked overlays (`create --base-overlay`)

An overlay's base is today always the trunk state. Stacking generalises it:
an overlay may be forked from another live overlay.

### 3.1 Creation

```bash
git switch -c feature/ABC-13-notifications feature/ABC-12-reports
tofu-overlay create --base-overlay abc-12-reports-3f9a1c
```

`create --base-overlay PARENT`:

- requires `PARENT` to be live and not frozen (`active` or `dirty`), and the
  current branch to contain the parent's branch tip
  (`git merge-base --is-ancestor <parent.branch> HEAD`), so the child's
  config is a superset of the parent's;
- pulls the **parent overlay state** (not the trunk state), forks it (new
  lineage, serial 0) and pushes it to `<key>@<child>`;
- records in the registry entry: `parent: PARENT`, `parent_etag` (the parent
  overlay object's ETag at fork), plus the usual `base_etag`/`base_serial`
  copied from the parent (the trunk freshness reference is inherited);
- the parent's entry gets `children: [child]` so `merge`/`finalize`/`abandon`
  on the parent can refuse or warn while children are live.

The registry gains a **parent chain**: `child.parent -> parent.parent -> ...
-> null` (trunk). Depth is bounded by `policy.max_stack_depth` (proposal: 2).

### 3.2 What the child sees and claims

- Base addresses for the child = trunk base addresses ∪ addresses created by
  every overlay in the parent chain. The policy treats the parent's created
  resources like base resources: the child may `update` them with an
  exclusive `update` claim (recorded against the parent's address; the
  parent's own plan then shows the drift warning), may not delete or
  replace them.
- A `create` in the child is checked against the trunk base identities, the
  parent chain's `create` claims (they are already present, so a re-create
  is an identity conflict, as today) and every other live overlay.
- `check`, `status` and `guard` include the chain in their views (`status`
  prints the chain; `guard` protects parent claims as it does for any
  overlay).

### 3.3 Freshness across the chain

Two freshness references exist for a child:

- **trunk freshness**: inherited `base_etag`; stale when the trunk moves, as
  today;
- **parent freshness**: `parent_etag`; stale when the parent applies again.

`apply` refuses when either is stale. `plan` warns and names which one.

### 3.4 Rebase across the chain

`rebase` on a child re-forks it from the *current* parent overlay state and
re-injects the child's own `create` instances (exactly today's algorithm with
the parent state as the "base" document). Provider equality and
`schema_version` checks apply as today.

`rebase` on a parent that has live children is allowed (it does not change
the parent's own resources) but marks every child `parent-stale`; each child
rebases in turn. The chain is rebased top-down; a bottom-up rebase is
refused because the parent would move again.

When the trunk moves, the whole chain is stale: the parent rebases on the
trunk, then each child rebases on its parent. `status` prints the order.

### 3.5 Merge, finalize, abandon

- `merge` on a child while the parent is live: the child's imports file
  contains only the child's own `create` claims (the parent's are the
  parent's job). Verification runs the child's branch config against the
  **parent overlay state**, not the trunk state, since that is where the
  parent's resources exist. The child can therefore be verified but **not
  finalized** before the parent: `finalize` on a child requires
  `parent == null` or the parent to be `merged`.
- The usual sequence is parent merged and finalized first. When the parent
  is finalized its children are **re-parented to the trunk**: the child's
  `parent` becomes `null`, `base_etag` is reset to the trunk ETag observed at
  the parent's finalize, and the child is marked stale (the trunk now holds
  the parent's resources, with the same ids, so the child's next `rebase`
  finds them in the base and drops its copies of them: no conflict).
- `abandon` on a parent with live children is refused unless
  `--cascade` (abandon children first, bottom-up) or the children are
  abandoned manually. Abandoning a child destroys only the child's own
  claims, as today; the parent's instances are `state rm`-ed from the child
  state first, like base instances.
- A merged parent whose children were re-parented is tombstoned normally.

### 3.6 Cross-stack outputs

Stacking solves situation 1. Situation 2 (cross-stack `remote_state`) is a
different axis: the child overlay is on another *stack*, not another branch.
It can reuse the mechanism if `terraform_remote_state` keys are rewritten
per overlay:

```hcl
data "terraform_remote_state" "network" {
  backend = "s3"
  config = {
    bucket = "acme-tfstate"
    key    = var.tofu_overlay_network_key   # base key, or "<key>@<name>" when set
    region = "eu-west-1"
  }
}
```

The tool already exports `TF_VAR_tofu_overlay_name`; a `--link STACK=OVERLAY`
option on `plan`/`apply` (persisted in the registry entry as `links`) would
export `TF_VAR_tofu_overlay_<stack>_key` with the linked overlay key when the
linked overlay is live, and the base key otherwise. `check` verifies the
links are live and fresh; `finalize` of the linked overlay drops the link
(the outputs are now in the linked stack's base). No state is copied.

This needs the stacks to opt in (the variable and the `key` expression), and
the linked overlay's state to be readable by the reader's role (it is, in
the same bucket).

### 3.7 Cost

- Registry: three fields (`parent`, `parent_etag`, `children`) and one
  status (`parent-stale`), plus `links`.
- Code: `create` and `rebase` take the base document from a parent overlay
  instead of the trunk; `_base_addresses`/`_base_identities` union the chain;
  `finalize` re-parents; `abandon` gains `--cascade`; `status` prints the
  chain and the rebase order; `merge` verifies against the parent state.
- No new raw S3 operation; every state read stays `state pull`, every write
  `state push`.

## 4. Design B: data sources instead of stacking

Before building stacking, consider whether the need is really "B modifies
A's resources" or only "B reads A's resources".

Most sequential-branch cases are the second: B needs the bucket's ARN or the
role's name. A `data` source in B's config reads the object from the cloud,
whatever state it lives in:

```hcl
data "aws_s3_bucket" "reports" {
  bucket = "s3-acme-dev-reports"     # created by branch A's overlay
}

resource "aws_s3_bucket_notification" "reports" {
  bucket = data.aws_s3_bucket.reports.id
  # ...
}
```

Properties:

- no state coupling: B claims only its own `aws_s3_bucket_notification`;
  A's bucket stays A's;
- the policy already handles it: `["read"]` actions are ignored;
- when A is merged, B's config does not change; when both are on the trunk
  the `data` source can be replaced by a direct reference in a follow-up
  refactor (or kept: reading one's own resource through a data source is
  legitimate, if slightly redundant);
- if A is abandoned, B's plan fails at refresh with a clear "bucket not
  found" error, which is the correct outcome;
- `check` can be taught to warn when a `data` source's identity matches a
  live overlay's `create` claim ("this branch depends on overlay A"), which
  gives visibility at zero cost in state handling.

Limits:

- it does not cover "B changes A's resource" (that is an `update` on a
  resource that is not in B's base; only stacking gives it a home);
- for cross-stack outputs it requires the reader to know the physical name
  or a tag to look the object up (the `data` source replaces the
  `remote_state` output), which is often a better design anyway (looser
  coupling between stacks) but is a config change in the reading stack;
- singletons with attributes that B must own (a bucket policy on A's bucket)
  remain a conflict, by design (LIMITS.md item 3).

## 5. Recommendation

1. Ship v1 without stacking. Document Design B as the recommended pattern
   for "B builds on A" and add the `check` warning that names the overlay a
   `data` source depends on.
2. Add `--link STACK=OVERLAY` (section 3.6) if cross-stack outputs on a
   branch become a recurring need: it is a small, state-free change.
3. Implement Design A (stacked overlays, depth 2) only if "B modifies A's
   resources before A is merged" happens often enough to justify the
   re-parenting and chain-rebase logic. Never implement sibling state
   injection.
