import argparse
import json
import logging
import os
import re
import tempfile

from .config import (
    clear_runtime_overrides,
    get_common_config,
    get_distill_config,
    resolve_dpo_training_data_file_path,
    set_runtime_overrides,
)
from .litellm_client import LiteLLMResponseParseError, chat_completions, responses
from .response_handler import (
    extract_content_from_stream_response,
    parse_batched_sentiment_response,
)
from .vcs import get_vcs_client

# --- Configuration ---
distill_config = get_distill_config()
ACTIVITIES_FETCH_LIMIT = distill_config["activities_fetch_limit"]
DIFF_SKIP_EXTENSIONS = distill_config["diff_skip_extensions"]
MAX_LLM_THREADS = distill_config["max_llm_threads"]

logger = logging.getLogger(__name__)

SENTIMENT_ACCEPTED = "ACCEPTED"
SENTIMENT_REJECTED = "REJECTED"
SENTIMENT_UNSURE = "UNSURE"
VALID_SENTIMENTS = {SENTIMENT_ACCEPTED, SENTIMENT_REJECTED, SENTIMENT_UNSURE}
ALLOWED_COMMENT_SEVERITIES = {"CRITICAL", "MAJOR", "ADVISORY"}
DEFAULT_COMMENT_SEVERITY = "ADVISORY"
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
    stream_response = bool(common_config.get("stream_response"))
    model_endpoint = (
        str(common_config.get("model_endpoint") or "responses").strip().lower()
    )
    dpo_training_data_dir = str(
        common_config.get("dpo_training_data_dir") or ""
    ).strip()

    if not team_name:
        raise ValueError(
            "TEAM_NAME is required. Pass --team-name (identifier for your team to the LLM model)."
        )

    if not dpo_training_data_dir:
        raise ValueError(
            "DPO training data directory is required. Pass --dpo-training-data-dir."
        )

    if not draft_model:
        raise ValueError("DRAFT_MODEL is required. Pass --draft-model.")

    dpo_training_data_file = resolve_dpo_training_data_file_path(
        team_name=team_name,
        dpo_training_data_dir=dpo_training_data_dir,
    )

    return {
        "team_name": team_name,
        "draft_model": draft_model,
        "stream_response": stream_response,
        "model_endpoint": model_endpoint,
        "dpo_training_data_file": dpo_training_data_file,
    }


def _build_runtime_overrides(
    team_name,
    draft_model,
    stream_response,
    dpo_training_data_dir,
    vcs_base_url=None,
    vcs_project_key=None,
    vcs_repo_slug=None,
    vcs_token=None,
    litellm_base_url=None,
    litellm_proxy_url=None,
    litellm_api_key=None,
    litellm_reasoning_effort=None,
):
    return {
        "team_name": team_name,
        "draft_model": draft_model,
        "stream_response": stream_response,
        "dpo_training_data_dir": dpo_training_data_dir,
        "vcs_base_url": vcs_base_url,
        "vcs_project_key": vcs_project_key,
        "vcs_repo_slug": vcs_repo_slug,
        "vcs_token": vcs_token,
        "litellm_base_url": litellm_base_url,
        "litellm_proxy_url": litellm_proxy_url,
        "litellm_api_key": litellm_api_key,
        "litellm_reasoning_effort": litellm_reasoning_effort,
    }


def fetch_pr_activities(vcs_client, pr_id, limit=ACTIVITIES_FETCH_LIMIT):
    return vcs_client.fetch_pr_activities(pr_id, limit=limit)


def fetch_pr_metadata(vcs_client, pr_id):
    return vcs_client.fetch_pr_metadata(pr_id)


def _is_bot_comment_text(text, team_name=""):
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


def _is_summary_comment_text(text, team_name=""):
    if not _is_bot_comment_text(text, team_name):
        return False
    return "**Verdict:**" in text and "**Summary:**" in text and "**Checklist**" in text


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


def _extract_comment_severity(text, file_path=None):
    inline_body = (text or "").split("\n\n###", 1)[0].strip()
    if not inline_body:
        return DEFAULT_COMMENT_SEVERITY

    match = SEVERITY_PREFIX_PATTERN.match(inline_body)
    if not match:
        return DEFAULT_COMMENT_SEVERITY

    return _resolve_comment_severity(match.group("severity"), file_path)


