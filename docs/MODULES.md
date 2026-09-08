# Module contracts (v1)

Every module implements exactly these public names with these signatures. Implementers may add private helpers. Types come from `tofu_overlay.models`. No module may import from `overlay.py`, `merge.py` or `cli.py` (those are the top of the dependency graph). Dependency order: `models` → `config`, `identity`, `state` → `store`, `backend`, `s3state`, `tofu`, `plan` → `registry` → `overlay`, `merge` → `cli`. `output` depends only on `models`. `store` depends only on `models` (it imports `s3state` lazily inside `make_store`); `backend` imports the error text from `store`. Nothing above `store` may import `s3state`: the registry, the orchestration and the CLI are typed against `store.StateStore` and obtain an implementation through `store.make_store`.

Conventions: Python 3.11, type hints everywhere, `from __future__ import annotations`, docstrings on public names, no `print` outside `output.py`, errors are subclasses of `models.OverlayError` carrying an `exit_code`. Comments in English. Keep functions small and testable; anything touching subprocess/boto3 must be injectable for tests.

## models.py

```python
class ExitCode(IntEnum): OK=0; ERROR=1; CHANGES=2; POLICY=3; STALE=4; REGISTRY=5; NOT_ALLOWED=6; FROZEN=7
class OverlayError(Exception): exit_code: ExitCode = ExitCode.ERROR   # subclasses: PolicyError(3), StaleError(4), RegistryError(5), NotAllowedError(6), FrozenError(7), ToolError(1)
class Status(StrEnum): CREATING, ACTIVE, APPLYING, DIRTY, MERGING, MERGED, ABANDONED, NEEDS_REVIEW
class ClaimKind(StrEnum): CREATE, UPDATE
class BackendConfig(BaseModel): backend_type:str="s3"; bucket:str; key:str; region:str|None; profile:str|None; dynamodb_table:str|None; use_lockfile:bool=False; encrypt:bool=True; kms_key_id:str|None; workspace:str="default"; backend_config_files:list[str]=[]
    # backend_type names the tofu backend; the other attributes are the s3 ones (see docs/ROADMAP.md for the split a second backend needs)
    # key builders (the ONLY place derived keys are built; a future backend may need its own layout): state_path() -> str (== key for default ws); overlay_prefix()->str ("<stem>@"); overlay_key(name)->str; registry_key()->str; archive_key(name, status, ts)->str; is_archive_key(key)->bool; lock_id(path)->str ("<bucket>/<path>"); md5_lock_id(path)->str ("<bucket>/<path>-md5")
    # extension-aware: "a/b/terraform.tfstate" -> overlay "a/b/terraform@NAME.tfstate", registry "a/b/terraform.overlays.json", archive "a/b/terraform@NAME.merged-TS.tfstate"
class Claim(BaseModel): kind:ClaimKind; type:str; identity:dict[str,Any]={}; id:str|None; import_id:str|None; after_hash:str|None; dependencies:list[str]=[]; claimed_at:str; updated_at:str   # dependencies: state `dependencies` of a created instance, recorded at apply (guard)
class Overlay(BaseModel): name; state_key; lineage:str|None; branch; owners:list[str]; caller_arn:str|None; binary:str="tofu"; tofu_version:str|None; created_at; updated_at; base_serial:int|None; base_etag:str|None; trunk_commit:str|None; status:Status; applied_commit:str|None; run_id:str|None; applying_since:str|None; claims:dict[str,Claim]={}; pending_revert:list[str]=[]; last_apply:dict|None
    # helpers: is_live()->bool (creating/active/applying/dirty/merging); create_claims()->dict[str,Claim]; update_claims()->dict[str,Claim]
class Tombstone(BaseModel): status:Status; at:str; branch:str; pending_revert:list[str]=[]
class RegistryDoc(BaseModel): version:int=1; tool_version:str; base:dict (bucket,key,lineage:str|None); overlays:dict[str,Overlay]={}; tombstones:dict[str,Tombstone]={}
    # helpers: live_overlays()->dict[str,Overlay]
class PolicyConfig(BaseModel): allowed_base_keys:list[str]=[]; trunk_branch:str="main"; env_dir_glob:str="stacks/*/env/*"; tombstone_days:int=14; apply_timeout_min:int=90; max_overlay_age_days:int=30
class ToolConfig(BaseModel): policy:PolicyConfig; binary:str="tofu"; identity:dict[str,list[str]]={}; import_ids:dict[str,str]={}; virtual_attributes:dict[str,list[str]]={}; non_importable:list[str]=[]; replace_prone:list[str]=[]
class ResourceChange(BaseModel): address; previous_address:str|None; module_address:str|None; mode:str|None; type; name; index:Any; deposed:str|None; actions:list[str]; before:Any; after:Any; after_unknown:Any; before_sensitive:Any; after_sensitive:Any; replace_paths:list; importing:dict|None; action_reason:str|None   # mode ("managed"/"data") is authoritative for data-source detection
class PlanSummary(BaseModel): create:int=0; update:int=0; delete:int=0; replace:int=0; import_:int=0 (alias "import"); no_op:int=0
class Violation(BaseModel): address:str; rule:str; message:str; other_overlay:str|None
class RemoteStateRef(BaseModel): name:str; bucket:str|None; key:str|None; region:str|None; unresolved:bool=False   # one data "terraform_remote_state" (s3) block; unresolved -> key None
class PolicyResult(BaseModel): violations:list[Violation]; warnings:list[str]; claims:dict[str,Claim]  # claims to acquire on apply
    # ok -> bool
class BackendInfo / misc small models as needed.
def utcnow_iso() -> str
def new_run_id() -> str  (uuid4 hex[:12])
```

