import logging
import os
import re
import json

import requests  # pyright: ignore[reportMissingModuleSource]
from tenacity import (  # type: ignore[reportMissingImports,reportMissingModuleSource]
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from .config import get_litellm_config
from .oauth2 import get_oauth2_token, REQUEST_TIMEOUT

logger = logging.getLogger(__name__)
RETRYABLE_STATUS_CODES = {408, 409, 425, 429}


def _normalize_api_path(path_value, default_path):
    normalized_path = str(path_value or default_path).strip()
    if not normalized_path:
        normalized_path = default_path

    if not normalized_path.startswith("/"):
        normalized_path = f"/{normalized_path}"

    return normalized_path


def _get_litellm_runtime_config():
    litellm_config = get_litellm_config()
    base_url = litellm_config.get("base_url")
    if not base_url:
        raise ValueError(
            "LITELLM_BASE_URL is required. Pass --litellm-base-url or set LITELLM_BASE_URL."
        )

    return {
        "base_url": base_url.rstrip("/"),
        "api_key": litellm_config.get("api_key"),
        "proxies": litellm_config.get("proxies"),
        "reasoning_effort": litellm_config.get("reasoning_effort", "high"),
        "chat_completions_path": _normalize_api_path(
            litellm_config.get("chat_completions_path"),
            "/chat/completions",
        ),
        "responses_path": _normalize_api_path(
            litellm_config.get("responses_path"),
            "/responses",
        ),
        "files_path": _normalize_api_path(
            litellm_config.get("files_path"),
            "/files",
        ),
        "fine_tuning_jobs_path": _normalize_api_path(
            litellm_config.get("fine_tuning_jobs_path"),
            "/fine_tuning/jobs",
        ),
    }


class LiteLLMResponseParseError(ValueError):
    """Raised when a successful LiteLLM response cannot be parsed as expected."""


def _resolve_litellm_auth_token(runtime_config):
    api_key = str(runtime_config.get("api_key") or "").strip()
    if api_key:
        logger.info("Using LiteLLM API key authentication")
        return api_key

    logger.info("Using LiteLLM OAuth2 token authentication")
    return get_oauth2_token()


def _is_retryable_request_exception(exc):
    if isinstance(exc, requests.exceptions.HTTPError):
        response = exc.response
        if response is None:
            return True

        status_code = int(response.status_code)
        return status_code >= 500 or status_code in RETRYABLE_STATUS_CODES

    return isinstance(
        exc,
        (
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
            requests.exceptions.ProxyError,
            requests.exceptions.SSLError,
            requests.exceptions.ChunkedEncodingError,
            requests.exceptions.ContentDecodingError,
        ),
    )


def _supports_reasoning_effort(model):
    normalized_model = str(model or "").strip().lower()
    if not normalized_model:
        return False

    unsupported_reasoning_models = get_litellm_config().get(
        "unsupported_reasoning_models", set()
    )
    return not any(
        str(unsupported_model).strip().lower() in normalized_model
        for unsupported_model in unsupported_reasoning_models
        if str(unsupported_model).strip()
    )


@retry(
    wait=wait_exponential(multiplier=1, min=2, max=20),
    stop=stop_after_attempt(3),
    retry=retry_if_exception(_is_retryable_request_exception),
    reraise=True,
)
def _post_with_retry(url, **kwargs):
    response = requests.post(  # pyright: ignore[reportCallIssue]
        url,
        timeout=REQUEST_TIMEOUT,
        **kwargs,
    )
    try:
        response.raise_for_status()
    except requests.exceptions.HTTPError:
        logger.warning(
            "HTTP request failed. status_code=%s content_type=%s body_preview=%s",
            response.status_code,
            response.headers.get("Content-Type", "unknown"),
            _safe_response_preview(response.text),
        )
        raise

    return response


@retry(
    wait=wait_exponential(multiplier=1, min=2, max=20),
    stop=stop_after_attempt(3),
    retry=retry_if_exception(_is_retryable_request_exception),
    reraise=True,
)
def _get_with_retry(url, **kwargs):
    response = requests.get(  # pyright: ignore[reportCallIssue]
        url,
        timeout=REQUEST_TIMEOUT,
        **kwargs,
    )
    try:
        response.raise_for_status()
    except requests.exceptions.HTTPError:
        logger.warning(
            "HTTP request failed. status_code=%s content_type=%s body_preview=%s",
            response.status_code,
            response.headers.get("Content-Type", "unknown"),
            _safe_response_preview(response.text),
        )
        raise

    return response


def _safe_response_preview(response_text, max_chars=160):
    if not response_text:
        return ""

    normalized_text = re.sub(r"\s+", " ", str(response_text)).strip()
    if len(normalized_text) <= max_chars:
        return normalized_text

    return f"{normalized_text[:max_chars]}..."


def _extract_sse_data_payloads(response_text):
    payloads = []
    current_event_data = []

    for raw_line in str(response_text or "").splitlines():
        line = raw_line.strip()

        if not line:
            if current_event_data:
                payload = "\n".join(current_event_data).strip()
                if payload and payload != "[DONE]":
                    payloads.append(payload)
                current_event_data = []
            continue

        if line.startswith(":"):
            continue

        if line.lower().startswith("data:"):
            current_event_data.append(line[5:].strip())

    if current_event_data:
        payload = "\n".join(current_event_data).strip()
        if payload and payload != "[DONE]":
            payloads.append(payload)

    return payloads


def _parse_sse_json_events(response_text):
    payloads = _extract_sse_data_payloads(response_text)
    if not payloads:
        raise ValueError("No SSE data payloads found in response")

    parsed_events = []
    parse_error = None
    for payload in payloads:
        try:
            parsed_payload = json.loads(payload)
        except json.JSONDecodeError as exc:
            parse_error = exc
            continue

        if isinstance(parsed_payload, dict):
            parsed_events.append(parsed_payload)

    if parsed_events:
        return parsed_events

    if parse_error:
        raise ValueError("Unable to parse SSE data payload as JSON") from parse_error

    raise ValueError("SSE payload did not contain JSON objects")


def _extract_chunk_content_text(content):
    if isinstance(content, str):
        return content

    if isinstance(content, list):
        return "".join(
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        )

    return ""


def _convert_sse_events_to_chat_completion(events):
    for event in reversed(events):
        choices = event.get("choices")
        if not isinstance(choices, list) or not choices:
            continue

        first_choice = choices[0]
        if not isinstance(first_choice, dict):
            continue

        message = first_choice.get("message")
        if isinstance(message, dict) and message.get("content") is not None:
            return event

    content_parts = []
    role = "assistant"
    finish_reason = None
    choice_index = 0
    metadata_event = events[-1]

    for event in events:
        choices = event.get("choices")
        if not isinstance(choices, list) or not choices:
            continue

        first_choice = choices[0]
        if not isinstance(first_choice, dict):
            continue

        if isinstance(first_choice.get("index"), int):
            choice_index = first_choice["index"]

        delta = first_choice.get("delta")
        if isinstance(delta, dict):
            if isinstance(delta.get("role"), str):
                role = delta["role"]

            text_chunk = _extract_chunk_content_text(delta.get("content"))
            if text_chunk:
                content_parts.append(text_chunk)
        elif isinstance(first_choice.get("text"), str):
            content_parts.append(first_choice["text"])

        if first_choice.get("finish_reason") is not None:
            finish_reason = first_choice.get("finish_reason")

    combined_content = "".join(content_parts)
    if not combined_content:
        raise ValueError("No assistant content found in SSE response")

    response_id = metadata_event.get("id", "")
    model_name = metadata_event.get("model", "")
    created_at = metadata_event.get("created", 0)

    if not isinstance(response_id, str):
        response_id = ""
    if not isinstance(model_name, str):
        model_name = ""
    if not isinstance(created_at, int):
        created_at = 0

    return {
        "id": response_id,
        "object": "chat.completion",
        "created": created_at,
        "model": model_name,
        "choices": [
            {
                "index": choice_index,
                "message": {"role": role, "content": combined_content},
                "finish_reason": finish_reason,
            }
        ],
    }


def _parse_non_stream_chat_completion_response(response_text):
    events = _parse_sse_json_events(response_text)
    return _convert_sse_events_to_chat_completion(events)


def _extract_response_output_text(response_payload):
    if not isinstance(response_payload, dict):
        return ""

    output_text = response_payload.get("output_text")
    if isinstance(output_text, str) and output_text:
        return output_text

    output = response_payload.get("output")
    if not isinstance(output, list):
        return ""

    content_parts = []
    for output_item in output:
        if not isinstance(output_item, dict):
            continue

        content = output_item.get("content")
        if not isinstance(content, list):
            continue

        for part in content:
            if not isinstance(part, dict):
                continue

            text = part.get("text")
            if isinstance(text, str) and text:
                content_parts.append(text)

    return "".join(content_parts)


def _convert_sse_events_to_response_object(events):
    for event in reversed(events):
        response_payload = event.get("response")
        if isinstance(response_payload, dict):
            return response_payload

    for event in reversed(events):
        if event.get("object") == "response":
            return event

    output_text_chunks = []
    metadata_event = events[-1]

    for event in events:
        event_type = str(event.get("type", ""))
        if "output_text" not in event_type:
            continue

        delta_text = event.get("delta")
        if isinstance(delta_text, str) and delta_text:
            output_text_chunks.append(delta_text)

    combined_output_text = "".join(output_text_chunks)
    if not combined_output_text:
        raise ValueError("No response content found in SSE response")

    response_id = metadata_event.get("response_id") or metadata_event.get("id", "")
    if not isinstance(response_id, str):
        response_id = ""

    model_name = metadata_event.get("model", "")
    if not isinstance(model_name, str):
        model_name = ""

    return {
        "id": response_id,
        "object": "response",
        "model": model_name,
        "output_text": combined_output_text,
    }


def _parse_non_stream_responses_api_response(response_text):
    response_text = str(response_text or "").strip()
    if not response_text:
        raise ValueError("LiteLLM responses payload is empty")

    try:
        parsed_json = json.loads(response_text)
        if isinstance(parsed_json, dict):
            return parsed_json
    except json.JSONDecodeError:
        pass

    events = _parse_sse_json_events(response_text)
    return _convert_sse_events_to_response_object(events)


def chat_completions(model, messages, stream=False, pr_id=None):
    """Call LiteLLM Chat Completions API.

    This endpoint is best suited for single-turn style interactions.
    It is stateless in practice: callers must provide the full conversation
    history in `messages` on every request.
    """
    runtime_config = _get_litellm_runtime_config()
    token = _resolve_litellm_auth_token(runtime_config)
    payload = {"model": model, "messages": messages}

    accept_header = "text/event-stream"
    logger.info(
        "Calling LiteLLM chat completion: model=%s, stream=%s, message_count=%s",
        model,
        stream,
        len(messages),
    )
    try:
        response = _post_with_retry(
            f"{runtime_config['base_url']}{runtime_config['chat_completions_path']}",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": accept_header,
            },
            json=payload,
            stream=stream,
            proxies=runtime_config["proxies"],
        )
        logger.info(
            "Received LiteLLM chat completion successfully. model=%s pr_id=%s",
            model,
            pr_id,
        )
    except requests.exceptions.RequestException:
        logger.exception("LiteLLM chat completion request failed")
        raise

    if stream:
        return response

    try:
        return _parse_non_stream_chat_completion_response(response.text)
    except ValueError as exc:
        logger.warning(
            "LiteLLM chat completion returned unparsable event-stream response. status_code=%s content_type=%s body_preview=%s parse_error=%s",
            response.status_code,
            response.headers.get("Content-Type", "unknown"),
            _safe_response_preview(response.text),
            str(exc),
        )
        raise LiteLLMResponseParseError(
            "LiteLLM chat completion returned unparsable event-stream response"
        ) from exc


