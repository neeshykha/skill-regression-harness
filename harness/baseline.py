"""The baseline this suite asserts against: what each description said, and what
CLI and model produced the last dispatch results.

Committed rather than gitignored, deliberately. It is part of the specification,
like the frozen cases. A fresh clone with no baseline cannot tell an unchanged
description from an unseen one, and "everything looks new" is the same failure as
"everything looks fine".

Why a description hash matters more than it sounds: dispatch results are only
valid for the descriptions that produced them. Edit one word of a skill's
description and every prior verdict for that skill is stale -- but nothing errors,
the report still renders, and the number stays green. This is the signal that says
the green number is out of date.
"""

import hashlib
import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .discover import Skill


def description_hash(description: str) -> str:
    """Whitespace-normalized so reflowing a description does not read as a change."""
    normalized = " ".join(description.split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def cli_version() -> str:
    try:
        out = subprocess.run(["claude", "--version"], capture_output=True, text=True, timeout=30)
        return out.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


@dataclass
class Baseline:
    descriptions: dict[str, str] = field(default_factory=dict)
    cli_version: str = ""
    model: str = ""

    @classmethod
    def load(cls, path: Path) -> "Baseline":
        if not path.exists():
            return cls()
        d = json.loads(path.read_text())
        return cls(
            descriptions=d.get("descriptions", {}),
            cli_version=d.get("cli_version", ""),
            model=d.get("model", ""),
        )

    def save(self, path: Path, skills: list[Skill], version: str, model: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "_comment": (
                "Baseline the dispatch results were produced against. Update it only "
                "when a run has been re-validated, never to silence a drift warning."
            ),
            "cli_version": version,
            "model": model,
            "descriptions": {s.dir.name: description_hash(s.description) for s in skills},
        }
        path.write_text(json.dumps(payload, indent=2) + "\n")

    def drift(self, skills: list[Skill], version: str, model: str) -> list[str]:
        """Reasons the recorded dispatch results should no longer be trusted."""
        reasons = []
        if self.cli_version and self.cli_version != version:
            reasons.append(f"CLI changed: {self.cli_version} -> {version}")
        if self.model and self.model != model:
            reasons.append(f"model changed: {self.model} -> {model}")
        current = {s.dir.name: description_hash(s.description) for s in skills}
        for name, h in current.items():
            if name not in self.descriptions:
                reasons.append(f"{name}: new skill, never dispatch-tested")
            elif self.descriptions[name] != h:
                reasons.append(f"{name}: description changed since last run")
        for name in self.descriptions:
            if name not in current:
                reasons.append(f"{name}: skill removed")
        return reasons
