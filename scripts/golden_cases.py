#!/usr/bin/env python
"""Run the golden cases against the configured model and print the measures.

    .venv/bin/python scripts/golden_cases.py [DIR] [--save scores.json] [--against scores.json]

DIR defaults to tests/fixtures/golden. Exits 1 when a case misses a bar, or - with
``--against`` - does worse than the scores saved from an earlier run. Run it on every
change to prompts, models, rules or tolerances.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main() -> int:
    from sherlocks.agents.llm import build_llm, llm_label
    from sherlocks.settings import load_settings
    from sherlocks.team import golden

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("dir", nargs="?", default="tests/fixtures/golden")
    parser.add_argument("--save", help="write the scores here (the starting numbers for the next change)")
    parser.add_argument("--against", help="scores saved earlier: fail if a measure got worse")
    args = parser.parse_args()
    settings = load_settings()
    llm = build_llm(settings)
    if llm is None:
        print("No model is configured (llm.* / ollama.*): the golden cases measure the model's work.", file=sys.stderr)
        return 2
    before = json.loads(Path(args.against).read_text()) if args.against else {}
    print(f"Model: {llm_label(settings)}")
    scores, failed = {}, 0
    for path in sorted(Path(args.dir).glob("*.json")):
        spec = golden.load(path)
        out = golden.run(spec, llm)
        s = out["scores"]
        scores[path.stem] = s
        problems = golden.failures(s, before.get(path.stem))
        failed += bool(problems)
        print(f"\n{spec.get('name') or path.stem}  ({out['seconds']}s)")
        print(f"  contradictions found   {s['contradictions_found']:.0%} of {s['contradictions_planted']} planted")
        print(f"  false contradictions   {s['false_contradictions']}")
        print(f"  lead that mattered     rank {s['lead_rank'] or '-'}")
        print(f"  right hypothesis first {s['right_hypothesis_first']}")
        print(f"  wrong merges           {s['wrong_merges']} {s['wrong_merge_names'] or ''}")
        print(f"  speculation as fact    {s['speculation_as_fact']}")
        for p in problems:
            print(f"  FAIL: {p}")
    if args.save:
        Path(args.save).write_text(json.dumps(scores, indent=1))
    print(f"\n{len(scores)} case(s), {failed} failing a bar")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
