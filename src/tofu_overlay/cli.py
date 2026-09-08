"""Typer command-line interface: option plumbing, exit-code mapping, output modes."""

from __future__ import annotations

import os
import sys
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any

import typer

from tofu_overlay import __version__, config
from tofu_overlay.backend import describe, resolve_backend
from tofu_overlay.identity import TypeKnowledge
from tofu_overlay.merge import MergeService
from tofu_overlay.merge import guard as guard_plan
from tofu_overlay.models import (
    BackendConfig,
    ExitCode,
    NotAllowedError,
    OverlayError,
    PolicyError,
    RegistryDoc,
    ToolConfig,
)
from tofu_overlay.output import Console
from tofu_overlay.overlay import OverlayService
from tofu_overlay.registry import Registry
from tofu_overlay.store import make_store
from tofu_overlay.tofu import validate_passthrough

app = typer.Typer(
    name="tofu-overlay",
    help="Copy-on-write state overlays for OpenTofu/Terraform.",
    add_completion=False,
    no_args_is_help=False,
    invoke_without_command=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)

MUTATING = {"create", "apply", "rebase", "merge", "finalize", "abandon"}

# Injection points for tests: a boto3 session and a TofuRunner-compatible factory.
INJECT: dict[str, Any] = {"session": None, "runner_factory": None}


@dataclass
class Globals:
    """Global options captured by the app callback."""

    chdir: Path
    name: str | None = None
    backend_config: list[Path] = field(default_factory=list)
    overrides: dict[str, str] = field(default_factory=dict)
    json: bool = False
    no_color: bool = False
    yes: bool = False
    verbose: bool = False
    # Injection points for tests.
    session: Any = None
    runner_factory: Callable[..., Any] | None = None


@dataclass
class Setup:
    """Everything a command needs once the backend is resolved."""

    g: Globals
    console: Console
    cfg: ToolConfig
    backend: BackendConfig

    def service(self, cwd: Path | None = None) -> OverlayService:
        return OverlayService(
            cwd or self.g.chdir,
            self.cfg,
            self.backend,
            self.console,
            name=self.g.name,
            session=self.g.session,
            runner_factory=self.g.runner_factory,
        )


# ---------------------------------------------------------------------- plumbing


def _console(g: Globals) -> Console:
    return Console(json_mode=g.json, ci=config.is_ci(), no_color=g.no_color)


def _resolve(g: Globals, cwd: Path, cfg: ToolConfig) -> BackendConfig:
    return resolve_backend(
        cwd,
        overrides=dict(g.overrides),
        backend_config_files=list(g.backend_config),
        data_dir=cwd / ".terraform",
    )


def _setup(ctx: typer.Context, *, mutating: bool) -> Setup:
    """Build the console, resolve the backend, echo it and enforce allowed_base_keys."""
    g: Globals = ctx.obj
    console = _console(g)
    cfg = config.load_config(g.chdir)
    backend = _resolve(g, g.chdir, cfg)
    if not g.json:
        console.backend_line(backend)
    if mutating and not config.base_key_allowed(backend.key, cfg.policy):
        raise NotAllowedError(
            f"base key {backend.key} is not in policy.allowed_base_keys "
            f"({cfg.policy.allowed_base_keys or 'empty'})"
        )
    setup = Setup(g, console, cfg, backend)
    if mutating:
        setup.service().refuse_symlinked_env()
    return setup


def _run(ctx: typer.Context, fn: Callable[[], int | ExitCode | None]) -> None:
    """Run a command body and map errors to exit codes."""
    g: Globals = ctx.obj
    console = _console(g)
    try:
        code = fn()
    except (typer.Exit, typer.Abort):
        raise
    except OverlayError as exc:
        console.error(str(exc))
        if g.verbose:
            console.info(traceback.format_exc())
        raise typer.Exit(int(exc.exit_code)) from None
    except Exception as exc:  # noqa: BLE001 - last resort mapping to exit 1
        console.error(f"unexpected error: {exc.__class__.__name__}: {exc}")
        if g.verbose:
            console.info(traceback.format_exc())
        raise typer.Exit(int(ExitCode.ERROR)) from None
    raise typer.Exit(int(code or 0))