def _is_line_comment(comment, team_name=""):
    if not isinstance(comment, dict):
        return False

    text = comment.get("text", "")
    if not _is_bot_comment_text(text, team_name) or _is_summary_comment_text(
        text, team_name
    ):
        return False

    anchor = comment.get("anchor")
    if not isinstance(anchor, dict):
        return False

    if not anchor.get("path"):
        return False

    line = anchor.get("line")
    if line is None:
        return False

    try:
        line_value = int(str(line))
    except (TypeError, ValueError):
        return False

    return line_value > 0 and comment.get("id") is not None


def _comment_category(comment, team_name=""):
    if not isinstance(comment, dict):
        return "human-comment"

    text = comment.get("text", "")
    if _is_summary_comment_text(text, team_name):
        return "summary-comment"
    if _is_bot_comment_text(text, team_name):
        return "bot-comment"
    return "human-comment"


def _format_category_counts(category_counts):
    categories = ["bot-comment", "summary-comment", "human-comment"]
    return ", ".join(
        f"{category}={category_counts.get(category, 0)}" for category in categories
    )


def _comment_starts_with(text, max_words=3):
    normalized_text = re.sub(r"\s+", " ", (text or "").strip())
    if not normalized_text:
        return ""

    words = normalized_text.split(" ")
    return " ".join(words[:max_words])


def _format_table_cell(value):
    normalized_value = re.sub(
        r"\s+", " ", str(value if value is not None else "")
    ).strip()
    return normalized_value.replace("|", "\\|")


def _format_comment_reply_count_table(rows):
    aligned_headers = ["comment_id", "category", "replies_count", "llm_sentiment"]
    trailing_header = "starts_with"

    normalized_rows = [
        {
            "comment_id": _format_table_cell(row.get("comment_id", "N/A")),
            "category": _format_table_cell(row.get("category", "unknown")),
            "replies_count": _format_table_cell(row.get("replies_count", 0)),
            "llm_sentiment": _format_table_cell(row.get("llm_sentiment", "")),
            "starts_with": _format_table_cell(row.get("starts_with", "")),
        }
        for row in rows
    ]

    if not normalized_rows:
        normalized_rows.append(
            {
                "comment_id": _format_table_cell("N/A"),
                "category": _format_table_cell("N/A"),
                "replies_count": _format_table_cell(0),
                "llm_sentiment": "",
                "starts_with": "",
            }
        )

    aligned_widths = [
        max(
            len(header),
            max(len(row[header]) for row in normalized_rows),
        )
        for header in aligned_headers
    ]

    def _render_line(col1, col2, col3, col4, starts_with):
        return (
            f"| {col1:<{aligned_widths[0]}} "
            f"| {col2:<{aligned_widths[1]}} "
            f"| {col3:<{aligned_widths[2]}} "
            f"| {col4:<{aligned_widths[3]}} "
            f"| {starts_with} |"
        )

    table_lines = [
        _render_line(
            aligned_headers[0],
            aligned_headers[1],
            aligned_headers[2],
            aligned_headers[3],
            trailing_header,
        ),
        _render_line(
            "-" * aligned_widths[0],
            "-" * aligned_widths[1],
            "-" * aligned_widths[2],
            "-" * aligned_widths[3],
            "---",
        ),
    ]

    for row in normalized_rows:
        table_lines.append(
            _render_line(
                row["comment_id"],
                row["category"],
                row["replies_count"],
                row["llm_sentiment"],
                row["starts_with"],
            )
        )

    return "\n".join(table_lines)


def _normalize_comment_id(comment_id):
    if comment_id is None:
        return None

    normalized_id = str(comment_id).strip()
    return normalized_id or None


def _comment_id(comment):
    if not isinstance(comment, dict):
        return None

    return _normalize_comment_id(comment.get("id"))


def _parent_comment_id(comment):
    if not isinstance(comment, dict):
        return None

    parent = comment.get("parent")
    if isinstance(parent, dict):
        normalized_parent_id = _normalize_comment_id(parent.get("id"))
        if normalized_parent_id:
            return normalized_parent_id

    return _normalize_comment_id(comment.get("parentId"))


