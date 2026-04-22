from abc import ABC, abstractmethod
from pathlib import Path
from typing import List


class RepoContextLanguageAdapter(ABC):
    language_name = ""
    supported_extensions = ()

    def supports_path(self, relative_path):
        extension = Path(str(relative_path or "")).suffix.lower()
        return extension in set(self.supported_extensions)

    @abstractmethod
    def build_repo_map_entry(self, relative_path, file_text):
        raise NotImplementedError

    @abstractmethod
    def resolve_related_file_paths(self, relative_path, file_text) -> List[str]:
        raise NotImplementedError

    @abstractmethod
    def derive_code_search_terms(self, relative_path, file_text) -> List[str]:
        raise NotImplementedError