def responses(
    model,
    input_items,
    previous_response_id=None,
    store=False,
    stream=False,
    pr_id=None,
):
    """Call LiteLLM Responses API.

    This endpoint is designed as an agentic primitive and is suitable for
    multi-turn workflows (including tool-calling patterns) within a single
    response flow. It can be stateful by default: pass
    `previous_response_id` to let the API continue prior response/tool state.
    It also sends a default `reasoning.effort` value from LiteLLM config.
    """
    runtime_config = _get_litellm_runtime_config()
    token = _resolve_litellm_auth_token(runtime_config)
    reasoning_effort = runtime_config["reasoning_effort"]
    applied_reasoning_effort = None
    payload = {
        "model": model,
        "input": input_items,
    }

    if _supports_reasoning_effort(model):
        payload["reasoning"] = {"effort": reasoning_effort}
        applied_reasoning_effort = reasoning_effort
    else:
        logger.info(
            "Skipping reasoning config for responses API model=%s",
            model,
        )

    if previous_response_id:
        payload["previous_response_id"] = previous_response_id
    if store:
        payload["store"] = True

    accept_header = "application/json, text/event-stream"
    if applied_reasoning_effort is not None:
        logger.info(
            "Calling LiteLLM responses API: model=%s, stream=%s, has_previous_response_id=%s, reasoning_effort=%s",
            model,
            stream,
            bool(previous_response_id),
            applied_reasoning_effort,
        )
    else:
        logger.info(
            "Calling LiteLLM responses API: model=%s, stream=%s, has_previous_response_id=%s",
            model,
            stream,
            bool(previous_response_id),
        )

    try:
        response = _post_with_retry(
            f"{runtime_config['base_url']}{runtime_config['responses_path']}",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": accept_header,
            },
            json=payload,
            stream=stream,
            proxies=runtime_config["proxies"],
        )
        logger.info(
            "Received LiteLLM responses API response successfully. model=%s pr_id=%s",
            model,
            pr_id,
        )
    except requests.exceptions.RequestException:
        logger.exception("LiteLLM responses API request failed")
        raise

    if stream:
        return response

    try:
        parsed_response = _parse_non_stream_responses_api_response(response.text)
        parsed_response.setdefault(
            "output_text", _extract_response_output_text(parsed_response)
        )
        return parsed_response
    except ValueError as exc:
        logger.warning(
            "LiteLLM responses API returned unparsable response. status_code=%s content_type=%s body_preview=%s parse_error=%s",
            response.status_code,
            response.headers.get("Content-Type", "unknown"),
            _safe_response_preview(response.text),
            str(exc),
        )
        raise LiteLLMResponseParseError(
            "LiteLLM responses API returned unparsable response"
        ) from exc


