"""Backend resolution for the ``s3`` backend.

Resolution order (highest precedence first): explicit overrides (CLI flags),
``TOFU_OVERLAY_*`` environment variables, ``-backend-config`` files, the backend
cached by ``init`` in ``<data_dir>/terraform.tfstate``, and finally the
``terraform { backend "s3" {} }`` block found in the stack's ``*.tf`` files.

Anything that is not an ``s3`` backend, any non-default workspace and any
unresolved ``${...}`` value is refused with a :class:`BackendResolutionError`.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import hcl2

from tofu_overlay.models import BackendConfig, RemoteStateRef, ToolError

__all__ = [
    "BackendResolutionError",
    "parse_hcl_backend",
    "parse_remote_state_refs",
    "parse_backend_config_files",
    "read_cached_backend",
    "resolve_backend",
    "describe",
]

SUPPORTED_BACKEND = "s3"
BLOCK_MARKER = "__is_block__"
REMOTE_STATE_TYPE = "terraform_remote_state"
OVERLAY_KEYS_VAR = "tofu_overlay_keys"
# The MULTI-STACK.md contract: key = lookup(var.tofu_overlay_keys, "<base key>", "<base key>").
_OVERLAY_LOOKUP_RE = re.compile(
    r'^\$\{\s*lookup\(\s*var\.' + OVERLAY_KEYS_VAR
    + r'\s*,\s*"(?P<key>[^"]+)"\s*(?:,\s*"[^"]*"\s*)?\)\s*\}$'
)
ENV_OVERRIDES = {
    "TOFU_OVERLAY_BUCKET": "bucket",
    "TOFU_OVERLAY_KEY": "key",
    "TOFU_OVERLAY_REGION": "region",
    "TOFU_OVERLAY_PROFILE": "profile",
    "TOFU_OVERLAY_DYNAMODB_TABLE": "dynamodb_table",
}
_KV_LINE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_-]*)\s*=\s*(.*?)\s*$")
_COMMENT_LINE = re.compile(r"^\s*(#|//)")


class BackendResolutionError(ToolError):
    """The backend could not be resolved to a usable ``s3`` configuration."""


# --------------------------------------------------------------------------- #
# python-hcl2 normalisation
# --------------------------------------------------------------------------- #


def _unquote(value: Any) -> Any:
    """Strip the literal quotes python-hcl2 8.x wraps string values in."""
    if isinstance(value, str) and len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        return value[1:-1]
    return value


def _clean(value: Any) -> Any:
    """Recursively unquote strings and drop ``__is_block__`` markers."""
    if isinstance(value, dict):
        return {
            _unquote(k): _clean(v)
            for k, v in value.items()
            if k != BLOCK_MARKER
        }
    if isinstance(value, list):
        return [_clean(v) for v in value]
    return _unquote(value)


def _blocks(node: Any, name: str) -> list[dict]:
    """Return every ``name`` block of a parsed HCL node as a list of dicts."""
    if not isinstance(node, dict):
        return []
    found = node.get(name)
    if found is None:
        return []
    if isinstance(found, dict):
        return [found]
    return [b for b in found if isinstance(b, dict)]


def _backend_entries(parsed: dict) -> list[tuple[str, dict]]:
    """Yield ``(backend_type, attrs)`` for every backend block of a parsed file."""
    entries: list[tuple[str, dict]] = []
    for tf_block in _blocks(parsed, "terraform"):
        for backend in _blocks(tf_block, "backend"):
            for label, body in backend.items():
                if label == BLOCK_MARKER:
                    continue
                attrs = _clean(body) if isinstance(body, dict) else {}
                entries.append((_unquote(label), attrs))
    return entries


def _parse_tf_file(path: Path) -> dict | None:
    """Parse one ``*.tf`` file; files without a backend block may fail silently."""
    text = path.read_text(encoding="utf-8")
    try:
        return hcl2.loads(text)
    except Exception as exc:  # lark raises many exception types
        if "backend" in text:
            raise BackendResolutionError(f"cannot parse {path}: {exc}") from exc
        return None


def parse_hcl_backend(dir: Path) -> dict | None:  # noqa: A002 - name fixed by contract
    """Find the ``terraform { backend "s3" {} }`` block in ``dir``'s ``*.tf`` files.

    Returns the backend attributes as a plain dict (quotes and block markers
    stripped, unresolved expressions kept verbatim as ``${...}``), or ``None``
    when no backend block exists. A backend of another type is refused.
    """
    dir = Path(dir)
    if not dir.is_dir():
        return None
    result: dict | None = None
    for tf in sorted(dir.glob("*.tf")):
        if not tf.is_file():
            continue
        parsed = _parse_tf_file(tf)
        if not parsed:
            continue
        for backend_type, attrs in _backend_entries(parsed):
            if backend_type != SUPPORTED_BACKEND:
                raise BackendResolutionError(
                    f"{tf.name} declares a {backend_type!r} backend; only "
                    f"{SUPPORTED_BACKEND!r} is supported"
                )
            if result is not None:
                raise BackendResolutionError(
                    f"several backend blocks found (last one in {tf.name})"
                )
            result = attrs
    return result


# --------------------------------------------------------------------------- #
# terraform_remote_state references
# --------------------------------------------------------------------------- #


def _literal(value: Any) -> str | None:
    """A cleaned HCL string that carries no ``${...}`` expression, else ``None``."""
    if isinstance(value, str) and "${" not in value:
        return value
    return None


def _ref_key(value: Any) -> str | None:
    """Literal key, or the map key of the ``lookup(var.tofu_overlay_keys, ...)`` contract."""
    literal = _literal(value)
    if literal is not None:
        return literal
    if isinstance(value, str):
        match = _OVERLAY_LOOKUP_RE.match(value.strip())
        if match:
            return match.group("key")
    return None


def _remote_state_entries(parsed: dict) -> list[tuple[str, dict]]:
    """Yield ``(name, attrs)`` for every ``data "terraform_remote_state"`` block."""
    entries: list[tuple[str, dict]] = []
    for data_block in _blocks(parsed, "data"):
        for type_label, by_name in data_block.items():
            if type_label == BLOCK_MARKER or _unquote(type_label) != REMOTE_STATE_TYPE:
                continue
            if not isinstance(by_name, dict):
                continue
            for name_label, body in by_name.items():
                if name_label == BLOCK_MARKER:
                    continue
                attrs = _clean(body) if isinstance(body, dict) else {}
                entries.append((_unquote(name_label), attrs))
    return entries


def _remote_config(attrs: dict) -> dict:
    """The ``config`` map of a remote_state block (attribute or block syntax)."""
    cfg = attrs.get("config")
    if isinstance(cfg, list):
        cfg = next((c for c in cfg if isinstance(c, dict)), None)
    return cfg if isinstance(cfg, dict) else {}


def _to_remote_ref(name: str, attrs: dict) -> RemoteStateRef:
    cfg = _remote_config(attrs)
    bucket_raw, key_raw = cfg.get("bucket"), cfg.get("key")
    bucket, key = _literal(bucket_raw), _ref_key(key_raw)
    workspace = _literal(cfg.get("workspace") or attrs.get("workspace")) or "default"
    unresolved = key is None or (bucket_raw is not None and bucket is None) or (
        workspace != "default"
    )
    return RemoteStateRef(
        name=name,
        bucket=bucket,
        key=None if unresolved else key,
        region=_literal(cfg.get("region")),
        unresolved=unresolved,
    )


def parse_remote_state_refs(dir: Path) -> list[RemoteStateRef]:  # noqa: A002 - contract
    """Every ``data "terraform_remote_state"`` block with an ``s3`` backend in ``dir``.

    Symlinked ``*.tf`` files are followed. Blocks with another backend are
    skipped. A key or bucket given as an expression (other than the
    ``lookup(var.tofu_overlay_keys, "<key>", ...)`` contract) yields an
    ``unresolved`` reference with ``key`` set to ``None``.
    """
    dir = Path(dir)
    if not dir.is_dir():
        return []
    refs: list[RemoteStateRef] = []
    for tf in sorted(dir.glob("*.tf")):
        if not tf.is_file():
            continue
        parsed = _parse_tf_file(tf)
        if not parsed:
            continue
        for name, attrs in _remote_state_entries(parsed):
            if _literal(attrs.get("backend")) != SUPPORTED_BACKEND:
                continue
            refs.append(_to_remote_ref(name, attrs))
    return refs


# --------------------------------------------------------------------------- #
# -backend-config files
# --------------------------------------------------------------------------- #


def _coerce_scalar(raw: str) -> Any:
    """Turn an unquoted ``key=value`` right-hand side into a Python scalar."""
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        return raw[1:-1]
    lowered = raw.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if re.fullmatch(r"-?\d+", raw):
        return int(raw)
    return raw


def _strip_trailing_comment(raw: str) -> str:
    """Drop a trailing ``# ...`` / ``// ...`` comment outside of quotes."""
    if raw and raw[0] in "\"'":
        end = raw.find(raw[0], 1)
        if end != -1:
            return raw[: end + 1]
        return raw
    for marker in (" #", "\t#", " //", "\t//"):
        pos = raw.find(marker)
        if pos != -1:
            raw = raw[:pos]
    return raw.strip()


