import os
import re
from pathlib import Path

try:
    from dotenv import load_dotenv  # type: ignore[reportMissingImports]
except ModuleNotFoundError:  # pragma: no cover - setup/runtime bootstrap fallback
    def load_dotenv(*_args, **_kwargs):
        return False

try:
    import tomllib  # type: ignore[reportMissingImports]
except ModuleNotFoundError:  # pragma: no cover - Python <3.11 fallback
    try:
        import tomli as tomllib  # type: ignore[reportMissingImports]
    except (
        ModuleNotFoundError
    ):  # pragma: no cover - local fallback when tomli isn't installed
        from pip._vendor import tomli as tomllib  # type: ignore[reportMissingImports]


_MISSING = object()
_VALID_REASONING_EFFORTS = {"low", "medium", "high"}
_VALID_MODEL_ENDPOINTS = {"responses", "chat_completions"}
_ENV_PLACEHOLDER_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(\|-([^}]*))?\}")
_RUNTIME_OVERRIDES = {}
_FILE_CONFIG = None
_ROOT_DIR = Path(__file__).resolve().parent.parent
_CONFIG_FILE_PATH = _ROOT_DIR / "reflex_reviewer.toml"
_DEFAULT_REPOSITORY_IGNORE_DIRECTORIES = {
    "__pycache__",
    ".venv",
    "venv",
    "env",
    ".mypy_cache",
    ".pytest_cache",
    ".tox",
    ".nox",
    ".ruff_cache",
    ".hypothesis",
    ".pyre",
    "build",
    "dist",
    ".eggs",
    "target",
    "bin",
    ".gradle",
    "out",
    "classes",
    ".idea",
    "logs",
    "htmlcov",
    ".coverage",
    ".cache",
    ".tmp",
    "tmp",
    "temp",
}


load_dotenv(_ROOT_DIR / ".env")


def _load_file_config():
    global _FILE_CONFIG
    if isinstance(_FILE_CONFIG, dict):
        return _FILE_CONFIG

    loaded = {}
    if _CONFIG_FILE_PATH.exists():
        config_text = _CONFIG_FILE_PATH.read_text(encoding="utf-8")
        parsed = tomllib.loads(config_text)
        if isinstance(parsed, dict):
            loaded = parsed

    _FILE_CONFIG = loaded
    return _FILE_CONFIG


def _config_value(section, key, default=_MISSING):
    value = _config_value_or_missing(section, key)
    if value is _MISSING:
        if default is _MISSING:
            return None
        return _resolve_env_placeholders(default)
    return value


def _config_value_or_missing(section, key):
    config = _load_file_config()
    if not isinstance(config, dict):
        return _MISSING

    section_data = config
    for section_part in str(section or "").split("."):
        section_name = section_part.strip()
        if not section_name:
            return _MISSING
        if not isinstance(section_data, dict) or section_name not in section_data:
            return _MISSING
        section_data = section_data.get(section_name)

    if isinstance(section_data, dict) and key in section_data:
        return _resolve_env_placeholders(section_data[key])

    return _MISSING


def _resolve_env_placeholder_match(match):
    env_key = str(match.group(1) or "").strip()
    default_value = match.group(3)
    env_value = os.getenv(env_key)

    if env_value is not None:
        return env_value

    if default_value is None:
        return None

    return _resolve_env_placeholders(default_value)


def _resolve_string_env_placeholders(raw_value):
    full_match = _ENV_PLACEHOLDER_PATTERN.fullmatch(raw_value)
    if full_match:
        return _resolve_env_placeholder_match(full_match)

    def _replacement(match):
        resolved = _resolve_env_placeholder_match(match)
        return "" if resolved is None else str(resolved)

    return _ENV_PLACEHOLDER_PATTERN.sub(_replacement, raw_value)


def _resolve_env_placeholders(value):
    if isinstance(value, str):
        return _resolve_string_env_placeholders(value)

    if isinstance(value, list):
        return [_resolve_env_placeholders(item) for item in value]

    if isinstance(value, tuple):
        return tuple(_resolve_env_placeholders(item) for item in value)

    if isinstance(value, dict):
        return {
            map_key: _resolve_env_placeholders(map_value)
            for map_key, map_value in value.items()
        }

    return value


