# Roadmap

Single place for what is deferred. The first part covers other state
backends (the code is structured for them, only `s3` is implemented); the
second part lists the functional items deferred by [DESIGN.md](DESIGN.md)
section 11 and detailed in [LIMITS.md](LIMITS.md) and
[SYNC-PROPOSAL.md](SYNC-PROPOSAL.md).

## 1. Other backends

### 1.1 How the code is split today

The tool talks to the remote state through one small contract,
`store.StateStore` (module `src/tofu_overlay/store.py`):

| Operation | Used for |
|---|---|
| `head(key)` / `exists(key)` | ETag/version token of the base and overlay objects (freshness, invariant 3.7) |
| `list_prefix(prefix)` | `<key>@*` scan: stray overlay objects, archives, `doctor`, `list --prefix` |
| `copy(src, dst)` | archive an overlay state (`finalize`, `abandon`, `rebase`) |
| `delete(key)` | delete an overlay state or an archive (`finalize`, `abandon`, `gc --purge`) |
| `get_json(key)` / `put_json(key, doc, if_match=, if_none_match=)` | the registry document with compare-and-swap; a lost race raises `store.CasConflict` |
| `digest_item_exists(path)` / `delete_digest_item(path)` | the backend's checksum bookkeeping for a state object (S3: DynamoDB `-md5` item) |
| `lock_info(path)` / `delete_lock_marker(path)` | `doctor` lock report and the leftover lock marker deleted at finalize/abandon (S3: DynamoDB lock item, `.tflock` object) |

`store.make_store(cfg, session=None)` maps `BackendConfig.backend_type` to
an implementation (`s3` → `s3state.S3State`) and raises
`ToolError("backend '<type>' is not supported yet, see docs/ROADMAP.md")`
for anything else. `backend.resolve_backend` applies the same rule as soon
as the HCL block, the cached backend or a `backend_type` entry of a
`-backend-config` layer names another type.

Everything above the store is backend-independent and must stay so:

- the registry document (`models.RegistryDoc`, DESIGN §5), the claims, the
  status machine and the CAS loop (`registry.Registry.update`);
- the policy checks and merge verification (`plan.py`), the type knowledge
  (`identity.py`), the state document helpers (`state.py`);
- the overlay orchestration (`overlay.py`), the import-strategy merge
  (`merge.py`) and the CLI (commands, exit codes, `--json` schema), which
  never import `s3state` directly.

### 1.2 Extension points

Adding a backend touches exactly these places:

1. **`store.StateStore`**: a new module implementing the eleven operations
   above with the backend's SDK, raising `models.ToolError` /
   `models.RegistryError` / `store.CasConflict` like `s3state.py` does.
2. **`store.make_store`**: one more branch on `backend_type`, plus the type
   in `store.SUPPORTED_BACKENDS`.
3. **`models.BackendConfig`**: the backend's attributes (today's fields are
   the `s3` ones: `bucket`, `region`, `profile`, `dynamodb_table`,
   `use_lockfile`, `encrypt`, `kms_key_id`) and, if the layout differs, its
   own key builders. All derived keys are built there and nowhere else
   (`overlay_key`, `overlay_prefix`, `registry_key`, `archive_key`,
   `is_archive_key`, `lock_id`, `md5_lock_id`); the `<key>@<name>` layout
   and the `<key>.overlays.json` registry key are the only assumptions the
   rest of the code makes about names.
4. **`backend.py`**: attribute names and coercions in `resolve_backend`,
   the `TOFU_OVERLAY_*` environment overrides, `describe()`, and the
   `terraform_remote_state` parser (`parse_remote_state_refs` only maps
   `s3` references today, see [MULTI-STACK.md](MULTI-STACK.md)).
5. **`tofu.TofuRunner.init`** backend-config handling: `init -reconfigure`
   is fed the original `-backend-config` files, then the resolved
   attributes (`_backend_values`, `s3` names today), then
   `-backend-config=key=<overlay key>` last. A backend whose state object
   is not addressed by `key` needs its own override attribute (see gcs and
   http below).
6. **CLI flags** (`--bucket`, `--key`, `--region`, `--profile`,
   `--dynamodb-table`) and the `list --bucket B --prefix P` scan, which
   build a `BackendConfig` by hand.

Spots that still assume `s3` semantics inside otherwise generic code, to
revisit with the first non-`s3` store: the dry-run description of
`_archive` (mentions DynamoDB), the `.tflock` suffix test in
`doctor`, `s3://` in messages and the imports-file header, and
`_caller_arn` (STS, informative only).

### 1.3 Per backend

**azurerm** (Azure Blob Storage)

- Registry CAS: blob `ETag` with `If-Match` on update and `If-None-Match: *`
  on creation map one-to-one onto `put_json`; `CasConflict` on HTTP 412/409.
