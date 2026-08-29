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

from harness.discover import Skill, is_templated, parse_frontmatter  # noqa: E402
from harness.lint import check_duplicate_names, check_name_matches_dir, check_references, check_trigger_collisions  # noqa: E402
from harness.preflight import envelope_error  # noqa: E402
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
