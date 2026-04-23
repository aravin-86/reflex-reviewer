import argparse
import json
import logging
import os
import re

from openai import (  # type: ignore[reportMissingImports]
    APIConnectionError,
    APITimeoutError,
    InternalServerError,
    RateLimitError,
)
from tenacity import (  # type: ignore[reportMissingImports]
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .config import (
    clear_runtime_overrides,
    get_common_config,
    get_model_config,
    get_review_config,
    set_runtime_overrides,
)
from .llm.api_client import chat_completions, responses
from .repository_context.service import (
    build_repo_map_for_changed_files,
    compose_repository_context_bundle,
    extract_changed_file_paths_from_diff,
    resolve_repository_path,
    retrieve_bounded_code_search_context,
    retrieve_related_files_context,
)
from .llm.response_handler import parse_review_payload
from .review_graph_runtime.graph import execute_review_graph
from .review_runtime.response_state import ReviewResponseStateStore
from .vcs import get_vcs_client

logger = logging.getLogger(__name__)

review_config = get_review_config()
model_config = get_model_config()
MAX_DIFF_CHARS = review_config["max_diff_chars"]
MAX_EXISTING_FEEDBACK_COMMENTS = review_config["max_existing_feedback_comments"]
ACTIVITIES_FETCH_LIMIT = review_config["activities_fetch_limit"]
SANITIZED_COMMENT_MAX_CHARS = review_config["sanitized_comment_max_chars"]
REPOSITORY_PATH = review_config["repository_path"]
MAX_CHANGED_FILES = review_config["max_changed_files"]
MAX_REPO_MAP_FILES = review_config["max_repo_map_files"]
MAX_REPO_MAP_CHARS = review_config["max_repo_map_chars"]
MAX_RELATED_FILES = review_config["max_related_files"]
MAX_RELATED_FILES_CHARS = review_config["max_related_files_chars"]
MAX_CODE_SEARCH_RESULTS = review_config["max_code_search_results"]
MAX_CODE_SEARCH_CHARS = review_config["max_code_search_chars"]
MAX_CODE_SEARCH_QUERY_TERMS = review_config["max_code_search_query_terms"]
REPOSITORY_IGNORE_DIRECTORIES = review_config["repository_ignore_directories"]
SKIP_EXTENSIONS = review_config["skip_extensions"]
SKIP_FILES = review_config["skip_files"]
MODEL_ENDPOINT = str(model_config.get("model_endpoint") or "responses").strip().lower()
RESPONSE_STATE_FILE = review_config["response_state_file"]
RESPONSE_STATE_TTL_DAYS = review_config["response_state_ttl_days"]
ALLOWED_COMMENT_SEVERITIES = {"CRITICAL", "MAJOR", "ADVISORY"}
DEFAULT_COMMENT_SEVERITY = "ADVISORY"
SUMMARY_COMMENT_MARKER = "<!-- reflex-reviewer-summary -->"
SUMMARY_COMMENT_SECTIONS = (
    ("**Recommendation:**", "**Review Summary:**", "**Checklist**"),
)
VERDICT_TO_RECOMMENDATION = {
    "APPROVED": "Looks Good",
    "LOOKS GOOD": "Looks Good",
    "CHANGES_SUGGESTED": "Changes Suggested",
    "CHANGES SUGGESTED": "Changes Suggested",
}
SEVERITY_PREFIX_PATTERN = re.compile(
    r"^\[(?P<severity>[^\]]+)\]\s*(?P<body>.*)$", re.DOTALL
)
BOT_SIGNATURE_PATTERN = re.compile(
    r"(?:\r?\n){2}\s*###\s*#?[A-Za-z0-9._-]+\s*$",
    re.DOTALL,
)
PURPOSE_FALLBACK = "Not specified in PR metadata"
PURPOSE_MAX_CHARS = 400


def _parse_bool(value):
    if isinstance(value, bool):
        return value

    normalized = str(value or "").strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False

    raise argparse.ArgumentTypeError(
        "Expected a boolean value: true/false, yes/no, on/off, 1/0"
    )


def _resolve_runtime_settings(config_overrides=None):
    common_config = get_common_config(config_overrides)
    team_name = str(common_config.get("team_name") or "")
    draft_model = str(common_config.get("draft_model") or "")
    judge_model = str(common_config.get("judge_model") or "")
    stream_response = bool(common_config.get("stream_response"))

    if not team_name:
        raise ValueError(
            "TEAM_NAME is required. Pass --team-name (identifier for your team to the LLM model)."
        )

    if not draft_model:
        raise ValueError("DRAFT_MODEL is required. Pass --draft-model.")

    if not judge_model:
        raise ValueError("JUDGE_MODEL is required. Pass --judge-model.")

    return {
        "team_name": team_name,
        "draft_model": draft_model,
        "judge_model": judge_model,
        "stream_response": stream_response,
    }


def _build_runtime_overrides(
    team_name,
    draft_model,
    judge_model,
    stream_response,
    vcs_base_url=None,
    vcs_project_key=None,
    vcs_repo_slug=None,
    vcs_token=None,
    llm_api_base_url=None,
    llm_api_proxy_url=None,
    llm_api_key=None,
    llm_api_reasoning_effort=None,
):
    return {
        "team_name": team_name,
        "draft_model": draft_model,
        "judge_model": judge_model,
        "stream_response": stream_response,
        "vcs_base_url": vcs_base_url,
        "vcs_project_key": vcs_project_key,
        "vcs_repo_slug": vcs_repo_slug,
        "vcs_token": vcs_token,
        "llm_api_base_url": llm_api_base_url,
        "llm_api_proxy_url": llm_api_proxy_url,
        "llm_api_key": llm_api_key,
        "llm_api_reasoning_effort": llm_api_reasoning_effort,
    }


def should_skip_file(file_path):
    """Check if file should be excluded from AI review."""
    if not file_path or file_path == "/dev/null":
        return True
    filename = os.path.basename(file_path)
    _, ext = os.path.splitext(file_path)
    return filename in SKIP_FILES or ext.lower() in SKIP_EXTENSIONS


def _normalize_repo_path(file_path):
    normalized = (file_path or "").strip().replace("\\", "/")
    normalized = re.sub(r"^(?:\./)+", "", normalized)
    normalized = re.sub(r"^(?:a|b)/", "", normalized)
    normalized = normalized.lstrip("/")
    return normalized.lower()


def _is_test_file_path(file_path):
    normalized = _normalize_repo_path(file_path)
    if not normalized:
        return False

    if normalized.startswith("tests/") or "/tests/" in normalized:
        return True

    filename = normalized.rsplit("/", 1)[-1]
    return (
        filename.startswith("test_")
        or filename.endswith("_test.py")
        or filename.endswith("_tests.py")
    )


def _normalize_comment_severity(severity):
    normalized = str(severity or "").strip().upper()
    if normalized in ALLOWED_COMMENT_SEVERITIES:
        return normalized
    return DEFAULT_COMMENT_SEVERITY


def _resolve_comment_severity(severity, file_path=None):
    normalized = _normalize_comment_severity(severity)
    if _is_test_file_path(file_path):
        return DEFAULT_COMMENT_SEVERITY
    return normalized


def _is_bot_comment_text(text, team_name):
    if not text:
        return False

    normalized_team_name = str(team_name or "").strip()
    if not normalized_team_name:
        return False

    hashtag_team_name = normalized_team_name.lstrip("#")
    markers = {f"### {normalized_team_name}"}
    if hashtag_team_name:
        markers.add(f"### #{hashtag_team_name}")

    return any(marker in text for marker in markers)


def _is_summary_comment_text(text, team_name):
    if SUMMARY_COMMENT_MARKER in (text or ""):
        return True

    if not _is_bot_comment_text(text, team_name):
        return False

    normalized_text = str(text or "")
    return any(
        all(section in normalized_text for section in summary_section)
        for summary_section in SUMMARY_COMMENT_SECTIONS
    )


def _parse_inline_comment_payload(text):
    inline_body = (text or "").strip()
    inline_body = BOT_SIGNATURE_PATTERN.sub("", inline_body).strip()
    if not inline_body:
        return DEFAULT_COMMENT_SEVERITY, ""

    match = SEVERITY_PREFIX_PATTERN.match(inline_body)
    if not match:
        return DEFAULT_COMMENT_SEVERITY, inline_body

    severity = _normalize_comment_severity(match.group("severity"))
    body = (match.group("body") or "").strip()
    return severity, body


def _is_root_comment(comment):
    if not isinstance(comment, dict):
        return False

    parent = comment.get("parent")
    if not parent:
        return True

    if not isinstance(parent, dict):
        return True

    parent_id = parent.get("id")
    if parent_id is None:
        return True

    return not str(parent_id).strip()


def _resolve_existing_inline_anchor_location(comment):
    anchor = comment.get("anchor")
    if not isinstance(anchor, dict):
        return None, None

    anchor_path = None
    for path_key in ("path", "srcPath", "filePath"):
        raw_path = anchor.get(path_key)
        if isinstance(raw_path, dict):
            raw_path = raw_path.get("toString")
        if isinstance(raw_path, str) and raw_path.strip():
            anchor_path = raw_path.strip()
            break

    anchor_line = None
    for line_key in ("line", "srcLine", "lineNumber"):
        raw_line = anchor.get(line_key)
        if raw_line is None:
            continue
        try:
            parsed_line = int(str(raw_line).strip())
        except (TypeError, ValueError):
            continue
        if parsed_line > 0:
            anchor_line = parsed_line
            break

    return anchor_path, anchor_line


def _safe_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _build_anchor_id(file_index, destination_line):
    """Build deterministic anchor identifier from diff file order and destination line."""
    return f"F{file_index}-L{destination_line}"


def convert_to_unified_diff_and_anchor_index(json_diff_data):
    """Converts VCS JSON diff to Unified format and builds line-anchor metadata."""
    unified_diff = []
    diffs = (json_diff_data or {}).get("diffs") or []
    skipped_count = 0
    anchor_index = {
        "by_path": {},
        "normalized_to_paths": {},
        "normalized_by_path": {},
        "by_anchor_id": {},
    }
    processed_file_count = 0

    for diff in diffs:
        if not isinstance(diff, dict):
            continue

        dest = (
            diff.get("destination", {}).get("toString", "/dev/null")
            if diff.get("destination")
            else "/dev/null"
        )

        if should_skip_file(dest):
            skipped_count += 1
            continue

        processed_file_count += 1

        source = (
            diff.get("source", {}).get("toString", "/dev/null")
            if diff.get("source")
            else "/dev/null"
        )

        unified_diff.append(f"--- {source}")
        unified_diff.append(f"+++ {dest}")

        path_meta = anchor_index["by_path"].setdefault(
            dest,
            {
                "line_types": {},
                "sorted_lines": [],
                "added_lines": [],
                "hunks": [],
            },
        )
        normalized_dest = _normalize_repo_path(dest)
        anchor_index["normalized_by_path"][dest] = normalized_dest
        normalized_paths = anchor_index["normalized_to_paths"].setdefault(
            normalized_dest, []
        )
        if dest not in normalized_paths:
            normalized_paths.append(dest)

        for hunk in diff.get("hunks", []):
            if not isinstance(hunk, dict):
                continue

            s_start = _safe_int(hunk.get("sourceLine", 0), 0)
            s_span = _safe_int(hunk.get("sourceSpan", 0), 0)
            d_start = _safe_int(hunk.get("destinationLine", 0), 0)
            d_span = _safe_int(hunk.get("destinationSpan", 0), 0)
            unified_diff.append(f"@@ -{s_start},{s_span} +{d_start},{d_span} @@")
            destination_line_cursor = d_start if d_start > 0 else 0
            hunk_meta = {
                "dest_start": d_start,
                "dest_span": d_span,
                "line_types": {},
                "sorted_lines": [],
                "added_lines": [],
                "min_line": None,
                "max_line": None,
            }

            for segment in hunk.get("segments", []):
                if not isinstance(segment, dict):
                    continue

                segment_type = (segment.get("type") or "CONTEXT").upper()
                prefix = {"ADDED": "+", "REMOVED": "-", "CONTEXT": " "}.get(
                    segment_type, " "
                )
                for line_obj in segment.get("lines", []):
                    line_text = (
                        line_obj.get("line", "")
                        if isinstance(line_obj, dict)
                        else str(line_obj or "")
                    )
                    anchor_id = None

                    if segment_type == "REMOVED":
                        unified_diff.append(f"{prefix}{line_text}")
                        continue

                    if destination_line_cursor > 0:
                        line_type = "ADDED" if segment_type == "ADDED" else "CONTEXT"
                        path_meta["line_types"][destination_line_cursor] = line_type
                        hunk_meta["line_types"][destination_line_cursor] = line_type
                        if line_type == "ADDED":
                            path_meta["added_lines"].append(destination_line_cursor)
                            hunk_meta["added_lines"].append(destination_line_cursor)

                        anchor_id = _build_anchor_id(
                            processed_file_count, destination_line_cursor
                        )
                        anchor_index["by_anchor_id"][anchor_id] = {
                            "path": dest,
                            "line": destination_line_cursor,
                            "line_type": line_type,
                            "anchor": {
                                "line": destination_line_cursor,
                                "lineType": line_type,
                                "fileType": "TO",
                                "path": dest,
                            },
                        }

                    rendered_line = f"{prefix}{line_text}"
                    if anchor_id:
                        rendered_line = f"{rendered_line}  ⟪ANCHOR_ID:{anchor_id}⟫"
                    unified_diff.append(rendered_line)
                    destination_line_cursor += 1

            hunk_meta["sorted_lines"] = sorted(hunk_meta["line_types"].keys())
            hunk_meta["added_lines"] = sorted(set(hunk_meta["added_lines"]))
            if hunk_meta["sorted_lines"]:
                hunk_meta["min_line"] = hunk_meta["sorted_lines"][0]
                hunk_meta["max_line"] = hunk_meta["sorted_lines"][-1]
                path_meta["hunks"].append(hunk_meta)

    for path_meta in anchor_index["by_path"].values():
        path_meta["sorted_lines"] = sorted(path_meta["line_types"].keys())
        path_meta["added_lines"] = sorted(set(path_meta.get("added_lines", [])))
        path_meta["hunks"] = sorted(
            [
                h
                for h in path_meta.get("hunks", [])
                if isinstance(h, dict) and h.get("sorted_lines")
            ],
            key=lambda h: h.get("min_line", 0),
        )

    logger.info(
        "Diff conversion complete. total=%s processed=%s skipped=%s",
        len(diffs),
        len(diffs) - skipped_count,
        skipped_count,
    )
    return "\n".join(unified_diff), anchor_index


def truncate_diff(diff_text):
    """Caps character count to prevent GPT-5.4 mini context overflow."""
    if len(diff_text) > MAX_DIFF_CHARS:
        logger.warning(
            "Diff still too large (%s chars). Truncating at limit.",
            len(diff_text),
        )
        return (
            diff_text[:MAX_DIFF_CHARS] + "\n\n[!] DIFF TRUNCATED DUE TO SIZE LIMITS [!]"
        )
    return diff_text


def _sanitize_response_id_component(value, fallback):
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "").strip())
    normalized = normalized.strip("-_.")
    return normalized or fallback


