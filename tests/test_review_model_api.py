import unittest
from unittest.mock import ANY, Mock, call, patch

import reflex_reviewer.review as review_module


class ReviewModelApiTests(unittest.TestCase):
    def test_normalize_comment_severity_accepts_only_supported_labels(self):
        self.assertEqual(review_module._normalize_comment_severity("critical"), "CRITICAL")
        self.assertEqual(review_module._normalize_comment_severity("MAJOR"), "MAJOR")
        self.assertEqual(review_module._normalize_comment_severity("minor"), "ADVISORY")

    def test_resolve_comment_severity_forces_advisory_for_test_paths(self):
        self.assertEqual(
            review_module._resolve_comment_severity("CRITICAL", "tests/test_review.py"),
            "ADVISORY",
        )
        self.assertEqual(
            review_module._resolve_comment_severity("MAJOR", "src/service.py"),
            "MAJOR",
        )

    def test_parse_inline_comment_payload_falls_back_to_advisory_for_unknown_severity(self):
        severity, body = review_module._parse_inline_comment_payload(
            "[BLOCKER] fix this\n\n### #TEAM-PRODUCT"
        )

        self.assertEqual(severity, "ADVISORY")
        self.assertEqual(body, "fix this")

    def test_post_inline_comment_coerces_test_comments_to_advisory(self):
        vcs_client = Mock()
        anchor = {
            "path": "tests/test_review.py",
            "line": 10,
            "lineType": "ADDED",
            "fileType": "TO",
        }

        review_module.post_inline_comment(
            vcs_client,
            pr_id=123,
            anchor=anchor,
            severity="CRITICAL",
            text="Please add edge-case coverage",
            team_name="TEAM-PRODUCT",
        )

        vcs_client.post_comment.assert_called_once()
        body = vcs_client.post_comment.call_args.args[1]
        self.assertIn("[ADVISORY]", body)

    def test_is_bot_comment_text_supports_hashtag_and_legacy_markers(self):
        self.assertTrue(
            review_module._is_bot_comment_text(
                "body\n\n### TEAM-PRODUCT", "TEAM-PRODUCT"
            )
        )
        self.assertTrue(
            review_module._is_bot_comment_text(
                "body\n\n### #TEAM-PRODUCT", "TEAM-PRODUCT"
            )
        )
        self.assertFalse(
            review_module._is_bot_comment_text(
                "body\n\n### #OTHER-TEAM", "TEAM-PRODUCT"
            )
        )

    def test_build_summary_comment_body_uses_hashtag_team_marker(self):
        body = review_module._build_summary_comment_body(
            verdict="APPROVED",
            summary="ok",
            checklist=[],
            team_name="TEAM-PRODUCT",
        )

        self.assertIn("### #TEAM-PRODUCT", body)

    def test_upsert_summary_comment_replaces_existing_summary(self):
        vcs_client = Mock()
        vcs_client.post_comment.return_value = {"id": 101}

        result = review_module.upsert_summary_comment(
            vcs_client,
            pr_id=123,
            verdict="APPROVED",
            summary="looks good",
            checklist=[],
            team_name="TEAM-PRODUCT",
            existing_summary_comment_id="45",
            existing_summary_comment_version=2,
        )

        self.assertEqual(result, {"id": 101})
        self.assertEqual(
            vcs_client.mock_calls[:2],
            [
                call.delete_comment(123, "45", version=2),
                call.post_comment(123, ANY),
            ],
        )

    def test_upsert_summary_comment_skips_when_existing_version_missing(self):
        vcs_client = Mock()

        result = review_module.upsert_summary_comment(
            vcs_client,
            pr_id=123,
            verdict="APPROVED",
            summary="looks good",
            checklist=[],
            team_name="TEAM-PRODUCT",
            existing_summary_comment_id="45",
            existing_summary_comment_version=None,
        )

        self.assertIsNone(result)
        vcs_client.delete_comment.assert_not_called()
        vcs_client.post_comment.assert_not_called()

    def test_build_previous_response_id_uses_project_repo_and_pr(self):
        previous_response_id = review_module._build_previous_response_id(
            {"project": "PRODUCT", "repo_slug": "product-control-plane"},
            123,
        )

        self.assertEqual(previous_response_id, "PRODUCT:product-control-plane:pr:123")

    def test_parse_review_payload_supports_responses_output_text(self):
        response = {
            "id": "resp_1",
            "object": "response",
            "output_text": (
                "```json\n"
                '{"verdict":"APPROVED","summary":"ok","checklist":[],"comments":[]}'
                "\n```"
            ),
        }

        parsed = review_module.parse_review_payload(response)

        self.assertEqual(parsed.get("verdict"), "APPROVED")
        self.assertEqual(parsed.get("summary"), "ok")

    @patch("reflex_reviewer.review.responses")
    def test_get_review_model_completion_uses_responses_api_when_configured(
        self, mock_responses
    ):
        mock_responses.return_value = {
            "id": "resp_2",
            "output_text": '{"verdict":"CHANGES_SUGGESTED","summary":"x","checklist":[],"comments":[]}',
        }

        with patch.object(review_module, "MODEL_ENDPOINT", "responses"):
            review_module.get_review_model_completion(
                "oca/llama4",
                "system prompt",
                "user prompt",
                pr_id=77,
                vcs_config={"project": "PRODUCT", "repo_slug": "control-plane"},
                previous_response_id="resp_prev",
                store_response=True,
                stream_response=False,
            )

        kwargs = mock_responses.call_args.kwargs
        self.assertEqual(kwargs.get("previous_response_id"), "resp_prev")
        self.assertEqual(kwargs.get("store"), True)
        self.assertEqual(kwargs.get("stream"), False)
        self.assertIn("input_items", kwargs)

    @patch("reflex_reviewer.review.get_model_completion")
    def test_get_review_model_completion_falls_back_to_chat_completions(
        self, mock_get_model_completion
    ):
        mock_get_model_completion.return_value = {
            "choices": [
                {
                    "message": {
                        "content": '{"verdict":"CHANGES_SUGGESTED","summary":"x","checklist":[],"comments":[]}'
                    }
                }
            ]
        }

        with patch.object(review_module, "MODEL_ENDPOINT", "chat_completions"):
            review_module.get_review_model_completion(
                "oca/llama4",
                "system prompt",
                "user prompt",
                pr_id=88,
                vcs_config={"project": "PRODUCT", "repo_slug": "control-plane"},
            )

        mock_get_model_completion.assert_called_once()

    @patch("reflex_reviewer.review.parse_review_payload")
    @patch("reflex_reviewer.review.get_review_model_completion")
    @patch("reflex_reviewer.review._extract_existing_comment_state")
    @patch("reflex_reviewer.review.build_existing_feedback_context")
    @patch("reflex_reviewer.review.convert_to_unified_diff_and_anchor_index")
    @patch("reflex_reviewer.review.ReviewResponseStateStore")
    @patch("reflex_reviewer.review.get_vcs_client")
    def test_run_uses_store_true_when_no_previous_response_id(
        self,
        mock_get_vcs_client,
        mock_state_store_cls,
        mock_convert_diff,
        mock_existing_feedback,
        mock_comment_state,
        mock_get_review_model_completion,
        mock_parse_review_payload,
    ):
        vcs_client = Mock()
        vcs_client.get_vcs_config.return_value = {
            "project": "PRODUCT",
            "repo_slug": "control-plane",
            "pr_id": 123,
        }
        vcs_client.fetch_pr_diff.return_value = {
            "diffs": [{"destination": {"toString": "a.py"}, "hunks": []}]
        }
        vcs_client.fetch_pr_metadata.return_value = ("title", "description")
        vcs_client.fetch_pr_activities.return_value = []
        vcs_client.post_comment.return_value = {"id": 1}
        mock_get_vcs_client.return_value = vcs_client

        mock_convert_diff.return_value = ("diff", {"by_anchor_id": {}})
        mock_existing_feedback.return_value = "No prior feedback available."
        mock_comment_state.return_value = {
            "summary_comment_id": None,
            "summary_comment_version": None,
            "unresolved_inline_comment_keys": set(),
        }

        state_store = Mock()
        state_store.get_previous_response_id.return_value = None
        mock_state_store_cls.return_value = state_store

        mock_get_review_model_completion.return_value = {
            "id": "resp_new",
            "output_text": '{"verdict":"APPROVED","summary":"ok","checklist":[],"comments":[]}',
        }
        mock_parse_review_payload.return_value = {
            "verdict": "APPROVED",
            "summary": "ok",
            "checklist": [],
            "comments": [],
        }

        with patch.object(review_module, "MODEL_ENDPOINT", "responses"):
            review_module.run(
                vcs_type="bitbucket",
                pr_id=123,
                team_name="TEAM-PRODUCT-CP-DEV",
                draft_model="oca/gpt-4.1",
                judge_model="oca/gpt-4.1",
            )

        kwargs = mock_get_review_model_completion.call_args_list[0].kwargs
        self.assertIsNone(kwargs.get("previous_response_id"))
        self.assertEqual(kwargs.get("store_response"), True)
        state_store.set_previous_response_id.assert_called_once_with(
            "PRODUCT:control-plane:pr:123",
            "resp_new",
        )

    @patch("reflex_reviewer.review.parse_review_payload")
    @patch("reflex_reviewer.review.get_review_model_completion")
    @patch("reflex_reviewer.review._extract_existing_comment_state")
    @patch("reflex_reviewer.review.build_existing_feedback_context")
    @patch("reflex_reviewer.review.convert_to_unified_diff_and_anchor_index")
    @patch("reflex_reviewer.review.ReviewResponseStateStore")
    @patch("reflex_reviewer.review.get_vcs_client")
    def test_run_reuses_previous_response_id_when_present(
        self,
        mock_get_vcs_client,
        mock_state_store_cls,
        mock_convert_diff,
        mock_existing_feedback,
        mock_comment_state,
        mock_get_review_model_completion,
        mock_parse_review_payload,
    ):
        vcs_client = Mock()
        vcs_client.get_vcs_config.return_value = {
            "project": "PRODUCT",
            "repo_slug": "control-plane",
            "pr_id": 123,
        }
        vcs_client.fetch_pr_diff.return_value = {
            "diffs": [{"destination": {"toString": "a.py"}, "hunks": []}]
        }
        vcs_client.fetch_pr_metadata.return_value = ("title", "description")
        vcs_client.fetch_pr_activities.return_value = []
        vcs_client.post_comment.return_value = {"id": 1}
        mock_get_vcs_client.return_value = vcs_client

        mock_convert_diff.return_value = ("diff", {"by_anchor_id": {}})
        mock_existing_feedback.return_value = "No prior feedback available."
        mock_comment_state.return_value = {
            "summary_comment_id": None,
            "summary_comment_version": None,
            "unresolved_inline_comment_keys": set(),
        }

        state_store = Mock()
        state_store.get_previous_response_id.return_value = "resp_prev"
        mock_state_store_cls.return_value = state_store

        mock_get_review_model_completion.return_value = {
            "id": "resp_latest",
            "output_text": '{"verdict":"APPROVED","summary":"ok","checklist":[],"comments":[]}',
        }
        mock_parse_review_payload.return_value = {
            "verdict": "APPROVED",
            "summary": "ok",
            "checklist": [],
            "comments": [],
        }

        with patch.object(review_module, "MODEL_ENDPOINT", "responses"):
            review_module.run(
                vcs_type="bitbucket",
                pr_id=123,
                team_name="TEAM-PRODUCT-CP-DEV",
                draft_model="oca/gpt-4.1",
                judge_model="oca/gpt-4.1",
            )

        kwargs = mock_get_review_model_completion.call_args_list[0].kwargs
        self.assertEqual(kwargs.get("previous_response_id"), "resp_prev")
        self.assertEqual(kwargs.get("store_response"), False)
        state_store.set_previous_response_id.assert_called_once_with(
            "PRODUCT:control-plane:pr:123",
            "resp_latest",
        )


if __name__ == "__main__":
    unittest.main()
