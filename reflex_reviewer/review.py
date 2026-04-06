import argparse
import json
import logging
import os
import re
from pathlib import Path

import requests  # type: ignore[reportMissingImports,reportMissingModuleSource]
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
from .llm_api_client import chat_completions, responses
from .response_handler import parse_review_payload
from .review_response_state import ReviewResponseStateStore
from .vcs import get_vcs_client

logger = logging.getLogger(__name__)

review_config = get_review_config()
model_config = get_model_config()
MAX_DIFF_CHARS = review_config["max_diff_chars"]
MAX_EXISTING_FEEDBACK_COMMENTS = review_config["max_existing_feedback_comments"]
ACTIVITIES_FETCH_LIMIT = review_config["activities_fetch_limit"]
SANITIZED_COMMENT_MAX_CHARS = review_config["sanitized_comment_max_chars"]
SKIP_EXTENSIONS = review_config["skip_extensions"]
SKIP_FILES = review_config["skip_files"]
MODEL_ENDPOINT = str(model_config.get("model_endpoint") or "responses").strip().lower()
RESPONSE_STATE_FILE = review_config["response_state_file"]
RESPONSE_STATE_TTL_DAYS = review_config["response_state_ttl_days"]
ALLOWED_COMMENT_SEVERITIES = {"CRITICAL", "MAJOR", "ADVISORY"}
DEFAULT_COMMENT_SEVERITY = "ADVISORY"
SUMMARY_COMMENT_MARKER = "<!-- reflex-reviewer-summary -->"
SEVERITY_PREFIX_PATTERN = re.compile(
    r"^\[(?P<severity>[^\]]+)\]\s*(?P<body>.*)$", re.DOTALL
)


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
    return "**Verdict:**" in text and "**Summary:**" in text and "**Checklist**" in text


def _parse_inline_comment_payload(text):
    inline_body = (text or "").split("\n\n###", 1)[0].strip()
    if not inline_body:
        return DEFAULT_COMMENT_SEVERITY, ""

    match = SEVERITY_PREFIX_PATTERN.match(inline_body)
    if not match:
        return DEFAULT_COMMENT_SEVERITY, inline_body

    severity = _normalize_comment_severity(match.group("severity"))
    body = (match.group("body") or "").strip()
    return severity, body


def _normalize_comment_text(text):
    return re.sub(r"\s+", " ", (text or "").strip()).lower()


def _inline_comment_key(path, line, severity, text):
    normalized_severity = _resolve_comment_severity(severity, path)
    return "|".join(
        [
            _normalize_repo_path(path),
            str(int(line)),
            normalized_severity,
            _normalize_comment_text(text),
        ]
    )


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


def _sanitize_comment_text(text):
    """Normalize whitespace and trim long comments for prompt efficiency."""
    if not text:
        return ""
    compact = re.sub(r"\s+", " ", text).strip()
    return compact[:SANITIZED_COMMENT_MAX_CHARS]


def build_existing_feedback_context(activities, team_name):
    """Build prompt context from recent comments (AI + human)."""
    comment_lines = []
    ai_count = 0
    human_count = 0

    for activity in activities:
        if activity.get("action") != "COMMENTED":
            continue

        comment = activity.get("comment", {})
        raw_text = comment.get("text", "")
        cleaned_text = _sanitize_comment_text(raw_text)
        if not cleaned_text:
            continue

        if _is_bot_comment_text(raw_text, team_name):
            ai_count += 1
            comment_type = "AI"
        else:
            human_count += 1
            author_name = comment.get("author", {}).get("displayName") or "Unknown"
            comment_type = f"Human ({author_name})"

        comment_lines.append(f"- {comment_type}: {cleaned_text}")

    if not comment_lines:
        return "No prior feedback available."

    recent_comments = comment_lines[-MAX_EXISTING_FEEDBACK_COMMENTS:]
    logger.info(
        "Review prompt context prepared from recent comments. included=%s ai=%s human=%s",
        len(recent_comments),
        ai_count,
        human_count,
    )
    return "\n".join(recent_comments)