def _to_int(value, default=None):
    if value is None:
        return default

    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_float(value, default=None):
    if value is None:
        return default

    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _normalize_request_timeout(
    read_timeout_raw,
    default_connect_timeout=10,
    default_read_timeout=30,
):
    read_timeout = _to_int(read_timeout_raw, default=default_read_timeout)

    if read_timeout is None or read_timeout <= 0:
        read_timeout = default_read_timeout

    return (default_connect_timeout, read_timeout)


def _to_set(value):
    if not isinstance(value, (list, tuple, set)):
        return set()
    return {str(item).strip() for item in value if str(item).strip()}


def _to_directory_name_set(value):
    """Parse directory names from comma-separated strings or iterable values."""
    if value is None:
        return set()

    raw_values = []
    if isinstance(value, str):
        raw_values.extend(value.split(","))
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            raw_values.extend(str(item or "").split(","))
    else:
        raw_values.append(str(value))

    normalized = set()
    for raw_value in raw_values:
        candidate = str(raw_value or "").strip().replace("\\", "/").strip("/")
        if not candidate:
            continue
        normalized.add(candidate.split("/")[-1])

    return normalized


def set_runtime_overrides(overrides=None):
    global _RUNTIME_OVERRIDES
    normalized_overrides = {}
    if isinstance(overrides, dict):
        normalized_overrides = {
            key: value for key, value in overrides.items() if value is not None
        }
    _RUNTIME_OVERRIDES = normalized_overrides


def clear_runtime_overrides():
    global _RUNTIME_OVERRIDES
    _RUNTIME_OVERRIDES = {}


def _merged_overrides(overrides=None):
    merged = dict(_RUNTIME_OVERRIDES)
    if isinstance(overrides, dict):
        for key, value in overrides.items():
            if value is not None:
                merged[key] = value
    return merged


def _resolve_override_only(overrides, override_key):
    merged_overrides = _merged_overrides(overrides)
    return merged_overrides.get(override_key)


def _resolve_toml_value(overrides, override_key, section, key, default=_MISSING):
    merged_overrides = _merged_overrides(overrides)
    if override_key in merged_overrides:
        return merged_overrides[override_key]

    value = _config_value_or_missing(section, key)
    if value is not _MISSING:
        return value

    if default is _MISSING:
        return None

    return default


def _config_value_from_sections(sections, key, default=_MISSING):
    for section in sections:
        value = _config_value_or_missing(section, key)
        if value is not _MISSING:
            return value

    if default is _MISSING:
        return None

    return _resolve_env_placeholders(default)


def _to_bool(value, default=False):
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _normalize_llm_api_reasoning_effort(raw, default="high"):
    normalized = str(raw if raw is not None else default).strip().lower()
    if normalized in _VALID_REASONING_EFFORTS:
        return normalized
    return default


def _normalize_vcs_type(raw, default="bitbucket"):
    normalized = str(raw if raw is not None else default).strip().lower()
    return normalized or default


def _normalize_model_endpoint(raw, default="responses"):
    normalized = str(raw if raw is not None else default).strip().lower()
    if normalized in _VALID_MODEL_ENDPOINTS:
        return normalized
    return default


def resolve_dpo_training_data_dir(dpo_training_data_dir):
    normalized_dir = str(dpo_training_data_dir or "").strip()
    if not normalized_dir:
        raise ValueError(
            "DPO training data directory is required. Pass --dpo-training-data-dir."
        )

    training_data_dir = Path(normalized_dir)
    if training_data_dir.exists() and not training_data_dir.is_dir():
        raise ValueError(
            "DPO training data directory path must be a directory, but points to a file. "
            f"path={training_data_dir}"
        )

    try:
        training_data_dir.mkdir(parents=True, exist_ok=True)
    except PermissionError as exc:
        raise ValueError(
            "Failed to create DPO training data directory due to permissions. "
            f"path={training_data_dir}"
        ) from exc
    except OSError as exc:
        raise ValueError(
            "Failed to create DPO training data directory. " f"path={training_data_dir}"
        ) from exc

    return str(training_data_dir)


