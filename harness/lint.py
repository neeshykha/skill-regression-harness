"""Layer 0: static checks over skill definitions. No model calls, no cost.

Everything here is deterministic and runs in milliseconds, which is the point:
the checks that need a model are the expensive half of this harness, so anything
provable from the files alone gets proven first.
"""

import re
from dataclasses import dataclass
from pathlib import Path

from .discover import Skill, is_templated
from .routing import NO_SKILL

ERROR = "error"
WARN = "warn"
INFO = "info"

# Trigger phrases a description claims by quoting them, e.g. "grade this car".
_QUOTED = re.compile(r"[\"“]([^\"”]{3,60})[\"”]")

# Phrases too generic to be evidence of a real collision. Two skills both
# quoting "check this" is a genuine overlap; both containing "the" is not.
_STOPWORDS = {"claude", "aneesh", "the", "this", "that", "it", "he", "him"}

# Boundary language, imperative and non-imperative both. interview-loop states
# its limit as "belongs to the postmortem skill" rather than "Do NOT use"; that
# is a real boundary and reporting it as absent was wrong.
_NEGATIVE_MARKERS = (
    "do not trigger",
    "do not use",
    "never trigger",
    "do not fire",
    "not for",
    "belongs to",
    "owns those",
    "unless it clearly",
)


@dataclass
class Finding:
    check: str
    severity: str
    skill: str
    detail: str


