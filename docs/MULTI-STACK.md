# Multi-stack branches: overlay-aware `terraform_remote_state`

A branch often touches two stacks at once: stack A (the *producer*) adds an
output, stack B (the *consumer*) reads it through
`data "terraform_remote_state"`. Without help, B's overlay would read A's
**base** state and see the old outputs until A is merged and applied by the
trunk.

`tofu-overlay` closes that gap with one explicit rule:

> When B's configuration reads A's base key through a `terraform_remote_state`
> block with an `s3` backend, and A's base holds a **live** overlay with the
> **same overlay name** as B's (same branch), then B's `plan`, `apply`,
> `merge` verification and `abandon` destroy plan read **A's overlay state**
> instead of A's base.

Nothing is rewritten in the configuration or in any state. The tool exports
the mapping as a variable and the stack decides to use it. Trunk pipelines,
which never set the variable, keep reading the base.

## 1. The HCL contract

Declare the variable once (typically in the shared `variables.tf` of the
stack) and use `lookup()` in every `terraform_remote_state` block:

```hcl
# variables.tf (shared by every env of the stack)
variable "tofu_overlay_keys" {
  description = "Set by tofu-overlay: base key -> overlay key of the same branch."
  type        = map(string)
  default     = {}
}

# remote.tf (env dir of stack B, e.g. stacks/lambda/env/dev)
data "terraform_remote_state" "eks" {
  backend = "s3"
  config = {
    bucket = "acme-tfstate"
    key    = lookup(var.tofu_overlay_keys, "acme/webshop/eks/dev", "acme/webshop/eks/dev")
    region = "eu-west-1"
  }
}
```

Rules the tool relies on:

- the `lookup()` map key **is** the base key of the producer, written as a
  literal, and the default is that same literal. The tool parses this exact
  shape (`lookup(var.tofu_overlay_keys, "<base key>", ...)`) as a resolved
  reference to `<base key>`; a plain literal `key = "acme/webshop/eks/dev"`
  is parsed too but never switches to the overlay (there is no variable to
  drive it);
- `bucket` and `region` should be literals as well. A `region` expression is
  tolerated (the current stack's region is used); a `bucket` expression makes
  the reference *unresolved*;
- `backend` must be `"s3"`. Other backends are ignored;
- `workspace` must be absent or `"default"` (overlays are default-workspace only).

The variable is exported to **every** tofu run of an overlay as
`TF_VAR_tofu_overlay_keys` containing a JSON object, alongside
`TF_VAR_tofu_overlay_name`:

```text
TF_VAR_tofu_overlay_keys={"acme/webshop/eks/dev":"acme/webshop/eks/dev@abc-12-reports-3f9a1c"}
```

When nothing is mapped the value is `{}`. OpenTofu/Terraform accept JSON
syntax for `map(string)` variables given through the environment. A stack
that does not declare the variable simply ignores it (a warning is printed
by tofu about an undeclared variable value only when it comes from a
`-var`/`-var-file`, not from `TF_VAR_*`).

## 2. How a reference is resolved

For each `data "terraform_remote_state"` block of the env directory (every
`*.tf`, symlinks followed), `plan`/`apply`/`status`/`check` do, read-only:

1. build the backend of the referenced base: the reference's `bucket`/`key`
   (and `region` when literal), the current stack's `profile`, `region` and
   `dynamodb_table` otherwise;
2. skip it when it points at the current stack's own base;
3. load that base's registry (`<key>.overlays.json`). A missing registry means
   "no overlays"; an unreadable or invalid registry is reported as a warning
   and the reference is skipped;
4. if an overlay named like the current one exists with a live status
   (`creating`, `active`, `applying`, `dirty`, `merging`) **and** its state
   object exists (`HEAD`), map `base key -> overlay key`.

`plan` and `apply` print one line per mapped reference:

```text
remote state eks: reading overlay acme/webshop/eks/dev@abc-12-reports-3f9a1c
```

`--json` output of `plan`, `apply` and `status` carries
`remote_overlays: {"<base key>": "<overlay key>"}`.

## 3. Order of operations for a multi-stack PR

Same branch, hence same overlay name on both stacks. With A = `stacks/eks`
(producer) and B = `stacks/lambda` (consumer):

```bash
git switch -c feature/ABC-12-reports

# 1. Producer first: fork and apply so its outputs exist in the overlay state.
cd stacks/eks/env/dev
tofu-overlay create
tofu-overlay apply

# 2. Consumer: its plan now reads A's overlay (the line above is printed).
cd ../../../lambda/env/dev
tofu-overlay create
tofu-overlay plan          # "remote state eks: reading overlay acme/webshop/eks/dev@..."
tofu-overlay apply

# 3. Merge both (imports files), open the PR, trunk applies the imports.
cd ../../../eks/env/dev    && tofu-overlay merge
cd ../../../lambda/env/dev && tofu-overlay merge

# 4. Finalize the producer FIRST (its outputs are now in the base), then the consumer.
cd ../../../eks/env/dev    && tofu-overlay finalize
cd ../../../lambda/env/dev && tofu-overlay plan      # reads the base again, automatically
tofu-overlay finalize
```

Why this order:

- outputs added by the producer exist only after its `apply`. A consumer plan
  run before that sees the producer's overlay state without the new output
  and fails exactly as it would against the base;
- `finalize` of the producer archives its overlay object. From that moment the
  consumer's reference is not mapped any more and its next plan reads the
  base, which by then contains the trunk's copy of the producer's resources
  and outputs. `finalize` on A warns when a live overlay of the same name on
  another stack of the repository references A's base (best effort: it walks
  `policy.env_dir_glob` from the repository root), telling you to re-plan it;
