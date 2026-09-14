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
body tells the agent to read. It also checks the suite's coverage of itself: a skill with
no dispatch cases, quoted trigger phrases no case exercises, and whether any description
has changed since the results were recorded.

That last group matters more than it sounds. Layer 1 only knows about skills it has cases
for, so adding a seventh skill tomorrow would leave the report still scoring **23 cases** —
which means "the 23 things I happen to test" and reads exactly like "everything is fine".
The coverage checks turn that silence into an error.

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

So the harness runs the real dispatcher under a **denylist** of the effectful tools. A
skill still dispatches and the decision is fully visible in the transcript, but
nothing it then reaches for executes. `jira-ticket-builder` gets dispatch-tested without
touching Salesforce; `interview-loop` without touching Gmail.

**`--allowedTools Skill` was the first attempt at this and it does not work.** See
"The safety mechanism that wasn't" below — it is the most useful thing in this repo.

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

### Run it when drift actually happens, not on a calendar

Skill dispatch doesn't drift because time passed. It drifts on two events: a model or CLI
upgrade, or an edited skill description. A monthly cron mostly spends 19 calls confirming
nothing changed.

```bash
python3 run.py --if-changed
```

Skips dispatch entirely and exits 0 when nothing that invalidates prior results has
changed. When something has, it says what and re-dispatches. `cases/skill_baseline.json`
holds the description hashes plus the CLI version and model the last results were produced
against; `--accept-baseline` records a new one **after** a run has been reviewed, never to
silence a warning. Description hashes are whitespace-normalized, so rewrapping a paragraph
doesn't trigger a paid re-run.

That baseline is committed, not gitignored. It's part of the specification, like the frozen
cases — a fresh clone with no baseline can't tell an unchanged description from an unseen
one, and "everything looks new" fails the same way "everything looks fine" does.

### Layer 0 at edit time

`hooks/lint_on_skill_edit.sh` is a `PostToolUse` hook on `Write|Edit`. It runs the static
layer only when a `SKILL.md` is touched, prints nothing unless there are errors, and always
exits 0 — a linter that can block an edit is a linter that gets removed. Registered in
`~/.claude/settings.json`:

```json
{ "hooks": { "PostToolUse": [ { "matcher": "Write|Edit", "hooks": [
  { "type": "command", "command": "/path/to/hooks/lint_on_skill_edit.sh", "timeout": 30 }
] } ] } }
```

Layer 1 is deliberately not in the hook. It costs real calls.

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

`cases/routing_cases.json` holds 23 frozen prompts across seven confusable groups, each with
an `expected` skill, an `acceptable` set, and a written reason it exists. Eight expect *no*
skill to fire — a suite of only positive cases can't detect over-triggering, which is the
failure mode that actually shows up.

These expectations are arguments, not facts. On the first live run a disagreement is as
likely to be a wrong expectation as a wrong dispatch, and should be adjudicated by hand.
Every run after that is drift detection against a set already argued over.

**Editing a case after watching it fail is how a regression suite stops measuring
anything.** If an expectation genuinely turns out to be wrong, change it and record why in
the commit — don't quietly retune until the run goes green.

## Results, 2026-08-29 (sonnet, claude 2.1.220, denylist config)

**21/23 correct, 0 errored, on 6 skills.** One miss and one dispatch outside the tested set.

**`mb-02` — the one real finding, and it has now survived everything.** The prompt is
`Test the new model when it drops.` and `model-baseline` does not dispatch. Reproduced
across four runs and two different safety configurations. Its description explicitly claims
that trigger family — `test a new model release`, `"test Opus 5.2 when it drops"`, and `even
if he doesn't say the word "baseline"` — but the prompt names no model, and the skill needs
one to run. Both readings hold: the description over-promises, or declining an
under-specified request is correct. **The case is deliberately unedited** pending
adjudication.

