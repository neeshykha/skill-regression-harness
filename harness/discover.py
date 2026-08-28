"""Finds and parses SKILL.md definitions on disk.

Deliberately does not use PyYAML. Skill frontmatter in practice is a flat
key/value block whose values are long single-line strings, and a stdlib parser
keeps this package installable with nothing but Python -- the same constraint
claude-eval-kit holds itself to.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

# Paths a SKILL.md body points at. Captures ~/... and /Users/... forms, and
# deliberately consumes <placeholder> / {placeholder} / * segments so a
# templated path is captured whole rather than truncated into a fragment that
# then looks like a missing file.
_PATH_RE = re.compile(r"(?:~|/Users/[A-Za-z0-9_.-]+)/[A-Za-z0-9_./<>{}*-]*[A-Za-z0-9_/>}*-]")

# Trailing punctuation that is prose, not part of the path.
_TRAILING = ".,;:)»\"'`"

_PLACEHOLDER_CHARS = set("<>{}*")


def is_templated(path: str) -> bool:
    """A path carrying a placeholder cannot be resolved, so its existence is not
    a fact this harness can establish either way."""
    return any(c in _PLACEHOLDER_CHARS for c in path)


@dataclass
class Skill:
    name: str
    description: str
    path: Path
    body: str
    frontmatter: dict[str, str] = field(default_factory=dict)

    @property
    def dir(self) -> Path:
        return self.path.parent

    def referenced_paths(self) -> list[str]:
        """Filesystem paths the skill body tells the agent to read.

        These are the quiet failure mode: a skill whose instructions point at a
        file that has since moved does not error, it just silently skips the
        step that made it good.
        """
        found = []
        for match in _PATH_RE.findall(self.body):
            cleaned = match.rstrip(_TRAILING)
            if cleaned and cleaned not in found:
                found.append(cleaned)
        return found


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Splits a SKILL.md into (frontmatter dict, body).

    Returns ({}, whole text) when there is no frontmatter block, which lint
    reports rather than raising -- a malformed skill is a finding, not a crash.
    """
    if not text.startswith("---"):
        return {}, text

    lines = text.splitlines()
    closing = None
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            closing = i
            break
    if closing is None:
        return {}, text

    meta: dict[str, str] = {}
    key = None
    for line in lines[1:closing]:
        if not line.strip():
            continue
        # A continuation line is indented and belongs to the previous key.
        if line[0] in " \t" and key:
            meta[key] += " " + line.strip()
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        meta[key] = value.strip()

    return meta, "\n".join(lines[closing + 1 :]).lstrip("\n")


def load_skill(skill_md: Path) -> Skill:
    text = skill_md.read_text(encoding="utf-8")
    meta, body = parse_frontmatter(text)
    return Skill(
        name=meta.get("name", ""),
        description=meta.get("description", ""),
        path=skill_md,
        body=body,
        frontmatter=meta,
    )


def discover(roots: list[Path]) -> list[Skill]:
    """Loads every SKILL.md directly beneath each root's child directories.

    Only one level deep on purpose: `~/.claude/skills/<name>/SKILL.md` is the
    install layout, and recursing would sweep in vendored marketplace copies
    that Aneesh does not own and cannot fix.
    """
    skills = []
    for root in roots:
        if not root.exists():
            continue
        for child in sorted(root.iterdir()):
            skill_md = child / "SKILL.md"
            if child.is_dir() and skill_md.is_file():
                skills.append(load_skill(skill_md))
    return skills
