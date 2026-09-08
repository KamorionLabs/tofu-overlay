"""Console output: human messages, tables, CI annotations, JSON mode and confirmations.

Rules:

- Human-readable messages (info/warn/error/success, backend line) always go to stderr.
- In ``json_mode`` stdout carries exactly one JSON document (:meth:`Console.json`);
  everything else, including the tofu passthrough stream and tables, goes to stderr.
- In CI colour is disabled and, on Azure DevOps (``TF_BUILD=True``), errors and warnings
  are also emitted as ``##vso[task.logissue ...]`` logging commands.
- Confirmations are never interactive in CI: they return False unless ``--yes`` was given.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from typing import Any, TextIO

from rich.console import Console as RichConsole
from rich.markup import escape
from rich.table import Table

from tofu_overlay.models import BackendConfig

JSON_SCHEMA_VERSION = 1


def _is_ado() -> bool:
    """True when running inside an Azure DevOps pipeline (``TF_BUILD=True``)."""
    return os.environ.get("TF_BUILD", "").strip().lower() == "true"


def _vso_escape(text: str) -> str:
    """Escape a message for ``##vso`` logging commands (newlines and carriage returns)."""
    return text.replace("\r", "%0D").replace("\n", "%0A")


def _lock_description(cfg: BackendConfig) -> str:
    if cfg.dynamodb_table and cfg.use_lockfile:
        return f"dynamodb:{cfg.dynamodb_table}+lockfile"
    if cfg.dynamodb_table:
        return f"dynamodb:{cfg.dynamodb_table}"
    if cfg.use_lockfile:
        return "lockfile"
    return "none"


class Console:
    """Thin wrapper over rich consoles with JSON and CI modes.

    ``stdout``, ``stderr`` and ``ask`` are plain attributes so tests can inject
    string buffers and a scripted input function.
    """

    def __init__(self, *, json_mode: bool = False, ci: bool = False, no_color: bool = False):
        self.json_mode = json_mode
        self.ci = ci
        self.no_color = no_color or ci
        self.ado = ci and _is_ado()
        self.stdout: TextIO = sys.stdout
        self.stderr: TextIO = sys.stderr
        self.ask: Callable[[str], str] = input
        self._rich_cache: dict[int, RichConsole] = {}

    # --------------------------------------------------------------- plumbing

    def _rich(self, file: TextIO) -> RichConsole:
        """Rich console bound to ``file`` (cached per file object)."""
        cached = self._rich_cache.get(id(file))
        if cached is not None and cached.file is file:
            return cached
        console = RichConsole(
            file=file,
            no_color=self.no_color,
            force_terminal=False if self.no_color else None,
            highlight=False,
            soft_wrap=True,
            emoji=False,
        )
        self._rich_cache[id(file)] = console
        return console

    def _err(self) -> RichConsole:
        return self._rich(self.stderr)

    def _out(self) -> RichConsole:
        """Result stream: stdout normally, stderr when stdout is reserved for JSON."""
        return self._rich(self.stderr if self.json_mode else self.stdout)

    def _vso(self, kind: str, msg: str) -> None:
        """Emit an Azure DevOps logging command (stderr in JSON mode to keep stdout pure)."""
        if not self.ado:
            return
        target = self.stderr if self.json_mode else self.stdout
        target.write(f"##vso[task.logissue type={kind};]{_vso_escape(msg)}\n")
        target.flush()

    # --------------------------------------------------------------- messages

    def info(self, msg: str) -> None:
        """Informational line on stderr."""
        self._err().print(escape(msg))

    def success(self, msg: str) -> None:
        """Success line on stderr."""
        self._err().print(f"[green]ok:[/green] {escape(msg)}")

    def warn(self, msg: str) -> None:
        """Warning line on stderr; also a ``##vso`` warning on Azure DevOps."""
        self._err().print(f"[yellow]warning:[/yellow] {escape(msg)}")
        self._vso("warning", msg)

    def error(self, msg: str) -> None:
        """Error line on stderr; also a ``##vso`` error on Azure DevOps."""
        self._err().print(f"[red]error:[/red] {escape(msg)}")
        self._vso("error", msg)

    def backend_line(self, cfg: BackendConfig) -> None:
        """First line of every command: the resolved backend tuple."""
        parts = [f"backend: s3://{cfg.bucket}/{cfg.state_path()}"]
        if cfg.region:
            parts.append(f"region={cfg.region}")
        if cfg.profile:
            parts.append(f"profile={cfg.profile}")
        parts.append(f"lock={_lock_description(cfg)}")
        if cfg.workspace != "default":
            parts.append(f"workspace={cfg.workspace}")
        self._err().print(f"[bold]{escape(' '.join(parts))}[/bold]")

    def table(self, title: str, columns: list[str], rows: list[list[str]]) -> None:
        """Render a table on the result stream."""
        table = Table(title=title or None, show_lines=False, expand=False)
        for column in columns:
            table.add_column(column)
        for row in rows:
            table.add_row(*(escape(str(cell)) for cell in row))
        self._out().print(table)

    def json(self, payload: dict) -> None:
        """Write the single JSON document of a ``--json`` run to stdout."""
        body: dict[str, Any] = {"schema": JSON_SCHEMA_VERSION, **payload}
        self.stdout.write(json.dumps(body, indent=2, default=str) + "\n")
        self.stdout.flush()

    def stream(self, line: str) -> None:
        """Tofu output passthrough (stdout, or stderr in JSON mode), verbatim."""
        target = self.stderr if self.json_mode else self.stdout
        target.write(line if line.endswith("\n") else line + "\n")
        target.flush()

    # ----------------------------------------------------------- confirmations

    def _interactive_allowed(self, prompt: str) -> bool:
        if self.ci:
            self.error(f"{prompt}: confirmation required; pass --yes in CI")
            return False
        return True

    def _read(self, prompt: str) -> str | None:
        try:
            return self.ask(prompt)
        except (EOFError, KeyboardInterrupt):
            self.stderr.write("\n")
            return None

    def confirm_typed(self, expected: str, prompt: str, *, yes: bool) -> bool:
        """Ask the user to type ``expected`` verbatim. ``--yes`` skips; CI without it -> False."""
        if yes:
            return True
        if not self._interactive_allowed(prompt):
            return False
        self._err().print(escape(prompt))
        answer = self._read(f"Type '{expected}' to continue: ")
        if answer is None or answer.strip() != expected:
            self.error("aborted: confirmation did not match")
            return False
        return True

    def confirm(self, prompt: str, *, yes: bool) -> bool:
        """Yes/no confirmation. ``--yes`` skips; CI without it -> False."""
        if yes:
            return True
        if not self._interactive_allowed(prompt):
            return False
        answer = self._read(f"{prompt} [y/N]: ")
        return answer is not None and answer.strip().lower() in ("y", "yes")
