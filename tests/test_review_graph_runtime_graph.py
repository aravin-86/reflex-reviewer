import unittest
from unittest.mock import Mock, patch

from reflex_reviewer.review_graph_runtime import graph as graph_module


def _noop(*_args, **_kwargs):
    return None


class ReviewGraphRuntimeGraphTests(unittest.TestCase):
    def _execute_graph(self, *, resolve_repository_path, react_enabled=True):
        compiled_graph = Mock()
        compiled_graph.invoke.return_value = {"halt": False}

        with patch.object(graph_module, "build_review_graph", return_value=compiled_graph) as mock_build:
            result = graph_module.execute_review_graph(
                initial_state={"pr_id": 4315, "halt": False},
                resolve_runtime_settings=_noop,
                get_vcs_client=_noop,
                resolve_repository_path=resolve_repository_path,
                extract_changed_file_paths_from_diff=_noop,
                build_repo_map_for_changed_files=_noop,
                retrieve_related_files_context=_noop,
                retrieve_bounded_code_search_context=_noop,
                compose_repository_context_bundle=_noop,
                repository_path="",
                max_changed_files=400,
                max_repo_map_files=150,
                max_repo_map_chars=100000,
                max_related_files=80,
                max_related_files_chars=150000,
                max_code_search_results=500,
                max_code_search_chars=150000,
                max_code_search_query_terms=50,
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
                get_review_model_completion=_noop,
                parse_review_payload=_noop,
                extract_previous_response_id=_noop,
                build_judge_prompt_user_content=_noop,
                response_state_store_cls=Mock,
                response_state_file="data/review_previous_response_ids.json",
                response_state_ttl_days=30,
                model_endpoint="chat_completions",
                react_enabled=react_enabled,
                react_max_draft_iterations=4,
                react_max_judge_iterations=3,
                react_max_tool_calls_per_agent=8,
                react_max_tool_result_chars=12000,
                react_require_initial_repository_tool=True,
                react_allow_judge_tool_retrieval=True,
                react_lazy_repository_context=True,
                react_default_include_changed_files=True,
            )

        return result, mock_build, compiled_graph

    def test_execute_review_graph_disables_react_when_repository_path_unavailable(self):
        with self.assertLogs("reflex_reviewer.review_graph_runtime.graph", level="INFO") as logs:
            result, mock_build, compiled_graph = self._execute_graph(
                resolve_repository_path=lambda _repository_path: None,
                react_enabled=True,
            )

        self.assertEqual(result.get("halt"), False)
        compiled_graph.invoke.assert_called_once_with({"pr_id": 4315, "halt": False})
        self.assertEqual(mock_build.call_args.kwargs.get("react_enabled"), False)
        self.assertIsNone(mock_build.call_args.kwargs.get("repository_path"))
        self.assertTrue(
            any(
                "ReAct disabled because REPOSITORY_PATH is unset or invalid. pr_id=4315"
                in line
                for line in logs.output
            )
        )

    def test_execute_review_graph_keeps_react_enabled_when_repository_path_is_resolved(self):
        result, mock_build, compiled_graph = self._execute_graph(
            resolve_repository_path=lambda _repository_path: "/tmp/sample-repo",
            react_enabled=True,
        )

        self.assertEqual(result.get("halt"), False)
        compiled_graph.invoke.assert_called_once_with({"pr_id": 4315, "halt": False})
        self.assertEqual(mock_build.call_args.kwargs.get("react_enabled"), True)
        self.assertEqual(
            mock_build.call_args.kwargs.get("repository_path"),
            "/tmp/sample-repo",
        )
