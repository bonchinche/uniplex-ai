import json
import urllib.error

import pytest

from image_ai.provider_base import ProviderRequest, ProviderError
from image_ai.providers.deepseek_provider import DeepSeekProvider


def make_request(tmp_path):
    image = tmp_path / "sample.png"
    image.write_bytes(b"fake-png-data")

    return ProviderRequest(
        request_id="test-request-1",
        image_path=image,
        mime_type="image/png",
        source_metadata={"sha256": "a" * 64},
        instructions={
            "language_mode": "ru",
            "extraction_scope": "Опиши изображение",
            "domain_context": "Тест UNIPLEX",
        },
        output_contract={"name": "test", "version": "1.0"},
        generation={"temperature": 0, "max_output_tokens": 100},
    )


def fake_response(payload):
    class Response:
        headers = {"x-request-id": "fake-request-id"}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, limit):
            return json.dumps(payload).encode("utf-8")

    return Response()


def test_successful_analysis_uses_json_and_image(monkeypatch, tmp_path):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key-not-real")

    api_result = {
        "model": "deepseek-flash",
        "choices": [{
            "message": {
                "content": json.dumps({"contract_version": "1.0"}),
                "finish_reason": "stop",
            }
        }],
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "total_tokens": 120,
        },
    }

    captured = {}

    def fake_urlopen(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return fake_response(api_result)

    monkeypatch.setattr(
        "image_ai.providers.deepseek_provider.urllib.request.urlopen",
        fake_urlopen,
    )

    response = DeepSeekProvider().analyze(make_request(tmp_path))

    body = json.loads(captured["request"].data)
    assert captured["request"].full_url.endswith("/chat/completions")
    assert captured["request"].get_header("Authorization") == "Bearer test-key-not-real"
    assert captured["timeout"] == 90
    assert body["response_format"] == {"type": "json_object"}
    assert body["messages"][1]["content"][1]["type"] == "image_url"
    assert body["messages"][1]["content"][1]["image_url"]["url"].startswith(
        "data:image/png;base64,"
    )
    assert response.provider_name == "deepseek"
    assert response.output == {"contract_version": "1.0"}
    assert response.usage["total_tokens"] == 120
    assert response.request_metadata["request_id"] == "fake-request-id"


def test_missing_api_key_does_not_send_request(monkeypatch, tmp_path):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY_FILE", str(tmp_path / "missing-secret"))

    def unexpected_request(*args, **kwargs):
        pytest.fail("HTTP request must not be sent")

    monkeypatch.setattr(
        "image_ai.providers.deepseek_provider.urllib.request.urlopen",
        unexpected_request,
    )

    with pytest.raises(ProviderError) as error:
        DeepSeekProvider().analyze(make_request(tmp_path))

    assert error.value.code == "provider_not_configured"
    assert error.value.retryable is False


def test_http_rate_limit_is_retryable(monkeypatch, tmp_path):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key-not-real")

    def rate_limited(*args, **kwargs):
        raise urllib.error.HTTPError(
            "https://api.deepseek.com/chat/completions",
            429,
            "Rate limited",
            {},
            None,
        )

    monkeypatch.setattr(
        "image_ai.providers.deepseek_provider.urllib.request.urlopen",
        rate_limited,
    )

    with pytest.raises(ProviderError) as error:
        DeepSeekProvider().analyze(make_request(tmp_path))

    assert error.value.code == "deepseek_http_429"
    assert error.value.retryable is True


def test_invalid_api_json_is_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key-not-real")

    monkeypatch.setattr(
        "image_ai.providers.deepseek_provider.urllib.request.urlopen",
        lambda *args, **kwargs: fake_response({"unexpected": "response"}),
    )

    with pytest.raises(ProviderError) as error:
        DeepSeekProvider().analyze(make_request(tmp_path))

    assert error.value.code == "deepseek_missing_choices"
    assert error.value.retryable is False


@pytest.mark.parametrize(
    ("payload", "expected_code"),
    [
        ({"error": {"message": "private response text"}}, "deepseek_api_error"),
        (
            {"choices": [{"message": {"content": {"text": "not a string"}}}]},
            "deepseek_invalid_content_type",
        ),
        (
            {"choices": [{"message": {"content": "{broken json"}}]},
            "deepseek_invalid_content_json",
        ),
        (
            {"choices": [{"message": {"content": "[]"}}]},
            "deepseek_invalid_output_shape",
        ),
    ],
)
def test_malformed_deepseek_responses_are_classified(
    monkeypatch, tmp_path, payload, expected_code
):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key-not-real")
    monkeypatch.setattr(
        "image_ai.providers.deepseek_provider.urllib.request.urlopen",
        lambda *args, **kwargs: fake_response(payload),
    )

    with pytest.raises(ProviderError) as error:
        DeepSeekProvider().analyze(make_request(tmp_path))

    assert error.value.code == expected_code
    assert error.value.retryable is False
    assert "private response text" not in str(error.value)
