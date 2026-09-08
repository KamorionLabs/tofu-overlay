"""Helpers over OpenTofu/Terraform state documents (format version 4).

Functions never mutate their input; they return new documents. Addresses are
formatted exactly like the binary does (``module.a.module.b.type.name["k"]``,
``type.name[0]``). Deposed instances are ignored when indexing.
"""

from __future__ import annotations

import copy
import json
import uuid
from typing import TYPE_CHECKING, Any

from tofu_overlay.models import ToolError

if TYPE_CHECKING:
    from tofu_overlay.identity import TypeKnowledge

_ResourceKey = tuple[str | None, str, str, str]


# ------------------------------------------------------------------ addresses


def _format_index(index_key: Any) -> str:
    if index_key is None:
        return ""
    if isinstance(index_key, bool):
        return f'["{str(index_key).lower()}"]'
    if isinstance(index_key, int):
        return f"[{index_key}]"
    if isinstance(index_key, float) and index_key.is_integer():
        return f"[{int(index_key)}]"
    return f"[{json.dumps(str(index_key))}]"


def address_of(resource: dict, instance: dict) -> str:
    """Return the tofu address of ``instance`` inside ``resource`` (deposed ignored)."""
    module = resource.get("module")
    prefix = f"{module}." if module else ""
    mode = "data." if resource.get("mode") == "data" else ""
    base = f"{prefix}{mode}{resource['type']}.{resource['name']}"
    return base + _format_index(instance.get("index_key"))


def index_instances(doc: dict) -> dict[str, tuple[dict, dict]]:
    """Map every current (non-deposed) instance address to ``(resource_entry, instance)``."""
    index: dict[str, tuple[dict, dict]] = {}
    for resource in doc.get("resources") or []:
        for instance in resource.get("instances") or []:
            if instance.get("deposed"):
                continue
            index[address_of(resource, instance)] = (resource, instance)
    return index


def addresses(doc: dict) -> set[str]:
    """Set of current instance addresses in ``doc``."""
    return set(index_instances(doc))


# ------------------------------------------------------------- lineage/serial


def is_encrypted(doc: dict) -> bool:
    """True when the document is a client-side encrypted state (out of scope in v1)."""
    return "encrypted_data" in doc


def new_lineage() -> str:
    """Fresh state lineage (uuid4)."""
    return str(uuid.uuid4())


def fork(doc: dict) -> dict:
    """Deep copy of ``doc`` with a new lineage and serial 0 (DESIGN §6 create)."""
    if is_encrypted(doc):
        raise ToolError("cannot fork an encrypted state document")
    new = copy.deepcopy(doc)
    new["lineage"] = new_lineage()
    new["serial"] = 0
    return new


def bump_serial(doc: dict, at_least: int) -> dict:
    """Return a copy of ``doc`` with ``serial = max(doc.serial, at_least) + 1``."""
    new = dict(doc)
    current = int(doc.get("serial") or 0)
    new["serial"] = max(current, int(at_least)) + 1
    return new


# ------------------------------------------------------------ inject/remove


def _resource_key(resource: dict) -> _ResourceKey:
    return (
        resource.get("module") or None,
        resource.get("mode", "managed"),
        resource["type"],
        resource["name"],
    )


def _find_entry(doc: dict, key: _ResourceKey) -> dict | None:
    for resource in doc.get("resources") or []:
        if _resource_key(resource) == key:
            return resource
    return None


def _max_schema_version(doc: dict, type_: str, provider: str | None) -> int | None:
    """Highest schema_version among ``doc`` instances of ``type_`` (same provider)."""
    versions = [
        int(instance.get("schema_version") or 0)
        for resource in doc.get("resources") or []
        if resource.get("type") == type_ and resource.get("provider") == provider
        for instance in resource.get("instances") or []
    ]
    return max(versions) if versions else None


def _check_schema_version(dst: dict, resource: dict, instance: dict, address: str) -> None:
    existing = _max_schema_version(dst, resource["type"], resource.get("provider"))
    incoming = int(instance.get("schema_version") or 0)
    if existing is not None and incoming > existing:
        raise ToolError(
            f"{address}: schema_version {incoming} is newer than the destination's "
            f"{existing} for {resource['type']} (provider too old on the destination)"
        )


