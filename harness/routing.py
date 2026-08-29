"""Layer 1: does a prompt still reach the skill it is supposed to reach?

This runs the real dispatcher rather than simulating it. The alternative --
handing a model the skill roster and asking which one it would pick -- measures
whether a model can match descriptions when told to, which is not the thing that
breaks. What breaks is dispatch: a model upgrade changes how aggressively
descriptions are matched, and a skill silently stops firing.

Safety: every run is invoked with `--allowedTools Skill`, an allowlist. The
Skill tool only loads instructions into context, which is inert; every tool the
loaded skill would then reach for (Bash, Read, the Salesforce and Gmail MCP
tools) is denied. So `jira-ticket-builder` can be dispatch-tested without
touching Salesforce, and `interview-loop` without touching Gmail.
"""

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .preflight import envelope_error

NO_SKILL = "none"

# Key under which the Skill tool carries the skill name. Recorded as a tuple
# because the field name is the one part of the stream shape this harness does
# not control; accepting the plausible spellings costs nothing.
_SKILL_INPUT_KEYS = ("skill", "name", "skill_name", "command")


@dataclass
class Dispatch:
    case_id: str
    invoked: str
    error: str | None = None
    # True when a terminating `result` block was seen. The CLI exits non-zero
    # when it stops at the turn cap, which is a normal outcome here -- so the
    # process exit code only means "crashed" if no result block ever arrived.
    completed: bool = False


def _tool_uses(block: dict):
    """Yields tool_use dicts from a stream-json line, top-level or nested."""
    if block.get("type") == "tool_use":
        yield block
    message = block.get("message")
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") == "tool_use":
                    yield item


def parse_stream(lines) -> Dispatch:
    """Reads a stream-json transcript and reports which skill was dispatched.

    Returns the first Skill invocation seen. First rather than any: routing is a
    decision, and the decision is the one made before the skill's own
    instructions enter the context and start influencing later calls.
    """
    error = None
    completed = False
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            block = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(block, dict):
            continue

        for tool in _tool_uses(block):
            if tool.get("name") != "Skill":
                continue
            payload = tool.get("input") or {}
            for key in _SKILL_INPUT_KEYS:
                if payload.get(key):
                    return Dispatch("", str(payload[key]), None, True)
            return Dispatch("", "unknown", None, True)

        if block.get("type") == "result":
            # Hitting the turn cap without ever invoking a Skill is a verdict,
            # not a failure. The routing decision is made on the first turn; if
            # no Skill tool_use appears anywhere in the transcript, the model
            # chose not to route to one. What it does afterwards -- reaching for
            # tools the allowlist denies until the cap stops it -- happens after
            # the decision and cannot change it.
            #
            # Treating the cap as an error cost real information on the first
            # live run: 4 of 19 cases reported no verdict, and mb-02 was hiding
            # a genuine routing miss behind one. Raising --max-turns would only
            # buy more denied tool calls at real cost; the cap is a budget, and
            # the answer is already in hand when it fires.
            completed = True
            if block.get("subtype") == "error_max_turns":
                continue
            error = envelope_error(block)

    return Dispatch("", NO_SKILL, error, completed)


def dispatch_one(prompt: str, model: str, cwd: Path, timeout: int = 120) -> Dispatch:
    result = subprocess.run(
        [
            "claude",
            "-p",
            prompt,
            "--model",
            model,
            "--output-format",
            "stream-json",
            "--verbose",
            "--allowedTools",
            "Skill",
            "--max-turns",
            "2",
        ],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    parsed = parse_stream(result.stdout.splitlines())
    if not parsed.completed and parsed.error is None:
        parsed.error = (
            f"claude CLI exited {result.returncode} with no terminating result block: "
            f"{(result.stderr or result.stdout)[:300]}"
        )
    return parsed


def run_cases(cases: list[dict], model: str, cwd: Path, out_path: Path, on_progress=None) -> None:
    """Dispatches every case not already recorded. Resumable, same contract as
    claude-eval-kit's BlindClassifier: an interrupted run is re-runnable and
    recorded failures are skipped rather than silently retried forever."""
    done: set[str] = set()
    if out_path.exists():
        for line in out_path.read_text().splitlines():
            if line.strip():
                done.add(json.loads(line)["id"])

    remaining = [c for c in cases if c["id"] not in done]
    with out_path.open("a") as out:
        for i, case in enumerate(remaining, 1):
            try:
                d = dispatch_one(case["prompt"], model, cwd)
                record = {"id": case["id"], "invoked": d.invoked}
                if d.error:
                    record = {"id": case["id"], "error": d.error}
            except Exception as exc:
                record = {"id": case["id"], "error": str(exc)}
            out.write(json.dumps(record) + "\n")
            out.flush()
            if on_progress:
                on_progress(i, len(remaining), case, record)
