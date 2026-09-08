# CI integration

`tofu-overlay` is designed to run in three places: on a developer's machine,
in a pull-request build (read-only gate), and in the trunk pipeline (guard
plus periodic hygiene). This document gives Azure DevOps snippets for each,
the IAM permissions every role needs, and generic notes for other CI systems.

Conventions used below:

- the stack lives in `stacks/<stack>/env/<env>` and the pipeline already
  passes backend settings through `-backend-config` files
  (`backend/<env>.s3.tfbackend`);
- the trunk branch is `main`;
- the tool is installed with `pip install kmr-tofu-overlay==0.1.0` (pin it).

## CI mode

`CI=true` or `TF_BUILD=True` (Azure DevOps sets the latter) switches the
tool to CI mode:

- no colour, no interactive prompts; typed confirmations return "no" unless
  `--yes` is passed;
- on Azure DevOps, errors and warnings are also emitted as
  `##vso[task.logissue type=error|warning]` lines so they appear in the run
  summary;
- `--allow-stale` and `--allow-behind` are refused (a stale overlay in CI is
  a signal, not an inconvenience);
- `apply --auto-approve` is accepted only together with `--yes`; a plain
  `apply` asks the tool's own confirmation, which is refused in CI (a saved
  plan never prompts in tofu);
- `TF_CLI_ARGS`, `TF_CLI_ARGS_plan`, `TF_CLI_ARGS_apply`... are removed from
  the environment of every tofu run: they would bypass the pass-through
  filter (`-target`, `-lock=false`...).

Exit codes are the same everywhere: 0 ok, 1 tool/tofu error, 2 changes
(`plan --detailed-exitcode` only), 3 policy violation, 4 stale or behind
trunk, 5 registry problem, 6 base key not allowed or overlay not found,
7 overlay frozen (`merging`). A PR build should fail on anything but 0
(and 2 when it asks for it).

## Azure DevOps

### 1. PR build validation: `check` + `plan`

Runs on every PR targeting `main`. Read-only: it never acquires claims and
never writes the registry. If the branch has no overlay the gate exits 0
with a "no overlay" line, so stacks that do not use overlays are unaffected.

```yaml
# pipelines/pr-overlay-check.yml
trigger: none
pr:
  branches:
    include: [main]

parameters:
  - name: stacks
    type: object
    default:
      - { path: stacks/storage/env/dev, backend: backend/dev.s3.tfbackend }
      - { path: stacks/lambda/env/dev,  backend: backend/dev.s3.tfbackend }

pool:
  vmImage: ubuntu-latest

variables:
  TF_PLUGIN_CACHE_DIR: $(Pipeline.Workspace)/.plugins

steps:
  - checkout: self
    fetchDepth: 0            # merge-base against origin/main needs history
    persistCredentials: true

  - task: AzureCLI@2         # or AWSShellScript@1: any task that exports AWS creds
    displayName: Assume the PR build role
    inputs:
      azureSubscription: sc-acme-aws-oidc
      scriptType: bash
      scriptLocation: inlineScript
      inlineScript: |
        # Export AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY/AWS_SESSION_TOKEN
        # for the pr-build role of the target account.
        ./pipelines/scripts/assume-role.sh arn:aws:iam::123456789012:role/iam-acme-dev-overlay-pr
        echo "##vso[task.setvariable variable=AWS_ACCESS_KEY_ID;issecret=true]$AWS_ACCESS_KEY_ID"
        echo "##vso[task.setvariable variable=AWS_SECRET_ACCESS_KEY;issecret=true]$AWS_SECRET_ACCESS_KEY"
        echo "##vso[task.setvariable variable=AWS_SESSION_TOKEN;issecret=true]$AWS_SESSION_TOKEN"

  - script: |
      pipx install kmr-tofu-overlay==0.1.0
      curl -fsSL https://get.opentofu.org/install-opentofu.sh | sh -s -- --install-method standalone
    displayName: Install tofu-overlay and OpenTofu

  - script: |
      set -euo pipefail
      # ADO checks out a detached merge commit for PR builds. The overlay
      # name is derived from the branch, so check out the source branch by
      # name; the merge commit is on top of it, ancestry against
      # origin/main still holds.
      branch="${SOURCE_BRANCH#refs/heads/}"
      git fetch origin main "$branch"
      git checkout -B "$branch" FETCH_HEAD
    displayName: Check out the PR source branch by name
    env:
      SOURCE_BRANCH: $(System.PullRequest.SourceBranch)

  - ${{ each stack in parameters.stacks }}:
      - script: |
          set -euo pipefail
          out="$(Build.ArtifactStagingDirectory)/check-$(echo ${{ stack.path }} | tr / -).json"
          tofu-overlay -C "${{ stack.path }}" --backend-config "${{ stack.backend }}" check --json > "$out"
        displayName: overlay check (${{ stack.path }})

      - script: |
          set -uo pipefail
          out="$(Build.ArtifactStagingDirectory)/plan-$(echo ${{ stack.path }} | tr / -).json"
          tofu-overlay -C "${{ stack.path }}" --backend-config "${{ stack.backend }}" plan --json > "$out"
          rc=$?
          # 6 = no overlay for this branch on this stack: nothing to plan.
          # plan never writes claims; the JSON carries the summary,
          # violations and warnings for a PR comment step.
          [ "$rc" -eq 6 ] && { echo "no overlay for this branch, skipped"; exit 0; }
          exit $rc
        displayName: overlay plan (${{ stack.path }})

  - publish: $(Build.ArtifactStagingDirectory)
    artifact: overlay-reports
    condition: always()
```