def _embedded_replies(comment):
    if not isinstance(comment, dict):
        return []

    nested_comments = comment.get("comments")
    if not isinstance(nested_comments, list):
        return []

    return [
        nested_comment
        for nested_comment in nested_comments
        if isinstance(nested_comment, dict)
    ]


def _build_comment_threads(activities):
    root_comments = []
    replies_by_parent = {}
    seen_reply_keys_by_parent = {}

    def _append_reply(parent_id, reply):
        if not parent_id or not isinstance(reply, dict):
            return

        normalized_parent_id = _normalize_comment_id(parent_id)
        if not normalized_parent_id:
            return

        reply_id = _comment_id(reply)
        dedupe_key = (
            f"id:{reply_id}"
            if reply_id
            else f"fallback:{normalized_parent_id}:{_normalize_text_for_key(reply.get('text', ''))}"
        )

        seen_reply_keys = seen_reply_keys_by_parent.setdefault(
            normalized_parent_id, set()
        )
        if dedupe_key in seen_reply_keys:
            return

        seen_reply_keys.add(dedupe_key)
        replies_by_parent.setdefault(normalized_parent_id, []).append(reply)

    for activity in activities or []:
        if not isinstance(activity, dict) or activity.get("action") != "COMMENTED":
            continue

        comment = activity.get("comment")
        if not isinstance(comment, dict):
            continue

        parent_id = _parent_comment_id(comment)
        if parent_id:
            _append_reply(parent_id, comment)
            continue

        root_comments.append(comment)

        root_comment_id = _comment_id(comment)
        if not root_comment_id:
            continue

        for embedded_reply in _embedded_replies(comment):
            _append_reply(root_comment_id, embedded_reply)

    return root_comments, replies_by_parent


def _normalized_reply_texts(replies):
    normalized_texts = []
    for reply in replies or []:
        if not isinstance(reply, dict):
            continue

        text = (reply.get("text", "") or "").strip()
        if text:
            normalized_texts.append(text)

    return normalized_texts


def _latest_non_empty_reply_text(replies):
    for reply in reversed(replies or []):
        if not isinstance(reply, dict):
            continue

        text = (reply.get("text", "") or "").strip()
        if text:
            return text

    return ""


def _build_rejected_preference_pair(
    prompt_text, rejected_comment_text, replies, comment_id, comment_category
):
    chosen_text = _latest_non_empty_reply_text(replies)
    if not chosen_text:
        logger.warning(
            "Skipping rejected %s thread without a non-empty reply. comment_id=%s",
            comment_category,
            comment_id,
        )
        return None

    return {
        "prompt": prompt_text,
        "chosen": chosen_text,
        "rejected": rejected_comment_text,
    }


def _is_root_comment(comment):
    if not isinstance(comment, dict):
        return False
    return _parent_comment_id(comment) is None


def _extract_dpo_pairs_from_threads(
    top_threads,
    sentiment_by_comment_id,
    prompt_text,
    team_name,
):
    dpo_pairs = []
    metrics = {
        "eligible_bot_comment_count": 0,
        "accepted_count": 0,
        "rejected_count": 0,
        "unsure_count": 0,
        "accepted_human_comment_count": 0,
        "rejected_human_comment_count": 0,
        "unsure_human_comment_count": 0,
    }

    for thread in top_threads:
        comment = thread.get("comment")
        if not isinstance(comment, dict):
            continue

        if not _is_root_comment(comment):
            continue

        category = _comment_category(comment, team_name)
        if category == "summary-comment":
            continue

        comment_text = (comment.get("text", "") or "").strip()
        if not comment_text:
            continue

        replies = thread.get("replies", [])
        comment_id = thread.get("comment_id")
        sentiment = sentiment_by_comment_id.get(comment_id or "", SENTIMENT_UNSURE)

        if category == "human-comment":
            if sentiment == SENTIMENT_ACCEPTED:
                metrics["accepted_human_comment_count"] += 1
                dpo_pairs.append(
                    {
                        "prompt": prompt_text,
                        "chosen": comment_text,
                        "rejected": "N/A",
                    }
                )
            elif sentiment == SENTIMENT_REJECTED:
                metrics["rejected_human_comment_count"] += 1
                rejected_human_pair = _build_rejected_preference_pair(
                    prompt_text,
                    comment_text,
                    replies,
                    comment_id,
                    category,
                )
                if rejected_human_pair:
                    dpo_pairs.append(rejected_human_pair)
            else:
                metrics["unsure_human_comment_count"] += 1
            continue

        if category != "bot-comment":
            continue

        metrics["eligible_bot_comment_count"] += 1
        if sentiment == SENTIMENT_ACCEPTED:
            metrics["accepted_count"] += 1
            dpo_pairs.append(
                {
                    "prompt": prompt_text,
                    "chosen": comment_text,
                    "rejected": "N/A",
                }
            )
            continue

        if sentiment == SENTIMENT_REJECTED:
            metrics["rejected_count"] += 1
            rejected_bot_pair = _build_rejected_preference_pair(
                prompt_text,
                comment_text,
                replies,
                comment_id,
                category,
            )
            if rejected_bot_pair:
                dpo_pairs.append(rejected_bot_pair)
            continue

        metrics["unsure_count"] += 1

    return dpo_pairs, metrics