def _norm(phrase: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", phrase.lower()).strip()


def check_frontmatter(skills: list[Skill]) -> list[Finding]:
    findings = []
    for s in skills:
        label = s.dir.name
        if not s.frontmatter:
            findings.append(Finding("frontmatter", ERROR, label, "no YAML frontmatter block found"))
            continue
        if not s.name:
            findings.append(Finding("frontmatter", ERROR, label, "frontmatter has no `name:` field"))
        if not s.description:
            findings.append(Finding("frontmatter", ERROR, label, "frontmatter has no `description:` field"))
        elif len(s.description) < 40:
            findings.append(
                Finding(
                    "frontmatter",
                    WARN,
                    label,
                    f"description is {len(s.description)} chars; too thin to route on reliably",
                )
            )
    return findings


def check_name_matches_dir(skills: list[Skill]) -> list[Finding]:
    """A skill is invoked by its frontmatter name; it is found by its directory.

    When the two disagree the skill still loads, so nothing errors -- it just
    cannot be invoked by the name its own directory advertises.
    """
    findings = []
    for s in skills:
        if s.name and s.name != s.dir.name:
            findings.append(
                Finding(
                    "name_matches_dir",
                    ERROR,
                    s.dir.name,
                    f"frontmatter name is `{s.name}` but directory is `{s.dir.name}`",
                )
            )
    return findings


def check_duplicate_names(skills: list[Skill]) -> list[Finding]:
    seen: dict[str, list[Skill]] = {}
    for s in skills:
        if s.name:
            seen.setdefault(s.name, []).append(s)
    findings = []
    for name, group in seen.items():
        if len(group) > 1:
            where = ", ".join(str(g.path) for g in group)
            findings.append(
                Finding("duplicate_name", ERROR, name, f"{len(group)} skills share the name `{name}`: {where}")
            )
    return findings


def check_references(skills: list[Skill], home: Path) -> list[Finding]:
    """Every concrete filesystem path a skill body points at must exist.

    Templated paths (`~/Downloads/scorecard-<model>.html`) are reported at INFO
    and never as errors. They are usually outputs the skill is about to write,
    so their absence is the expected state, and the placeholder means there is
    no single path to test in any case. Treating them as errors produced this
    harness's first false positive against model-baseline.
    """
    findings = []
    for s in skills:
        for ref in s.referenced_paths():
            if is_templated(ref):
                findings.append(
                    Finding("templated_reference", INFO, s.dir.name, f"path is templated, existence not checked: {ref}")
                )
                continue
            resolved = Path(ref.replace("~", str(home), 1)) if ref.startswith("~") else Path(ref)
            if not resolved.exists():
                findings.append(
                    Finding("broken_reference", ERROR, s.dir.name, f"references a path that does not exist: {ref}")
                )
    return findings


def check_negative_guidance(skills: list[Skill]) -> list[Finding]:
    """A description with no stated boundary is the one that over-triggers.

    Reported as a warning, not an error: plenty of skills are narrow enough that
    no exclusion is needed. It earns attention when the routing layer shows that
    skill stealing prompts from another.
    """
    findings = []
    for s in skills:
        lowered = s.description.lower()
        if s.description and not any(m in lowered for m in _NEGATIVE_MARKERS):
            findings.append(
                Finding(
                    "negative_guidance",
                    WARN,
                    s.dir.name,
                    "description states no explicit non-trigger boundary (no 'Do NOT trigger...' clause)",
                )
            )
    return findings


def check_trigger_collisions(skills: list[Skill]) -> list[Finding]:
    """Two skills quoting the same trigger phrase are competing for that prompt.

    Only quoted phrases count. A description claims a phrase by putting it in
    quotes, and that is concrete enough to act on -- fuzzy semantic overlap is
    what the routing layer measures, not this.
    """
    claims: dict[str, set[str]] = {}
    for s in skills:
        for raw in _QUOTED.findall(s.description):
            phrase = _norm(raw)
            if not phrase or phrase in _STOPWORDS or len(phrase.split()) < 2:
                continue
            claims.setdefault(phrase, set()).add(s.dir.name)

    findings = []
    for phrase, owners in sorted(claims.items()):
        if len(owners) > 1:
            findings.append(
                Finding(
                    "trigger_collision",
                    WARN,
                    " + ".join(sorted(owners)),
                    f'both claim the trigger phrase "{phrase}"',
                )
            )
    return findings


def check_dispatch_coverage(skills: list[Skill], cases: list[dict]) -> list[Finding]:
    """A skill with no dispatch cases is untested and reports as untested nowhere.

    This is the harness's worst silent failure: add a sixth skill and Layer 1
    still reports 18/19, because the suite only knows about skills it has cases
    for. "18/19" then means "18 of the 19 things I happen to test", which reads
    identically to "everything is fine".
    """
    covered = {c["expected"] for c in cases} | {a for c in cases for a in c.get("acceptable", [])}
    findings = []
    for s in skills:
        if s.dir.name not in covered:
            findings.append(
                Finding(
                    "dispatch_coverage",
                    ERROR,
                    s.dir.name,
                    "no dispatch case expects this skill; Layer 1 does not test it at all",
                )
            )
    return findings


def check_untested_triggers(skills: list[Skill], cases: list[dict], max_listed: int = 6) -> list[Finding]:
    """Trigger phrases a description quotes but no case prompt exercises.

    A quoted phrase is a promise the description makes. Testing two of six means
    the other four are unverified, and the report's pass rate says nothing about
    them -- which is how model-baseline's claimed "test Opus 5.2 when it drops"
    family went unexamined until one variant of it was written down as a case.
    """
    prompts = " ".join(_norm(c["prompt"]) for c in cases)
    findings = []
    for s in skills:
        untested = []
        for raw in _QUOTED.findall(s.description):
            phrase = _norm(raw)
            if not phrase or len(phrase.split()) < 2 or phrase in _STOPWORDS:
                continue
            if phrase not in prompts and phrase not in untested:
                untested.append(phrase)
        if untested:
            shown = ", ".join(f'"{p}"' for p in untested[:max_listed])
            more = f" (+{len(untested) - max_listed} more)" if len(untested) > max_listed else ""
            findings.append(
                Finding(
                    "untested_trigger",
                    INFO,
                    s.dir.name,
                    f"{len(untested)} quoted trigger phrase(s) exercised by no case: {shown}{more}",
                )
            )
    return findings


# The README spells some counts as digits and some as words in the same
# sentence ("23 frozen prompts across seven confusable groups"), so both parse.
_NUMBER_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20,
}

# Matched against whitespace-normalized README text: these sentences wrap across
# lines in the source, so `\s+` is doing real work here rather than being defensive.
_DOC_CLAIMS = (
    ("frozen prompts", re.compile(r"holds\s+(\w+)\s+frozen prompts", re.I)),
    ("confusable groups", re.compile(r"across\s+(\w+)\s+confusable groups", re.I)),
    ("cases expecting no skill", re.compile(r"(\w+)\s+expect\s+\*?no\*?\s+skill to fire", re.I)),
)


def _as_int(token: str) -> int | None:
    token = token.strip().lower()
    return int(token) if token.isdigit() else _NUMBER_WORDS.get(token)


