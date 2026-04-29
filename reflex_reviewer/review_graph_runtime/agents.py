import logging
from pathlib import Path
from typing import Dict, Tuple, cast

from reflex_reviewer.llm.response_handler import (
    extract_content_from_non_stream_response,
    extract_content_from_stream_response,
    extract_json_from_content,
)

from .state import ReviewGraphState

logger = logging.getLogger(__name__)


class ReviewGraphAgents:
    _ACTION_TOOL_CALL = "tool_call"
    _ACTION_FINAL_REVIEW = "final_review"
    _DEFAULT_EMPTY_REVIEW = {
        "verdict": "CHANGES_SUGGESTED",
        "summary": "No review output generated.",
        "checklist": [],
        "comments": [],
    }

    def __init__(
        self,
        *,
        get_review_model_completion,
        parse_review_payload,
        extract_previous_response_id,
        build_judge_prompt_user_content,
        build_repo_map_for_changed_files,
        retrieve_related_files_context,
        retrieve_bounded_code_search_context,
        compose_repository_context_bundle,
        max_repo_map_files,
        max_repo_map_chars,
        max_related_files,
        max_related_files_chars,
        max_code_search_results,
        max_code_search_chars,
        max_code_search_query_terms,
        repository_ignore_directories,
        response_state_store_cls,
        response_state_file,
        response_state_ttl_days,
        model_endpoint,
        react_enabled,
        react_max_draft_iterations,
        react_max_judge_iterations,
        react_max_tool_calls_per_agent,
        react_max_tool_result_chars,
        react_allow_judge_tool_retrieval,
    ):
        self._get_review_model_completion = get_review_model_completion
        self._parse_review_payload = parse_review_payload
        self._extract_previous_response_id = extract_previous_response_id
        self._build_judge_prompt_user_content = build_judge_prompt_user_content
        self._build_repo_map_for_changed_files = build_repo_map_for_changed_files
        self._retrieve_related_files_context = retrieve_related_files_context
        self._retrieve_bounded_code_search_context = retrieve_bounded_code_search_context
        self._compose_repository_context_bundle = compose_repository_context_bundle
        self._max_repo_map_files = max_repo_map_files
        self._max_repo_map_chars = max_repo_map_chars
        self._max_related_files = max_related_files
        self._max_related_files_chars = max_related_files_chars
        self._max_code_search_results = max_code_search_results
        self._max_code_search_chars = max_code_search_chars
        self._max_code_search_query_terms = max_code_search_query_terms
        self._repository_ignore_directories = repository_ignore_directories
        self._response_state_store_cls = response_state_store_cls
        self._response_state_file = response_state_file
        self._response_state_ttl_days = response_state_ttl_days
        self._model_endpoint = str(model_endpoint or "responses").strip().lower()
        self._react_enabled = bool(react_enabled)
        self._react_max_draft_iterations = max(1, int(react_max_draft_iterations or 1))
        self._react_max_judge_iterations = max(1, int(react_max_judge_iterations or 1))
        self._react_max_tool_calls_per_agent = max(
            1, int(react_max_tool_calls_per_agent or 1)
        )
        self._react_max_tool_result_chars = max(200, int(react_max_tool_result_chars or 200))
        self._react_allow_judge_tool_retrieval = bool(react_allow_judge_tool_retrieval)
        self._prompts_dir = Path(__file__).resolve().parent.parent / "prompts"

    @staticmethod
    def _state_pr_id(state: ReviewGraphState):
        pr_id = state.get("pr_id")
        return pr_id if pr_id is not None else "unknown"

    def _log_node_start(self, node_name, state: ReviewGraphState):
        logger.info(
            "Review node started. node=%s pr_id=%s",
            node_name,
            self._state_pr_id(state),
        )

    def _log_node_complete(self, node_name, state: ReviewGraphState, **metrics):
        logger.info(
            "Review node completed. node=%s pr_id=%s metrics=%s",
            node_name,
            self._state_pr_id(state),
            metrics,
        )

    def _is_halted(self, node_name, state: ReviewGraphState):
        if not state.get("halt"):
            return False

        logger.info(
            "Review node skipped. node=%s pr_id=%s reason=halted",
            node_name,
            self._state_pr_id(state),
        )
        return True

    @staticmethod
    def _safe_int(value, default):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _clip_tool_result(self, value):
        text = str(value or "")
        if len(text) <= self._react_max_tool_result_chars:
            return text
        return text[: self._react_max_tool_result_chars - 18].rstrip() + "\n... [truncated]"

    def _extract_json_payload_from_model_response(self, response):
        if isinstance(response, dict):
            raw_content = extract_content_from_non_stream_response(response)
        else:
            raw_content = extract_content_from_stream_response(response)

        if not raw_content:
            raise ValueError("Model response content is empty")

        payload = extract_json_from_content(raw_content)
        if not isinstance(payload, dict):
            raise ValueError("Expected JSON object payload")
        return payload

    @staticmethod
    def _normalize_review_payload(review_data):
        if not isinstance(review_data, dict):
            return dict(ReviewGraphAgents._DEFAULT_EMPTY_REVIEW)

        checklist = review_data.get("checklist")
        comments = review_data.get("comments")
        return {
            "verdict": str(review_data.get("verdict") or "CHANGES_SUGGESTED").strip()
            or "CHANGES_SUGGESTED",
            "summary": str(review_data.get("summary") or "No issues identified.").strip()
            or "No issues identified.",
            "checklist": checklist if isinstance(checklist, list) else [],
            "comments": comments if isinstance(comments, list) else [],
        }

    @staticmethod
    def _looks_like_review_payload(review_data):
        if not isinstance(review_data, dict):
            return False
        return any(
            key in review_data for key in ("verdict", "summary", "checklist", "comments")
        )

    @staticmethod
    def _build_tool_trace_block(tool_trace):
        if not tool_trace:
            return "- None"
        lines = []
        for idx, trace in enumerate(tool_trace, start=1):
            lines.append(
                (
                    "- #{idx} tool={tool} status={status} result_chars={chars}\n"
                    "  preview={preview}"
                ).format(
                    idx=idx,
                    tool=trace.get("tool_name", "unknown"),
                    status=trace.get("status", "unknown"),
                    chars=trace.get("result_chars", 0),
                    preview=str(trace.get("result_preview") or "").replace("\n", " "),
                )
            )
        return "\n".join(lines)

    @staticmethod
    def _build_react_control_block(
        *,
        agent_name,
        iteration,
        max_iterations,
        tool_calls,
        max_tool_calls,
        allow_tool_calls,
        tool_trace,
    ):
        tools_policy = (
            "You may request one tool call per response when more evidence is required."
            if allow_tool_calls
            else "Tool calls are disabled for this run; return final_review directly."
        )
        allowed_tools = "\n".join(
            [
                "- get_changed_files",
                "- get_repo_map",
                "- get_related_files",
                "- search_code",
                "- get_repository_context_bundle",
            ]
        )
        return (
            "\n\n## INTERNAL REACT CONTROLLER ({agent_name})\n"
            "- Iteration: {iteration}/{max_iterations}\n"
            "- Tool calls used: {tool_calls}/{max_tool_calls}\n"
            "- Policy: {tools_policy}\n"
            "- Think internally; do not expose chain-of-thought.\n"
            "- Return strict JSON only, using ONE of the following shapes:\n"
            "  1) Tool request:\n"
            "     {{\"action\":\"tool_call\",\"tool_name\":\"<tool>\",\"arguments\":{{...}},\"reason_summary\":\"<one sentence>\"}}\n"
            "  2) Final output:\n"
            "     {{\"action\":\"final_review\",\"review_data\":{{\"verdict\":\"APPROVED|CHANGES_SUGGESTED\",\"summary\":\"...\",\"checklist\":[],\"comments\":[]}}}}\n"
            "- Allowed tools:\n{allowed_tools}\n"
            "- Prior tool trace:\n{tool_trace_block}\n"
        ).format(
            agent_name=agent_name,
            iteration=iteration,
            max_iterations=max_iterations,
            tool_calls=tool_calls,
            max_tool_calls=max_tool_calls,
            tools_policy=tools_policy,
            allowed_tools=allowed_tools,
            tool_trace_block=ReviewGraphAgents._build_tool_trace_block(tool_trace),
        )

    def _parse_agent_action(self, response):
        payload = self._extract_json_payload_from_model_response(response)
        action = str(payload.get("action") or "").strip().lower()

        if action == self._ACTION_TOOL_CALL:
            return {
                "action": self._ACTION_TOOL_CALL,
                "tool_name": str(payload.get("tool_name") or "").strip(),
                "arguments": payload.get("arguments")
                if isinstance(payload.get("arguments"), dict)
                else {},
            }

        if action == self._ACTION_FINAL_REVIEW:
            review_data = payload.get("review_data")
            if not isinstance(review_data, dict):
                review_data = payload
            return {
                "action": self._ACTION_FINAL_REVIEW,
                "review_data": self._normalize_review_payload(review_data),
            }

        if any(key in payload for key in ("verdict", "summary", "comments", "checklist")):
            return {
                "action": self._ACTION_FINAL_REVIEW,
                "review_data": self._normalize_review_payload(payload),
            }

        raise ValueError("Unsupported ReAct agent action payload")

    def _run_tool_call(
        self,
        *,
        tool_name,
        arguments,
        state,
        repository_context_bundle,
    ) -> Tuple[bool, str, Dict[str, str]]:
        changed_file_paths = list(state.get("changed_file_paths") or [])
        repository_path = state.get("repository_path")
        effective_bundle = dict(repository_context_bundle or {})

        if tool_name == "get_changed_files":
            changed_files_context = str(
                state.get("changed_files_context")
                or "No changed files identified from diff metadata."
            )
            effective_bundle["changed_files_context"] = changed_files_context
            return True, self._clip_tool_result(changed_files_context), effective_bundle

        if tool_name == "get_repo_map":
            max_files = max(
                1,
                min(
                    self._safe_int(arguments.get("max_files"), self._max_repo_map_files),
                    self._max_repo_map_files,
                ),
            )
            max_chars = max(
                200,
                min(
                    self._safe_int(arguments.get("max_chars"), self._max_repo_map_chars),
                    self._max_repo_map_chars,
                ),
            )
            repo_map = self._build_repo_map_for_changed_files(
                repository_path,
                changed_file_paths,
                max_files=max_files,
                max_chars=max_chars,
            )
            effective_bundle["repo_map"] = str(repo_map or "")
            return True, self._clip_tool_result(repo_map), effective_bundle

        if tool_name == "get_related_files":
            max_files = max(
                1,
                min(
                    self._safe_int(
                        arguments.get("max_related_files"), self._max_related_files
                    ),
                    self._max_related_files,
                ),
            )
            max_chars = max(
                200,
                min(
                    self._safe_int(
                        arguments.get("max_chars"), self._max_related_files_chars
                    ),
                    self._max_related_files_chars,
                ),
            )
            related_files_context = self._retrieve_related_files_context(
                repository_path,
                changed_file_paths,
                max_related_files=max_files,
                max_chars=max_chars,
            )
            effective_bundle["related_files_context"] = str(related_files_context or "")
            return True, self._clip_tool_result(related_files_context), effective_bundle

        if tool_name == "search_code":
            max_results = max(
                1,
                min(
                    self._safe_int(
                        arguments.get("max_results"), self._max_code_search_results
                    ),
                    self._max_code_search_results,
                ),
            )
            max_chars = max(
                200,
                min(
                    self._safe_int(arguments.get("max_chars"), self._max_code_search_chars),
                    self._max_code_search_chars,
                ),
            )
            max_query_terms = max(
                1,
                min(
                    self._safe_int(
                        arguments.get("max_query_terms"), self._max_code_search_query_terms
                    ),
                    self._max_code_search_query_terms,
                ),
            )
            code_search_context = self._retrieve_bounded_code_search_context(
                repository_path,
                changed_file_paths,
                max_results=max_results,
                max_chars=max_chars,
                max_query_terms=max_query_terms,
                ignore_directories=self._repository_ignore_directories,
            )
            effective_bundle["code_search_context"] = str(code_search_context or "")
            return True, self._clip_tool_result(code_search_context), effective_bundle

        if tool_name == "get_repository_context_bundle":
            composed_bundle = self._compose_repository_context_bundle(
                effective_bundle.get("repo_map"),
                effective_bundle.get("related_files_context"),
                effective_bundle.get("code_search_context"),
            )
            effective_bundle.update(composed_bundle)
            return (
                True,
                self._clip_tool_result(
                    "Repository context bundle assembled with available sections."
                ),
                effective_bundle,
            )

        return (
            False,
            "Unknown tool requested. Allowed tools: get_changed_files, get_repo_map, get_related_files, search_code, get_repository_context_bundle.",
            effective_bundle,
        )

    def _run_react_loop(
        self,
        *,
        node_name,
        state,
        model_name,
        system_prompt,
        base_user_prompt,
        max_iterations,
        allow_tool_calls,
        repository_context_bundle,
        persist_response_state,
    ):
        pr_id = state.get("pr_id")
        run_stream_response = bool(state.get("stream_response"))
        model_endpoint = str(state.get("model_endpoint") or self._model_endpoint).strip().lower()

        response_state_store = None
        state_key = state.get("state_key")
        previous_response_id = None
        store_response = False
        if (
            persist_response_state
            and model_endpoint == "responses"
            and state_key
        ):
            response_state_store = self._response_state_store_cls(
                self._response_state_file,
                ttl_days=self._response_state_ttl_days,
            )
            previous_response_id = response_state_store.get_previous_response_id(state_key)
            store_response = previous_response_id is None

        tool_trace = []
        tool_calls = 0
        repository_bundle = dict(repository_context_bundle or {})
        latest_response = None

        for iteration in range(1, max_iterations + 1):
            react_block = self._build_react_control_block(
                agent_name=node_name,
                iteration=iteration,
                max_iterations=max_iterations,
                tool_calls=tool_calls,
                max_tool_calls=self._react_max_tool_calls_per_agent,
                allow_tool_calls=allow_tool_calls,
                tool_trace=tool_trace,
            )
            user_prompt = base_user_prompt + react_block

            model_response = self._get_review_model_completion(
                model_name,
                system_prompt,
                user_prompt,
                pr_id=pr_id,
                vcs_config=state.get("vcs_config"),
                previous_response_id=previous_response_id,
                store_response=store_response,
                model_endpoint=model_endpoint,
                stream_response=run_stream_response,
            )
            latest_response = model_response

            if model_endpoint == "responses" and response_state_store and state_key:
                latest_response_id = self._extract_previous_response_id(model_response)
                if latest_response_id:
                    response_state_store.set_previous_response_id(state_key, latest_response_id)
                    previous_response_id = latest_response_id
                    store_response = False

            try:
                action_payload = self._parse_agent_action(model_response)
            except ValueError:
                try:
                    fallback_payload = self._parse_review_payload(model_response)
                    if self._looks_like_review_payload(fallback_payload):
                        fallback_review = self._normalize_review_payload(fallback_payload)
                        return (
                            fallback_review,
                            iteration,
                            tool_calls,
                            tool_trace,
                            repository_bundle,
                        )
                except ValueError:
                    logger.warning(
                        "ReAct agent response was neither action JSON nor review payload. node=%s iteration=%s",
                        node_name,
                        iteration,
                    )
                    continue

            action = action_payload.get("action")
            if action == self._ACTION_FINAL_REVIEW:
                return (
                    self._normalize_review_payload(action_payload.get("review_data")),
                    iteration,
                    tool_calls,
                    tool_trace,
                    repository_bundle,
                )

            if action == self._ACTION_TOOL_CALL:
                if not allow_tool_calls:
                    tool_trace.append(
                        {
                            "tool_name": action_payload.get("tool_name"),
                            "status": "rejected",
                            "result_chars": 0,
                            "reason": "tool-calls-disabled",
                        }
                    )
                    continue

                if tool_calls >= self._react_max_tool_calls_per_agent:
                    tool_trace.append(
                        {
                            "tool_name": action_payload.get("tool_name"),
                            "status": "rejected",
                            "result_chars": 0,
                            "reason": "max-tool-calls-reached",
                        }
                    )
                    continue

                success, tool_result, repository_bundle = self._run_tool_call(
                    tool_name=action_payload.get("tool_name"),
                    arguments=action_payload.get("arguments") or {},
                    state=state,
                    repository_context_bundle=repository_bundle,
                )
                tool_calls += 1
                tool_trace.append(
                    {
                        "tool_name": action_payload.get("tool_name"),
                        "status": "ok" if success else "error",
                        "result_chars": len(str(tool_result or "")),
                        "result_preview": self._clip_tool_result(tool_result)[:800],
                    }
                )
                continue

        if latest_response is not None:
            try:
                latest_payload = self._parse_review_payload(latest_response)
                if self._looks_like_review_payload(latest_payload):
                    return (
                        self._normalize_review_payload(latest_payload),
                        max_iterations,
                        tool_calls,
                        tool_trace,
                        repository_bundle,
                    )
            except ValueError:
                logger.warning(
                    "ReAct loop exhausted and fallback parse failed. node=%s pr_id=%s",
                    node_name,
                    self._state_pr_id(state),
                )

        return (
            dict(self._DEFAULT_EMPTY_REVIEW),
            max_iterations,
            tool_calls,
            tool_trace,
            repository_bundle,
        )

    def draft_reviewer(self, state: ReviewGraphState) -> ReviewGraphState:
        node_name = "draft_reviewer"
        if self._is_halted(node_name, state):
            return cast(ReviewGraphState, {})

        self._log_node_start(node_name, state)

        pr_id = state.get("pr_id")
        run_draft_model = state.get("draft_model")
        model_endpoint = str(state.get("model_endpoint") or self._model_endpoint).strip().lower()

        logger.info(
            "Requesting draft review model response. model=%s pr_id=%s react=%s",
            run_draft_model,
            pr_id,
            self._react_enabled,
        )

        if not self._react_enabled:
            run_stream_response = bool(state.get("stream_response"))
            previous_response_id = None
            store_response = False
            response_state_store = None
            state_key = state.get("state_key")

            if model_endpoint == "responses" and state_key:
                response_state_store = self._response_state_store_cls(
                    self._response_state_file,
                    ttl_days=self._response_state_ttl_days,
                )
                previous_response_id = response_state_store.get_previous_response_id(state_key)
                store_response = previous_response_id is None

            draft_response = self._get_review_model_completion(
                run_draft_model,
                state.get("draft_sys_p", ""),
                state.get("draft_user_p", ""),
                pr_id=pr_id,
                vcs_config=state.get("vcs_config"),
                previous_response_id=previous_response_id,
                store_response=store_response,
                model_endpoint=model_endpoint,
                stream_response=run_stream_response,
            )

            if model_endpoint == "responses" and response_state_store and state_key:
                latest_response_id = self._extract_previous_response_id(draft_response)
                if latest_response_id:
                    response_state_store.set_previous_response_id(state_key, latest_response_id)

            try:
                draft_review_data = self._parse_review_payload(draft_response)
            except ValueError:
                logger.exception("Unable to parse draft review payload")
                self._log_node_complete(node_name, state, status="parse-error")
                return cast(ReviewGraphState, {"halt": True})

            draft_comments = draft_review_data.get("comments")
            comment_count = len(draft_comments) if isinstance(draft_comments, list) else 0
            self._log_node_complete(
                node_name,
                state,
                model=run_draft_model,
                model_endpoint=model_endpoint,
                draft_comments=comment_count,
            )
            return cast(ReviewGraphState, {"draft_review_data": draft_review_data})

        initial_bundle = {
            "changed_files_context": str(
                state.get("changed_files_context")
                or "No changed-file context available."
            )
        }
        draft_review_data, draft_iteration_count, draft_tool_calls, draft_tool_trace, draft_bundle = (
            self._run_react_loop(
                node_name=node_name,
                state=state,
                model_name=run_draft_model,
                system_prompt=state.get("draft_sys_p", ""),
                base_user_prompt=state.get("draft_user_p", ""),
                max_iterations=self._react_max_draft_iterations,
                allow_tool_calls=True,
                repository_context_bundle=initial_bundle,
                persist_response_state=True,
            )
        )

        draft_comments = draft_review_data.get("comments")
        comment_count = len(draft_comments) if isinstance(draft_comments, list) else 0
        self._log_node_complete(
            node_name,
            state,
            model=run_draft_model,
            model_endpoint=model_endpoint,
            draft_comments=comment_count,
            draft_iteration_count=draft_iteration_count,
            draft_tool_calls=draft_tool_calls,
        )
        return cast(
            ReviewGraphState,
            {
                "draft_review_data": draft_review_data,
                "draft_iteration_count": draft_iteration_count,
                "draft_tool_calls": draft_tool_calls,
                "draft_tool_trace": draft_tool_trace,
                "draft_repository_context_bundle": draft_bundle,
            },
        )

    def evidence_judge(self, state: ReviewGraphState) -> ReviewGraphState:
        node_name = "evidence_judge"
        if self._is_halted(node_name, state):
            return cast(ReviewGraphState, {})

        self._log_node_start(node_name, state)

        pr_id = state.get("pr_id")
        run_judge_model = state.get("judge_model")
        run_stream_response = bool(state.get("stream_response"))
        model_endpoint = str(state.get("model_endpoint") or self._model_endpoint).strip().lower()

        with open(self._prompts_dir / "judge_review_system_prompt.md", "r", encoding="utf-8") as f:
            judge_sys_p = f.read().replace("{{TEAM_NAME}}", str(state.get("team_name") or ""))

        draft_payload = state.get("normalized_draft_review_data")
        if not isinstance(draft_payload, dict) or not draft_payload:
            draft_payload = state.get("draft_review_data")
        if not isinstance(draft_payload, dict):
            draft_payload = {}

        initial_judge_bundle = {
            "changed_files_context": str(
                state.get("changed_files_context")
                or "No changed-file context available."
            )
        }

        with open(self._prompts_dir / "judge_review_user_prompt.md", "r", encoding="utf-8") as f:
            judge_user_p = self._build_judge_prompt_user_content(
                prompt_template=f.read(),
                pr_title=state.get("pr_title", ""),
                pr_description=state.get("pr_description", ""),
                safe_diff=state.get("safe_diff", ""),
                existing_feedback=state.get("existing_feedback", ""),
                changed_files_context=initial_judge_bundle["changed_files_context"],
                draft_review_data=draft_payload,
                repository_context_bundle=initial_judge_bundle,
            )

        logger.info(
            "Requesting judge review model response. model=%s pr_id=%s react=%s",
            run_judge_model,
            pr_id,
            self._react_enabled,
        )

        if not self._react_enabled:
            judge_response = self._get_review_model_completion(
                run_judge_model,
                judge_sys_p,
                judge_user_p,
                pr_id=pr_id,
                vcs_config=state.get("vcs_config"),
                previous_response_id=None,
                store_response=False,
                model_endpoint=model_endpoint,
                stream_response=run_stream_response,
            )

            try:
                review_data = self._parse_review_payload(judge_response)
            except ValueError:
                logger.exception("Unable to parse judge review payload")
                self._log_node_complete(node_name, state, status="parse-error")
                return cast(ReviewGraphState, {"halt": True})

            final_comments = review_data.get("comments")
            final_comment_count = (
                len(final_comments) if isinstance(final_comments, list) else 0
            )
            self._log_node_complete(
                node_name,
                state,
                model=run_judge_model,
                model_endpoint=model_endpoint,
                final_comments=final_comment_count,
            )
            return cast(ReviewGraphState, {"review_data": review_data})

        review_data, judge_iteration_count, judge_tool_calls, judge_tool_trace, judge_bundle = (
            self._run_react_loop(
                node_name=node_name,
                state=state,
                model_name=run_judge_model,
                system_prompt=judge_sys_p,
                base_user_prompt=judge_user_p,
                max_iterations=self._react_max_judge_iterations,
                allow_tool_calls=self._react_allow_judge_tool_retrieval,
                repository_context_bundle=initial_judge_bundle,
                persist_response_state=False,
            )
        )

        final_comments = review_data.get("comments")
        final_comment_count = len(final_comments) if isinstance(final_comments, list) else 0
        self._log_node_complete(
            node_name,
            state,
            model=run_judge_model,
            model_endpoint=model_endpoint,
            final_comments=final_comment_count,
            judge_iteration_count=judge_iteration_count,
            judge_tool_calls=judge_tool_calls,
        )
        return cast(
            ReviewGraphState,
            {
                "review_data": review_data,
                "judge_iteration_count": judge_iteration_count,
                "judge_tool_calls": judge_tool_calls,
                "judge_tool_trace": judge_tool_trace,
                "judge_repository_context_bundle": judge_bundle,
            },
        )