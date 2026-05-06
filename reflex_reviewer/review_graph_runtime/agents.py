import logging
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Tuple, cast

from reflex_reviewer.llm.response_handler import (
    extract_content_from_non_stream_response,
    extract_content_from_stream_response,
    extract_json_from_content,
)
from reflex_reviewer.review_output_contracts import (
    NON_REACT_OUTPUT_CONTRACT,
    REACT_OUTPUT_CONTRACT,
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
    _REPLY_SENTIMENT_REJECTED = "REJECTED"
    _REPLY_SENTIMENT_NOT_REJECTED = "NOT_REJECTED"
    _REPLY_SENTIMENT_UNSURE = "UNSURE"
    _DEFAULT_OUTSTANDING_SUMMARY = (
        "Prior bot feedback still appears applicable in the current diff. "
        "Please address the existing inline comments before considering this ready."
    )
    _COMMENT_TOKEN_STOPWORDS = {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "in",
        "into",
        "is",
        "it",
        "of",
        "on",
        "or",
        "that",
        "the",
        "this",
        "to",
        "was",
        "with",
        "should",
        "can",
        "could",
        "would",
        "please",
        "consider",
        "change",
        "changes",
        "update",
        "use",
        "value",
    }
    _MIN_DUPLICATE_TOKEN_INTERSECTION = 2
    _MIN_DUPLICATE_TOKEN_OVERLAP = 0.75

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
        resolve_comment_severity,
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
        react_require_initial_repository_tool,
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
        self._resolve_comment_severity = resolve_comment_severity
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
        self._react_require_initial_repository_tool = bool(
            react_require_initial_repository_tool
        )
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
    def _build_tool_observations_block(tool_trace):
        if not tool_trace:
            return "- None"

        lines = []
        for idx, trace in enumerate(tool_trace, start=1):
            preview = str(trace.get("result_preview") or "").strip()
            if not preview:
                preview = "No tool output captured."
            lines.append(
                (
                    "- Observation #{idx} ({tool}, {status}):\n"
                    "  {preview}"
                ).format(
                    idx=idx,
                    tool=trace.get("tool_name", "unknown"),
                    status=trace.get("status", "unknown"),
                    preview=preview.replace("\n", "\n  "),
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
        require_repository_tool_before_final,
        tool_trace,
        tool_observations,
    ):
        tools_policy = (
            "You may request one tool call per response when more evidence is required."
            if allow_tool_calls
            else "Tool calls are disabled for this run; return final_review directly."
        )
        repository_tool_policy = (
            "Before returning final_review, you must make at least one repository-evidence tool call (get_repo_map/get_related_files/search_code/get_repository_context_bundle) when those sections are deferred/unavailable in the prompt."
            if require_repository_tool_before_final and allow_tool_calls
            else "Repository-evidence tool calls are optional when evidence is already sufficient."
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
            "- Repository evidence policy: {repository_tool_policy}\n"
            "- Think internally; do not expose chain-of-thought.\n"
            "- Return strict JSON only, using ONE of the following shapes:\n"
            "  1) Tool request:\n"
            "     {{\"action\":\"tool_call\",\"tool_name\":\"<tool>\",\"arguments\":{{...}},\"reason_summary\":\"<one sentence>\"}}\n"
            "  2) Final output:\n"
            "     {{\"action\":\"final_review\",\"review_data\":{{\"verdict\":\"APPROVED|CHANGES_SUGGESTED\",\"summary\":\"...\",\"checklist\":[],\"comments\":[]}}}}\n"
            "- During ReAct control, do not output the bare review schema directly; always wrap final output in action=final_review.\n"
            "- Allowed tools:\n{allowed_tools}\n"
            "- Prior tool trace:\n{tool_trace_block}\n"
            "- Tool observations:\n{tool_observations}\n"
        ).format(
            agent_name=agent_name,
            iteration=iteration,
            max_iterations=max_iterations,
            tool_calls=tool_calls,
            max_tool_calls=max_tool_calls,
            tools_policy=tools_policy,
            repository_tool_policy=repository_tool_policy,
            allowed_tools=allowed_tools,
            tool_trace_block=ReviewGraphAgents._build_tool_trace_block(tool_trace),
            tool_observations=tool_observations,
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

    @staticmethod
    def _normalize_repo_path(file_path):
        normalized = str(file_path or "").strip().replace("\\", "/")
        normalized = re.sub(r"^(?:\./)+", "", normalized)
        normalized = re.sub(r"^(?:a|b)/", "", normalized)
        normalized = normalized.lstrip("/")
        return normalized.lower()

    @classmethod
    def _normalize_comment_text_for_fingerprint(cls, text):
        normalized = str(text or "").lower()
        normalized = normalized.replace("->", " ").replace("=>", " ")
        normalized = (
            normalized.replace("::", " ")
            .replace("/", " ")
            .replace("\\", " ")
            .replace("_", " ")
            .replace("-", " ")
        )
        normalized = re.sub(r"`{1,3}", " ", normalized)
        normalized = re.sub(r"[^a-z0-9]+", " ", normalized)
        normalized = re.sub(r"\s+", " ", normalized).strip()
        return normalized

    @classmethod
    def _token_set_for_similarity(cls, text):
        normalized_text = cls._normalize_comment_text_for_fingerprint(text)
        if not normalized_text:
            return set()
        return {
            token
            for token in normalized_text.split()
            if len(token) >= 3 and token not in cls._COMMENT_TOKEN_STOPWORDS
        }

    @classmethod
    def _comments_are_near_duplicates(cls, left_text, right_text):
        left_normalized = cls._normalize_comment_text_for_fingerprint(left_text)
        right_normalized = cls._normalize_comment_text_for_fingerprint(right_text)
        if not left_normalized or not right_normalized:
            return False
        if left_normalized == right_normalized:
            return True

        left_tokens = cls._token_set_for_similarity(left_normalized)
        right_tokens = cls._token_set_for_similarity(right_normalized)
        if not left_tokens or not right_tokens:
            return False

        intersection_count = len(left_tokens & right_tokens)
        if intersection_count < cls._MIN_DUPLICATE_TOKEN_INTERSECTION:
            return False

        min_token_count = min(len(left_tokens), len(right_tokens))
        if min_token_count <= 0:
            return False

        overlap_ratio = intersection_count / float(min_token_count)
        return overlap_ratio >= cls._MIN_DUPLICATE_TOKEN_OVERLAP

    @staticmethod
    def _normalize_reply_sentiment(sentiment):
        normalized = str(sentiment or "").strip().upper().replace(" ", "_")
        if normalized in {
            ReviewGraphAgents._REPLY_SENTIMENT_REJECTED,
            ReviewGraphAgents._REPLY_SENTIMENT_NOT_REJECTED,
            ReviewGraphAgents._REPLY_SENTIMENT_UNSURE,
        }:
            return normalized
        return ReviewGraphAgents._REPLY_SENTIMENT_UNSURE

    def _resolve_comment_severity_with_context(self, severity, file_path=None, comment_text=None):
        try:
            return self._resolve_comment_severity(severity, file_path, comment_text)
        except TypeError:
            return self._resolve_comment_severity(severity, file_path)

    @staticmethod
    def _sanitize_reply_text(value, max_chars=400):
        text = re.sub(r"\s+", " ", str(value or "")).strip()
        if len(text) <= max_chars:
            return text
        return text[: max_chars - 16].rstrip() + "... [truncated]"

    def _classify_existing_bot_comment_reply_sentiments(self, state: ReviewGraphState, threads):
        if not threads:
            return {}

        run_judge_model = str(state.get("judge_model") or "").strip()
        if not run_judge_model:
            return {}

        model_endpoint = str(state.get("model_endpoint") or self._model_endpoint).strip().lower()
        run_stream_response = bool(state.get("stream_response"))

        prompt_threads = []
        for thread in threads:
            if not isinstance(thread, dict):
                continue
            comment_id = str(thread.get("comment_id") or "").strip()
            if not comment_id:
                continue
            prompt_threads.append(
                {
                    "comment_id": comment_id,
                    "path": str(thread.get("path") or ""),
                    "line": thread.get("line"),
                    "bot_comment": self._sanitize_reply_text(thread.get("text"), max_chars=300),
                    "replies": [
                        self._sanitize_reply_text(reply, max_chars=240)
                        for reply in (thread.get("reply_texts") or [])
                        if str(reply or "").strip()
                    ],
                }
            )

        if not prompt_threads:
            return {}

        classifier_sys_p = (
            "You classify whether human replies reject previous bot review comments. "
            "Return strict JSON only."
        )
        classifier_user_p = (
            "Classify each thread reply sentiment relative to the bot comment.\n"
            "- REJECTED: human replies indicate bot comment is wrong/not applicable/false positive/rejected.\n"
            "- NOT_REJECTED: replies do not reject the bot comment.\n"
            "- UNSURE: ambiguous.\n\n"
            f"Threads:\n{json.dumps(prompt_threads, ensure_ascii=False)}\n\n"
            "Return JSON in this exact shape:\n"
            '{"results":[{"comment_id":"<id>","sentiment":"REJECTED|NOT_REJECTED|UNSURE"}]}'
        )

        try:
            response = self._get_review_model_completion(
                run_judge_model,
                classifier_sys_p,
                classifier_user_p,
                pr_id=state.get("pr_id"),
                vcs_config=state.get("vcs_config"),
                previous_response_id=None,
                store_response=False,
                model_endpoint=model_endpoint,
                stream_response=run_stream_response,
            )
            payload = self._extract_json_payload_from_model_response(response)
            results = payload.get("results")
            if not isinstance(results, list):
                return {}

            sentiment_by_comment_id = {}
            for row in results:
                if not isinstance(row, dict):
                    continue
                comment_id = str(row.get("comment_id") or "").strip()
                if not comment_id:
                    continue
                sentiment_by_comment_id[comment_id] = self._normalize_reply_sentiment(
                    row.get("sentiment")
                )
            return sentiment_by_comment_id
        except Exception:
            logger.warning(
                "Policy guard reply-sentiment classification failed; defaulting to UNSURE for replied threads.",
                exc_info=True,
            )
            return {}

    @staticmethod
    def _format_outstanding_checklist_item(existing_comment):
        path = str(existing_comment.get("path") or "").strip()
        if path.lower() == "unknown-file":
            path = ""

        line = existing_comment.get("line")
        try:
            line_value = int(line)
        except (TypeError, ValueError):
            line_value = 0

        if path and line_value > 0:
            location = f"{path}:{line_value}"
        elif path:
            location = path
        else:
            location = ""
        text = re.sub(r"\s+", " ", str(existing_comment.get("text") or "")).strip()
        if len(text) > 140:
            text = text[:137].rstrip() + "..."
        if location:
            return f"Address existing bot comment: {location} — {text}"
        return f"Address existing bot comment — {text}"

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

        deferred_repo_sections = {
            section_name
            for section_name in ("repo_map", "related_files_context", "code_search_context")
            if "deferred" in str(repository_bundle.get(section_name, "")).strip().lower()
        }
        initial_repository_tool_required = bool(
            allow_tool_calls
            and self._react_require_initial_repository_tool
            and node_name in {"draft_reviewer", "evidence_judge"}
            and deferred_repo_sections
        )
        repository_tool_names = {
            "get_repo_map",
            "get_related_files",
            "search_code",
            "get_repository_context_bundle",
        }
        repository_tool_called = False

        for iteration in range(1, max_iterations + 1):
            react_block = self._build_react_control_block(
                agent_name=node_name,
                iteration=iteration,
                max_iterations=max_iterations,
                tool_calls=tool_calls,
                max_tool_calls=self._react_max_tool_calls_per_agent,
                allow_tool_calls=allow_tool_calls,
                require_repository_tool_before_final=(
                    initial_repository_tool_required and not repository_tool_called
                ),
                tool_trace=tool_trace,
                tool_observations=self._build_tool_observations_block(tool_trace),
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
                if initial_repository_tool_required and not repository_tool_called:
                    tool_trace.append(
                        {
                            "tool_name": "_policy",
                            "status": "rejected",
                            "result_chars": 0,
                            "result_preview": "Final review rejected until at least one repository evidence tool is called.",
                            "reason": "initial-repository-tool-required",
                        }
                    )
                    continue
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
                if action_payload.get("tool_name") in repository_tool_names:
                    repository_tool_called = True
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
            ),
            "repo_map": str(state.get("repo_map") or "No repository map context available."),
            "related_files_context": str(
                state.get("related_files_context")
                or "No related-file context available."
            ),
            "code_search_context": str(
                state.get("code_search_context") or "No code-search context available."
            ),
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
        output_contract = (
            REACT_OUTPUT_CONTRACT if self._react_enabled else NON_REACT_OUTPUT_CONTRACT
        )

        with open(self._prompts_dir / "judge_review_system_prompt.md", "r", encoding="utf-8") as f:
            judge_sys_p = (
                f.read()
                .replace("{{TEAM_NAME}}", str(state.get("team_name") or ""))
                .replace("{{OUTPUT_CONTRACT}}", output_contract)
            )

        draft_payload = state.get("normalized_draft_review_data")
        if not isinstance(draft_payload, dict) or not draft_payload:
            draft_payload = state.get("draft_review_data")
        if not isinstance(draft_payload, dict):
            draft_payload = {}

        initial_judge_bundle = {
            "changed_files_context": str(
                state.get("changed_files_context")
                or "No changed-file context available."
            ),
            "repo_map": str(state.get("repo_map") or "No repository map context available."),
            "related_files_context": str(
                state.get("related_files_context")
                or "No related-file context available."
            ),
            "code_search_context": str(
                state.get("code_search_context") or "No code-search context available."
            ),
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
                output_contract=output_contract,
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

    def policy_guard_agent(self, state: ReviewGraphState) -> ReviewGraphState:
        node_name = "policy_guard_agent"
        if self._is_halted(node_name, state):
            return cast(ReviewGraphState, {})

        self._log_node_start(node_name, state)

        existing_comments_by_anchor = {}
        existing_indexed_comments = []
        for existing_comment in state.get("existing_bot_inline_comments", []):
            if not isinstance(existing_comment, dict):
                continue

            normalized_path = self._normalize_repo_path(existing_comment.get("path"))
            normalized_line = self._safe_int(existing_comment.get("line"), default=0)
            existing_text = str(existing_comment.get("text") or "").strip()
            if not existing_text:
                continue

            existing_indexed_comments.append(existing_comment)

            if not normalized_path or normalized_line <= 0:
                continue

            anchor_key = (normalized_path, normalized_line)
            existing_comments_by_anchor.setdefault(anchor_key, []).append(existing_comment)

        guarded_comments = []
        accepted_comments_by_anchor = {}
        duplicate_suppressed_count = 0
        existing_duplicate_suppressed_count = 0
        pending_existing_duplicate_matches = []

        for comment in state.get("resolved_comments", []):
            if not isinstance(comment, dict):
                continue

            comment_path = self._normalize_repo_path(comment.get("path"))
            comment_line = self._safe_int(comment.get("line"), default=0)
            comment_text = str(comment.get("text") or "").strip()

            anchor_key = None
            if comment_path and comment_line > 0 and comment_text:
                anchor_key = (comment_path, comment_line)

            matched_existing_comment = None
            suppressed_due_to_current_batch = False
            if anchor_key:
                existing_anchor_comments = existing_comments_by_anchor.get(anchor_key, [])
                accepted_anchor_texts = accepted_comments_by_anchor.get(anchor_key, [])

                for existing_comment in existing_anchor_comments:
                    if ReviewGraphAgents._comments_are_near_duplicates(
                        comment_text,
                        str(existing_comment.get("text") or ""),
                    ):
                        matched_existing_comment = existing_comment
                        break

                suppressed_due_to_current_batch = any(
                    ReviewGraphAgents._comments_are_near_duplicates(comment_text, accepted_text)
                    for accepted_text in accepted_anchor_texts
                )

            if matched_existing_comment is not None or suppressed_due_to_current_batch:
                duplicate_suppressed_count += 1
                if matched_existing_comment is not None:
                    existing_duplicate_suppressed_count += 1
                    pending_existing_duplicate_matches.append(
                        {
                            "existing_comment": matched_existing_comment,
                            "candidate_text": comment_text,
                        }
                    )
                continue

            guarded_comments.append(
                {
                    **comment,
                    "severity": self._resolve_comment_severity_with_context(
                        comment.get("severity"),
                        comment.get("path"),
                        comment.get("text"),
                    ),
                }
            )
            if anchor_key:
                accepted_comments_by_anchor.setdefault(anchor_key, []).append(comment_text)

        normalized_verdict = str(state.get("verdict") or "CHANGES_SUGGESTED").strip().upper()
        normalized_verdict = normalized_verdict.replace(" ", "_")
        evaluate_all_existing_when_approved = (
            normalized_verdict == "APPROVED" and not guarded_comments
        )

        pending_existing_matches_for_outstanding = list(pending_existing_duplicate_matches)
        if evaluate_all_existing_when_approved:
            for existing_comment in existing_indexed_comments:
                pending_existing_matches_for_outstanding.append(
                    {
                        "existing_comment": existing_comment,
                        "candidate_text": "",
                    }
                )

        deduped_pending_matches = []
        seen_pending_keys = set()
        for pending in pending_existing_matches_for_outstanding:
            existing_comment = pending.get("existing_comment")
            if not isinstance(existing_comment, dict):
                continue
            dedupe_key = (
                str(existing_comment.get("comment_id") or "").strip(),
                self._normalize_repo_path(existing_comment.get("path")),
                self._safe_int(existing_comment.get("line"), default=0),
                self._normalize_comment_text_for_fingerprint(
                    str(existing_comment.get("text") or "")
                ),
            )
            if dedupe_key in seen_pending_keys:
                continue
            seen_pending_keys.add(dedupe_key)
            deduped_pending_matches.append(pending)

        threads_for_llm = []
        thread_ids_for_llm = set()
        for pending in deduped_pending_matches:
            existing_comment = pending.get("existing_comment")
            if not isinstance(existing_comment, dict):
                continue
            comment_id = str(existing_comment.get("comment_id") or "").strip()
            reply_texts = [
                str(reply or "").strip()
                for reply in (existing_comment.get("reply_texts") or [])
                if str(reply or "").strip()
            ]
            if not comment_id or not reply_texts:
                continue
            if comment_id in thread_ids_for_llm:
                continue
            thread_ids_for_llm.add(comment_id)
            threads_for_llm.append(existing_comment)

        reply_sentiment_by_comment_id = self._classify_existing_bot_comment_reply_sentiments(
            state,
            threads_for_llm,
        )

        outstanding_existing_bot_comments = []
        outstanding_seen = set()
        for pending in deduped_pending_matches:
            existing_comment = pending.get("existing_comment")
            if not isinstance(existing_comment, dict):
                continue

            comment_id = str(existing_comment.get("comment_id") or "").strip()
            reply_texts = [
                str(reply or "").strip()
                for reply in (existing_comment.get("reply_texts") or [])
                if str(reply or "").strip()
            ]
            sentiment = (
                reply_sentiment_by_comment_id.get(comment_id, self._REPLY_SENTIMENT_UNSURE)
                if reply_texts
                else self._REPLY_SENTIMENT_NOT_REJECTED
            )
            is_rejected = sentiment == self._REPLY_SENTIMENT_REJECTED
            if is_rejected:
                continue

            dedupe_key = (
                self._normalize_repo_path(existing_comment.get("path")),
                self._safe_int(existing_comment.get("line"), default=0),
                re.sub(r"\s+", " ", str(existing_comment.get("text") or "")).strip().lower(),
            )
            if dedupe_key in outstanding_seen:
                continue
            outstanding_seen.add(dedupe_key)
            outstanding_existing_bot_comments.append(
                {
                    "comment_id": comment_id,
                    "path": existing_comment.get("path"),
                    "line": existing_comment.get("line"),
                    "severity": existing_comment.get("severity"),
                    "text": existing_comment.get("text"),
                    "sentiment": sentiment,
                }
            )

        skipped_inline_count = self._safe_int(
            state.get("skipped_inline_count"),
            default=0,
        )
        skipped_inline_count += duplicate_suppressed_count

        should_force_changes_suggested = bool(guarded_comments) or bool(
            outstanding_existing_bot_comments
        )
        verdict = (
            "CHANGES_SUGGESTED"
            if should_force_changes_suggested
            else str(state.get("verdict") or "CHANGES_SUGGESTED")
        )

        summary = str(state.get("summary") or "No issues identified.")
        raw_checklist = state.get("checklist")
        checklist = list(raw_checklist) if isinstance(raw_checklist, list) else []
        if outstanding_existing_bot_comments:
            summary = self._DEFAULT_OUTSTANDING_SUMMARY
            outstanding_items = [
                self._format_outstanding_checklist_item(comment)
                for comment in outstanding_existing_bot_comments
            ]
            existing_items = [str(item).strip() for item in checklist if str(item).strip()]
            checklist = outstanding_items + [
                item for item in existing_items if item not in outstanding_items
            ]

        result: ReviewGraphState = {
            "resolved_comments": guarded_comments,
            "skipped_inline_count": skipped_inline_count,
            "existing_duplicate_suppressed_count": existing_duplicate_suppressed_count,
            "existing_bot_comment_reply_sentiment_by_id": reply_sentiment_by_comment_id,
            "outstanding_existing_bot_comments": outstanding_existing_bot_comments,
            "verdict": verdict,
            "summary": summary,
            "checklist": checklist,
        }
        self._log_node_complete(
            node_name,
            state,
            input_comments=len(state.get("resolved_comments", [])),
            guarded_comments=len(guarded_comments),
            duplicate_suppressed=duplicate_suppressed_count,
            existing_duplicate_suppressed=existing_duplicate_suppressed_count,
            outstanding_existing_comments=len(outstanding_existing_bot_comments),
            skipped_inline_count=skipped_inline_count,
            verdict=verdict,
        )
        return result