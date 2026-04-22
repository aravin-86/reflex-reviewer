import logging
import os
import re
from pathlib import Path

from .adapters import (
    get_default_repo_context_adapters,
    resolve_repo_context_adapter,
)


logger = logging.getLogger(__name__)


REPO_MAP_UNAVAILABLE = "Repository map unavailable (set REPOSITORY_PATH to enable repository-aware context)."
RELATED_FILES_UNAVAILABLE = "Related-file retrieval unavailable (set REPOSITORY_PATH to enable repository-aware context)."
CODE_SEARCH_UNAVAILABLE = (
    "Code search unavailable (set REPOSITORY_PATH to enable repository-aware context)."
)

NO_REPO_MAP_DATA = "No changed files were mappable under REPOSITORY_PATH."
NO_RELATED_FILE_DATA = "No deterministic related files were found for changed files."
NO_CODE_SEARCH_DATA = (
    "No bounded code search matches were found for deterministic query terms."
)

_HIDDEN_DIRS = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".nox",
    "node_modules",
    "dist",
    "build",
    "target",
    "classes",
    "out",
    ".gradle",
    ".idea",
}
_SEARCHABLE_EXTENSIONS = {
    ".py",
    ".java",
    ".kt",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".go",
    ".rb",
    ".php",
    ".cs",
    ".c",
    ".h",
    ".cpp",
    ".hpp",
}
_LANGUAGE_ADAPTERS = get_default_repo_context_adapters()


def _log_missing_repository_root(repository_path, operation_name):
    """Log a warning when repository path is unavailable for an operation."""
    logger.warning(
        "Repository path does not contain expected content for repository context operation; operation=%s repository_path=%s",
        operation_name,
        str(repository_path or "").strip() or "<empty>",
    )


def _log_missing_expected_paths(repository_root, operation_name, missing_paths):
    """Log one bounded warning for missing expected files under repository root."""
    missing = [str(path or "").strip() for path in list(missing_paths or []) if path]
    if not missing:
        return

    sample_paths = ", ".join(missing[:5])
    logger.warning(
        "Repository path does not contain expected files; operation=%s repository_path=%s missing_count=%s sample_missing_paths=%s",
        operation_name,
        str(repository_root),
        len(missing),
        sample_paths,
    )


def _normalize_repo_relative_path(file_path):
    """Normalize a raw diff path into a clean repository-relative path string."""
    normalized = str(file_path or "").strip().replace("\\", "/")
    normalized = re.sub(r"^(?:\./)+", "", normalized)
    normalized = re.sub(r"^(?:a|b)/", "", normalized)
    normalized = normalized.lstrip("/")
    if normalized == "dev/null":
        return ""
    return normalized


def _resolve_repository_root(repository_path):
    """Resolve repository_path to an existing absolute directory, or return None."""
    normalized = str(repository_path or "").strip()
    if not normalized:
        return None

    raw_path = Path(normalized)
    resolved_path = raw_path if raw_path.is_absolute() else (Path.cwd() / raw_path)
    resolved_path = resolved_path.resolve()
    if not resolved_path.exists() or not resolved_path.is_dir():
        return None
    return resolved_path


def resolve_repository_path(repository_path):
    """Return the resolved repository root as a string when the path is valid."""
    resolved = _resolve_repository_root(repository_path)
    if resolved is None:
        return None
    return str(resolved)


def _read_text(path):
    """Read file text as UTF-8 and return an empty string on read failures."""
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError as error:
        logger.warning(
            "Failed to read repository-context file text; file_path=%s error=%s",
            str(path),
            str(error),
        )
        return ""


def _normalize_ignore_directories(ignore_directories):
    """Normalize ignore directory names to bare folder names."""
    if ignore_directories is None:
        return set()

    raw_values = []
    if isinstance(ignore_directories, str):
        raw_values.extend(ignore_directories.split(","))
    elif isinstance(ignore_directories, (list, tuple, set)):
        for item in ignore_directories:
            raw_values.extend(str(item or "").split(","))
    else:
        raw_values.append(str(ignore_directories))

    normalized_values = set()
    for raw_value in raw_values:
        candidate = str(raw_value or "").strip().replace("\\", "/").strip("/")
        if not candidate:
            continue
        normalized_values.add(candidate.split("/")[-1])

    return normalized_values


