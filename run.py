#!/usr/bin/env python3
"""Skill regression harness.

    python3 run.py --lint-only      static checks, no model calls, no cost
    python3 run.py                  static checks + live dispatch, renders HTML
    python3 run.py --rerun ID       re-dispatch one case to see if a miss reproduces

Exit codes are the report-by-exception contract:
    0  nothing to act on
    1  findings (lint errors, dispatch failures, or errored calls)
    2  could not run (CLI missing, logged out, bad inputs)
"""

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path

from harness import baseline as baseline_mod
from harness import preflight as preflight_mod
from harness import report as report_mod
from harness import routing
from harness._evalkit import source as evalkit_source
from harness.discover import discover
from harness.lint import load_runtime_paths, run_lint


def load_cases(path: Path) -> tuple[list[dict], list[dict]]:
    data = json.loads(path.read_text())
    return data["cases"], data.get("traps", [])


def rerun(
    cases: list[dict],
    ids: list[str],
    times: int,
    model: str,
    own: frozenset = frozenset(),
    out_path: Path | None = None,
) -> int:
    """Re-dispatches named cases to see whether a routing disagreement reproduces.

    One disagreeing dispatch is a hypothesis, not a finding: every dispatch is a
    real sub-session with run-to-run variance. --retry-failed can't answer this,
    because it only drops rows that errored; a wrong-skill result is a recorded
    verdict and stays put. This writes to its own file, so the day's dispatch
    results, the HTML report, and the baseline stay as the full run left them.

    Returns 1 when any case disagreed on every run (it reproduces) or a call
    errored, 0 otherwise, and 2 when it could not run.
    """
    by_id = {c["id"]: c for c in cases}
    unknown = [i for i in ids if i not in by_id]
    if unknown:
        print(f"Unknown case id(s): {', '.join(unknown)}", file=sys.stderr)
        return 2
    if times < 1:
        print("--times must be at least 1", file=sys.stderr)
        return 2
    if os.environ.get(routing.RECURSION_GUARD):
        print(
            f"{routing.RECURSION_GUARD} is set: this process is running inside a dispatched "
            "sub-session. Refusing to re-dispatch.",
            file=sys.stderr,
        )
        return 2
    pre = preflight_mod.check()
    if not pre.ok:
        print(f"PREFLIGHT FAILED: {pre.detail}", file=sys.stderr)
        return 2

    out_path = out_path or (Path(__file__).parent / "runs" / f"rerun-{date.today():%Y-%m-%d}.jsonl")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"Re-dispatching {len(ids)} case(s), {times} time(s) each, against {model}...", file=sys.stderr)

    finding = False
    with out_path.open("a") as out:
        for cid in ids:
            case = by_id[cid]
            good = errors = 0
            for n in range(1, times + 1):
                try:
                    d = routing.dispatch_one(case["prompt"], model, Path.home())
                    invoked, error = d.invoked, d.error
                except Exception as exc:
                    invoked, error = None, str(exc)
                row = {"id": cid, "run": n, "model": model}
                row.update({"error": error} if error else {"invoked": invoked})
                out.write(json.dumps(row) + "\n")
                out.flush()

                o = report_mod.RoutingOutcome(case=case, invoked=invoked, error=error, own=own)
                if error:
                    errors += 1
                    verdict = f"ERROR: {error[:60]}"
                elif o.ok or o.foreign:
                    good += 1
                    verdict = "ok" if o.ok else "ok (outside tested set)"
                else:
                    verdict = "miss"
                print(f"  [{n}/{times}] {cid:9} -> {invoked or 'no verdict':24} {verdict}", file=sys.stderr)

            if errors:
                summary = f"{errors} errored call(s)"
                finding = True
            elif good == 0:
                summary = "reproduces"
                finding = True
            elif good == times:
                summary = "did not reproduce"
            else:
                summary = "flaky"
            print(f"  {cid}: {good}/{times} correct, expected {case['expected']} -- {summary}", file=sys.stderr)

    print(f"\nRows appended to {out_path}", file=sys.stderr)
    return 1 if finding else 0