## config.py

```python
def find_repo_root(start: Path) -> Path | None          # git toplevel via `git rev-parse --show-toplevel`, else walk up for .git
def load_config(start: Path) -> ToolConfig                # merges .tofu-overlay.yaml (walk up to repo root) over defaults; env TOFU_OVERLAY_BINARY overrides binary
def is_ci() -> bool                                        # CI=true or TF_BUILD=True (case-insensitive)
def is_ado() -> bool                                       # TF_BUILD=True
def current_branch(cwd: Path) -> str                       # git branch --show-current; ToolError if detached/none
def head_commit(cwd: Path) -> str
def is_tree_dirty(cwd: Path) -> bool
def branch_contains_trunk(cwd: Path, trunk: str) -> bool | None   # merge-base --is-ancestor origin/<trunk> HEAD; None if origin/<trunk> unknown
def remote_branch_exists(cwd: Path, branch: str) -> bool
def overlay_name_for(branch: str) -> str                   # slug: lowercase, [^a-z0-9]+ -> '-', strip '-', [:34] + '-' + sha1(branch)[:6]; validate ^[a-z0-9][a-z0-9-]{0,40}$
def resolve_overlay_name(explicit: str|None, cwd: Path) -> str   # --name > TOFU_OVERLAY_NAME > overlay_name_for(current_branch)
def git_user_email(cwd: Path) -> str
def base_key_allowed(key: str, policy: PolicyConfig) -> bool     # fnmatch over allowed_base_keys; empty list -> False
def ensure_gitignored(repo_root: Path, entry: str) -> bool        # True if `.tofu-overlay/` is ignored (git check-ignore); never edits files
```

## backend.py

```python
class BackendResolutionError(ToolError)
def parse_hcl_backend(dir: Path) -> dict | None            # scan *.tf (symlinks followed) for terraform{backend "<type>"{}}; python-hcl2 8.x returns quoted strings ("\"x\"") -> strip quotes; return raw dict of attrs plus "backend_type": <type> (any type; several blocks -> error)
def parse_remote_state_refs(dir: Path) -> list[RemoteStateRef]   # every data "terraform_remote_state" with backend = "s3" in *.tf (symlinks followed); config.bucket/key/region unquoted; key literal or the `lookup(var.tofu_overlay_keys, "<key>", ...)` contract -> resolved; any other expression (or a bucket expression, or a non-default workspace) -> unresolved=True, key None; other backends skipped
def parse_backend_config_files(files: list[Path]) -> dict    # key=value and HCL files (tofu -backend-config syntax)
def read_cached_backend(data_dir: Path) -> dict | None      # <data_dir>/terraform.tfstate JSON: {"backend": {"type": ..., "config": {...}}} -> config plus "backend_type" (any type)
def resolve_backend(cwd: Path, *, overrides: dict, backend_config_files: list[Path], data_dir: Path|None) -> BackendConfig
    # precedence: overrides > files > cached > HCL; backend_type from the merged layers (default "s3") must be in store.SUPPORTED_BACKENDS else BackendResolutionError(store.unsupported_backend_message(type)) = "backend '<type>' is not supported yet, see docs/ROADMAP.md (...)"; error if bucket/key missing or contain "${"; error if workspace != default (TF_WORKSPACE env or <cwd>/.terraform/environment)
def describe(cfg: BackendConfig) -> str                     # "<backend_type>://bucket/key (region, profile, table, lockfile)"
```