def _truncate_text(value, max_chars, suffix_label):
    """Bound text length and add a suffix that says which section was truncated."""
    text = str(value or "")
    if max_chars is None or max_chars <= 0 or len(text) <= max_chars:
        return text

    suffix = f"\n... [{suffix_label} truncated]"
    if max_chars <= len(suffix):
        return suffix[:max_chars]
    return text[: max_chars - len(suffix)].rstrip() + suffix


def extract_changed_file_paths_from_diff(raw_diff_data, max_files=200):
    """Collect unique changed file paths from diff source/destination entries."""
    if max_files is not None and max_files <= 0:
        return []

    diffs = (raw_diff_data or {}).get("diffs") or []
    changed_files = []
    seen = set()

    for diff in diffs:
        if not isinstance(diff, dict):
            continue

        for side in ("destination", "source"):
            side_value = diff.get(side)
            raw_path = ""
            if isinstance(side_value, dict):
                raw_path = side_value.get("toString", "")
            elif isinstance(side_value, str):
                raw_path = side_value

            normalized_path = _normalize_repo_relative_path(raw_path)
            if not normalized_path or normalized_path in seen:
                continue

            seen.add(normalized_path)
            changed_files.append(normalized_path)
            if max_files is not None and len(changed_files) >= max_files:
                return changed_files

    return changed_files


def build_repo_map_for_changed_files(
    repository_path,
    changed_file_paths,
    max_files=20,
    max_chars=4000,
):
    """Build a bounded repo map by summarizing each changed file.

    The method normalizes each changed path, keeps only existing files under
    repository_path, asks a language adapter to build a structured summary, and
    falls back to a file-type entry when no adapter summary is available.
    """
    repository_root = _resolve_repository_root(repository_path)
    if repository_root is None:
        _log_missing_repository_root(repository_path, "build_repo_map")
        return REPO_MAP_UNAVAILABLE

    mapped_entries = []
    missing_changed_files = []
    for relative_path in list(changed_file_paths or []):
        if max_files is not None and len(mapped_entries) >= max_files:
            break

        normalized_path = _normalize_repo_relative_path(relative_path)
        if not normalized_path:
            continue

        file_path = repository_root / normalized_path
        if not file_path.exists() or not file_path.is_file():
            missing_changed_files.append(normalized_path)
            continue

        file_text = _read_text(file_path)
        adapter = resolve_repo_context_adapter(normalized_path, _LANGUAGE_ADAPTERS)
        if adapter is not None:
            entry = str(
                adapter.build_repo_map_entry(normalized_path, file_text) or ""
            ).strip()
            if entry:
                mapped_entries.append(f"- {normalized_path} | {entry}")
                continue

        suffix = file_path.suffix.lower()
        mapped_entries.append(f"- {normalized_path} | file_type: {suffix or 'unknown'}")

    if not mapped_entries:
        _log_missing_expected_paths(
            repository_root,
            "build_repo_map",
            missing_changed_files,
        )
        return NO_REPO_MAP_DATA

    return _truncate_text("\n".join(mapped_entries), max_chars, "repository map")


def _build_snippet(file_text, max_lines=18, max_chars=420):
    """Build a compact snippet from the first non-empty lines of file text."""
    lines = [
        line.rstrip() for line in str(file_text or "").splitlines() if line.strip()
    ]
    if not lines:
        return ""

    snippet = "\n".join(lines[:max_lines])
    if len(snippet) <= max_chars:
        return snippet
    return snippet[: max_chars - 1].rstrip() + "…"