- `check` on the consumer warns while the producer is `merging` (it will
  disappear at finalize) and errors when a mapped overlay's object no longer
  exists (registry says live, object gone: run `doctor` on the producer).

## 4. When the producer overlay does not exist

Nothing special happens: the reference is not mapped, `TF_VAR_tofu_overlay_keys`
does not contain the key, `lookup()` returns its default and the consumer
reads the base. This is also what the trunk pipeline does, since it never
runs under `tofu-overlay`. A consumer overlay can therefore exist on its own,
and a producer overlay can be abandoned at any time: the consumer's next plan
falls back to the base.

## 5. Alternative: look resources up by identity

`terraform_remote_state` couples B to A's state layout. When the value B
needs is the physical name or id of a resource, a data source by identity is
often a better design and needs no mapping at all:

```hcl
data "aws_eks_cluster" "this" {
  name = "k8s-acme-dev-webshop"
}
```

The overlay of A creates the object in the cloud at `apply`; B's data source
finds it whether A's overlay is live, merged or abandoned. The price is that B
must know the physical name (deterministic names, see LIMITS.md §9), and that
a value only present in A's outputs (a computed ARN, a generated password
reference) still needs `remote_state`.

## 6. Limits

- **Same overlay name across stacks = same branch.** The mapping is keyed by
  the overlay name derived from the branch (or `--name`/`TOFU_OVERLAY_NAME`,
  which must then be the same on both stacks). Two different branches never
  see each other's overlays.
- **A reference with an unresolved key cannot be mapped.** A key built from
  an expression the tool cannot evaluate (`"acme/${var.env}/eks"`, `local.key`,
  a `bucket` expression, a non-default `workspace`) is reported by `plan` and
  `check` as a warning and reads the base. Use the literal contract of §1.
- **Outputs added by the producer only exist after its `apply`.** Plan the
  consumer after the producer applied.
- **The map is keyed by base key only.** Two references to the same key in
  different buckets would collide; keep one state bucket per key.
- **One hop.** B reads A's overlay; if A itself reads C through `remote_state`,
  A's overlay state was produced by A's own runs (which applied the same
  rule), so the chain is consistent as long as each stack is applied in
  dependency order.
- **`finalize` ordering help is best effort** and only runs when a repository
  root is found: it parses the sibling env dirs' `*.tf`, resolves their
  backend from the HCL block or cached `.terraform` (no `--backend-config`
  files) and reads their registries. Stacks whose backend needs external
  configuration are silently skipped.
- **The consumer does not get a claim on the producer's resources.** Claims
  and conflict checks stay per base; a producer overlay abandoned while the
  consumer still depends on its objects leaves the consumer's next plan with
  missing dependencies, exactly as with a deleted base resource.
