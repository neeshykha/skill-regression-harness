"""Renders a standalone HTML report. No dependencies, no network, no assets.

Written to ~/Downloads so HTML Shelf mirrors it automatically.
"""

import html
from dataclasses import dataclass

from ._evalkit import TrapGroup, audit_traps, confusion_matrix
from .routing import NO_SKILL

_CSS = """
:root{--bg:#fbfbfa;--fg:#1c1b19;--muted:#6b6862;--line:#e2ded7;--card:#fff;
--err:#b23c2b;--warn:#a8730d;--info:#5a6b7a;--ok:#2f7d4f;--errbg:#fdf1ef;--warnbg:#fdf7e8;--okbg:#f0f7f2}
@media (prefers-color-scheme:dark){:root{--bg:#191817;--fg:#eae7e1;--muted:#9d9890;--line:#332f2b;--card:#211f1d;
--err:#e8776a;--warn:#e0ac4d;--info:#9db3c4;--ok:#69c48d;--errbg:#2a1c1a;--warnbg:#2a2418;--okbg:#1a251e}}
*{box-sizing:border-box}
body{margin:0;padding:2.2rem 1.4rem 4rem;background:var(--bg);color:var(--fg);
font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
.wrap{max-width:940px;margin:0 auto}
h1{font-size:1.55rem;margin:0 0 .2rem;letter-spacing:-.01em}
h2{font-size:1.05rem;margin:2.4rem 0 .7rem;letter-spacing:-.005em}
.sub{color:var(--muted);margin:0 0 1.8rem;font-size:.9rem}
.banner{padding:.85rem 1rem;border-radius:8px;margin:0 0 1.6rem;border:1px solid var(--line);font-size:.92rem}
.banner.bad{background:var(--errbg);border-color:var(--err)}
.banner.good{background:var(--okbg);border-color:var(--ok)}
.tiles{display:flex;flex-wrap:wrap;gap:.7rem;margin:0 0 .6rem}
.tile{flex:1 1 150px;background:var(--card);border:1px solid var(--line);border-radius:8px;padding:.8rem .9rem}
.tile .n{font-size:1.6rem;font-weight:600;letter-spacing:-.02em}
.tile .l{color:var(--muted);font-size:.78rem;text-transform:uppercase;letter-spacing:.05em}
.scroll{overflow-x:auto;-webkit-overflow-scrolling:touch}
table{border-collapse:collapse;width:100%;font-size:.88rem;background:var(--card);
border:1px solid var(--line);border-radius:8px}
th,td{text-align:left;padding:.5rem .7rem;border-bottom:1px solid var(--line);vertical-align:top}
th{font-weight:600;font-size:.76rem;text-transform:uppercase;letter-spacing:.05em;color:var(--muted)}
tr:last-child td{border-bottom:none}
code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.85em;
background:var(--bg);padding:.1rem .3rem;border-radius:3px;border:1px solid var(--line)}
.pill{display:inline-block;padding:.05rem .45rem;border-radius:20px;font-size:.72rem;
font-weight:600;text-transform:uppercase;letter-spacing:.04em}
.pill.error{background:var(--errbg);color:var(--err)}
.pill.warn{background:var(--warnbg);color:var(--warn)}
.pill.info{background:var(--bg);color:var(--info)}
.pill.pass{background:var(--okbg);color:var(--ok)}
.pill.fail{background:var(--errbg);color:var(--err)}
.miss td{background:var(--errbg)}
.note{color:var(--muted);font-size:.85rem;margin:.5rem 0 0}
footer{margin-top:3rem;padding-top:1rem;border-top:1px solid var(--line);color:var(--muted);font-size:.8rem}
"""