def _parse_kv_lines(text: str) -> dict | None:
    """Parse ``key = value`` lines; ``None`` when a line is not of that shape."""
    result: dict[str, Any] = {}
    for line in text.splitlines():
        if not line.strip() or _COMMENT_LINE.match(line):
            continue
        match = _KV_LINE.match(line)
        if not match or "{" in match.group(2):
            return None
        result[match.group(1)] = _coerce_scalar(_strip_trailing_comment(match.group(2)))
    return result


def _parse_config_text(text: str, source: str) -> dict:
    """Parse the content of one ``-backend-config`` file (key=value or HCL)."""
    simple = _parse_kv_lines(text)
    if simple is not None:
        return simple
    try:
        return _clean(hcl2.loads(text))
    except Exception as exc:
        raise BackendResolutionError(f"cannot parse backend config {source}: {exc}") from exc


def _parse_inline_pair(item: str) -> dict:
    """``-backend-config=key=value`` given inline instead of a file."""
    match = _KV_LINE.match(item)
    if not match:
        raise BackendResolutionError(f"backend config {item!r}: not a file nor key=value")
    return {match.group(1): _coerce_scalar(match.group(2))}


def parse_backend_config_files(files: list[Path]) -> dict:
    """Merge ``-backend-config`` inputs in order (later entries win).

    Each entry is a file in tofu's ``-backend-config=FILE`` syntax (HCL
    attributes or bare ``key=value`` lines). Like tofu itself, an entry that is
    not an existing file but looks like ``key=value`` is taken as an inline pair.
    """
    merged: dict[str, Any] = {}
    for item in files:
        path = Path(item)
        if path.is_file():
            merged.update(_parse_config_text(path.read_text(encoding="utf-8"), str(path)))
        elif "=" in str(item):
            merged.update(_parse_inline_pair(str(item)))
        else:
            raise BackendResolutionError(f"backend config file not found: {path}")
    return merged


