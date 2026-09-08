"""Registry document: load with fail-closed rules, CAS updates, status machine, claims.

The registry is one JSON document per base state, stored next to it under
``<key>.overlays.json`` and written with the store's compare-and-swap
``put_json`` (S3: conditional requests). Every mutation
goes through :meth:`Registry.update`, which implements the GET/mutate/PUT-IfMatch
loop described in DESIGN.md section 5. Mutation callbacks must be idempotent
(set-by-key, never append) so that a retry after a lost conflict converges.
"""

from __future__ import annotations

import copy
import random
import time
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import ValidationError

from tofu_overlay.models import (
    LIVE_STATUSES,
    BackendConfig,
    Claim,
    ClaimKind,
    FrozenError,
    NotAllowedError,
    Overlay,
    PolicyError,
    RegistryDoc,
    RegistryError,
    StaleError,
    Status,
    Tombstone,
    ToolError,
    Violation,
    utcnow_iso,
)
from tofu_overlay.store import CasConflict, StateStore

REGISTRY_VERSION = 1


def _parse_iso(value: str) -> datetime:
    """Parse an ISO-8601 timestamp; naive values are assumed to be UTC."""
    ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts


def _identity_key(claim: Claim) -> str | None:
    """Canonical ``type|k=v|k=v`` string for a claim identity (sorted keys), None if empty."""
    if not claim.identity:
        return None
    parts = [f"{k}={claim.identity[k]}" for k in sorted(claim.identity)]
    return "|".join([claim.type, *parts])


def _dump(doc: RegistryDoc) -> dict[str, Any]:
    """JSON-ready dump used both for store writes and for idempotency comparisons."""
    return doc.model_dump(mode="json", by_alias=True)


def _merge_claim(existing: Claim | None, wanted: Claim, now: str) -> Claim:
    """Set-by-key merge: keep the original ``claimed_at``, refresh everything else."""
    if existing is None:
        return wanted.model_copy(update={"claimed_at": wanted.claimed_at or now, "updated_at": now})
    data = wanted.model_dump()
    data["claimed_at"] = existing.claimed_at
    data["updated_at"] = now
    # Never lose a physical id already learned from a previous apply.
    for field in ("id", "import_id"):
        if data.get(field) is None and getattr(existing, field) is not None:
            data[field] = getattr(existing, field)
    if not data.get("identity") and existing.identity:
        data["identity"] = dict(existing.identity)
    return Claim.model_validate(data)


