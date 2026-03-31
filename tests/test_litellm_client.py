import unittest
import json
import tempfile
from unittest.mock import Mock, patch

import requests
from tenacity import wait_none  # type: ignore[reportMissingImports,reportMissingModuleSource]

import reflex_reviewer.litellm_client as litellm_client_module
from reflex_reviewer.litellm_client import (
    LiteLLMResponseParseError,
    responses,
    chat_completions,
    upload_file,
    create_fine_tune_job,
    retrieve_fine_tune_job_status,
)


def _mock_response(
    *,
    status_code=200,
    content_type="text/event-stream;charset=utf-8",
    text="",
):
    response = Mock()
    response.raise_for_status = Mock()
    response.status_code = status_code
    response.headers = {"Content-Type": content_type}
    response.text = text

    return response


class LiteLLMClientTests(unittest.TestCase):
    def setUp(self):
        self._litellm_config_patcher = patch(
            "reflex_reviewer.litellm_client.get_litellm_config",
            return_value={
                "base_url": "https://litellm.example.test",
                "api_key": None,
                "proxies": None,
                "reasoning_effort": "high",
                "unsupported_reasoning_models": {"gpt-4.1"},
            },
        )
        self._litellm_config_patcher.start()
        self._disable_retry_backoff()

    def tearDown(self):
        self._litellm_config_patcher.stop()

    def _disable_retry_backoff(self):
        for operation in (
            litellm_client_module._post_with_retry,
            litellm_client_module._get_with_retry,
        ):
            retrying = getattr(operation, "retry", None)
            if not retrying:
                continue
            retrying.wait = wait_none()
            retrying.sleep = lambda _: None

    @patch("reflex_reviewer.litellm_client.get_oauth2_token", return_value="token")
    @patch("reflex_reviewer.litellm_client.requests.post")
    def test_chat_completion_parses_single_sse_payload_with_message(
        self, mock_post, _mock_token
    ):
        payload = {
            "id": "chatcmpl-1",
            "object": "chat.completion",
            "created": 1774606584,
            "model": "oca/grok4-fast-reasoning",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": '{"sentiment":"ACCEPTED"}',
                    },
                    "finish_reason": "stop",
                }
            ],
        }
        mock_post.return_value = _mock_response(text=f"data: {json.dumps(payload)}\n\n")

        result = chat_completions(
            model="oca/grok4-fast-reasoning",
            messages=[{"role": "user", "content": "hello"}],
            stream=False,
        )

        self.assertIsInstance(result, dict)
        if not isinstance(result, dict):
            self.fail("Expected a JSON dict response")

        first_choice = result.get("choices", [{}])[0]
        first_message = (
            first_choice.get("message", {}) if isinstance(first_choice, dict) else {}
        )
        self.assertEqual(first_message.get("content"), '{"sentiment":"ACCEPTED"}')
        self.assertEqual(
            mock_post.call_args.kwargs["headers"]["Accept"], "text/event-stream"
        )
        mock_post.assert_called_once()

    @patch("reflex_reviewer.litellm_client.get_oauth2_token", return_value="token")
    @patch("reflex_reviewer.litellm_client.requests.post")
    def test_chat_completion_reconstructs_message_from_chunked_sse_payload(
        self, mock_post, _mock_token
    ):
        events = [
            {
                "id": "chatcmpl-2",
                "object": "chat.completion.chunk",
                "created": 1774606584,
                "model": "oca/grok4-fast-reasoning",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant"},
                        "finish_reason": None,
                    }
                ],
            },
            {
                "id": "chatcmpl-2",
                "object": "chat.completion.chunk",
                "created": 1774606584,
                "model": "oca/grok4-fast-reasoning",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": '{"sentiment":"'},
                        "finish_reason": None,
                    }
                ],
            },
            {
                "id": "chatcmpl-2",
                "object": "chat.completion.chunk",
                "created": 1774606584,
                "model": "oca/grok4-fast-reasoning",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": 'REJECTED"}'},
                        "finish_reason": "stop",
                    }
                ],
            },
        ]

        sse_body = "\n\n".join(f"data: {json.dumps(event)}" for event in events)
        sse_body = f"{sse_body}\n\n" + "data: [DONE]\n\n"

        mock_post.return_value = _mock_response(text=sse_body)

        result = chat_completions(
            model="oca/grok4-fast-reasoning",
            messages=[{"role": "user", "content": "hello"}],
            stream=False,
        )

        self.assertIsInstance(result, dict)
        if not isinstance(result, dict):
            self.fail("Expected reconstructed JSON dict response")

        first_choice = result.get("choices", [{}])[0]
        first_message = (
            first_choice.get("message", {}) if isinstance(first_choice, dict) else {}
        )
        self.assertEqual(first_message.get("content"), '{"sentiment":"REJECTED"}')

    @patch("reflex_reviewer.litellm_client.get_oauth2_token", return_value="token")
    @patch("reflex_reviewer.litellm_client.requests.post")
    def test_chat_completion_raises_parse_error_when_sse_payload_is_malformed(
        self, mock_post, _mock_token
    ):
        mock_post.return_value = _mock_response(
            text="data: not-a-json-event\n\n",
        )

        with self.assertRaises(LiteLLMResponseParseError) as context:
            chat_completions(
                model="oca/grok4-fast-reasoning",
                messages=[{"role": "user", "content": "hello"}],
                stream=False,
            )

        self.assertIn("unparsable event-stream", str(context.exception))

    @patch("reflex_reviewer.litellm_client.get_oauth2_token", return_value="token")
    @patch("reflex_reviewer.litellm_client.requests.post")
    def test_chat_completion_retries_on_transient_error_and_succeeds(
        self, mock_post, _mock_token
    ):
        payload = {
            "id": "chatcmpl-3",
            "object": "chat.completion",
            "created": 1774606585,
            "model": "oca/grok4-fast-reasoning",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": '{"sentiment":"ACCEPTED"}',
                    },
                    "finish_reason": "stop",
                }
            ],
        }

        mock_post.side_effect = [
            requests.exceptions.Timeout("temporary timeout"),
            _mock_response(text=f"data: {json.dumps(payload)}\n\n"),
        ]

        result = chat_completions(
            model="oca/grok4-fast-reasoning",
            messages=[{"role": "user", "content": "hello"}],
            stream=False,
        )

        self.assertEqual(mock_post.call_count, 2)
        self.assertIsInstance(result, dict)
        if not isinstance(result, dict):
            self.fail("Expected a JSON dict response")

        first_choice = result.get("choices", [{}])[0]
        first_message = (
            first_choice.get("message", {}) if isinstance(first_choice, dict) else {}
        )
        self.assertEqual(first_message.get("content"), '{"sentiment":"ACCEPTED"}')

    @patch("reflex_reviewer.litellm_client.get_oauth2_token", return_value="token")
    @patch("reflex_reviewer.litellm_client.requests.post")
    def test_create_response_parses_json_response_and_sets_defaults(
        self, mock_post, _mock_token
    ):
        mock_post.return_value = _mock_response(
            content_type="application/json",
            text=json.dumps(
                {
                    "id": "resp_123",
                    "object": "response",
                    "output": [
                        {
                            "type": "message",
                            "content": [{"type": "output_text", "text": "hello world"}],
                        }
                    ],
                }
            ),
        )

        result = responses(
            model="oca/grok4-fast-reasoning",
            input_items=[{"role": "user", "content": "hello"}],
            stream=False,
        )

        self.assertIsInstance(result, dict)
        if not isinstance(result, dict):
            self.fail("Expected a JSON dict response")

        self.assertEqual(result.get("id"), "resp_123")
        self.assertEqual(result.get("output_text"), "hello world")
        self.assertEqual(mock_post.call_args.args[0].endswith("/responses"), True)
        sent_payload = mock_post.call_args.kwargs["json"]
        self.assertEqual(sent_payload.get("reasoning", {}).get("effort"), "high")
        self.assertEqual(
            mock_post.call_args.kwargs["headers"]["Accept"],
            "application/json, text/event-stream",
        )

    @patch("reflex_reviewer.litellm_client.get_oauth2_token", return_value="token")
    @patch("reflex_reviewer.litellm_client.requests.post")
    def test_create_response_supports_previous_response_id(
        self, mock_post, _mock_token
    ):
        mock_post.return_value = _mock_response(
            content_type="application/json",
            text=json.dumps(
                {
                    "id": "resp_456",
                    "object": "response",
                    "output_text": "follow-up",
                }
            ),
        )

        responses(
            model="oca/grok4-fast-reasoning",
            input_items=[{"role": "user", "content": "follow-up"}],
            previous_response_id="resp_previous",
            stream=False,
        )

        sent_payload = mock_post.call_args.kwargs["json"]
        self.assertEqual(sent_payload.get("previous_response_id"), "resp_previous")
        self.assertNotIn("store", sent_payload)
        self.assertEqual(sent_payload.get("reasoning", {}).get("effort"), "high")

    @patch("reflex_reviewer.litellm_client.get_oauth2_token", return_value="token")
    @patch("reflex_reviewer.litellm_client.requests.post")
    def test_create_response_supports_store_flag(self, mock_post, _mock_token):
        mock_post.return_value = _mock_response(
            content_type="application/json",
            text=json.dumps(
                {
                    "id": "resp_789",
                    "object": "response",
                    "output_text": "initial",
                }
            ),
        )

        responses(
            model="oca/grok4-fast-reasoning",
            input_items=[{"role": "user", "content": "initial"}],
            store=True,
            stream=False,
        )

        sent_payload = mock_post.call_args.kwargs["json"]
        self.assertEqual(sent_payload.get("store"), True)

    @patch("reflex_reviewer.litellm_client.get_oauth2_token", return_value="token")
    @patch("reflex_reviewer.litellm_client.requests.post")
    def test_create_response_skips_reasoning_for_gpt_4_1_models(
        self, mock_post, _mock_token
    ):
        mock_post.return_value = _mock_response(
            content_type="application/json",
            text=json.dumps(
                {
                    "id": "resp_gpt41",
                    "object": "response",
                    "output_text": "ok",
                }
            ),
        )

        with self.assertLogs("reflex_reviewer.litellm_client", level="INFO") as logs:
            responses(
                model="oca/gpt-4.1",
                input_items=[{"role": "user", "content": "hello"}],
                stream=False,
            )

        sent_payload = mock_post.call_args.kwargs["json"]
        self.assertNotIn("reasoning", sent_payload)

        logs_text = "\n".join(logs.output)
        self.assertIn("Skipping reasoning config for responses API model=oca/gpt-4.1", logs_text)
        self.assertIn("Calling LiteLLM responses API: model=oca/gpt-4.1", logs_text)
        self.assertNotIn("reasoning_effort=", logs_text)

    @patch("reflex_reviewer.litellm_client.get_oauth2_token", return_value="token")
    @patch("reflex_reviewer.litellm_client.requests.post")
    def test_create_response_logs_reasoning_effort_when_applied(
        self, mock_post, _mock_token
    ):
        mock_post.return_value = _mock_response(
            content_type="application/json",
            text=json.dumps(
                {
                    "id": "resp_reasoning",
                    "object": "response",
                    "output_text": "ok",
                }
            ),
        )

        with self.assertLogs("reflex_reviewer.litellm_client", level="INFO") as logs:
            responses(
                model="oca/grok4-fast-reasoning",
                input_items=[{"role": "user", "content": "hello"}],
                stream=False,
            )

        logs_text = "\n".join(logs.output)
        self.assertIn("Calling LiteLLM responses API: model=oca/grok4-fast-reasoning", logs_text)
        self.assertIn("reasoning_effort=high", logs_text)

    @patch("reflex_reviewer.litellm_client.get_oauth2_token", return_value="token")
    @patch(
        "reflex_reviewer.litellm_client.get_litellm_config",
        return_value={
            "base_url": "https://litellm.example.test",
            "api_key": None,
            "proxies": None,
            "reasoning_effort": "high",
            "unsupported_reasoning_models": {"custom-reasoning-model"},
        },
    )
    @patch("reflex_reviewer.litellm_client.requests.post")
    def test_create_response_skips_reasoning_for_configured_unsupported_models(
        self, mock_post, _mock_config, _mock_token
    ):
        mock_post.return_value = _mock_response(
            content_type="application/json",
            text=json.dumps(
                {
                    "id": "resp_custom_unsupported",
                    "object": "response",
                    "output_text": "ok",
                }
            ),
        )

        responses(
            model="oca/custom-reasoning-model-v2",
            input_items=[{"role": "user", "content": "hello"}],
            stream=False,
        )

        sent_payload = mock_post.call_args.kwargs["json"]
        self.assertNotIn("reasoning", sent_payload)

    @patch("reflex_reviewer.litellm_client.get_oauth2_token", return_value="token")
    @patch("reflex_reviewer.litellm_client.requests.post")
    def test_create_response_parses_sse_response_payload(self, mock_post, _mock_token):
        sse_event = {
            "response": {
                "id": "resp_sse_1",
                "object": "response",
                "output_text": "sse output",
            }
        }
        mock_post.return_value = _mock_response(
            text=f"data: {json.dumps(sse_event)}\n\n"
        )

        result = responses(
            model="oca/grok4-fast-reasoning",
            input_items=[{"role": "user", "content": "hello"}],
            stream=False,
        )

        self.assertIsInstance(result, dict)
        if not isinstance(result, dict):
            self.fail("Expected a JSON dict response")

        self.assertEqual(result.get("id"), "resp_sse_1")
        self.assertEqual(result.get("output_text"), "sse output")

    @patch("reflex_reviewer.litellm_client.get_oauth2_token", return_value="token")
    @patch("reflex_reviewer.litellm_client.requests.post")
    def test_create_response_raises_parse_error_when_payload_is_malformed(
        self, mock_post, _mock_token
    ):
        mock_post.return_value = _mock_response(
            text="data: not-a-json-event\n\n",
        )

        with self.assertRaises(LiteLLMResponseParseError) as context:
            responses(
                model="oca/grok4-fast-reasoning",
                input_items=[{"role": "user", "content": "hello"}],
                stream=False,
            )

        self.assertIn("unparsable response", str(context.exception))

    @patch("reflex_reviewer.litellm_client.get_oauth2_token", return_value="token")
    @patch("reflex_reviewer.litellm_client.requests.post")
    def test_create_response_retries_on_transient_error_and_succeeds(
        self, mock_post, _mock_token
    ):
        mock_post.side_effect = [
            requests.exceptions.ConnectionError("temporary network issue"),
            _mock_response(
                content_type="application/json",
                text=json.dumps(
                    {
                        "id": "resp_retry_1",
                        "object": "response",
                        "output_text": "retry-success",
                    }
                ),
            ),
        ]

        result = responses(
            model="oca/grok4-fast-reasoning",
            input_items=[{"role": "user", "content": "hello"}],
            stream=False,
        )

        self.assertEqual(mock_post.call_count, 2)
        self.assertIsInstance(result, dict)
        if not isinstance(result, dict):
            self.fail("Expected a JSON dict response")

        self.assertEqual(result.get("id"), "resp_retry_1")
        self.assertEqual(result.get("output_text"), "retry-success")

    @patch("reflex_reviewer.litellm_client.get_oauth2_token", return_value="token")
    @patch("reflex_reviewer.litellm_client.requests.post")
    def test_create_response_does_not_retry_on_http_400(self, mock_post, _mock_token):
        bad_request = _mock_response(
            status_code=400,
            content_type="application/json",
            text='{"error":"invalid_request_error"}',
        )
        bad_request.raise_for_status.side_effect = requests.exceptions.HTTPError(
            "400 Client Error",
            response=bad_request,
        )
        mock_post.return_value = bad_request

        with self.assertRaises(requests.exceptions.HTTPError):
            responses(
                model="oca/gpt-4.1",
                input_items=[{"role": "user", "content": "hello"}],
                stream=False,
            )

        self.assertEqual(mock_post.call_count, 1)

    @patch(
        "reflex_reviewer.litellm_client.get_oauth2_token", return_value="oauth-token"
    )
    @patch(
        "reflex_reviewer.litellm_client.get_litellm_config",
        return_value={
            "base_url": "https://litellm.example.test",
            "api_key": None,
            "proxies": None,
            "reasoning_effort": "high",
        },
    )
    @patch("reflex_reviewer.litellm_client.requests.post")
    def test_chat_completion_falls_back_to_oauth2_when_api_key_missing(
        self,
        mock_post,
        _mock_config,
        mock_get_oauth2_token,
    ):
        mock_post.return_value = _mock_response()

        chat_completions(
            model="oca/grok4-fast-reasoning",
            messages=[{"role": "user", "content": "hello"}],
            stream=True,
        )

        self.assertEqual(
            mock_post.call_args.kwargs["headers"]["Authorization"],
            "Bearer oauth-token",
        )
        mock_get_oauth2_token.assert_called_once()

    @patch(
        "reflex_reviewer.litellm_client.get_oauth2_token",
        side_effect=AssertionError(
            "oauth2 token should not be used when api key exists"
        ),
    )
    @patch(
        "reflex_reviewer.litellm_client.get_litellm_config",
        return_value={
            "base_url": "https://litellm.example.test",
            "api_key": "cli-api-key",
            "proxies": None,
            "reasoning_effort": "high",
        },
    )
    @patch("reflex_reviewer.litellm_client.requests.post")
    def test_create_response_prefers_api_key_over_oauth2(
        self,
        mock_post,
        _mock_config,
        _mock_oauth2,
    ):
        mock_post.return_value = _mock_response()

        responses(
            model="oca/grok4-fast-reasoning",
            input_items=[{"role": "user", "content": "hello"}],
            stream=True,
        )

        self.assertEqual(
            mock_post.call_args.kwargs["headers"]["Authorization"],
            "Bearer cli-api-key",
        )

    @patch(
        "reflex_reviewer.litellm_client.get_oauth2_token",
        side_effect=AssertionError(
            "oauth2 token should not be used when api key exists"
        ),
    )
    @patch(
        "reflex_reviewer.litellm_client.get_litellm_config",
        return_value={
            "base_url": "https://litellm.example.test",
            "api_key": "cli-api-key",
            "proxies": None,
            "reasoning_effort": "high",
            "files_path": "/files",
            "fine_tuning_jobs_path": "/fine_tuning/jobs",
        },
    )
    @patch("reflex_reviewer.litellm_client._get_with_retry")
    @patch("reflex_reviewer.litellm_client._post_with_retry")
    def test_file_and_fine_tune_apis_use_api_key_when_provided(
        self,
        mock_post_with_retry,
        mock_get_with_retry,
        _mock_config,
        _mock_oauth2,
    ):
        post_response = Mock()
        post_response.json.side_effect = [
            {"id": "file_1"},
            {"id": "job_1"},
        ]
        mock_post_with_retry.return_value = post_response

        get_response = Mock()
        get_response.json.return_value = {"id": "job_1", "status": "running"}
        mock_get_with_retry.return_value = get_response

        with tempfile.NamedTemporaryFile(mode="w", delete=True) as temp_file:
            temp_file.write("sample")
            temp_file.flush()
            file_id = upload_file(temp_file.name)

        job_id = create_fine_tune_job(
            training_file_id="file_1",
            validation_file_id="file_2",
            model="oca/model",
        )
        status_payload = retrieve_fine_tune_job_status("job_1")

        self.assertEqual(file_id, "file_1")
        self.assertEqual(job_id, "job_1")
        self.assertEqual(status_payload.get("status"), "running")

        self.assertEqual(
            mock_post_with_retry.call_args_list[0].kwargs["headers"]["Authorization"],
            "Bearer cli-api-key",
        )
        self.assertEqual(
            mock_post_with_retry.call_args_list[1].kwargs["headers"]["Authorization"],
            "Bearer cli-api-key",
        )
        self.assertEqual(
            mock_get_with_retry.call_args.kwargs["headers"]["Authorization"],
            "Bearer cli-api-key",
        )


if __name__ == "__main__":
    unittest.main()