def _build_previous_response_id(vcs_config, pr_id):
    if not isinstance(vcs_config, dict):
        vcs_config = {}

    project_key = _sanitize_response_id_component(
        vcs_config.get("project"), "unknown-project"
    )
    repo_slug = _sanitize_response_id_component(
        vcs_config.get("repo_slug"), "unknown-repo"
    )
    normalized_pr_id = _sanitize_response_id_component(pr_id, "unknown-pr")

    return f"{project_key}:{repo_slug}:pr:{normalized_pr_id}"


def _extract_previous_response_id(response):
    if not isinstance(response, dict):
        return None

    response_id = response.get("id")
    if isinstance(response_id, str) and response_id.strip():
        return response_id.strip()

    previous_response_id = response.get("previous_response_id")
    if isinstance(previous_response_id, str) and previous_response_id.strip():
        return previous_response_id.strip()

    return None


def _build_model_input_items(sys_p, user_p):
    return [
        {"role": "system", "content": sys_p},
        {"role": "user", "content": user_p},
    ]


@retry(
    wait=wait_exponential(multiplier=1, min=4, max=60),
    stop=stop_after_attempt(5),
    retry=retry_if_exception_type(
        (RateLimitError, APIConnectionError, APITimeoutError, InternalServerError)
    ),
    reraise=True,
)
def get_model_completion(target_model, sys_p, user_p, stream_response=True, pr_id=None):
    return chat_completions(
        model=target_model,
        messages=_build_model_input_items(sys_p, user_p),
        stream=stream_response,
        pr_id=pr_id,
    )


