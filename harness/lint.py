"""Layer 0: static checks over skill definitions. No model calls, no cost.

Everything here is deterministic and runs in milliseconds, which is the point:
the checks that need a model are the expensive half of this harness, so anything
provable from the files alone gets proven first.
"""

import re
from dataclasses import dataclass
from pathlib import Path

from .discover import Skill, is_templated

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


def run_lint(skills: list[Skill], home: Path) -> list[Finding]:
    findings: list[Finding] = []
    findings += check_frontmatter(skills)
    findings += check_name_matches_dir(skills)
    findings += check_duplicate_names(skills)
    findings += check_references(skills, home)
    findings += check_trigger_collisions(skills)
    findings += check_negative_guidance(skills)
    order = {ERROR: 0, WARN: 1, INFO: 2}
    return sorted(findings, key=lambda f: (order[f.severity], f.check, f.skill))
