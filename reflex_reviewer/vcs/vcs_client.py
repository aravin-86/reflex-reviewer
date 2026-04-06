from abc import ABC, abstractmethod
from typing import Optional


class VCSClient(ABC):
    @abstractmethod
    def fetch_pr_diff(self, pr_id: str) -> dict: ...

    @abstractmethod
    def fetch_pr_metadata(self, pr_id: str) -> tuple[str, str]: ...

    @abstractmethod
    def fetch_pr_activities(self, pr_id: str, limit: int = 1000) -> list[dict]: ...

    @abstractmethod
    def post_comment(
        self, pr_id: str, text: str, anchor: Optional[dict] = None
    ) -> dict: ...

    @abstractmethod
    def update_comment(
        self, pr_id: str, comment_id: str, text: str, version: Optional[int] = None
    ) -> dict: ...

    @abstractmethod
    def delete_comment(self, pr_id: str, comment_id: str, version: int) -> None: ...

    @abstractmethod
    def get_vcs_config(self) -> dict: ...