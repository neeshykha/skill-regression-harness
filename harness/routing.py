"""Layer 1: does a prompt still reach the skill it is supposed to reach?

This runs the real dispatcher rather than simulating it. The alternative --
handing a model the skill roster and asking which one it would pick -- measures
whether a model can match descriptions when told to, which is not the thing that
breaks. What breaks is dispatch: a model upgrade changes how aggressively
descriptions are matched, and a skill silently stops firing.

Safety: every run is invoked with a denylist of the effectful tools. A skill
still dispatches and is fully visible in the transcript, but nothing it then
reaches for executes. Verified with the same probe that caught the original
flaw -- a bash command touching a file INSIDE the home tree, where the
working-directory guard does not apply -- and no file is created.

`--permission-mode plan` was tried here and had to be reverted: it is safe, but
it CHANGES WHAT IS MEASURED. Plan mode declines to dispatch skills whose work
has side effects, so `model-baseline` went 0/3 under it versus 2/3 under the
denylist alone, and `interview-loop`'s "prep me for X" stopped firing entirely.
Those are exactly the skills where routing matters most operationally. A safety
mechanism that suppresses the behaviour under test is not a safe harness, it is
a broken one.

`--allowedTools Skill` was used for this at first and DOES NOT WORK. It does not
restrict anything: a probe on 2026-08-29 confirmed Bash still ran under it, and
the only reason a write failed was an unrelated working-directory guard -- which
does not apply inside the home tree, where these runs are cwd'd. The concrete
damage: dispatch-testing a skill whose own instructions say "run the harness"
recursed, and appended 213 junk rows to a predictions file.
"""

import os

# Set in the environment of every dispatched sub-session. run.py refuses to
# start a dispatch layer when it sees this, so a skill that tells the agent to
# run this harness cannot recurse even if the tool gating is wrong again.
RECURSION_GUARD = "SKILL_HARNESS_DISPATCHING"

# Belt-and-braces alongside plan mode. Plan mode is what actually stops
# execution; this narrows what the model will even reach for.
_DENIED_TOOLS = [
    "Bash",
    "Write",
    "Edit",
    "NotebookEdit",
    "WebFetch",
    "WebSearch",
    "Task",
    "Agent",
    "ToolSearch",
]

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
            "--disallowedTools",
            *_DENIED_TOOLS,
            "--max-turns",
            "2",
        ],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        env={**os.environ, RECURSION_GUARD: "1"},
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