def _emit(setup: Setup, payload: dict, human: Callable[[], None]) -> None:
    if setup.g.json:
        setup.console.json(payload)
    else:
        human()


def _env_dirs(setup: Setup) -> list[Path]:
    """Env directories of the repo matching policy.env_dir_glob."""
    root = config.find_repo_root(setup.g.chdir) or setup.g.chdir
    return sorted(
        p
        for p in root.glob(setup.cfg.policy.env_dir_glob)
        if p.is_dir() and config.symlinked_ancestor(p) is None
    )


def _services_for_repo(setup: Setup) -> list[OverlayService]:
    services = []
    for env_dir in _env_dirs(setup):
        try:
            backend = _resolve(setup.g, env_dir, setup.cfg)
        except OverlayError as exc:
            setup.console.warn(f"{env_dir}: {exc}")
            continue
        services.append(
            OverlayService(
                env_dir, setup.cfg, backend, setup.console,
                name=setup.g.name, session=setup.g.session, runner_factory=setup.g.runner_factory,
            )
        )
    return services


def _list_rows(console: Console, rows: list[dict], title: str) -> None:
    columns = [
        "name", "branch", "owners", "status", "fresh", "age_days", "claims", "pending_revert",
    ]

    def cell(row: dict, column: str) -> str:
        return ", ".join(row[column]) if column == "owners" else str(row[column])

    console.table(title, columns, [[cell(r, c) for c in columns] for r in rows])


def _absolute_backend_config(entry: Path) -> Path:
    """Make a ``--backend-config`` file absolute: tofu runs in the ``-C`` directory.

    An inline ``key=value`` entry (not an existing file) is kept verbatim.
    """
    return entry.resolve() if entry.is_file() else entry


# ---------------------------------------------------------------------- callback


@app.callback()
def main_callback(
    ctx: typer.Context,
    chdir: Annotated[Path, typer.Option("-C", "--chdir", help="Stack env directory.")] = Path("."),
    name: Annotated[
        str | None, typer.Option("--name", help="Overlay name (default: from branch).")
    ] = None,
    backend_config: Annotated[
        list[Path] | None,
        typer.Option("--backend-config", help="tofu -backend-config file (repeatable)."),
    ] = None,
    bucket: Annotated[str | None, typer.Option("--bucket", envvar="TOFU_OVERLAY_BUCKET")] = None,
    key: Annotated[str | None, typer.Option("--key", envvar="TOFU_OVERLAY_KEY")] = None,
    region: Annotated[str | None, typer.Option("--region", envvar="TOFU_OVERLAY_REGION")] = None,
    profile: Annotated[str | None, typer.Option("--profile", envvar="TOFU_OVERLAY_PROFILE")] = None,
    dynamodb_table: Annotated[
        str | None, typer.Option("--dynamodb-table", envvar="TOFU_OVERLAY_DYNAMODB_TABLE")
    ] = None,
    json_mode: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
    no_color: Annotated[bool, typer.Option("--no-color")] = False,
    yes: Annotated[bool, typer.Option("--yes", help="Skip confirmations.")] = False,
    print_backend: Annotated[
        bool, typer.Option("--print-backend", help="Print backend and exit.")
    ] = False,
    verbose: Annotated[bool, typer.Option("-v", "--verbose")] = False,
) -> None:
    """Copy-on-write state overlays for OpenTofu/Terraform."""
    overrides = {
        k: v
        for k, v in {
            "bucket": bucket, "key": key, "region": region,
            "profile": profile, "dynamodb_table": dynamodb_table,
        }.items()
        if v
    }
    ctx.obj = Globals(
        # Logical path (absolute, not resolved) so a symlinked env dir stays detectable.
        chdir=Path(os.path.normpath(chdir.absolute())),
        name=name,
        backend_config=[_absolute_backend_config(p) for p in backend_config or []],
        overrides=overrides,
        json=json_mode,
        no_color=no_color,
        yes=yes,
        verbose=verbose,
        session=INJECT.get("session"),
        runner_factory=INJECT.get("runner_factory"),
    )
    if print_backend:

        def body() -> int:
            g: Globals = ctx.obj
            console = _console(g)
            backend = _resolve(g, g.chdir, config.load_config(g.chdir))
            if g.json:
                console.json({"backend": backend.model_dump()})
            else:
                console.info(describe(backend))
            return 0

        _run(ctx, body)
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())
        raise typer.Exit(0)


