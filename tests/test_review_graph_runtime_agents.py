import unittest
from typing import cast

from reflex_reviewer.review_graph_runtime.agents import ReviewGraphAgents
from reflex_reviewer.review_graph_runtime.state import ReviewGraphState


class _InMemoryResponseStore:
    def __init__(self, *_args, **_kwargs):
        self._value = None

    def get_previous_response_id(self, _key):
        return self._value

    def set_previous_response_id(self, _key, value):
        self._value = value


class ReviewGraphRuntimeAgentsTests(unittest.TestCase):
    def _build_agents(self, *, get_review_model_completion, react_enabled=True):
        return ReviewGraphAgents(
            get_review_model_completion=get_review_model_completion,
            parse_review_payload=lambda payload: payload,
            extract_previous_response_id=lambda payload: payload.get("id")
            if isinstance(payload, dict)
            else None,
            build_judge_prompt_user_content=lambda **kwargs: "judge-user-prompt",
            build_repo_map_for_changed_files=lambda *_args, **_kwargs: "repo-map-context",
            retrieve_related_files_context=lambda *_args, **_kwargs: "related-files-context",
            retrieve_bounded_code_search_context=lambda *_args, **_kwargs: "search-code-context",
            compose_repository_context_bundle=lambda repo_map, related_files_context, code_search_context: {
                "repo_map": repo_map or "",
                "related_files_context": related_files_context or "",
                "code_search_context": code_search_context or "",
            },
            max_repo_map_files=10,
            max_repo_map_chars=500,
            max_related_files=10,
            max_related_files_chars=500,
            max_code_search_results=10,
            max_code_search_chars=500,
            max_code_search_query_terms=10,
            repository_ignore_directories=set(),
            response_state_store_cls=_InMemoryResponseStore,
            response_state_file="data/state.json",
            response_state_ttl_days=7,
            model_endpoint="chat_completions",
            react_enabled=react_enabled,
            react_max_draft_iterations=3,
            react_max_judge_iterations=2,
            react_max_tool_calls_per_agent=3,
            react_max_tool_result_chars=400,
            react_allow_judge_tool_retrieval=True,
        )

    @staticmethod
    def _action_payload(action):
        return {
            "choices": [{"message": {"content": action}}],
        }

    def test_draft_reviewer_react_handles_tool_call_then_final_review(self):
        responses = [
            self._action_payload(
                '{"action":"tool_call","tool_name":"get_repo_map","arguments":{}}'
            ),
            self._action_payload(
                '{"action":"final_review","review_data":{"verdict":"CHANGES_SUGGESTED","summary":"final","checklist":[],"comments":[{"anchor_id":"F1-L10","severity":"MAJOR","text":"Issue"}]}}'
            ),
        ]

        def _completion(*_args, **_kwargs):
            return responses.pop(0)

        agents = self._build_agents(get_review_model_completion=_completion, react_enabled=True)
        state = cast(
            ReviewGraphState,
            {
                "pr_id": 11,
                "halt": False,
                "draft_model": "draft-model",
                "stream_response": False,
                "model_endpoint": "chat_completions",
                "vcs_config": {},
                "draft_sys_p": "sys",
                "draft_user_p": "user",
                "changed_file_paths": ["src/app.py"],
                "changed_files_context": "- src/app.py",
                "repository_path": "/tmp/repo",
            },
        )

        result = agents.draft_reviewer(state)
        self.assertEqual(result.get("draft_iteration_count"), 2)
        self.assertEqual(result.get("draft_tool_calls"), 1)
        self.assertEqual(
            result.get("draft_review_data", {}).get("summary"),
            "final",
        )
        self.assertEqual(
            len(result.get("draft_review_data", {}).get("comments", [])),
            1,
        )

    def test_draft_reviewer_react_rejects_invalid_tool_and_still_finalizes(self):
        responses = [
            self._action_payload(
                '{"action":"tool_call","tool_name":"invalid_tool","arguments":{}}'
            ),
            self._action_payload(
                '{"action":"final_review","review_data":{"verdict":"APPROVED","summary":"done","checklist":[],"comments":[]}}'
            ),
        ]

        def _completion(*_args, **_kwargs):
            return responses.pop(0)

        agents = self._build_agents(get_review_model_completion=_completion, react_enabled=True)
        state = cast(
            ReviewGraphState,
            {
                "pr_id": 22,
                "halt": False,
                "draft_model": "draft-model",
                "stream_response": False,
                "model_endpoint": "chat_completions",
                "vcs_config": {},
                "draft_sys_p": "sys",
                "draft_user_p": "user",
                "changed_file_paths": ["src/app.py"],
                "changed_files_context": "- src/app.py",
                "repository_path": "/tmp/repo",
            },
        )

        result = agents.draft_reviewer(state)
        self.assertEqual(result.get("draft_iteration_count"), 2)
        self.assertEqual(result.get("draft_tool_calls"), 1)
        trace = result.get("draft_tool_trace") or []
        self.assertEqual(trace[0].get("status"), "error")
        self.assertEqual(result.get("draft_review_data", {}).get("verdict"), "APPROVED")

    def test_draft_reviewer_react_stops_at_max_iterations(self):
        responses = [
            self._action_payload(
                '{"action":"tool_call","tool_name":"get_repo_map","arguments":{}}'
            ),
            self._action_payload(
                '{"action":"tool_call","tool_name":"search_code","arguments":{}}'
            ),
            self._action_payload(
                '{"action":"tool_call","tool_name":"get_related_files","arguments":{}}'
            ),
        ]

        def _completion(*_args, **_kwargs):
            return responses.pop(0)

        agents = self._build_agents(get_review_model_completion=_completion, react_enabled=True)
        state = cast(
            ReviewGraphState,
            {
                "pr_id": 33,
                "halt": False,
                "draft_model": "draft-model",
                "stream_response": False,
                "model_endpoint": "chat_completions",
                "vcs_config": {},
                "draft_sys_p": "sys",
                "draft_user_p": "user",
                "changed_file_paths": ["src/app.py"],
                "changed_files_context": "- src/app.py",
                "repository_path": "/tmp/repo",
            },
        )

        result = agents.draft_reviewer(state)
        self.assertEqual(result.get("draft_iteration_count"), 3)
        self.assertEqual(result.get("draft_tool_calls"), 3)
        self.assertEqual(
            result.get("draft_review_data", {}).get("summary"),
            "No review output generated.",
        )


if __name__ == "__main__":
    unittest.main()