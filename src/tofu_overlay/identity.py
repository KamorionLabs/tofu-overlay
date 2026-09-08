"""Type knowledge: identity attributes, import id formats, importability flags.

The package ships two YAML files (``data/identity.yaml`` and
``data/import_ids.yaml``); user overrides from ``.tofu-overlay.yaml`` are
merged on top (user wins). Everything here is pure: no I/O beyond reading the
package data at load time.
"""

from __future__ import annotations

import fnmatch
import json
import re
from importlib import resources
from typing import Any

import yaml

from tofu_overlay.models import ToolConfig, ToolError

_PACKAGE = "tofu_overlay"
_IDENTITY_FILE = "identity.yaml"
_IMPORT_IDS_FILE = "import_ids.yaml"

_PLACEHOLDER = re.compile(r"\{([^{}]+)\}")
_OPTIONAL_GROUP = re.compile(r"\[([^\[\]]*)\]")

_MISSING = object()


def _read_package_yaml(name: str) -> dict[str, Any]:
    """Read one YAML document shipped under ``tofu_overlay/data``."""
    path = resources.files(_PACKAGE).joinpath("data", name)
    try:
        with path.open("r", encoding="utf-8") as fh:
            doc = yaml.safe_load(fh) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ToolError(f"cannot read package data {name}: {exc}") from exc
    if not isinstance(doc, dict):
        raise ToolError(f"package data {name} must be a mapping")
    return doc


