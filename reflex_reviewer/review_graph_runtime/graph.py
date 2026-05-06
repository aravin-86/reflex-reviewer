import logging

try:
    from langgraph.graph import (  # type: ignore[reportMissingImports]
        END,
        START,
        StateGraph,
    )

    LANGGRAPH_AVAILABLE = True
except ModuleNotFoundError:  # pragma: no cover - exercised only in missing-dependency envs
    END = "__end__"
    START = "__start__"
    StateGraph = None
    LANGGRAPH_AVAILABLE = False

from .agents import ReviewGraphAgents
from .nodes import ReviewGraphNodes
from .state import ReviewGraphState

logger = logging.getLogger(__name__)


def _resolve_effective_react_settings(*, react_enabled, repository_path, resolve_repository_path):
    """Resolve repository path once and disable ReAct when repository context is unavailable."""
    resolved_repository_path = resolve_repository_path(repository_path)
    has_repository_context = bool(str(resolved_repository_path or "").strip())
    effective_react_enabled = bool(react_enabled) and has_repository_context
    return effective_react_enabled, resolved_repository_path


class _FallbackCompiledReviewGraph:
    def __init__(self, nodes_in_order):
        self._nodes_in_order = nodes_in_order

    def invoke(self, initial_state):
        state = dict(initial_state or {})
        pr_id = state.get("pr_id") if state.get("pr_id") is not None else "unknown"
        logger.info("Review graph started. mode=fallback pr_id=%s", pr_id)
        for node in self._nodes_in_order:
            update = node(state)
            if not update:
                continue

            if not isinstance(update, dict):
                raise TypeError("Review graph node must return a dict-like state update.")
            state.update(update)

        logger.info(
            "Review graph completed. mode=fallback pr_id=%s halted=%s",
            pr_id,
            bool(state.get("halt")),
        )
        return state