def _extract_existing_comment_state(activities, team_name):
    """Extract unresolved bot inline comment keys from existing PR comments."""
    comment_entries = []
    human_reply_parent_ids = set()
    unresolved_inline_comment_keys = set()

    for activity in activities:
        if activity.get("action") != "COMMENTED":
            continue

        comment = activity.get("comment", {})
        if not isinstance(comment, dict):
            continue

        comment_entries.append(comment)
        parent = comment.get("parent") or {}
        parent_id = parent.get("id") if isinstance(parent, dict) else None
        if parent_id is not None and not _is_bot_comment_text(
            comment.get("text", ""), team_name
        ):
            human_reply_parent_ids.add(str(parent_id))

    for comment in comment_entries:
        raw_text = comment.get("text", "")
        if not _is_bot_comment_text(raw_text, team_name):
            continue

        comment_id = comment.get("id")
        if _is_summary_comment_text(raw_text, team_name):
            continue

        anchor = comment.get("anchor")
        if not isinstance(anchor, dict):
            continue

        anchor_path = anchor.get("path")
        anchor_line = anchor.get("line")
        if not anchor_path or anchor_line is None:
            continue

        try:
            anchor_line = int(anchor_line)
        except (TypeError, ValueError):
            continue

        severity, text = _parse_inline_comment_payload(raw_text)
        inline_key = _inline_comment_key(anchor_path, anchor_line, severity, text)

        if comment_id is None or str(comment_id) not in human_reply_parent_ids:
            unresolved_inline_comment_keys.add(inline_key)

    return {
        "unresolved_inline_comment_keys": unresolved_inline_comment_keys,
    }


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
    normalized_verdict = (verdict or "CHANGES_SUGGESTED").strip()
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
        f"**Verdict:** `{normalized_verdict}`\n\n"
        f"**Summary:** {normalized_summary}\n\n"
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
):
    draft_review_json = json.dumps(draft_review_data, ensure_ascii=False)
    return (
        prompt_template.replace("{{DIFF_CONTENT}}", safe_diff)
        .replace("{{PR_TITLE}}", pr_title)
        .replace("{{PR_DESCRIPTION}}", pr_description)
        .replace("{{EXISTING_FEEDBACK}}", existing_feedback)
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
        runtime_settings = _resolve_runtime_settings(runtime_overrides)
        run_team_name = runtime_settings["team_name"]
        run_draft_model = runtime_settings["draft_model"]
        run_judge_model = runtime_settings["judge_model"]
        run_stream_response = runtime_settings["stream_response"]
        logger.info("Review run started.")
        vcs_client = get_vcs_client(
            vcs_type=vcs_type,
            config_overrides=runtime_overrides,
        )
        vcs_config = vcs_client.get_vcs_config()
        pr_id = pr_id if pr_id is not None else vcs_config.get("pr_id")
        if not pr_id:
            raise ValueError("PR id is required. Set VCS_PR_ID.")

        logger.info("Fetching PR diff for review. pr_id=%s", pr_id)

        # 1. Convert JSON -> Unified Git Diff (with skipping) + anchor map.
        raw_diff_data = vcs_client.fetch_pr_diff(pr_id)
        raw_git_diff, anchor_index = convert_to_unified_diff_and_anchor_index(
            raw_diff_data
        )
        # 2. Hard Truncate if still over limit
        safe_diff = truncate_diff(raw_git_diff)

        if not safe_diff.strip():
            logger.warning(
                "No reviewable code changes after filtering. pr_id=%s", pr_id
            )
            return

        pr_title, pr_description = fetch_pr_metadata(vcs_client, pr_id)
        activities = fetch_pr_activities(vcs_client, pr_id)
        existing_feedback = build_existing_feedback_context(activities, run_team_name)
        existing_comment_state = _extract_existing_comment_state(
            activities, run_team_name
        )

        # 3. Request Review from GPT-5.4 mini
        prompts_dir = Path(__file__).resolve().parent / "prompts"
        with open(prompts_dir / "review_system_prompt.md", "r", encoding="utf-8") as f:
            draft_sys_p = f.read().replace("{{TEAM_NAME}}", run_team_name)
        with open(prompts_dir / "review_user_prompt.md", "r", encoding="utf-8") as f:
            draft_user_p = (
                f.read()
                .replace("{{DIFF_CONTENT}}", safe_diff)
                .replace("{{PR_TITLE}}", pr_title)
                .replace("{{PR_DESCRIPTION}}", pr_description)
                .replace("{{EXISTING_FEEDBACK}}", existing_feedback)
            )

        logger.info(
            "Requesting draft review model response. model=%s pr_id=%s",
            run_draft_model,
            pr_id,
        )

        state_key = _build_previous_response_id(vcs_config or {}, pr_id)
        previous_response_id = None
        store_response = False

        if MODEL_ENDPOINT == "responses":
            response_state_store = ReviewResponseStateStore(
                RESPONSE_STATE_FILE,
                ttl_days=RESPONSE_STATE_TTL_DAYS,
            )
            previous_response_id = response_state_store.get_previous_response_id(
                state_key
            )
            store_response = previous_response_id is None

        draft_response = get_review_model_completion(
            run_draft_model,
            draft_sys_p,
            draft_user_p,
            pr_id=pr_id,
            vcs_config=vcs_config,
            previous_response_id=previous_response_id,
            store_response=store_response,
            model_endpoint=MODEL_ENDPOINT,
            stream_response=run_stream_response,
        )

        if MODEL_ENDPOINT == "responses":
            latest_response_id = _extract_previous_response_id(draft_response)
            if latest_response_id:
                response_state_store.set_previous_response_id(
                    state_key,
                    latest_response_id,
                )
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
            draft_review_data = parse_review_payload(draft_response)
        except ValueError:
            logger.exception("Unable to parse draft review payload")
            return

        with open(
            prompts_dir / "judge_review_system_prompt.md", "r", encoding="utf-8"
        ) as f:
            judge_sys_p = f.read().replace("{{TEAM_NAME}}", run_team_name)
        with open(prompts_dir / "judge_review_user_prompt.md", "r", encoding="utf-8") as f:
            judge_user_p = _build_judge_prompt_user_content(
                prompt_template=f.read(),
                pr_title=pr_title,
                pr_description=pr_description,
                safe_diff=safe_diff,
                existing_feedback=existing_feedback,
                draft_review_data=draft_review_data,
            )

        logger.info(
            "Requesting judge review model response. model=%s pr_id=%s",
            run_judge_model,
            pr_id,
        )
        judge_response = get_review_model_completion(
            run_judge_model,
            judge_sys_p,
            judge_user_p,
            pr_id=pr_id,
            vcs_config=vcs_config,
            previous_response_id=None,
            store_response=False,
            model_endpoint=MODEL_ENDPOINT,
            stream_response=run_stream_response,
        )

        try:
            review_data = parse_review_payload(judge_response)
        except ValueError:
            logger.exception("Unable to parse judge review payload")
            return

        verdict = review_data.get("verdict", "CHANGES_SUGGESTED")
        summary = review_data.get("summary", "No issues identified.")
        checklist = review_data.get("checklist", [])
        comments = review_data.get("comments", [])

        if not isinstance(comments, list) or not comments:
            try:
                upsert_summary_comment(
                    vcs_client,
                    pr_id,
                    verdict,
                    summary,
                    checklist,
                    run_team_name,
                )
            except requests.exceptions.RequestException:
                logger.exception("Failed to post summary comment")

            logger.info(
                "No inline comments to post. Review run completed with summary only."
            )
            return

        posted_inline_count = 0
        skipped_inline_count = 0
        posted_inline_keys = set()
        unresolved_inline_comment_keys = set(
            existing_comment_state.get("unresolved_inline_comment_keys", set())
        )

        for comment in comments:
            anchor_id = (comment.get("anchor_id") or "").strip()
            severity = _normalize_comment_severity(comment.get("severity"))
            text = (comment.get("text") or "").strip()

            if not text or not anchor_id:
                skipped_inline_count += 1
                logger.warning(
                    "Skipping inline comment missing required anchor_id/text",
                )
                continue

            resolved_anchor = _resolve_anchor_by_id(anchor_id, anchor_index)

            if not resolved_anchor.get("resolved"):
                skipped_inline_count += 1
                logger.warning(
                    "Skipping inline comment: reason=%s anchor_id=%s resolved_path=%s",
                    resolved_anchor.get("reason"),
                    anchor_id,
                    resolved_anchor.get("path"),
                )
                continue

            severity = _resolve_comment_severity(severity, resolved_anchor["path"])

            inline_key = _inline_comment_key(
                resolved_anchor["path"], resolved_anchor["line"], severity, text
            )
            if inline_key in unresolved_inline_comment_keys:
                skipped_inline_count += 1
                logger.info(
                    "Skipping repost of unresolved existing comment for %s:%s",
                    resolved_anchor["path"],
                    resolved_anchor["line"],
                )
                continue

            if inline_key in posted_inline_keys:
                skipped_inline_count += 1
                logger.info(
                    "Skipping duplicate inline comment in current run for %s:%s",
                    resolved_anchor["path"],
                    resolved_anchor["line"],
                )
                continue

            try:
                post_inline_comment(
                    vcs_client,
                    pr_id,
                    resolved_anchor["anchor"],
                    severity,
                    text,
                    run_team_name,
                )
                posted_inline_count += 1
                posted_inline_keys.add(inline_key)
            except requests.exceptions.RequestException as e:
                logger.warning(
                    "Failed to post inline comment for %s:%s - %s",
                    resolved_anchor["path"],
                    resolved_anchor["line"],
                    e,
                )

        try:
            upsert_summary_comment(
                vcs_client,
                pr_id,
                verdict,
                summary,
                checklist,
                run_team_name,
            )
        except requests.exceptions.RequestException:
            logger.exception("Failed to post summary comment")

        logger.info(
            "Review run completed. pr_id=%s posted_inline=%s skipped_inline=%s",
            pr_id,
            posted_inline_count,
            skipped_inline_count,
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
