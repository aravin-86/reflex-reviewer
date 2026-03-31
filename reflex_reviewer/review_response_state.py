import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


class ReviewResponseStateStore:
    """File-backed previous_response_id store keyed by project/repo/pr identifier."""

    def __init__(self, state_file_path, ttl_days=30):
        self._state_file_path = Path(state_file_path)
        self._ttl_days = max(int(ttl_days or 0), 0)

    def get_previous_response_id(self, key):
        state = self._load_state()
        entries = state.setdefault("entries", {})
        self._purge_expired_entries(entries)

        entry = entries.get(key)
        if not isinstance(entry, dict):
            self._save_state(state)
            return None

        previous_response_id = entry.get("previous_response_id")
        if not isinstance(previous_response_id, str) or not previous_response_id.strip():
            entries.pop(key, None)
            self._save_state(state)
            return None

        self._save_state(state)
        return previous_response_id.strip()

    def set_previous_response_id(self, key, previous_response_id):
        if not key:
            return

        cleaned_previous_response_id = str(previous_response_id or "").strip()
        if not cleaned_previous_response_id:
            return

        state = self._load_state()
        entries = state.setdefault("entries", {})
        self._purge_expired_entries(entries)
        entries[key] = {
            "previous_response_id": cleaned_previous_response_id,
            "updated_at": self._current_time().isoformat(),
        }
        self._save_state(state)

    def _load_state(self):
        if not self._state_file_path.exists():
            return {"entries": {}}

        try:
            raw_payload = self._state_file_path.read_text(encoding="utf-8")
            parsed_payload = json.loads(raw_payload)
        except (OSError, json.JSONDecodeError):
            logger.warning(
                "Unable to read review response state file. Starting with empty state.",
                exc_info=True,
            )
            return {"entries": {}}

        if not isinstance(parsed_payload, dict):
            return {"entries": {}}

        entries = parsed_payload.get("entries")
        if not isinstance(entries, dict):
            return {"entries": {}}

        return {"entries": entries}

    def _save_state(self, state):
        self._state_file_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_file_path = self._state_file_path.with_suffix(
            f"{self._state_file_path.suffix}.tmp"
        )

        try:
            tmp_file_path.write_text(
                json.dumps(state, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            tmp_file_path.replace(self._state_file_path)
        except OSError:
            logger.warning(
                "Unable to persist review response state file.",
                exc_info=True,
            )

    def _purge_expired_entries(self, entries):
        if self._ttl_days <= 0:
            entries.clear()
            return

        expiration_cutoff = self._current_time() - timedelta(days=self._ttl_days)
        keys_to_remove = []
        for key, entry in entries.items():
            if not isinstance(entry, dict):
                keys_to_remove.append(key)
                continue

            updated_at_raw = entry.get("updated_at")
            updated_at = self._parse_timestamp(updated_at_raw)
            if updated_at is None or updated_at < expiration_cutoff:
                keys_to_remove.append(key)

        for key in keys_to_remove:
            entries.pop(key, None)

    @staticmethod
    def _parse_timestamp(value):
        if not isinstance(value, str) or not value.strip():
            return None

        normalized_value = value.strip().replace("Z", "+00:00")
        try:
            parsed_timestamp = datetime.fromisoformat(normalized_value)
        except ValueError:
            return None

        if parsed_timestamp.tzinfo is None:
            return parsed_timestamp.replace(tzinfo=timezone.utc)

        return parsed_timestamp.astimezone(timezone.utc)

    @staticmethod
    def _current_time():
        return datetime.now(timezone.utc)