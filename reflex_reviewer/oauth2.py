import json
import logging
import time
from pathlib import Path

from authlib.integrations.requests_client import (
    OAuth2Session,
)  # pyright: ignore[reportMissingModuleSource]

from .config import get_oauth2_config


logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = (10, 30)


def _configure_cli_logging():
    root_logger = logging.getLogger()
    if root_logger.handlers:
        return

    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s:%(name)s:%(message)s",
    )


def _get_runtime_oauth2_config():
    raw_config = get_oauth2_config()

    refresh_buffer_seconds = raw_config.get("refresh_buffer_seconds")
    try:
        refresh_buffer_seconds = int(str(refresh_buffer_seconds or "60"))
    except (TypeError, ValueError):
        refresh_buffer_seconds = 60

    token_cache_file = Path(
        str(
            raw_config.get("token_cache_file")
        )
    )

    return {
        "token_url": str(raw_config.get("token_url") or "").strip(),
        "user_id": str(raw_config.get("user_id") or "").strip(),
        "user_secret": str(raw_config.get("user_secret") or "").strip(),
        "token_cache_file": token_cache_file,
        "refresh_buffer_seconds": refresh_buffer_seconds,
        "llm_api_scope": str(raw_config.get("llm_api_scope") or "").strip(),
    }


def _load_cached_token(token_cache_file):
    if not token_cache_file.exists():
        return None

    try:
        data = json.loads(token_cache_file.read_text())
        if data.get("access_token") and data.get("expires_at"):
            return data
    except Exception as exc:
        logger.warning("Unable to read token cache, regenerating token: %s", exc)
    return None


def _is_token_valid(cached_token, refresh_buffer_seconds, requested_scope=None):
    now = int(time.time())
    if cached_token.get("expires_at", 0) <= (now + refresh_buffer_seconds):
        return False

    cached_scope = cached_token.get("scope")
    normalized_requested_scope = requested_scope or ""
    normalized_cached_scope = cached_scope or ""
    return normalized_cached_scope == normalized_requested_scope


def _save_cached_token(token, expires_in, token_cache_file, scope=None):
    expires_at = int(time.time()) + int(expires_in)
    payload = {
        "access_token": token,
        "expires_at": expires_at,
        "scope": scope or "",
    }
    token_cache_file.parent.mkdir(parents=True, exist_ok=True)
    token_cache_file.write_text(json.dumps(payload))


def _request_new_token(runtime_config, scope=None):
    user_id = runtime_config["user_id"]
    user_secret = runtime_config["user_secret"]
    token_url = runtime_config["token_url"]

    if not user_id or not user_secret:
        raise ValueError(
            "Missing credentials. Set OAUTH2_USER_ID/OAUTH2_USER_SECRET."
        )

    client = OAuth2Session(user_id, user_secret, scope=scope)
    token_response = client.fetch_token(
        token_url,
        grant_type="client_credentials",
        auth=(user_id, user_secret),
        timeout=REQUEST_TIMEOUT,
    )

    access_token = token_response.get("access_token")
    expires_in = token_response.get("expires_in", 3600)
    if not access_token:
        raise ValueError("Token endpoint did not return access_token")

    _save_cached_token(
        access_token,
        expires_in,
        runtime_config["token_cache_file"],
        scope=scope,
    )
    return access_token


def get_oauth2_token():
    runtime_config = _get_runtime_oauth2_config()
    scope = runtime_config["llm_api_scope"]
    cached = _load_cached_token(runtime_config["token_cache_file"])
    if cached and _is_token_valid(
        cached,
        runtime_config["refresh_buffer_seconds"],
        requested_scope=scope,
    ):
        logger.info("Using cached IDCS OAuth token.")
        return cached["access_token"]

    logger.info("Cached token missing/expired. Fetching new IDCS OAuth token.")
    return _request_new_token(runtime_config, scope=scope)


def main():
    _configure_cli_logging()
    try:
        access_token = get_oauth2_token()
    except Exception:
        logger.exception("Failed to fetch IDCS OAuth token.")
        return 1

    print(access_token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
