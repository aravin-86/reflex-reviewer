import unittest
from typing import cast

from reflex_reviewer.review_graph_runtime.agents import ReviewGraphAgents
from reflex_reviewer.review_graph_runtime.state import ReviewGraphState
from reflex_reviewer.review_output_contracts import (
    NON_REACT_OUTPUT_CONTRACT,
    REACT_OUTPUT_CONTRACT,
)


class _InMemoryResponseStore:
    def __init__(self, *_args, **_kwargs):
        self._value = None

    def get_previous_response_id(self, _key):
        return self._value

    def set_previous_response_id(self, _key, value):
        self._value = value


class ReviewGraphRuntimeAgentsTests(unittest.TestCase):
    def _build_agents(
        self,
        *,
        get_review_model_completion,
        react_enabled=True,
        react_max_judge_iterations=2,
    ):
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
            resolve_comment_severity=lambda severity, _path, _text=None: severity,
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
            react_max_judge_iterations=react_max_judge_iterations,
            react_max_tool_calls_per_agent=3,
            react_max_tool_result_chars=400,
            react_require_initial_repository_tool=True,
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

    def test_draft_reviewer_react_requires_initial_repository_tool_when_lazy_context_deferred(self):
        responses = [
            self._action_payload(
                '{"action":"final_review","review_data":{"verdict":"APPROVED","summary":"too-early","checklist":[],"comments":[]}}'
            ),
            self._action_payload(
                '{"action":"tool_call","tool_name":"get_repo_map","arguments":{}}'
            ),
            self._action_payload(
                '{"action":"final_review","review_data":{"verdict":"CHANGES_SUGGESTED","summary":"final-after-tool","checklist":[],"comments":[]}}'
            ),
        ]

        def _completion(*_args, **_kwargs):
            return responses.pop(0)

        agents = self._build_agents(get_review_model_completion=_completion, react_enabled=True)
        state = cast(
            ReviewGraphState,
            {
                "pr_id": 12,
                "halt": False,
                "draft_model": "draft-model",
                "stream_response": False,
                "model_endpoint": "chat_completions",
                "vcs_config": {},
                "draft_sys_p": "sys",
                "draft_user_p": "user",
                "changed_file_paths": ["src/app.py"],
                "changed_files_context": "- src/app.py",
                "repo_map": "Repository map unavailable during initial prompt bootstrap. Retrieval deferred for lazy ReAct context; use internal tools when additional repository evidence is required.",
                "related_files_context": "Related-file retrieval unavailable during initial prompt bootstrap. Deferred for lazy ReAct context; use internal tools when additional repository evidence is required.",
                "code_search_context": "Code search unavailable during initial prompt bootstrap. Retrieval deferred for lazy ReAct context; use internal tools when additional repository evidence is required.",
                "repository_path": "/tmp/repo",
            },
        )

        result = agents.draft_reviewer(state)
        self.assertEqual(result.get("draft_iteration_count"), 3)
        self.assertEqual(result.get("draft_tool_calls"), 1)
        self.assertEqual(
            result.get("draft_review_data", {}).get("summary"),
            "final-after-tool",
        )
        trace = result.get("draft_tool_trace") or []
        self.assertTrue(
            any(
                row.get("reason") == "initial-repository-tool-required"
                for row in trace
                if isinstance(row, dict)
            )
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

    def test_policy_guard_agent_forces_changes_suggested_for_unrejected_duplicate(self):
        def _completion(*_args, **_kwargs):
            return self._action_payload(
                '{"results":[{"comment_id":"700","sentiment":"NOT_REJECTED"}]}'
            )

        agents = self._build_agents(get_review_model_completion=_completion, react_enabled=True)
        state = cast(
            ReviewGraphState,
            {
                "pr_id": 44,
                "halt": False,
                "judge_model": "judge-model",
                "stream_response": False,
                "model_endpoint": "chat_completions",
                "vcs_config": {},
                "verdict": "APPROVED",
                "summary": "final summary",
                "checklist": [],
                "resolved_comments": [
                    {
                        "anchor": {"path": "src/service.py", "line": 10},
                        "path": "src/service.py",
                        "line": 10,
                        "severity": "CRITICAL",
                        "text": "Add better edge assertions",
                    }
                ],
                "existing_bot_inline_comments": [
                    {
                        "comment_id": "700",
                        "path": "src/service.py",
                        "line": 10,
                        "severity": "CRITICAL",
                        "text": "Add better edge assertions",
                        "reply_texts": ["I do not think this is fixed yet"],
                    }
                ],
                "skipped_inline_count": 0,
            },
        )

        result = agents.policy_guard_agent(state)
        self.assertEqual(result.get("verdict"), "CHANGES_SUGGESTED")
        self.assertEqual(result.get("resolved_comments"), [])
        self.assertEqual(result.get("existing_duplicate_suppressed_count"), 1)
        self.assertEqual(result.get("skipped_inline_count"), 1)
        self.assertIn("Prior bot feedback still appears applicable", result.get("summary", ""))
        checklist = result.get("checklist") or []
        self.assertTrue(any("Address existing bot comment:" in str(item) for item in checklist))

    def test_policy_guard_agent_keeps_approved_for_rejected_duplicate(self):
        def _completion(*_args, **_kwargs):
            return self._action_payload(
                '{"results":[{"comment_id":"700","sentiment":"REJECTED"}]}'
            )

        agents = self._build_agents(get_review_model_completion=_completion, react_enabled=True)
        state = cast(
            ReviewGraphState,
            {
                "pr_id": 45,
                "halt": False,
                "judge_model": "judge-model",
                "stream_response": False,
                "model_endpoint": "chat_completions",
                "vcs_config": {},
                "verdict": "APPROVED",
                "summary": "final summary",
                "checklist": [],
                "resolved_comments": [
                    {
                        "anchor": {"path": "src/service.py", "line": 10},
                        "path": "src/service.py",
                        "line": 10,
                        "severity": "CRITICAL",
                        "text": "Add better edge assertions",
                    }
                ],
                "existing_bot_inline_comments": [
                    {
                        "comment_id": "700",
                        "path": "src/service.py",
                        "line": 10,
                        "severity": "CRITICAL",
                        "text": "Add better edge assertions",
                        "reply_texts": ["This is a false positive and not applicable"],
                    }
                ],
                "skipped_inline_count": 0,
            },
        )

        result = agents.policy_guard_agent(state)
        self.assertEqual(result.get("verdict"), "APPROVED")
        self.assertEqual(result.get("resolved_comments"), [])
        self.assertEqual(result.get("existing_duplicate_suppressed_count"), 1)
        self.assertEqual(result.get("skipped_inline_count"), 1)
        self.assertEqual(result.get("summary"), "final summary")

    def test_policy_guard_agent_forces_changes_suggested_for_existing_unreplied_comment_without_new_findings(
        self,
    ):
        agents = self._build_agents(
            get_review_model_completion=lambda *_args, **_kwargs: self._action_payload(
                '{"results":[]}'
            ),
            react_enabled=True,
        )
        state = cast(
            ReviewGraphState,
            {
                "pr_id": 46,
                "halt": False,
                "judge_model": "judge-model",
                "stream_response": False,
                "model_endpoint": "chat_completions",
                "vcs_config": {},
                "verdict": "APPROVED",
                "summary": "final summary",
                "checklist": [],
                "resolved_comments": [],
                "existing_bot_inline_comments": [
                    {
                        "comment_id": "801",
                        "path": "src/service.py",
                        "line": 12,
                        "severity": "MAJOR",
                        "text": "Null handling still needs a guard clause.",
                        "reply_texts": [],
                    }
                ],
                "skipped_inline_count": 0,
            },
        )

        result = agents.policy_guard_agent(state)
        self.assertEqual(result.get("verdict"), "CHANGES_SUGGESTED")
        self.assertEqual(result.get("resolved_comments"), [])
        self.assertEqual(result.get("existing_duplicate_suppressed_count"), 0)
        self.assertEqual(result.get("skipped_inline_count"), 0)
        self.assertIn("Prior bot feedback still appears applicable", result.get("summary", ""))
        checklist = result.get("checklist") or []
        self.assertTrue(any("Address existing bot comment:" in str(item) for item in checklist))

    def test_policy_guard_agent_outstanding_checklist_omits_unknown_file_placeholder(self):
        agents = self._build_agents(
            get_review_model_completion=lambda *_args, **_kwargs: self._action_payload(
                '{"results":[]}'
            ),
            react_enabled=True,
        )
        state = cast(
            ReviewGraphState,
            {
                "pr_id": 48,
                "halt": False,
                "judge_model": "judge-model",
                "stream_response": False,
                "model_endpoint": "chat_completions",
                "vcs_config": {},
                "verdict": "APPROVED",
                "summary": "final summary",
                "checklist": [],
                "resolved_comments": [],
                "existing_bot_inline_comments": [
                    {
                        "comment_id": "803",
                        "path": "",
                        "line": 0,
                        "severity": "MAJOR",
                        "text": "This validator is called from updateDistributedDatabase when ifMatch is present, and it creates a new fixed thread pool for each request.",
                        "reply_texts": [],
                    }
                ],
                "skipped_inline_count": 0,
            },
        )

        result = agents.policy_guard_agent(state)
        checklist = [str(item) for item in (result.get("checklist") or [])]
        self.assertTrue(any("Address existing bot comment —" in item for item in checklist))
        self.assertTrue(all("unknown-file" not in item for item in checklist))

    def test_policy_guard_agent_keeps_approved_for_existing_rejected_comment_without_new_findings(
        self,
    ):
        def _completion(*_args, **_kwargs):
            return self._action_payload(
                '{"results":[{"comment_id":"802","sentiment":"REJECTED"}]}'
            )

        agents = self._build_agents(get_review_model_completion=_completion, react_enabled=True)
        state = cast(
            ReviewGraphState,
            {
                "pr_id": 47,
                "halt": False,
                "judge_model": "judge-model",
                "stream_response": False,
                "model_endpoint": "chat_completions",
                "vcs_config": {},
                "verdict": "APPROVED",
                "summary": "final summary",
                "checklist": [],
                "resolved_comments": [],
                "existing_bot_inline_comments": [
                    {
                        "comment_id": "802",
                        "path": "src/service.py",
                        "line": 18,
                        "severity": "MAJOR",
                        "text": "Please validate empty payload handling.",
                        "reply_texts": ["This is a false positive for this endpoint."],
                    }
                ],
                "skipped_inline_count": 0,
            },
        )

        result = agents.policy_guard_agent(state)
        self.assertEqual(result.get("verdict"), "APPROVED")
        self.assertEqual(result.get("resolved_comments"), [])
        self.assertEqual(result.get("existing_duplicate_suppressed_count"), 0)
        self.assertEqual(result.get("skipped_inline_count"), 0)
        self.assertEqual(result.get("summary"), "final summary")

    def test_evidence_judge_renders_react_output_contract_when_enabled(self):
        captured = {}

        def _judge_builder(**kwargs):
            captured["output_contract"] = kwargs.get("output_contract")
            return "judge-user-prompt"

        def _completion(model_name, sys_prompt, _user_prompt, **_kwargs):
            captured["judge_sys_p"] = sys_prompt
            return self._action_payload(
                '{"action":"final_review","review_data":{"verdict":"APPROVED","summary":"ok","checklist":[],"comments":[]}}'
            )

        agents = ReviewGraphAgents(
            get_review_model_completion=_completion,
            parse_review_payload=lambda payload: payload,
            extract_previous_response_id=lambda payload: payload.get("id")
            if isinstance(payload, dict)
            else None,
            build_judge_prompt_user_content=_judge_builder,
            build_repo_map_for_changed_files=lambda *_args, **_kwargs: "repo-map-context",
            retrieve_related_files_context=lambda *_args, **_kwargs: "related-files-context",
            retrieve_bounded_code_search_context=lambda *_args, **_kwargs: "search-code-context",
            compose_repository_context_bundle=lambda repo_map, related_files_context, code_search_context: {
                "repo_map": repo_map or "",
                "related_files_context": related_files_context or "",
                "code_search_context": code_search_context or "",
            },
            resolve_comment_severity=lambda severity, _path, _text=None: severity,
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
            react_enabled=True,
            react_max_draft_iterations=3,
            react_max_judge_iterations=2,
            react_max_tool_calls_per_agent=3,
            react_max_tool_result_chars=400,
            react_require_initial_repository_tool=True,
            react_allow_judge_tool_retrieval=True,
        )

        state = cast(
            ReviewGraphState,
            {
                "pr_id": 88,
                "halt": False,
                "judge_model": "judge-model",
                "stream_response": False,
                "model_endpoint": "chat_completions",
                "vcs_config": {},
                "team_name": "TEAM-ONE",
                "pr_title": "t",
                "pr_description": "d",
                "safe_diff": "diff",
                "existing_feedback": "feedback",
                "changed_files_context": "- src/app.py",
                "draft_review_data": {"comments": []},
            },
        )

        agents.evidence_judge(state)
        self.assertEqual(captured.get("output_contract"), REACT_OUTPUT_CONTRACT)
        self.assertIn('"action":"tool_call"', str(captured.get("judge_sys_p") or ""))

    def test_evidence_judge_react_requires_initial_repository_tool_when_lazy_context_deferred(
        self,
    ):
        responses = [
            self._action_payload(
                '{"action":"final_review","review_data":{"verdict":"APPROVED","summary":"too-early","checklist":[],"comments":[]}}'
            ),
            self._action_payload(
                '{"action":"tool_call","tool_name":"get_repo_map","arguments":{}}'
            ),
            self._action_payload(
                '{"action":"final_review","review_data":{"verdict":"CHANGES_SUGGESTED","summary":"final-after-tool","checklist":[],"comments":[]}}'
            ),
        ]

        def _completion(*_args, **_kwargs):
            return responses.pop(0)

        agents = self._build_agents(
            get_review_model_completion=_completion,
            react_enabled=True,
            react_max_judge_iterations=3,
        )
        state = cast(
            ReviewGraphState,
            {
                "pr_id": 90,
                "halt": False,
                "judge_model": "judge-model",
                "stream_response": False,
                "model_endpoint": "chat_completions",
                "vcs_config": {},
                "team_name": "TEAM-ONE",
                "pr_title": "t",
                "pr_description": "d",
                "safe_diff": "diff",
                "existing_feedback": "feedback",
                "changed_file_paths": ["src/app.py"],
                "changed_files_context": "- src/app.py",
                "repo_map": "Repository map unavailable during initial prompt bootstrap. Retrieval deferred for lazy ReAct context; use internal tools when additional repository evidence is required.",
                "related_files_context": "Related-file retrieval unavailable during initial prompt bootstrap. Deferred for lazy ReAct context; use internal tools when additional repository evidence is required.",
                "code_search_context": "Code search unavailable during initial prompt bootstrap. Retrieval deferred for lazy ReAct context; use internal tools when additional repository evidence is required.",
                "repository_path": "/tmp/repo",
                "draft_review_data": {"comments": []},
            },
        )

        result = agents.evidence_judge(state)
        self.assertEqual(result.get("judge_iteration_count"), 3)
        self.assertEqual(result.get("judge_tool_calls"), 1)
        self.assertEqual(result.get("review_data", {}).get("summary"), "final-after-tool")
        trace = result.get("judge_tool_trace") or []
        self.assertTrue(
            any(
                row.get("reason") == "initial-repository-tool-required"
                for row in trace
                if isinstance(row, dict)
            )
        )

    def test_evidence_judge_renders_non_react_output_contract_when_disabled(self):
        captured = {}

        def _judge_builder(**kwargs):
            captured["output_contract"] = kwargs.get("output_contract")
            return "judge-user-prompt"

        def _completion(model_name, sys_prompt, _user_prompt, **_kwargs):
            captured["judge_sys_p"] = sys_prompt
            return {
                "verdict": "APPROVED",
                "summary": "ok",
                "checklist": [],
                "comments": [],
            }

        agents = ReviewGraphAgents(
            get_review_model_completion=_completion,
            parse_review_payload=lambda payload: payload,
            extract_previous_response_id=lambda payload: payload.get("id")
            if isinstance(payload, dict)
            else None,
            build_judge_prompt_user_content=_judge_builder,
            build_repo_map_for_changed_files=lambda *_args, **_kwargs: "repo-map-context",
            retrieve_related_files_context=lambda *_args, **_kwargs: "related-files-context",
            retrieve_bounded_code_search_context=lambda *_args, **_kwargs: "search-code-context",
            compose_repository_context_bundle=lambda repo_map, related_files_context, code_search_context: {
                "repo_map": repo_map or "",
                "related_files_context": related_files_context or "",
                "code_search_context": code_search_context or "",
            },
            resolve_comment_severity=lambda severity, _path, _text=None: severity,
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
            react_enabled=False,
            react_max_draft_iterations=3,
            react_max_judge_iterations=2,
            react_max_tool_calls_per_agent=3,
            react_max_tool_result_chars=400,
            react_require_initial_repository_tool=True,
            react_allow_judge_tool_retrieval=True,
        )

        state = cast(
            ReviewGraphState,
            {
                "pr_id": 89,
                "halt": False,
                "judge_model": "judge-model",
                "stream_response": False,
                "model_endpoint": "chat_completions",
                "vcs_config": {},
                "team_name": "TEAM-ONE",
                "pr_title": "t",
                "pr_description": "d",
                "safe_diff": "diff",
                "existing_feedback": "feedback",
                "changed_files_context": "- src/app.py",
                "draft_review_data": {"comments": []},
            },
        )

        agents.evidence_judge(state)
        self.assertEqual(captured.get("output_contract"), NON_REACT_OUTPUT_CONTRACT)
        self.assertIn(
            "Return a valid JSON object with this structure:",
            str(captured.get("judge_sys_p") or ""),
        )


if __name__ == "__main__":
    unittest.main()