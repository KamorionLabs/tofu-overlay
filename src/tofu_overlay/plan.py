"""Plan JSON analysis and policy checks (DESIGN §7, §8 and the trunk guard).

Input is the document produced by ``tofu show -json PLANFILE``. Everything
here is pure: no subprocess, no network. The registry is only used for its
conflict computation (checks 3 and 4).
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import TYPE_CHECKING, Any

from tofu_overlay import state as statemod
from tofu_overlay.identity import TypeKnowledge
from tofu_overlay.models import (
    Claim,
    ClaimKind,
    Overlay,
    PlanSummary,
    PolicyError,
    PolicyResult,
    RegistryDoc,
    ResourceChange,
    VerifyResult,
    Violation,
    utcnow_iso,
)

if TYPE_CHECKING:
    from tofu_overlay.registry import Registry

# Explicit action allow-list (DESIGN §7.2). Anything else is denied.
_CREATE = ("create",)
_UPDATE = ("update",)
_NOOP = ("no-op",)
_READ = ("read",)
_DELETE = ("delete",)
_FORGET = ("forget",)
_REPLACE = frozenset({("delete", "create"), ("create", "delete")})
_FORGET_CREATE = ("forget", "create")
_DESTRUCTIVE = frozenset({_DELETE, _FORGET, _FORGET_CREATE}) | _REPLACE

_INDEX_SUFFIX = re.compile(r"\[[^\]]*\]$")


# ------------------------------------------------------------------- parsing


def _change_from_json(entry: dict[str, Any]) -> ResourceChange:
    change = entry.get("change") or {}
    return ResourceChange(
        address=entry["address"],
        previous_address=entry.get("previous_address"),
        module_address=entry.get("module_address"),
        mode=entry.get("mode"),
        type=entry.get("type", ""),
        name=entry.get("name", ""),
        index=entry.get("index"),
        deposed=entry.get("deposed"),
        actions=list(change.get("actions") or []),
        before=change.get("before"),
        after=change.get("after"),
        after_unknown=change.get("after_unknown"),
        before_sensitive=change.get("before_sensitive"),
        after_sensitive=change.get("after_sensitive"),
        replace_paths=list(change.get("replace_paths") or []),
        importing=change.get("importing"),
        action_reason=entry.get("action_reason"),
    )


def _count(summary: PlanSummary, change: ResourceChange) -> None:
    actions = tuple(change.actions)
    if change.importing:
        summary.import_ += 1
    if actions == _CREATE:
        summary.create += 1
    elif actions == _UPDATE:
        summary.update += 1
    elif actions in (_DELETE, _FORGET):
        summary.delete += 1
    elif actions in _REPLACE or actions == _FORGET_CREATE:
        summary.replace += 1
    elif actions == _NOOP:
        summary.no_op += 1


def parse_plan(show_json: dict) -> tuple[list[ResourceChange], list[dict], PlanSummary]:
    """Flatten ``tofu show -json`` into changes, drift entries and a summary.

    Data sources stay in the change list (their actions are ``read``/``no-op``
    and ignored by the policy); ``resource_drift`` entries are returned raw.
    """
    changes = [_change_from_json(e) for e in show_json.get("resource_changes") or []]
    drift = [e for e in (show_json.get("resource_drift") or []) if isinstance(e, dict)]
    summary = PlanSummary()
    for change in changes:
        _count(summary, change)
    return changes, drift, summary


def is_base_address(address: str, base_addresses: set[str]) -> bool:
    """True when ``address`` is an instance address of the base state."""
    return address in base_addresses


# ------------------------------------------------------------------ helpers


def _is_data(change: ResourceChange) -> bool:
    """True for data sources; the plan's ``mode`` is authoritative, the address a fallback.

    The address heuristic only serves hand-built changes without ``mode``: a
    managed resource inside ``module "data"`` must not be mistaken for one.
    """
    if change.mode is not None:
        return change.mode == "data"
    return change.address.startswith("data.") or ".data." in change.address


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _strip_index(address: str) -> str:
    return _INDEX_SUFFIX.sub("", address)


def _violation(
    change: ResourceChange, rule: str, message: str, other: str | None = None
) -> Violation:
    reason = f" ({change.action_reason})" if change.action_reason else ""
    return Violation(
        address=change.address, rule=rule, message=message + reason, other_overlay=other
    )


def _identity_of(change: ResourceChange, knowledge: TypeKnowledge, *, source: str) -> dict:
    """Identity from ``after`` (creates) or ``before`` (updates), minus sensitive values."""
    if source == "after":
        values, marks = change.after, change.after_sensitive
    else:
        values = change.before
        marks = change.before_sensitive
        if marks is None:
            marks = change.after_sensitive
    attrs = statemod.strip_sensitive(values, marks)
    return knowledge.identity_for(change.type, attrs if isinstance(attrs, dict) else {})


def _create_claim(
    change: ResourceChange, knowledge: TypeKnowledge, existing: Claim | None, now: str
) -> tuple[Claim, str | None]:
    """Build (or refresh) a create claim; returns the claim and an optional warning."""
    identity = _identity_of(change, knowledge, source="after")
    warning = None
    if not identity and existing is not None and existing.identity:
        identity = existing.identity
    if not identity:
        warning = (
            f"{change.address}: identity unknown at plan time, address-only claim "
            "(identity is recorded after apply)"
        )
    attrs = change.after if isinstance(change.after, dict) else {}
    return (
        Claim(
            kind=ClaimKind.CREATE,
            type=change.type,
            identity=identity,
            id=existing.id if existing else None,
            import_id=knowledge.import_id_for(change.type, attrs)
            or (existing.import_id if existing else None),
            after_hash=_hash(change.after),
            claimed_at=existing.claimed_at if existing else now,
            updated_at=now,
        ),
        warning,
    )


def _update_claim(
    change: ResourceChange, knowledge: TypeKnowledge, existing: Claim | None, now: str
) -> Claim:
    identity = _identity_of(change, knowledge, source="before")
    if not identity and existing is not None:
        identity = existing.identity
    return Claim(
        kind=ClaimKind.UPDATE,
        type=change.type,
        identity=identity,
        id=(change.before or {}).get("id") if isinstance(change.before, dict) else None,
        import_id=None,
        after_hash=_hash(change.after),
        claimed_at=existing.claimed_at if existing else now,
        updated_at=now,
    )


# ----------------------------------------------------------------- evaluate


class _Evaluation:
    """Mutable working set of one policy evaluation (kept private)."""

    def __init__(
        self,
        me: Overlay,
        knowledge: TypeKnowledge,
        base_addresses: set[str],
        trunk_drift: dict[str, list[str]] | None = None,
    ) -> None:
        self.me = me
        self.knowledge = knowledge
        self.base_addresses = base_addresses
        self.trunk_drift: dict[str, list[str]] = dict(trunk_drift or {})
        self.claims: dict[str, Claim] = dict(me.claims)
        self.own: set[str] = set(me.create_claims())
        self.violations: list[Violation] = []
        self.warnings: list[str] = []
        self.drift: list[str] = []
        self.ignored: list[str] = []
        self.now = utcnow_iso()

    # -- classification ---------------------------------------------------- #

    def is_base(self, address: str) -> bool:
        return is_base_address(address, self.base_addresses)

    def is_own(self, address: str) -> bool:
        return address in self.own and not self.is_base(address)

    # -- per change -------------------------------------------------------- #

    def visit(self, change: ResourceChange) -> None:
        if _is_data(change):
            return
        if change.importing:
            self.violations.append(
                _violation(change, "import", "an overlay adopts nothing: remove the import block")
            )
            return
        if change.previous_address and not self._visit_move(change):
            return
        if change.deposed and not self.is_own(change.address):
            self.violations.append(
                _violation(change, "deposed", "deposed object on a resource not created here")
            )
            return
        self._visit_actions(change)

    def _visit_move(self, change: ResourceChange) -> bool:
        """Handle ``previous_address``; returns False when the change is settled."""
        old = change.previous_address or ""
        if self.is_base(old) or self.is_base(change.address):
            self.violations.append(
                _violation(change, "moved", f"moved from {old}: base resources cannot be moved")
            )
            return False
        if old in self.own:
            self.claims[change.address] = self.claims.pop(old)
            self.own.discard(old)
            self.own.add(change.address)
            self.warnings.append(f"{change.address}: claim moved from {old}")
        return True

    def _visit_actions(self, change: ResourceChange) -> None:
        actions = tuple(change.actions)
        if actions in (_NOOP, _READ):
            return
        if actions == _CREATE:
            self._visit_create(change)
        elif actions == _UPDATE:
            self._visit_update(change)
        elif actions in _DESTRUCTIVE:
            self._visit_destructive(change, actions)
        else:
            self.violations.append(
                _violation(change, "action", f"actions {list(actions)} are not allowed")
            )

    def _visit_create(self, change: ResourceChange) -> None:
        if self.is_base(change.address):
            self.violations.append(
                _violation(
                    change,
                    "base-address",
                    "address exists in the base state; the overlay would shadow a trunk resource",
                )
            )
            return
        claim, warning = _create_claim(
            change, self.knowledge, self.claims.get(change.address), self.now
        )
        self.claims[change.address] = claim
        self.own.add(change.address)
        if warning:
            self.warnings.append(warning)

    def _visit_update(self, change: ResourceChange) -> None:
        if self.is_base(change.address):
            self._visit_base_update(change)
            return
        if not self.is_own(change.address):
            self.warnings.append(
                f"{change.address}: not in the base state and not claimed; treated as "
                "created by this overlay (run apply to re-register the claim)"
            )

    def _visit_base_update(self, change: ResourceChange) -> None:
        """Ignored-attributes rule (§7.7), then trunk drift (§7.8), then the update claim."""
        existing = self.claims.get(change.address)
        if existing is not None and existing.kind is ClaimKind.UPDATE:
            # Already claimed by this overlay: the update is its own, whatever the trunk does.
            self.claims[change.address] = _update_claim(change, self.knowledge, existing, self.now)
            return
        differing = _differing_attributes(change)
        if differing and differing <= self.knowledge.ignored_attrs(change.type):
            self.ignored.append(change.address)
            return
        if change.address in self.trunk_drift:
            self.drift.append(change.address)
            return
        self.claims[change.address] = _update_claim(change, self.knowledge, None, self.now)

    def _visit_destructive(self, change: ResourceChange, actions: tuple[str, ...]) -> None:
        if not self.is_own(change.address):
            self.violations.append(
                _violation(
                    change,
                    "destructive",
                    f"actions {list(actions)} on a base resource; overlays are additive",
                )
            )
            return
        if actions in (_DELETE, _FORGET):
            self.claims.pop(change.address, None)
            self.own.discard(change.address)
            self.warnings.append(
                f"{change.address}: own resource removed, its claim is released after apply"
            )
            return
        claim, warning = _create_claim(
            change, self.knowledge, self.claims.get(change.address), self.now
        )
        self.claims[change.address] = claim
        if warning:
            self.warnings.append(warning)

    # -- global checks ----------------------------------------------------- #

    def summarise_noise(self) -> None:
        """One warning each for ignored-attribute updates and trunk drift (§7.7, §7.8)."""
        if self.ignored:
            self.warnings.append(
                f"{len(self.ignored)} update(s) only touch environment-dependent attributes "
                f"({', '.join(sorted(self.ignored))}): not claimed, still applied by tofu"
            )
        if self.drift:
            self.warnings.append(
                f"{len(self.drift)} base resource(s) differ because the trunk is not applied "
                f"on this base ({', '.join(sorted(self.drift))}): run the trunk pipeline, "
                "then `rebase`, or pass --accept-drift to claim them"
            )

    def check_base_identities(self, base_identities: set[str]) -> None:
        """Check 5: a create whose identity already exists in the base state."""
        for address, claim in sorted(self.claims.items()):
            if claim.kind is not ClaimKind.CREATE or self.is_base(address):
                continue
            key = self.knowledge.identity_key(claim.type, claim.identity)
            if key and key in base_identities:
                self.violations.append(
                    Violation(
                        address=address,
                        rule="base-identity",
                        message=f"identity {key} already exists in the base state",
                        other_overlay=None,
                    )
                )

    def check_drift(self, drift: list[dict]) -> None:
        """Check 6 (warning): refreshed value of an update claim differs from the applied one."""
        updates = {a: c for a, c in self.claims.items() if c.kind is ClaimKind.UPDATE}
        for entry in drift:
            address = entry.get("address")
            claim = updates.get(address) if isinstance(address, str) else None
            if claim is None:
                continue
            refreshed = (entry.get("change") or {}).get("after")
            if claim.after_hash is None or _hash(refreshed) != claim.after_hash:
                self.warnings.append(
                    f"{address}: drifted since the overlay applied it; the trunk (or someone) "
                    "changed it, rebase"
                )


def evaluate(
    changes: list[ResourceChange],
    drift: list[dict],
    *,
    base_addresses: set[str],
    base_identities: set[str],
    me: Overlay,
    doc: RegistryDoc,
    knowledge: TypeKnowledge,
    registry: Registry,
    trunk_drift: dict[str, list[str]] | None = None,
) -> PolicyResult:
    """Run DESIGN §7 checks 2-8 on a plan and build the claims to acquire on apply.

    Existing claims of ``me`` are kept (refreshed when the plan touches their
    address); claims of own resources deleted by the plan are dropped.

    ``trunk_drift`` is the trunk baseline (address -> actions of a plan of the
    trunk config against the base state). An ``update`` on a base address is
    first tested against the ignored-attributes rule (§7.7: no claim, listed
    in ``ignored``), then against ``trunk_drift`` (§7.8: no claim, listed in
    ``drift``), and only then becomes an ``update`` claim. An address already
    under an ``update`` claim of ``me`` stays claimed. ``None`` means "no
    baseline available": nothing is classified as drift.
    """
    ev = _Evaluation(me, knowledge, base_addresses, trunk_drift)
    for change in changes:
        ev.visit(change)
    ev.summarise_noise()
    ev.violations.extend(registry.conflicts_for(doc, me.name, ev.claims))
    ev.check_base_identities(base_identities)
    ev.check_drift(drift)
    return PolicyResult(
        violations=ev.violations,
        warnings=ev.warnings,
        claims=ev.claims,
        drift=sorted(ev.drift),
        ignored=sorted(ev.ignored),
    )


# ------------------------------------------------------------ targeted apply


def gate_targeted_plan(
    changes: list[ResourceChange],
    targeted: PolicyResult,
    *,
    me: Overlay,
    full_claims: dict[str, Claim],
) -> tuple[dict[str, Claim], list[str]]:
    """DESIGN §7.9: accept the tool-targeted plan of ``apply --only-claims``.

    ``targeted`` is the evaluation (same inputs as the full plan) of a plan
    targeted at ``full_claims``, the claims of the full plan. Every non-no-op
    managed change must be a claim of this overlay (an existing claim of
    ``me`` or one of ``full_claims``) or an ignored-attributes update: trunk
    drift and any other address that tofu pulled in through dependencies
    refuse the apply with ``PolicyError``, as do the usual violations
    (delete/replace of base addresses stay denied).

    Returns the claims to acquire (the targeted evaluation's own, which are by
    construction a subset of ``full_claims`` plus the untouched claims of
    ``me``) and one warning per full-plan claim the targeted plan left out
    (dropped, not acquired).
    """
    if not targeted.ok:
        raise PolicyError(
            f"{len(targeted.violations)} policy violation(s) in the targeted plan, apply refused"
        )
    if targeted.drift:
        raise PolicyError(
            f"{len(targeted.drift)} dependency(ies) of your changes carry trunk drift "
            f"({', '.join(targeted.drift)}); apply the trunk first or use --accept-drift"
        )
    planned = {
        c.address
        for c in changes
        if not _is_data(c) and tuple(c.actions) not in (_NOOP, _READ)
    }
    allowed = set(full_claims) | set(me.claims) | set(targeted.ignored)
    unknown = sorted(planned - allowed)
    if unknown:
        raise PolicyError(
            f"{len(unknown)} address(es) outside the overlay's claims pulled into the targeted "
            f"plan ({', '.join(unknown)}); apply the trunk first or use --accept-drift"
        )
    warnings = [
        f"{address}: claimed by the full plan but absent from the targeted plan; claim dropped"
        for address in sorted(set(full_claims) - set(me.claims) - planned)
    ]
    return dict(targeted.claims), warnings


# --------------------------------------------------------------- merge verify


def _differing_attributes(change: ResourceChange) -> set[str]:
    """Top-level attributes whose known value changes.

    Attributes flagged in ``after_unknown`` are computed side-effects of the
    known differences (``version``, ``last_modified``, ``metadata``...) and are
    ignored: counting them would defeat ``virtual_attributes``.
    """
    before = change.before if isinstance(change.before, dict) else {}
    after = change.after if isinstance(change.after, dict) else {}
    unknown = change.after_unknown if isinstance(change.after_unknown, dict) else {}
    computed = {k for k, v in unknown.items() if v}
    return {
        k
        for k in set(before) | set(after)
        if k not in computed and before.get(k) != after.get(k)
    }


def _verify_create_claim(
    change: ResourceChange | None, address: str, claim: Claim, knowledge: TypeKnowledge
) -> str | None:
    """Return an error message when an imported create claim does not verify."""
    if change is None:
        return f"{address}: absent from the plan (removed from the branch config?)"
    if not change.importing:
        return f"{address}: no import block in effect ({change.actions})"
    return _verify_imported_change(change, address, claim, knowledge)


def _verify_imported_change(
    change: ResourceChange, address: str, claim: Claim, knowledge: TypeKnowledge
) -> str | None:
    actions = tuple(change.actions)
    if actions == _NOOP:
        return None
    if actions != _UPDATE:
        return f"{address}: import followed by {list(actions)}"
    if change.replace_paths:
        return f"{address}: import followed by a replacement ({change.replace_paths})"
    offenders = sorted(_differing_attributes(change) - knowledge.virtual_attrs(claim.type))
    if offenders:
        return f"{address}: import followed by an update of {', '.join(offenders)}"
    return None


def _verify_accepted_recreate(change: ResourceChange | None, address: str) -> tuple[str, bool]:
    """(message, is_error) for a create claim the trunk was allowed to recreate."""
    if change is None:
        return f"{address}: absent from the plan (removed from the branch config?)", True
    if change.importing:
        return f"{address}: accepted for recreation but an import block is in effect", True
    if tuple(change.actions) == _CREATE:
        return f"{address}: will be recreated by the trunk (--accept-recreate)", False
    return f"{address}: accepted for recreation but planned as {change.actions}", True


def verify_import_plan(
    changes: list[ResourceChange],
    *,
    me: Overlay,
    knowledge: TypeKnowledge,
    allow_import_updates: bool,
    accepted_recreate: set[str] | None = None,
    trunk_drift: dict[str, list[str]] | None = None,
) -> VerifyResult:
    """DESIGN §8: verify the branch config planned against the base state.

    ``accepted_recreate`` lists the create claims left out of the imports file
    (``merge --accept-recreate``): they must plan as a plain ``create``.

    ``trunk_drift`` is the trunk baseline (address -> actions of a plan of the
    trunk config against the base state), the same document ``evaluate`` uses
    for §7.8. An ``update`` on an address that is in the baseline and is not
    one of the overlay's claims is **trunk drift**: the base lags the trunk and
    a trunk plan produces that update too, so it is not the branch's doing and
    does not block the merge. It is listed in ``VerifyResult.drift`` and
    summarised in a single warning. Any other update outside the claims stays
    an error unless ``allow_import_updates``. ``None`` means "no baseline
    available": nothing is tolerated as drift.
    """
    errors: list[str] = []
    warnings: list[str] = []
    drift: list[str] = []
    accepted = set(accepted_recreate or ())
    by_address = {c.address: c for c in changes if not c.deposed}
    creates = me.create_claims()
    updates = me.update_claims()

    for address, claim in sorted(creates.items()):
        if address in accepted:
            message, is_error = _verify_accepted_recreate(by_address.get(address), address)
            (errors if is_error else warnings).append(message)
            continue
        if not knowledge.known(claim.type):
            warnings.append(f"{address}: no import format for {claim.type}, using {{id}}")
        error = _verify_create_claim(by_address.get(address), address, claim, knowledge)
        if error:
            errors.append(error)

    for change in changes:
        if change.address in creates or _is_data(change):
            continue
        actions = tuple(change.actions)
        if actions in (_NOOP, _READ):
            continue
        if actions == _UPDATE and change.address in updates and not change.replace_paths:
            continue
        if actions == _UPDATE and not change.replace_paths:
            if trunk_drift is not None and change.address in trunk_drift:
                drift.append(change.address)
            else:
                msg = f"{change.address}: update outside the overlay's claims"
                (warnings if allow_import_updates else errors).append(msg)
        elif actions == _CREATE:
            warnings.append(f"{change.address}: created by the trunk (not applied in the overlay)")
        elif change.importing:
            errors.append(f"{change.address}: import block on a resource without a create claim")
        else:
            errors.append(f"{change.address}: {list(actions)} is not allowed in a merge plan")

    if drift:
        warnings.append(
            f"{len(drift)} base resource(s) differ because the trunk is not applied on this "
            f"base ({', '.join(sorted(drift))}): tolerated, a trunk plan produces them too"
        )
    return VerifyResult(errors=errors, warnings=warnings, drift=sorted(drift))


# ---------------------------------------------------------------- trunk guard


def _dependencies_of_creates(ov: Overlay, state_doc: dict | None) -> set[str]:
    """Addresses (index stripped) the overlay's created instances depend on.

    Sources: the ``dependencies`` recorded in each create claim after apply
    (always available to the trunk pipeline through the registry alone) and,
    when given, the overlay state document itself.
    """
    deps: set[str] = set()
    for claim in ov.create_claims().values():
        deps.update(_strip_index(d) for d in claim.dependencies)
    if not state_doc:
        return deps
    index = statemod.index_instances(state_doc)
    for address in ov.create_claims():
        entry = index.get(address)
        if entry is None:
            continue
        deps.update(_strip_index(d) for d in entry[1].get("dependencies") or [])
    return deps


def guard_trunk_plan(
    changes: list[ResourceChange],
    *,
    doc: RegistryDoc,
    knowledge: TypeKnowledge,
    overlay_states: dict[str, dict] | None,
) -> list[Violation]:
    """DESIGN §6 ``guard``: violations of a trunk plan against live overlay claims."""
    violations: list[Violation] = []
    for name, ov in sorted(doc.live_overlays().items()):
        creates = ov.create_claims()
        identities = {
            key
            for c in creates.values()
            if (key := knowledge.identity_key(c.type, c.identity)) is not None
        }
        updates = set(ov.update_claims())
        deps = _dependencies_of_creates(ov, (overlay_states or {}).get(name))
        for change in changes:
            violations.extend(
                _guard_change(change, name, creates, identities, updates, deps, knowledge)
            )
    return violations


def _guard_change(
    change: ResourceChange,
    name: str,
    creates: dict[str, Claim],
    identities: set[str],
    updates: set[str],
    deps: set[str],
    knowledge: TypeKnowledge,
) -> list[Violation]:
    out: list[Violation] = []
    actions = set(change.actions)
    if "create" in actions:
        if change.address in creates:
            out.append(_violation(change, "address-claimed", "address created by an overlay", name))
        key = knowledge.identity_key(change.type, _identity_of(change, knowledge, source="after"))
        if key and key in identities:
            out.append(
                _violation(
                    change, "identity-claimed", f"identity {key} claimed by an overlay", name
                )
            )
    if actions & {"delete", "forget"}:
        if change.address in updates:
            out.append(
                _violation(
                    change, "update-claim", "destroys a resource under an update claim", name
                )
            )
        if _strip_index(change.address) in deps or change.address in deps:
            out.append(
                _violation(
                    change,
                    "dependency",
                    "destroys a resource an overlay's creations depend on",
                    name,
                )
            )
    return out


# ------------------------------------------------------------------- summary


def render_summary(summary: PlanSummary) -> str:
    """One-line plan summary in tofu's own wording."""
    parts = []
    if summary.import_:
        parts.append(f"{summary.import_} to import")
    parts.append(f"{summary.create} to add")
    parts.append(f"{summary.update} to change")
    if summary.replace:
        parts.append(f"{summary.replace} to replace")
    parts.append(f"{summary.delete} to destroy")
    return "Plan: " + ", ".join(parts) + "."
