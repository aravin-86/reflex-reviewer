from typing import Any, Dict, List, Optional, TypedDict


class ReviewGraphState(TypedDict, total=False):
    runtime_overrides: Dict[str, Any]
    vcs_type: Optional[str]
    pr_id: int
    repository_path: Optional[str]

    team_name: str
    draft_model: str
    judge_model: str
    stream_response: bool
    model_endpoint: str

    vcs_client: Any
    vcs_config: Dict[str, Any]
    state_key: str

    raw_diff_data: Dict[str, Any]
    safe_diff: str
    anchor_index: Dict[str, Any]

    pr_title: str
    pr_description: str
    review_purpose: str
    existing_feedback: str
    existing_bot_inline_comments: List[Dict[str, Any]]

    changed_file_paths: List[str]
    changed_files_context: str
    repo_map: str
    related_files_context: str
    code_search_context: str
    repository_context_bundle: Dict[str, str]
    draft_repository_context_bundle: Dict[str, str]
    judge_repository_context_bundle: Dict[str, str]

    draft_sys_p: str
    draft_user_p: str
    draft_review_data: Dict[str, Any]
    normalized_draft_review_data: Dict[str, Any]
    draft_iteration_count: int
    draft_tool_calls: int
    draft_tool_trace: List[Dict[str, Any]]

    review_data: Dict[str, Any]
    judge_iteration_count: int
    judge_tool_calls: int
    judge_tool_trace: List[Dict[str, Any]]
    comments: List[Dict[str, Any]]
    resolved_comments: List[Dict[str, Any]]
    raw_comment_count: int

    verdict: str
    summary: str
    checklist: List[str]

    posted_inline_count: int
    skipped_inline_count: int
    existing_duplicate_suppressed_count: int
    existing_bot_comment_reply_sentiment_by_id: Dict[str, str]
    outstanding_existing_bot_comments: List[Dict[str, Any]]
    halt: bool
