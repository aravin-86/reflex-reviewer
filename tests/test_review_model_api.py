import unittest
from unittest.mock import ANY, Mock, patch

import reflex_reviewer.review as review_module


class ReviewModelApiTests(unittest.TestCase):
    def test_extract_pr_description_summary_and_changes_ignores_test_results(self):
        description = """Summary
OSD-11172: Implementation to use evergreen OS image for Proxy and GSM compute instance.
Changes
OSD-11172: Implementation to use evergreen OS image for Proxy and GSM compute instance.
Test Results
Tested in DEV@FRA
Logs: https://example.com/build-logs
Does this PULL Request depend on any other Pull Requests?"""

        summary, changes = review_module._extract_pr_description_summary_and_changes(
            description
        )

        self.assertEqual(
            summary,
            "OSD-11172: Implementation to use evergreen OS image for Proxy and GSM compute instance.",
        )
        self.assertEqual(
            changes,
            "OSD-11172: Implementation to use evergreen OS image for Proxy and GSM compute instance.",
        )

    def test_build_review_purpose_uses_title_summary_and_skips_duplicate_changes(self):
        description = """Summary
Feature summary text.
Changes
Feature summary text.
Test Results
Smoke tested in DEV"""

        purpose = review_module._build_review_purpose(
            "OSD-11172: Evergreen image migration", description
        )

        self.assertEqual(
            purpose,
            "PR Title: OSD-11172: Evergreen image migration | Summary: Feature summary text.",
        )

    def test_build_review_purpose_falls_back_when_pr_metadata_is_missing(self):
        self.assertEqual(
            review_module._build_review_purpose("", "N/A"),
            review_module.PURPOSE_FALLBACK,
        )

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

    def test_parse_inline_comment_payload_strips_trailing_team_signature(self):
        severity, body = review_module._parse_inline_comment_payload(
            "[CRITICAL] Add better edge assertions\r\n\r\n### #TEAM-PRODUCT"
        )

        self.assertEqual(severity, "CRITICAL")
        self.assertEqual(body, "Add better edge assertions")

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

    def test_is_root_comment_identifies_root_and_reply_comments(self):
        self.assertTrue(review_module._is_root_comment({"id": "1", "text": "root"}))
        self.assertTrue(review_module._is_root_comment({"id": "1", "parent": {"id": ""}}))
        self.assertFalse(
            review_module._is_root_comment({"id": "2", "parent": {"id": "1"}})
        )

    def test_build_existing_feedback_context_uses_only_root_human_and_bot_comments(self):
        activities = [
            {
                "action": "COMMENTED",
                "comment": {
                    "id": 10,
                    "text": "Human root comment",
                    "author": {"displayName": "Alice"},
                },
            },
            {
                "action": "COMMENTED",
                "comment": {
                    "id": 11,
                    "text": "[CRITICAL] Bot root issue\n\n### #TEAM-ONE",
                    "anchor": {
                        "srcPath": {"toString": "a/src/service.py"},
                        "srcLine": "42",
                    },
                },
            },
            {
                "action": "COMMENTED",
                "comment": {
                    "id": 12,
                    "parent": {"id": 10},
                    "text": "Human reply should not be included",
                    "author": {"displayName": "Bob"},
                },
            },
            {
                "action": "COMMENTED",
                "comment": {
                    "id": 13,
                    "text": "### #TEAM-ONE\n\n<!-- reflex-reviewer-summary -->\n\n**Verdict:** `APPROVED`\n\n**Summary:** ok\n\n**Checklist**\n- None",
                },
            },
        ]

        context = review_module.build_existing_feedback_context(activities, "TEAM-ONE")

        self.assertIn("Human (Alice): Human root comment", context)
        self.assertIn(
            "Bot (severity=CRITICAL) | file=a/src/service.py | line=42: Bot root issue",
            context,
        )
        self.assertNotIn("reply should not be included", context)
        self.assertNotIn("**Verdict:**", context)

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
        self.assertIn("<!-- reflex-reviewer-summary -->", body)

    def test_upsert_summary_comment_posts_without_deleting_existing_summary(self):
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
        vcs_client.delete_comment.assert_not_called()
        vcs_client.post_comment.assert_called_once_with(123, ANY)

    def test_upsert_summary_comment_posts_when_existing_version_missing(self):
        vcs_client = Mock()
        vcs_client.post_comment.return_value = {"id": 202}

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

        self.assertEqual(result, {"id": 202})
        vcs_client.delete_comment.assert_not_called()
        vcs_client.post_comment.assert_called_once_with(123, ANY)

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
    @patch("reflex_reviewer.review.convert_to_unified_diff_and_anchor_index")
    @patch("reflex_reviewer.review.get_vcs_client")
    def test_run_posts_inline_and_summary_without_code_side_existing_comment_dedupe(
        self,
        mock_get_vcs_client,
        mock_convert_diff,
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
            "diffs": [{"destination": {"toString": "src/service.py"}, "hunks": []}]
        }
        vcs_client.fetch_pr_metadata.return_value = ("title", "description")
        vcs_client.fetch_pr_activities.return_value = [
            {
                "action": "COMMENTED",
                "comment": {
                    "id": 700,
                    "text": "[CRITICAL] Add better edge assertions\r\n\r\n### #TEAM-ONE",
                    "anchor": {
                        "srcPath": {"toString": "a/src/service.py"},
                        "srcLine": "10",
                    },
                },
            }
        ]
        vcs_client.post_comment.return_value = {"id": 1}
        mock_get_vcs_client.return_value = vcs_client

        mock_convert_diff.return_value = (
            "diff",
            {
                "by_anchor_id": {
                    "F1-L10": {
                        "anchor": {
                            "path": "src/service.py",
                            "line": 10,
                            "lineType": "ADDED",
                            "fileType": "TO",
                        },
                        "path": "src/service.py",
                        "line": 10,
                    }
                }
            },
        )

        mock_get_review_model_completion.side_effect = [
            {"id": "draft_1"},
            {"id": "judge_1"},
        ]
        mock_parse_review_payload.side_effect = [
            {
                "verdict": "CHANGES_SUGGESTED",
                "summary": "draft",
                "checklist": [],
                "comments": [],
            },
            {
                "verdict": "CHANGES_SUGGESTED",
                "summary": "final summary",
                "checklist": [],
                "comments": [
                    {
                        "anchor_id": "F1-L10",
                        "severity": "CRITICAL",
                        "text": "Add better edge assertions",
                    }
                ],
            },
        ]

        with patch.object(review_module, "MODEL_ENDPOINT", "chat_completions"):
            review_module.run(
                vcs_type="bitbucket",
                pr_id=123,
                team_name="TEAM-ONE",
                draft_model="oca/gpt-4.1",
                judge_model="oca/gpt-4.1",
            )

        first_review_call = mock_get_review_model_completion.call_args_list[0]
        draft_user_prompt = first_review_call.args[2]
        self.assertIn(
            "Purpose (from PR title + description): PR Title: title", draft_user_prompt
        )
        self.assertNotIn("{{PURPOSE}}", draft_user_prompt)

        self.assertEqual(vcs_client.post_comment.call_count, 2)
        inline_call = vcs_client.post_comment.call_args_list[0]
        summary_call = vcs_client.post_comment.call_args_list[1]
        self.assertIn("anchor", inline_call.kwargs)
        self.assertNotIn("anchor", summary_call.kwargs)
        summary_body = summary_call.args[1]
        self.assertIn("**Verdict:**", summary_body)

    @patch("reflex_reviewer.review.parse_review_payload")
    @patch("reflex_reviewer.review.get_review_model_completion")
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
        mock_existing_feedback.return_value = "No prior root comments available."

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
        mock_existing_feedback.return_value = "No prior root comments available."

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