# ---------------------------------------------------------------------- commands


@app.command()
def version() -> None:
    """Print the tool version."""
    typer.echo(__version__)


@app.command()
def create(
    ctx: typer.Context,
    force_name: Annotated[
        bool, typer.Option("--force-name", help="Reuse a tombstoned name.")
    ] = False,
) -> None:
    """Fork the base state into a new overlay bound to the current branch."""

    def body() -> int:
        setup = _setup(ctx, mutating=True)
        ov = setup.service().create(force_name=force_name)
        _emit(setup, {"overlay": ov.model_dump(mode="json")}, lambda: None)
        return 0

    _run(ctx, body)


def _plan_exit(policy_ok: bool, has_changes: bool, detailed: bool) -> int:
    if not policy_ok:
        return int(ExitCode.POLICY)
    if detailed and has_changes:
        return int(ExitCode.CHANGES)
    return 0


@app.command(context_settings={"allow_extra_args": False})
def plan(
    ctx: typer.Context,
    detailed_exitcode: Annotated[bool, typer.Option("--detailed-exitcode")] = False,
    allow_behind: Annotated[bool, typer.Option("--allow-behind")] = False,
    tofu_args: Annotated[list[str] | None, typer.Argument(metavar="[-- TOFU_ARGS...]")] = None,
) -> None:
    """Plan the branch against the overlay state and run the policy checks."""

    def body() -> int:
        extra = list(tofu_args or [])
        validate_passthrough(extra)
        setup = _setup(ctx, mutating=False)
        svc = setup.service()
        policy, summary, planfile, stale = svc.plan(
            extra=extra, allow_behind=allow_behind, detailed_exitcode=detailed_exitcode
        )
        summary_dict = summary.model_dump(by_alias=True)
        changes = sum(v for k, v in summary_dict.items() if k != "no_op") > 0
        _emit(
            setup,
            {
                "policy": policy.model_dump(mode="json"),
                "summary": summary_dict,
                "planfile": str(planfile),
                "stale": stale,
                "remote_overlays": svc.remote_overlay_keys(svc.name),
            },
            lambda: None,
        )
        return _plan_exit(policy.ok, changes, detailed_exitcode)

    _run(ctx, body)


@app.command()
def apply(
    ctx: typer.Context,
    auto_approve: Annotated[bool, typer.Option("--auto-approve")] = False,
    allow_stale: Annotated[bool, typer.Option("--allow-stale")] = False,
    allow_behind: Annotated[bool, typer.Option("--allow-behind")] = False,
    tofu_args: Annotated[list[str] | None, typer.Argument(metavar="[-- TOFU_ARGS...]")] = None,
) -> None:
    """Acquire claims and apply the plan on the overlay state."""

    def body() -> int:
        setup = _setup(ctx, mutating=True)
        if auto_approve and not (config.is_ci() or setup.g.yes):
            raise PolicyError("--auto-approve requires --yes outside CI")
        svc = setup.service()
        ov = svc.apply(
            auto_approve=auto_approve,
            allow_stale=allow_stale,
            allow_behind=allow_behind,
            extra=list(tofu_args or []),
            yes=setup.g.yes,
        )
        _emit(
            setup,
            {
                "overlay": ov.model_dump(mode="json"),
                "remote_overlays": svc.remote_overlay_keys(svc.name),
            },
            lambda: None,
        )
        return 0

    _run(ctx, body)


@app.command()
def status(
    ctx: typer.Context,
    repo: Annotated[
        bool, typer.Option("--repo", help="Every base under policy.env_dir_glob.")
    ] = False,
) -> None:
    """Show the overlays of this base (or of the whole repo)."""

    def body() -> int:
        setup = _setup(ctx, mutating=False)
        services = _services_for_repo(setup) if repo else [setup.service()]
        payloads = []
        for svc in services:
            payload = svc.status()
            payloads.append(payload)
            if not setup.g.json:
                _list_rows(
                    setup.console, [_status_row(r) for r in payload["overlays"]],
                    f"{payload['base']['bucket']}/{payload['base']['key']}"
                    + (f" (current: {payload['current']})" if payload["current"] else ""),
                )
                for key, overlay_key in sorted(payload["remote_overlays"].items()):
                    setup.console.info(f"remote state {key}: reading overlay {overlay_key}")
        if setup.g.json:
            setup.console.json({"bases": payloads} if repo else payloads[0])
        return 0

    _run(ctx, body)