## store.py

```python
SUPPORTED_BACKENDS: tuple[str, ...] = ("s3",)
class CasConflict(RegistryError)                            # a conditional registry write lost the race (re-exported by s3state for compatibility)
def unsupported_backend_message(backend_type: str) -> str  # "backend '<type>' is not supported yet, see docs/ROADMAP.md" + per-type hint (azurerm: leases, gcs: generations, http, local)
@runtime_checkable
class StateStore(Protocol):                                 # exactly the operations the tool needs from a backend; see docs/ROADMAP.md for the per-backend mapping
    cfg: BackendConfig
    def head(self, key: str) -> dict | None                 # {"etag": str, "size": int, "last_modified": str} or None when absent
    def exists(self, key: str) -> bool
    def list_prefix(self, prefix: str) -> list[str]
    def copy(self, src: str, dst: str) -> None
    def delete(self, key: str) -> None                      # absent object is not an error
    def get_json(self, key: str) -> tuple[dict, str] | None # (doc, version token) or None when absent
    def put_json(self, key: str, doc: dict, *, if_match: str | None, if_none_match: bool=False) -> str   # CAS write; CasConflict when the token no longer matches / object already exists
    def digest_item_exists(self, path: str) -> bool         # backend checksum bookkeeping of a state key (no-op False when the backend has none)
    def delete_digest_item(self, path: str) -> None
    def lock_info(self, path: str) -> dict | None           # current tofu lock info, None when unlocked
    def delete_lock_marker(self, path: str) -> None         # leftover lock marker of a state key, no-op when absent
def make_store(cfg: BackendConfig, session: Any = None) -> StateStore   # "s3" -> s3state.S3State(cfg, session=session); other backend_type -> ToolError(unsupported_backend_message(type))
```

## s3state.py

The `s3` implementation of `store.StateStore`; nothing above `store` imports it.

```python
class S3State:                                              # implements StateStore
    def __init__(self, cfg: BackendConfig, session: boto3.Session | None = None)
    def head(self, key: str) -> dict | None                 # HEAD: {"etag": str, "size": int, "last_modified": str} or None if 404
    def exists(self, key: str) -> bool
    def list_prefix(self, prefix: str) -> list[str]         # ListObjectsV2, paginated
    def copy(self, src: str, dst: str) -> None              # CopyObject, re-applies SSE (AES256 or aws:kms + kms_key_id) per cfg
    def delete(self, key: str) -> None
    def get_json(self, key: str) -> tuple[dict, str] | None # (doc, etag) or None if 404
    def put_json(self, key: str, doc: dict, *, if_match: str | None, if_none_match: bool=False) -> str   # IfMatch / IfNoneMatch: *; returns new etag; raises CasConflict on 412/409 (PreconditionFailed, ConditionalRequestConflict)
    # DynamoDB (only when cfg.dynamodb_table; False/None/no-op otherwise):
    def digest_item_exists(self, path: str) -> bool         # LockID=<bucket>/<path>-md5 item
    def delete_digest_item(self, path: str) -> None
    def lock_info(self, path: str) -> dict | None           # current tofu lock info for LockID=<bucket>/<path>, or None
    def delete_lock_marker(self, path: str) -> None         # S3 <path>.tflock (use_lockfile) if present
CasConflict = store.CasConflict                             # re-export
```

