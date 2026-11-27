import base64
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

from ..provider_base import (
    ImageAIProvider,
    ProviderCapabilities,
    ProviderError,
    ProviderRequest,
    ProviderResponse,
)


class DeepSeekProvider(ImageAIProvider):
    API_URL = "https://api.deepseek.com/chat/completions"
    MIME_TYPES = ("image/png", "image/jpeg", "image/webp", "image/gif")

    def __init__(
        self,
        model="deepseek-flash",
        timeout_seconds=90,
        max_image_bytes=25 * 1024 * 1024,
        schema_path="/opt/uniplex/config/image_analysis_contract_v1.json",
    ):
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.max_image_bytes = max_image_bytes
        self.schema_path = Path(schema_path)

    def capabilities(self):
        return ProviderCapabilities(
            provider_name="deepseek",
            supported_mime_types=self.MIME_TYPES,
            max_image_bytes=self.max_image_bytes,
            supports_structured_output=True,
        )

    def analyze(self, request: ProviderRequest) -> ProviderResponse:
        api_key = os.environ.get("DEEPSEEK_API_KEY")

        if not api_key:
            key_file = os.environ.get(
                "DEEPSEEK_API_KEY_FILE",
                "/run/secrets/deepseek.env",
            )
            try:
                for line in Path(key_file).read_text(
                    encoding="utf-8"
                ).splitlines():
                    if line.startswith("DEEPSEEK_API_KEY="):
                        api_key = line.split("=", 1)[1].strip()
                        break
            except OSError:
                pass

        if not api_key:
            raise ProviderError(
                "provider_not_configured",
                "DeepSeek API key is not configured",
                retryable=False,
            )

        path = Path(request.image_path)

        if request.mime_type not in self.MIME_TYPES:
            raise ProviderError(
                "unsupported_mime_type",
                "DeepSeek does not support this image format",
                retryable=False,
            )

        try:
            image_size = path.stat().st_size
        except OSError as exc:
            raise ProviderError(
                "image_not_found", "Image file is unavailable"
            ) from exc

        if image_size <= 0 or image_size > self.max_image_bytes:
            raise ProviderError(
                "image_too_large",
                "Image exceeds the configured size limit",
                retryable=False,
            )

        try:
            schema = json.loads(
                self.schema_path.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as exc:
            raise ProviderError(
                "output_schema_unavailable",
                "Output schema could not be loaded",
                retryable=False,
            ) from exc

        try:
            image_data = base64.b64encode(path.read_bytes()).decode("ascii")
        except OSError as exc:
            raise ProviderError(
                "image_read_failed", "Image could not be read"
            ) from exc

        data_url = (
            f"data:{request.mime_type};base64,{image_data}"
        )

        instructions = request.instructions
        system_prompt = (
            "Ты анализируешь изображения документов для компании UNIPLEX. "
            "Извлекай только фактически видимые сведения. "
            "Не угадывай нечитаемые значения. "
            "Каждый факт должен иметь источник: source_text либо координаты. "
            "Не считай собственную уверенность подтверждением факта. "
            "Новые факты не должны автоматически приниматься человеком. "
            "Верни JSON строго по переданной JSON Schema. "
            "Не добавляй Markdown или текст вне JSON."
        )

        user_text = (
            f"Задача: {instructions.get('extraction_scope', '')}\n"
            f"Контекст: {instructions.get('domain_context', '')}\n"
            f"Режим языка: {instructions.get('language_mode', 'auto')}\n"
            f"Контракт: {request.output_contract}\n"
            f"JSON Schema:\n{json.dumps(schema, ensure_ascii=False)}"
        )

        generation = request.generation or {}
        temperature = generation.get("temperature", 0)
        max_tokens = generation.get("max_output_tokens", 4096)

        if (
            isinstance(temperature, bool)
            or not isinstance(temperature, (int, float))
            or not 0 <= temperature <= 1
        ):
            raise ProviderError(
                "invalid_generation_config",
                "Invalid generation temperature",
                retryable=False,
            )

        if (
            type(max_tokens) is not int
            or not 1 <= max_tokens <= 8192
        ):
            raise ProviderError(
                "invalid_generation_config",
                "Invalid maximum output token count",
                retryable=False,
            )

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": user_text},
                        {
                            "type": "image_url",
                            "image_url": {"url": data_url},
                        },
                    ],
                },
            ],
            "response_format": {"type": "json_object"},
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        http_request = urllib.request.Request(
            self.API_URL,
            data=body,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        started = time.perf_counter()

        try:
            with urllib.request.urlopen(
                http_request, timeout=self.timeout_seconds
            ) as response:
                response_body = response.read(8 * 1024 * 1024 + 1)
                request_id = response.headers.get("x-request-id")

        except urllib.error.HTTPError as exc:
            retryable = exc.code in (408, 409, 429) or exc.code >= 500
            raise ProviderError(
                f"deepseek_http_{exc.code}",
                "DeepSeek API returned an HTTP error",
                retryable=retryable,
            ) from exc

        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ProviderError(
                "deepseek_connection_error",
                "Could not connect to DeepSeek API",
                retryable=True,
            ) from exc

        if len(response_body) > 8 * 1024 * 1024:
            raise ProviderError(
                "deepseek_response_too_large",
                "DeepSeek response exceeded the size limit",
                retryable=False,
            )

        try:
            api_result = json.loads(response_body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProviderError(
                "deepseek_invalid_http_json",
                "DeepSeek returned an invalid HTTP JSON response",
                retryable=False,
            ) from exc

        if not isinstance(api_result, dict):
            raise ProviderError(
                "deepseek_invalid_response_shape",
                "DeepSeek response must be a JSON object",
                retryable=False,
            )

        if "error" in api_result:
            raise ProviderError(
                "deepseek_api_error",
                "DeepSeek API returned an error payload",
                retryable=False,
            )

        choices = api_result.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ProviderError(
                "deepseek_missing_choices",
                "DeepSeek response has no choices",
                retryable=False,
            )

        choice = choices[0]
        if not isinstance(choice, dict):
            raise ProviderError(
                "deepseek_invalid_choice",
                "DeepSeek response choice has an invalid format",
                retryable=False,
            )

        message = choice.get("message")
        if not isinstance(message, dict):
            raise ProviderError(
                "deepseek_missing_message",
                "DeepSeek response has no valid message",
                retryable=False,
            )

        content = message.get("content")
        if not isinstance(content, str):
            raise ProviderError(
                "deepseek_invalid_content_type",
                "DeepSeek message content is not a string",
                retryable=False,
            )

        usage = api_result.get("usage")
        if not isinstance(usage, dict):
            usage = {}

        finish_reason = choice.get("finish_reason")
        if not isinstance(finish_reason, str):
            finish_reason = None
        else:
            finish_reason = "".join(
                ch for ch in finish_reason[:40]
                if ch.isalnum() or ch in "_-"
            )

        model_name = api_result.get("model")
        if not isinstance(model_name, str):
            model_name = None
        else:
            model_name = "".join(
                ch for ch in model_name[:80]
                if ch.isalnum() or ch in "._-"
            )

        completion_tokens = usage.get("completion_tokens")
        if type(completion_tokens) is not int or completion_tokens < 0:
            completion_tokens = None

        diagnostics = {
            "request_id": request_id[:128] if isinstance(request_id, str) else None,
            "model": model_name,
            "finish_reason": finish_reason,
            "completion_tokens": completion_tokens,
            "content_length": len(content),
            "markdown_fence": content.lstrip().startswith("```"),
        }

        if finish_reason == "length":
            raise ProviderError(
                "deepseek_output_truncated",
                "DeepSeek output reached the generation token limit",
                retryable=False,
                diagnostics=diagnostics,
            )

        candidate = content.strip()
        if candidate.startswith("```") and candidate.endswith("```"):
            lines = candidate.splitlines()
            if len(lines) >= 3:
                candidate = "\\n".join(lines[1:-1]).strip()

        try:
            output = json.loads(candidate)
        except json.JSONDecodeError as exc:
            raise ProviderError(
                "deepseek_invalid_content_json",
                "DeepSeek message content is not valid JSON",
                retryable=False,
                diagnostics=diagnostics,
            ) from exc

        if not isinstance(output, dict):
            raise ProviderError(
                "deepseek_invalid_output_shape",
                "DeepSeek output must be a JSON object",
                retryable=False,
            )

        usage = api_result.get("usage") or {}

        return ProviderResponse(
            provider_name="deepseek",
            model_name=api_result.get("model", self.model),
            model_version=api_result.get("model"),
            output=output,
            request_metadata={
                "request_id": request_id,
                "finish_reason": message.get("finish_reason"),
            },
            usage={
                "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
                "total_tokens": usage.get("total_tokens"),
            },
            latency_ms=int((time.perf_counter() - started) * 1000),
        )