def _status_row(row: dict) -> dict:
    return {**row, "claims": len(row["claims"]), "pending_revert": len(row["pending_revert"])}


@app.command("list")
def list_cmd(
    ctx: typer.Context,
    prefix: Annotated[
        str | None,
        typer.Option("--prefix", help="Scan *.overlays.json under this prefix (needs --bucket)."),
    ] = None,
) -> None:
    """List overlays of this base, or scan a bucket prefix without a checkout."""

    def body() -> int:
        g: Globals = ctx.obj
        if prefix is not None:
            return _list_prefix(g, prefix)
        setup = _setup(ctx, mutating=False)
        rows = setup.service().list()
        _emit(setup, {"overlays": rows}, lambda: _list_rows(setup.console, rows, "overlays"))
        return 0

    _run(ctx, body)


def _list_prefix(g: Globals, prefix: str) -> int:
    """`list --bucket B --prefix P`: read every registry document under the prefix."""
    console = _console(g)
    bucket = g.overrides.get("bucket")
    if not bucket:
        raise PolicyError("--prefix requires --bucket")
    cfg = BackendConfig(
        bucket=bucket,
        key=prefix,
        region=g.overrides.get("region"),
        profile=g.overrides.get("profile"),
        dynamodb_table=g.overrides.get("dynamodb_table"),
        kms_key_id=None,
    )
    store = make_store(cfg, session=g.session)
    rows = []
    for key in sorted(store.list_prefix(prefix)):
        if not key.endswith(".overlays.json"):
            continue
        loaded = store.get_json(key)
        if loaded is None:
            continue
        doc = RegistryDoc.model_validate(loaded[0])
        for name, ov in sorted(doc.overlays.items()):
            rows.append(
                {
                    "name": name, "branch": ov.branch, "owners": ov.owners,
                    "status": str(ov.status), "fresh": None, "age_days": None,
                    "claims": len(ov.claims), "pending_revert": len(ov.pending_revert),
                    "base_key": doc.base.get("key"), "registry_key": key,
                }
            )
    if g.json:
        console.json({"overlays": rows})
    else:
        columns = ["base_key", "name", "branch", "status", "claims"]
        console.table(
            f"{cfg.backend_type}://{bucket}/{prefix}", columns,
            [[str(r[c]) for c in columns] for r in rows]
        )
    return 0


@app.command()
def check(
    ctx: typer.Context,
    repo: Annotated[
        bool, typer.Option("--repo", help="Every base under policy.env_dir_glob.")
    ] = False,
) -> None:
    """CI gate: read-only consistency checks for the branch's overlay."""

    def body() -> int:
        setup = _setup(ctx, mutating=False)
        services = _services_for_repo(setup) if repo else [setup.service()]
        results = []
        all_errors: list[str] = []
        for svc in services:
            ok, errors, warnings = svc.check()
            results.append({"base_key": svc.backend.key, "ok": ok, "errors": errors,
                            "warnings": warnings})
            all_errors += errors
            for w in warnings:
                setup.console.warn(f"{svc.backend.key}: {w}")
            for e in errors:
                setup.console.error(f"{svc.backend.key}: {e}")
            if ok and not setup.g.json:
                setup.console.success(f"{svc.backend.key}: check passed")
        if setup.g.json:
            setup.console.json({"ok": not all_errors, "results": results})
        return _check_exit(all_errors)

    _run(ctx, body)


def _check_exit(errors: list[str]) -> int:
    if not errors:
        return 0
    if all(e.startswith(("stale:", "behind:")) for e in errors):
        return int(ExitCode.STALE)
    return int(ExitCode.POLICY)