def _as_str_list(value: Any, what: str) -> list[str]:
    """Coerce a YAML value into a list of strings, raising on anything else."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return list(value)
    raise ToolError(f"{what} must be a list of strings, got {value!r}")


def _as_str_list_map(value: Any, what: str) -> dict[str, list[str]]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ToolError(f"{what} must be a mapping of type -> list of attributes")
    return {str(k): _as_str_list(v, f"{what}[{k}]") for k, v in value.items()}


def _as_str_map(value: Any, what: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict) or not all(isinstance(v, str) for v in value.values()):
        raise ToolError(f"{what} must be a mapping of type -> format string")
    return {str(k): v for k, v in value.items()}


def _get_path(attrs: Any, path: str) -> Any:
    """Resolve a dotted path (``metadata.0.name``) over nested dicts/lists.

    Returns ``_MISSING`` when any segment is absent, ``None`` when the final
    value is present but null.
    """
    cur = attrs
    for part in path.split("."):
        if isinstance(cur, dict):
            if part not in cur:
                return _MISSING
            cur = cur[part]
        elif isinstance(cur, list) and part.isdigit():
            idx = int(part)
            if idx >= len(cur):
                return _MISSING
            cur = cur[idx]
        else:
            return _MISSING
    return cur


def _normalise(type_: str, attr: str, value: Any) -> Any:
    """Canonicalise an identity value so equal objects compare equal."""
    if isinstance(value, str):
        leaf = attr.rsplit(".", 1)[-1]
        if type_.startswith("aws_route53") and leaf == "name":
            return value.rstrip(".").lower()
        if type_ == "aws_route53_record" and leaf == "type":
            return value.upper()
        if leaf in ("domain_name", "fqdn") and not type_.startswith("aws_api_gateway"):
            return value.rstrip(".").lower()
        return value
    if isinstance(value, bool):
        return value
    if isinstance(value, list):
        return sorted(value, key=lambda v: json.dumps(v, sort_keys=True, default=str))
    return value


def _placeholder_value(value: Any) -> str | None:
    """Render an attribute value inside an import id, or None if impossible."""
    if value is None or value is _MISSING:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (str, int, float)):
        return str(value)
    return None


class TypeKnowledge:
    """Per-type knowledge used for claims, conflicts and import generation."""

    FALLBACK_IDENTITY_ATTRS = (
        "name",
        "bucket",
        "identifier",
        "function_name",
        "domain_name",
        "cluster_identifier",
        "cluster_id",
        "replication_group_id",
        "key",
    )

    def __init__(
        self,
        identity: dict[str, list[str]],
        import_formats: dict[str, str],
        non_importable: list[str],
        replace_prone: list[str],
        virtual_attributes: dict[str, list[str]],
    ) -> None:
        self.identity: dict[str, list[str]] = {k: list(v) for k, v in identity.items()}
        self.import_formats: dict[str, str] = dict(import_formats)
        self.non_importable: list[str] = list(dict.fromkeys(non_importable))
        self.replace_prone: list[str] = list(dict.fromkeys(replace_prone))
        self.virtual_attributes: dict[str, list[str]] = {
            k: list(dict.fromkeys(v)) for k, v in virtual_attributes.items()
        }

    # ------------------------------------------------------------------ load
    @classmethod
    def load(cls, cfg: ToolConfig) -> TypeKnowledge:
        """Load package data and merge the user configuration on top.

        Per-type identity lists and import formats from the user replace the
        package entries; virtual attribute lists, ``non_importable`` and
        ``replace_prone`` are unioned with the package lists.
        """
        identity_doc = _read_package_yaml(_IDENTITY_FILE)
        import_doc = _read_package_yaml(_IMPORT_IDS_FILE)

        identity = _as_str_list_map(identity_doc.get("identity"), "identity")
        identity.update(_as_str_list_map(cfg.identity, "config identity"))

        formats = _as_str_map(import_doc.get("formats"), "formats")
        formats.update(_as_str_map(cfg.import_ids, "config import_ids"))

        non_importable = _as_str_list(import_doc.get("non_importable"), "non_importable")
        non_importable += _as_str_list(cfg.non_importable, "config non_importable")

        replace_prone = _as_str_list(import_doc.get("replace_prone"), "replace_prone")
        replace_prone += _as_str_list(cfg.replace_prone, "config replace_prone")

        virtual = _as_str_list_map(import_doc.get("virtual_attributes"), "virtual_attributes")
        for type_, attrs in _as_str_list_map(
            cfg.virtual_attributes, "config virtual_attributes"
        ).items():
            virtual[type_] = list(dict.fromkeys(virtual.get(type_, []) + attrs))

        return cls(identity, formats, non_importable, replace_prone, virtual)

    # -------------------------------------------------------------- identity
    def identity_for(self, type_: str, attrs: dict) -> dict[str, Any]:
        """Return the normalised identity of a resource from its attributes.

        With an explicit attribute list every listed attribute must be present
        (absent keys mean "unknown at plan time"); null values are skipped. A
        partial identity is never returned because it would produce false
        conflicts. Without an explicit list the first present fallback
        attribute is used. ``{}`` means "no identity known".
        """
        if not isinstance(attrs, dict) or not attrs:
            return {}
        paths = self.identity.get(type_)
        if paths:
            return self._explicit_identity(type_, attrs, paths)
        return self._fallback_identity(type_, attrs)

    def _explicit_identity(self, type_: str, attrs: dict, paths: list[str]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for path in paths:
            value = _get_path(attrs, path)
            if value is _MISSING:
                return {}
            if value is None or value == [] or value == {}:
                continue
            result[path] = _normalise(type_, path, value)
        return result

    def _fallback_identity(self, type_: str, attrs: dict) -> dict[str, Any]:
        for attr in self.FALLBACK_IDENTITY_ATTRS:
            value = attrs.get(attr)
            if isinstance(value, str) and value:
                return {attr: _normalise(type_, attr, value)}
        return {}

    def identity_key(self, type_: str, identity: dict) -> str | None:
        """Canonical ``type|k=v|k=v`` string for comparisons; None if empty."""
        if not identity:
            return None
        # Same rendering as registry._identity_key so both sides agree.
        return "|".join([type_, *(f"{k}={identity[k]}" for k in sorted(identity))])

    # ------------------------------------------------------------- import id
    def import_id_for(self, type_: str, attrs: dict) -> str | None:
        """Build the import id from the type's format and the attributes.

        Placeholders are dotted attribute paths. Optional ``[...]`` groups are
        dropped when any placeholder inside them is missing or empty. Returns
        None when a mandatory placeholder cannot be resolved.
        """
        if not isinstance(attrs, dict):
            return None
        fmt = self.import_formats.get(type_, "{id}")
        fmt = _OPTIONAL_GROUP.sub(lambda m: self._render_group(m.group(1), attrs), fmt)
        missing = False

        def _sub(match: re.Match[str]) -> str:
            nonlocal missing
            rendered = _placeholder_value(_get_path(attrs, match.group(1)))
            if rendered is None:
                missing = True
                return ""
            return rendered

        result = _PLACEHOLDER.sub(_sub, fmt)
        return None if missing else result

    @staticmethod
    def _render_group(group: str, attrs: dict) -> str:
        values: dict[str, str] = {}
        for name in _PLACEHOLDER.findall(group):
            rendered = _placeholder_value(_get_path(attrs, name))
            # SDKv2 stores an unset optional string as "": the group is absent.
            if rendered is None or rendered == "":
                return ""
            values[name] = rendered
        return _PLACEHOLDER.sub(lambda m: values[m.group(1)], group)

    # ----------------------------------------------------------------- flags
    @staticmethod
    def _matches(type_: str, patterns: list[str]) -> bool:
        return any(type_ == p or fnmatch.fnmatchcase(type_, p) for p in patterns)

    def is_importable(self, type_: str) -> bool:
        """True unless the type matches a ``non_importable`` pattern."""
        return not self._matches(type_, self.non_importable)

    def is_replace_prone(self, type_: str) -> bool:
        """True when importing the type almost always leads to a replacement."""
        return self._matches(type_, self.replace_prone)

    def virtual_attrs(self, type_: str) -> set[str]:
        """Attributes that never round-trip through the provider for this type."""
        result: set[str] = set()
        for pattern, attrs in self.virtual_attributes.items():
            if type_ == pattern or fnmatch.fnmatchcase(type_, pattern):
                result.update(attrs)
        return result

    def known(self, type_: str) -> bool:
        """True when the type has an explicit import id format."""
        return type_ in self.import_formats