def retrieve_related_files_context(
    repository_path,
    changed_file_paths,
    max_related_files=12,
    max_chars=5000,
):
    """Build bounded related-file context snippets for changed files.

    For each changed file with a matching adapter, it asks the adapter for
    deterministic related paths, removes duplicates and changed-file overlaps,
    reads selected files, and returns path + snippet blocks.
    """
    repository_root = _resolve_repository_root(repository_path)
    if repository_root is None:
        _log_missing_repository_root(repository_path, "retrieve_related_files")
        return RELATED_FILES_UNAVAILABLE

    changed_paths = {
        _normalize_repo_relative_path(path) for path in list(changed_file_paths or [])
    }
    changed_paths.discard("")

    candidate_paths = []
    candidate_seen = set()
    missing_changed_files = []
    missing_related_candidates = []

    for changed_path in list(changed_file_paths or []):
        normalized_changed_path = _normalize_repo_relative_path(changed_path)
        if not normalized_changed_path:
            continue

        changed_file = repository_root / normalized_changed_path
        if not changed_file.exists() or not changed_file.is_file():
            missing_changed_files.append(normalized_changed_path)
            continue

        adapter = resolve_repo_context_adapter(
            normalized_changed_path, _LANGUAGE_ADAPTERS
        )
        if adapter is None:
            continue

        for candidate_path in adapter.resolve_related_file_paths(
            normalized_changed_path,
            _read_text(changed_file),
        ):
            normalized_candidate = _normalize_repo_relative_path(candidate_path)
            if (
                not normalized_candidate
                or normalized_candidate in changed_paths
                or normalized_candidate in candidate_seen
            ):
                continue

            candidate_file = repository_root / normalized_candidate
            if not candidate_file.exists() or not candidate_file.is_file():
                missing_related_candidates.append(normalized_candidate)
                continue

            candidate_seen.add(normalized_candidate)
            candidate_paths.append(normalized_candidate)

    if not candidate_paths:
        _log_missing_expected_paths(
            repository_root,
            "retrieve_related_files.changed",
            missing_changed_files,
        )
        _log_missing_expected_paths(
            repository_root,
            "retrieve_related_files.candidates",
            missing_related_candidates,
        )
        return NO_RELATED_FILE_DATA

    selected_paths = (
        candidate_paths[: max_related_files or 0] if max_related_files else []
    )
    if not selected_paths:
        return NO_RELATED_FILE_DATA

    context_entries = []
    for relative_path in selected_paths:
        target_file = repository_root / relative_path
        snippet = _build_snippet(_read_text(target_file))
        if not snippet:
            continue
        context_entries.append(f"- {relative_path}\n```\n{snippet}\n```")

    if not context_entries:
        return NO_RELATED_FILE_DATA

    return _truncate_text("\n".join(context_entries), max_chars, "related files")


def _iter_repository_files(repository_root, ignore_directories=None):
    """Yield repository files allowed for code search, skipping hidden/build dirs."""
    effective_hidden_dirs = set(_HIDDEN_DIRS)
    effective_hidden_dirs.update(_normalize_ignore_directories(ignore_directories))

    for current_root, dir_names, file_names in os.walk(repository_root):
        dir_names[:] = sorted(
            name for name in dir_names if name not in effective_hidden_dirs
        )
        for file_name in sorted(file_names):
            file_path = Path(current_root) / file_name
            if file_path.suffix.lower() not in _SEARCHABLE_EXTENSIONS:
                continue
            yield file_path


def _append_term_unique(terms, term, max_terms):
    """Append a term only if it is non-trivial, unique, and within term limits."""
    normalized_term = str(term or "").strip()
    if len(normalized_term) < 3:
        return

    lower_terms = {existing.lower() for existing in terms}
    if normalized_term.lower() in lower_terms:
        return

    terms.append(normalized_term)
    if max_terms is not None and len(terms) > max_terms:
        del terms[max_terms:]