Notes:

- ADO checks out a detached merge commit for PR builds. `check`/`plan` need
  the branch name to derive the overlay name and to verify the
  branch/overlay binding, hence the explicit checkout step. The alternative
  is to export `TOFU_OVERLAY_NAME` (compute it once on the developer's
  machine with `tofu-overlay status --json`, or reproduce the
  slug+sha1 rule) and pass `--name`, which disables the binding check.
- `check` exits 0 with a "no overlay" line when the branch has no overlay on
  a stack; `plan` exits 6 in that case, which the snippet maps to "skipped".
- `plan` in a `merging` overlay runs the verify mode (branch config against
  the base state) and fails if any created resource does not import as a
  no-op: this is the review signal for the imports file.
- Add a branch policy on `main` requiring this build.

### 2. Manual branch pipeline: `apply` behind an environment approval

Developers can apply from their machine; teams that prefer a shared runner
use a manual pipeline. The environment `dev-overlay` carries an approval
check so a reviewer sees which branch and which claims are about to land on
the sandbox.

```yaml
# pipelines/overlay-apply.yml
trigger: none      # manual runs only, from any branch

parameters:
  - name: stack
    type: string
    default: stacks/storage/env/dev
  - name: backend
    type: string
    default: backend/dev.s3.tfbackend

pool:
  vmImage: ubuntu-latest

stages:
  - stage: plan
    jobs:
      - job: plan
        steps:
          - checkout: self
            fetchDepth: 0
          - template: templates/aws-creds.yml
            parameters: { role: arn:aws:iam::123456789012:role/iam-acme-dev-overlay-apply }
          - template: templates/install-overlay.yml
          - script: |
              set -euo pipefail
              tofu-overlay -C "${{ parameters.stack }}" --backend-config "${{ parameters.backend }}" status
              rc=0
              tofu-overlay -C "${{ parameters.stack }}" --backend-config "${{ parameters.backend }}" plan --detailed-exitcode || rc=$?
              # 0 = nothing to apply, 2 = changes present; anything else fails the stage.
              case "$rc" in 0|2) ;; *) exit "$rc" ;; esac
              echo "##vso[task.setvariable variable=planRc;isOutput=true]$rc"
            name: planStep
            displayName: overlay plan

  - stage: apply
    dependsOn: plan
    condition: succeeded()
    jobs:
      - deployment: apply
        environment: dev-overlay          # approval check configured on the environment
        strategy:
          runOnce:
            deploy:
              steps:
                - checkout: self
                  fetchDepth: 0
                - template: templates/aws-creds.yml
                  parameters: { role: arn:aws:iam::123456789012:role/iam-acme-dev-overlay-apply }
                - template: templates/install-overlay.yml
                - script: |
                    set -euo pipefail
                    # A fresh plan is run inside apply; the tfplan of the
                    # previous stage is not reused on purpose (claims are
                    # acquired against the registry at apply time).
                    tofu-overlay -C "${{ parameters.stack }}" \
                      --backend-config "${{ parameters.backend }}" \
                      --yes apply --auto-approve
                  displayName: overlay apply
```