# --------------------------------------------------------------------------- #
# cached backend
# --------------------------------------------------------------------------- #


def read_cached_backend(data_dir: Path) -> dict | None:
    """Read the backend cached by ``init`` in ``<data_dir>/terraform.tfstate``.

    Returns the ``backend.config`` dict without ``null`` entries, ``None`` when
    the file is missing or unreadable. A cached backend of another type is
    refused.
    """
    path = Path(data_dir) / "terraform.tfstate"
    if not path.is_file():
        return None
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    backend = doc.get("backend") if isinstance(doc, dict) else None
    if not isinstance(backend, dict):
        return None
    backend_type = backend.get("type")
    if backend_type and backend_type != SUPPORTED_BACKEND:
        raise BackendResolutionError(
            f"{path} caches a {backend_type!r} backend; only {SUPPORTED_BACKEND!r} is supported"
        )
    config = backend.get("config")
    if not isinstance(config, dict):
        return None
    return {k: v for k, v in config.items() if v is not None}


# --------------------------------------------------------------------------- #
# resolution
# --------------------------------------------------------------------------- #


def _env_overrides(environ: dict[str, str]) -> dict:
    return {attr: environ[var] for var, attr in ENV_OVERRIDES.items() if environ.get(var)}


def _current_workspace(cwd: Path, data_dir: Path | None, environ: dict[str, str]) -> str:
    """Workspace from ``TF_WORKSPACE`` or the ``environment`` marker file."""
    if environ.get("TF_WORKSPACE"):
        return environ["TF_WORKSPACE"].strip()
    candidates = [Path(cwd) / ".terraform" / "environment"]
    if data_dir is not None:
        candidates.append(Path(data_dir) / "environment")
    for marker in candidates:
        if marker.is_file():
            value = marker.read_text(encoding="utf-8").strip()
            if value:
                return value
    return "default"


