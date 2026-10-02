"""Offline development loop.

The single most useful thing in cdr_report_app's CLI was being able to exercise the
pipeline without the API, the job system or a database. This is the same idea:
``sherlocks capabilities`` answers "why did nothing run" in one second, and
``sherlocks osint --no-db`` runs a whole scan and renders a PDF without writing a row.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from sherlocks.logging_config import configure_logging
from sherlocks.osint.models import OsintSubject
from sherlocks.settings import Settings, load_settings

logger = logging.getLogger(__name__)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sherlocks", description="Sherlocks CLI.")
    parser.add_argument("--config", help="Path to a YAML config file.")
    parser.add_argument("--env-file", help="Path to a .env file.")
    parser.add_argument("-v", "--verbose", action="store_true", help="DEBUG logging.")

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("capabilities", help="Report what this deployment can run, and why not.")

    init = sub.add_parser("init-db", help="Create the Sherlocks schema and tables.")
    init.add_argument("--yes", action="store_true", help="Do not prompt.")

    osint = sub.add_parser("osint", help="Run an OSINT scan against one subject.")
    osint.add_argument("--name")
    osint.add_argument("--phone")
    osint.add_argument("--email")
    osint.add_argument("--username")
    osint.add_argument("--cnic")
    osint.add_argument("--city")
    osint.add_argument("--employer")
    osint.add_argument("--notes")
    osint.add_argument("--output", help="Where to write the PDF.")
    osint.add_argument("--no-pdf", action="store_true", help="Skip rendering.")
    osint.add_argument(
        "--no-db",
        action="store_true",
        help="Run without touching Postgres. Nothing is persisted.",
    )
    osint.add_argument("--json", action="store_true", help="Print the report as JSON.")

    plan = sub.add_parser("plan", help="Show the plan for a subject without running it.")
    plan.add_argument("--name")
    plan.add_argument("--phone")
    plan.add_argument("--email")
    plan.add_argument("--username")
    plan.add_argument("--cnic")
    plan.add_argument("--city")
    plan.add_argument("--employer")

    graph = sub.add_parser("graph", help="Build a person link graph from a CNIC and/or mobile.")
    graph.add_argument("identifier", nargs="+", help="A CNIC, a mobile number, or one of each.")
    graph.add_argument("--depth", type=int, help="Hops to expand (1 = the subject's direct links).")
    graph.add_argument("--max-persons", type=int, help="Stop after searching this many people.")
    graph.add_argument("--backend", choices=["demo", "live"], help="Override linkgraph.backend.")
    graph.add_argument("--systems", help="Comma-separated subset, e.g. cro,psrms,old_tenant.")
    graph.add_argument("--no-fir-rosters", action="store_true", help="Do not open FIR reports.")
    graph.add_argument("--caller-id", action="store_true", help="Add Caller ID name tags.")
    graph.add_argument("--no-ai", action="store_true", help="Rules only for address matching.")
    graph.add_argument("--no-db", action="store_true", help="Keep everything in memory.")
    graph.add_argument("--json", help="Write the run (graph and log) to this file.")

    return parser


def _subject_from_args(args: argparse.Namespace) -> OsintSubject:
    return OsintSubject(
        full_name=getattr(args, "name", None),
        phone=getattr(args, "phone", None),
        email=getattr(args, "email", None),
        username=getattr(args, "username", None),
        cnic=getattr(args, "cnic", None),
        city=getattr(args, "city", None),
        employer=getattr(args, "employer", None),
        notes=getattr(args, "notes", None),
    )


def _cmd_capabilities(settings: Settings) -> int:
    from sherlocks.agents.ollama import OllamaClient
    from sherlocks.osint.client import OsintClient
    from sherlocks.render._reportlib import report_app_available

    client = OsintClient(settings)
    print(f"OSINT enabled        : {client.enabled}")
    print(f"openosint importable : {client.library_available}")
    print(f"Bright Data keys     : {settings.osint.brightdata_ready}")
    print(f"Report PDF engine    : {report_app_available(settings)}")

    if settings.ollama.ready:
        with OllamaClient(settings.ollama) as llm:
            reachable = llm.health()
            print(f"Ollama at {settings.ollama.base_url}: {'reachable' if reachable else 'UNREACHABLE'}")
            if reachable:
                print(f"  models: {', '.join(llm.available_models()) or '(none)'}")
    else:
        print("Ollama               : disabled in configuration")

    print("\nTools:")
    for row in client.capability_report():
        mark = "OK " if row["runnable"] else "-- "
        print(f"  {mark}{row['tool']:<20}{row['skip_reason'] or ''}")
    return 0


def _cmd_init_db(settings: Settings, args: argparse.Namespace) -> int:
    from sherlocks.db.session import init_db

    target = f"{settings.database.url} (schema '{settings.database.schema_name}')"
    if not args.yes:
        reply = input(f"Create Sherlocks tables in {target}? [y/N] ").strip().lower()
        if reply not in ("y", "yes"):
            print("Aborted.")
            return 1
    init_db(settings)
    print(f"Schema and tables ready in {target}")
    return 0


def _cmd_plan(settings: Settings, args: argparse.Namespace) -> int:
    from sherlocks.services.osint_service import build_agent

    subject = _subject_from_args(args)
    if subject.is_empty():
        print("Supply at least one of --name --phone --email --username --cnic")
        return 2

    plan = build_agent(settings).plan(subject)
    print(f"Subject : {plan.subject_label}")
    print(f"Reason  : {plan.reasoning}")
    if not plan.tools:
        print("No tool would run.")
        return 0
    for item in plan.tools:
        mark = "OK " if item.runnable else "-- "
        suffix = "" if item.runnable else f"  ({item.skip_reason})"
        print(f"  {mark}{item.tool:<20}{item.query}{suffix}")
    return 0


def _cmd_osint(settings: Settings, args: argparse.Namespace) -> int:
    subject = _subject_from_args(args)
    if subject.is_empty():
        print("Supply at least one of --name --phone --email --username --cnic")
        return 2

    if not settings.osint.enabled:
        print(
            "OSINT is disabled. Set osint.enabled (or SHERLOCKS_OSINT_ENABLED=true) - "
            "reaching the public internet on behalf of an investigation is a policy call."
        )
        return 3

    if args.no_db:
        from sherlocks.services.osint_service import build_agent

        report = build_agent(settings).run(subject)
        pdf_path = None
        if not args.no_pdf:
            from sherlocks.render.osint_report import render_osint_report

            pdf_path = render_osint_report(report, args.output, settings)
    else:
        from sherlocks.services.osint_service import run_osint

        outcome = run_osint(subject, settings=settings, render_pdf=not args.no_pdf)
        report = outcome.report
        pdf_path = outcome.pdf_path
        print(f"Run id  : {outcome.run_id}")

    if args.json:
        print(json.dumps(report.model_dump(mode="json"), indent=2, ensure_ascii=False))
        return 0

    print(f"Subject : {report.subject.label()}")
    print(f"Outcome : {report.counts}")
    for result in report.results:
        mark = "OK " if result.is_hit else "-- "
        detail = result.error or result.summary
        print(f"  {mark}{result.tool:<20}{(detail or '')[:90]}")
    if report.discovered_identifiers:
        print("\nCandidate identifiers (unverified):")
        for item in report.discovered_identifiers:
            print(f"  {item['kind']:<16}{item['value']}  [{item['tool']}]")
    if pdf_path:
        print(f"\nPDF     : {pdf_path}")
    return 0


def _cmd_graph(settings: Settings, args: argparse.Namespace) -> int:
    from sherlocks.linkgraph.models import GraphRunParams
    from sherlocks.linkgraph.normalize import detect_identifier
    from sherlocks.linkgraph.runs import RunManager, memory_manager

    cnic = phone = None
    for token in args.identifier:
        found = detect_identifier(token)
        if not found:
            print(f"Not a CNIC or Pakistani mobile: {token}")
            return 2
        if found[0] == "cnic":
            cnic = found[1]
        else:
            phone = found[1]

    lg = settings.linkgraph
    backend = args.backend or lg.backend
    lg.allow_backend_override = True  # a flag typed on the command line is explicit enough
    if backend == "live":
        print("LIVE mode: every person searched is ~20 queries logged by the upstream systems.")
    params = GraphRunParams(
        cnic=cnic, phone=phone, depth=args.depth or lg.default_depth,
        max_persons=args.max_persons or lg.default_max_persons,
        systems=[s.strip() for s in args.systems.split(",")] if args.systems else None,
        include_fir_rosters=not args.no_fir_rosters, include_caller_id=args.caller_id,
        ai_address_matching=not args.no_ai, backend=backend,
    )
    if args.no_db:
        manager = memory_manager(settings)
    else:
        from sherlocks.db.session import init_db

        init_db(settings)
        manager = RunManager(settings)
    handle = manager.start(params, created_by="cli", wait=True)
    run = manager.get(handle.id) or {}
    graph = run.get("graph", {"nodes": [], "edges": []})
    nodes = {n["id"]: n for n in graph["nodes"]}

    print(f"\nRun {handle.id} · {run.get('status')} · {run.get('message')}")
    print("Stats:", ", ".join(f"{k}={v}" for k, v in (run.get("stats") or {}).items()))
    print("\nPeople:")
    for node in sorted((n for n in graph["nodes"] if n["kind"] == "person"), key=lambda n: n["data"]["depth"]):
        d = node["data"]
        flags = f" [{', '.join(d['flags'])}]" if d["flags"] else ""
        print(f"  d{d['depth']} {node['label'][:28]:<28} {d['cnic'] or '-':<14} {', '.join(d['phones'][:2]) or '-':<25} "
              f"{d['search_status']}{flags}")

    owners: dict[str, list[str]] = {}
    for edge in graph["edges"]:
        if edge["kind"] == "found_in":
            owners.setdefault(edge["target"], []).append(edge["source"])
    print("\nStrong links (stated by a system):")
    for edge in graph["edges"]:
        if edge["kind"] != "strong":
            continue
        sources = owners.get(edge["source"], [edge["source"]]) if edge["source"].startswith("s:") else [edge["source"]]
        via = nodes[edge["source"]]["label"] if edge["source"].startswith("s:") else edge.get("system") or ""
        for source in sources:
            if source != edge["target"]:
                print(f"  {nodes[source]['label'][:24]:<24} → {nodes[edge['target']]['label'][:24]:<24} {edge['label']}  ({via})")
    print("\nWeak links (inferred - leads, not evidence):")
    for edge in sorted((e for e in graph["edges"] if e["kind"] == "weak"), key=lambda e: -(e["score"] or 0)):
        print(f"  {edge['score']:.2f} {nodes[edge['source']]['label'][:24]:<24} ~ {nodes[edge['target']]['label'][:24]:<24} {edge['label']}")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle_out:
            json.dump(run, handle_out, ensure_ascii=False, indent=1, default=str)
        print(f"\nWritten: {args.json}")
    return 0 if run.get("status") == "completed" else 1


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    settings = load_settings(config_path=args.config, env_file=args.env_file)
    if args.verbose:
        settings.app.log_level = "DEBUG"
    configure_logging(settings, force=True)

    if args.command == "capabilities":
        return _cmd_capabilities(settings)
    if args.command == "init-db":
        return _cmd_init_db(settings, args)
    if args.command == "plan":
        return _cmd_plan(settings, args)
    if args.command == "osint":
        return _cmd_osint(settings, args)
    if args.command == "graph":
        return _cmd_graph(settings, args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