`create`, `rebase`, `merge`, `abandon` and `finalize` are left to the
developer's machine on purpose: they need a typed confirmation and a
checkout that is the branch itself, not a build artefact. If they must run
in a pipeline, pass `--yes` and make the environment approval the
confirmation.

### 3. Trunk pipeline: `guard` on the plan JSON

The trunk pipeline already runs `tofu plan -out` on merge to `main`. Add a
`show -json` and a `guard` step between plan and apply. `guard` is
read-only and fails (exit 3) if the trunk plan:

- creates a resource whose identity is claimed by a live overlay (the
  overlay would then be unmergeable);
- deletes or replaces an address under an `update` claim;
- deletes an address that an overlay's created instances depend on.

```yaml
# excerpt of pipelines/stack-storage.yml, apply stage
- script: |
    set -euo pipefail
    cd stacks/storage/env/dev
    tofu init -input=false -backend-config=../../../../backend/dev.s3.tfbackend
    tofu plan -input=false -out=tfplan
    tofu show -json tfplan > tfplan.json
  displayName: tofu plan

- script: |
    set -euo pipefail
    tofu-overlay -C stacks/storage/env/dev --backend-config backend/dev.s3.tfbackend \
      guard stacks/storage/env/dev/tfplan.json
  displayName: overlay guard
  # exit 3 = an overlay would be broken by this apply; talk to its owner
  # (tofu-overlay status lists owners) before forcing.

- script: |
    set -euo pipefail
    cd stacks/storage/env/dev
    tofu apply -input=false tfplan
  displayName: tofu apply
```

When a PR containing a `zz_overlay_<name>.imports.tf` file is merged, this
same apply adopts the overlay's resources. The overlay owner then runs
`tofu-overlay finalize` and opens a follow-up PR removing the imports file
(`doctor` reports imports files whose overlay is merged).

### 4. Scheduled hygiene: `doctor` and `gc`

Read-only by default; `gc --purge` deletes archive objects only and needs
`--yes` in CI. Run it against every base under `policy.env_dir_glob` with
`status --repo`, or per stack as below.

```yaml
# pipelines/overlay-hygiene.yml
trigger: none
schedules:
  - cron: "0 6 * * 1-5"
    displayName: weekday overlay hygiene
    branches:
      include: [main]
    always: true

pool:
  vmImage: ubuntu-latest

steps:
  - checkout: self
    fetchDepth: 0
  - template: templates/aws-creds.yml
    parameters: { role: arn:aws:iam::123456789012:role/iam-acme-dev-overlay-pr }
  - template: templates/install-overlay.yml
  - script: |
      set -euo pipefail
      rc=0
      for dir in stacks/*/env/dev; do
        echo "== $dir"
        tofu-overlay -C "$dir" --backend-config backend/dev.s3.tfbackend doctor || rc=$?
        tofu-overlay -C "$dir" --backend-config backend/dev.s3.tfbackend status --json \
          > "$(Build.ArtifactStagingDirectory)/status-$(basename $(dirname $(dirname $dir))).json"
        tofu-overlay -C "$dir" --backend-config backend/dev.s3.tfbackend gc || rc=$?
      done
      exit $rc
    displayName: overlay doctor / status / gc
  - publish: $(Build.ArtifactStagingDirectory)
    artifact: overlay-hygiene
    condition: always()
```

Findings worth an alert: overlays older than `max_overlay_age_days`,
overlays whose branch no longer exists on the remote, stale `applying`
entries, orphan `<key>@*` objects absent from the registry.

## IAM permissions per role

Three roles cover every use. Paths below assume the base key
`acme/webshop/storage/dev` in bucket `acme-tfstate` with lock table
`acme-tfstate-lock`; adapt the globs to `policy.allowed_base_keys`.

Object families touched by the tool for a base key `K`:

| Object | Purpose |
|---|---|
| `K` | base state (read-only for the tool; written by the trunk pipeline) |
| `K@*` | overlay states (`K@<name>`), archives (`K@<name>.<status>-<ts>`), rebase archives |
| `K@*.tflock` | `use_lockfile` lock objects on overlay keys |
| `K.overlays.json` | registry document |
| DynamoDB `LockID = acme-tfstate/K` | tofu lock item on the base (read by `doctor`) |
| DynamoDB `LockID = acme-tfstate/K-md5` | base digest item (never touched) |
| DynamoDB `LockID = acme-tfstate/K@*` and `K@*-md5` | overlay lock and digest items (written by tofu, deleted at finalize/abandon) |

