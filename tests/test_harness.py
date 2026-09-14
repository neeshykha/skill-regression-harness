"""Stdlib unittest only, matching claude-eval-kit's zero-dependency install.

The dispatch parser is tested against recorded stream shapes rather than a live
CLI: the parsing half is what carries the logic, and it is the half that can be
pinned down without spending money or needing a session.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness.baseline import Baseline, description_hash  # noqa: E402
from harness.discover import Skill, discover, is_templated, parse_frontmatter  # noqa: E402
from harness.lint import (  # noqa: E402
    check_dispatch_coverage,
    check_docs_counts,
    check_duplicate_names,
    check_name_matches_dir,
    check_references,
    check_trigger_collisions,
    check_untested_triggers,
)
from harness.preflight import envelope_error  # noqa: E402
from harness import routing as routing_mod  # noqa: E402
from harness.routing import NO_SKILL, parse_stream  # noqa: E402


def make_skill(name, description="", body="", dirname=None):
    d = Path(tempfile.gettempdir()) / (dirname or name)
    return Skill(name=name, description=description, path=d / "SKILL.md", body=body, frontmatter={"name": name})


class TestFrontmatter(unittest.TestCase):
    def test_parses_name_and_description(self):
        meta, body = parse_frontmatter("---\nname: car-check\ndescription: Grade a car.\n---\n\n# Car Check\ntext\n")
        self.assertEqual(meta["name"], "car-check")
        self.assertEqual(meta["description"], "Grade a car.")
        self.assertTrue(body.startswith("# Car Check"))

    def test_no_frontmatter_returns_whole_text(self):
        meta, body = parse_frontmatter("# Just a doc\n")
        self.assertEqual(meta, {})
        self.assertEqual(body, "# Just a doc\n")

    def test_unterminated_frontmatter_is_not_a_crash(self):
        meta, _ = parse_frontmatter("---\nname: broken\nno closing fence\n")
        self.assertEqual(meta, {})

    def test_colon_in_value_survives(self):
        meta, _ = parse_frontmatter("---\ndescription: Use when he says: run it\n---\nbody\n")
        self.assertEqual(meta["description"], "Use when he says: run it")


class TestReferences(unittest.TestCase):
    def test_finds_tilde_and_absolute_paths(self):
        s = make_skill("x", body="Read ~/Documents/a.md and /Users/aneesh/b.md first.")
        self.assertEqual(s.referenced_paths(), ["~/Documents/a.md", "/Users/aneesh/b.md"])

    def test_strips_trailing_prose_punctuation(self):
        s = make_skill("x", body="See ~/Documents/notes.md, then stop.")
        self.assertIn("~/Documents/notes.md", s.referenced_paths())

    def test_templated_path_is_captured_whole(self):
        """Regression: the regex used to truncate at '<', producing a fragment
        that was then reported as a missing file. That was this harness's first
        false positive, against model-baseline."""
        s = make_skill("x", body="copy it to ~/Downloads/scorecard-<model>-<date>.html")
        refs = s.referenced_paths()
        self.assertEqual(refs, ["~/Downloads/scorecard-<model>-<date>.html"])
        self.assertTrue(is_templated(refs[0]))

    def test_templated_paths_never_reported_as_broken(self):
        s = make_skill("x", body="write ~/Downloads/out-<model>.html", dirname="x")
        findings = check_references([s], Path.home())
        self.assertEqual([f.check for f in findings], ["templated_reference"])

    def test_missing_concrete_path_is_an_error(self):
        s = make_skill("x", body="Read ~/definitely/not/here-9f3a.md")
        findings = check_references([s], Path.home())
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].severity, "error")


class TestLintChecks(unittest.TestCase):
    def test_name_directory_mismatch(self):
        s = make_skill("car-check", dirname="carcheck")
        self.assertEqual(len(check_name_matches_dir([s])), 1)

    def test_matching_name_is_silent(self):
        self.assertEqual(check_name_matches_dir([make_skill("car-check", dirname="car-check")]), [])

    def test_duplicate_names_flagged(self):
        a = make_skill("improvement-notes", dirname="a")
        b = make_skill("improvement-notes", dirname="b")
        self.assertEqual(len(check_duplicate_names([a, b])), 1)

    def test_shared_quoted_trigger_phrase(self):
        a = make_skill("a", description='Trigger on "grade this car" always.', dirname="a")
        b = make_skill("b", description='Also fires for "grade this car" sometimes.', dirname="b")
        findings = check_trigger_collisions([a, b])
        self.assertEqual(len(findings), 1)
        self.assertIn("grade this car", findings[0].detail)

    def test_single_word_quotes_are_not_collisions(self):
        a = make_skill("a", description='Say "run".', dirname="a")
        b = make_skill("b", description='Say "run".', dirname="b")
        self.assertEqual(check_trigger_collisions([a, b]), [])


class TestEnvelopeError(unittest.TestCase):
    def test_expired_session_is_detected_despite_success_subtype(self):
        """The exact envelope reproduced from claude 2.1.220 on 2026-08-28.
        `subtype` reads "success" while the call failed."""
        env = {
            "is_error": True,
            "subtype": "success",
            "terminal_reason": "api_error",
            "result": "Failed to authenticate: OAuth session expired and could not be refreshed",
        }
        self.assertIn("OAuth session expired", envelope_error(env))

    def test_clean_envelope_returns_none(self):
        self.assertIsNone(envelope_error({"is_error": False, "subtype": "success", "result": "pong"}))

    def test_api_error_without_is_error_still_caught(self):
        msg = envelope_error({"terminal_reason": "api_error", "result": "boom"})
        self.assertIn("boom", msg)
        self.assertIn("terminal_reason=api_error", msg)

    def test_error_with_no_result_text_still_carries_diagnosis(self):
        """The first live run produced four failures reported only as
        "unknown error". They did not reproduce, so the detail was lost. An
        error string has to say enough to tell a rate limit from a hung turn."""
        msg = envelope_error({"is_error": True, "subtype": "error_max_turns", "num_turns": 2})
        self.assertIn("no result text", msg)
        self.assertIn("subtype=error_max_turns", msg)
        self.assertIn("num_turns=2", msg)


class TestParseStream(unittest.TestCase):
    def _lines(self, *objs):
        return [json.dumps(o) for o in objs]

    def test_detects_nested_skill_tool_use(self):
        lines = self._lines(
            {"type": "system", "subtype": "init"},
            {
                "type": "assistant",
                "message": {"content": [{"type": "tool_use", "name": "Skill", "input": {"skill": "car-check"}}]},
            },
        )
        self.assertEqual(parse_stream(lines).invoked, "car-check")

    def test_detects_top_level_tool_use(self):
        lines = self._lines({"type": "tool_use", "name": "Skill", "input": {"skill": "model-baseline"}})
        self.assertEqual(parse_stream(lines).invoked, "model-baseline")

    def test_ignores_non_skill_tools(self):
        lines = self._lines(
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "WebSearch", "input": {}}]}},
            {"type": "result", "is_error": False, "result": "done"},
        )
        self.assertEqual(parse_stream(lines).invoked, NO_SKILL)

    def test_first_skill_wins(self):
        lines = self._lines(
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Skill", "input": {"skill": "a"}}]}},
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Skill", "input": {"skill": "b"}}]}},
        )
        self.assertEqual(parse_stream(lines).invoked, "a")

    def test_surfaces_auth_error_from_result_block(self):
        lines = self._lines(
            {"type": "result", "is_error": True, "subtype": "success", "result": "Failed to authenticate: expired"}
        )
        d = parse_stream(lines)
        self.assertEqual(d.invoked, NO_SKILL)
        self.assertIn("Failed to authenticate", d.error)

    def test_max_turns_without_a_skill_is_a_none_verdict_not_an_error(self):
        """Observed on the first live run: cases whose prompts want a denied
        tool burn both turns retrying and terminate with error_max_turns. The
        routing decision was already made and observed -- no Skill was
        invoked -- so the verdict is `none`."""
        lines = self._lines(
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {}}]}},
            {"type": "result", "is_error": True, "subtype": "error_max_turns", "num_turns": 2},
        )
        d = parse_stream(lines)
        self.assertEqual(d.invoked, NO_SKILL)
        self.assertIsNone(d.error)

    def test_max_turns_after_a_skill_still_reports_that_skill(self):
        lines = self._lines(
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Skill", "input": {"skill": "car-check"}}]}},
            {"type": "result", "is_error": True, "subtype": "error_max_turns"},
        )
        self.assertEqual(parse_stream(lines).invoked, "car-check")

    def test_real_errors_are_still_errors(self):
        lines = self._lines({"type": "result", "is_error": True, "subtype": "success", "result": "overloaded_error"})
        self.assertIn("overloaded_error", parse_stream(lines).error)

    def test_malformed_lines_are_skipped(self):
        lines = ["not json", "", '{"type":"tool_use","name":"Skill","input":{"skill":"x"}}']
        self.assertEqual(parse_stream(lines).invoked, "x")

    def test_alternate_input_key(self):
        lines = self._lines({"type": "tool_use", "name": "Skill", "input": {"skill_name": "interview-loop"}})
        self.assertEqual(parse_stream(lines).invoked, "interview-loop")


class TestCaseSet(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).resolve().parent.parent / "cases" / "routing_cases.json"
        self.data = json.loads(path.read_text())

    def test_ids_unique(self):
        ids = [c["id"] for c in self.data["cases"]]
        self.assertEqual(len(ids), len(set(ids)))

    def test_expected_is_in_acceptable(self):
        for c in self.data["cases"]:
            self.assertIn(c["expected"], c["acceptable"], c["id"])

    def test_every_case_documents_why_it_exists(self):
        for c in self.data["cases"]:
            self.assertTrue(c.get("why"), c["id"])

    def test_trap_ids_all_resolve(self):
        known = {c["id"] for c in self.data["cases"]}
        for trap in self.data["traps"]:
            for cid in trap["ids"]:
                self.assertIn(cid, known, f"{trap['label']} references unknown case {cid}")

    def test_every_case_belongs_to_a_trap_group(self):
        grouped = {cid for t in self.data["traps"] for cid in t["ids"]}
        self.assertEqual({c["id"] for c in self.data["cases"]} - grouped, set())

    def test_has_negative_cases(self):
        """A suite of only positive cases cannot detect over-triggering, which is
        the failure mode a skill with no stated boundary actually has."""
        self.assertGreaterEqual(sum(1 for c in self.data["cases"] if c["expected"] == "none"), 5)


class TestDispatchSafety(unittest.TestCase):
    """Pins the safety posture. The first mechanism (`--allowedTools Skill`) did not
    restrict anything -- Bash executed under it, and dispatch-testing a skill whose
    instructions say "run the harness" recursed 19 predictions rows into 232. These
    assertions exist so that cannot silently revert."""

    def _captured_call(self):
        captured = {}

        class FakeResult:
            returncode = 0
            stdout = json.dumps(
                {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Skill", "input": {"skill": "x"}}]}}
            )
            stderr = ""

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["env"] = kwargs.get("env") or {}
            return FakeResult()

        real = routing_mod.subprocess.run
        routing_mod.subprocess.run = fake_run
        try:
            routing_mod.dispatch_one("prompt", "sonnet", Path.home())
        finally:
            routing_mod.subprocess.run = real
        return captured

    def test_does_not_use_plan_mode(self):
        """Plan mode is safe but changes what is measured: it declines to
        dispatch side-effectful skills. model-baseline went 0/3 under it vs 2/3
        under the denylist alone, and interview-loop's "prep me for X" stopped
        firing. A control that suppresses the behaviour under test is a broken
        harness, not a safe one."""
        self.assertNotIn("--permission-mode", self._captured_call()["cmd"])

    def test_never_uses_the_broken_allowlist(self):
        self.assertNotIn("--allowedTools", self._captured_call()["cmd"])

    def test_denies_the_effectful_tools(self):
        cmd = self._captured_call()["cmd"]
        for tool in ("Bash", "Write", "Edit", "WebFetch", "WebSearch", "ToolSearch"):
            self.assertIn(tool, cmd, f"{tool} must be on the denylist")

    def test_toolsearch_specifically_is_denied(self):
        """With Bash alone denied, the model reached for ToolSearch to find another
        route. The denylist has to cover the escape hatch, not just the front door."""
        self.assertIn("ToolSearch", routing_mod._DENIED_TOOLS)

    def test_sets_the_recursion_guard_in_the_child_environment(self):
        env = self._captured_call()["env"]
        self.assertEqual(env.get(routing_mod.RECURSION_GUARD), "1")


class TestDocsCounts(unittest.TestCase):
    """Pinned against the drift that shipped: a README claiming 19 prompts /
    six groups / five negatives while the file held 23 / seven / eight."""

    CASES = [
        {"id": "a-01", "prompt": "x", "expected": "car-check", "acceptable": ["car-check"]},
        {"id": "a-02", "prompt": "y", "expected": "none", "acceptable": ["none"]},
        {"id": "b-01", "prompt": "z", "expected": "none", "acceptable": ["none"]},
    ]
    TRAPS = [{"label": "a", "ids": ["a-01", "a-02"]}, {"label": "b", "ids": ["b-01"]}]

    def _readme(self, text):
        d = Path(tempfile.mkdtemp())
        p = d / "README.md"
        p.write_text(text)
        return p

    def test_matching_counts_are_silent(self):
        p = self._readme("holds 3 frozen prompts across two confusable groups. Two expect *no* skill to fire.")
        self.assertEqual(check_docs_counts(self.CASES, self.TRAPS, p), [])

    def test_number_words_and_digits_both_parse(self):
        p = self._readme("holds three frozen prompts across 2 confusable groups. 2 expect no skill to fire.")
        self.assertEqual(check_docs_counts(self.CASES, self.TRAPS, p), [])

    def test_wrong_count_is_an_error_naming_both_numbers(self):
        p = self._readme("holds 19 frozen prompts across six confusable groups. Five expect *no* skill to fire.")
        findings = check_docs_counts(self.CASES, self.TRAPS, p)
        self.assertEqual([f.severity for f in findings], ["error", "error", "error"])
        self.assertIn("README says 19 frozen prompts; routing_cases.json has 3", findings[0].detail)

    def test_claims_wrap_across_lines(self):
        """The real README wraps mid-sentence; matching must survive newlines."""
        p = self._readme("holds 3 frozen prompts across\ntwo confusable groups, each with\nan `expected` skill. Two expect *no*\nskill to fire.")
        self.assertEqual(check_docs_counts(self.CASES, self.TRAPS, p), [])

    def test_unparseable_readme_is_info_not_error(self):
        """Rewording the prose should report a silence, never manufacture a failure."""
        p = self._readme("This README says nothing about the case set at all.")
        findings = check_docs_counts(self.CASES, self.TRAPS, p)
        self.assertEqual({f.severity for f in findings}, {"info"})

    def test_missing_readme_is_info(self):
        findings = check_docs_counts(self.CASES, self.TRAPS, Path(tempfile.mkdtemp()) / "nope.md")
        self.assertEqual([f.severity for f in findings], ["info"])

    def test_traps_not_partitioning_cases_blocks_the_comparison(self):
        """An ungrouped case makes the group count meaningless, so report that
        instead of asserting a number derived from a broken grouping."""
        p = self._readme("holds 3 frozen prompts across two confusable groups. Two expect *no* skill to fire.")
        findings = check_docs_counts(self.CASES, [{"label": "a", "ids": ["a-01"]}], p)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].severity, "error")
        self.assertIn("does not partition", findings[0].detail)

    def test_live_readme_matches_the_live_case_set(self):
        root = Path(__file__).resolve().parent.parent
        data = json.loads((root / "cases" / "routing_cases.json").read_text())
        findings = check_docs_counts(data["cases"], data["traps"], root / "README.md")
        self.assertEqual([f for f in findings if f.severity == "error"], [])


class TestDispatchCoverage(unittest.TestCase):
    def test_skill_with_no_cases_is_an_error(self):
        """The harness's worst silent failure: a sixth skill appears, Layer 1
        still reports 18/19, and nothing says the new one is untested."""
        skills = [make_skill("car-check", dirname="car-check"), make_skill("brand-new", dirname="brand-new")]
        cases = [{"id": "c1", "prompt": "x", "expected": "car-check", "acceptable": ["car-check"]}]
        findings = check_dispatch_coverage(skills, cases)
        self.assertEqual([f.skill for f in findings], ["brand-new"])
        self.assertEqual(findings[0].severity, "error")

    def test_skill_named_only_in_acceptable_counts_as_covered(self):
        skills = [make_skill("interview-loop", dirname="interview-loop")]
        cases = [{"id": "c1", "prompt": "x", "expected": "none", "acceptable": ["none", "interview-loop"]}]
        self.assertEqual(check_dispatch_coverage(skills, cases), [])

    def test_manual_only_skill_is_covered_by_a_guard_case(self):
        """toil-mining sets disable-model-invocation, so no positive case can
        pass. A negative case naming it in `guards` tests the flag instead."""
        s = make_skill("toil-mining", dirname="toil-mining")
        s.frontmatter["disable-model-invocation"] = "true"
        cases = [{"id": "c1", "prompt": "mine toil", "expected": "none", "acceptable": ["none"], "guards": ["toil-mining"]}]
        self.assertEqual(check_dispatch_coverage([s], cases), [])

    def test_guard_does_not_cover_a_dispatchable_skill(self):
        """Otherwise `guards` marks any skill covered without testing it."""
        s = make_skill("brand-new", dirname="brand-new")
        cases = [{"id": "c1", "prompt": "x", "expected": "none", "acceptable": ["none"], "guards": ["brand-new"]}]
        self.assertEqual([f.skill for f in check_dispatch_coverage([s], cases)], ["brand-new"])

    def test_guard_on_a_positive_case_does_not_count(self):
        s = make_skill("toil-mining", dirname="toil-mining")
        s.frontmatter["disable-model-invocation"] = "true"
        cases = [{"id": "c1", "prompt": "x", "expected": "car-check", "acceptable": ["car-check"], "guards": ["toil-mining"]}]
        self.assertEqual([f.skill for f in check_dispatch_coverage([s], cases)], ["toil-mining"])

    def test_live_skill_set_is_fully_covered(self):
        path = Path(__file__).resolve().parent.parent / "cases" / "routing_cases.json"
        cases = json.loads(path.read_text())["cases"]
        skills = discover([Path.home() / ".claude" / "skills"])
        if not skills:
            self.skipTest("no installed skills on this machine")
        self.assertEqual(check_dispatch_coverage(skills, cases), [])


class TestUntestedTriggers(unittest.TestCase):
    def test_quoted_phrase_with_no_case_is_reported(self):
        s = make_skill("mb", description='Say "run the baseline" or "test it when it drops".', dirname="mb")
        cases = [{"id": "c1", "prompt": "run the baseline against Opus", "expected": "mb", "acceptable": ["mb"]}]
        findings = check_untested_triggers([s], cases)
        self.assertEqual(len(findings), 1)
        self.assertIn("test it when it drops", findings[0].detail)
        self.assertNotIn('"run the baseline"', findings[0].detail)

    def test_fully_exercised_description_is_silent(self):
        s = make_skill("mb", description='Say "run the baseline".', dirname="mb")
        cases = [{"id": "c1", "prompt": "Run the baseline!", "expected": "mb", "acceptable": ["mb"]}]
        self.assertEqual(check_untested_triggers([s], cases), [])


class TestBaseline(unittest.TestCase):
    def setUp(self):
        self.skills = [make_skill("a", description="Do the thing.", dirname="a")]
        self.base = Baseline(
            descriptions={"a": description_hash("Do the thing.")}, cli_version="2.1.220", model="sonnet"
        )

    def test_no_drift_when_nothing_changed(self):
        self.assertEqual(self.base.drift(self.skills, "2.1.220", "sonnet"), [])

    def test_edited_description_drifts(self):
        edited = [make_skill("a", description="Do the thing, but also other things.", dirname="a")]
        reasons = self.base.drift(edited, "2.1.220", "sonnet")
        self.assertEqual(len(reasons), 1)
        self.assertIn("description changed", reasons[0])

    def test_reflowed_description_does_not_drift(self):
        """Whitespace normalization: rewrapping a long description is not a
        semantic change and must not force a paid re-run."""
        reflowed = [make_skill("a", description="Do   the\n  thing.", dirname="a")]
        self.assertEqual(self.base.drift(reflowed, "2.1.220", "sonnet"), [])

    def test_cli_upgrade_drifts(self):
        reasons = self.base.drift(self.skills, "2.2.0", "sonnet")
        self.assertIn("CLI changed", reasons[0])

    def test_model_change_drifts(self):
        reasons = self.base.drift(self.skills, "2.1.220", "opus")
        self.assertIn("model changed", reasons[0])

    def test_new_and_removed_skills_both_drift(self):
        two = self.skills + [make_skill("b", description="Another.", dirname="b")]
        self.assertTrue(any("new skill" in r for r in self.base.drift(two, "2.1.220", "sonnet")))
        self.assertTrue(any("removed" in r for r in self.base.drift([], "2.1.220", "sonnet")))

    def test_empty_baseline_treats_everything_as_new(self):
        reasons = Baseline().drift(self.skills, "2.1.220", "sonnet")
        self.assertTrue(any("never dispatch-tested" in r for r in reasons))

    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "baseline.json"
            Baseline().save(p, self.skills, "2.1.220", "sonnet")
            self.assertEqual(Baseline.load(p).drift(self.skills, "2.1.220", "sonnet"), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