def _select_top_comment_threads(root_comments, replies_by_parent, limit):
    indexed_threads = []
    for index, comment in enumerate(root_comments):
        comment_id = _comment_id(comment)
        replies = replies_by_parent.get(comment_id, []) if comment_id else []
        indexed_threads.append(
            {
                "index": index,
                "comment": comment,
                "comment_id": comment_id,
                "replies": replies,
                "replies_count": len(replies),
            }
        )

    indexed_threads.sort(key=lambda item: (-item["replies_count"], item["index"]))
    return indexed_threads[: max(0, limit)]


def _build_batched_sentiment_messages(comment_threads, team_name=""):
    thread_payload = []
    for thread in comment_threads:
        comment = thread.get("comment", {})
        replies = thread.get("replies", [])
        comment_text = (
            comment.get("text", "") if isinstance(comment, dict) else ""
        ).strip()
        comment_category = _comment_category(comment, team_name)
        comment_anchor = comment.get("anchor", {}) if isinstance(comment, dict) else {}
        comment_anchor_path = (
            comment_anchor.get("path") if isinstance(comment_anchor, dict) else None
        )
        comment_severity = (
            _extract_comment_severity(comment_text, comment_anchor_path)
            if comment_category == "bot-comment"
            else ""
        )
        thread_payload.append(
            {
                "comment_id": thread.get("comment_id"),
                "category": comment_category,
                "severity": comment_severity,
                "comment_text": comment_text,
                "replies": _normalized_reply_texts(replies),
                "replies_count": thread.get("replies_count", 0),
            }
        )

    return [
        {
            "role": "system",
            "content": (
                "You classify pull-request comment thread sentiment. "
                "Return strict JSON only."
            ),
        },
        {
            "role": "user",
            "content": (
                "Classify each comment thread sentiment as ACCEPTED, REJECTED, or UNSURE. "
                "Use the replies as evidence and return one result per comment_id.\n\n"
                f"Threads:\n{json.dumps(thread_payload, ensure_ascii=False)}\n\n"
                "Return JSON in this exact shape: "
                '{"results": [{"comment_id": "<id>", "sentiment": "ACCEPTED|REJECTED|UNSURE"}]}'
            ),
        },
    ]


def _parse_batched_sentiment_response(response):
    if not isinstance(response, dict):
        streamed_content = extract_content_from_stream_response(response)
        response = {"choices": [{"message": {"content": streamed_content}}]}

    return parse_batched_sentiment_response(
        response=response,
        normalize_comment_id=_normalize_comment_id,
        valid_sentiments=VALID_SENTIMENTS,
    )