- Object operations: `HEAD` blob, list blobs by prefix inside the container,
  server-side copy (`Copy Blob`, synchronous within an account), delete.
- Overlay key: `-backend-config=key=<overlay key>` works the same (the
  `key` attribute is the blob name inside `container_name`), so the
  `<key>@<name>` layout and the `init` sequence are unchanged.
- Lock: tofu acquires a blob **lease** on the state blob and stores its lock
  info in blob metadata; `lock_info` reads lease status plus metadata,
  `delete_lock_marker` would have to break the lease (destructive: keep it a
  no-op, let `doctor` report it).
- No digest item: `digest_item_exists` returns `False`,
  `delete_digest_item` is a no-op.
- `BackendConfig`: `storage_account_name`, `container_name`,
  `resource_group_name`, authentication attributes (`use_azuread_auth`,
  `use_oidc`...) replace `bucket`/`region`/`profile`; `session` becomes a
  credential object.

**gcs** (Google Cloud Storage)

- Registry CAS: object **generation** preconditions (`ifGenerationMatch`,
  `ifGenerationMatch=0` for creation); the "ETag" returned by `get_json` is
  the generation number.
- Object operations: metadata `GET`, list by prefix, rewrite/copy, delete.
- Layout: the backend has no `key`; the state object is
  `<prefix>/<workspace>.tfstate`. The key builders must derive the overlay
  object from `prefix` (e.g. `<prefix>/default@<name>.tfstate`) and
  `TofuRunner.init` must override `prefix=` rather than `key=`, with the
  overlay reading `default.tfstate` under its own prefix.
- Lock: a `<state object>.tflock` object created with a generation-0
  precondition; `lock_info` reads it, `delete_lock_marker` deletes it. No
  digest item.
- Encryption: `encryption_key` / `kms_encryption_key` re-applied on copy,
  like `kms_key_id` today.

**http**

- Server-dependent. The protocol has `GET`/`POST`(or `PUT`)/`DELETE` on
  `address` and `LOCK`/`UNLOCK` on `lock_address`/`unlock_address`; ETags
  and conditional writes are **not guaranteed**, and there is no listing.
- The registry needs a store that does offer CAS: either the same server
  when it honours `If-Match` (to be probed at `init`), or a separate
  registry store (an object store or a key-value table) configured next to
  the backend. `StateStore` may need to be split into a state part and a
  registry part for this backend.
- Without listing, `doctor`'s "unregistered object" and archive scans are
  impossible; the registry becomes the only inventory. Archives need a
  dedicated `address` per copy, and `copy` becomes `GET` + `POST`.
- Overlay key: one `address` per overlay, so `TofuRunner.init` overrides
  `address=` (and `lock_address`/`unlock_address`) instead of `key=`.

**local**

- Development use only (single machine, no shared sandbox): files under a
  directory, CAS by atomic rename with an inode/mtime token or `flock`,
  listing by glob, lock = tofu's `.terraform.tfstate.lock.info` file. Useful
  to run the whole lifecycle in tests without moto, not a production target.

### 1.4 Order of work

1. Split `BackendConfig` into a common part (`backend_type`, `key` layout
   builders, `backend_config_files`, `workspace`) and per-type attribute
   models; keep `s3` field names as they are so `--json` output and
   `.tofu-overlay.yaml` stay compatible.
2. Make `TofuRunner.init` take the override attribute name from the config.
3. Implement `azurerm` (closest semantics), then `gcs`; decide the http
   registry-store question with a real server in hand.

## 2. Functional items deferred by DESIGN §11

- `merge --strategy state`: direct injection into the base state under the
  tofu lock protocol, with lineage, provider address and schema-version
  checks. Analysed in [LIMITS.md](LIMITS.md) section 11.
- `--destructive exclusive`: a single overlay may replace or delete a base
  resource, with an exclusive claim and a `guard` rule on the trunk
  (LIMITS.md section 12).
- Stacked overlays (`create --base-overlay`): [SYNC-PROPOSAL.md](SYNC-PROPOSAL.md).
- `registry repair`: rebuild a registry document from the `<key>@*` objects
  and their pulled states; `doctor` reports, nothing repairs.
- DynamoDB registry backend: per-overlay items instead of one JSON document,
  for finer contention. Not needed at the current scale, and a natural
  companion of the store split described for `http` above.
- Identity-based `import {}` blocks (`identity = {...}`, OpenTofu >= 1.10)
  in place of the `import_ids.yaml` formats.
- Non-default workspaces: `BackendConfig.state_path()` already models the
  `env:/<workspace>/<key>` layout of `s3`, everything else refuses them
  (LIMITS.md section 5).
- State encryption surgery (LIMITS.md section 6).