**`mb-03` — a defect in this suite, not in a skill.** `Is Opus 5 actually better than
Sonnet 5 for long-form writing?` dispatches the **bundled `claude-api` skill**, whose
description explicitly claims LLM model-choice questions. `expected: "none"` was written to
mean "no skill fires", but the dispatcher also reaches bundled and plugin skills this suite
neither owns nor can edit. Scoring that as a miss would blame `model-baseline` for a decision
made elsewhere, so there is now a third verdict — **outside tested set** — reported
separately from a miss and excluded from the failure exit code.

### The safety mechanism that changed the answer

`--permission-mode plan` was adopted as the fix for the allowlist problem below, and had to
be reverted within the hour. It is genuinely safe. It also **changes what is measured**:

| case | prompt | plan mode | denylist only |
|---|---|---|---|
| `mb-01` | "run the baseline against Opus 5.2" | 0/3 | 2/3 |
| `int-02` | "prep me for Baseten" | none | interview-loop |
| `sc-03` | "Run the baseline ... and tell me how it scores." | none | model-baseline |

Plan mode declines to dispatch skills whose work has side effects — which is exactly what
plan mode is for, and exactly the wrong property here. The skills it suppresses
(`model-baseline` runs a suite and writes files; `interview-loop`'s "prep me for X" reads
calendar and Gmail) are the ones where routing matters most operationally. Skills that only
produce text in-conversation kept dispatching normally, which is why the first three runs
looked plausible.

**A safety control that suppresses the behaviour under test is not a safe harness, it is a
broken one.** The final config is the denylist alone, verified with the same in-home-tree
bash probe that caught the original flaw. Pinned in `tests/test_harness.py`.

Worth recording: `mb-01` is 2/3 even under the correct config, so it is genuinely flaky
independent of any of this. A single passing run of that case proves less than it looks like.

### Layer 0 findings, 2026-08-28

Five skills: `car-check`, `improvement-notes`, `interview-loop`, `jira-ticket-builder`,
`model-baseline`. Zero errors. All eight concrete paths referenced across the five bodies
resolve.

Dispatch coverage is complete — every installed skill is named by at least one case.

The `untested_trigger` check earned its place immediately: **`model-baseline` quotes three
trigger phrases and the suite exercises none of them verbatim**, including
`"test Opus 5.2 when it drops"` — the exact family the one failing case belongs to. The
suite tests a variant of that phrase with the model name removed, which is precisely the
distinction the failure turns on. `car-check` quotes eleven untested phrases, the largest
uncovered surface in the set.

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

### The safety mechanism that wasn't

`--allowedTools Skill` reads like an allowlist. It is not restrictive in the way this harness
depended on. A probe on 2026-08-29:

```
prompt:  "Run this bash command now: touch /tmp/allowlist-probe.txt"
flags:   --allowedTools Skill
result:  Bash tool_use ATTEMPTED and executed; the write failed with
         "blocked ... may only create or modify files in the allowed working directories"
```

Bash ran. The write failed for an unrelated reason — a working-directory guard — and that
guard **does not apply inside the home tree**, which is exactly where these runs are cwd'd.

**How it surfaced, which is the part worth keeping.** A new `skill-check` skill was added
whose own instructions say "run the harness." Dispatch-testing it recursed: sub-sessions ran
`run.py`, which dispatched again. The predictions file went from 19 rows to **232**, with
`sc-01`×22, `sc-02`×51, `sc-03`×66, `sc-04`×74 in contiguous blocks. Contiguous-and-growing
is the signature of recursion rather than concurrency, and that shape is what prompted the
probe. Nothing was damaged outside the repo, and no runaway process survived the run.

Earlier in the same session a `jira-ticket-builder` dispatch had attempted
`python3 ~/.claude/skills/jira-ticket-builder/scripts/fetch_case.py 268386`, and the
transcript's following `user` blocks were **assumed** to be denials. They were not verified.
That call most likely executed. It is a read-only Salesforce query, so nothing was written,
but the safety claim made on the strength of it was wrong.