def get_review_model_completion(
    target_model,
    sys_p,
    user_p,
    pr_id=None,
    vcs_config=None,
    previous_response_id=None,
    store_response=False,
    model_endpoint=None,
    stream_response=True,
):
    del vcs_config  # retained for backward-compatible function signature
    model_input_items = _build_model_input_items(sys_p, user_p)
    resolved_model_endpoint = (
        str(model_endpoint if model_endpoint is not None else MODEL_ENDPOINT)
        .strip()
        .lower()
    )

    if resolved_model_endpoint == "responses":
        return responses(
            model=target_model,
            input_items=model_input_items,
            previous_response_id=previous_response_id,
            store=store_response,
            stream=stream_response,
            pr_id=pr_id,
        )

    if resolved_model_endpoint != "chat_completions":
        logger.warning(
            "Unknown MODEL_ENDPOINT=%s. Falling back to chat_completions.",
            resolved_model_endpoint,
        )

    return get_model_completion(
        target_model,
        sys_p,
        user_p,
        stream_response=stream_response,
        pr_id=pr_id,
    )


def fetch_pr_metadata(vcs_client, pr_id):
    """Fetch PR title and description for additional review context."""
    return vcs_client.fetch_pr_metadata(pr_id)


def fetch_pr_activities(vcs_client, pr_id, limit=ACTIVITIES_FETCH_LIMIT):
    """Fetch PR activities with fault-tolerant behavior."""
    try:
        return vcs_client.fetch_pr_activities(pr_id, limit=limit)
    except Exception:
        logger.warning(
            "Failed to fetch PR activities; continuing without history.", exc_info=True
        )
        return []


