import logging
import re
from pathlib import Path
from typing import Optional, cast

import requests  # type: ignore[reportMissingImports,reportMissingModuleSource]

from .state import ReviewGraphState

logger = logging.getLogger(__name__)


class ReviewGraphNodes:
    DEFAULT_COMMENT_SEVERITY = "ADVISORY"
    ALLOWED_COMMENT_SEVERITIES = {"CRITICAL", "MAJOR", "ADVISORY"}
    SUMMARY_COMMENT_MARKER = "<!-- reflex-reviewer-summary -->"
    SUMMARY_COMMENT_SECTIONS = (
        ("**Recommendation:**", "**Review Summary:**", "**Checklist**"),
    )
    SEVERITY_PREFIX_PATTERN = re.compile(
        r"^\[(?P<severity>[^\]]+)\]\s*(?P<body>.*)$", re.DOTALL
    )
    BOT_SIGNATURE_PATTERN = re.compile(
        r"(?:\r?\n){2}\s*###\s*#?[A-Za-z0-9._-]+\s*$",
        re.DOTALL,
    )
    COMMENT_TOKEN_STOPWORDS = {
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
    MIN_DUPLICATE_TOKEN_INTERSECTION = 2
    MIN_DUPLICATE_TOKEN_OVERLAP = 0.75

    def __init__(
        self,
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
        model_endpoint,
    ):
        """Store all injected collaborators, limits, and prompt locations for graph nodes."""
        self._resolve_runtime_settings = resolve_runtime_settings
        self._get_vcs_client = get_vcs_client
        self._resolve_repository_path = resolve_repository_path
        self._extract_changed_file_paths_from_diff = (
            extract_changed_file_paths_from_diff
        )
        self._build_repo_map_for_changed_files = build_repo_map_for_changed_files
        self._retrieve_related_files_context = retrieve_related_files_context
        self._retrieve_bounded_code_search_context = (
            retrieve_bounded_code_search_context
        )
        self._compose_repository_context_bundle = compose_repository_context_bundle
        self._repository_path = repository_path
        self._max_changed_files = max_changed_files
        self._max_repo_map_files = max_repo_map_files
        self._max_repo_map_chars = max_repo_map_chars
        self._max_related_files = max_related_files
        self._max_related_files_chars = max_related_files_chars
        self._max_code_search_results = max_code_search_results
        self._max_code_search_chars = max_code_search_chars
        self._max_code_search_query_terms = max_code_search_query_terms
        self._repository_ignore_directories = repository_ignore_directories
        self._convert_to_unified_diff_and_anchor_index = (
            convert_to_unified_diff_and_anchor_index
        )
        self._truncate_diff = truncate_diff
        self._fetch_pr_metadata = fetch_pr_metadata
        self._fetch_pr_activities = fetch_pr_activities
        self._build_existing_feedback_context = build_existing_feedback_context
        self._build_review_purpose = build_review_purpose
        self._build_previous_response_id = build_previous_response_id
        self._normalize_comment_severity = normalize_comment_severity
        self._resolve_comment_severity = resolve_comment_severity
        self._resolve_anchor_by_id = resolve_anchor_by_id
        self._post_inline_comment = post_inline_comment
        self._upsert_summary_comment = upsert_summary_comment
        self._model_endpoint = str(model_endpoint or "responses").strip().lower()
        self._prompts_dir = Path(__file__).resolve().parent.parent / "prompts"

    @staticmethod
    def _get_required_state_value(state: ReviewGraphState, key, default=""):
        """Return a state value, falling back when the key is missing or None."""
        value = state.get(key)
        if value is None:
            return default
        return value

    @staticmethod
    def _state_pr_id(state: ReviewGraphState):
        """Return the PR id from state, or 'unknown' when unavailable."""
        pr_id = state.get("pr_id")
        return pr_id if pr_id is not None else "unknown"

    @staticmethod
    def _collect_context_paths(text, split_token: Optional[str] = "|"):
        """Extract unique bullet-list paths from a context block."""
        paths = []
        for line in str(text or "").splitlines():
            stripped = line.strip()
            if not stripped.startswith("- "):
                continue
            payload = stripped[2:]
            if split_token and split_token in payload:
                payload = payload.split(split_token, 1)[0]
            payload = payload.strip()
            if not payload:
                continue
            if payload not in paths:
                paths.append(payload)
        return paths

    @staticmethod
    def _extract_code_search_terms(code_search_context):
        """Parse the leading 'Search terms:' line from code-search context output."""
        first_line = str(code_search_context or "").splitlines()[0:1]
        if not first_line:
            return []
        line = first_line[0].strip()
        if not line.lower().startswith("search terms:"):
            return []
        terms = line.split(":", 1)[-1].strip()
        if not terms:
            return []
        return [term.strip() for term in terms.split(",") if term.strip()]

    @staticmethod
    def _build_context_preview(text, max_chars=600):
        """Collapse whitespace and trim context text for safe log previews."""
        normalized = re.sub(r"\s+", " ", str(text or "")).strip()
        if len(normalized) <= max_chars:
            return normalized
        return normalized[: max_chars - 16].rstrip() + "... [truncated]"

    @staticmethod
    def _normalize_char_limit(limit_value):
        """Return a safe non-negative integer char limit for aggregate usage logs."""
        try:
            normalized = int(limit_value)
        except (TypeError, ValueError):
            return 0
        return normalized if normalized > 0 else 0

    @staticmethod
    def _estimate_tokens_from_chars(char_count):
        """Estimate tokens from character count using a lightweight 4 chars/token heuristic."""
        try:
            safe_chars = max(0, int(char_count or 0))
        except (TypeError, ValueError):
            safe_chars = 0
        return (safe_chars + 3) // 4

    @staticmethod
    def _safe_int(value, default=0):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _resolve_comment_severity_with_context(self, severity, file_path=None, comment_text=None):
        """Resolve severity with best-effort compatibility for resolver signatures."""
        try:
            return self._resolve_comment_severity(severity, file_path, comment_text)
        except TypeError:
            return self._resolve_comment_severity(severity, file_path)

    @staticmethod
    def _normalize_repo_path(file_path):
        normalized = str(file_path or "").strip().replace("\\", "/")
        normalized = re.sub(r"^(?:\./)+", "", normalized)
        normalized = re.sub(r"^(?:a|b)/", "", normalized)
        normalized = normalized.lstrip("/")
        return normalized.lower()

    @classmethod
    def _is_root_comment(cls, comment):
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

    @classmethod
    def _is_bot_comment_text(cls, text, team_name):
        if not text:
            return False

        normalized_team_name = str(team_name or "").strip()
        if not normalized_team_name:
            return False

        hashtag_team_name = normalized_team_name.lstrip("#")
        markers = {f"### {normalized_team_name}"}
        if hashtag_team_name:
            markers.add(f"### #{hashtag_team_name}")

        return any(marker in str(text) for marker in markers)

    @classmethod
    def _is_summary_comment_text(cls, text, team_name):
        text_value = str(text or "")
        if cls.SUMMARY_COMMENT_MARKER in text_value:
            return True

        if not cls._is_bot_comment_text(text_value, team_name):
            return False

        return any(
            all(section in text_value for section in summary_section)
            for summary_section in cls.SUMMARY_COMMENT_SECTIONS
        )

    @classmethod
    def _strip_bot_signature(cls, text):
        return cls.BOT_SIGNATURE_PATTERN.sub("", str(text or "").strip()).strip()

    @classmethod
    def _parse_inline_comment_payload(cls, text):
        inline_body = cls._strip_bot_signature(text)
        if not inline_body:
            return cls.DEFAULT_COMMENT_SEVERITY, ""

        match = cls.SEVERITY_PREFIX_PATTERN.match(inline_body)
        if not match:
            return cls.DEFAULT_COMMENT_SEVERITY, inline_body

        severity = str(match.group("severity") or "").strip().upper()
        if severity not in cls.ALLOWED_COMMENT_SEVERITIES:
            severity = cls.DEFAULT_COMMENT_SEVERITY
        body = str(match.group("body") or "").strip()
        return severity, body

    @staticmethod
    def _resolve_existing_inline_anchor_location(comment):
        if not isinstance(comment, dict):
            return None, None

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

    @classmethod
    def _normalize_comment_text_for_fingerprint(cls, text):
        normalized = cls._strip_bot_signature(text).lower()
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
            if len(token) >= 3 and token not in cls.COMMENT_TOKEN_STOPWORDS
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
        if intersection_count < cls.MIN_DUPLICATE_TOKEN_INTERSECTION:
            return False

        min_token_count = min(len(left_tokens), len(right_tokens))
        if min_token_count <= 0:
            return False

        overlap_ratio = intersection_count / float(min_token_count)
        return overlap_ratio >= cls.MIN_DUPLICATE_TOKEN_OVERLAP

    def _extract_existing_bot_inline_comments(self, activities, team_name):
        extracted_comments = []
        seen = set()

        for activity in activities or []:
            if not isinstance(activity, dict):
                continue
            if activity.get("action") != "COMMENTED":
                continue

            comment = activity.get("comment")
            if not isinstance(comment, dict):
                continue
            if not self._is_root_comment(comment):
                continue

            raw_text = str(comment.get("text") or "")
            if not raw_text.strip():
                continue
            if self._is_summary_comment_text(raw_text, team_name):
                continue
            if not self._is_bot_comment_text(raw_text, team_name):
                continue

            anchor_path, anchor_line = self._resolve_existing_inline_anchor_location(
                comment
            )
            normalized_path = self._normalize_repo_path(anchor_path)
            normalized_line = self._safe_int(anchor_line, default=0)
            if not normalized_path or normalized_line <= 0:
                continue

            severity, inline_body = self._parse_inline_comment_payload(raw_text)
            normalized_text = self._strip_bot_signature(inline_body or raw_text)
            if not normalized_text:
                continue

            fingerprint_tokens = sorted(self._token_set_for_similarity(normalized_text))
            fingerprint = " ".join(
                fingerprint_tokens
            ) or self._normalize_comment_text_for_fingerprint(normalized_text)
            if not fingerprint:
                continue

            dedupe_key = (normalized_path, normalized_line, fingerprint)
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)

            extracted_comments.append(
                {
                    "path": anchor_path,
                    "line": normalized_line,
                    "severity": severity,
                    "text": normalized_text,
                    "fingerprint": fingerprint,
                }
            )

        return extracted_comments

    def _log_node_start(self, node_name, state: ReviewGraphState):
        """Write a minimal start log for the given review-graph node."""
        logger.info(
            "Review node started. node=%s pr_id=%s",
            node_name,
            self._state_pr_id(state),
        )

    def _log_node_complete(self, node_name, state: ReviewGraphState, **metrics):
        """Write a completion log for the node with compact metric details."""
        logger.info(
            "Review node completed. node=%s pr_id=%s metrics=%s",
            node_name,
            self._state_pr_id(state),
            metrics,
        )

    def _is_halted(self, node_name, state: ReviewGraphState):
        """Return True and log skip when the flow is halted; otherwise False."""
        if not state.get("halt"):
            return False

        logger.info(
            "Review node skipped. node=%s pr_id=%s reason=halted",
            node_name,
            self._state_pr_id(state),
        )
        return True

    def fetch_pr_context(self, state: ReviewGraphState) -> ReviewGraphState:
        """Fetch PR runtime context and return the base state for downstream nodes.

        This node resolves runtime settings, fetches PR diff and anchors, truncates
        diff text for prompt safety, and gathers metadata, activities, and purpose.
        It halts the flow when no reviewable diff remains.
        """
        node_name = "fetch_pr_context"
        if self._is_halted(node_name, state):
            return cast(ReviewGraphState, {})

        self._log_node_start(node_name, state)

        runtime_overrides = state.get("runtime_overrides")
        runtime_settings = self._resolve_runtime_settings(runtime_overrides)
        run_team_name = runtime_settings["team_name"]
        run_draft_model = runtime_settings["draft_model"]
        run_judge_model = runtime_settings["judge_model"]
        run_stream_response = runtime_settings["stream_response"]

        logger.info("Review run started.")
        vcs_client = self._get_vcs_client(
            vcs_type=state.get("vcs_type"),
            config_overrides=runtime_overrides,
        )
        vcs_config = vcs_client.get_vcs_config()
        pr_id = (
            state.get("pr_id")
            if state.get("pr_id") is not None
            else vcs_config.get("pr_id")
        )
        if not pr_id:
            raise ValueError("PR id is required. Set VCS_PR_ID.")

        logger.info("Fetching PR diff for review. pr_id=%s", pr_id)
        raw_diff_data = vcs_client.fetch_pr_diff(pr_id)
        raw_git_diff, anchor_index = self._convert_to_unified_diff_and_anchor_index(
            raw_diff_data
        )
        safe_diff = self._truncate_diff(raw_git_diff)

        if not safe_diff.strip():
            logger.warning(
                "No reviewable code changes after filtering. pr_id=%s", pr_id
            )
            result: ReviewGraphState = {
                "halt": True,
                "pr_id": pr_id,
                "vcs_client": vcs_client,
                "vcs_config": vcs_config,
                "team_name": run_team_name,
                "draft_model": run_draft_model,
                "judge_model": run_judge_model,
                "stream_response": run_stream_response,
                "model_endpoint": self._model_endpoint,
            }
            self._log_node_complete(node_name, result, halted=True, safe_diff_chars=0)
            return result

        pr_title, pr_description = self._fetch_pr_metadata(vcs_client, pr_id)
        activities = self._fetch_pr_activities(vcs_client, pr_id)
        existing_feedback = self._build_existing_feedback_context(
            activities, run_team_name
        )
        existing_bot_inline_comments = self._extract_existing_bot_inline_comments(
            activities,
            run_team_name,
        )
        review_purpose = self._build_review_purpose(pr_title, pr_description)
        state_key = self._build_previous_response_id(vcs_config or {}, pr_id)

        result: ReviewGraphState = {
            "halt": False,
            "pr_id": pr_id,
            "repository_path": self._resolve_repository_path(self._repository_path),
            "vcs_client": vcs_client,
            "vcs_config": vcs_config,
            "team_name": run_team_name,
            "draft_model": run_draft_model,
            "judge_model": run_judge_model,
            "stream_response": run_stream_response,
            "model_endpoint": self._model_endpoint,
            "raw_diff_data": raw_diff_data,
            "safe_diff": safe_diff,
            "anchor_index": anchor_index,
            "pr_title": pr_title,
            "pr_description": pr_description,
            "existing_feedback": existing_feedback,
            "existing_bot_inline_comments": existing_bot_inline_comments,
            "review_purpose": review_purpose,
            "state_key": state_key,
        }
        self._log_node_complete(
            node_name,
            result,
            halted=False,
            safe_diff_chars=len(safe_diff),
            anchor_count=len(anchor_index.get("by_anchor_id", {})),
            existing_bot_inline_comments=len(existing_bot_inline_comments),
        )
        return result

    def extract_changed_files(self, state: ReviewGraphState) -> ReviewGraphState:
        """Build the changed-file path list from raw diff data for context stages."""
        node_name = "extract_changed_files"
        if self._is_halted(node_name, state):
            return cast(ReviewGraphState, {})

        self._log_node_start(node_name, state)
        changed_file_paths = self._extract_changed_file_paths_from_diff(
            state.get("raw_diff_data", {}),
            max_files=self._max_changed_files,
        )
        result: ReviewGraphState = {
            "changed_file_paths": changed_file_paths,
        }
        self._log_node_complete(
            node_name,
            state,
            changed_files=len(changed_file_paths),
        )
        return result

    def build_repo_map(self, state: ReviewGraphState) -> ReviewGraphState:
        """Build repository-map context from changed files using configured limits."""
        node_name = "build_repo_map"
        if self._is_halted(node_name, state):
            return cast(ReviewGraphState, {})

        self._log_node_start(node_name, state)
        repo_map = self._build_repo_map_for_changed_files(
            state.get("repository_path"),
            state.get("changed_file_paths", []),
            max_files=self._max_repo_map_files,
            max_chars=self._max_repo_map_chars,
        )

        result: ReviewGraphState = {"repo_map": repo_map}
        repo_paths = self._collect_context_paths(repo_map, split_token="|")
        logger.info(
            "Repository context repo map prepared. pr_id=%s entries=%s sample_paths=%s preview=%s",
            self._state_pr_id(state),
            len(repo_paths),
            repo_paths[:5],
            self._build_context_preview(repo_map),
        )
        self._log_node_complete(node_name, state, repo_map_chars=len(repo_map or ""))
        return result

    def retrieve_related_files(self, state: ReviewGraphState) -> ReviewGraphState:
        """Build related-file context snippets inferred from changed files."""
        node_name = "retrieve_related_files"
        if self._is_halted(node_name, state):
            return cast(ReviewGraphState, {})

        self._log_node_start(node_name, state)
        related_files_context = self._retrieve_related_files_context(
            state.get("repository_path"),
            state.get("changed_file_paths", []),
            max_related_files=self._max_related_files,
            max_chars=self._max_related_files_chars,
        )

        result: ReviewGraphState = {
            "related_files_context": related_files_context,
        }
        related_paths = self._collect_context_paths(
            related_files_context, split_token=None
        )
        logger.info(
            "Repository context related files prepared. pr_id=%s files=%s sample_paths=%s preview=%s",
            self._state_pr_id(state),
            len(related_paths),
            related_paths[:5],
            self._build_context_preview(related_files_context),
        )
        self._log_node_complete(
            node_name,
            state,
            related_files_chars=len(related_files_context or ""),
        )
        return result

    def retrieve_code_search_context(self, state: ReviewGraphState) -> ReviewGraphState:
        """Build bounded code-search context from deterministic term matches."""
        node_name = "retrieve_code_search_context"
        if self._is_halted(node_name, state):
            return cast(ReviewGraphState, {})

        self._log_node_start(node_name, state)
        code_search_context = self._retrieve_bounded_code_search_context(
            state.get("repository_path"),
            state.get("changed_file_paths", []),
            max_results=self._max_code_search_results,
            max_chars=self._max_code_search_chars,
            max_query_terms=self._max_code_search_query_terms,
            ignore_directories=self._repository_ignore_directories,
        )

        result: ReviewGraphState = {
            "code_search_context": code_search_context,
        }
        code_search_matches = self._collect_context_paths(
            code_search_context,
            split_token=" [",
        )
        search_terms = self._extract_code_search_terms(code_search_context)
        logger.info(
            "Repository context code search prepared. pr_id=%s terms=%s matches=%s sample_matches=%s preview=%s",
            self._state_pr_id(state),
            search_terms,
            len(code_search_matches),
            code_search_matches[:5],
            self._build_context_preview(code_search_context),
        )
        self._log_node_complete(
            node_name,
            state,
            code_search_chars=len(code_search_context or ""),
        )
        return result

    def compose_repository_context(self, state: ReviewGraphState) -> ReviewGraphState:
        """Combine repo map, related files, and code search into one context bundle."""
        node_name = "compose_repository_context"
        if self._is_halted(node_name, state):
            return cast(ReviewGraphState, {})

        self._log_node_start(node_name, state)
        repository_context_bundle = self._compose_repository_context_bundle(
            state.get("repo_map"),
            state.get("related_files_context"),
            state.get("code_search_context"),
        )

        result: ReviewGraphState = {
            "repository_context_bundle": repository_context_bundle,
            "repo_map": repository_context_bundle.get("repo_map", ""),
            "related_files_context": repository_context_bundle.get(
                "related_files_context", ""
            ),
            "code_search_context": repository_context_bundle.get(
                "code_search_context", ""
            ),
        }

        repo_map_chars = len(result.get("repo_map", ""))
        related_files_chars = len(result.get("related_files_context", ""))
        code_search_chars = len(result.get("code_search_context", ""))
        repository_context_total_chars = (
            repo_map_chars + related_files_chars + code_search_chars
        )

        max_repo_map_chars = self._normalize_char_limit(self._max_repo_map_chars)
        max_related_files_chars = self._normalize_char_limit(
            self._max_related_files_chars
        )
        max_code_search_chars = self._normalize_char_limit(self._max_code_search_chars)
        repository_context_total_configured_chars = (
            max_repo_map_chars + max_related_files_chars + max_code_search_chars
        )

        repository_context_total_tokens_estimate = self._estimate_tokens_from_chars(
            repository_context_total_chars
        )
        repository_context_total_configured_tokens_estimate = (
            self._estimate_tokens_from_chars(repository_context_total_configured_chars)
        )

        logger.info(
            "Repository context bundle composed. pr_id=%s total_used_chars=%s total_configured_chars=%s repo_map_used_chars=%s repo_map_configured_chars=%s related_files_used_chars=%s related_files_configured_chars=%s code_search_used_chars=%s code_search_configured_chars=%s",
            self._state_pr_id(state),
            repository_context_total_chars,
            repository_context_total_configured_chars,
            repo_map_chars,
            max_repo_map_chars,
            related_files_chars,
            max_related_files_chars,
            code_search_chars,
            max_code_search_chars,
        )
        logger.info(
            "Repository context size estimate (tokens). pr_id=%s total_used_tokens_estimate=%s total_configured_tokens_estimate=%s",
            self._state_pr_id(state),
            repository_context_total_tokens_estimate,
            repository_context_total_configured_tokens_estimate,
        )

        self._log_node_complete(
            node_name,
            state,
            repo_map_chars=repo_map_chars,
            related_files_chars=related_files_chars,
            code_search_chars=code_search_chars,
            repository_context_total_chars=repository_context_total_chars,
            repository_context_total_configured_chars=repository_context_total_configured_chars,
            repository_context_total_tokens_estimate=repository_context_total_tokens_estimate,
            repository_context_total_configured_tokens_estimate=repository_context_total_configured_tokens_estimate,
        )
        return result

    def prepare_review_inputs(self, state: ReviewGraphState) -> ReviewGraphState:
        """Render final draft-review prompts from state values and context bundle.

        The method loads prompt templates, fills placeholders with diff, PR
        metadata, existing feedback, and repository context, then returns the
        ready-to-send system and user prompts.
        """
        node_name = "prepare_review_inputs"
        if self._is_halted(node_name, state):
            return cast(ReviewGraphState, {})

        self._log_node_start(node_name, state)

        team_name = str(self._get_required_state_value(state, "team_name", ""))
        safe_diff = str(self._get_required_state_value(state, "safe_diff", ""))
        review_purpose = str(
            self._get_required_state_value(state, "review_purpose", "")
        )
        pr_title = str(self._get_required_state_value(state, "pr_title", ""))
        pr_description = str(
            self._get_required_state_value(state, "pr_description", "")
        )
        existing_feedback = str(
            self._get_required_state_value(state, "existing_feedback", "")
        )
        repository_context_bundle = state.get("repository_context_bundle")
        if not isinstance(repository_context_bundle, dict):
            repository_context_bundle = {}

        repo_map = str(
            repository_context_bundle.get("repo_map")
            or self._get_required_state_value(state, "repo_map", "")
            or "No repository map context available."
        )
        related_files_context = str(
            repository_context_bundle.get("related_files_context")
            or self._get_required_state_value(state, "related_files_context", "")
            or "No related-file context available."
        )
        code_search_context = str(
            repository_context_bundle.get("code_search_context")
            or self._get_required_state_value(state, "code_search_context", "")
            or "No code-search context available."
        )

        with open(
            self._prompts_dir / "review_system_prompt.md", "r", encoding="utf-8"
        ) as f:
            draft_sys_p = f.read().replace("{{TEAM_NAME}}", team_name)

        with open(
            self._prompts_dir / "review_user_prompt.md", "r", encoding="utf-8"
        ) as f:
            draft_user_p = (
                f.read()
                .replace("{{DIFF_CONTENT}}", safe_diff)
                .replace("{{PURPOSE}}", review_purpose)
                .replace("{{PR_TITLE}}", pr_title)
                .replace("{{PR_DESCRIPTION}}", pr_description)
                .replace("{{EXISTING_ROOT_COMMENTS}}", existing_feedback)
                .replace("{{EXISTING_FEEDBACK}}", existing_feedback)
                .replace("{{REPOSITORY_MAP}}", repo_map)
                .replace("{{RELATED_FILES_CONTEXT}}", related_files_context)
                .replace("{{CODE_SEARCH_CONTEXT}}", code_search_context)
            )

        result: ReviewGraphState = {
            "draft_sys_p": draft_sys_p,
            "draft_user_p": draft_user_p,
        }
        self._log_node_complete(
            node_name,
            state,
            draft_system_prompt_chars=len(draft_sys_p),
            draft_user_prompt_chars=len(draft_user_p),
        )
        return result

    def finding_normalizer(self, state: ReviewGraphState) -> ReviewGraphState:
        """Normalize draft comment fields so downstream nodes receive consistent data."""
        node_name = "finding_normalizer"
        if self._is_halted(node_name, state):
            return cast(ReviewGraphState, {})

        self._log_node_start(node_name, state)

        draft_review_data = state.get("draft_review_data")
        if not isinstance(draft_review_data, dict):
            self._log_node_complete(
                node_name, state, input_comments=0, normalized_comments=0
            )
            return cast(
                ReviewGraphState,
                {"normalized_draft_review_data": {"comments": []}},
            )

        comments = draft_review_data.get("comments")
        if not isinstance(comments, list):
            comments = []

        normalized_comments = []
        for comment in comments:
            if not isinstance(comment, dict):
                continue

            normalized_comments.append(
                {
                    **comment,
                    "anchor_id": str(comment.get("anchor_id") or "").strip(),
                    "severity": self._normalize_comment_severity(
                        comment.get("severity")
                    ),
                    "text": str(comment.get("text") or "").strip(),
                }
            )

        result: ReviewGraphState = {
            "normalized_draft_review_data": {
                **draft_review_data,
                "comments": normalized_comments,
            }
        }
        self._log_node_complete(
            node_name,
            state,
            input_comments=len(comments),
            normalized_comments=len(normalized_comments),
        )
        return result

    def anchor_resolver(self, state: ReviewGraphState) -> ReviewGraphState:
        """Resolve comment anchor ids to concrete inline locations and drop invalid ones."""
        node_name = "anchor_resolver"
        if self._is_halted(node_name, state):
            return cast(ReviewGraphState, {})

        self._log_node_start(node_name, state)

        comments = state.get("comments")
        if not isinstance(comments, list):
            comments = []

        resolved_comments = []
        skipped_inline_count = 0

        for comment in comments:
            if not isinstance(comment, dict):
                skipped_inline_count += 1
                logger.warning("Skipping inline comment with invalid payload type")
                continue

            anchor_id = str(comment.get("anchor_id") or "").strip()
            severity = self._normalize_comment_severity(comment.get("severity"))
            text = str(comment.get("text") or "").strip()

            if not text or not anchor_id:
                skipped_inline_count += 1
                logger.warning(
                    "Skipping inline comment missing required anchor_id/text"
                )
                continue

            resolved_anchor = self._resolve_anchor_by_id(
                anchor_id, state.get("anchor_index", {})
            )
            if not resolved_anchor.get("resolved"):
                skipped_inline_count += 1
                logger.warning(
                    "Skipping inline comment: reason=%s anchor_id=%s resolved_path=%s",
                    resolved_anchor.get("reason"),
                    anchor_id,
                    resolved_anchor.get("path"),
                )
                continue

            resolved_comments.append(
                {
                    "anchor": resolved_anchor["anchor"],
                    "path": resolved_anchor["path"],
                    "line": resolved_anchor["line"],
                    "severity": severity,
                    "text": text,
                }
            )

        result: ReviewGraphState = {
            "resolved_comments": resolved_comments,
            "skipped_inline_count": skipped_inline_count,
        }
        self._log_node_complete(
            node_name,
            state,
            input_comments=len(comments),
            resolved_comments=len(resolved_comments),
            skipped_inline_comments=skipped_inline_count,
        )
        return result

    def policy_guard(self, state: ReviewGraphState) -> ReviewGraphState:
        """Apply final severity policy and same-anchor near-duplicate suppression."""
        node_name = "policy_guard"
        if self._is_halted(node_name, state):
            return cast(ReviewGraphState, {})

        self._log_node_start(node_name, state)

        existing_comments_by_anchor = {}
        for existing_comment in state.get("existing_bot_inline_comments", []):
            if not isinstance(existing_comment, dict):
                continue

            normalized_path = self._normalize_repo_path(existing_comment.get("path"))
            normalized_line = self._safe_int(existing_comment.get("line"), default=0)
            if not normalized_path or normalized_line <= 0:
                continue

            existing_text = str(existing_comment.get("text") or "").strip()
            if not existing_text:
                continue

            anchor_key = (normalized_path, normalized_line)
            existing_comments_by_anchor.setdefault(anchor_key, []).append(existing_text)

        guarded_comments = []
        accepted_comments_by_anchor = {}
        duplicate_suppressed_count = 0
        for comment in state.get("resolved_comments", []):
            if not isinstance(comment, dict):
                continue

            comment_path = self._normalize_repo_path(comment.get("path"))
            comment_line = self._safe_int(comment.get("line"), default=0)
            comment_text = str(comment.get("text") or "").strip()

            anchor_key = None
            if comment_path and comment_line > 0 and comment_text:
                anchor_key = (comment_path, comment_line)

            if anchor_key:
                existing_anchor_texts = existing_comments_by_anchor.get(anchor_key, [])
                accepted_anchor_texts = accepted_comments_by_anchor.get(anchor_key, [])
                if any(
                    self._comments_are_near_duplicates(comment_text, existing_text)
                    for existing_text in existing_anchor_texts
                ) or any(
                    self._comments_are_near_duplicates(comment_text, accepted_text)
                    for accepted_text in accepted_anchor_texts
                ):
                    duplicate_suppressed_count += 1
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
                accepted_comments_by_anchor.setdefault(anchor_key, []).append(
                    comment_text
                )

        skipped_inline_count = self._safe_int(
            state.get("skipped_inline_count"), default=0
        )
        skipped_inline_count += duplicate_suppressed_count

        result: ReviewGraphState = {
            "resolved_comments": guarded_comments,
            "skipped_inline_count": skipped_inline_count,
        }
        self._log_node_complete(
            node_name,
            state,
            input_comments=len(state.get("resolved_comments", [])),
            guarded_comments=len(guarded_comments),
            duplicate_suppressed=duplicate_suppressed_count,
            skipped_inline_count=skipped_inline_count,
        )
        return result

    def summary_builder(self, state: ReviewGraphState) -> ReviewGraphState:
        """Build normalized review summary fields with safe defaults when missing."""
        node_name = "summary_builder"
        if self._is_halted(node_name, state):
            return cast(ReviewGraphState, {})

        self._log_node_start(node_name, state)

        review_data = state.get("review_data")
        if not isinstance(review_data, dict):
            review_data = {}

        comments = review_data.get("comments")
        normalized_comments = comments if isinstance(comments, list) else []

        result: ReviewGraphState = {
            "verdict": review_data.get("verdict", "CHANGES_SUGGESTED"),
            "summary": review_data.get("summary", "No issues identified."),
            "checklist": review_data.get("checklist", []),
            "comments": normalized_comments,
            "raw_comment_count": len(normalized_comments),
        }
        self._log_node_complete(
            node_name,
            state,
            verdict=result["verdict"],
            comments_count=len(normalized_comments),
        )
        return result

    def publish_review(self, state: ReviewGraphState) -> ReviewGraphState:
        """Publish inline comments and summary to VCS, tracking posted/skipped counts.

        If no inline comments are available, it posts only the summary. Network
        failures are logged and do not crash the publish loop.
        """
        node_name = "publish_review"
        if self._is_halted(node_name, state):
            return cast(ReviewGraphState, {})

        self._log_node_start(node_name, state)

        vcs_client = state.get("vcs_client")
        pr_id = state.get("pr_id")
        run_team_name = str(state.get("team_name") or "")
        if vcs_client is None or pr_id is None or not run_team_name:
            logger.warning(
                "Skipping publish step due to missing runtime context. pr_id=%s team_name_present=%s",
                pr_id,
                bool(run_team_name),
            )
            self._log_node_complete(
                node_name,
                state,
                status="skipped-missing-runtime-context",
            )
            return cast(ReviewGraphState, {})

        verdict = state.get("verdict", "CHANGES_SUGGESTED")
        summary = state.get("summary", "No issues identified.")
        checklist = state.get("checklist", [])
        resolved_comments = state.get("resolved_comments", [])

        if not resolved_comments:
            try:
                self._upsert_summary_comment(
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
            result: ReviewGraphState = {
                "posted_inline_count": 0,
                "skipped_inline_count": state.get("skipped_inline_count", 0),
            }
            self._log_node_complete(
                node_name,
                state,
                posted_inline_count=0,
                skipped_inline_count=result["skipped_inline_count"],
            )
            return result

        posted_inline_count = 0
        skipped_inline_count = state.get("skipped_inline_count", 0)

        for comment in resolved_comments:
            try:
                self._post_inline_comment(
                    vcs_client,
                    pr_id,
                    comment["anchor"],
                    comment.get("severity"),
                    comment.get("text"),
                    run_team_name,
                )
                posted_inline_count += 1
            except requests.exceptions.RequestException as exc:
                logger.warning(
                    "Failed to post inline comment for %s:%s - %s",
                    comment.get("path"),
                    comment.get("line"),
                    exc,
                )

        try:
            self._upsert_summary_comment(
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

        result: ReviewGraphState = {
            "posted_inline_count": posted_inline_count,
            "skipped_inline_count": skipped_inline_count,
        }
        self._log_node_complete(
            node_name,
            state,
            posted_inline_count=posted_inline_count,
            skipped_inline_count=skipped_inline_count,
        )
        return result