def _derive_code_search_terms(repository_root, changed_file_paths, max_terms=12):
    """Derive search terms from changed file names and adapter-provided keywords."""
    terms = []
    missing_changed_files = []

    for changed_path in list(changed_file_paths or []):
        if max_terms is not None and len(terms) >= max_terms:
            break

        normalized_changed_path = _normalize_repo_relative_path(changed_path)
        if not normalized_changed_path:
            continue

        stem = Path(normalized_changed_path).stem
        if stem and stem != "__init__":
            _append_term_unique(terms, stem, max_terms)

        changed_file = repository_root / normalized_changed_path
        if not changed_file.exists() or not changed_file.is_file():
            missing_changed_files.append(normalized_changed_path)
            continue

        adapter = resolve_repo_context_adapter(
            normalized_changed_path, _LANGUAGE_ADAPTERS
        )
        if adapter is None:
            continue

        for derived_term in adapter.derive_code_search_terms(
            normalized_changed_path,
            _read_text(changed_file),
        ):
            _append_term_unique(terms, derived_term, max_terms)
            if max_terms is not None and len(terms) >= max_terms:
                break

    return terms, missing_changed_files


def retrieve_bounded_code_search_context(
    repository_path,
    changed_file_paths,
    max_results=40,
    max_chars=5000,
    max_query_terms=12,
    ignore_directories=None,
):
    """Build bounded code-search context from term matches in other repo files.

    It derives deterministic query terms from changed files, scans searchable
    repository files except changed paths, records unique line-level matches,
    and returns a capped text block with terms and matching lines.
    """
    repository_root = _resolve_repository_root(repository_path)
    if repository_root is None:
        _log_missing_repository_root(repository_path, "retrieve_code_search")
        return CODE_SEARCH_UNAVAILABLE

    search_terms, missing_changed_files = _derive_code_search_terms(
        repository_root,
        changed_file_paths,
        max_terms=max_query_terms,
    )
    if not search_terms:
        _log_missing_expected_paths(
            repository_root,
            "derive_code_search_terms",
            missing_changed_files,
        )
        logger.warning(
            "Repository path did not contain expected searchable content for changed files; operation=retrieve_code_search repository_path=%s",
            str(repository_root),
        )
        return NO_CODE_SEARCH_DATA

    changed_paths = {
        _normalize_repo_relative_path(path) for path in list(changed_file_paths or [])
    }
    changed_paths.discard("")

    results = []
    seen_matches = set()

    for file_path in _iter_repository_files(
        repository_root,
        ignore_directories=ignore_directories,
    ):
        if max_results is not None and len(results) >= max_results:
            break

        relative_path = file_path.relative_to(repository_root).as_posix()
        if relative_path in changed_paths:
            continue

        file_text = _read_text(file_path)
        if not file_text:
            continue

        for line_number, line_text in enumerate(file_text.splitlines(), start=1):
            line_body = re.sub(r"\s+", " ", line_text).strip()
            if not line_body:
                continue

            lowered_body = line_body.lower()
            matched_term = None
            for term in search_terms:
                if term.lower() in lowered_body:
                    matched_term = term
                    break

            if not matched_term:
                continue

            match_key = (relative_path, line_number, matched_term.lower())
            if match_key in seen_matches:
                continue

            seen_matches.add(match_key)
            safe_line = line_body[:220]
            results.append(
                f"- {relative_path}:{line_number} [{matched_term}] {safe_line}"
            )

            if max_results is not None and len(results) >= max_results:
                break

    if not results:
        _log_missing_expected_paths(
            repository_root,
            "derive_code_search_terms",
            missing_changed_files,
        )
        logger.warning(
            "Repository path did not contain expected code-search text for derived terms; operation=retrieve_code_search repository_path=%s terms=%s",
            str(repository_root),
            ", ".join(search_terms[:6]),
        )
        return NO_CODE_SEARCH_DATA

    header = f"Search terms: {', '.join(search_terms)}"
    search_output = f"{header}\n" + "\n".join(results)
    return _truncate_text(search_output, max_chars, "code search")


def compose_repository_context_bundle(
    repo_map, related_files_context, code_search_context
):
    """Normalize repository-context sections and return them in one bundle."""
    normalized_repo_map = str(repo_map or "").strip() or NO_REPO_MAP_DATA
    normalized_related_files = (
        str(related_files_context or "").strip() or NO_RELATED_FILE_DATA
    )
    normalized_code_search = (
        str(code_search_context or "").strip() or NO_CODE_SEARCH_DATA
    )

    return {
        "repo_map": normalized_repo_map,
        "related_files_context": normalized_related_files,
        "code_search_context": normalized_code_search,
    }
