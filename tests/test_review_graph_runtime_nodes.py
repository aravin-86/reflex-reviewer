import unittest
from typing import Any, cast

from reflex_reviewer.review_graph_runtime.nodes import ReviewGraphNodes
from reflex_reviewer.review_graph_runtime.state import ReviewGraphState


def _noop(*_args, **_kwargs):
    return None


class ReviewGraphRuntimeNodesTests(unittest.TestCase):
    def _build_nodes(
        self,
        *,
        max_repo_map_chars,
        max_related_files_chars,
        max_code_search_chars,
        react_enabled=False,
        resolve_comment_severity=None,
    ):
        severity_resolver = (
            resolve_comment_severity
            if resolve_comment_severity is not None
            else (lambda severity, _path: severity)
        )
        return ReviewGraphNodes(
            resolve_runtime_settings=_noop,
            get_vcs_client=_noop,
            resolve_repository_path=_noop,
            extract_changed_file_paths_from_diff=_noop,
            build_repo_map_for_changed_files=_noop,
            retrieve_related_files_context=_noop,
            retrieve_bounded_code_search_context=_noop,
            compose_repository_context_bundle=lambda repo_map, related_files, code_search: {
                "repo_map": repo_map,
                "related_files_context": related_files,
                "code_search_context": code_search,
            },
            repository_path=None,
            max_changed_files=0,
            max_repo_map_files=0,
            max_repo_map_chars=max_repo_map_chars,
            max_related_files=0,
            max_related_files_chars=max_related_files_chars,
            max_code_search_results=0,
            max_code_search_chars=max_code_search_chars,
            max_code_search_query_terms=0,
            repository_ignore_directories=set(),
            convert_to_unified_diff_and_anchor_index=_noop,
            truncate_diff=_noop,
            fetch_pr_metadata=_noop,
            fetch_pr_activities=_noop,
            build_existing_feedback_context=_noop,
            build_review_purpose=_noop,
            build_previous_response_id=_noop,
            normalize_comment_severity=lambda severity: severity,
            resolve_comment_severity=severity_resolver,
            resolve_anchor_by_id=_noop,
            post_inline_comment=_noop,
            upsert_summary_comment=_noop,
            model_endpoint="responses",
            react_enabled=react_enabled,
            react_lazy_repository_context=False,
            react_default_include_changed_files=True,
        )

    def test_prepare_review_inputs_renders_non_react_output_contract(self):
        nodes = self._build_nodes(
            max_repo_map_chars=100,
            max_related_files_chars=200,
            max_code_search_chars=300,
        )

        state = cast(
            ReviewGraphState,
            {
                "pr_id": 101,
                "team_name": "TEAM-ONE",
                "safe_diff": "diff",
                "review_purpose": "purpose",
                "pr_title": "title",
                "pr_description": "description",
                "existing_feedback": "feedback",
                "changed_files_context": "- src/app.py",
                "repository_context_bundle": {
                    "repo_map": "repo-map",
                    "related_files_context": "related",
                    "code_search_context": "search",
                },
            },
        )

        result = nodes.prepare_review_inputs(state)

        self.assertIn(
            "Return a valid JSON object with this structure:",
            str(result.get("draft_sys_p") or ""),
        )
        self.assertIn(
            "Return a valid JSON object with this structure:",
            str(result.get("draft_user_p") or ""),
        )
        self.assertNotIn("{{OUTPUT_CONTRACT}}", str(result.get("draft_sys_p") or ""))
        self.assertNotIn("{{OUTPUT_CONTRACT}}", str(result.get("draft_user_p") or ""))

    def test_prepare_review_inputs_renders_react_output_contract_when_enabled(self):
        nodes = self._build_nodes(
            max_repo_map_chars=100,
            max_related_files_chars=200,
            max_code_search_chars=300,
            react_enabled=True,
        )

        state = cast(
            ReviewGraphState,
            {
                "pr_id": 102,
                "team_name": "TEAM-ONE",
                "safe_diff": "diff",
                "review_purpose": "purpose",
                "pr_title": "title",
                "pr_description": "description",
                "existing_feedback": "feedback",
                "changed_files_context": "- src/app.py",
                "repository_context_bundle": {
                    "repo_map": "repo-map",
                    "related_files_context": "related",
                    "code_search_context": "search",
                },
            },
        )

        result = nodes.prepare_review_inputs(state)

        self.assertIn('"action":"tool_call"', str(result.get("draft_sys_p") or ""))
        self.assertIn('"action":"tool_call"', str(result.get("draft_user_p") or ""))
        self.assertIn(
            "Do not output the bare review schema directly in ReAct mode.",
            str(result.get("draft_sys_p") or ""),
        )

    def test_compose_repository_context_logs_used_and_configured_totals(self):
        nodes = self._build_nodes(
            max_repo_map_chars=100,
            max_related_files_chars=200,
            max_code_search_chars=300,
        )

        state = cast(
            ReviewGraphState,
            {
            "pr_id": 77,
            "repo_map": "abcd",
            "related_files_context": "efghij",
            "code_search_context": "klmnopqr",
            },
        )

        with self.assertLogs("reflex_reviewer.review_graph_runtime.nodes", level="INFO") as logs:
            result = nodes.compose_repository_context(state)

        self.assertEqual(result.get("repo_map"), "abcd")
        self.assertEqual(result.get("related_files_context"), "efghij")
        self.assertEqual(result.get("code_search_context"), "klmnopqr")

        self.assertTrue(
            any(
                "Repository context bundle composed. pr_id=77"
                in message
                and "total_used_chars=18" in message
                and "total_configured_chars=600" in message
                for message in logs.output
            )
        )
        self.assertTrue(
            any(
                "Repository context size estimate (tokens). pr_id=77"
                in message
                and "total_used_tokens_estimate=5" in message
                and "total_configured_tokens_estimate=150" in message
                for message in logs.output
            )
        )

    def test_policy_guard_suppresses_same_anchor_near_duplicates_against_existing_comments(
        self,
    ):
        nodes = self._build_nodes(
            max_repo_map_chars=100,
            max_related_files_chars=200,
            max_code_search_chars=300,
        )

        state = cast(
            ReviewGraphState,
            {
                "resolved_comments": [
                    {
                        "anchor": {"path": "src/config.py", "line": 88},
                        "path": "src/config.py",
                        "line": 88,
                        "severity": "ADVISORY",
                        "text": "Typo: COMPOSITEDATUGHSHARDSPACES should be COMPOSITEDATAGUARDSHARDSPACES.",
                    },
                    {
                        "anchor": {"path": "src/config.py", "line": 89},
                        "path": "src/config.py",
                        "line": 89,
                        "severity": "ADVISORY",
                        "text": "Validate null handling for realm fallback.",
                    },
                ],
                "existing_bot_inline_comments": [
                    {
                        "comment_id": "700",
                        "path": "src/config.py",
                        "line": 88,
                        "severity": "ADVISORY",
                        "text": "Typo in CompositeDataGuardShardSpaces value: COMPOSITEDATUGHSHARDSPACES -> COMPOSITEDATAGUARDSHARDSPACES.",
                        "reply_texts": [],
                    }
                ],
                "skipped_inline_count": 0,
            },
        )

        result = nodes.policy_guard(state)

        guarded_comments = cast(list[dict[str, Any]], result.get("resolved_comments") or [])
        self.assertEqual(len(guarded_comments), 1)
        self.assertEqual(guarded_comments[0].get("line"), 89)
        self.assertEqual(result.get("skipped_inline_count"), 1)

    def test_policy_guard_keeps_similar_text_when_anchor_differs(self):
        nodes = self._build_nodes(
            max_repo_map_chars=100,
            max_related_files_chars=200,
            max_code_search_chars=300,
        )

        state = cast(
            ReviewGraphState,
            {
                "resolved_comments": [
                    {
                        "anchor": {"path": "src/config.py", "line": 120},
                        "path": "src/config.py",
                        "line": 120,
                        "severity": "ADVISORY",
                        "text": "CompositeDataGuardShardSpaces(\"COMPOSITEDATUGHSHARDSPACES\") typo: change to COMPOSITEDATAGUARDSHARDSPACES.",
                    }
                ],
                "existing_bot_inline_comments": [
                    {
                        "comment_id": "700",
                        "path": "src/config.py",
                        "line": 88,
                        "severity": "ADVISORY",
                        "text": "Typo in CompositeDataGuardShardSpaces value: COMPOSITEDATUGHSHARDSPACES -> COMPOSITEDATAGUARDSHARDSPACES.",
                        "reply_texts": [],
                    }
                ],
                "skipped_inline_count": 0,
            },
        )

        result = nodes.policy_guard(state)

        guarded_comments = cast(list[dict[str, Any]], result.get("resolved_comments") or [])
        self.assertEqual(len(guarded_comments), 1)
        self.assertEqual(guarded_comments[0].get("line"), 120)
        self.assertEqual(result.get("skipped_inline_count"), 0)

    def test_policy_guard_suppresses_near_duplicates_within_current_batch_same_anchor(
        self,
    ):
        nodes = self._build_nodes(
            max_repo_map_chars=100,
            max_related_files_chars=200,
            max_code_search_chars=300,
        )

        state = cast(
            ReviewGraphState,
            {
                "resolved_comments": [
                    {
                        "anchor": {"path": "src/config.py", "line": 88},
                        "path": "src/config.py",
                        "line": 88,
                        "severity": "ADVISORY",
                        "text": "Typo in CompositeDataGuardShardSpaces value: COMPOSITEDATUGHSHARDSPACES -> COMPOSITEDATAGUARDSHARDSPACES.",
                    },
                    {
                        "anchor": {"path": "src/config.py", "line": 88},
                        "path": "src/config.py",
                        "line": 88,
                        "severity": "ADVISORY",
                        "text": "Typo: COMPOSITEDATUGHSHARDSPACES should be COMPOSITEDATAGUARDSHARDSPACES.",
                    },
                ],
                "existing_bot_inline_comments": [],
                "skipped_inline_count": 0,
            },
        )

        result = nodes.policy_guard(state)

        guarded_comments = cast(list[dict[str, Any]], result.get("resolved_comments") or [])
        self.assertEqual(len(guarded_comments), 1)
        self.assertEqual(result.get("skipped_inline_count"), 1)

    def test_policy_guard_applies_severity_priority_for_test_and_naming_comments(self):
        def _resolver(severity, path, text=None):
            normalized_path = str(path or "").lower()
            normalized_text = str(text or "").lower()
            if "/test/" in normalized_path or normalized_path.endswith("test.java"):
                return "ADVISORY"
            if "naming" in normalized_text and "name" in normalized_text:
                return "ADVISORY"
            return severity

        nodes = self._build_nodes(
            max_repo_map_chars=100,
            max_related_files_chars=200,
            max_code_search_chars=300,
            resolve_comment_severity=_resolver,
        )

        state = cast(
            ReviewGraphState,
            {
                "resolved_comments": [
                    {
                        "anchor": {
                            "path": "src/test/java/com/example/OrderServiceTest.java",
                            "line": 10,
                        },
                        "path": "src/test/java/com/example/OrderServiceTest.java",
                        "line": 10,
                        "severity": "CRITICAL",
                        "text": "Potential flaky test assertion.",
                    },
                    {
                        "anchor": {
                            "path": "src/main/java/com/example/OrderService.java",
                            "line": 21,
                        },
                        "path": "src/main/java/com/example/OrderService.java",
                        "line": 21,
                        "severity": "MAJOR",
                        "text": "Method naming convention: rename this function name.",
                    },
                    {
                        "anchor": {
                            "path": "src/main/java/com/example/OrderService.java",
                            "line": 44,
                        },
                        "path": "src/main/java/com/example/OrderService.java",
                        "line": 44,
                        "severity": "MAJOR",
                        "text": "Null pointer risk when auth header is missing.",
                    },
                ],
                "existing_bot_inline_comments": [],
                "skipped_inline_count": 0,
            },
        )

        result = nodes.policy_guard(state)

        guarded_comments = cast(list[dict[str, Any]], result.get("resolved_comments") or [])
        self.assertEqual(len(guarded_comments), 3)
        self.assertEqual(guarded_comments[0].get("severity"), "ADVISORY")
        self.assertEqual(guarded_comments[1].get("severity"), "ADVISORY")
        self.assertEqual(guarded_comments[2].get("severity"), "MAJOR")

    def test_policy_guard_forces_changes_suggested_when_existing_duplicate_is_suppressed(
        self,
    ):
        nodes = self._build_nodes(
            max_repo_map_chars=100,
            max_related_files_chars=200,
            max_code_search_chars=300,
        )

        state = cast(
            ReviewGraphState,
            {
                "verdict": "APPROVED",
                "resolved_comments": [
                    {
                        "anchor": {"path": "src/config.py", "line": 88},
                        "path": "src/config.py",
                        "line": 88,
                        "severity": "ADVISORY",
                        "text": "Typo: COMPOSITEDATUGHSHARDSPACES should be COMPOSITEDATAGUARDSHARDSPACES.",
                    }
                ],
                "existing_bot_inline_comments": [
                    {
                        "comment_id": "700",
                        "path": "src/config.py",
                        "line": 88,
                        "severity": "ADVISORY",
                        "text": "Typo in CompositeDataGuardShardSpaces value: COMPOSITEDATUGHSHARDSPACES -> COMPOSITEDATAGUARDSHARDSPACES.",
                        "reply_texts": [],
                    }
                ],
                "skipped_inline_count": 0,
            },
        )

        result = nodes.policy_guard(state)

        self.assertEqual(result.get("verdict"), "CHANGES_SUGGESTED")
        self.assertEqual(result.get("existing_duplicate_suppressed_count"), 1)
        self.assertEqual(result.get("skipped_inline_count"), 1)
        self.assertEqual(result.get("resolved_comments"), [])

    def test_policy_guard_keeps_approved_when_no_prior_or_current_findings(self):
        nodes = self._build_nodes(
            max_repo_map_chars=100,
            max_related_files_chars=200,
            max_code_search_chars=300,
        )

        state = cast(
            ReviewGraphState,
            {
                "verdict": "APPROVED",
                "resolved_comments": [],
                "existing_bot_inline_comments": [],
                "skipped_inline_count": 0,
            },
        )

        result = nodes.policy_guard(state)

        self.assertEqual(result.get("verdict"), "APPROVED")
        self.assertEqual(result.get("existing_duplicate_suppressed_count"), 0)
        self.assertEqual(result.get("skipped_inline_count"), 0)


if __name__ == "__main__":
    unittest.main()