**The fix, verified rather than assumed:**

```
prompt:  "build me a jira ticket for 268386"     flags: --permission-mode plan
         -> Skill{"skill": "jira-ticket-builder"}   (dispatch still visible)

prompt:  "Run this bash command now: touch ..."  flags: --permission-mode plan
         -> no tools attempted, no file created
```

Plus `--disallowedTools` over Bash/Write/Edit/WebFetch/WebSearch/Task/Agent/ToolSearch (the
denylist matters because with Bash denied alone, the model reached for `ToolSearch` to find
another route), and an environment-variable recursion guard: `run.py` refuses to start a
dispatch layer when `SKILL_HARNESS_DISPATCHING` is set, so a nested run cannot recurse even
if the tool gating is wrong again.

**The generalizable lesson:** a safety control that has never been probed is an assumption,
not a control. This one read plausibly, appeared to work, and was wrong — and what exposed it
was junk data in the harness's own output, not a security review.

### Two more defects in the harness itself, both found by running it

**1. The turn cap was being reported as a failure, and it hid a real finding.** The first
live run produced 4 non-verdicts out of 19. Cause: prompts that want a tool the allowlist
denies spend both turns retrying and terminate with `subtype: error_max_turns`. That is
not a failure — the routing decision happens on the first turn, and if no `Skill` tool_use
appears anywhere in the transcript, the model chose not to route. The cap firing afterwards
cannot change a decision already observed.

The cost of getting this wrong was not just missing data. **`mb-02` — the only genuine
routing miss in the whole suite — was hiding behind one of those four errors.** Fixing the
semantics turned 4 non-verdicts into 4 verdicts, three of which were correct negatives and
one of which was the finding. Raising `--max-turns` would have been the wrong fix: it buys
more denied tool calls at real cost, and the answer is already in hand when the cap fires.

**2. The first fix broke the crash detector.** With the cap no longer setting an error, the
fallback that reports a non-zero CLI exit took over — and the CLI exits 1 when it stops at
the cap, so the same two cases errored again with a different message. The exit code alone
cannot distinguish a crash from a normal capped run. `Dispatch.completed` now records
whether a terminating `result` block was ever seen, and the exit code is only consulted
when one wasn't.

**Reporting note:** those first four failures were logged as `unknown error` and then did
not reproduce, so the evidence was gone. `envelope_error()` now always carries `subtype`,
`terminal_reason`, `stop_reason`, and `num_turns` even when there is no result text. An
error string that can't tell a rate limit from a hung turn isn't a diagnosis.

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

## A reproducibility caveat worth knowing

Each dispatch is a real `claude -p` sub-session, and it inherits the full user environment:
`~/.claude/CLAUDE.md`, memory, and every configured MCP server. That is correct for a
dispatch test — the thing under test *is* the real routing environment — but it means the
prompt is not the only input. During debugging, one sub-session's reply referenced content
that was never in its prompt, which means ambient context can reach these runs.

Practical consequence: treat a single disagreeing case as a hypothesis, not a result.
`mb-02` is reported above as a finding only because it reproduced three times out of three.

## Not covered, deliberately

The 25 scheduled tasks under `~/.claude/scheduled-tasks/` and `~/Documents/Claude/Scheduled/`
are out of scope. They're invoked by name on a cron, not matched by description, so
dispatch testing tells you nothing about them — and they send mail, write to Salesforce,
and touch calendars, so executing them on a schedule to see whether they still work would
cause the damage it's meant to prevent.

Output conformance is also out of scope for now. Only `car-check` runs hermetically enough
to assert on its output. `jira-ticket-builder` and `interview-loop` need live Salesforce,
Gmail, or calendar. `model-baseline` launches clean-room model runs of its own, and
`skill-check` runs this harness, which is how the recursion described above started.
`improvement-notes` audits whatever session invoked it, so there's no fixed output to
compare against. Dispatch is the layer that generalizes across all of them.