def sanitize_team_name_for_identifier(team_name):
    normalized_team_name = str(team_name or "").strip()
    if not normalized_team_name:
        raise ValueError(
            "TEAM_NAME is required. Pass --team-name (identifier for your team to the LLM model)."
        )

    sanitized_team_name = re.sub(r"[^a-z0-9]+", "-", normalized_team_name.lower())
    sanitized_team_name = re.sub(r"-{2,}", "-", sanitized_team_name).strip("-")

    if not sanitized_team_name:
        raise ValueError(
            "TEAM_NAME must include at least one alphanumeric character after sanitization."
        )

    return sanitized_team_name


def sanitize_team_name_for_dpo_filename(team_name):
    return sanitize_team_name_for_identifier(team_name).replace("-", "_")


def resolve_dpo_training_data_file_path(team_name, dpo_training_data_dir):
    normalized_team_name = str(team_name or "").strip()
    if not normalized_team_name:
        raise ValueError(
            "TEAM_NAME is required. Pass --team-name (identifier for your team to the LLM model)."
        )

    invalid_separators = [separator for separator in (os.sep, os.altsep) if separator]
    if any(separator in normalized_team_name for separator in invalid_separators):
        raise ValueError(
            "TEAM_NAME cannot contain path separators when deriving DPO training data filename."
        )

    resolved_directory = resolve_dpo_training_data_dir(dpo_training_data_dir)
    sanitized_team_name = sanitize_team_name_for_dpo_filename(normalized_team_name)
    file_name = f"{sanitized_team_name}_dpo_training_data.jsonl"
    return str(Path(resolved_directory) / file_name)


def resolve_refine_split_file_paths(dpo_training_data_dir):
    resolved_directory = Path(resolve_dpo_training_data_dir(dpo_training_data_dir))
    return {
        "train": str(resolved_directory / "train.jsonl"),
        "val": str(resolved_directory / "val.jsonl"),
    }


def get_vcs_config(overrides=None):
    token = _resolve_toml_value(overrides, "vcs_token", "vcs", "token")
    if not token:
        token = os.getenv("VCS_TOKEN")

    return {
        "type": _normalize_vcs_type(
            _resolve_toml_value(overrides, "vcs_type", "vcs", "type", "bitbucket")
        ),
        "base_url": _resolve_toml_value(overrides, "vcs_base_url", "vcs", "base_url"),
        "project": _resolve_toml_value(overrides, "vcs_project_key", "vcs", "project"),
        "repo_slug": _resolve_toml_value(
            overrides, "vcs_repo_slug", "vcs", "repo_slug"
        ),
        "pr_id": _resolve_toml_value(overrides, "vcs_pr_id", "vcs", "pr_id"),
        "token": token,
    }


def get_common_config(overrides=None):
    model_config = get_model_config(overrides)

    return {
        "team_name": _resolve_override_only(overrides, "team_name"),
        "draft_model": model_config.get("draft_model"),
        "judge_model": model_config.get("judge_model"),
        "stream_response": bool(model_config.get("stream_response")),
        "model_endpoint": model_config.get("model_endpoint"),
        "dpo_training_data_dir": _resolve_override_only(
            overrides,
            "dpo_training_data_dir",
        ),
    }