def _normalize_purpose_text(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _truncate_purpose_text(value, max_chars=PURPOSE_MAX_CHARS):
    normalized = _normalize_purpose_text(value)
    if len(normalized) <= max_chars:
        return normalized
    return normalized[: max_chars - 1].rstrip() + "…"


def _extract_pr_description_summary_and_changes(pr_description):
    raw_description = str(pr_description or "")
    if not raw_description.strip() or raw_description.strip().upper() == "N/A":
        return "", ""

    summary_lines = []
    changes_lines = []
    current_section = None

    for raw_line in raw_description.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        normalized_heading = line.rstrip(":").strip().lower()
        if normalized_heading == "summary":
            current_section = "summary"
            continue
        if normalized_heading == "changes":
            current_section = "changes"
            continue
        if normalized_heading == "test results":
            current_section = None
            continue

        if re.match(r"^does\s+this\s+pull\s+request\b", line, re.IGNORECASE):
            current_section = None
            continue

        if current_section == "summary":
            summary_lines.append(line)
        elif current_section == "changes":
            changes_lines.append(line)

    summary_text = _truncate_purpose_text(" ".join(summary_lines), max_chars=180)
    changes_text = _truncate_purpose_text(" ".join(changes_lines), max_chars=180)
    return summary_text, changes_text


def _build_review_purpose(pr_title, pr_description):
    normalized_title = _normalize_purpose_text(pr_title)
    summary_text, changes_text = _extract_pr_description_summary_and_changes(
        pr_description
    )

    purpose_parts = []
    if normalized_title:
        purpose_parts.append(f"PR Title: {normalized_title}")
    if summary_text:
        purpose_parts.append(f"Summary: {summary_text}")
    if changes_text and changes_text.lower() != summary_text.lower():
        purpose_parts.append(f"Changes: {changes_text}")

    if not purpose_parts:
        fallback_description = _normalize_purpose_text(pr_description)
        if fallback_description and fallback_description.upper() != "N/A":
            purpose_parts.append(_truncate_purpose_text(fallback_description, max_chars=220))

    if not purpose_parts:
        return PURPOSE_FALLBACK

    return _truncate_purpose_text(" | ".join(purpose_parts))


def _sanitize_comment_text(text):
    """Normalize whitespace and trim long comments for prompt efficiency."""
    if not text:
        return ""
    compact = re.sub(r"\s+", " ", text).strip()
    return compact[:SANITIZED_COMMENT_MAX_CHARS]


def build_existing_feedback_context(activities, team_name):
    """Build prompt context from recent root comments (bot + human)."""
    comment_lines = []
    bot_count = 0
    human_count = 0

    for activity in activities:
        if activity.get("action") != "COMMENTED":
            continue

        comment = activity.get("comment", {})
        if not _is_root_comment(comment):
            continue

        raw_text = comment.get("text", "")
        if _is_summary_comment_text(raw_text, team_name):
            continue

        is_bot_comment = _is_bot_comment_text(raw_text, team_name)
        cleaned_text = _sanitize_comment_text(raw_text)
        if is_bot_comment:
            severity, bot_body = _parse_inline_comment_payload(raw_text)
            if bot_body:
                cleaned_text = _sanitize_comment_text(bot_body)
            if not cleaned_text:
                continue
            bot_count += 1
            comment_type = f"Bot (severity={severity})"
        else:
            if not cleaned_text:
                continue
            human_count += 1
            author_name = comment.get("author", {}).get("displayName") or "Unknown"
            comment_type = f"Human ({author_name})"

        anchor_path, anchor_line = _resolve_existing_inline_anchor_location(comment)
        anchor_parts = []
        if anchor_path:
            anchor_parts.append(f"file={anchor_path}")
        if anchor_line is not None:
            anchor_parts.append(f"line={anchor_line}")
        anchor_suffix = f" | {' | '.join(anchor_parts)}" if anchor_parts else ""
        comment_lines.append(f"- {comment_type}{anchor_suffix}: {cleaned_text}")

    if not comment_lines:
        return "No prior root comments available."

    recent_comments = comment_lines[-MAX_EXISTING_FEEDBACK_COMMENTS:]
    logger.info(
        "Review prompt context prepared from recent root comments. included=%s bot=%s human=%s",
        len(recent_comments),
        bot_count,
        human_count,
    )
    return "\n".join(recent_comments)


def _resolve_anchor_by_id(anchor_id, anchor_index):
    raw_anchor_id = (anchor_id or "").strip()
    if not raw_anchor_id:
        return {
            "resolved": False,
            "reason": "missing-anchor-id",
            "path": None,
            "line": None,
        }

    anchor_meta = anchor_index.get("by_anchor_id", {}).get(raw_anchor_id)
    if not anchor_meta:
        return {
            "resolved": False,
            "reason": "anchor-id-not-found",
            "path": None,
            "line": None,
        }

    return {
        "resolved": True,
        "anchor": anchor_meta["anchor"],
        "path": anchor_meta["path"],
        "line": anchor_meta["line"],
        "resolution": "anchor-id",
    }


def _build_summary_comment_body(verdict, summary, checklist, team_name):
    normalized_verdict = (verdict or "CHANGES_SUGGESTED").strip().upper()
    recommendation_label = VERDICT_TO_RECOMMENDATION.get(
        normalized_verdict, "Changes Suggested"
    )
    normalized_summary = (summary or "No issues identified.").strip()
    safe_checklist = checklist if isinstance(checklist, list) else []
    checklist_md = (
        "\n".join(f"- {item}" for item in safe_checklist if str(item).strip())
        or "- None"
    )

    hashtag_team_name = str(team_name or "").strip().lstrip("#")

    body = (
        f"### #{hashtag_team_name}\n\n"
        f"{SUMMARY_COMMENT_MARKER}\n\n"
        f"**Recommendation:** `{recommendation_label}`\n\n"
        f"**Review Summary:** {normalized_summary}\n\n"
        f"**Checklist**\n"
        f"{checklist_md}"
    )
    return body


def upsert_summary_comment(
    vcs_client,
    pr_id,
    verdict,
    summary,
    checklist,
    team_name,
    existing_summary_comment_id=None,
    existing_summary_comment_version=None,
):
    """Post a new summary comment for the current review run."""
    body = _build_summary_comment_body(verdict, summary, checklist, team_name)

    # Keep backward-compatible signature while intentionally posting append-only
    # summary comments for each run.
    del existing_summary_comment_id
    del existing_summary_comment_version

    result = vcs_client.post_comment(pr_id, body)
    logger.info("Posted summary comment. pr_id=%s", pr_id)
    return result


def post_inline_comment(vcs_client, pr_id, anchor, severity, text, team_name):
    """Posts a severity-tagged inline comment anchored to a changed destination line.

    This method is used for actionable code findings. It creates a VCS
    anchor on the destination file so the
    comment appears directly against the relevant diff line.
    """
    hashtag_team_name = str(team_name or "").strip().lstrip("#")
    anchor_path = anchor.get("path") if isinstance(anchor, dict) else None
    normalized_severity = _resolve_comment_severity(severity, anchor_path)
    body = f"[{normalized_severity}] {text}\n\n### #{hashtag_team_name}"
    return vcs_client.post_comment(pr_id, body, anchor=anchor)


def _build_judge_prompt_user_content(
    prompt_template,
    pr_title,
    pr_description,
    safe_diff,
    existing_feedback,
    draft_review_data,
    repository_context_bundle,
):
    repository_context = repository_context_bundle or {}
    repo_map = str(repository_context.get("repo_map") or "No repository map context available.")
    related_files_context = str(
        repository_context.get("related_files_context")
        or "No related-file context available."
    )
    code_search_context = str(
        repository_context.get("code_search_context")
        or "No code-search context available."
    )
    draft_review_json = json.dumps(draft_review_data, ensure_ascii=False)
    return (
        prompt_template.replace("{{DIFF_CONTENT}}", safe_diff)
        .replace("{{PR_TITLE}}", pr_title)
        .replace("{{PR_DESCRIPTION}}", pr_description)
        .replace("{{EXISTING_ROOT_COMMENTS}}", existing_feedback)
        .replace("{{EXISTING_FEEDBACK}}", existing_feedback)
        .replace("{{REPOSITORY_MAP}}", repo_map)
        .replace("{{RELATED_FILES_CONTEXT}}", related_files_context)
        .replace("{{CODE_SEARCH_CONTEXT}}", code_search_context)
        .replace("{{DRAFT_REVIEW_JSON}}", draft_review_json)
    )


def run(
    vcs_type=None,
    pr_id=None,
    team_name=None,
    draft_model=None,
    judge_model=None,
    stream_response=None,
    vcs_base_url=None,
    vcs_project_key=None,
    vcs_repo_slug=None,
    vcs_token=None,
    llm_api_base_url=None,
    llm_api_proxy_url=None,
    llm_api_key=None,
    llm_api_reasoning_effort=None,
):
    runtime_overrides = _build_runtime_overrides(
        team_name=team_name,
        draft_model=draft_model,
        judge_model=judge_model,
        stream_response=stream_response,
        vcs_base_url=vcs_base_url,
        vcs_project_key=vcs_project_key,
        vcs_repo_slug=vcs_repo_slug,
        vcs_token=vcs_token,
        llm_api_base_url=llm_api_base_url,
        llm_api_proxy_url=llm_api_proxy_url,
        llm_api_key=llm_api_key,
        llm_api_reasoning_effort=llm_api_reasoning_effort,
    )

    set_runtime_overrides(runtime_overrides)

    try:
        execute_review_graph(
            initial_state={
                "runtime_overrides": runtime_overrides,
                "vcs_type": vcs_type,
                "pr_id": pr_id,
                "halt": False,
            },
            resolve_runtime_settings=_resolve_runtime_settings,
            get_vcs_client=get_vcs_client,
            resolve_repository_path=resolve_repository_path,
            extract_changed_file_paths_from_diff=extract_changed_file_paths_from_diff,
            build_repo_map_for_changed_files=build_repo_map_for_changed_files,
            retrieve_related_files_context=retrieve_related_files_context,
            retrieve_bounded_code_search_context=retrieve_bounded_code_search_context,
            compose_repository_context_bundle=compose_repository_context_bundle,
            repository_path=REPOSITORY_PATH,
            max_changed_files=MAX_CHANGED_FILES,
            max_repo_map_files=MAX_REPO_MAP_FILES,
            max_repo_map_chars=MAX_REPO_MAP_CHARS,
            max_related_files=MAX_RELATED_FILES,
            max_related_files_chars=MAX_RELATED_FILES_CHARS,
            max_code_search_results=MAX_CODE_SEARCH_RESULTS,
            max_code_search_chars=MAX_CODE_SEARCH_CHARS,
            max_code_search_query_terms=MAX_CODE_SEARCH_QUERY_TERMS,
            repository_ignore_directories=REPOSITORY_IGNORE_DIRECTORIES,
            convert_to_unified_diff_and_anchor_index=convert_to_unified_diff_and_anchor_index,
            truncate_diff=truncate_diff,
            fetch_pr_metadata=fetch_pr_metadata,
            fetch_pr_activities=fetch_pr_activities,
            build_existing_feedback_context=build_existing_feedback_context,
            build_review_purpose=_build_review_purpose,
            build_previous_response_id=_build_previous_response_id,
            normalize_comment_severity=_normalize_comment_severity,
            resolve_comment_severity=_resolve_comment_severity,
            resolve_anchor_by_id=_resolve_anchor_by_id,
            post_inline_comment=post_inline_comment,
            upsert_summary_comment=upsert_summary_comment,
            get_review_model_completion=get_review_model_completion,
            parse_review_payload=parse_review_payload,
            extract_previous_response_id=_extract_previous_response_id,
            build_judge_prompt_user_content=_build_judge_prompt_user_content,
            response_state_store_cls=ReviewResponseStateStore,
            response_state_file=RESPONSE_STATE_FILE,
            response_state_ttl_days=RESPONSE_STATE_TTL_DAYS,
            model_endpoint=MODEL_ENDPOINT,
        )

    except Exception:
        logger.exception("Unexpected error")
    finally:
        clear_runtime_overrides()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Run AI pull request review using the configured VCS provider"
    )
    parser.add_argument(
        "--vcs-type",
        help="VCS provider (bitbucket, oci_devops_scm, github); defaults to VCS_TYPE env or 'bitbucket'",
    )
    parser.add_argument(
        "--pr-id",
        type=int,
        help="Pull request ID; defaults to VCS_PR_ID env var when omitted",
    )
    parser.add_argument(
        "--team-name",
        required=True,
        help="Identifier for your team to the LLM model",
    )
    parser.add_argument(
        "--draft-model",
        required=False,
        help=(
            "Draft model used to generate the initial review payload "
            "(overrides model.draft_model in reflex_reviewer.toml)"
        ),
    )
    parser.add_argument(
        "--judge-model",
        required=False,
        help=(
            "Judge model used to filter/rewrite draft review output "
            "(overrides model.judge_model in reflex_reviewer.toml)"
        ),
    )
    parser.add_argument(
        "--stream-response",
        type=_parse_bool,
        default=None,
        help="Enable streaming responses (overrides model.stream_response from reflex_reviewer.toml)",
    )
    parser.add_argument("--vcs-base-url", help="Override VCS_BASE_URL")
    parser.add_argument("--vcs-project-key", help="Override VCS_PROJECT_KEY")
    parser.add_argument("--vcs-repo-slug", help="Override VCS_REPO_SLUG")
    parser.add_argument("--vcs-token", help="Override VCS_TOKEN")
    parser.add_argument("--llm-api-base-url", help="Override LLM_API_BASE_URL")
    parser.add_argument("--llm-api-proxy-url", help="Override LLM_API_PROXY_URL")
    parser.add_argument("--llm-api-key", help="Override LLM_API_KEY")
    parser.add_argument(
        "--llm-api-reasoning-effort",
        help="LLM API reasoning effort: low|medium|high (defaults to env or high)",
    )
    args = parser.parse_args()
    run(
        vcs_type=args.vcs_type,
        pr_id=args.pr_id,
        team_name=args.team_name,
        draft_model=args.draft_model,
        judge_model=args.judge_model,
        stream_response=args.stream_response,
        vcs_base_url=args.vcs_base_url,
        vcs_project_key=args.vcs_project_key,
        vcs_repo_slug=args.vcs_repo_slug,
        vcs_token=args.vcs_token,
        llm_api_base_url=args.llm_api_base_url,
        llm_api_proxy_url=args.llm_api_proxy_url,
        llm_api_key=args.llm_api_key,
        llm_api_reasoning_effort=args.llm_api_reasoning_effort,
    )
