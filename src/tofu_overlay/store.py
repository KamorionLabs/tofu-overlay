"""Backend-neutral state store contract and factory.

Everything above this layer (registry, overlay orchestration, merge, CLI)
talks to the remote state backend through :class:`StateStore`: a small set of
object operations plus the registry's compare-and-swap JSON writes and the
backend's lock bookkeeping. ``s3`` is the only implementation today
(:class:`tofu_overlay.s3state.S3State`); :func:`make_store` is the single place
that maps ``BackendConfig.backend_type`` to an implementation. What another
backend would have to provide is listed in docs/ROADMAP.md.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from tofu_overlay.models import BackendConfig, RegistryError, ToolError

__all__ = [
    "SUPPORTED_BACKENDS",
    "CasConflict",
    "StateStore",
    "make_store",
    "unsupported_backend_message",
]

SUPPORTED_BACKENDS: tuple[str, ...] = ("s3",)

_BACKEND_HINTS = {
    "azurerm": "blob ETag conditional writes and lease-based locks",
    "gcs": "object generation preconditions and .tflock objects",
    "http": "server-dependent conditional writes",
    "local": "development use only",
}


class CasConflict(RegistryError):
    """A conditional registry write lost the race (S3: HTTP 412 or 409)."""


def unsupported_backend_message(backend_type: str) -> str:
    """Error text for a backend type that has no :class:`StateStore` implementation."""
    message = f"backend '{backend_type}' is not supported yet, see docs/ROADMAP.md"
    hint = _BACKEND_HINTS.get(backend_type)
    if hint:
        message += f" ({backend_type} needs {hint})"
    return message


@runtime_checkable
class StateStore(Protocol):
    """Operations the tool needs from a remote state backend.

    State objects are only ever inspected (``head``/``exists``/``list_prefix``)
    or moved (``copy`` to an archive key, ``delete`` at finalize/abandon); every
    state write goes through tofu (DESIGN 3.1). The registry document is a JSON
    object written with compare-and-swap semantics: ``put_json`` must fail with
    :class:`CasConflict` when the object changed since ``get_json`` returned the
    given ``if_match`` token, or already exists when ``if_none_match`` is set.

    ``path`` arguments are state object keys; the digest and lock methods cover
    the backend's own bookkeeping for that key (S3: the DynamoDB ``-md5`` item,
    the DynamoDB lock item and the ``.tflock`` object) and are no-ops when the
    backend has nothing of the kind.
    """

    cfg: BackendConfig

    # -- objects ------------------------------------------------------------ #

    def head(self, key: str) -> dict | None:
        """``{"etag", "size", "last_modified"}`` of an object, ``None`` when absent."""

    def exists(self, key: str) -> bool:
        """True when the object exists (a missing key is never an empty state)."""

    def list_prefix(self, prefix: str) -> list[str]:
        """Every object key under ``prefix``."""

    def copy(self, src: str, dst: str) -> None:
        """Server-side copy of one object (encryption settings re-applied)."""

    def delete(self, key: str) -> None:
        """Delete one object; an absent object is not an error."""

    # -- registry document (CAS) -------------------------------------------- #

    def get_json(self, key: str) -> tuple[dict, str] | None:
        """``(document, version token)`` of a JSON object, ``None`` when absent."""

    def put_json(
        self,
        key: str,
        doc: dict,
        *,
        if_match: str | None,
        if_none_match: bool = False,
    ) -> str:
        """Conditionally write a JSON document; returns the new version token."""

    # -- backend bookkeeping for a state key -------------------------------- #

    def digest_item_exists(self, path: str) -> bool:
        """True when the backend keeps a digest/checksum item for ``path``."""

    def delete_digest_item(self, path: str) -> None:
        """Delete the digest item of ``path`` (no-op when the backend has none)."""

    def lock_info(self, path: str) -> dict | None:
        """Current tofu lock info held on ``path``, ``None`` when unlocked."""

    def delete_lock_marker(self, path: str) -> None:
        """Delete a leftover lock marker of ``path`` (no-op when absent)."""


def make_store(cfg: BackendConfig, session: Any = None) -> StateStore:
    """Build the :class:`StateStore` for ``cfg.backend_type``.

    ``session`` is passed to the implementation (a ``boto3.Session`` for
    ``s3``; tests inject a moto-backed one). Unsupported types raise
    :class:`ToolError` pointing at docs/ROADMAP.md.
    """
    if cfg.backend_type == "s3":
        from tofu_overlay.s3state import S3State

        return S3State(cfg, session=session)
    raise ToolError(unsupported_backend_message(cfg.backend_type))