def _entry_for(dst: dict, resource: dict, address: str) -> dict:
    """Return the destination entry matching ``resource``, creating it if absent."""
    entry = _find_entry(dst, _resource_key(resource))
    if entry is not None:
        if entry.get("provider") != resource.get("provider"):
            raise ToolError(
                f"{address}: provider differs ({resource.get('provider')!r} vs "
                f"{entry.get('provider')!r} in destination)"
            )
        return entry
    entry = {k: copy.deepcopy(v) for k, v in resource.items() if k != "instances"}
    entry["instances"] = []
    dst.setdefault("resources", []).append(entry)
    return entry


def inject_instances(dst: dict, src: dict, addresses: set[str]) -> dict:
    """Return a copy of ``dst`` with the ``addresses`` instances of ``src`` inserted.

    Instances are merged per resource entry (module, mode, type, name); the
    provider must match and the incoming ``schema_version`` must not exceed the
    destination's for that type. An address already present in ``dst`` or absent
    from ``src`` is an error.
    """
    new = copy.deepcopy(dst)
    src_index = index_instances(src)
    dst_addresses = set(index_instances(new))
    for address in sorted(addresses):
        if address not in src_index:
            raise ToolError(f"{address}: not found in source state")
        if address in dst_addresses:
            raise ToolError(f"{address}: already present in destination state")
        resource, instance = src_index[address]
        _check_schema_version(new, resource, instance, address)
        entry = _entry_for(new, resource, address)
        entry["instances"].append(copy.deepcopy(instance))
        dst_addresses.add(address)
    return new


def remove_addresses(doc: dict, addresses: set[str]) -> dict:
    """Return a copy of ``doc`` without ``addresses`` (deposed instances included).

    Unknown addresses are ignored; resource entries left empty are dropped.
    """
    new = copy.deepcopy(doc)
    kept: list[dict] = []
    for resource in new.get("resources") or []:
        resource["instances"] = [
            instance
            for instance in resource.get("instances") or []
            if address_of(resource, instance) not in addresses
        ]
        if resource["instances"]:
            kept.append(resource)
    new["resources"] = kept
    return new


# --------------------------------------------------------------- attributes


def _walk(value: Any, segments: list[str]) -> Any:
    for segment in segments:
        if isinstance(value, dict):
            if segment in value:
                value = value[segment]
            elif segment.isdigit() and int(segment) in value:
                value = value[int(segment)]
            else:
                return None
        elif isinstance(value, list):
            if not segment.lstrip("-").isdigit():
                return None
            index = int(segment)
            if index < 0 or index >= len(value):
                return None
            value = value[index]
        else:
            return None
    return value


def attribute(inst: dict, path: str) -> Any:
    """Resolve a dotted path (``metadata.0.name``) over an instance's attributes.

    ``inst`` may be a state instance (``{"attributes": {...}}``) or the attributes
    dict itself; list segments must be integers. Missing paths yield ``None``.
    """
    segments = [s for s in path.split(".") if s != ""]
    if not segments:
        return None
    attrs = inst.get("attributes")
    if isinstance(attrs, dict):
        found = _walk(attrs, segments)
        if found is not None:
            return found
    return _walk(inst, segments)


def identity_from_state(inst_attrs: dict, type_: str, knowledge: TypeKnowledge) -> dict[str, Any]:
    """Identity of a state instance (normalised by the type knowledge); ``{}`` if unknown."""
    return knowledge.identity_for(type_, inst_attrs)


# ---------------------------------------------------------------- sensitive


def strip_sensitive(after: Any, after_sensitive: Any) -> Any:
    """Remove values flagged ``true`` in the parallel ``after_sensitive`` structure.

    Dict keys are dropped, list elements are dropped, a wholly sensitive value
    becomes ``None``. Structures are never mutated in place.
    """
    if after_sensitive is True:
        return None
    if isinstance(after, dict):
        marks = after_sensitive if isinstance(after_sensitive, dict) else {}
        return {
            k: strip_sensitive(v, marks.get(k))
            for k, v in after.items()
            if marks.get(k) is not True
        }
    if isinstance(after, list):
        marks = after_sensitive if isinstance(after_sensitive, list) else []
        return [
            strip_sensitive(v, marks[i] if i < len(marks) else None)
            for i, v in enumerate(after)
            if not (i < len(marks) and marks[i] is True)
        ]
    return after
