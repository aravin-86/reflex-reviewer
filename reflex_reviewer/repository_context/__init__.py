"""Repository-aware context package.

This package contains language adapters and orchestration helpers used to
prepare bounded repository context artifacts for review prompts.
"""

from .adapters import (  # noqa: F401
    JavaRepoContextAdapter,
    PythonRepoContextAdapter,
    get_default_repo_context_adapters,
    resolve_repo_context_adapter,
)
from .contract import RepoContextLanguageAdapter  # noqa: F401
from .service import (  # noqa: F401
    CODE_SEARCH_UNAVAILABLE,
    NO_CODE_SEARCH_DATA,
    NO_RELATED_FILE_DATA,
    NO_REPO_MAP_DATA,
    RELATED_FILES_UNAVAILABLE,
    REPO_MAP_UNAVAILABLE,
    build_repo_map_for_changed_files,
    compose_repository_context_bundle,
    extract_changed_file_paths_from_diff,
    resolve_repository_path,
    retrieve_bounded_code_search_context,
    retrieve_related_files_context,
)