def _resolve_thread_sentiments_with_llm(
    comment_threads,
    draft_model=None,
    model_endpoint="responses",
    stream_response=False,
    team_name="",
):
    if not comment_threads:
        return {}

    resolved_draft_model = str(draft_model or "")
    resolved_model_endpoint = str(model_endpoint or "responses").strip().lower()
    logger.info(
        "Invoking batched LLM sentiment classification. model=%s endpoint=%s thread_count=%s stream=%s",
        resolved_draft_model,
        resolved_model_endpoint,
        len(comment_threads),
        stream_response,
    )
    try:
        model_messages = _build_batched_sentiment_messages(comment_threads, team_name)

        if resolved_model_endpoint == "responses":
            response = responses(
                model=resolved_draft_model,
                input_items=model_messages,
                stream=stream_response,
            )
        else:
            if resolved_model_endpoint != "chat_completions":
                logger.warning(
                    "Unknown MODEL_ENDPOINT=%s. Falling back to chat_completions for distillation.",
                    resolved_model_endpoint,
                )

            response = chat_completions(
                model=resolved_draft_model,
                messages=model_messages,
                stream=stream_response,
            )

        sentiment_by_comment_id = _parse_batched_sentiment_response(response)
        logger.info(
            "Batched LLM sentiment classification complete. resolved_threads=%s unresolved_threads=%s",
            len(sentiment_by_comment_id),
            max(0, len(comment_threads) - len(sentiment_by_comment_id)),
        )
        return sentiment_by_comment_id
    except LiteLLMResponseParseError:
        logger.warning(
            "Batched LLM sentiment classification returned unparsable response. Skipping thread sentiment resolution."
        )
        return {}
    except Exception:
        logger.warning(
            "Batched LLM sentiment classification failed. Skipping thread sentiment resolution.",
            exc_info=True,
        )
        return {}


def _normalize_text_for_key(text):
    return re.sub(r"\s+", " ", (text or "").strip())


def _build_dpo_key(entry):
    return "|".join(
        [
            _normalize_text_for_key(entry.get("prompt", "")),
            _normalize_text_for_key(entry.get("chosen", "")),
            _normalize_text_for_key(entry.get("rejected", "")),
        ]
    )


def _load_existing_dpo_keys(file_path):
    if not os.path.exists(file_path):
        logger.info(
            "DPO data file does not exist yet; treating as empty. file=%s", file_path
        )
        return set()

    keys = set()
    with open(file_path, "r", encoding="utf-8") as existing_file:
        for line in existing_file:
            stripped = (line or "").strip()
            if not stripped:
                continue

            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError:
                logger.warning(
                    "Skipping malformed JSONL row while deduplicating DPO data"
                )
                continue

            if isinstance(parsed, dict):
                keys.add(_build_dpo_key(parsed))

    logger.info(
        "Loaded existing DPO keys for deduplication. file=%s keys=%s",
        file_path,
        len(keys),
    )
    return keys


def _filter_unique_dpo_pairs(entries, existing_keys):
    unique_entries = []
    seen_keys = set(existing_keys)

    for entry in entries:
        key = _build_dpo_key(entry)
        if key in seen_keys:
            continue

        seen_keys.add(key)
        unique_entries.append(entry)

    return unique_entries


def is_noisy_pr_title(title):
    normalized = (title or "").strip()
    if not normalized:
        return True

    normalized_lower = normalized.lower()
    if len(normalized) < 10:
        return True

    noisy_titles = {
        "wip",
        "draft",
        "update",
        "changes",
        "minor changes",
        "fix",
        "bug fix",
        "misc",
        "temp",
    }

    if normalized_lower in noisy_titles:
        return True

    # Generic-only title without useful detail is treated as noisy.
    if re.fullmatch(
        r"(fix|update|refactor|cleanup|changes?)(\s+\w+)?", normalized_lower
    ):
        return True

    return False


def build_prompt(diff_text, pr_title):
    if is_noisy_pr_title(pr_title):
        return diff_text
    return f"PR Title: {pr_title}\n\n{diff_text}"


