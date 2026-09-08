"""OpenTofu/Terraform subprocess runner.

Every state read or write goes through the binary (``state pull`` / ``state push``
/ ``apply``) in a dedicated ``TF_DATA_DIR`` so that the backend lock protocol,
digest items and lineage/serial checks stay consistent (DESIGN §3.1).
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path

from tofu_overlay.models import BackendConfig, ToolError

FORBIDDEN_PASSTHROUGH = (
    "-target",
    "-replace",
    "-refresh-only",
    "-destroy",
    "-state",
    "-lock=false",
    "-lock=0",
    "-out",
)

# Environment variables through which tofu reads extra CLI arguments; they would
# bypass validate_passthrough, so every tofu run drops them (DESIGN 3.9).
CLI_ARGS_ENV_PREFIX = "TF_CLI_ARGS"

DEFAULT_PLUGIN_CACHE_DIR = Path.home() / ".cache" / "tofu-overlay" / "plugins"
LOCK_FILE_NAME = ".terraform.lock.hcl"
LOCK_MARKER_NAME = "tofu-overlay.lock.sha256"
_TAIL_LINES = 40


def validate_passthrough(args: list[str]) -> None:
    """Reject pass-through arguments that would break the overlay safety model.

    Matching is done on the flag name after stripping leading dashes so that
    ``-target``, ``--target``, ``-target=addr`` and ``-lock=false`` are all
    rejected (DESIGN §3.9).
    """
    for arg in args:
        flag = arg.lstrip("-").lower()
        if not flag or not arg.startswith("-"):
            continue
        for forbidden in FORBIDDEN_PASSTHROUGH:
            name = forbidden.lstrip("-")
            if flag == name or flag.startswith(name + "=") or flag.startswith(name + "-"):
                raise ToolError(f"pass-through argument not allowed: {arg}")


class TofuRunner:
    """Run the OpenTofu/Terraform binary against one data dir.

    ``popen_factory`` is a public attribute (default ``subprocess.Popen``) that
    tests can replace to avoid spawning real processes.
    """

    def __init__(
        self,
        binary: str,
        cwd: Path,
        data_dir: Path,
        env: dict[str, str] | None = None,
        stream: Callable[[str], None] | None = None,
    ) -> None:
        self.binary = binary
        self.cwd = Path(cwd)
        self.data_dir = Path(data_dir)
        self.extra_env = dict(env or {})
        self.stream = stream
        self.popen_factory: Callable[..., subprocess.Popen] = subprocess.Popen

    # ------------------------------------------------------------------ env

    def environment(self) -> dict[str, str]:
        """Build the process environment for every tofu invocation."""
        env = {k: v for k, v in os.environ.items() if not k.startswith(CLI_ARGS_ENV_PREFIX)}
        env.update(self.extra_env)
        env["TF_DATA_DIR"] = str(self.data_dir)
        env.setdefault("TF_PLUGIN_CACHE_DIR", str(DEFAULT_PLUGIN_CACHE_DIR))
        env["TF_IN_AUTOMATION"] = "1"
        return env

    # ------------------------------------------------------------------ run

    def _spawn(self, args: list[str], *, capture: bool) -> subprocess.Popen:
        env = self.environment()
        Path(env["TF_PLUGIN_CACHE_DIR"]).mkdir(parents=True, exist_ok=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        cmd = [self.binary, *args]
        kwargs: dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.PIPE}
        kwargs["stderr"] = subprocess.PIPE if capture else subprocess.STDOUT
        try:
            return self.popen_factory(cmd, cwd=str(self.cwd), env=env, text=True, **kwargs)
        except FileNotFoundError as exc:
            raise ToolError(f"binary not found: {self.binary}") from exc

    def _emit(self, line: str) -> None:
        if self.stream is not None:
            self.stream(line.rstrip("\n"))

    def _run(self, args: list[str], *, capture: bool = False) -> tuple[int, str]:
        """Run ``binary args`` and return ``(returncode, output)``.

        - streaming mode (default): stdout+stderr are merged, sent line by line to
          the ``stream`` callback and collected;
        - ``capture``: stdout is returned untouched, stderr is streamed.

        stdin is never inherited: the tool asks its own confirmations and every
        tofu command runs with ``-input=false``.
        """
        proc = self._spawn(args, capture=capture)
        if capture:
            stdout, stderr = proc.communicate()
            for line in (stderr or "").splitlines():
                self._emit(line)
            return proc.returncode, stdout or ""
        collected: list[str] = []
        assert proc.stdout is not None
        for line in proc.stdout:
            collected.append(line)
            self._emit(line)
        proc.wait()
        return proc.returncode, "".join(collected)

    def _fail(self, what: str, rc: int, output: str) -> ToolError:
        msg = f"{self.binary} {what} failed (exit {rc})"
        if self.stream is None and output.strip():
            tail = "\n".join(output.strip().splitlines()[-_TAIL_LINES:])
            msg = f"{msg}:\n{tail}"
        return ToolError(msg)

    def _checked(self, args: list[str], *, capture: bool = False) -> str:
        rc, out = self._run(args, capture=capture)
        if rc != 0:
            raise self._fail(args[0], rc, out)
        return out

    # -------------------------------------------------------------- version

    def version(self) -> str:
        """Return the binary version (e.g. ``1.11.1``)."""
        out = self._checked(["version", "-json"], capture=True)
        try:
            return str(json.loads(out)["terraform_version"])
        except (ValueError, KeyError, TypeError):
            first = out.strip().splitlines()[0] if out.strip() else ""
            token = first.split()[-1] if first else ""
            if not token:
                raise ToolError("could not parse tofu version output") from None
            return token.lstrip("v")

    # ----------------------------------------------------------------- init

    def init(self, cfg: BackendConfig, key: str, *, reconfigure: bool = True) -> None:
        """Initialise the data dir against ``key`` (``-backend-config=key=`` last)."""
        args = ["init", "-input=false"]
        if reconfigure:
            args.append("-reconfigure")
        args.extend(f"-backend-config={f}" for f in cfg.backend_config_files)
        args.extend(f"-backend-config={k}={v}" for k, v in self._backend_values(cfg))
        args.append(f"-backend-config=key={key}")
        self._checked(args)
        self._write_lock_marker()

    @staticmethod
    def _backend_values(cfg: BackendConfig) -> list[tuple[str, str]]:
        values: list[tuple[str, str]] = [("bucket", cfg.bucket)]
        for attr in ("region", "profile", "dynamodb_table", "kms_key_id"):
            value = getattr(cfg, attr, None)
            if value:
                values.append((attr, str(value)))
        if getattr(cfg, "use_lockfile", False):
            values.append(("use_lockfile", "true"))
        return values

    def _cached_backend(self) -> dict | None:
        path = self.data_dir / "terraform.tfstate"
        if not path.is_file():
            return None
        try:
            doc = json.loads(path.read_text())
        except ValueError:
            return None
        backend = doc.get("backend") if isinstance(doc, dict) else None
        return backend if isinstance(backend, dict) else None

    def _lock_digest(self) -> str:
        lock = self.cwd / LOCK_FILE_NAME
        if not lock.is_file():
            return ""
        return hashlib.sha256(lock.read_bytes()).hexdigest()

    def _write_lock_marker(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / LOCK_MARKER_NAME).write_text(self._lock_digest())

    def needs_init(self, key: str) -> bool:
        """True when the data dir is not initialised for ``key`` or the lock file changed."""
        backend = self._cached_backend()
        if backend is None or backend.get("type") != "s3":
            return True
        config = backend.get("config") or {}
        if config.get("key") != key:
            return True
        marker = self.data_dir / LOCK_MARKER_NAME
        if not marker.is_file():
            return True
        return marker.read_text().strip() != self._lock_digest()

    def ensure_backend_key(self, key: str) -> None:
        """Raise ToolError unless the cached backend of the data dir points at ``key``."""
        backend = self._cached_backend()
        if backend is None:
            raise ToolError(f"data dir {self.data_dir} is not initialised (run init)")
        actual = (backend.get("config") or {}).get("key")
        if backend.get("type") != "s3" or actual != key:
            raise ToolError(
                f"data dir {self.data_dir} is bound to backend key {actual!r}, expected {key!r}"
            )

    # ----------------------------------------------------------------- plan

    def plan(
        self,
        out: Path,
        *,
        extra: list[str] | None = None,
        destroy: bool = False,
        targets: list[str] | None = None,
        refresh: bool = True,
    ) -> int:
        """Run ``plan -detailed-exitcode``; return 0 (no changes) or 2 (changes)."""
        extra = list(extra or [])
        validate_passthrough(extra)
        args = ["plan", "-input=false", "-detailed-exitcode", f"-out={out}"]
        if destroy:
            args.append("-destroy")
        if not refresh:
            args.append("-refresh=false")
        args.extend(f"-target={t}" for t in targets or [])
        args.extend(extra)
        rc, output = self._run(args)
        if rc in (0, 2):
            return rc
        raise self._fail("plan", rc, output)

    def show_json(self, planfile: Path) -> dict:
        """Return ``show -json PLANFILE`` parsed."""
        out = self._checked(["show", "-json", str(planfile)], capture=True)
        try:
            doc = json.loads(out)
        except ValueError as exc:
            raise ToolError(f"could not parse `show -json` output for {planfile}") from exc
        if not isinstance(doc, dict):
            raise ToolError(f"unexpected `show -json` output for {planfile}")
        return doc

    # ---------------------------------------------------------------- apply

    def apply(self, planfile: Path, *, auto_approve: bool = True) -> None:
        """Apply a saved plan, streaming its output.

        A saved plan file is its own approval for tofu/terraform (they never
        prompt for one), so the confirmation must happen before this call;
        ``auto_approve`` only adds the (ignored) ``-auto-approve`` flag.
        """
        args = ["apply", "-input=false"]
        if auto_approve:
            args.append("-auto-approve")
        args.append(str(planfile))
        rc, output = self._run(args)
        if rc != 0:
            raise self._fail("apply", rc, output)

    # ---------------------------------------------------------------- state

    def state_pull(self) -> dict:
        """Return the remote state document; an empty document is an error."""
        out = self._checked(["state", "pull"], capture=True)
        if not out.strip():
            raise ToolError("state pull returned an empty document (missing state?)")
        try:
            doc = json.loads(out)
        except ValueError as exc:
            raise ToolError("state pull returned invalid JSON") from exc
        if not isinstance(doc, dict):
            raise ToolError("state pull returned an unexpected document")
        return doc

    def state_push(self, doc: dict, *, force: bool = False) -> None:
        """Write ``doc`` to a temp file inside the data dir and ``state push`` it."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix="push-", suffix=".tfstate", dir=str(self.data_dir))
        path = Path(tmp)
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(doc, fh)
            args = ["state", "push"]
            if force:
                args.append("-force")
            args.append(str(path))
            self._checked(args)
        finally:
            path.unlink(missing_ok=True)

    def state_rm(self, addresses: list[str]) -> None:
        """Remove ``addresses`` from the state (no-op on an empty list)."""
        if not addresses:
            return
        self._checked(["state", "rm", *addresses])