## registry.py

```python
class Registry:
    def __init__(self, store: StateStore, cfg: BackendConfig, tool_version: str)
    store: StateStore
    key: str                                                # cfg.registry_key()
    def load(self) -> tuple[RegistryDoc, str | None]        # (doc, etag); if object missing: if any overlay objects exist under cfg.overlay_prefix() -> RegistryError("registry missing but overlays exist, run doctor"); else empty doc with etag None. Invalid JSON/newer version -> RegistryError
    def update(self, fn: Callable[[RegistryDoc], None], *, attempts: int = 8) -> RegistryDoc   # CAS loop as in DESIGN §5; fn must be idempotent; after exhaustion re-load and return if fn(doc) is a no-op (compare dumps)
    def get_overlay(self, doc: RegistryDoc, name: str) -> Overlay   # NotAllowedError(6) if absent
    def conflicts_for(self, doc: RegistryDoc, me: str, wanted: dict[str, Claim]) -> list[Violation]   # checks 3 and 4 of DESIGN §7 against every live overlay except `me`
    def acquire_claims(self, name: str, wanted: dict[str, Claim], run_id: str, base_etag: str) -> RegistryDoc
        # single update(): overlay must be live & not frozen; base_etag must equal overlay.base_etag; conflicts_for must be empty else PolicyError; set claims (idempotent), status APPLYING, run_id, applying_since
    def finish_apply(self, name: str, *, ok: bool, claims: dict[str, Claim], applied_commit: str|None, caller_arn: str|None, tofu_version: str|None, base_etag_after: str|None, summary: dict, run_id: str|None = None, released: Iterable[str] = ()) -> RegistryDoc
        # RegistryError when run_id is given and differs from the entry's (superseded apply); `released` create claims are dropped on ok=True only
    def set_status(self, name: str, status: Status, *, expect_status: Iterable[Status]|None = None, expect_entry: Overlay|None = None, **fields) -> RegistryDoc   # preconditions checked inside the CAS closure: status in expect_status, entry identical to expect_entry as loaded (RegistryError, FrozenError if merging); `claims` merged set-by-key
    def release_overlay(self, name: str, final: Status, *, pending_revert: Iterable[str]|None = None, expect_status: Iterable[Status]|None = None) -> RegistryDoc   # move to tombstones, pending_revert kept (defaults to the entry's)
    def tombstoned(self, doc: RegistryDoc, name: str, days: int) -> bool
    def stale_applying(self, ov: Overlay, timeout_min: int) -> bool
```

## tofu.py

```python
FORBIDDEN_PASSTHROUGH = ("-target", "-replace", "-refresh-only", "-destroy", "-state", "-lock=false", "-lock=0")
def validate_passthrough(args: list[str]) -> None           # ToolError on forbidden (prefix match, also "--target")
class TofuRunner:
    def __init__(self, binary: str, cwd: Path, data_dir: Path, env: dict[str,str] | None = None, stream: Callable[[str],None] | None = None)
    def version(self) -> str
    def init(self, cfg: BackendConfig, key: str, *, reconfigure: bool = True) -> None   # `init -input=false -reconfigure -backend-config=FILE... -backend-config=key=KEY` (key LAST); sets TF_DATA_DIR, TF_PLUGIN_CACHE_DIR (default ~/.cache/tofu-overlay/plugins), TF_IN_AUTOMATION=1
    def needs_init(self, key: str) -> bool                  # data_dir/terraform.tfstate missing or its backend key != key or lock file changed
    def plan(self, out: Path, *, extra: list[str] = [], destroy: bool = False, targets: list[str] = [], refresh: bool = True) -> int   # returns exit code (0/2 with -detailed-exitcode); raises ToolError on 1
    def show_json(self, planfile: Path) -> dict
    def apply(self, planfile: Path, *, auto_approve: bool = True) -> None   # streams; a saved plan never prompts, confirmation is the caller's job; TF_CLI_ARGS* dropped from the env
    def state_pull(self) -> dict
    def state_push(self, doc: dict, *, force: bool = False) -> None   # writes temp file, `state push [-force] FILE`
    def state_rm(self, addresses: list[str]) -> None
    def ensure_backend_key(self, key: str) -> None          # verify data_dir/terraform.tfstate backend config key == key else ToolError
```