def convert_to_unified_diff(json_diff_data):
    unified_diff = []
    diff_entries = (json_diff_data or {}).get("diffs") or []
    processed_files = 0
    skipped_files = 0
    hunk_count = 0
    line_count = 0

    for diff in diff_entries:
        if not isinstance(diff, dict):
            continue

        source_info = diff.get("source") or {}
        destination_info = diff.get("destination") or {}

        source = (
            source_info.get("toString", "/dev/null")
            if isinstance(source_info, dict)
            else "/dev/null"
        )
        dest = (
            destination_info.get("toString", "/dev/null")
            if isinstance(destination_info, dict)
            else "/dev/null"
        )

        source = source or "/dev/null"
        dest = dest or "/dev/null"

        if any(dest.endswith(ext) for ext in DIFF_SKIP_EXTENSIONS):
            skipped_files += 1
            continue

        processed_files += 1
        unified_diff.append(f"--- {source}")
        unified_diff.append(f"+++ {dest}")
        for hunk in diff.get("hunks") or []:
            if not isinstance(hunk, dict):
                continue

            hunk_count += 1
            unified_diff.append(
                f"@@ -{hunk.get('sourceLine', 0)},{hunk.get('sourceSpan', 0)} +{hunk.get('destinationLine', 0)},{hunk.get('destinationSpan', 0)} @@"
            )
            for seg in hunk.get("segments") or []:
                if not isinstance(seg, dict):
                    continue

                segment_type = seg.get("type")
                segment_type = (
                    segment_type if isinstance(segment_type, str) else "CONTEXT"
                )
                prefix = {"ADDED": "+", "REMOVED": "-", "CONTEXT": " "}.get(
                    segment_type, " "
                )
                for line in seg.get("lines") or []:
                    line_text = (
                        line.get("line", "")
                        if isinstance(line, dict)
                        else str(line or "")
                    )
                    unified_diff.append(f"{prefix}{line_text}")
                    line_count += 1

    unified_text = "\n".join(unified_diff)
    logger.info(
        "Unified diff conversion complete. total_diffs=%s processed_files=%s skipped_files=%s hunks=%s lines=%s chars=%s",
        len(diff_entries),
        processed_files,
        skipped_files,
        hunk_count,
        line_count,
        len(unified_text),
    )
    return unified_text


def append_jsonl_entries_atomic(file_path, entries):
    directory = os.path.dirname(file_path) or "."
    os.makedirs(directory, exist_ok=True)

    with tempfile.NamedTemporaryFile(
        mode="w", delete=False, dir=directory, encoding="utf-8"
    ) as temp_file:
        temp_path = temp_file.name

        if os.path.exists(file_path):
            with open(file_path, "r", encoding="utf-8") as existing_file:
                temp_file.write(existing_file.read())

        for entry in entries:
            temp_file.write(json.dumps(entry) + "\n")

    os.replace(temp_path, file_path)


