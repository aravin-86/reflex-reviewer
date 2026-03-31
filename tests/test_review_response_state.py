import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from reflex_reviewer.review_response_state import ReviewResponseStateStore


class ReviewResponseStateStoreTests(unittest.TestCase):
    def test_set_and_get_previous_response_id(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            state_file = Path(tmp_dir) / "response_state.json"
            store = ReviewResponseStateStore(state_file_path=state_file, ttl_days=30)

            state_key = "PRODUCT:control-plane:pr:123"
            store.set_previous_response_id(state_key, "resp_abc")

            self.assertEqual(store.get_previous_response_id(state_key), "resp_abc")

    def test_get_purges_entries_older_than_ttl(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            state_file = Path(tmp_dir) / "response_state.json"
            now = datetime.now(timezone.utc)
            old_timestamp = (now - timedelta(days=31)).isoformat()
            fresh_timestamp = (now - timedelta(days=1)).isoformat()
            state_file.write_text(
                json.dumps(
                    {
                        "entries": {
                            "PRODUCT:control-plane:pr:1": {
                                "previous_response_id": "resp_old",
                                "updated_at": old_timestamp,
                            },
                            "PRODUCT:control-plane:pr:2": {
                                "previous_response_id": "resp_fresh",
                                "updated_at": fresh_timestamp,
                            },
                        }
                    }
                ),
                encoding="utf-8",
            )

            store = ReviewResponseStateStore(state_file_path=state_file, ttl_days=30)

            self.assertIsNone(
                store.get_previous_response_id("PRODUCT:control-plane:pr:1")
            )
            self.assertEqual(
                store.get_previous_response_id("PRODUCT:control-plane:pr:2"),
                "resp_fresh",
            )

            persisted_state = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertNotIn("PRODUCT:control-plane:pr:1", persisted_state["entries"])

    def test_invalid_state_file_is_handled_gracefully(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            state_file = Path(tmp_dir) / "response_state.json"
            state_file.write_text("not-json", encoding="utf-8")
            store = ReviewResponseStateStore(state_file_path=state_file, ttl_days=30)

            self.assertIsNone(store.get_previous_response_id("PRODUCT:repo:pr:123"))


if __name__ == "__main__":
    unittest.main()