@app.command()
def rebase(ctx: typer.Context) -> None:
    """Rebuild the overlay state on the current base (typed confirmation)."""

    def body() -> int:
        setup = _setup(ctx, mutating=True)
        ov = setup.service().rebase(yes=setup.g.yes)
        _emit(setup, {"overlay": ov.model_dump(mode="json")}, lambda: None)
        return 0

    _run(ctx, body)


@app.command()
def merge(
    ctx: typer.Context,
    undo: Annotated[
        bool, typer.Option("--undo", help="Remove the imports file, back to active.")
    ] = False,
    allow_import_updates: Annotated[bool, typer.Option("--allow-import-updates")] = False,
    accept_recreate: Annotated[
        str | None,
        typer.Option("--accept-recreate", help="Comma-separated addresses the trunk may recreate."),
    ] = None,
    allow_unapplied: Annotated[bool, typer.Option("--allow-unapplied")] = False,
) -> None:
    """Generate import blocks for the trunk and freeze the overlay (import strategy)."""

    def body() -> int:
        setup = _setup(ctx, mutating=True)
        service = MergeService(setup.service())
        if undo:
            service.undo(yes=setup.g.yes)
            return 0
        addresses = [a.strip() for a in (accept_recreate or "").split(",") if a.strip()]
        path = service.merge(
            allow_import_updates=allow_import_updates,
            accept_recreate=addresses,
            allow_unapplied=allow_unapplied,
            yes=setup.g.yes,
        )
        _emit(setup, {"imports_file": str(path)}, lambda: None)
        return 0

    _run(ctx, body)


@app.command()
def finalize(
    ctx: typer.Context,
    purge: Annotated[
        bool, typer.Option("--purge", help="Do not keep an archive of the state.")
    ] = False,
) -> None:
    """After the trunk applied the imports: archive the overlay and tombstone it."""

    def body() -> int:
        setup = _setup(ctx, mutating=True)
        setup.service().finalize(purge=purge, yes=setup.g.yes)
        return 0

    _run(ctx, body)


@app.command()
def abandon(
    ctx: typer.Context,
    keep_resources: Annotated[bool, typer.Option("--keep-resources")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
) -> None:
    """Destroy the overlay's own resources and release its claims."""

    def body() -> int:
        setup = _setup(ctx, mutating=True)
        setup.service().abandon(keep_resources=keep_resources, dry_run=dry_run, yes=setup.g.yes)
        return 0

    _run(ctx, body)


@app.command()
def doctor(ctx: typer.Context) -> None:
    """Read-only consistency report (registry, objects, locks, repo hygiene)."""

    def body() -> int:
        setup = _setup(ctx, mutating=False)
        findings = setup.service().doctor()
        _emit(setup, {"findings": findings}, lambda: None)
        return 0

    _run(ctx, body)


@app.command()
def guard(
    ctx: typer.Context,
    plan_json: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True)],
) -> None:
    """Trunk pipeline gate: fail if the trunk plan collides with an overlay's claims."""

    def body() -> int:
        setup = _setup(ctx, mutating=False)
        store = make_store(setup.backend, session=setup.g.session)
        registry = Registry(store, setup.backend, __version__)
        violations = guard_plan(
            plan_json, registry=registry, knowledge=TypeKnowledge.load(setup.cfg)
        )
        for v in violations:
            other = f" (overlay {v.other_overlay})" if v.other_overlay else ""
            setup.console.error(f"{v.rule}: {v.address} {v.message}{other}")
        _emit(
            setup,
            {"ok": not violations, "violations": [v.model_dump(mode="json") for v in violations]},
            lambda: setup.console.success("guard passed") if not violations else None,
        )
        return int(ExitCode.POLICY) if violations else 0

    _run(ctx, body)


@app.command()
def gc(
    ctx: typer.Context,
    purge: Annotated[
        bool, typer.Option("--purge", help="Delete archived states (typed confirm).")
    ] = False,
) -> None:
    """Report orphan-branch overlays and archives; --purge deletes archives only."""

    def body() -> int:
        setup = _setup(ctx, mutating=purge)
        findings = setup.service().gc(purge=purge, yes=setup.g.yes)
        _emit(setup, {"findings": findings}, lambda: None)
        return 0

    _run(ctx, body)


def main() -> None:
    """Console-script entry point."""
    try:
        app()
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