def check_docs_counts(cases: list[dict], traps: list[dict], readme: Path) -> list[Finding]:
    """The README describes the case set in prose, and prose drifts from the file.

    This is the check that had to be added by hand after the drift it detects
    shipped. The README's spec paragraph claimed 19 prompts across six groups
    with five expecting no skill, four lines above its own results section
    reporting 21/23. The case set had grown to 23 across seven groups when
    skill-check was added, and the prose did not follow. The "five" was worse:
    it was wrong before the growth too, since the 19-case set already had seven
    negative cases. Nothing failed, nothing errored, and the numbers were copied
    out of the README into other documents before anyone counted the file.

    Same failure mode this harness exists for, one level up. A skill description
    that no longer matches the skill is caught by Layer 1; a README that no
    longer matches the case set was caught by nothing.

    ERROR rather than WARN because a mismatch is provable from two files with no
    model in the loop, exactly like a broken path reference. A README this can't
    parse at all degrades to INFO instead, so rewording the prose reports a
    silence rather than manufacturing a failure.
    """
    if not readme.exists():
        return [Finding("docs_counts", INFO, readme.name, f"no README found at {readme}; counts not checked")]

    trap_ids = [i for t in traps for i in t.get("ids", [])]
    case_ids = {c["id"] for c in cases}
    orphans = sorted(case_ids - set(trap_ids))
    dupes = len(trap_ids) - len(set(trap_ids))
    if orphans or dupes:
        # Guard, not a side quest: "seven confusable groups" only means anything
        # while `traps` partitions `cases`. Comparing a group count against the
        # README while the grouping is broken would assert a meaningless number.
        detail = (
            f"`traps` does not partition `cases`, so the group count is not meaningful: "
            f"{len(orphans)} case(s) in no trap group"
            + (f" ({', '.join(orphans[:5])})" if orphans else "")
            + f", {dupes} duplicate id(s) across groups"
        )
        return [Finding("docs_counts", ERROR, "routing_cases.json", detail)]

    actual = {
        "frozen prompts": len(cases),
        "confusable groups": len(traps),
        "cases expecting no skill": sum(1 for c in cases if c.get("expected") == NO_SKILL),
    }

    findings = []
    text = " ".join(readme.read_text().split())
    for label, pattern in _DOC_CLAIMS:
        m = pattern.search(text)
        if not m:
            findings.append(
                Finding("docs_counts", INFO, readme.name, f"README states no {label} count to check")
            )
            continue
        claimed = _as_int(m.group(1))
        if claimed is None:
            findings.append(
                Finding("docs_counts", INFO, readme.name, f'{label}: cannot read "{m.group(1)}" as a number')
            )
        elif claimed != actual[label]:
            findings.append(
                Finding(
                    "docs_counts",
                    ERROR,
                    readme.name,
                    f"README says {claimed} {label}; routing_cases.json has {actual[label]}",
                )
            )
    return findings


def check_baseline_drift(reasons: list[str]) -> list[Finding]:
    """Recorded dispatch results are only valid for the baseline that produced
    them. A changed description or a new CLI does not error anything -- the
    report still renders and the pass rate stays green while meaning less."""
    return [
        Finding("baseline_drift", WARN, reason.split(":")[0].strip(), f"dispatch results may be stale — {reason}")
        for reason in reasons
    ]


def run_lint(
    skills: list[Skill],
    home: Path,
    cases: list[dict] | None = None,
    drift_reasons: list[str] | None = None,
    traps: list[dict] | None = None,
    readme: Path | None = None,
) -> list[Finding]:
    findings: list[Finding] = []
    findings += check_frontmatter(skills)
    findings += check_name_matches_dir(skills)
    findings += check_duplicate_names(skills)
    findings += check_references(skills, home)
    if cases is not None:
        findings += check_dispatch_coverage(skills, cases)
    if cases is not None and traps is not None:
        findings += check_docs_counts(cases, traps, readme or Path(__file__).resolve().parent.parent / "README.md")
    findings += check_baseline_drift(drift_reasons or [])
    findings += check_trigger_collisions(skills)
    findings += check_negative_guidance(skills)
    if cases is not None:
        findings += check_untested_triggers(skills, cases)
    order = {ERROR: 0, WARN: 1, INFO: 2}
    return sorted(findings, key=lambda f: (order[f.severity], f.check, f.skill))