def run(
    vcs_type=None,
    pr_id=None,
    team_name=None,
    draft_model=None,
    stream_response=None,
    dpo_training_data_dir=None,
    vcs_base_url=None,
    vcs_project_key=None,
    vcs_repo_slug=None,
    vcs_token=None,
    litellm_base_url=None,
    litellm_proxy_url=None,
    litellm_api_key=None,
    litellm_reasoning_effort=None,
):
    runtime_overrides = _build_runtime_overrides(
        team_name=team_name,
        draft_model=draft_model,
        stream_response=stream_response,
        dpo_training_data_dir=dpo_training_data_dir,
        vcs_base_url=vcs_base_url,
        vcs_project_key=vcs_project_key,
        vcs_repo_slug=vcs_repo_slug,
        vcs_token=vcs_token,
        litellm_base_url=litellm_base_url,
        litellm_proxy_url=litellm_proxy_url,
        litellm_api_key=litellm_api_key,
        litellm_reasoning_effort=litellm_reasoning_effort,
    )
    set_runtime_overrides(runtime_overrides)

    try:
        runtime_settings = _resolve_runtime_settings(runtime_overrides)
        run_team_name = runtime_settings["team_name"]
        run_draft_model = runtime_settings["draft_model"]
        run_stream_response = runtime_settings["stream_response"]
        run_model_endpoint = runtime_settings["model_endpoint"]
        run_dpo_training_data_file = runtime_settings["dpo_training_data_file"]
        logger.info("Distillation run started.")
        vcs_client = get_vcs_client(
            vcs_type=vcs_type,
            config_overrides=runtime_overrides,
        )
        pr_id = pr_id if pr_id is not None else vcs_client.get_vcs_config().get("pr_id")
        if not pr_id:
            raise ValueError("PR id is required. Set VCS_PR_ID.")

        diff_data = vcs_client.fetch_pr_diff(pr_id)
        diff_count = len((diff_data or {}).get("diffs") or [])
        logger.info(
            "Fetched PR diff for distillation. pr_id=%s diff_entries=%s",
            pr_id,
            diff_count,
        )

        clean_diff = convert_to_unified_diff(diff_data)
        if not clean_diff.strip():
            logger.info("Unified diff is empty after filtering. pr_id=%s", pr_id)

        pr_title = ""
        try:
            pr_title, _ = fetch_pr_metadata(vcs_client, pr_id)
        except Exception:
            logger.warning(
                "Skipping PR title enrichment due to metadata fetch failure.",
                exc_info=True,
            )

        title_used_in_prompt = not is_noisy_pr_title(pr_title)
        prompt_text = build_prompt(clean_diff, pr_title)
        logger.info(
            "Prepared distillation prompt. pr_id=%s title_used=%s",
            pr_id,
            title_used_in_prompt,
        )

        activities = {
            "values": fetch_pr_activities(
                vcs_client,
                pr_id,
                limit=ACTIVITIES_FETCH_LIMIT,
            )
        }
        activity_values = activities.get("values", [])
        logger.info(
            "Fetched PR activities for distillation. pr_id=%s activity_count=%s",
            pr_id,
            len(activity_values),
        )

        root_comments, replies_by_parent = _build_comment_threads(activity_values)
        total_reply_comments = sum(
            len(thread_replies) for thread_replies in replies_by_parent.values()
        )
        logger.info(
            "Prepared comment threads for distillation. pr_id=%s root_comments=%s reply_comments=%s",
            pr_id,
            len(root_comments),
            total_reply_comments,
        )

        candidate_comments = [
            comment
            for comment in root_comments
            if _comment_category(comment, run_team_name) != "summary-comment"
        ]
        top_threads = _select_top_comment_threads(
            candidate_comments,
            replies_by_parent,
            limit=MAX_LLM_THREADS,
        )

        logger.info(
            "Selected top comment threads for LLM sentiment classification. pr_id=%s selected_threads=%s max_threads=%s",
            pr_id,
            len(top_threads),
            MAX_LLM_THREADS,
        )

        sentiment_by_comment_id = _resolve_thread_sentiments_with_llm(
            top_threads,
            draft_model=run_draft_model,
            model_endpoint=run_model_endpoint,
            stream_response=run_stream_response,
            team_name=run_team_name,
        )

        comment_reply_rows = []
        category_counts = {"bot-comment": 0, "summary-comment": 0, "human-comment": 0}
        for comment in root_comments:
            category = _comment_category(comment, run_team_name)
            category_counts[category] = category_counts.get(category, 0) + 1

            comment_id = _comment_id(comment)
            replies = replies_by_parent.get(comment_id, []) if comment_id else []
            comment_text = comment.get("text", "") if isinstance(comment, dict) else ""

            comment_reply_rows.append(
                {
                    "comment_id": comment_id if comment_id is not None else "N/A",
                    "category": category,
                    "replies_count": len(replies),
                    "llm_sentiment": sentiment_by_comment_id.get(comment_id or "", ""),
                    "starts_with": _comment_starts_with(comment_text),
                }
            )

        dpo_pairs, extraction_metrics = _extract_dpo_pairs_from_threads(
            top_threads,
            sentiment_by_comment_id,
            prompt_text,
            run_team_name,
        )

        logger.info(
            "Comment reply count table. pr_id=%s\n%s",
            pr_id,
            _format_comment_reply_count_table(comment_reply_rows),
        )

        logger.info(
            "Comment category breakdown. pr_id=%s categories={%s}",
            pr_id,
            _format_category_counts(category_counts),
        )

        logger.info(
            "Distillation extraction summary. pr_id=%s selected_threads=%s eligible_bot_comments=%s accepted=%s rejected=%s unsure=%s accepted_human_comments=%s rejected_human_comments=%s unsure_human_comments=%s generated_pairs=%s",
            pr_id,
            len(top_threads),
            extraction_metrics["eligible_bot_comment_count"],
            extraction_metrics["accepted_count"],
            extraction_metrics["rejected_count"],
            extraction_metrics["unsure_count"],
            extraction_metrics["accepted_human_comment_count"],
            extraction_metrics["rejected_human_comment_count"],
            extraction_metrics["unsure_human_comment_count"],
            len(dpo_pairs),
        )

        if dpo_pairs:
            existing_keys = _load_existing_dpo_keys(run_dpo_training_data_file)
            unique_dpo_pairs = _filter_unique_dpo_pairs(dpo_pairs, existing_keys)
            skipped_duplicate_count = len(dpo_pairs) - len(unique_dpo_pairs)
            logger.info(
                "Deduplication summary. pr_id=%s generated_pairs=%s existing_keys=%s unique_pairs=%s skipped_duplicates=%s",
                pr_id,
                len(dpo_pairs),
                len(existing_keys),
                len(unique_dpo_pairs),
                skipped_duplicate_count,
            )

            if unique_dpo_pairs:
                append_jsonl_entries_atomic(
                    run_dpo_training_data_file,
                    unique_dpo_pairs,
                )
                logger.info(
                    "Successfully saved %s DPO samples. Skipped duplicates=%s",
                    len(unique_dpo_pairs),
                    skipped_duplicate_count,
                )
            else:
                logger.info(
                    "No new DPO samples to save after deduplication. skipped_duplicates=%s",
                    skipped_duplicate_count,
                )
        else:
            if not root_comments:
                logger.info(
                    "Distillation produced no DPO samples. reason=no_root_comments pr_id=%s",
                    pr_id,
                )
            elif len(top_threads) == 0:
                logger.info(
                    "Distillation produced no DPO samples. reason=no_threads_selected_for_llm pr_id=%s",
                    pr_id,
                )
            else:
                logger.info(
                    "Distillation produced no DPO samples. reason=no_pairs_generated pr_id=%s",
                    pr_id,
                )

        logger.info("Distillation run completed. pr_id=%s", pr_id)

    except Exception:
        logger.exception("Distillation run failed. pr_id=%s", pr_id)
    finally:
        clear_runtime_overrides()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Distill pull request feedback into DPO training samples"
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
            "Draft model used across distill/refine flows "
            "(overrides model.draft_model in reflex_reviewer.toml)"
        ),
    )
    parser.add_argument(
        "--stream-response",
        type=_parse_bool,
        default=None,
        help="Enable streaming responses (overrides model.stream_response from reflex_reviewer.toml)",
    )
    parser.add_argument(
        "--dpo-training-data-dir",
        required=True,
        help=(
            "Parent directory for DPO training data. Reflex Reviewer reads/writes "
            "the team-specific dataset file as "
            "<dir>/{sanitized_team_name}_dpo_training_data.jsonl."
        ),
    )
    parser.add_argument("--vcs-base-url", help="Override VCS_BASE_URL")
    parser.add_argument("--vcs-project-key", help="Override VCS_PROJECT_KEY")
    parser.add_argument("--vcs-repo-slug", help="Override VCS_REPO_SLUG")
    parser.add_argument("--vcs-token", help="Override VCS_TOKEN")
    parser.add_argument("--litellm-base-url", help="Override LITELLM_BASE_URL")
    parser.add_argument("--litellm-proxy-url", help="Override LITELLM_PROXY_URL")
    parser.add_argument("--litellm-api-key", help="Override LITELLM_API_KEY")
    parser.add_argument(
        "--litellm-reasoning-effort",
        help="LiteLLM reasoning effort: low|medium|high (defaults to env or high)",
    )
    args = parser.parse_args()
    run(
        vcs_type=args.vcs_type,
        pr_id=args.pr_id,
        team_name=args.team_name,
        draft_model=args.draft_model,
        stream_response=args.stream_response,
        dpo_training_data_dir=args.dpo_training_data_dir,
        vcs_base_url=args.vcs_base_url,
        vcs_project_key=args.vcs_project_key,
        vcs_repo_slug=args.vcs_repo_slug,
        vcs_token=args.vcs_token,
        litellm_base_url=args.litellm_base_url,
        litellm_proxy_url=args.litellm_proxy_url,
        litellm_api_key=args.litellm_api_key,
        litellm_reasoning_effort=args.litellm_reasoning_effort,
    )
