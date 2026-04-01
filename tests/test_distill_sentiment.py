import unittest
from unittest.mock import patch

from reflex_reviewer.distill import (
    SENTIMENT_ACCEPTED,
    SENTIMENT_REJECTED,
    SENTIMENT_UNSURE,
    _build_rejected_preference_pair,
    _build_batched_sentiment_messages,
    _build_comment_threads,
    _comment_category,
    _comment_id,
    _extract_dpo_pairs_from_threads,
    _format_comment_reply_count_table,
    _is_bot_comment_text,
    _is_root_comment,
    _is_summary_comment_text,
    _parse_batched_sentiment_response,
    _parent_comment_id,
    _select_top_comment_threads,
)


class DistillThreadAssociationTests(unittest.TestCase):
    def test_bot_comment_detection_supports_hashtag_and_legacy_markers(self):
        self.assertTrue(_is_bot_comment_text("text\n\n### TEAM-ONE", "TEAM-ONE"))
        self.assertTrue(_is_bot_comment_text("text\n\n### #TEAM-ONE", "TEAM-ONE"))
        self.assertFalse(_is_bot_comment_text("text\n\n### #TEAM-TWO", "TEAM-ONE"))

    def test_summary_comment_detection_uses_marker_and_summary_sections(self):
        summary_text = (
            "### #TEAM-ONE\n\n"
            "**Verdict:** `APPROVED`\n\n"
            "**Summary:** Looks good\n\n"
            "**Checklist**\n"
            "- None"
        )

        self.assertTrue(_is_summary_comment_text(summary_text, "TEAM-ONE"))
        self.assertEqual(
            _comment_category({"text": summary_text}, "TEAM-ONE"), "summary-comment"
        )

    def test_build_comment_threads_associates_replies_with_normalized_parent_id(self):
        activities = [
            {
                "action": "COMMENTED",
                "comment": {"id": 100, "text": "Root comment"},
            },
            {
                "action": "COMMENTED",
                "comment": {
                    "id": 101,
                    "text": "Reply using parent.id as string",
                    "parent": {"id": "100"},
                },
            },
            {
                "action": "COMMENTED",
                "comment": {
                    "id": 102,
                    "text": "Reply using parentId fallback",
                    "parentId": 100,
                },
            },
        ]

        root_comments, replies_by_parent = _build_comment_threads(activities)

        self.assertEqual(len(root_comments), 1)
        self.assertEqual(_comment_id(root_comments[0]), "100")
        self.assertEqual(len(replies_by_parent.get("100", [])), 2)

    def test_build_comment_threads_does_not_treat_replies_as_roots(self):
        activities = [
            {
                "action": "COMMENTED",
                "comment": {"id": "200", "text": "Thread root"},
            },
            {
                "action": "COMMENTED",
                "comment": {
                    "id": "201",
                    "text": "Thread reply",
                    "parent": {"id": 200},
                },
            },
        ]

        root_comments, replies_by_parent = _build_comment_threads(activities)

        self.assertEqual([c.get("text") for c in root_comments], ["Thread root"])
        self.assertEqual(
            [r.get("text") for r in replies_by_parent.get("200", [])],
            ["Thread reply"],
        )

    def test_build_comment_threads_reads_embedded_replies_from_comment_comments(self):
        activities = [
            {
                "action": "COMMENTED",
                "comment": {
                    "id": 300,
                    "text": "Root with nested replies",
                    "comments": [
                        {"id": 301, "text": "Embedded reply 1"},
                        {"id": 302, "text": "Embedded reply 2"},
                    ],
                },
            }
        ]

        root_comments, replies_by_parent = _build_comment_threads(activities)

        self.assertEqual(len(root_comments), 1)
        self.assertEqual(_comment_id(root_comments[0]), "300")
        self.assertEqual(
            [r.get("id") for r in replies_by_parent.get("300", [])],
            [301, 302],
        )

    def test_build_comment_threads_deduplicates_when_embedded_and_standalone_reply_repeat(
        self,
    ):
        activities = [
            {
                "action": "COMMENTED",
                "comment": {
                    "id": 400,
                    "text": "Root with duplicate reply sources",
                    "comments": [
                        {"id": 401, "text": "Same reply surfaced in nested list"},
                    ],
                },
            },
            {
                "action": "COMMENTED",
                "comment": {
                    "id": 401,
                    "text": "Same reply surfaced in nested list",
                    "parent": {"id": 400},
                },
            },
        ]

        _, replies_by_parent = _build_comment_threads(activities)

        replies = replies_by_parent.get("400", [])
        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0].get("id"), 401)

    def test_parent_comment_id_prefers_parent_block_then_parent_id_fallback(self):
        self.assertEqual(_parent_comment_id({"parent": {"id": " 77 "}}), "77")
        self.assertEqual(_parent_comment_id({"parentId": 88}), "88")
        self.assertIsNone(_parent_comment_id({"parent": {"id": "  "}}))