### Developer (`iam-acme-dev-overlay-dev`)

Everything except writing the base state. Needs the same provider
permissions as the trunk apply on the sandbox account, plus:

| Service | Actions | Resources |
|---|---|---|
| S3 | `s3:ListBucket` | `arn:aws:s3:::acme-tfstate` (prefix condition `acme/webshop/*` optional) |
| S3 | `s3:GetObject` | `arn:aws:s3:::acme-tfstate/acme/webshop/*/dev` (base, via `tofu state pull`) |
| S3 | `s3:GetObject`, `s3:PutObject`, `s3:DeleteObject` | `arn:aws:s3:::acme-tfstate/acme/webshop/*/dev@*` (overlay states, `.tflock`, archives) |
| S3 | `s3:GetObject`, `s3:PutObject` | `arn:aws:s3:::acme-tfstate/acme/webshop/*/dev.overlays.json` (registry) |
| S3 | `s3:PutObject` with `s3:x-amz-copy-source` implied by `GetObject` on the source | archive copies stay under `dev@*` |
| KMS | `kms:Decrypt`, `kms:GenerateDataKey` | the bucket's CMK, if SSE-KMS |
| DynamoDB | `dynamodb:GetItem`, `dynamodb:PutItem`, `dynamodb:DeleteItem` | table `acme-tfstate-lock`, `dynamodb:LeadingKeys` in `["acme-tfstate/acme/webshop/*/dev@*"]` (overlay lock and `-md5` items) |
| DynamoDB | `dynamodb:GetItem` | same table, `LeadingKeys` `acme-tfstate/acme/webshop/*/dev` (read the base lock for `doctor`) |

Explicitly **denied** (a Deny statement, so a mis-scoped Allow elsewhere
cannot override it):

| Service | Actions | Resources |
|---|---|---|
| S3 | `s3:PutObject`, `s3:DeleteObject` | `arn:aws:s3:::acme-tfstate/acme/webshop/*/dev` (base state) |
| DynamoDB | `dynamodb:PutItem`, `dynamodb:DeleteItem` | `LeadingKeys` `acme-tfstate/acme/webshop/*/dev-md5` (base digest) |

Note: an S3 resource ARN `.../dev` does not match `.../dev@x`, and
`.../dev@*` does not match `.../dev.overlays.json`, so the three families can
be scoped independently. If the base key has an extension
(`terraform.tfstate`) the overlay key is `terraform@<name>.tfstate` and the
registry `terraform.overlays.json`; use `terraform@*` and
`terraform.overlays.json` accordingly.

### PR build (`iam-acme-dev-overlay-pr`)

Read-only. `check`, `plan`, `status`, `doctor`, `gc` (without `--purge`).
`plan` still needs `tofu init` against the overlay key, which performs a
lock acquisition only for the plan when a DynamoDB table is configured
(`-lock=false` is rejected by the tool), so the lock item on the overlay
key must be writable.

| Service | Actions | Resources |
|---|---|---|
| S3 | `s3:ListBucket` | bucket |
| S3 | `s3:GetObject` | base `.../dev`, overlays `.../dev@*`, registry `.../dev.overlays.json` |
| KMS | `kms:Decrypt` | bucket CMK |
| DynamoDB | `dynamodb:GetItem`, `dynamodb:PutItem`, `dynamodb:DeleteItem` | `LeadingKeys` `acme-tfstate/acme/webshop/*/dev@*` (overlay lock items; tofu writes and removes its own lock during plan) |
| DynamoDB | `dynamodb:GetItem` | `LeadingKeys` `acme-tfstate/acme/webshop/*/dev`, `.../dev-md5` |
| Provider read permissions | `Describe*`/`Get*`/`List*` of the services in the stack | for the refresh step of `plan` |

No `s3:PutObject` anywhere: the registry is never written by these
commands, and `use_lockfile` on a read-only role fails the plan (use the
DynamoDB lock, or grant `s3:PutObject`/`DeleteObject` on `dev@*.tflock`
only).

### Trunk pipeline (`iam-acme-dev-trunk`)

The existing trunk role, plus read access to the registry so `guard` can
run. The trunk never touches overlay objects.