def get_model_config(overrides=None):
    stream_response = _to_bool(
        _resolve_toml_value(
            overrides,
            "stream_response",
            "model",
            "stream_response",
            True,
        ),
        default=True,
    )

    draft_model = _resolve_toml_value(
        overrides,
        "draft_model",
        "model",
        "draft_model",
    )
    if draft_model is not None:
        draft_model = str(draft_model).strip()
        if not draft_model:
            draft_model = None

    judge_model = _resolve_toml_value(
        overrides,
        "judge_model",
        "model",
        "judge_model",
    )
    if judge_model is not None:
        judge_model = str(judge_model).strip()
        if not judge_model:
            judge_model = None

    model_endpoint = _normalize_model_endpoint(
        _resolve_toml_value(
            overrides,
            "model_endpoint",
            "model",
            "model_endpoint",
            "responses",
        ),
        default="responses",
    )

    raw_reasoning_effort = _resolve_toml_value(
        overrides,
        "llm_api_reasoning_effort",
        "model",
        "reasoning_effort",
        "high",
    )
    if not raw_reasoning_effort:
        raw_reasoning_effort = os.getenv("LLM_API_REASONING_EFFORT") or "high"

    reasoning_effort = _normalize_llm_api_reasoning_effort(raw_reasoning_effort)
    unsupported_reasoning_models = _to_set(
        _resolve_toml_value(
            overrides,
            "unsupported_reasoning_models",
            "model",
            "unsupported_reasoning_models",
            [],
        )
    )

    return {
        "draft_model": draft_model,
        "judge_model": judge_model,
        "stream_response": stream_response,
        "model_endpoint": model_endpoint,
        "reasoning_effort": reasoning_effort,
        "unsupported_reasoning_models": unsupported_reasoning_models,
    }


def get_review_config():
    configured_repository_ignore_directories = _to_directory_name_set(
        _config_value(
            "review.repository_context",
            "ignore_directories",
        )
    )

    return {
        "response_state_file": _config_value("review", "response_state_file"),
        "response_state_ttl_days": _to_int(
            _config_value("review", "response_state_ttl_days")
        ),
        "max_diff_chars": _to_int(_config_value("review", "max_diff_chars")),
        "max_existing_feedback_comments": _to_int(
            _config_value("review", "max_existing_feedback_comments")
        ),
        "activities_fetch_limit": _to_int(
            _config_value("review", "activities_fetch_limit")
        ),
        "sanitized_comment_max_chars": _to_int(
            _config_value("review", "sanitized_comment_max_chars")
        ),
        "repository_path": _config_value(
            "review.repository_context",
            "repository_path",
        ),
        "max_changed_files": _to_int(
            _config_value(
                "review.repository_context",
                "max_changed_files",
                400,
            ),
            default=400,
        ),
        "max_repo_map_files": _to_int(
            _config_value(
                "review.repository_context",
                "max_repo_map_files",
                150,
            ),
            default=150,
        ),
        "max_repo_map_chars": _to_int(
            _config_value(
                "review.repository_context",
                "max_repo_map_chars",
                100000,
            ),
            default=100000,
        ),
        "max_related_files": _to_int(
            _config_value(
                "review.repository_context",
                "max_related_files",
                80,
            ),
            default=80,
        ),
        "max_related_files_chars": _to_int(
            _config_value(
                "review.repository_context",
                "max_related_files_chars",
                150000,
            ),
            default=150000,
        ),
        "max_code_search_results": _to_int(
            _config_value(
                "review.repository_context",
                "max_code_search_results",
                500,
            ),
            default=500,
        ),
        "max_code_search_chars": _to_int(
            _config_value(
                "review.repository_context",
                "max_code_search_chars",
                150000,
            ),
            default=150000,
        ),
        "max_code_search_query_terms": _to_int(
            _config_value(
                "review.repository_context",
                "max_code_search_query_terms",
                50,
            ),
            default=50,
        ),
        "repository_ignore_directories": set(_DEFAULT_REPOSITORY_IGNORE_DIRECTORIES)
        | configured_repository_ignore_directories,
        "skip_extensions": _to_set(_config_value("review", "skip_extensions")),
        "skip_files": _to_set(_config_value("review", "skip_files")),
    }


def get_distill_config():
    return {
        "activities_fetch_limit": _to_int(
            _config_value("distill", "activities_fetch_limit")
        ),
        "max_llm_threads": _to_int(_config_value("distill", "max_llm_threads")),
        "diff_skip_extensions": _to_set(
            _config_value("distill", "diff_skip_extensions")
        ),
    }


