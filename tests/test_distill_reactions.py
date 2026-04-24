import unittest

from reflex_reviewer.distill import SENTIMENT_ACCEPTED, SENTIMENT_REJECTED
from reflex_reviewer.distill_reactions import (
    extract_reaction_sentiments_from_activities,
    merge_thread_sentiments,
    split_threads_by_reaction_sentiment,
)


class DistillReactionExtractionTests(unittest.TestCase):
    def test_extract_reaction_sentiments_supports_thumbsup_summary_shape(self):
        activities = [
            {
                "action": "REACTION_ADDED",
                "comment": {
                    "id": "101",
                    "reactions": [
                        {"name": "THUMBS_UP", "count": 3},
                        {"name": "THUMBS_DOWN", "count": 1},
                    ],
                },
            }
        ]

        resolved = extract_reaction_sentiments_from_activities(activities)

        self.assertEqual(resolved, {"101": SENTIMENT_ACCEPTED})

    def test_extract_reaction_sentiments_supports_action_fallback(self):
        activities = [{"action": "THUMBS_DOWN", "comment": {"id": "202"}}]

        resolved = extract_reaction_sentiments_from_activities(activities)

        self.assertEqual(resolved, {"202": SENTIMENT_REJECTED})

    def test_extract_reaction_sentiments_skips_ambiguous_equal_positive_negative_counts(self):
        activities = [
            {
                "action": "REACTION_ADDED",
                "comment": {
                    "id": "303",
                    "reactions": [
                        {"name": "thumbs_up", "count": 2},
                        {"name": "thumbs_down", "count": 2},
                    ],
                },
            }
        ]

        resolved = extract_reaction_sentiments_from_activities(activities)

        self.assertEqual(resolved, {})


class DistillReactionSplitAndMergeTests(unittest.TestCase):
    def test_split_threads_by_reaction_sentiment_routes_resolved_threads_out_of_llm(self):
        threads = [
            {"comment_id": "1", "comment": {"id": "1", "text": "comment-1"}},
            {"comment_id": "2", "comment": {"id": "2", "text": "comment-2"}},
            {"comment_id": "3", "comment": {"id": "3", "text": "comment-3"}},
        ]

        llm_threads, overrides = split_threads_by_reaction_sentiment(
            threads,
            {
                "1": SENTIMENT_ACCEPTED,
                "3": SENTIMENT_REJECTED,
            },
            valid_sentiments={
                SENTIMENT_ACCEPTED,
                SENTIMENT_REJECTED,
            },
        )

        self.assertEqual([thread["comment_id"] for thread in llm_threads], ["2"])
        self.assertEqual(
            overrides,
            {
                "1": SENTIMENT_ACCEPTED,
                "3": SENTIMENT_REJECTED,
            },
        )

    def test_merge_thread_sentiments_prioritizes_reaction_overrides(self):
        merged = merge_thread_sentiments(
            {
                "1": SENTIMENT_REJECTED,
                "2": SENTIMENT_ACCEPTED,
            },
            {
                "1": SENTIMENT_ACCEPTED,
            },
        )

        self.assertEqual(
            merged,
            {
                "1": SENTIMENT_ACCEPTED,
                "2": SENTIMENT_ACCEPTED,
            },
        )


if __name__ == "__main__":
    unittest.main()
