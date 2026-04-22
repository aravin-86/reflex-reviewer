import logging
from pathlib import Path
from typing import cast

from .state import ReviewGraphState

logger = logging.getLogger(__name__)


class ReviewGraphAgents:
    def __init__(
        self,
        *,
        get_review_model_completion,
        parse_review_payload,
        extract_previous_response_id,
        build_judge_prompt_user_content,
        response_state_store_cls,
        response_state_file,
        response_state_ttl_days,
        model_endpoint,
    ):
        self._get_review_model_completion = get_review_model_completion
        self._parse_review_payload = parse_review_payload
        self._extract_previous_response_id = extract_previous_response_id
        self._build_judge_prompt_user_content = build_judge_prompt_user_content
        self._response_state_store_cls = response_state_store_cls
        self._response_state_file = response_state_file
        self._response_state_ttl_days = response_state_ttl_days
        self._model_endpoint = str(model_endpoint or "responses").strip().lower()
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

    def draft_reviewer(self, state: ReviewGraphState) -> ReviewGraphState:
        node_name = "draft_reviewer"
        if self._is_halted(node_name, state):
            return cast(ReviewGraphState, {})

        self._log_node_start(node_name, state)

        pr_id = state.get("pr_id")
        run_draft_model = state.get("draft_model")
        run_stream_response = bool(state.get("stream_response"))
        model_endpoint = str(state.get("model_endpoint") or self._model_endpoint).strip().lower()

        logger.info(
            "Requesting draft review model response. model=%s pr_id=%s",
            run_draft_model,
            pr_id,
        )

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
            elif run_stream_response and not isinstance(draft_response, dict):
                logger.info(
                    "Skipping response-id persistence for streamed responses API payload. state_key=%s",
                    state_key,
                )
            else:
                logger.warning(
                    "Responses API payload did not include a response id. state_key=%s",
                    state_key,
                )

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

        with open(self._prompts_dir / "judge_review_user_prompt.md", "r", encoding="utf-8") as f:
            judge_user_p = self._build_judge_prompt_user_content(
                prompt_template=f.read(),
                pr_title=state.get("pr_title", ""),
                pr_description=state.get("pr_description", ""),
                safe_diff=state.get("safe_diff", ""),
                existing_feedback=state.get("existing_feedback", ""),
                draft_review_data=draft_payload,
                repository_context_bundle=state.get("repository_context_bundle", {}),
            )

        logger.info(
            "Requesting judge review model response. model=%s pr_id=%s",
            run_judge_model,
            pr_id,
        )
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
        final_comment_count = len(final_comments) if isinstance(final_comments, list) else 0
        self._log_node_complete(
            node_name,
            state,
            model=run_judge_model,
            model_endpoint=model_endpoint,
            final_comments=final_comment_count,
        )
        return cast(ReviewGraphState, {"review_data": review_data})