class DistillTopThreadsAndBatchedSentimentTests(unittest.TestCase):
    def test_select_top_comment_threads_orders_by_reply_count_and_limits(self):
        root_comments = []
        replies_by_parent = {}

        for index in range(25):
            comment_id = str(1000 + index)
            root_comments.append({"id": comment_id, "text": f"comment-{index}"})

            reply_count = index
            replies_by_parent[comment_id] = [
                {"id": f"{comment_id}-r-{i}", "text": f"reply-{i}"}
                for i in range(reply_count)
            ]

        selected = _select_top_comment_threads(
            root_comments, replies_by_parent, limit=20
        )

        self.assertEqual(len(selected), 20)
        selected_ids = [thread["comment_id"] for thread in selected]
        self.assertEqual(selected_ids[0], "1024")
        self.assertEqual(selected_ids[-1], "1005")

    def test_build_batched_sentiment_messages_includes_comment_and_reply_payload(self):
        comment_threads = [
            {
                "comment_id": "101",
                "comment": {
                    "id": "101",
                    "text": "Please fix this\n\n### #TEAM-ONE",
                },
                "replies": [{"id": "201", "text": "Fixed now"}],
                "replies_count": 1,
            }
        ]

        messages = _build_batched_sentiment_messages(comment_threads, team_name="TEAM-ONE")

        self.assertEqual(len(messages), 2)
        self.assertIn('"comment_id": "101"', messages[1]["content"])
        self.assertIn('"severity": "ADVISORY"', messages[1]["content"])
        self.assertIn('"replies": ["Fixed now"]', messages[1]["content"])

    def test_build_batched_sentiment_messages_forces_advisory_for_test_file_comments(self):
        comment_threads = [
            {
                "comment_id": "501",
                "comment": {
                    "id": "501",
                    "text": "[CRITICAL] Add better edge assertions\n\n### #TEAM-ONE",
                    "anchor": {"path": "tests/test_service.py", "line": 30},
                },
                "replies": [],
                "replies_count": 0,
            }
        ]

        messages = _build_batched_sentiment_messages(comment_threads, team_name="TEAM-ONE")

        self.assertIn('"severity": "ADVISORY"', messages[1]["content"])

    def test_parse_batched_sentiment_response_accepts_valid_items_only(self):
        response = {
            "choices": [
                {
                    "message": {
                        "content": (
                            '{"results":[{"comment_id":"100","sentiment":"ACCEPTED"},'
                            '{"comment_id":"101","sentiment":"MAYBE"},'
                            '{"id":"102","sentiment":"REJECTED"}]}'
                        )
                    }
                }
            ]
        }

        parsed = _parse_batched_sentiment_response(response)

        self.assertEqual(parsed.get("100"), SENTIMENT_ACCEPTED)
        self.assertEqual(parsed.get("102"), SENTIMENT_REJECTED)
        self.assertNotIn("101", parsed)

    @patch("reflex_reviewer.distill.chat_completions")
    def test_single_batched_llm_call_for_top_20_threads(self, mock_chat_completions):
        top_threads = []
        for idx in range(20):
            comment_id = str(idx + 1)
            top_threads.append(
                {
                    "comment_id": comment_id,
                    "comment": {"id": comment_id, "text": f"comment-{comment_id}"},
                    "replies": [{"id": f"r-{comment_id}", "text": "reply"}],
                    "replies_count": 1,
                }
            )

        mock_chat_completions.return_value = {
            "choices": [
                {
                    "message": {
                        "content": '{"results":[{"comment_id":"1","sentiment":"UNSURE"}]}'
                    }
                }
            ]
        }

        from reflex_reviewer.distill import _resolve_thread_sentiments_with_llm

        result = _resolve_thread_sentiments_with_llm(
            top_threads,
            model_endpoint="chat_completions",
        )

        self.assertEqual(result.get("1"), SENTIMENT_UNSURE)
        mock_chat_completions.assert_called_once()

    @patch("reflex_reviewer.distill.responses")
    def test_single_batched_llm_call_uses_responses_endpoint(self, mock_responses):
        top_threads = [
            {
                "comment_id": "1",
                "comment": {"id": "1", "text": "comment-1"},
                "replies": [{"id": "r-1", "text": "reply"}],
                "replies_count": 1,
            }
        ]

        mock_responses.return_value = {
            "id": "resp_1",
            "object": "response",
            "output_text": '{"results":[{"comment_id":"1","sentiment":"ACCEPTED"}]}',
        }

        from reflex_reviewer.distill import _resolve_thread_sentiments_with_llm

        result = _resolve_thread_sentiments_with_llm(
            top_threads,
            model_endpoint="responses",
        )

        self.assertEqual(result.get("1"), SENTIMENT_ACCEPTED)
        mock_responses.assert_called_once()