def build_review_graph(
    *,
    resolve_runtime_settings,
    get_vcs_client,
    resolve_repository_path,
    extract_changed_file_paths_from_diff,
    build_repo_map_for_changed_files,
    retrieve_related_files_context,
    retrieve_bounded_code_search_context,
    compose_repository_context_bundle,
    repository_path,
    max_changed_files,
    max_repo_map_files,
    max_repo_map_chars,
    max_related_files,
    max_related_files_chars,
    max_code_search_results,
    max_code_search_chars,
    max_code_search_query_terms,
    repository_ignore_directories,
    convert_to_unified_diff_and_anchor_index,
    truncate_diff,
    fetch_pr_metadata,
    fetch_pr_activities,
    build_existing_feedback_context,
    build_review_purpose,
    build_previous_response_id,
    normalize_comment_severity,
    resolve_comment_severity,
    resolve_anchor_by_id,
    post_inline_comment,
    upsert_summary_comment,
    get_review_model_completion,
    parse_review_payload,
    extract_previous_response_id,
    build_judge_prompt_user_content,
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
    react_lazy_repository_context,
    react_default_include_changed_files,
):
    nodes = ReviewGraphNodes(
        resolve_runtime_settings=resolve_runtime_settings,
        get_vcs_client=get_vcs_client,
        resolve_repository_path=resolve_repository_path,
        extract_changed_file_paths_from_diff=extract_changed_file_paths_from_diff,
        build_repo_map_for_changed_files=build_repo_map_for_changed_files,
        retrieve_related_files_context=retrieve_related_files_context,
        retrieve_bounded_code_search_context=retrieve_bounded_code_search_context,
        compose_repository_context_bundle=compose_repository_context_bundle,
        repository_path=repository_path,
        max_changed_files=max_changed_files,
        max_repo_map_files=max_repo_map_files,
        max_repo_map_chars=max_repo_map_chars,
        max_related_files=max_related_files,
        max_related_files_chars=max_related_files_chars,
        max_code_search_results=max_code_search_results,
        max_code_search_chars=max_code_search_chars,
        max_code_search_query_terms=max_code_search_query_terms,
        repository_ignore_directories=repository_ignore_directories,
        convert_to_unified_diff_and_anchor_index=convert_to_unified_diff_and_anchor_index,
        truncate_diff=truncate_diff,
        fetch_pr_metadata=fetch_pr_metadata,
        fetch_pr_activities=fetch_pr_activities,
        build_existing_feedback_context=build_existing_feedback_context,
        build_review_purpose=build_review_purpose,
        build_previous_response_id=build_previous_response_id,
        normalize_comment_severity=normalize_comment_severity,
        resolve_comment_severity=resolve_comment_severity,
        resolve_anchor_by_id=resolve_anchor_by_id,
        post_inline_comment=post_inline_comment,
        upsert_summary_comment=upsert_summary_comment,
        model_endpoint=model_endpoint,
        react_enabled=react_enabled,
        react_lazy_repository_context=react_lazy_repository_context,
        react_default_include_changed_files=react_default_include_changed_files,
    )
    agents = ReviewGraphAgents(
        get_review_model_completion=get_review_model_completion,
        parse_review_payload=parse_review_payload,
        extract_previous_response_id=extract_previous_response_id,
        build_judge_prompt_user_content=build_judge_prompt_user_content,
        build_repo_map_for_changed_files=build_repo_map_for_changed_files,
        retrieve_related_files_context=retrieve_related_files_context,
        retrieve_bounded_code_search_context=retrieve_bounded_code_search_context,
        compose_repository_context_bundle=compose_repository_context_bundle,
        resolve_comment_severity=resolve_comment_severity,
        max_repo_map_files=max_repo_map_files,
        max_repo_map_chars=max_repo_map_chars,
        max_related_files=max_related_files,
        max_related_files_chars=max_related_files_chars,
        max_code_search_results=max_code_search_results,
        max_code_search_chars=max_code_search_chars,
        max_code_search_query_terms=max_code_search_query_terms,
        repository_ignore_directories=repository_ignore_directories,
        response_state_store_cls=response_state_store_cls,
        response_state_file=response_state_file,
        response_state_ttl_days=response_state_ttl_days,
        model_endpoint=model_endpoint,
        react_enabled=react_enabled,
        react_max_draft_iterations=react_max_draft_iterations,
        react_max_judge_iterations=react_max_judge_iterations,
        react_max_tool_calls_per_agent=react_max_tool_calls_per_agent,
        react_max_tool_result_chars=react_max_tool_result_chars,
        react_require_initial_repository_tool=react_require_initial_repository_tool,
        react_allow_judge_tool_retrieval=react_allow_judge_tool_retrieval,
    )

    nodes_in_order = [
        nodes.fetch_pr_context,
        nodes.extract_changed_files,
        nodes.build_repo_map,
        nodes.retrieve_related_files,
        nodes.retrieve_code_search_context,
        nodes.compose_repository_context,
        nodes.prepare_review_inputs,
        agents.draft_reviewer,
        nodes.finding_normalizer,
        agents.evidence_judge,
        nodes.summary_builder,
        nodes.anchor_resolver,
        agents.policy_guard_agent,
        nodes.publish_review,
    ]

    if not LANGGRAPH_AVAILABLE or StateGraph is None:
        return _FallbackCompiledReviewGraph(nodes_in_order)

    review_graph = StateGraph(ReviewGraphState)
    review_graph.add_node("fetch_pr_context", nodes.fetch_pr_context)
    review_graph.add_node("extract_changed_files", nodes.extract_changed_files)
    review_graph.add_node("build_repo_map", nodes.build_repo_map)
    review_graph.add_node("retrieve_related_files", nodes.retrieve_related_files)
    review_graph.add_node(
        "retrieve_code_search_context",
        nodes.retrieve_code_search_context,
    )
    review_graph.add_node("compose_repository_context", nodes.compose_repository_context)
    review_graph.add_node("prepare_review_inputs", nodes.prepare_review_inputs)
    review_graph.add_node("draft_reviewer", agents.draft_reviewer)
    review_graph.add_node("finding_normalizer", nodes.finding_normalizer)
    review_graph.add_node("evidence_judge", agents.evidence_judge)
    review_graph.add_node("summary_builder", nodes.summary_builder)
    review_graph.add_node("anchor_resolver", nodes.anchor_resolver)
    review_graph.add_node("policy_guard_agent", agents.policy_guard_agent)
    review_graph.add_node("publish_review", nodes.publish_review)

    review_graph.add_edge(START, "fetch_pr_context")
    review_graph.add_edge("fetch_pr_context", "extract_changed_files")
    review_graph.add_edge("extract_changed_files", "build_repo_map")
    review_graph.add_edge("extract_changed_files", "retrieve_related_files")
    review_graph.add_edge("extract_changed_files", "retrieve_code_search_context")
    review_graph.add_edge("build_repo_map", "compose_repository_context")
    review_graph.add_edge("retrieve_related_files", "compose_repository_context")
    review_graph.add_edge("retrieve_code_search_context", "compose_repository_context")
    review_graph.add_edge("compose_repository_context", "prepare_review_inputs")
    review_graph.add_edge("prepare_review_inputs", "draft_reviewer")
    review_graph.add_edge("draft_reviewer", "finding_normalizer")
    review_graph.add_edge("finding_normalizer", "evidence_judge")
    review_graph.add_edge("evidence_judge", "summary_builder")
    review_graph.add_edge("summary_builder", "anchor_resolver")
    review_graph.add_edge("anchor_resolver", "policy_guard_agent")
    review_graph.add_edge("policy_guard_agent", "publish_review")
    review_graph.add_edge("publish_review", END)

    return review_graph.compile()