## state.py

```python
def address_of(resource: dict, instance: dict) -> str       # module.m.aws_x.y["k"] / [0] ; deposed ignored
def index_instances(doc: dict) -> dict[str, tuple[dict, dict]]   # address -> (resource_entry, instance)
def addresses(doc: dict) -> set[str]
def is_encrypted(doc: dict) -> bool                          # "encrypted_data" in doc
def new_lineage() -> str
def fork(doc: dict) -> dict                                  # deep copy, new lineage, serial 0
def inject_instances(dst: dict, src: dict, addresses: set[str]) -> dict   # merge per resource entry keyed (module, mode, type, name, provider); instance-level insert; ToolError if provider differs or schema_version(src) > existing same-type in dst; refuse if address already in dst
def remove_addresses(doc: dict, addresses: set[str]) -> dict
def bump_serial(doc: dict, at_least: int) -> dict            # serial = max(doc.serial, at_least) + 1
def attribute(inst: dict, path: str) -> Any                  # "metadata.0.name" style path over attributes
def identity_from_state(inst_attrs: dict, type_: str, knowledge: TypeKnowledge) -> dict[str, Any]
def strip_sensitive(after: Any, after_sensitive: Any) -> Any  # remove keys marked true in the parallel structure
```

## identity.py

```python
class TypeKnowledge:
    def __init__(self, identity: dict[str,list[str]], import_formats: dict[str,str], non_importable: list[str], replace_prone: list[str], virtual_attributes: dict[str,list[str]])
    @classmethod
    def load(cls, cfg: ToolConfig) -> "TypeKnowledge"       # package data YAML merged with cfg overrides (user wins)
    FALLBACK_IDENTITY_ATTRS = ("name","bucket","identifier","function_name","domain_name","cluster_identifier","cluster_id","replication_group_id","key")
    def identity_for(self, type_: str, attrs: dict) -> dict[str, Any]   # normalised (route53 name lowercase, no trailing dot); {} if nothing known/present
    def identity_key(self, type_: str, identity: dict) -> str | None    # canonical string "type|k=v|k=v" for comparisons; None if empty
    def import_id_for(self, type_: str, attrs: dict) -> str | None      # format from table, "{attr}" placeholders with dotted paths; default "{id}"; None if a placeholder is missing
    def is_importable(self, type_: str) -> bool             # not in non_importable (glob patterns like "random_*")
    def is_replace_prone(self, type_: str) -> bool
    def virtual_attrs(self, type_: str) -> set[str]
    def known(self, type_: str) -> bool                     # type has an explicit import format
```