class DistillCommentReplyTableFormattingTests(unittest.TestCase):
    def test_columns_before_starts_with_are_aligned_vertically(self):
        rows = [
            {
                "comment_id": "7",
                "category": "bot-comment",
                "replies_count": 0,
                "llm_sentiment": "UNSURE",
                "starts_with": "short",
            },
            {
                "comment_id": "14736430",
                "category": "summary-comment",
                "replies_count": 12,
                "llm_sentiment": "ACCEPTED",
                "starts_with": "much longer trailing content",
            },
        ]

        rendered = _format_comment_reply_count_table(rows)
        lines = rendered.splitlines()

        pipe_positions = [index for index, char in enumerate(lines[0]) if char == "|"]

        for line in lines[1:]:
            line_pipe_positions = [
                index for index, char in enumerate(line) if char == "|"
            ]
            self.assertEqual(line_pipe_positions[:5], pipe_positions[:5])

    def test_renders_llm_sentiment_before_starts_with_header(self):
        rendered = _format_comment_reply_count_table([])
        header_line = rendered.splitlines()[0]

        self.assertIn("| llm_sentiment | starts_with |", header_line)

    def test_sanitizes_newlines_tabs_and_pipes_in_cell_values(self):
        rows = [
            {
                "comment_id": "101",
                "category": "human-comment",
                "replies_count": 1,
                "llm_sentiment": "ACCEPTED\nREJECTED\twith|pipe",
                "starts_with": "line 1\nline 2\twith|pipe",
            }
        ]

        rendered = _format_comment_reply_count_table(rows)
        data_line = rendered.splitlines()[2]

        self.assertIn("ACCEPTED REJECTED with\\|pipe", data_line)
        self.assertIn("line 1 line 2 with\\|pipe", data_line)
        self.assertNotIn("\n", data_line)
        self.assertNotIn("\t", data_line)

    def test_renders_default_row_when_no_rows(self):
        rendered = _format_comment_reply_count_table([])
        lines = rendered.splitlines()

        self.assertEqual(len(lines), 3)
        self.assertIn("N/A", lines[2])
        self.assertIn("| 0", lines[2])
        self.assertIn("|  |", lines[2])


class DistillRejectedPreferencePairTests(unittest.TestCase):
    def test_build_rejected_preference_pair_returns_pair_for_human_thread(self):
        pair = _build_rejected_preference_pair(
            "prompt text",
            "original human root comment",
            [{"id": "11", "text": "latest human reply"}],
            "10",
            "human-comment",
        )

        self.assertEqual(
            pair,
            {
                "prompt": "prompt text",
                "chosen": "latest human reply",
                "rejected": "original human root comment",
            },
        )

    def test_build_rejected_preference_pair_returns_pair_for_bot_thread(self):
        pair = _build_rejected_preference_pair(
            "prompt text",
            "original bot line comment",
            [{"id": "21", "text": "latest user reply"}],
            "20",
            "bot-comment",
        )

        self.assertEqual(
            pair,
            {
                "prompt": "prompt text",
                "chosen": "latest user reply",
                "rejected": "original bot line comment",
            },
        )

    def test_build_rejected_preference_pair_returns_none_for_missing_reply(self):
        with self.assertLogs("reflex_reviewer.distill", level="WARNING") as logs:
            pair = _build_rejected_preference_pair(
                "prompt text",
                "original comment",
                [{"id": "31", "text": "   "}],
                "30",
                "human-comment",
            )

        self.assertIsNone(pair)
        self.assertTrue(
            any(
                "Skipping rejected human-comment thread without a non-empty reply"
                in log_line
                for log_line in logs.output
            )
        )