@dataclass
class RoutingOutcome:
    case: dict
    invoked: str | None
    error: str | None
    # Names of the skills this suite actually tests. Anything else the dispatcher
    # reaches is a third outcome, not a failure of a tested skill.
    own: frozenset = frozenset()

    @property
    def strict_ok(self) -> bool:
        return self.invoked == self.case["expected"]

    @property
    def ok(self) -> bool:
        return self.invoked in self.case.get("acceptable", [self.case["expected"]])

    @property
    def foreign(self) -> bool:
        """Dispatched to a skill outside the tested set.

        `expected: "none"` was written to mean "no skill fires", but the
        dispatcher can also reach bundled and plugin skills that this suite does
        not own and Aneesh cannot edit. mb-03 ("Is Opus 5 better than Sonnet 5
        for long-form writing?") reaches the bundled `claude-api` skill, whose
        description explicitly claims LLM model-choice questions -- arguably
        correct behaviour, and not something model-baseline did wrong. Scoring
        that as a miss would blame his skills for a decision made elsewhere.
        """
        return bool(self.invoked) and self.invoked != NO_SKILL and self.invoked not in self.own and not self.ok


def _e(x) -> str:
    return html.escape(str(x))


def _tiles(pairs) -> str:
    cells = "".join(f'<div class="tile"><div class="n">{_e(n)}</div><div class="l">{_e(l)}</div></div>' for n, l in pairs)
    return f'<div class="tiles">{cells}</div>'


def _lint_table(findings) -> str:
    if not findings:
        return '<p class="note">No findings. Every skill has complete frontmatter, a name matching its directory, and no broken file references.</p>'
    rows = "".join(
        f'<tr><td><span class="pill {_e(f.severity)}">{_e(f.severity)}</span></td>'
        f"<td><code>{_e(f.check)}</code></td><td>{_e(f.skill)}</td><td>{_e(f.detail)}</td></tr>"
        for f in findings
    )
    return f'<div class="scroll"><table><tr><th></th><th>Check</th><th>Skill</th><th>Detail</th></tr>{rows}</table></div>'


def _routing_tables(outcomes: list[RoutingOutcome], traps: list[dict]) -> str:
    scored = {o.case["id"]: {"skill": o.invoked} for o in outcomes if o.error is None}
    truth = {o.case["id"]: {"skill": o.case["expected"]} for o in outcomes}

    groups = [TrapGroup(ids=t["ids"], label=t["label"], field="skill") for t in traps]
    trap_rows = "".join(
        f"<tr><td>{_e(r.group.label)}</td><td><b>{r.hits}/{r.total}</b></td>"
        f'<td><span class="pill {"pass" if r.hits == r.total else "fail"}">'
        f'{"pass" if r.hits == r.total else "fail"}</span></td></tr>'
        for r in audit_traps(scored, truth, groups)
    )
    trap_table = (
        f'<div class="scroll"><table><tr><th>Confusable group</th><th>Hits</th><th></th></tr>{trap_rows}</table></div>'
    )

    case_rows = []
    for o in sorted(outcomes, key=lambda x: (x.error is None and x.ok, x.case["id"])):
        if o.error:
            status = '<span class="pill error">error</span>'
            got = f"<code>{_e(o.error[:120])}</code>"
        elif o.ok:
            status = '<span class="pill pass">pass</span>'
            got = f"<code>{_e(o.invoked)}</code>"
        elif o.foreign:
            status = '<span class="pill info">outside set</span>'
            got = f"<code>{_e(o.invoked)}</code>"
        else:
            status = '<span class="pill fail">fail</span>'
            got = f"<code>{_e(o.invoked)}</code>"
        cls = "" if (o.error is None and (o.ok or o.foreign)) else ' class="miss"'
        case_rows.append(
            f"<tr{cls}><td>{status}</td><td><code>{_e(o.case['id'])}</code></td>"
            f"<td>{_e(o.case['prompt'])}</td>"
            f"<td><code>{_e(o.case['expected'])}</code></td><td>{got}</td>"
            f"<td>{_e(o.case.get('why', ''))}</td></tr>"
        )
    case_table = (
        '<div class="scroll"><table><tr><th></th><th>ID</th><th>Prompt</th>'
        f"<th>Expected</th><th>Dispatched</th><th>Why this case exists</th></tr>{''.join(case_rows)}</table></div>"
    )

    categories = sorted({o.case["expected"] for o in outcomes} | {v["skill"] for v in scored.values() if v["skill"]})
    matrix = confusion_matrix(scored, truth, "skill", categories)
    head = "".join(f"<th>{_e(c)}</th>" for c in categories)
    mrows = "".join(
        f"<tr><th>{_e(true)}</th>"
        + "".join(f"<td>{matrix[true].get(pred, 0) or ''}</td>" for pred in categories)
        + "</tr>"
        for true in categories
        if sum(matrix[true].values())
    )
    conf = f'<div class="scroll"><table><tr><th>expected \\ dispatched</th>{head}</tr>{mrows}</table></div>'

    return (
        f"<h2>Confusable groups</h2>{trap_table}"
        f"<h2>Every case</h2>{case_table}"
        f"<h2>Confusion matrix</h2>{conf}"
        '<p class="note">Rows are the expected skill, columns what dispatch actually chose.</p>'
    )


