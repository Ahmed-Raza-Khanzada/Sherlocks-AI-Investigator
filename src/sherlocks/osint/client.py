"""Bridge between Sherlocks and the OpenOSINT tool library.

Three facts about OpenOSINT shape everything here, all of them established by
reading its source rather than its README:

1. **Every entrypoint is an ``async def`` that returns a string and never raises.**
   Failures come back as prose - ``"Scan error: ..."``, ``"Internal error: ..."``,
   ``"'holehe' is not installed or not in PATH."``. So the status of a run cannot be
   read from an exception; it has to be classified from the text. That is what
   :func:`classify_output` does, and it is the most fragile part of this module -
   see the note there before changing an OpenOSINT version.

2. **Three tools shell out to external binaries** (phoneinfoga, sherlock, holehe) and
   three more call a billed Bright Data API. Both classes of prerequisite are checked
   *before* dispatch so a run can report why a tool was skipped rather than burning a
   timeout or a billed request to find out.

3. **A tool can hang in a way ``asyncio`` cannot cancel.** The Bright Data tools do
   their HTTP work inside ``asyncio.to_thread``; ``wait_for`` abandons the awaitable
   but the thread keeps running. So by default each tool executes in its own spawned
   process, which can be killed outright. ``isolate=False`` runs in-process and is
   for tests and the CLI only.

Bright Data credentials are passed to the tools as an explicit ``api_keys`` mapping.
They are never written into ``os.environ`` of this process - OpenOSINT falls back to
the environment, and we do not want a key leaking into every other subprocess the
worker spawns.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import multiprocessing as mp
import time
from dataclasses import dataclass
from typing import Any

from sherlocks.osint.models import FindingStatus, PlannedTool, ToolResult
from sherlocks.osint.tools import TOOL_CATALOG, ToolSpec, binary_on_path
from sherlocks.settings import OsintSettings, Settings, load_settings

logger = logging.getLogger(__name__)

# Grace period added to a tool's own timeout before the supervising process is killed.
# The tool should always time out first and return its own message; this is the backstop.
_KILL_GRACE_SECONDS = 20

# Substrings OpenOSINT emits when a prerequisite is missing rather than when a scan
# genuinely failed. These map to NOT_CONFIGURED, which is an operator problem, not an
# investigative finding.
_NOT_CONFIGURED_MARKERS = (
    "environment variable is not set",
    "is not installed or not in path",
    "library is not installed",
)

# Substrings meaning "the tool ran and found nothing". Distinct from an error: a clean
# no-result is a real answer about the subject, an error tells us nothing at all.
_NO_RESULT_MARKERS = (
    "no data found",
    "no accounts found",
    "no registered services found",
    "no results",
    "(empty response body)",
)

_ERROR_PREFIXES = (
    "scan error:",
    "internal error:",
    "invalid input:",
    "invalid url:",
    "error:",
)


class OsintDisabled(RuntimeError):
    """Raised when a run is attempted while ``osint.enabled`` is false."""


class ToolNotAllowed(RuntimeError):
    """Raised when a tool outside the configured allowlist is requested."""


@dataclass(slots=True)
class _RawOutcome:
    """What actually came back from the tool process, before classification."""

    text: str
    failed: bool = False
    error: str | None = None


def classify_output(text: str) -> tuple[FindingStatus, str | None]:
    """Map a tool's returned string onto a :class:`FindingStatus`.

    Returns ``(status, error_message)``. OpenOSINT has no machine-readable result type,
    so this is prefix and substring matching against its message constants. When
    upgrading OpenOSINT, re-check ``_NOT_CONFIGURED_MARKERS``, ``_NO_RESULT_MARKERS``
    and ``_ERROR_PREFIXES`` against its ``tools/*.py`` before trusting a run.
    """
    stripped = (text or "").strip()
    if not stripped:
        return FindingStatus.NO_RESULT, None

    lowered = stripped.lower()

    for marker in _NOT_CONFIGURED_MARKERS:
        if marker in lowered:
            return FindingStatus.NOT_CONFIGURED, stripped.splitlines()[0]

    if lowered.startswith("timeout") or "timed out after" in lowered:
        return FindingStatus.ERROR, stripped.splitlines()[0]

    for prefix in _ERROR_PREFIXES:
        if lowered.startswith(prefix):
            return FindingStatus.ERROR, stripped.splitlines()[0]

    for marker in _NO_RESULT_MARKERS:
        if marker in lowered:
            return FindingStatus.NO_RESULT, None

    return FindingStatus.OK, None


def _call_tool_sync(spec: ToolSpec, query: str, kwargs: dict[str, Any]) -> str:
    """Import the tool and drive its coroutine to completion. Runs in the child.

    Some tools (sherlock especially) write a ``<query>.txt`` results file into the
    current working directory. Run inside a scratch dir so those never litter the
    project root; the file is discarded, only the returned text matters.
    """
    import contextlib
    import os
    import tempfile
    from pathlib import Path

    module = importlib.import_module(spec.module)
    func = getattr(module, spec.entrypoint)

    workdir = os.environ.get("SHERLOCKS_OSINT_WORKDIR")
    prev = Path.cwd()
    target = Path(workdir) if workdir else Path(tempfile.gettempdir()) / "sherlocks_osint"
    try:
        target.mkdir(parents=True, exist_ok=True)
        os.chdir(target)
    except OSError:
        target = None  # if we cannot move, run where we are rather than fail the tool
    try:
        return asyncio.run(func(query, **kwargs))
    finally:
        if target is not None:
            with contextlib.suppress(OSError):
                os.chdir(prev)


def _child_entry(conn: Any, spec: ToolSpec, query: str, kwargs: dict[str, Any]) -> None:
    """Process entrypoint for isolated execution.

    Sends ``(ok: bool, payload: str)`` back over the pipe. Nothing here may raise past
    the pipe, or the parent would see a silent empty result instead of a reason.
    """
    try:
        conn.send((True, _call_tool_sync(spec, query, kwargs)))
    except BaseException as exc:  # noqa: BLE001 - the reason must reach the parent
        conn.send((False, f"{type(exc).__name__}: {exc}"))
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001, S110 - the child is exiting; a failed close
            pass  # has nowhere left to be reported


class OsintClient:
    """Runs catalogued OpenOSINT tools under an allowlist, a timeout and a rate limit.

    The client makes no decisions about *what* to investigate - that is the agent's
    job. It only enforces that whatever was decided is permitted, prerequisites are
    present, and a single hung tool cannot stall a job.
    """

    def __init__(
        self,
        settings: Settings | OsintSettings | None = None,
        *,
        isolate: bool = True,
    ) -> None:
        if settings is None:
            settings = load_settings()
        self.settings: OsintSettings = (
            settings.osint if isinstance(settings, Settings) else settings
        )
        self.isolate = isolate
        self._ctx = mp.get_context("spawn")
        self._last_call_at: float = 0.0

    # -- capability reporting -------------------------------------------------

    @property
    def enabled(self) -> bool:
        return bool(self.settings.enabled)

    @property
    def library_available(self) -> bool:
        """Whether the ``openosint`` package can be imported at all."""
        try:
            importlib.import_module("openosint")
        except Exception:  # noqa: BLE001 - any import failure means "not usable here"
            return False
        return True

    def api_keys(self) -> dict[str, str]:
        """Credentials handed to the Bright Data tools, empty entries omitted."""
        pairs = {
            "BRIGHTDATA_API_KEY": self.settings.brightdata_api_key,
            "BRIGHTDATA_SERP_ZONE": self.settings.brightdata_serp_zone,
            "BRIGHTDATA_UNLOCKER_ZONE": self.settings.brightdata_unlocker_zone,
            "HIBP_API_KEY": self.settings.hibp_api_key,
            "GITHUB_TOKEN": self.settings.github_token,
            "IPINFO_TOKEN": self.settings.ipinfo_token,
        }
        return {key: value for key, value in pairs.items() if value}

    def allowed_tools(self) -> list[str]:
        """Catalogued tools that configuration also permits, catalogue order kept."""
        allowed = set(self.settings.allowed_tools or [])
        return [name for name in TOOL_CATALOG if name in allowed]

    def skip_reason(self, tool: str) -> str | None:
        """Why ``tool`` cannot run right now, or ``None`` if it can.

        Checked before dispatch so a report can say "sherlock is not installed"
        instead of showing an empty section.
        """
        spec = TOOL_CATALOG.get(tool)
        if spec is None:
            return f"unknown tool '{tool}'"
        if tool not in self.allowed_tools():
            return f"'{tool}' is not in the configured allowlist"
        if not self.library_available:
            return "the openosint package is not installed"
        if spec.requires_binary and not binary_on_path(spec.requires_binary):
            return f"'{spec.requires_binary}' is not installed or not on PATH"
        if spec.requires_package:
            import importlib.util

            if importlib.util.find_spec(spec.requires_package) is None:
                return f"the '{spec.requires_package}' package is not installed"
        if spec.requires_brightdata and not self.settings.brightdata_ready:
            return "no Bright Data API key and SERP zone are configured"
        if spec.requires_key and not getattr(self.settings, spec.api_key_setting or "", None):
            return f"no {spec.api_key_setting} configured (paid key - add it in .env)"
        if spec.name == "search_enformion" and not self.settings.enformion_api_profile:
            return "no enformion_api_profile configured (ENFORMION_API_PROFILE in .env)"
        return None

    def runnable_tools(self) -> list[str]:
        return [name for name in self.allowed_tools() if self.skip_reason(name) is None]

    def capability_report(self) -> list[dict[str, Any]]:
        """One row per catalogued tool - for the CLI and the API health endpoint."""
        rows: list[dict[str, Any]] = []
        for name, spec in TOOL_CATALOG.items():
            reason = self.skip_reason(name)
            rows.append(
                {
                    "tool": name,
                    "accepts": spec.accepts.value,
                    "description": spec.description,
                    "requires_binary": spec.requires_binary,
                    "requires_brightdata": spec.requires_brightdata,
                    "runnable": reason is None,
                    "skip_reason": reason,
                }
            )
        return rows

    # -- execution ------------------------------------------------------------

    def _timeout_for(self, spec: ToolSpec) -> int:
        """A tool never gets longer than the configured per-tool ceiling."""
        return max(5, min(spec.default_timeout, self.settings.per_tool_timeout_seconds))

    def _tool_kwargs(self, spec: ToolSpec, timeout: int) -> dict[str, Any]:
        kwargs: dict[str, Any] = dict(spec.extra_kwargs)
        # generate_dorks takes only a target - passing a timeout would be a TypeError.
        if spec.name != "generate_dorks":
            kwargs["timeout_seconds"] = timeout
        if spec.requires_brightdata:
            kwargs["api_keys"] = self.api_keys()
        # Single-key tools (GitHub token, HIBP key, Apify token) take the key as api_key=.
        if spec.api_key_setting:
            key = getattr(self.settings, spec.api_key_setting, None)
            if key:
                kwargs["api_key"] = key
        # Apify: which actor and input field to use are configurable per deployment.
        if spec.name == "search_apify":
            kwargs["actor"] = self.settings.apify_actor
            kwargs["input_field"] = self.settings.apify_input_field
        if spec.name == "search_socialcrawl":
            kwargs["platforms"] = list(self.settings.socialcrawl_platforms)
        if spec.name == "search_enformion":
            kwargs["profile"] = self.settings.enformion_api_profile
        return kwargs

    def _respect_rate_limit(self) -> None:
        delay = float(self.settings.delay_between_calls_seconds or 0)
        if delay <= 0 or self._last_call_at == 0.0:
            return
        elapsed = time.monotonic() - self._last_call_at
        if elapsed < delay:
            time.sleep(delay - elapsed)

    def _run_isolated(self, spec: ToolSpec, query: str, kwargs: dict[str, Any], timeout: int) -> _RawOutcome:
        parent_conn, child_conn = self._ctx.Pipe(duplex=False)
        process = self._ctx.Process(
            target=_child_entry,
            args=(child_conn, spec, query, kwargs),
            daemon=True,
        )
        process.start()
        child_conn.close()

        deadline = timeout + _KILL_GRACE_SECONDS
        payload: tuple[bool, str] | None = None
        try:
            if parent_conn.poll(deadline):
                payload = parent_conn.recv()
        except EOFError:
            payload = None
        finally:
            parent_conn.close()

        process.join(timeout=5)
        if process.is_alive():
            # The backstop the whole isolation mode exists for.
            logger.warning("osint tool %s exceeded %ss; killing pid %s", spec.name, deadline, process.pid)
            process.kill()
            process.join(timeout=5)
            return _RawOutcome(
                text="",
                failed=True,
                error=f"'{spec.name}' exceeded {deadline}s and was terminated",
            )

        if payload is None:
            return _RawOutcome(
                text="",
                failed=True,
                error=f"'{spec.name}' produced no output (exit code {process.exitcode})",
            )

        ok, text = payload
        if not ok:
            return _RawOutcome(text="", failed=True, error=text)
        return _RawOutcome(text=text)

    def _run_inline(self, spec: ToolSpec, query: str, kwargs: dict[str, Any], timeout: int) -> _RawOutcome:
        """In-process execution. Cannot kill a hung thread - tests and CLI only."""
        try:
            return _RawOutcome(text=_call_tool_sync(spec, query, kwargs))
        except Exception as exc:  # noqa: BLE001 - mirrors the isolated path's contract
            return _RawOutcome(text="", failed=True, error=f"{type(exc).__name__}: {exc}")

    def run_tool(self, tool: str, query: str) -> ToolResult:
        """Run one tool and classify the outcome. Never raises for a tool failure.

        Raises only on a policy violation - OSINT disabled, or a tool outside the
        allowlist - because those are programming errors, not investigative outcomes.
        """
        if not self.enabled:
            raise OsintDisabled(
                "osint.enabled is false; enable it deliberately before reaching the public internet"
            )
        spec = TOOL_CATALOG.get(tool)
        if spec is None or tool not in self.allowed_tools():
            raise ToolNotAllowed(f"'{tool}' is not an allowed OSINT tool")

        query = (query or "").strip()
        started = time.monotonic()

        if not query:
            return ToolResult(
                tool=tool,
                query="",
                status=FindingStatus.SKIPPED,
                summary="No query value available for this tool.",
                error="empty query",
            )

        reason = self.skip_reason(tool)
        if reason is not None:
            return ToolResult(
                tool=tool,
                query=query,
                status=FindingStatus.NOT_CONFIGURED,
                summary=f"Skipped: {reason}.",
                error=reason,
            )

        timeout = self._timeout_for(spec)
        kwargs = self._tool_kwargs(spec, timeout)

        self._respect_rate_limit()
        logger.info("osint: running %s against %r (timeout %ss)", tool, query, timeout)
        try:
            if self.isolate:
                outcome = self._run_isolated(spec, query, kwargs, timeout)
            else:
                outcome = self._run_inline(spec, query, kwargs, timeout)
        finally:
            self._last_call_at = time.monotonic()

        duration_ms = int((time.monotonic() - started) * 1000)

        if outcome.failed:
            logger.warning("osint: %s failed - %s", tool, outcome.error)
            return ToolResult(
                tool=tool,
                query=query,
                status=FindingStatus.ERROR,
                summary=f"{tool} failed to run.",
                error=outcome.error,
                duration_ms=duration_ms,
            )

        status, error = classify_output(outcome.text)
        return ToolResult(
            tool=tool,
            query=query,
            status=status,
            # The raw text is kept verbatim: it is the evidence trail for a finding
            # an analyst may later have to justify. Parsing happens downstream.
            data={"raw": outcome.text},
            summary=_first_line(outcome.text),
            error=error,
            duration_ms=duration_ms,
        )

    def run_planned(self, planned: list[PlannedTool]) -> list[ToolResult]:
        """Execute a plan in order, honouring ``max_tools_per_run``.

        Tools the planner already marked unrunnable are recorded as skipped rather
        than dropped - a report that silently omits a tool is worse than one that says
        why it did not run.
        """
        results: list[ToolResult] = []
        budget = max(0, int(self.settings.max_tools_per_run or 0))
        spent = 0

        for item in planned:
            if not item.runnable:
                # Distinguish "this machine cannot run it" from "the planner chose not
                # to". Both read as "not run" in a report, but only the first is an
                # operator's problem to fix.
                status = (
                    FindingStatus.NOT_CONFIGURED
                    if self.skip_reason(item.tool) is not None
                    else FindingStatus.SKIPPED
                )
                results.append(
                    ToolResult(
                        tool=item.tool,
                        query=item.query,
                        status=status,
                        summary=f"Skipped: {item.skip_reason}.",
                        error=item.skip_reason,
                    )
                )
                continue
            if budget and spent >= budget:
                results.append(
                    ToolResult(
                        tool=item.tool,
                        query=item.query,
                        status=FindingStatus.SKIPPED,
                        summary=f"Skipped: run budget of {budget} tools was exhausted.",
                        error="max_tools_per_run reached",
                    )
                )
                continue
            spent += 1
            results.append(self.run_tool(item.tool, item.query))

        return results


def _first_line(text: str, limit: int = 240) -> str:
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:limit]
    return ""