class Registry:
    """Access to the per-base registry document with compare-and-swap updates."""

    def __init__(self, store: StateStore, cfg: BackendConfig, tool_version: str) -> None:
        self.store = store
        self.cfg = cfg
        self.tool_version = tool_version
        self.key: str = cfg.registry_key()
        # Injectable for tests: sleep function, jitter source and backoff bounds (seconds).
        self.sleep: Callable[[float], None] = time.sleep
        self.jitter: Callable[[], float] = random.random
        self.backoff_base: float = 0.2
        self.backoff_cap: float = 5.0
        # Used by acquire_claims to decide whether an APPLYING entry is abandoned.
        self.apply_timeout_min: int = 90

    # ----------------------------------------------------------------- loading

    def _empty_doc(self) -> RegistryDoc:
        return RegistryDoc(
            version=REGISTRY_VERSION,
            tool_version=self.tool_version,
            base={"bucket": self.cfg.bucket, "key": self.cfg.state_path(), "lineage": None},
        )

    def _validate_doc(self, raw: dict[str, Any]) -> RegistryDoc:
        version = raw.get("version")
        if not isinstance(version, int):
            raise RegistryError(f"registry {self.key}: missing or invalid 'version'")
        if version > REGISTRY_VERSION:
            raise RegistryError(
                f"registry {self.key}: document version {version} is newer than supported "
                f"version {REGISTRY_VERSION}; upgrade tofu-overlay"
            )
        try:
            doc = RegistryDoc.model_validate(raw)
        except ValidationError as exc:
            raise RegistryError(f"registry {self.key}: invalid document: {exc}") from exc
        base_key = doc.base.get("key")
        base_bucket = doc.base.get("bucket")
        if base_key != self.cfg.state_path() or base_bucket != self.cfg.bucket:
            raise RegistryError(
                f"registry {self.key}: base mismatch (document says "
                f"s3://{base_bucket}/{base_key}, backend is "
                f"s3://{self.cfg.bucket}/{self.cfg.state_path()})"
            )
        self._validate_state_keys(doc)
        return doc

    def _validate_state_keys(self, doc: RegistryDoc) -> None:
        """Fail closed on an entry whose ``state_key`` is not the key derived from its name.

        Deletes at finalize/abandon target ``state_key``: a hand-edited or
        foreign document must never point one at the base state (invariant 3.2).
        """
        for name, ov in doc.overlays.items():
            expected = self.cfg.overlay_key(name)
            if ov.state_key == expected and ov.state_key != self.cfg.state_path():
                continue
            raise RegistryError(
                f"registry {self.key}: overlay '{name}' has state_key {ov.state_key!r}, "
                f"expected {expected!r}; run doctor"
            )

    def load(self) -> tuple[RegistryDoc, str | None]:
        """Read the registry; fail closed on missing-with-overlays, invalid or newer docs."""
        try:
            found = self.store.get_json(self.key)
        except RegistryError:
            raise
        except Exception as exc:  # boto3 / JSON errors: registry unreachable or unreadable
            raise RegistryError(f"registry {self.key}: unreadable ({exc})") from exc
        if found is None:
            stray = self.store.list_prefix(self.cfg.overlay_prefix())
            if stray:
                raise RegistryError(
                    f"registry {self.key} is missing but overlay objects exist "
                    f"({', '.join(sorted(stray)[:5])}); run doctor"
                )
            return self._empty_doc(), None
        raw, etag = found
        if not isinstance(raw, dict):
            raise RegistryError(f"registry {self.key}: document is not a JSON object")
        return self._validate_doc(raw), etag

    # ------------------------------------------------------------------ update

    def _backoff(self, attempt: int) -> float:
        delay = min(self.backoff_base * (2**attempt), self.backoff_cap)
        return delay + self.jitter() * self.backoff_base

    def _already_applied(self, fn: Callable[[RegistryDoc], None]) -> RegistryDoc | None:
        """Re-load once and return the doc if ``fn`` would change nothing."""
        doc, _etag = self.load()
        probe = copy.deepcopy(doc)
        fn(probe)
        return doc if _dump(probe) == _dump(doc) else None

    def update(self, fn: Callable[[RegistryDoc], None], *, attempts: int = 8) -> RegistryDoc:
        """GET → ``fn(doc)`` → PUT IfMatch; retry on conflict with bounded backoff.

        ``fn`` must be idempotent. When the retry budget is exhausted the document
        is re-read once and the update is considered done if ``fn`` is a no-op on it.
        """
        for attempt in range(max(attempts, 1)):
            doc, etag = self.load()
            if etag == "":
                raise RegistryError(
                    f"registry {self.key}: the store returned an empty ETag; refusing an "
                    "unconditional write"
                )
            before = _dump(doc)
            fn(doc)
            doc.tool_version = self.tool_version
            after = _dump(doc)
            if after == before:
                return doc
            try:
                self.store.put_json(self.key, after, if_match=etag, if_none_match=etag is None)
                return doc
            except CasConflict:
                self.sleep(self._backoff(attempt))
        settled = self._already_applied(fn)
        if settled is not None:
            return settled
        raise RegistryError(
            f"registry {self.key}: too many concurrent updates ({attempts} attempts); retry later"
        )

    # ----------------------------------------------------------------- queries

    def get_overlay(self, doc: RegistryDoc, name: str) -> Overlay:
        """Return the overlay entry or raise NotAllowedError (exit 6)."""
        ov = doc.overlays.get(name)
        if ov is None:
            raise NotAllowedError(f"overlay '{name}' not found in registry {self.key}")
        return ov

    def conflicts_for(self, doc: RegistryDoc, me: str, wanted: dict[str, Claim]) -> list[Violation]:
        """Address (check 3) and identity (check 4) conflicts against other live overlays."""
        violations: list[Violation] = []
        wanted_identities = {
            addr: key
            for addr, claim in wanted.items()
            if claim.kind == ClaimKind.CREATE and (key := _identity_key(claim)) is not None
        }
        for other_name, other in doc.overlays.items():
            if other_name == me or other.status not in LIVE_STATUSES:
                continue
            violations.extend(self._address_conflicts(other_name, other, wanted))
            violations.extend(self._identity_conflicts(other_name, other, wanted_identities))
        return violations

    @staticmethod
    def _address_conflicts(
        other_name: str, other: Overlay, wanted: dict[str, Claim]
    ) -> list[Violation]:
        return [
            Violation(
                address=addr,
                rule="address-conflict",
                message=(
                    f"{addr} is already claimed ({other.claims[addr].kind}) by overlay "
                    f"'{other_name}' (branch {other.branch}, status {other.status})"
                ),
                other_overlay=other_name,
            )
            for addr in wanted
            if addr in other.claims
        ]

    @staticmethod
    def _identity_conflicts(
        other_name: str, other: Overlay, wanted_identities: dict[str, str]
    ) -> list[Violation]:
        theirs = {
            key: addr
            for addr, claim in other.claims.items()
            if claim.kind == ClaimKind.CREATE and (key := _identity_key(claim)) is not None
        }
        return [
            Violation(
                address=addr,
                rule="identity-conflict",
                message=(
                    f"{addr} has the same identity ({key}) as {theirs[key]} created by overlay "
                    f"'{other_name}' (branch {other.branch}, status {other.status})"
                ),
                other_overlay=other_name,
            )
            for addr, key in wanted_identities.items()
            if key in theirs
        ]

    def tombstoned(self, doc: RegistryDoc, name: str, days: int) -> bool:
        """True if ``name`` was merged/abandoned less than ``days`` days ago."""
        ts = doc.tombstones.get(name)
        if ts is None:
            return False
        try:
            at = _parse_iso(ts.at)
        except ValueError:
            return True  # unreadable timestamp: fail closed, keep the name reserved
        return datetime.now(UTC) - at < timedelta(days=days)

    def stale_applying(self, ov: Overlay, timeout_min: int) -> bool:
        """True if the overlay is APPLYING for longer than ``timeout_min`` minutes."""
        if ov.status != Status.APPLYING:
            return False
        since = ov.applying_since or ov.updated_at
        if not since:
            return True
        try:
            started = _parse_iso(since)
        except ValueError:
            return True
        return datetime.now(UTC) - started > timedelta(minutes=timeout_min)

    # --------------------------------------------------------------- mutations

    def _check_can_apply(self, ov: Overlay, name: str, run_id: str) -> None:
        """Status-table gate for ``apply`` (DESIGN.md section 5)."""
        if ov.status == Status.MERGING:
            raise FrozenError(f"overlay '{name}' is frozen (merging); use finalize or merge --undo")
        if ov.status == Status.CREATING:
            raise ToolError(f"overlay '{name}' is still being created; re-run create")
        if ov.status == Status.APPLYING and ov.run_id != run_id:
            if not self.stale_applying(ov, self.apply_timeout_min):
                raise RegistryError(
                    f"overlay '{name}' has an apply in progress (run {ov.run_id}, "
                    f"since {ov.applying_since}); wait or check doctor"
                )
        elif ov.status not in (Status.ACTIVE, Status.DIRTY, Status.APPLYING):
            raise NotAllowedError(f"overlay '{name}' is {ov.status}; apply is not allowed")

    def acquire_claims(
        self, name: str, wanted: dict[str, Claim], run_id: str, base_etag: str
    ) -> RegistryDoc:
        """Atomically acquire claims and flip the overlay to APPLYING (single CAS update)."""
        now = utcnow_iso()

        def mutate(doc: RegistryDoc) -> None:
            ov = self.get_overlay(doc, name)
            self._check_can_apply(ov, name, run_id)
            if ov.base_etag != base_etag:
                raise StaleError(
                    f"overlay '{name}' is stale: base ETag {base_etag} != recorded "
                    f"{ov.base_etag}; run rebase"
                )
            conflicts = self.conflicts_for(doc, name, wanted)
            if conflicts:
                lines = "\n".join(f"  - {v.message}" for v in conflicts)
                raise PolicyError(f"claim conflicts for overlay '{name}':\n{lines}")
            for addr, claim in wanted.items():
                ov.claims[addr] = _merge_claim(ov.claims.get(addr), claim, now)
            if not (ov.status == Status.APPLYING and ov.run_id == run_id):
                ov.applying_since = now
            ov.status = Status.APPLYING
            ov.run_id = run_id
            ov.updated_at = now

        return self.update(mutate)

    def finish_apply(
        self,
        name: str,
        *,
        ok: bool,
        claims: dict[str, Claim],
        applied_commit: str | None,
        caller_arn: str | None,
        tofu_version: str | None,
        base_etag_after: str | None,
        summary: dict,
        run_id: str | None = None,
        released: Iterable[str] = (),
    ) -> RegistryDoc:
        """Record the outcome of an apply: ACTIVE on success, DIRTY (claims kept) on failure.

        ``run_id`` must still be the overlay's current run: a superseded (stale)
        apply is refused so it cannot flip the status under a newer run.
        ``released`` addresses (own resources the plan deleted) are dropped from
        the claims on success only.
        """
        now = utcnow_iso()
        to_release = set(released)

        def mutate(doc: RegistryDoc) -> None:
            ov = self.get_overlay(doc, name)
            if run_id is not None and ov.run_id != run_id:
                raise RegistryError(
                    f"apply {run_id} of overlay '{name}' was superseded by run {ov.run_id}; "
                    "its outcome is not recorded"
                )
            for addr, claim in claims.items():
                if addr in to_release:
                    continue
                ov.claims[addr] = _merge_claim(ov.claims.get(addr), claim, now)
            if ok:
                for addr in to_release:
                    ov.claims.pop(addr, None)
            if caller_arn is not None:
                ov.caller_arn = caller_arn
            if tofu_version is not None:
                ov.tofu_version = tofu_version
            if ok:
                ov.status = Status.ACTIVE
                ov.applied_commit = applied_commit
            else:
                ov.status = Status.DIRTY
            ov.run_id = None
            ov.applying_since = None
            ov.last_apply = {
                "at": now,
                "ok": ok,
                "summary": dict(summary),
                "base_etag_after": base_etag_after,
                "base_moved": bool(base_etag_after and base_etag_after != ov.base_etag),
            }
            ov.updated_at = now

        return self.update(mutate)

    def _check_expected(
        self,
        ov: Overlay,
        name: str,
        expect_status: Iterable[Status] | None,
        expect_entry: Overlay | None,
    ) -> None:
        """Precondition of a status change: the entry is as it was when the command loaded it.

        ``expect_entry`` compares the whole entry (timestamps have second
        precision, so ``updated_at`` alone cannot tell two writes apart).
        """
        if expect_status is not None and ov.status not in set(expect_status):
            if ov.status == Status.MERGING:
                raise FrozenError(f"overlay '{name}' is frozen (merging); refusing to change it")
            raise RegistryError(
                f"overlay '{name}' is {ov.status} (run {ov.run_id}), changed since it was "
                "loaded; re-run the command"
            )
        if expect_entry is not None and ov.model_dump(mode="json") != expect_entry.model_dump(
            mode="json"
        ):
            raise RegistryError(
                f"overlay '{name}' changed since it was loaded (now {ov.status}, updated "
                f"{ov.updated_at}, run {ov.run_id}); re-run the command"
            )

    def set_status(
        self,
        name: str,
        status: Status,
        *,
        expect_status: Iterable[Status] | None = None,
        expect_entry: Overlay | None = None,
        **fields: Any,
    ) -> RegistryDoc:
        """Set the overlay status and any extra Overlay fields (validated by name).

        ``expect_status``/``expect_entry`` guard against a concurrent change
        (typically an apply) landing between the command's load and this
        write. ``claims`` are merged set-by-key, never replaced, so claims
        acquired meanwhile survive.
        """
        now = utcnow_iso()
        unknown = set(fields) - set(Overlay.model_fields)
        if unknown:
            raise ToolError(f"unknown overlay fields: {', '.join(sorted(unknown))}")
        expected = tuple(expect_status) if expect_status is not None else None

        def mutate(doc: RegistryDoc) -> None:
            ov = self.get_overlay(doc, name)
            self._check_expected(ov, name, expected, expect_entry)
            for field, value in fields.items():
                if field == "claims":
                    for addr, claim in value.items():
                        ov.claims[addr] = _merge_claim(ov.claims.get(addr), claim, now)
                    continue
                setattr(ov, field, value)
            ov.status = status
            ov.updated_at = now

        return self.update(mutate)

    def release_overlay(
        self,
        name: str,
        final: Status,
        *,
        pending_revert: Iterable[str] | None = None,
        expect_status: Iterable[Status] | None = None,
    ) -> RegistryDoc:
        """Remove the overlay entry and record a tombstone (idempotent).

        The tombstone keeps ``pending_revert`` (base resources the overlay
        modified and the trunk still has to revert); it defaults to the entry's.
        """
        now = utcnow_iso()
        expected = tuple(expect_status) if expect_status is not None else None

        def mutate(doc: RegistryDoc) -> None:
            ov = doc.overlays.get(name)
            if ov is None:
                if name in doc.tombstones:
                    return  # already released
                raise NotAllowedError(f"overlay '{name}' not found in registry {self.key}")
            self._check_expected(ov, name, expected, None)
            doc.overlays.pop(name)
            reverts = ov.pending_revert if pending_revert is None else list(pending_revert)
            doc.tombstones[name] = Tombstone(
                status=final, at=now, branch=ov.branch, pending_revert=sorted(set(reverts))
            )

        return self.update(mutate)
