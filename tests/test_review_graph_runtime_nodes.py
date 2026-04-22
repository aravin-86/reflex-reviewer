import unittest
from typing import cast

from reflex_reviewer.review_graph_runtime.nodes import ReviewGraphNodes
from reflex_reviewer.review_graph_runtime.state import ReviewGraphState


def _noop(*_args, **_kwargs):
    return None


class ReviewGraphRuntimeNodesTests(unittest.TestCase):
    def _build_nodes(self, *, max_repo_map_chars, max_related_files_chars, max_code_search_chars):
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
            normalize_comment_severity=_noop,
            resolve_comment_severity=_noop,
            resolve_anchor_by_id=_noop,
            post_inline_comment=_noop,
            upsert_summary_comment=_noop,
            model_endpoint="responses",
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


if __name__ == "__main__":
    unittest.main()