def get_refine_config():
    return {
        "timeout_seconds": _to_int(_config_value("refine", "timeout_seconds")),
        "initial_poll_interval_seconds": _to_int(
            _config_value("refine", "initial_poll_interval_seconds")
        ),
        "max_poll_interval_seconds": _to_int(
            _config_value("refine", "max_poll_interval_seconds")
        ),
        "train_split_ratio": _to_float(_config_value("refine", "train_split_ratio")),
        "min_samples_to_train": _to_int(
            _config_value("refine", "min_samples_to_train")
        ),
    }


def get_llm_api_config(overrides=None):
    model_config = get_model_config(overrides)
    proxy_url = _resolve_toml_value(overrides, "llm_api_proxy_url", "llm_api", "proxy_url")
    base_url = _resolve_toml_value(overrides, "llm_api_base_url", "llm_api", "base_url")
    api_key = _resolve_toml_value(overrides, "llm_api_key", "llm_api", "api_key")

    if not base_url:
        base_url = os.getenv("LLM_API_BASE_URL")
    if not proxy_url:
        proxy_url = os.getenv("LLM_API_PROXY_URL")
    if not api_key:
        api_key = os.getenv("LLM_API_KEY")

    read_timeout_seconds = _resolve_toml_value(
        overrides,
        "llm_api_read_timeout_seconds",
        "llm_api",
        "read_timeout_seconds",
        30,
    )

    merged_overrides = _merged_overrides(overrides)
    if "llm_api_read_timeout_seconds" not in merged_overrides:
        env_read_timeout_seconds = os.getenv("LLM_API_READ_TIMEOUT_SECONDS")
        if env_read_timeout_seconds is not None:
            read_timeout_seconds = env_read_timeout_seconds

    reasoning_effort = model_config.get("reasoning_effort")

    proxies = None
    if proxy_url:
        proxies = {
            "http": proxy_url,
            "https": proxy_url,
            "HTTP": proxy_url,
            "HTTPS": proxy_url,
        }

    return {
        "base_url": base_url,
        "api_key": api_key,
        "request_timeout": _normalize_request_timeout(
            read_timeout_seconds,
        ),
        "reasoning_effort": reasoning_effort,
        "unsupported_reasoning_models": model_config.get(
            "unsupported_reasoning_models", set()
        ),
        "proxies": proxies,
        "chat_completions_path": _config_value(
            "llm_api", "chat_completions_path", "/chat/completions"
        ),
        "responses_path": _config_value("llm_api", "responses_path", "/responses"),
        "files_path": _config_value("llm_api", "files_path", "/files"),
        "fine_tuning_jobs_path": _config_value(
            "llm_api", "fine_tuning_jobs_path", "/fine_tuning/jobs"
        ),
    }


def get_oauth2_config():
    user_id = _config_value_from_sections(("oauth2", "oauth"), "user_id")
    user_secret = _config_value_from_sections(("oauth2", "oauth"), "user_secret")

    if not user_id:
        user_id = (
            os.getenv("OAUTH2_USER_ID")
            or os.getenv("product_build_user_id_key")
            or os.getenv("osd_build_user_id_key")
        )

    if not user_secret:
        user_secret = (
            os.getenv("OAUTH2_USER_SECRET")
            or os.getenv("product_build_user_secret_key")
            or os.getenv("osd_build_user_secret_key")
        )

    llm_api_scope = _config_value_from_sections(
        ("oauth2", "oauth"),
        "llm_api_scope",
        "generate_code/openid generate_code/use",
    )
    if not llm_api_scope:
        llm_api_scope = os.getenv("OAUTH2_TOKEN_LLM_API_SCOPE") or "generate_code/openid generate_code/use"

    return {
        "token_url": _config_value_from_sections(("oauth2", "oauth"), "token_url"),
        "user_id": user_id,
        "user_secret": user_secret,
        "token_cache_file": _config_value_from_sections(
            ("oauth2", "oauth"), "token_cache_file"
        ),
        "refresh_buffer_seconds": _to_int(
            _config_value_from_sections(
                ("oauth2", "oauth"), "refresh_buffer_seconds", 60
            ),
            default=60,
        ),
        "llm_api_scope": llm_api_scope,
    }