def main() -> int:
    default_out = Path.home() / "Downloads" / f"skill-regression-{date.today():%Y-%m-%d}.html"
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--skills-dir", type=Path, action="append", default=None)
    p.add_argument("--cases", type=Path, default=Path(__file__).parent / "cases" / "routing_cases.json")
    p.add_argument("--model", default="sonnet")
    p.add_argument("--out", type=Path, default=default_out)
    p.add_argument("--predictions", type=Path, default=None, help="resumable JSONL of dispatch results")
    p.add_argument("--lint-only", action="store_true")
    p.add_argument("--retry-failed", action="store_true", help="re-dispatch cases previously recorded as errors")
    p.add_argument(
        "--rerun",
        action="append",
        default=None,
        metavar="CASE_ID",
        help="re-dispatch this case (repeatable) --times times and print each verdict, to see whether "
        "a routing disagreement reproduces. Rows go to runs/rerun-<date>.jsonl; the day's dispatch "
        "file, the HTML report, and the baseline are not touched.",
    )
    p.add_argument("--times", type=int, default=2, help="with --rerun: dispatches per case (default 2)")
    p.add_argument(
        "--if-changed",
        action="store_true",
        help="skip dispatch when nothing that invalidates prior results has changed "
        "(no new/edited skill description, same CLI and model). This is the scheduled-run entry point: "
        "drift is caused by events, not by dates.",
    )
    p.add_argument(
        "--accept-baseline",
        action="store_true",
        help="record the current descriptions, CLI, and model as the baseline. Run only after a "
        "dispatch run has been reviewed -- never to silence a drift warning.",
    )
    p.add_argument("--baseline", type=Path, default=Path(__file__).parent / "cases" / "skill_baseline.json")
    p.add_argument(
        "--runtime-paths",
        type=Path,
        default=Path(__file__).parent / "cases" / "runtime_paths.json",
        help="paths a skill mentions that something else creates at runtime; reported at INFO, not as broken",
    )
    args = p.parse_args()

    roots = args.skills_dir or [Path.home() / ".claude" / "skills"]
    skills = discover(roots)
    if not skills:
        print(f"No SKILL.md found under: {', '.join(str(r) for r in roots)}", file=sys.stderr)
        return 2

    cases, traps = load_cases(args.cases)
    if args.rerun:
        return rerun(cases, args.rerun, args.times, args.model, own=frozenset(s.dir.name for s in skills))

    version = baseline_mod.cli_version()
    base = baseline_mod.Baseline.load(args.baseline)
    drift = base.drift(skills, version, args.model)

    findings = run_lint(
        skills,
        Path.home(),
        cases=cases,
        drift_reasons=drift,
        traps=traps,
        runtime_paths=load_runtime_paths(args.runtime_paths),
    )
    lint_errors = [f for f in findings if f.severity == "error"]

    print(f"{len(skills)} skills: {', '.join(s.dir.name for s in skills)}", file=sys.stderr)
    for f in findings:
        print(f"  [{f.severity:5}] {f.check:22} {f.skill:22} {f.detail}", file=sys.stderr)

    pre = preflight_mod.check()
    outcomes = None

    if args.if_changed and not drift:
        print("\nNothing invalidating prior results has changed; dispatch skipped.", file=sys.stderr)
        print(f"  baseline: {base.cli_version or 'unset'} / {base.model or 'unset'}", file=sys.stderr)
        args.lint_only = True
    elif args.if_changed:
        print(f"\nRe-dispatching, {len(drift)} reason(s):", file=sys.stderr)
        for r in drift:
            print(f"  - {r}", file=sys.stderr)

    if os.environ.get(routing.RECURSION_GUARD) and not args.lint_only:
        print(
            f"\n{routing.RECURSION_GUARD} is set: this process is running inside a dispatched "
            "sub-session. Refusing to start a dispatch layer. Running static checks only.",
            file=sys.stderr,
        )
        args.lint_only = True

    if args.lint_only:
        print("\nLint only; dispatch layer skipped.", file=sys.stderr)
    elif not pre.ok:
        print(f"\nPREFLIGHT FAILED: {pre.detail}", file=sys.stderr)
    else:
        pred_path = args.predictions or (Path(__file__).parent / "runs" / f"dispatch-{date.today():%Y-%m-%d}.jsonl")
        pred_path.parent.mkdir(parents=True, exist_ok=True)
        if args.retry_failed and pred_path.exists():
            rows = [json.loads(x) for x in pred_path.read_text().splitlines() if x.strip()]
            keep = [r for r in rows if "error" not in r]
            pred_path.write_text("".join(json.dumps(r) + "\n" for r in keep))
            print(f"Dropped {len(rows) - len(keep)} failed rows for retry", file=sys.stderr)

        print(f"\nDispatching {len(cases)} cases against {args.model}...", file=sys.stderr)
        routing.run_cases(
            cases,
            args.model,
            cwd=Path.home(),
            out_path=pred_path,
            on_progress=lambda i, n, c, r: print(
                f"  [{i}/{n}] {c['id']:9} -> {r.get('invoked') or 'ERROR: ' + r.get('error', '')[:60]}",
                file=sys.stderr,
            ),
        )

        recorded = {}
        for line in pred_path.read_text().splitlines():
            if line.strip():
                row = json.loads(line)
                recorded[row["id"]] = row
        outcomes = [
            report_mod.RoutingOutcome(
                case=c,
                invoked=recorded.get(c["id"], {}).get("invoked"),
                error=recorded.get(c["id"], {}).get("error") or (None if c["id"] in recorded else "not run"),
                own=frozenset(s.dir.name for s in skills),
            )
            for c in cases
        ]

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        report_mod.render(
            generated=f"{date.today():%Y-%m-%d}",
            model=args.model,
            preflight=pre,
            lint_findings=findings,
            outcomes=outcomes,
            traps=traps,
            evalkit_source=evalkit_source,
        )
    )
    print(f"\nReport: {args.out}", file=sys.stderr)

    if args.accept_baseline:
        base.save(args.baseline, skills, version, args.model)
        print(f"Baseline recorded: {args.baseline}", file=sys.stderr)

    if not args.lint_only and not pre.ok:
        return 2
    if lint_errors:
        return 1
    if outcomes and any(o.error or (not o.ok and not o.foreign) for o in outcomes):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
