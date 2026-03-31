import unittest
from unittest.mock import Mock, patch
import requests
from tenacity import wait_none  # type: ignore[reportMissingImports,reportMissingModuleSource]

from reflex_reviewer.vcs.bitbucket_vcs import BitbucketVCSClient


def _response_with_payload(payload):
    response = Mock()
    response.raise_for_status = Mock()
    response.json.return_value = payload
    return response


class BitbucketVCSActivitiesPaginationTests(unittest.TestCase):
    def setUp(self):
        self.client = BitbucketVCSClient(
            {
                "base_url": "https://bitbucket.example.com",
                "project": "PRODUCT",
                "repo_slug": "repo",
                "token": "test-token",
            }
        )

    @patch("reflex_reviewer.vcs.bitbucket_vcs.requests.get")
    def test_fetch_pr_activities_paginates_and_merges_pages(self, mock_get):
        mock_get.side_effect = [
            _response_with_payload(
                {
                    "values": [{"id": 1, "action": "COMMENTED"}],
                    "isLastPage": False,
                    "nextPageStart": 2,
                }
            ),
            _response_with_payload(
                {
                    "values": [{"id": 2, "action": "COMMENTED"}],
                    "isLastPage": True,
                }
            ),
        ]

        activities = self.client.fetch_pr_activities("4123", limit=2)

        self.assertEqual([a["id"] for a in activities], [1, 2])
        self.assertEqual(mock_get.call_count, 2)
        self.assertEqual(
            mock_get.call_args_list[0].kwargs["params"], {"limit": 2, "start": 0}
        )
        self.assertEqual(
            mock_get.call_args_list[1].kwargs["params"], {"limit": 2, "start": 2}
        )

    @patch("reflex_reviewer.vcs.bitbucket_vcs.requests.get")
    def test_fetch_pr_activities_keeps_parent_and_reply_when_split_across_pages(
        self, mock_get
    ):
        mock_get.side_effect = [
            _response_with_payload(
                {
                    "values": [
                        {
                            "action": "COMMENTED",
                            "comment": {"id": 100, "text": "Top-level human comment"},
                        }
                    ],
                    "isLastPage": False,
                    "nextPageStart": 1,
                }
            ),
            _response_with_payload(
                {
                    "values": [
                        {
                            "action": "COMMENTED",
                            "comment": {
                                "id": 101,
                                "text": "Reply on second page",
                                "parent": {"id": 100},
                            },
                        }
                    ],
                    "isLastPage": True,
                }
            ),
        ]

        activities = self.client.fetch_pr_activities("4123", limit=1)

        parent_ids = {
            str(a.get("comment", {}).get("parent", {}).get("id"))
            for a in activities
            if isinstance(a.get("comment"), dict)
            and isinstance(a.get("comment", {}).get("parent"), dict)
            and a.get("comment", {}).get("parent", {}).get("id") is not None
        }
        self.assertIn("100", parent_ids)
        self.assertEqual(len(activities), 2)


class BitbucketVCSRetryTests(unittest.TestCase):
    def setUp(self):
        self.client = BitbucketVCSClient(
            {
                "base_url": "https://bitbucket.example.com",
                "project": "PRODUCT",
                "repo_slug": "repo",
                "token": "test-token",
            }
        )
        self._disable_retry_backoff()

    def _disable_retry_backoff(self):
        for operation in (
            self.client._get_with_retry,
            self.client._post_with_retry,
            self.client._put_with_retry,
            self.client._delete_with_retry,
        ):
            retrying = getattr(operation, "retry", None)
            if not retrying:
                continue
            retrying.wait = wait_none()
            retrying.sleep = lambda _: None

    @patch("reflex_reviewer.vcs.bitbucket_vcs.requests.get")
    def test_fetch_pr_diff_retries_on_transient_error_and_succeeds(self, mock_get):
        mock_get.side_effect = [
            requests.exceptions.Timeout("transient timeout"),
            _response_with_payload({"diffs": []}),
        ]

        response = self.client.fetch_pr_diff("4123")

        self.assertEqual(response, {"diffs": []})
        self.assertEqual(mock_get.call_count, 2)

    @patch("reflex_reviewer.vcs.bitbucket_vcs.requests.get")
    def test_fetch_pr_diff_raises_after_retry_exhaustion(self, mock_get):
        mock_get.side_effect = requests.exceptions.ConnectionError("persistent failure")

        with self.assertRaises(requests.exceptions.RequestException):
            self.client.fetch_pr_diff("4123")

        self.assertEqual(mock_get.call_count, 3)

    @patch("reflex_reviewer.vcs.bitbucket_vcs.requests.post")
    def test_post_comment_retries_on_transient_error_and_succeeds(self, mock_post):
        mock_post.side_effect = [
            requests.exceptions.ConnectionError("temporary issue"),
            _response_with_payload({"id": 99}),
        ]

        response = self.client.post_comment("4123", "Looks good")

        self.assertEqual(response, {"id": 99})
        self.assertEqual(mock_post.call_count, 2)

    @patch("reflex_reviewer.vcs.bitbucket_vcs.requests.put")
    def test_update_comment_retries_on_transient_error_and_succeeds(self, mock_put):
        mock_put.side_effect = [
            requests.exceptions.Timeout("temporary issue"),
            _response_with_payload({"id": 100, "version": 2}),
        ]

        response = self.client.update_comment("4123", "100", "Updated", version=1)

        self.assertEqual(response, {"id": 100, "version": 2})
        self.assertEqual(mock_put.call_count, 2)

    @patch("reflex_reviewer.vcs.bitbucket_vcs.requests.delete")
    def test_delete_comment_retries_on_transient_error_and_succeeds(self, mock_delete):
        success_response = Mock()
        success_response.raise_for_status = Mock()

        mock_delete.side_effect = [
            requests.exceptions.Timeout("temporary issue"),
            success_response,
        ]

        self.client.delete_comment("4123", "100", version=7)

        self.assertEqual(mock_delete.call_count, 2)
        self.assertEqual(mock_delete.call_args.kwargs["params"], {"version": 7})


if __name__ == "__main__":
    unittest.main()