def _as_bool(value: Any, attr: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in ("true", "false"):
        return value.lower() == "true"
    raise BackendResolutionError(f"backend attribute {attr!r} must be a boolean, got {value!r}")


def _as_str(value: Any, attr: str) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, str | int):
        raise BackendResolutionError(f"backend attribute {attr!r} must be a string, got {value!r}")
    text = str(value).strip()
    if "${" in text:
        raise BackendResolutionError(
            f"backend attribute {attr!r} is unresolved ({text}); pass it with "
            f"--backend-config or --{attr.replace('_', '-')}"
        )
    return text or None


def _merge_layers(*layers: dict | None) -> dict:
    """Merge dicts left to right; ``None`` values never override."""
    merged: dict[str, Any] = {}
    for layer in layers:
        for k, v in (layer or {}).items():
            if v is not None:
                merged[k] = v
    return merged


def resolve_backend(
    cwd: Path,
    *,
    overrides: dict,
    backend_config_files: list[Path],
    data_dir: Path | None,
) -> BackendConfig:
    """Resolve the ``s3`` backend of the stack in ``cwd``.

    Precedence: ``overrides`` > ``TOFU_OVERLAY_*`` env > ``backend_config_files``
    > cached backend in ``data_dir`` > HCL block. ``bucket`` and ``key`` are
    mandatory; unresolved ``${...}`` values and non-default workspaces are
    refused.
    """
    cwd = Path(cwd)
    environ = dict(os.environ)
    workspace = _current_workspace(cwd, data_dir, environ)
    if workspace != "default":
        raise BackendResolutionError(
            f"workspace {workspace!r} is selected; only the default workspace is supported"
        )
    layers = (
        parse_hcl_backend(cwd),
        read_cached_backend(data_dir) if data_dir is not None else None,
        parse_backend_config_files(backend_config_files),
        _env_overrides(environ),
        overrides,
    )
    if layers[0] is None and not any(layers[1:]):
        raise BackendResolutionError(
            f"no s3 backend found in {cwd}; pass --bucket/--key or --backend-config"
        )
    merged = _merge_layers(*layers)
    bucket = _as_str(merged.get("bucket"), "bucket")
    key = _as_str(merged.get("key"), "key")
    if not bucket or not key:
        missing = [n for n, v in (("bucket", bucket), ("key", key)) if not v]
        raise BackendResolutionError(f"backend is missing {', '.join(missing)}")
    return BackendConfig(
        bucket=bucket,
        key=key,
        region=_as_str(merged.get("region"), "region"),
        profile=_as_str(merged.get("profile"), "profile"),
        dynamodb_table=_as_str(merged.get("dynamodb_table"), "dynamodb_table"),
        use_lockfile=_as_bool(merged.get("use_lockfile", False), "use_lockfile"),
        encrypt=_as_bool(merged.get("encrypt", True), "encrypt"),
        kms_key_id=_as_str(merged.get("kms_key_id"), "kms_key_id"),
        workspace="default",
        backend_config_files=[str(f) for f in backend_config_files],
    )


def describe(cfg: BackendConfig) -> str:
    """One-line human description: ``s3://bucket/key (region, profile, table, lockfile)``."""
    details = [
        f"region={cfg.region or '-'}",
        f"profile={cfg.profile or '-'}",
        f"table={cfg.dynamodb_table or '-'}",
        f"lockfile={'yes' if cfg.use_lockfile else 'no'}",
    ]
    if cfg.kms_key_id:
        details.append(f"kms={cfg.kms_key_id}")
    if cfg.workspace != "default":
        details.append(f"workspace={cfg.workspace}")
    return f"s3://{cfg.bucket}/{cfg.key} ({', '.join(details)})"
