"""CDR sandbox: runs the CDR analyst's code over the call records, safely.

The analyst (a model) writes a few lines of Python that compute an answer from ``calls`` with the functions of
``cdr_tools``. That code is untrusted. It runs here:

* **checked first** (:func:`check`): parsed, never executed, and refused if it imports anything but a few
  standard modules, touches a name such as ``open``, ``exec``, ``eval``, ``getattr`` or ``__import__``, or any
  ``_private`` / ``__dunder__`` attribute;
* **in its own process**: ``python -I`` (no site packages, no environment), an empty environment (no keys,
  no paths), a fresh temporary folder that is deleted after, and limits on CPU time, memory, file size and
  processes, with a wall-clock timeout;
* with restricted builtins: only plain data functions, and an ``__import__`` that allows only the modules
  above.

The records go in as a JSON file; what the code puts in ``result`` comes back as JSON, cut to a size the
model can read. The records themselves never reach the model.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ALLOWED_MODULES = {"math", "statistics", "collections", "datetime", "itertools", "re", "json", "functools",
                   "operator", "string"}
FORBIDDEN_NAMES = {"open", "exec", "eval", "compile", "__import__", "globals", "locals", "vars", "getattr", "setattr",
                   "delattr", "input", "breakpoint", "help", "exit", "quit", "memoryview", "classmethod",
                   "staticmethod", "super", "property", "type", "object", "__builtins__", "__loader__", "__spec__"}
TIMEOUT_S = 30
RESULT_CHARS = 20000
TOOLS = Path(__file__).with_name("cdr_tools.py")

_HARNESS = r'''
import builtins, json, resource, sys
resource.setrlimit(resource.RLIMIT_CPU, (__CPU__, __CPU__))
resource.setrlimit(resource.RLIMIT_AS, (1536 * 1024 * 1024, 1536 * 1024 * 1024))
resource.setrlimit(resource.RLIMIT_FSIZE, (1024 * 1024, 1024 * 1024))
resource.setrlimit(resource.RLIMIT_NPROC, (0, 0))
import collections, datetime, functools, itertools, math, operator, re, statistics, string
import importlib.util
spec = importlib.util.spec_from_file_location("cdr_tools", sys.argv[1])
tools = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tools)
calls = json.load(open(sys.argv[2], encoding="utf-8"))
code = open(sys.argv[3], encoding="utf-8").read()
ALLOWED = __ALLOWED__
def _import(name, globals=None, locals=None, fromlist=(), level=0):
    if name.split(".")[0] not in ALLOWED:
        raise ImportError("not allowed: " + name)
    return __import__(name, globals, locals, fromlist, level)
SAFE = {k: getattr(builtins, k) for k in (
    "abs", "all", "any", "bool", "dict", "divmod", "enumerate", "filter", "float", "frozenset", "int", "isinstance",
    "len", "list", "map", "max", "min", "print", "range", "reversed", "round", "set", "slice", "sorted", "str", "sum",
    "tuple", "zip", "Exception", "ValueError", "KeyError", "TypeError", "IndexError", "ZeroDivisionError", "None",
    "True", "False")}
SAFE["__import__"] = _import
scope = {"__builtins__": SAFE, "calls": calls, "result": None}
scope.update({f.__name__: f for f in tools.FUNCTIONS})
for m in (collections, datetime, functools, itertools, math, operator, re, statistics, string, json):
    scope[m.__name__] = m
printed = []
SAFE["print"] = lambda *a, **k: printed.append(" ".join(str(x) for x in a))
try:
    exec(compile(code, "<analyst>", "exec"), scope)
    out = {"ok": True, "result": scope.get("result"), "printed": printed[-20:]}
except Exception as exc:
    out = {"ok": False, "error": type(exc).__name__ + ": " + str(exc)[:500], "printed": printed[-20:]}
sys.stdout.write(json.dumps(out, default=str, ensure_ascii=False))
'''


class Refused(ValueError):
    """The code is not allowed to run."""


def check(code: str) -> None:
    """Refuse code that could reach outside the records. Raises :class:`Refused` with the reason."""
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        raise Refused(f"syntax error: {exc.msg} (line {exc.lineno})") from exc
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] not in ALLOWED_MODULES:
                    raise Refused(f"import of {alias.name} is not allowed")
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] not in ALLOWED_MODULES or node.level:
                raise Refused(f"import from {node.module} is not allowed")
        elif isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            raise Refused(f"{node.id} is not allowed")
        elif isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            raise Refused(f"attribute {node.attr} is not allowed")
        elif isinstance(node, (ast.Global, ast.Nonlocal, ast.AsyncFunctionDef, ast.Await)):
            raise Refused(f"{type(node).__name__} is not allowed")


def run(code: str, calls: list[dict[str, Any]], *, timeout: int = TIMEOUT_S, workdir: str | Path | None = None) -> dict[str, Any]:
    """Run checked code over ``calls``. ``{ok, result, printed, error, ms}``; never raises for the code's errors."""
    started = time.monotonic()
    try:
        check(code)
    except Refused as exc:
        return {"ok": False, "error": f"refused: {exc}", "ms": 0}
    harness = _HARNESS.replace("__CPU__", str(int(timeout))).replace("__ALLOWED__", repr(sorted(ALLOWED_MODULES)))
    with tempfile.TemporaryDirectory(prefix="sherlocks-cdr-", dir=workdir) as tmp:
        folder = Path(tmp)
        (folder / "harness.py").write_text(harness, encoding="utf-8")
        (folder / "calls.json").write_text(json.dumps(calls, default=str, ensure_ascii=False), encoding="utf-8")
        (folder / "code.py").write_text(code, encoding="utf-8")
        try:
            done = subprocess.run(  # fixed argv; the code is checked and runs with no environment
                [sys.executable, "-I", "-S", str(folder / "harness.py"), str(TOOLS), str(folder / "calls.json"),
                 str(folder / "code.py")],
                cwd=folder, env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}, capture_output=True, text=True,
                timeout=timeout + 5, start_new_session=True, check=False)
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": f"timed out after {timeout}s", "ms": int((time.monotonic() - started) * 1000)}
    ms = int((time.monotonic() - started) * 1000)
    try:
        out = json.loads(done.stdout or "{}")
    except json.JSONDecodeError:
        return {"ok": False, "error": (done.stderr or done.stdout or "no output")[-500:], "ms": ms}
    text = json.dumps(out.get("result"), default=str, ensure_ascii=False)
    if len(text) > RESULT_CHARS:
        out["result"] = text[:RESULT_CHARS] + " ... (cut)"
        out["cut"] = True
    out["ms"] = ms
    return out