`data/identity.yaml` and `data/import_ids.yaml` must cover at least: aws_s3_bucket*, aws_iam_role/policy/user, aws_iam_role_policy (`{role}:{name}`), aws_iam_role_policy_attachment (`{role}/{policy_arn}`), aws_iam_user_policy_attachment, aws_lambda_function/permission (`{function_name}/{statement_id}`)/alias/layer_version(replace_prone), aws_route53_record (`{zone_id}_{name}_{type}` + `_{set_identifier}`), aws_route53_zone, aws_cloudwatch_log_group, aws_cloudwatch_event_rule/target (`{event_bus_name}/{rule}/{target_id}`), aws_scheduler_schedule (`{group_name}/{name}`), aws_sqs_queue, aws_sns_topic, aws_dynamodb_table, aws_ecr_repository, aws_secretsmanager_secret, aws_ssm_parameter, aws_kms_key/alias, aws_security_group, aws_vpc_security_group_ingress_rule/egress_rule, aws_security_group_rule (format documented as complex, mark replace_prone-like "manual"), aws_lb/lb_target_group/lb_listener/lb_listener_rule, aws_db_instance, aws_rds_cluster, aws_elasticache_*, aws_eks_cluster/node_group/addon, aws_eks_pod_identity_association (`{cluster_name},{association_id}`), aws_eks_access_entry, aws_cloudfront_distribution/cache_policy/function, aws_acm_certificate, aws_acm_certificate_validation (non_importable), aws_wafv2_web_acl (`{id}/{name}/{scope}`), aws_wafv2_web_acl_association (`{web_acl_arn},{resource_arn}`), aws_api_gateway_rest_api/resource/method/integration/deployment/stage, aws_apigatewayv2_api, aws_cognito_user_pool/client, aws_s3_object (`{bucket}/{key}`), aws_route (`{route_table_id}_{destination_cidr_block}`), aws_route_table_association (`{subnet_id}/{route_table_id}`), aws_efs_file_system, aws_backup_*, aws_kms_grant (`{key_id}:{grant_id}`), aws_cloudwatch_log_subscription_filter (`{log_group_name}|{name}`), kubernetes_namespace, kubernetes_service_account_v1 (`{metadata.0.namespace}/{metadata.0.name}`), kubernetes_manifest (non_importable in v1), helm_release (`{namespace}/{name}`), random_* / null_resource / terraform_data / time_sleep / tls_private_key / local_file / archive_file / aws_lambda_invocation / aws_iam_policy_attachment / aws_lb_target_group_attachment / aws_dynamodb_table_item / aws_iam_access_key (non_importable). virtual_attributes: aws_lambda_function [filename, source_code_hash, publish, skip_destroy], aws_s3_bucket [force_destroy], aws_iam_role [force_detach_policies], aws_ecr_repository [force_delete], aws_kms_key [deletion_window_in_days, bypass_policy_lockout_safety_check], aws_db_instance/aws_rds_cluster [master_password, skip_final_snapshot, apply_immediately, final_snapshot_identifier, allow_major_version_upgrade], aws_secretsmanager_secret [recovery_window_in_days, force_overwrite_replica_secret], helm_release [values, set, wait, timeout, cleanup_on_fail], kubernetes_* [wait_for_default_secret, wait_for_rollout].

## plan.py

```python
def parse_plan(show_json: dict) -> tuple[list[ResourceChange], list[dict], PlanSummary]   # resource_changes, resource_drift, summary
def is_base_address(address: str, base_addresses: set[str]) -> bool
def evaluate(changes: list[ResourceChange], drift: list[dict], *, base_addresses: set[str], base_identities: set[str], me: Overlay, doc: RegistryDoc, knowledge: TypeKnowledge, registry: Registry) -> PolicyResult
    # implements DESIGN §7 items 2-6; claims: create -> Claim(kind=create, identity from strip_sensitive(after)...), update on base -> Claim(kind=update, after_hash=sha256(json(after)))
    # existing own claims are kept; an update on an address already claimed by me is fine
def verify_import_plan(changes: list[ResourceChange], *, me: Overlay, knowledge: TypeKnowledge, allow_import_updates: bool, accepted_recreate: set[str]|None = None) -> tuple[bool, list[str], list[str]]   # DESIGN §8: (ok, errors, warnings)
def guard_trunk_plan(changes: list[ResourceChange], *, doc: RegistryDoc, knowledge: TypeKnowledge, overlay_states: dict[str, dict] | None) -> list[Violation]   # DESIGN §6 guard
def render_summary(summary: PlanSummary) -> str
```

## output.py

```python
class Console:
    def __init__(self, *, json_mode: bool = False, ci: bool = False, no_color: bool = False)
    def info/warn/error/success(self, msg: str) -> None     # in CI+ADO: error/warn also emit ##vso[task.logissue type=error|warning]
    def backend_line(self, cfg: BackendConfig) -> None      # first line of every command
    def table(self, title: str, columns: list[str], rows: list[list[str]]) -> None
    def json(self, payload: dict) -> None                   # payload gets "schema": 1
    def stream(self, line: str) -> None                     # tofu output passthrough
    def confirm_typed(self, expected: str, prompt: str, *, yes: bool) -> bool   # user must type `expected`; in CI without --yes -> False
    def confirm(self, prompt: str, *, yes: bool) -> bool
```

## overlay.py