class DistillDpoExtractionTests(unittest.TestCase):
    def test_extract_dpo_pairs_includes_accepted_and_rejected_bot_comments(self):
        bot_comment_accepted = "Please rename variable\n\n### #TEAM-ONE"
        bot_comment_rejected = "Please split this function\n\n### #TEAM-ONE"
        summary_comment = (
            "### #TEAM-ONE\n\n"
            "**Verdict:** `APPROVED`\n\n"
            "**Summary:** Looks good\n\n"
            "**Checklist**\n"
            "- None"
        )

        top_threads = [
            {
                "comment_id": "1",
                "comment": {"id": "1", "text": bot_comment_accepted},
                "replies": [],
                "replies_count": 0,
            },
            {
                "comment_id": "2",
                "comment": {"id": "2", "text": bot_comment_rejected},
                "replies": [
                    {"id": "2-1", "text": "Initial response"},
                    {"id": "2-2", "text": "Addressed with a refactor"},
                ],
                "replies_count": 2,
            },
            {
                "comment_id": "3",
                "comment": {"id": "3", "text": summary_comment},
                "replies": [{"id": "3-1", "text": "Thanks"}],
                "replies_count": 1,
            },
        ]
        sentiment_by_comment_id = {
            "1": SENTIMENT_ACCEPTED,
            "2": SENTIMENT_REJECTED,
            "3": SENTIMENT_ACCEPTED,
        }

        dpo_pairs, metrics = _extract_dpo_pairs_from_threads(
            top_threads,
            sentiment_by_comment_id,
            prompt_text="prompt",
            team_name="TEAM-ONE",
        )

        self.assertEqual(metrics["eligible_bot_comment_count"], 2)
        self.assertEqual(metrics["accepted_count"], 1)
        self.assertEqual(metrics["rejected_count"], 1)
        self.assertEqual(metrics["unsure_count"], 0)

        self.assertEqual(
            dpo_pairs,
            [
                {
                    "prompt": "prompt",
                    "chosen": bot_comment_accepted,
                    "rejected": "N/A",
                },
                {
                    "prompt": "prompt",
                    "chosen": "Addressed with a refactor",
                    "rejected": bot_comment_rejected,
                },
            ],
        )

    def test_extract_dpo_pairs_uses_only_root_comments_for_human_and_bot(self):
        bot_comment_accepted = "Please rename variable\n\n### #TEAM-ONE"
        bot_comment_rejected = "Please split this function\n\n### #TEAM-ONE"
        human_comment_accepted = "Can we add a docstring?"
        human_comment_rejected = "I think this needs a test"
        reply_comment = "This is a reply and must never become a DPO sample"

        top_threads = [
            {
                "comment_id": "b-accepted",
                "comment": {"id": "b-accepted", "text": bot_comment_accepted},
                "replies": [],
                "replies_count": 0,
            },
            {
                "comment_id": "b-rejected",
                "comment": {"id": "b-rejected", "text": bot_comment_rejected},
                "replies": [{"id": "rb-1", "text": "Refactored as requested"}],
                "replies_count": 1,
            },
            {
                "comment_id": "h-accepted",
                "comment": {"id": "h-accepted", "text": human_comment_accepted},
                "replies": [{"id": "rh-1", "text": "Done"}],
                "replies_count": 1,
            },
            {
                "comment_id": "h-rejected",
                "comment": {"id": "h-rejected", "text": human_comment_rejected},
                "replies": [{"id": "rh-2", "text": "Keeping current approach"}],
                "replies_count": 1,
            },
            {
                "comment_id": "reply-comment",
                "comment": {
                    "id": "reply-comment",
                    "text": reply_comment,
                    "parent": {"id": "h-rejected"},
                },
                "replies": [{"id": "rr-1", "text": "Nested reply"}],
                "replies_count": 1,
            },
        ]

        sentiment_by_comment_id = {
            "b-accepted": SENTIMENT_ACCEPTED,
            "b-rejected": SENTIMENT_REJECTED,
            "h-accepted": SENTIMENT_ACCEPTED,
            "h-rejected": SENTIMENT_REJECTED,
            "reply-comment": SENTIMENT_ACCEPTED,
        }

        self.assertFalse(_is_root_comment(top_threads[-1]["comment"]))

        dpo_pairs, metrics = _extract_dpo_pairs_from_threads(
            top_threads,
            sentiment_by_comment_id,
            prompt_text="prompt",
            team_name="TEAM-ONE",
        )

        self.assertEqual(metrics["eligible_bot_comment_count"], 2)
        self.assertEqual(metrics["accepted_count"], 1)
        self.assertEqual(metrics["rejected_count"], 1)
        self.assertEqual(metrics["accepted_human_comment_count"], 1)
        self.assertEqual(metrics["rejected_human_comment_count"], 1)

        self.assertEqual(len(dpo_pairs), 4)
        self.assertEqual(
            dpo_pairs,
            [
                {
                    "prompt": "prompt",
                    "chosen": bot_comment_accepted,
                    "rejected": "N/A",
                },
                {
                    "prompt": "prompt",
                    "chosen": "Refactored as requested",
                    "rejected": bot_comment_rejected,
                },
                {
                    "prompt": "prompt",
                    "chosen": human_comment_accepted,
                    "rejected": "N/A",
                },
                {
                    "prompt": "prompt",
                    "chosen": "Keeping current approach",
                    "rejected": human_comment_rejected,
                },
            ],
        )
        self.assertFalse(any(pair["chosen"] == reply_comment for pair in dpo_pairs))


if __name__ == "__main__":
    unittest.main()