| Service | Actions | Resources |
|---|---|---|
| S3 | `s3:GetObject`, `s3:PutObject` | base `.../dev` (already granted for the trunk apply) |
| S3 | `s3:GetObject` | registry `.../dev.overlays.json` (`guard`) |
| S3 | - | overlays `.../dev@*`: not needed, the dependency check of `guard` reads the `dependencies` recorded in each create claim at apply |
| DynamoDB | `dynamodb:GetItem`, `dynamodb:PutItem`, `dynamodb:DeleteItem` | `LeadingKeys` `acme-tfstate/acme/webshop/*/dev`, `.../dev-md5` (already granted) |

Optionally deny `s3:PutObject`/`DeleteObject` on `dev@*` and
`dev.overlays.json` for the trunk role, so a mis-run overlay command from
the trunk pipeline cannot alter overlays.

### Example policy fragment (developer role)

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "OverlayObjects",
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
      "Resource": [
        "arn:aws:s3:::acme-tfstate/acme/webshop/*/dev@*",
        "arn:aws:s3:::acme-tfstate/acme/webshop/*/dev.overlays.json"
      ]
    },
    {
      "Sid": "BaseRead",
      "Effect": "Allow",
      "Action": ["s3:GetObject"],
      "Resource": "arn:aws:s3:::acme-tfstate/acme/webshop/*/dev"
    },
    {
      "Sid": "BaseNeverWritten",
      "Effect": "Deny",
      "Action": ["s3:PutObject", "s3:DeleteObject"],
      "Resource": "arn:aws:s3:::acme-tfstate/acme/webshop/*/dev"
    },
    {
      "Sid": "OverlayLockItems",
      "Effect": "Allow",
      "Action": ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:DeleteItem"],
      "Resource": "arn:aws:dynamodb:eu-west-1:123456789012:table/acme-tfstate-lock",
      "Condition": {
        "ForAllValues:StringLike": {
          "dynamodb:LeadingKeys": ["acme-tfstate/acme/webshop/*/dev@*"]
        }
      }
    },
    {
      "Sid": "BaseDigestNeverWritten",
      "Effect": "Deny",
      "Action": ["dynamodb:PutItem", "dynamodb:DeleteItem"],
      "Resource": "arn:aws:dynamodb:eu-west-1:123456789012:table/acme-tfstate-lock",
      "Condition": {
        "ForAllValues:StringLike": {
          "dynamodb:LeadingKeys": ["acme-tfstate/acme/webshop/*/dev-md5"]
        }
      }
    }
  ]
}
```

## Other CI systems

Nothing is Azure-specific except the `##vso[...]` log lines, which are
emitted only when `TF_BUILD=True`. On GitHub Actions, GitLab CI, Bitbucket
Pipelines or Jenkins:

- set `CI=true` (GitHub Actions and GitLab do it by default; Jenkins does
  not: export it in the job);
- make sure the checkout has enough history for
  `git merge-base --is-ancestor origin/<trunk> HEAD` (`fetch-depth: 0` on
  GitHub Actions, `GIT_DEPTH: 0` on GitLab) and that `origin/<trunk>` is
  fetched (`git fetch origin main`);
- on detached PR checkouts, set `TOFU_OVERLAY_NAME` or check out the source
  branch by name so the branch/overlay binding check passes;
- use `--json` outputs for PR comments; the payload carries `schema: 1`,
  the summary, violations (with rule and address) and warnings;
- map exit codes: fail the job on non-zero, treat 2 from
  `plan --detailed-exitcode` as "changes present" if the pipeline asks for
  it, and surface 3/4/5/6/7 with their meaning in the job summary.

Minimal GitHub Actions job:

```yaml
overlay-check:
  runs-on: ubuntu-latest
  permissions: { id-token: write, contents: read }
  steps:
    - uses: actions/checkout@v4
      with: { fetch-depth: 0, ref: ${{ github.head_ref }} }
    - uses: aws-actions/configure-aws-credentials@v4
      with:
        role-to-assume: arn:aws:iam::123456789012:role/iam-acme-dev-overlay-pr
        aws-region: eu-west-1
    - uses: opentofu/setup-opentofu@v1
    - run: pipx install kmr-tofu-overlay==0.1.0
    - run: |
        tofu-overlay -C stacks/storage/env/dev --backend-config backend/dev.s3.tfbackend check
        tofu-overlay -C stacks/storage/env/dev --backend-config backend/dev.s3.tfbackend plan --json > plan.json
```
