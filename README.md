# skill-regression-harness

Claude skills are prompt-matched, not called. Nothing imports them, nothing type-checks
them, and nothing fails loudly when one stops working. A model upgrade shifts how
aggressively descriptions get matched, or an edit to one skill's description quietly
starts stealing another's prompts — and the only symptom is that a skill you rely on
stops firing and you don't notice for a month.

This is the regression suite for that. It runs against real installed skills, in two
layers, and reports by exception.

**Layer 0 — static checks.** Deterministic, instant, free. Missing or malformed
frontmatter, a `name:` that disagrees with its own directory, two skills sharing a name,
quoted trigger phrases claimed by more than one skill, and every filesystem path a skill
body tells the agent to read.

**Layer 1 — dispatch.** Frozen prompts run through the real CLI. Does each one still
reach the skill it's supposed to reach, and do the prompts that should reach *nothing*
still reach nothing?

Scoring is [claude-eval-kit](https://github.com/neeshykha/claude-eval-kit) — confusion
matrix and confusable-pattern trap auditing. Skill dispatch is that kit's second domain
after support triage, which is the point of having extracted it.

---

## Why dispatch, not simulation

The cheap version of Layer 1 hands a model the skill roster and asks which one it would
pick. That measures whether a model can match descriptions when explicitly told to, which
is not the thing that breaks. What breaks is dispatch itself.

So the harness runs the real dispatcher. Every call is invoked with `--allowedTools Skill`,
an allowlist: the Skill tool only loads instructions into context, which is inert, and
every tool the loaded skill would then reach for is denied. `jira-ticket-builder` gets
dispatch-tested without touching Salesforce. `interview-loop` without touching Gmail.

## The auth failure this is built around

When the CLI's OAuth session expires, `claude -p` **exits 0** and returns an envelope that
reads, in part:

```json
{"is_error": true, "subtype": "success", "terminal_reason": "api_error",
 "result": "Failed to authenticate: OAuth session expired and could not be refreshed"}
```

`subtype` says `success`. A runner checking the exit code sees a pass. A runner checking
`subtype` sees a pass. Both then record confident, wrong answers for every case in the
suite — a regression harness reporting green while measuring nothing.

`preflight.py` checks `claude auth status` before any dispatch runs, and `envelope_error()`
reads `is_error` and `terminal_reason`, never `subtype` and never the exit code. Reproduced
against claude 2.1.220 on 2026-08-28; the envelope above is verbatim, and it's pinned in
`tests/test_harness.py`.

## Usage

```bash
python3 run.py --lint-only
```

Static checks only. No model calls, no cost, runs in milliseconds.

```bash
python3 run.py
```

Both layers. Writes `~/Downloads/skill-regression-<date>.html` (HTML Shelf mirrors
Downloads). Dispatch results land in `runs/dispatch-<date>.jsonl` and the run is
resumable — interrupt it and re-run, already-dispatched cases are skipped. Recorded
failures are skipped too; `--retry-failed` drops them first, and it's opt-in so a
deterministically-failing case can't retry forever.

Exit codes are the report-by-exception contract: `0` nothing to act on, `1` findings,
`2` couldn't run.

## Install

Python 3.10+, no third-party dependencies beyond eval-kit:

```bash
pip install -e /path/to/claude-eval-kit
```

If it isn't installed, `harness/_evalkit.py` falls back to a sibling checkout at
`../claude-eval-kit` and says which path it used in the report footer. The fallback exists
because this is meant to run unattended: a scheduled check that dies on an import error
reports nothing, and a check that silently stops reporting is worse than no check.

## The case set is a specification, not ground truth

`cases/routing_cases.json` holds 19 frozen prompts across six confusable groups, each with
an `expected` skill, an `acceptable` set, and a written reason it exists. Five expect *no*
skill to fire — a suite of only positive cases can't detect over-triggering, which is the
failure mode that actually shows up.

These expectations are arguments, not facts. On the first live run a disagreement is as
likely to be a wrong expectation as a wrong dispatch, and should be adjudicated by hand.
Every run after that is drift detection against a set already argued over.

**Editing a case after watching it fail is how a regression suite stops measuring
anything.** If an expectation genuinely turns out to be wrong, change it and record why in
the commit — don't quietly retune until the run goes green.

## Status

Layer 0 runs, and its findings against the live skill set are real (below). Layer 1 is
built and unit-tested against recorded stream shapes, but **has not yet been executed
against a live session** — the CLI on this machine is logged out. Until it runs, the exact
shape of the Skill tool's entry in the `stream-json` transcript is the one part of this
harness taken on inference rather than observation; `parse_stream()` accepts the plausible
spellings of the skill-name key for that reason. No numbers are reported for Layer 1
because there are none to report.

### Layer 0 findings, 2026-08-28

Five skills: `car-check`, `improvement-notes`, `interview-loop`, `jira-ticket-builder`,
`model-baseline`. Zero errors. All eight concrete paths referenced across the five bodies
resolve.

Two warnings, both real and both load-bearing for the case set:
`jira-ticket-builder` and `model-baseline` state **no non-trigger boundary** in their
descriptions. Every other skill draws one — `car-check` disclaims automotive trivia,
`improvement-notes` disclaims interview debriefs, `interview-loop` hands debriefs to the
postmortem skill. Those two don't, so they're the two most likely to over-fire, and the
case set targets them directly: `jira-04` ("How many cases did the team close last week?")
and `mb-03` ("Is Opus 5 actually better than Sonnet 5 for long-form writing?") both expect
`none`.

That's the intended relationship between the layers: the free one predicts where the
expensive one should look.

### One false positive, caught before shipping

The first lint run reported a broken reference in `model-baseline`, pointing at
`~/Downloads/model-baseline-scorecard-`. The real line is:

```
copy it to ~/Downloads/model-baseline-scorecard-<model>-<YYYY-MM-DD>.html
```

A templated **output** path. The path regex truncated at `<`, produced a fragment, and
then correctly observed that the fragment didn't exist. The fix was to the detector, not
the skill: templated paths are captured whole and reported at INFO, never as errors —
their absence is the expected state for something the skill is about to write. Pinned as a
regression test.

## Not covered, deliberately

The 25 scheduled tasks under `~/.claude/scheduled-tasks/` and `~/Documents/Claude/Scheduled/`
are out of scope. They're invoked by name on a cron, not matched by description, so
dispatch testing tells you nothing about them — and they send mail, write to Salesforce,
and touch calendars, so executing them on a schedule to see whether they still work would
cause the damage it's meant to prevent.

Output conformance is also out of scope for now. Only `car-check` runs hermetically enough
to assert on its output; the rest need live Salesforce, Gmail, or calendar. Dispatch is the
layer that generalizes across all five.
