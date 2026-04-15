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
        with mock.patch.object(summary, "_call_llm", return_value=json.dumps({"summarize": ["Docs.md"]})):
            selected, low = summary.triage_files(["Proof.lean", "Docs.md"], diff_by_file, "dummy-model")
        self.assertEqual(low, [])
        self.assertEqual(selected, ["Proof.lean", "Docs.md"])

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

    def test_find_existing_comment_matches_legacy_identifier(self):
        pr = FakePR([FakeComment("text\n<!-- gemini-pr-summary-2026-01-01-00-00-00 -->\n")])
        found = summary.find_existing_comment(pr)
        self.assertIsNotNone(found)


if __name__ == "__main__":
    unittest.main()