def upload_file(file_path, purpose="fine-tune"):
    runtime_config = _get_litellm_runtime_config()
    token = _resolve_litellm_auth_token(runtime_config)
    logger.info(
        "Uploading file to LiteLLM: file_name=%s, purpose=%s",
        os.path.basename(file_path),
        purpose,
    )
    with open(file_path, "rb") as f:
        files = {"file": (os.path.basename(file_path), f)}
        try:
            response = _post_with_retry(
                f"{runtime_config['base_url']}{runtime_config['files_path']}",
                headers={"Authorization": f"Bearer {token}"},
                files=files,
                data={"purpose": purpose},
                proxies=runtime_config["proxies"],
            )
        except requests.exceptions.RequestException:
            logger.exception(
                "LiteLLM file upload failed. file_name=%s purpose=%s",
                os.path.basename(file_path),
                purpose,
            )
            raise

    return response.json()["id"]


def create_fine_tune_job(
    training_file_id, validation_file_id, model, method="dpo", suffix=""
):
    runtime_config = _get_litellm_runtime_config()
    token = _resolve_litellm_auth_token(runtime_config)
    payload = {
        "training_file": training_file_id,
        "validation_file": validation_file_id,
        "model": model,
        "method": method,
    }
    if suffix:
        payload["suffix"] = suffix

    logger.info(
        "Creating LiteLLM fine-tune job: model=%s, method=%s, training_file_id=%s, validation_file_id=%s",
        model,
        method,
        training_file_id,
        validation_file_id,
    )
    try:
        response = _post_with_retry(
            f"{runtime_config['base_url']}{runtime_config['fine_tuning_jobs_path']}",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json=payload,
            proxies=runtime_config["proxies"],
        )
    except requests.exceptions.RequestException:
        logger.exception(
            "LiteLLM fine-tune job creation failed. model=%s method=%s", model, method
        )
        raise

    return response.json()["id"]


def retrieve_fine_tune_job_status(job_id):
    runtime_config = _get_litellm_runtime_config()
    token = _resolve_litellm_auth_token(runtime_config)
    fine_tuning_jobs_path = runtime_config["fine_tuning_jobs_path"].rstrip("/")
    try:
        response = _get_with_retry(
            f"{runtime_config['base_url']}{fine_tuning_jobs_path}/{job_id}",
            headers={"Authorization": f"Bearer {token}"},
            proxies=runtime_config["proxies"],
        )
    except requests.exceptions.RequestException:
        logger.exception(
            "LiteLLM fine-tune job status retrieval failed. job_id=%s", job_id
        )
        raise

    return response.json()