def execute_review_graph(
    *,
    initial_state,
    resolve_runtime_settings,
    get_vcs_client,
    resolve_repository_path,
    extract_changed_file_paths_from_diff,
    build_repo_map_for_changed_files,
    retrieve_related_files_context,
    retrieve_bounded_code_search_context,
    compose_repository_context_bundle,
    repository_path,
    max_changed_files,
    max_repo_map_files,
    max_repo_map_chars,
    max_related_files,
    max_related_files_chars,
    max_code_search_results,
    max_code_search_chars,
    max_code_search_query_terms,
    repository_ignore_directories,
    convert_to_unified_diff_and_anchor_index,
    truncate_diff,
    fetch_pr_metadata,
    fetch_pr_activities,
    build_existing_feedback_context,
    build_review_purpose,
    build_previous_response_id,
    normalize_comment_severity,
    resolve_comment_severity,
    resolve_anchor_by_id,
    post_inline_comment,
    upsert_summary_comment,
    get_review_model_completion,
    parse_review_payload,
    extract_previous_response_id,
    build_judge_prompt_user_content,
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
    react_lazy_repository_context,
    react_default_include_changed_files,
):
    pr_id = (
        initial_state.get("pr_id")
        if isinstance(initial_state, dict) and initial_state.get("pr_id") is not None
        else "unknown"
    )
    logger.info(
        "Review graph execution started. pr_id=%s langgraph_available=%s",
        pr_id,
        LANGGRAPH_AVAILABLE,
    )

    effective_react_enabled, resolved_repository_path = _resolve_effective_react_settings(
        react_enabled=react_enabled,
        repository_path=repository_path,
        resolve_repository_path=resolve_repository_path,
    )
    if react_enabled and not effective_react_enabled:
        logger.info(
            "ReAct disabled because REPOSITORY_PATH is unset or invalid. pr_id=%s",
            pr_id,
        )

    review_graph = build_review_graph(
        resolve_runtime_settings=resolve_runtime_settings,
        get_vcs_client=get_vcs_client,
        resolve_repository_path=resolve_repository_path,
        extract_changed_file_paths_from_diff=extract_changed_file_paths_from_diff,
        build_repo_map_for_changed_files=build_repo_map_for_changed_files,
        retrieve_related_files_context=retrieve_related_files_context,
        retrieve_bounded_code_search_context=retrieve_bounded_code_search_context,
        compose_repository_context_bundle=compose_repository_context_bundle,
        repository_path=resolved_repository_path,
        max_changed_files=max_changed_files,
        max_repo_map_files=max_repo_map_files,
        max_repo_map_chars=max_repo_map_chars,
        max_related_files=max_related_files,
        max_related_files_chars=max_related_files_chars,
        max_code_search_results=max_code_search_results,
        max_code_search_chars=max_code_search_chars,
        max_code_search_query_terms=max_code_search_query_terms,
        repository_ignore_directories=repository_ignore_directories,
        convert_to_unified_diff_and_anchor_index=convert_to_unified_diff_and_anchor_index,
        truncate_diff=truncate_diff,
        fetch_pr_metadata=fetch_pr_metadata,
        fetch_pr_activities=fetch_pr_activities,
        build_existing_feedback_context=build_existing_feedback_context,
        build_review_purpose=build_review_purpose,
        build_previous_response_id=build_previous_response_id,
        normalize_comment_severity=normalize_comment_severity,
        resolve_comment_severity=resolve_comment_severity,
        resolve_anchor_by_id=resolve_anchor_by_id,
        post_inline_comment=post_inline_comment,
        upsert_summary_comment=upsert_summary_comment,
        get_review_model_completion=get_review_model_completion,
        parse_review_payload=parse_review_payload,
        extract_previous_response_id=extract_previous_response_id,
        build_judge_prompt_user_content=build_judge_prompt_user_content,
        response_state_store_cls=response_state_store_cls,
        response_state_file=response_state_file,
        response_state_ttl_days=response_state_ttl_days,
        model_endpoint=model_endpoint,
        react_enabled=effective_react_enabled,
        react_max_draft_iterations=react_max_draft_iterations,
        react_max_judge_iterations=react_max_judge_iterations,
        react_max_tool_calls_per_agent=react_max_tool_calls_per_agent,
        react_max_tool_result_chars=react_max_tool_result_chars,
        react_require_initial_repository_tool=react_require_initial_repository_tool,
        react_allow_judge_tool_retrieval=react_allow_judge_tool_retrieval,
        react_lazy_repository_context=react_lazy_repository_context,
        react_default_include_changed_files=react_default_include_changed_files,
    )
    result = review_graph.invoke(initial_state)
    logger.info(
        "Review graph execution completed. pr_id=%s halted=%s",
        pr_id,
        bool((result or {}).get("halt")) if isinstance(result, dict) else False,
    )
    return result