def render(
    *,
    generated: str,
    model: str,
    preflight,
    lint_findings,
    outcomes: list[RoutingOutcome] | None,
    traps: list[dict],
    evalkit_source: str,
) -> str:
    errors = [f for f in lint_findings if f.severity == "error"]
    warns = [f for f in lint_findings if f.severity == "warn"]

    if outcomes:
        ran = [o for o in outcomes if o.error is None]
        foreign = [o for o in ran if o.foreign]
        failed = [o for o in ran if not o.ok and not o.foreign]
        strict_miss = [o for o in ran if o.ok and not o.strict_ok]
        errored = [o for o in outcomes if o.error]
        tiles = _tiles(
            [
                (f"{len(ran) - len(failed)}/{len(ran)}", "dispatch correct"),
                (len(failed), "dispatch wrong"),
                (len(foreign), "outside tested set"),
                (len(errored), "call errored"),
                (len(errors), "lint errors"),
                (len(warns), "lint warnings"),
            ]
        )
        clean = not failed and not errored and not errors
        banner = (
            '<div class="banner good">Nothing to act on. Every frozen prompt reached the skill it should, '
            "and the static checks are clean.</div>"
            if clean
            else f'<div class="banner bad"><b>{len(failed)} dispatch failure(s), {len(errored)} errored call(s), '
            f"{len(errors)} lint error(s)"
            + (f", {len(foreign)} dispatch(es) to a skill outside the tested set" if foreign else "")
            + ".</b> Detail below.</div>"
        )
        routing = _routing_tables(outcomes, traps)
        if strict_miss:
            routing += (
                f'<p class="note">{len(strict_miss)} case(s) counted correct via the wider <code>acceptable</code> '
                "set rather than an exact match on <code>expected</code>; see the case table.</p>"
            )
    else:
        tiles = _tiles([("—", "dispatch correct"), ("—", "dispatch wrong"), (len(errors), "lint errors"), (len(warns), "lint warnings")])
        banner = (
            '<div class="banner bad"><b>Routing layer did not run.</b><br>'
            f"{_e(preflight.detail)}</div>"
        )
        routing = (
            '<h2>Confusable groups</h2><p class="note">Not run. The static layer above is complete and '
            "independent of the CLI; only the dispatch layer needs an authenticated session.</p>"
        )

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Skill Regression — {_e(generated)}</title><style>{_CSS}</style></head>
<body><div class="wrap">
<h1>Skill Regression Harness</h1>
<p class="sub">{_e(generated)} &middot; model <code>{_e(model)}</code> &middot; CLI {_e(preflight.cli_version or "unknown")}</p>
{banner}
{tiles}
<h2>Static checks</h2>
{_lint_table(lint_findings)}
{routing}
<footer>Scoring by claude-eval-kit ({_e(evalkit_source)}). Dispatch runs under
a tool denylist (<code>--disallowedTools</code>), so a skill's routing decision is
visible but nothing it reaches for executes.</footer>
</div></body></html>"""
