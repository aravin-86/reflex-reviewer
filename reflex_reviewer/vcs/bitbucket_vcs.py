import requests
import logging
from typing import Optional
from tenacity import (  # type: ignore[reportMissingImports,reportMissingModuleSource]
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from reflex_reviewer.oauth2 import get_oauth2_token


REQUEST_TIMEOUT = (10, 60)
MAX_ERROR_BODY_CHARS = 1000
RETRYABLE_ERRORS = (requests.exceptions.RequestException,)
logger = logging.getLogger(__name__)


class BitbucketVCSClient:
    def __init__(self, config: dict):
        self._config = config
        self._base_url = config["base_url"]
        self._project = config["project"]
        self._repo_slug = config["repo_slug"]
        self._token = config.get("token") or get_oauth2_token()

    def _pr_api_url(self, pr_id: str) -> str:
        return (
            f"{self._base_url}/rest/api/1.0/projects/{self._project}"
            f"/repos/{self._repo_slug}/pull-requests/{pr_id}"
        )

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

    @retry(
        wait=wait_exponential(multiplier=1, min=2, max=20),
        stop=stop_after_attempt(3),
        retry=retry_if_exception_type(RETRYABLE_ERRORS),
        reraise=True,
    )
    def _get_with_retry(self, url: str, **kwargs):
        response = requests.get(url, timeout=REQUEST_TIMEOUT, **kwargs)
        response.raise_for_status()
        return response

    @retry(
        wait=wait_exponential(multiplier=1, min=2, max=20),
        stop=stop_after_attempt(3),
        retry=retry_if_exception_type(RETRYABLE_ERRORS),
        reraise=True,
    )
    def _post_with_retry(self, url: str, **kwargs):
        response = requests.post(url, timeout=REQUEST_TIMEOUT, **kwargs)
        response.raise_for_status()
        return response

    @retry(
        wait=wait_exponential(multiplier=1, min=2, max=20),
        stop=stop_after_attempt(3),
        retry=retry_if_exception_type(RETRYABLE_ERRORS),
        reraise=True,
    )
    def _put_with_retry(self, url: str, **kwargs):
        response = requests.put(url, timeout=REQUEST_TIMEOUT, **kwargs)
        response.raise_for_status()
        return response

    @retry(
        wait=wait_exponential(multiplier=1, min=2, max=20),
        stop=stop_after_attempt(3),
        retry=retry_if_exception_type(RETRYABLE_ERRORS),
        reraise=True,
    )
    def _delete_with_retry(self, url: str, **kwargs):
        response = requests.delete(url, timeout=REQUEST_TIMEOUT, **kwargs)
        response.raise_for_status()
        return response

    def fetch_pr_diff(self, pr_id: str) -> dict:
        resp = self._get_with_retry(
            f"{self._pr_api_url(pr_id)}/diff", headers=self._headers()
        )
        return resp.json()

    def fetch_pr_metadata(self, pr_id: str) -> tuple[str, str]:
        resp = self._get_with_retry(self._pr_api_url(pr_id), headers=self._headers())
        pr_data = resp.json()
        pr_title = (pr_data.get("title") or "").strip()
        pr_description = (pr_data.get("description") or "").strip() or "N/A"
        return pr_title, pr_description

    def fetch_pr_activities(self, pr_id: str, limit: int = 1000) -> list[dict]:
        page_size = limit if isinstance(limit, int) and limit > 0 else 1000
        start = 0
        activities = []
        pages_fetched = 0

        while True:
            resp = self._get_with_retry(
                f"{self._pr_api_url(pr_id)}/activities",
                headers=self._headers(),
                params={"limit": page_size, "start": start},
            )

            payload = resp.json() or {}
            page_values = payload.get("values")
            if isinstance(page_values, list):
                activities.extend(page_values)

            pages_fetched += 1
            if payload.get("isLastPage", True):
                break

            next_page_start = payload.get("nextPageStart")
            if not isinstance(next_page_start, (int, str)):
                logger.warning(
                    "Stopping PR activity pagination due to missing/invalid nextPageStart. pr_id=%s current_start=%s pages=%s",
                    pr_id,
                    start,
                    pages_fetched,
                )
                break

            try:
                next_start = int(next_page_start)
            except ValueError:
                logger.warning(
                    "Stopping PR activity pagination due to missing/invalid nextPageStart. pr_id=%s current_start=%s pages=%s",
                    pr_id,
                    start,
                    pages_fetched,
                )
                break

            if next_start <= start:
                logger.warning(
                    "Stopping PR activity pagination due to non-advancing nextPageStart. pr_id=%s current_start=%s next_start=%s pages=%s",
                    pr_id,
                    start,
                    next_start,
                    pages_fetched,
                )
                break

            start = next_start

        if pages_fetched > 1:
            logger.info(
                "Fetched paginated PR activities. pr_id=%s pages=%s total_activities=%s",
                pr_id,
                pages_fetched,
                len(activities),
            )

        return activities

    def post_comment(self, pr_id: str, text: str, anchor=None) -> dict:
        payload = {"text": text}
        if anchor:
            payload["anchor"] = anchor

        try:
            resp = self._post_with_retry(
                f"{self._pr_api_url(pr_id)}/comments",
                headers=self._headers(),
                json=payload,
            )
            return resp.json()
        except requests.exceptions.HTTPError as exc:
            response = getattr(exc, "response", None)
            status_code = getattr(response, "status_code", "unknown")
            response_body = ((getattr(response, "text", "") or ""))[
                :MAX_ERROR_BODY_CHARS
            ]
            anchor_preview = payload.get("anchor")
            logger.error(
                "Bitbucket comment POST failed. pr_id=%s status=%s anchor=%s response=%s",
                pr_id,
                status_code,
                anchor_preview,
                response_body,
            )
            raise
        except requests.exceptions.RequestException as exc:
            logger.error(
                "Bitbucket comment POST request error. pr_id=%s anchor=%s error=%s",
                pr_id,
                payload.get("anchor"),
                exc,
            )
            raise

    def update_comment(
        self,
        pr_id: str,
        comment_id: str,
        text: str,
        version: Optional[int] = None,
    ) -> dict:
        payload: dict[str, object] = {"text": text}
        if isinstance(version, int):
            payload["version"] = version

        try:
            resp = self._put_with_retry(
                f"{self._pr_api_url(pr_id)}/comments/{comment_id}",
                headers=self._headers(),
                json=payload,
            )
            return resp.json()
        except requests.exceptions.RequestException:
            logger.error(
                "Bitbucket comment PUT request error. pr_id=%s comment_id=%s",
                pr_id,
                comment_id,
                exc_info=True,
            )
            raise

    def delete_comment(self, pr_id: str, comment_id: str, version: int) -> None:
        try:
            self._delete_with_retry(
                f"{self._pr_api_url(pr_id)}/comments/{comment_id}",
                headers=self._headers(),
                params={"version": int(version)},
            )
        except requests.exceptions.RequestException:
            logger.error(
                "Bitbucket comment DELETE request error. pr_id=%s comment_id=%s",
                pr_id,
                comment_id,
                exc_info=True,
            )
            raise

    def get_vcs_config(self) -> dict:
        return dict(self._config)
