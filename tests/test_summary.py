import json
import sys
import types
import unittest
from unittest import mock


github_module = types.ModuleType("github")
github_module.Github = object
github_module.Auth = types.SimpleNamespace(Token=object)
sys.modules.setdefault("github", github_module)

pull_request_module = types.ModuleType("github.PullRequest")
pull_request_module.PullRequest = object
sys.modules.setdefault("github.PullRequest", pull_request_module)

repo_module = types.ModuleType("github.Repository")
repo_module.Repository = object
sys.modules.setdefault("github.Repository", repo_module)

import summary


class FakeComment:
    def __init__(self, body):
        self.body = body


class FakePR:
    def __init__(self, comments):
        self._comments = comments

    def get_issue_comments(self):
        return self._comments


class SummaryTests(unittest.TestCase):
    def test_truncate_file_diff_uses_hunk_boundary(self):
        file_diff = (
            "diff --git a/A.lean b/A.lean\n"
            "@@ -1,3 +1,3 @@\n"
            "-old\n"
            "+new\n"
            "@@ -20,3 +20,3 @@\n"
            "-old2\n"
            "+new2\n"
        )
        truncated, was_truncated = summary._truncate_file_diff(file_diff, max_chars=55)
        self.assertTrue(was_truncated)
        self.assertIn("@@ -1,3 +1,3 @@", truncated)
        self.assertNotIn("@@ -20,3 +20,3 @@", truncated)

    def test_triage_enforces_proof_signal_file_in_normal_mode(self):
        diff_by_file = {
            "Proof.lean": "diff --git a/Proof.lean b/Proof.lean\n+  sorry\n",
            "Docs.md": "diff --git a/Docs.md b/Docs.md\n+docs\n",
        }
        triage_response = summary._TriageSimple(summarize=["Docs.md"])
        with mock.patch.object(summary, "_call_llm", return_value=triage_response):
            selected, low = summary.triage_files(["Proof.lean", "Docs.md"], diff_by_file, "dummy-model")
        self.assertEqual(low, [])
        self.assertEqual(selected, ["Proof.lean", "Docs.md"])

    def test_triage_tiered_promotes_proof_signal_to_high(self):
        # Tiered mode triggers when |files| > LARGE_PR_FILE_THRESHOLD.
        file_paths = [f"Noise{i}.md" for i in range(50)] + ["Proof.lean"]
        diff_by_file = {fp: f"diff --git a/{fp} b/{fp}\n+x\n" for fp in file_paths}
        # Proof.lean contains a proof signal so should be force-promoted to high,
        # even though the triage agent puts it in `low`.
        diff_by_file["Proof.lean"] = "diff --git a/Proof.lean b/Proof.lean\n+  sorry\n"
        triage_response = summary._TriageTiered(
            high=["Noise0.md"], low=["Proof.lean", "Noise1.md"],
        )
        with mock.patch.object(summary, "_call_llm", return_value=triage_response):
            high, low = summary.triage_files(file_paths, diff_by_file, "dummy-model")
        self.assertIn("Proof.lean", high)
        self.assertNotIn("Proof.lean", low)

    def test_triage_falls_back_to_all_files_on_provider_failure(self):
        diff_by_file = {
            "Proof.lean": "diff --git a/Proof.lean b/Proof.lean\n+  sorry\n",
            "Docs.md": "diff --git a/Docs.md b/Docs.md\n+docs\n",
        }
        with mock.patch.object(summary, "_call_llm", side_effect=RuntimeError("API down")):
            selected, low = summary.triage_files(["Proof.lean", "Docs.md"], diff_by_file, "dummy-model")
        self.assertEqual(low, [])
        self.assertEqual(set(selected), {"Proof.lean", "Docs.md"})

    def test_call_prose_unwraps_summary_field(self):
        """_call_prose wraps generate_structured with _ProseSummary and returns the summary string."""
        fake_provider = mock.Mock()
        fake_provider.generate_structured.return_value = (
            summary._ProseSummary(summary="hello world"),
            summary.TokenUsage(input_tokens=1, output_tokens=1),
        )
        original_provider = summary._provider
        try:
            summary._provider = fake_provider
            result = summary._call_prose("any prompt", "dummy-model")
        finally:
            summary._provider = original_provider
        self.assertEqual(result, "hello world")
        # Confirms the schema wiring — we should have asked for _ProseSummary.
        _, kwargs = fake_provider.generate_structured.call_args
        self.assertIs(kwargs["schema"], summary._ProseSummary)

    def test_analyzer_uses_source_lookup_for_body_only_sorry_change(self):
        old_source = "\n".join([
            "theorem bodyOnly : True := by",
            "  have h : True := by",
            "    trivial",
            "  exact h",
        ])
        new_source = "\n".join([
            "theorem bodyOnly : True := by",
            "  have h : True := by",
            "    sorry",
            "  exact h",
        ])
        diff = "\n".join([
            "diff --git a/Test.lean b/Test.lean",
            "@@ -3,1 +3,1 @@",
            "-    trivial",
            "+    sorry",
        ])

        def fake_load(path, revision=None):
            if revision:
                return old_source
            return new_source

        with mock.patch.object(summary, "_load_lean_source", side_effect=fake_load):
            analyzer = summary.DiffAnalyzer(["theorem"], base_revision="base")
            _, added, removed, affected, *_ = analyzer.analyze(diff)

        self.assertEqual(len(added), 1)
        self.assertEqual(removed, [])
        self.assertEqual(affected, [])
        self.assertIn("bodyOnly", added[0])

    # ------------------------------------------------------------------
    # Provider wiring regression guards for the generate_structured refactor
    # ------------------------------------------------------------------

    def test_summarize_file_diff_substitutes_placeholders_and_returns_prose(self):
        captured = {}

        def fake(prompt, model_name, schema):
            captured["prompt"] = prompt
            captured["schema"] = schema
            return summary._ProseSummary(summary="file summary")

        with mock.patch.object(summary, "_call_llm", side_effect=fake):
            result = summary.summarize_file_diff(
                "X.lean",
                "+++diff+++",
                "model",
                "FILE={{FILE_PATH}} D={{FILE_DIFF}}",
            )

        self.assertEqual(result, "file summary")
        self.assertIn("FILE=X.lean", captured["prompt"])
        self.assertIn("D=+++diff+++", captured["prompt"])
        self.assertIs(captured["schema"], summary._ProseSummary)

    def test_synthesize_summary_empty_result_raises(self):
        empty = summary._ProseSummary(summary="")
        template = "T={{PR_TITLE}} B={{PR_BODY}} S={{PER_FILE_SUMMARIES}} H={{PR_TYPE_HINT}}"
        with mock.patch.object(summary, "_read_prompt_template", return_value=template), \
             mock.patch.object(summary, "_call_llm", return_value=empty):
            with self.assertRaises(RuntimeError):
                summary.synthesize_summary(["f1"], "model", "title", "body", "hint ")

    def test_synthesize_summary_substitutes_all_placeholders(self):
        captured = {}

        def fake(prompt, model_name, schema):
            captured["prompt"] = prompt
            return summary._ProseSummary(summary="synthesised")

        template = "T={{PR_TITLE}} B={{PR_BODY}} S={{PER_FILE_SUMMARIES}} H={{PR_TYPE_HINT}}"
        with mock.patch.object(summary, "_read_prompt_template", return_value=template), \
             mock.patch.object(summary, "_call_llm", side_effect=fake):
            result = summary.synthesize_summary(["f1", "f2"], "model", "title", "body", "hint ")

        self.assertEqual(result, "synthesised")
        self.assertIn("T=title", captured["prompt"])
        self.assertIn("B=body", captured["prompt"])
        self.assertIn("- f1", captured["prompt"])
        self.assertIn("- f2", captured["prompt"])
        self.assertIn("H=hint ", captured["prompt"])

    def test_refine_summary_falls_back_to_draft_on_exception(self):
        with mock.patch.object(summary, "_read_prompt_template", return_value="x"), \
             mock.patch.object(summary, "_call_llm", side_effect=RuntimeError("API down")):
            result = summary.refine_summary("draft!", "title", "body", "model")
        self.assertEqual(result, "draft!")

    def test_check_style_adherence_returns_none_without_style_guide(self):
        # The empty-style-guide short-circuit must not hit the provider.
        with mock.patch.object(summary, "_call_llm", side_effect=AssertionError("provider must not be called")):
            result = summary.check_style_adherence("diff content", "", "model", "template")
        self.assertIsNone(result)

    # ------------------------------------------------------------------
    # DiffAnalyzer: quality signals and declaration tracking
    # ------------------------------------------------------------------

    def test_diff_analyzer_flags_quality_signals(self):
        diff = "\n".join([
            "diff --git a/Q.lean b/Q.lean",
            "@@ -1,1 +1,4 @@",
            " def existing : Nat := 1",
            "+theorem foo : True := by native_decide",
            "+theorem bar : False := by admit",
            "+#eval foo",
        ])
        with mock.patch.object(summary, "_load_lean_source", return_value=""):
            analyzer = summary.DiffAnalyzer(["theorem", "def"])
            *_, warnings = analyzer.analyze(diff)
        signals = {w["signal"] for w in warnings}
        self.assertEqual(signals, {"native_decide", "admit", "#eval"})

    def test_diff_analyzer_ignores_commented_quality_signals(self):
        diff = "\n".join([
            "diff --git a/Q.lean b/Q.lean",
            "@@ -1,1 +1,2 @@",
            " def foo := 1",
            "+-- TODO: replace with native_decide when proof is stable",
        ])
        with mock.patch.object(summary, "_load_lean_source", return_value=""):
            analyzer = summary.DiffAnalyzer(["def"])
            *_, warnings = analyzer.analyze(diff)
        self.assertEqual(warnings, [])

    def test_diff_analyzer_tracks_added_and_removed_decls(self):
        diff = "\n".join([
            "diff --git a/X.lean b/X.lean",
            "@@ -1,2 +1,2 @@",
            " existing line",
            "-theorem oldThm : False := by skip",
            "+theorem newThm : True := trivial",
        ])
        with mock.patch.object(summary, "_load_lean_source", return_value=""):
            analyzer = summary.DiffAnalyzer(["theorem"])
            _, _, _, _, added_decls, removed_decls, affected_decls, _ = analyzer.analyze(diff)
        self.assertTrue(any("newThm" in s for s in added_decls))
        self.assertTrue(any("oldThm" in s for s in removed_decls))
        self.assertEqual(affected_decls, [])

    def test_diff_analyzer_tracks_affected_decl_when_same_name_in_both(self):
        diff = "\n".join([
            "diff --git a/X.lean b/X.lean",
            "@@ -1,1 +1,1 @@",
            "-theorem sharedThm : Old := old",
            "+theorem sharedThm : New := new",
        ])
        with mock.patch.object(summary, "_load_lean_source", return_value=""):
            analyzer = summary.DiffAnalyzer(["theorem"])
            _, _, _, _, added_decls, removed_decls, affected_decls, _ = analyzer.analyze(diff)
        self.assertEqual(added_decls, [])
        self.assertEqual(removed_decls, [])
        self.assertEqual(len(affected_decls), 1)
        self.assertEqual(affected_decls[0]["file"], "X.lean")

    # ------------------------------------------------------------------
    # SummaryCache
    # ------------------------------------------------------------------

    def _make_cache_comment(self, fingerprint, cache_payload):
        """Build a comment body in the shape SummaryCache expects."""
        body = "### 🤖 PR Summary\n\n"
        body += summary.COMMENT_IDENTIFIER.replace("{{timestamp}}", "2026-04-18-T") + "\n\n"
        body += f"{summary.CACHE_IDENTIFIER}{json.dumps(cache_payload)}-->\n\n"
        return FakePR([FakeComment(body)])

    def test_summary_cache_returns_entry_when_fingerprint_matches(self):
        fp = "fingerprint-abc"
        payload = {"File.lean": {"hash": "h1", "summary": "cached summary"}, "_config": fp}
        pr = self._make_cache_comment(fp, payload)
        cache = summary.SummaryCache(pr, fp)
        self.assertEqual(cache.get("File.lean", "h1"), "cached summary")
        # Hash mismatch → miss.
        self.assertIsNone(cache.get("File.lean", "h2"))

    def test_summary_cache_invalidates_on_stale_fingerprint(self):
        payload = {"File.lean": {"hash": "h1", "summary": "x"}, "_config": "old-fp"}
        pr = self._make_cache_comment("old-fp", payload)
        cache = summary.SummaryCache(pr, "new-fp")
        self.assertIsNone(cache.get("File.lean", "h1"))

    # ------------------------------------------------------------------
    # Pure helpers: validate_pr_title, split_diff_into_files, _detect_proof_signals
    # ------------------------------------------------------------------

    def test_validate_pr_title_valid_plain(self):
        is_valid, t, msg = summary.validate_pr_title("feat: add X")
        self.assertTrue(is_valid)
        self.assertEqual(t, "feat")
        self.assertIsNone(msg)

    def test_validate_pr_title_valid_with_scope(self):
        is_valid, t, msg = summary.validate_pr_title("fix(auth): handle null token")
        self.assertTrue(is_valid)
        self.assertEqual(t, "fix")
        self.assertIsNone(msg)

    def test_validate_pr_title_invalid_format(self):
        is_valid, t, msg = summary.validate_pr_title("Add feature X")
        self.assertFalse(is_valid)
        self.assertIsNone(t)
        self.assertIn("conventional commit", msg)

    def test_validate_pr_title_empty_is_accepted(self):
        # No title supplied → nothing to validate; short-circuit to True.
        is_valid, t, msg = summary.validate_pr_title("")
        self.assertTrue(is_valid)
        self.assertIsNone(t)
        self.assertIsNone(msg)

    def test_split_diff_into_files_multi_file(self):
        diff = "\n".join([
            "diff --git a/A.lean b/A.lean",
            "--- a/A.lean",
            "+++ b/A.lean",
            "@@ -1 +1 @@",
            "-old",
            "+new",
            "diff --git a/B.lean b/B.lean",
            "--- a/B.lean",
            "+++ b/B.lean",
            "@@ -1 +1 @@",
            "-old2",
            "+new2",
        ])
        result = summary.split_diff_into_files(diff)
        self.assertIn("A.lean", result)
        self.assertIn("B.lean", result)
        self.assertIn("-old\n+new", result["A.lean"])
        self.assertIn("-old2\n+new2", result["B.lean"])

    def test_split_diff_into_files_empty(self):
        self.assertEqual(summary.split_diff_into_files(""), {})

    def test_detect_proof_signals_each_keyword(self):
        for keyword in ("sorry", "admit", "native_decide"):
            diff = f"diff --git a/X.lean b/X.lean\n+  {keyword}\n"
            self.assertEqual(summary._detect_proof_signals(diff), {keyword})

    def test_detect_proof_signals_all_keywords_combined(self):
        diff = "\n".join([
            "diff --git a/X.lean b/X.lean",
            "+  sorry",
            "-  admit",
            "+  native_decide",
        ])
        self.assertEqual(
            summary._detect_proof_signals(diff),
            {"sorry", "admit", "native_decide"},
        )

    def test_detect_proof_signals_ignores_context_lines(self):
        # Context lines (leading space, no +/-) must not register a match.
        diff = "diff --git a/X.lean b/X.lean\n  sorry on a context line\n"
        self.assertEqual(summary._detect_proof_signals(diff), set())

    def test_detect_proof_signals_ignores_diff_headers(self):
        # `+++` and `---` are file-header markers, not added/removed content.
        diff = "+++ b/sorry.lean\n--- a/sorry.lean\n def real := 1\n"
        self.assertEqual(summary._detect_proof_signals(diff), set())


if __name__ == "__main__":
    unittest.main()