```python
class OverlayService:
    def __init__(self, cwd: Path, cfg: ToolConfig, backend: BackendConfig, console: Console, *, name: str | None = None, session=None, runner_factory=None)
    # properties: name, registry, store (StateStore built by store.make_store(backend, session=session)), knowledge, data_dir (.tofu-overlay/<name>), base_data_dir (.tofu-overlay/_base), repo_root
    # remote-state lookups on other bases build their store through make_store as well; archive detection uses backend.is_archive_key, prefix scans backend.overlay_prefix()
    def create(self, *, force_name: bool = False) -> Overlay
    def plan(self, *, extra: list[str] = [], allow_behind: bool = False, detailed_exitcode: bool = False) -> tuple[PolicyResult, PlanSummary, Path, bool]   # (policy, summary, planfile, stale)
    def apply(self, *, auto_approve: bool, allow_stale: bool, allow_behind: bool, extra: list[str] = [], yes: bool = False) -> Overlay   # console.confirm before acquire_claims unless auto_approve/yes; plan file unlinked afterwards
    def status(self) -> dict                                 # JSON-able
    def list(self) -> list[dict]
    def check(self) -> tuple[bool, list[str], list[str]]     # (ok, errors, warnings); "no overlay for branch" -> (True, [], ["no overlay"])
    def rebase(self, *, yes: bool) -> Overlay
    def abandon(self, *, keep_resources: bool, dry_run: bool, yes: bool) -> None
    def finalize(self, *, purge: bool, yes: bool) -> None
    def gc(self, *, purge: bool, yes: bool) -> list[dict]
    def doctor(self) -> list[dict]                          # findings [{level, code, message}]
    def remote_overlay_keys(self, name: str) -> dict[str, str]   # {ref base key -> overlay state_key} for every resolved remote_state ref whose base registry holds a live overlay `name` with an existing object; own base ignored; missing registry = {}; registry error = warning + skip. Read-only. Exported as TF_VAR_tofu_overlay_keys (JSON) with TF_VAR_tofu_overlay_name to every tofu run
    # internals expected (private): _validate_overlay(ov) (§3.7), _freshness(ov) -> (stale: bool, current_etag), _base_addresses(refresh: bool) cached under base_data_dir/base_addresses.json, _base_identities(), _pull_base(), _runner(data_dir) etc.
```

## merge.py

```python
IMPORTS_FILENAME = "zz_overlay_{name}.imports.tf"
def render_imports(ov: Overlay, *, base: BackendConfig, base_etag: str, commit: str, generated_at: str) -> str   # sorted by address; header comment; per block `# identity: k=v`
def write_imports(env_dir: Path, name: str, content: str) -> Path   # refuse if env_dir is a symlink or target exists as symlink
def read_imports_addresses(path: Path) -> dict[str, str]     # address -> id parsed back from the file (regex over import blocks)
class MergeService:                                        # reaches the store only through svc.store / svc.registry (StateStore-typed)
    def __init__(self, svc: OverlayService)
    def merge(self, *, allow_import_updates: bool, accept_recreate: list[str], allow_unapplied: bool, yes: bool) -> Path
    def undo(self, *, yes: bool) -> None
    def verify(self, *, accepted: set[str]|None = None) -> tuple[bool, list[str], list[str]]    # plan branch config against base state in base data dir, then plan.verify_import_plan; accepted defaults to the create claims absent from the imports file
def guard(plan_json_path: Path, *, registry: Registry, knowledge: TypeKnowledge) -> list[Violation]
```

## cli.py

Typer app `tofu-overlay` with commands: create, plan, apply, status, list, check, rebase, merge, finalize, abandon, doctor, guard, gc, version. Never imports `s3state`: `guard` and `list --prefix` obtain their store through `store.make_store(cfg, session=INJECT["session"])`. Global options: `-C/--chdir`, `--name`, `--backend-config` (repeatable), `--bucket/--key/--region/--profile/--dynamodb-table`, `--json`, `--no-color`, `--yes`, `--print-backend`, `-v/--verbose`. Every command: build Console, resolve backend, echo backend line (not in `--json`), enforce allowed_base_keys for mutating commands, map `OverlayError.exit_code` to the process exit code, unexpected exceptions → exit 1 with message. `plan` passes anything after `--` to tofu after `validate_passthrough`.
