import json
import logging
import re

logger = logging.getLogger(__name__)


def extract_content_from_non_stream_response(
    response,
    content_error_message="Extracted content is not a string",
):
    """Extract message content from non-streaming chat-completions or responses API."""
    if not isinstance(response, dict):
        raise ValueError("Non-stream response must be a dict")

    output_text = response.get("output_text")
    if isinstance(output_text, str) and output_text:
        return output_text

    output = response.get("output")
    if isinstance(output, list):
        output_text_parts = []
        for output_item in output:
            if not isinstance(output_item, dict):
                continue

            content_items = output_item.get("content")
            if not isinstance(content_items, list):
                continue

            for content_item in content_items:
                if not isinstance(content_item, dict):
                    continue

                text = content_item.get("text")
                if isinstance(text, str) and text:
                    output_text_parts.append(text)

        if output_text_parts:
            return "".join(output_text_parts)

    content = ""
    choices = response.get("choices")
    if isinstance(choices, list) and choices:
        first_choice = choices[0]
        if isinstance(first_choice, dict):
            message = first_choice.get("message")
            if isinstance(message, dict):
                content = message.get("content", "")
                if isinstance(content, list):
                    content = "".join(
                        part.get("text", "")
                        for part in content
                        if isinstance(part, dict) and isinstance(part.get("text"), str)
                    )
            elif "text" in first_choice:
                content = first_choice.get("text", "")

    if not content:
        content = response.get("content", "")

    if not isinstance(content, str):
        raise ValueError(content_error_message)

    return content


def extract_json_from_content(
    content,
    empty_content_error_message="Model response content is empty",
    invalid_json_error_message="Unable to extract valid JSON from response content",
):
    """Extract and parse JSON from model response text (with optional markdown wrapper)."""
    if not content:
        raise ValueError(empty_content_error_message)

    normalized_content = re.sub(r"^```(?:json)?\s*\n?", "", content, flags=re.MULTILINE)
    normalized_content = re.sub(
        r"\n?```$\s*", "", normalized_content, flags=re.MULTILINE
    ).strip()

    try:
        return json.loads(normalized_content)
    except json.JSONDecodeError:
        json_match = re.search(r"\{.*\}", normalized_content, re.DOTALL)
        if json_match:
            try:
                return json.loads(json_match.group(0))
            except json.JSONDecodeError:
                pass

        raise ValueError(invalid_json_error_message)


def extract_content_from_stream_response(response):
    """Extract message content from streaming chat-completions or responses API."""
    content_chunks = []
    final_response_text = ""

    for raw_line in response.iter_lines(decode_unicode=True):
        if not raw_line:
            continue

        line = raw_line.strip()
        if not line.startswith("data:"):
            continue

        data = line[len("data:") :].strip()
        if data == "[DONE]":
            break

        try:
            event = json.loads(data)
        except json.JSONDecodeError:
            logger.warning("Skipping malformed stream event")
            continue

        response_payload = event.get("response")
        if isinstance(response_payload, dict):
            response_text = extract_content_from_non_stream_response(response_payload)
            if response_text:
                final_response_text = response_text
            continue

        if event.get("object") == "response":
            response_text = extract_content_from_non_stream_response(event)
            if response_text:
                final_response_text = response_text
            continue

        delta = event.get("choices", [{}])[0].get("delta", {})
        chunk = delta.get("content", "")
        if isinstance(chunk, list):
            chunk = "".join(
                part.get("text", "")
                for part in chunk
                if isinstance(part, dict) and isinstance(part.get("text"), str)
            )
        if chunk:
            content_chunks.append(chunk)

        event_type = str(event.get("type", ""))
        if "output_text" in event_type:
            output_chunk = event.get("delta", "")
            if isinstance(output_chunk, str) and output_chunk:
                content_chunks.append(output_chunk)

    if final_response_text:
        return final_response_text

    return "".join(content_chunks)


def parse_review_payload(response):
    """Extract and parse review JSON payload from model response."""
    if isinstance(response, dict):
        raw_content = extract_content_from_non_stream_response(response)
    else:
        raw_content = extract_content_from_stream_response(response)

    if not raw_content:
        raise ValueError("Model response content is empty")

    try:
        review_data = extract_json_from_content(raw_content)
        if not isinstance(review_data, dict):
            raise ValueError("Review payload must be a JSON object")
        return review_data
    except (json.JSONDecodeError, ValueError) as exc:
        logger.error("Failed to parse review response JSON payload")
        raise ValueError(f"Invalid JSON in model response: {exc}")


def parse_batched_sentiment_response(
    response,
    normalize_comment_id,
    valid_sentiments,
):
    """Parse and validate batched sentiment response into comment_id->sentiment mapping."""
    content = extract_content_from_non_stream_response(
        response,
        content_error_message="Extracted sentiment content is not a string",
    )
    payload = extract_json_from_content(
        content,
        empty_content_error_message="Model response content is empty",
        invalid_json_error_message="Unable to extract JSON object from model response",
    )
    if not isinstance(payload, dict):
        raise ValueError("Batched sentiment payload must be a JSON object")

    results = payload.get("results")
    if not isinstance(results, list):
        raise ValueError("Batched sentiment payload must contain a results list")

    sentiment_by_comment_id = {}
    for item in results:
        if not isinstance(item, dict):
            continue

        comment_id = normalize_comment_id(item.get("comment_id") or item.get("id"))
        sentiment = str(item.get("sentiment", "")).strip().upper()
        if comment_id and sentiment in valid_sentiments:
            sentiment_by_comment_id[comment_id] = sentiment

    return sentiment_by_comment_id