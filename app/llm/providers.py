from __future__ import annotations

import httpx

from app.config import Settings, get_settings
from app.llm.base import LLMError, LLMResult, build_prompt, system_prompt
from app.services.retrieval import best_sentences


class ExtractiveProvider:
    """No model: returns verbatim sentences from the best fragment. Always passes the guard."""

    name = "extractive"

    def generate(self, business: str, question: str, fragments: list[str], tone: str = "friendly") -> LLMResult:
        if not fragments:
            raise LLMError("no fragments")
        return LLMResult(best_sentences(fragments[0], question, limit=2), 0)


class YandexProvider:
    name = "yandex"
    URL = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"

    def __init__(self, s: Settings, client: httpx.Client | None = None) -> None:
        if not s.yandex_api_key or not s.yandex_folder_id:
            raise LLMError("YANDEX_API_KEY / YANDEX_FOLDER_ID are not configured")
        self.s = s
        self.client = client or httpx.Client(timeout=s.llm_timeout_seconds)

    def generate(self, business: str, question: str, fragments: list[str], tone: str = "friendly") -> LLMResult:
        body = {
            "modelUri": f"gpt://{self.s.yandex_folder_id}/{self.s.yandex_model}",
            "completionOptions": {"stream": False, "temperature": 0.1, "maxTokens": "400"},
            "messages": [
                {"role": "system", "text": system_prompt(business, tone)},
                {"role": "user", "text": build_prompt(question, fragments)},
            ],
        }
        headers = {
            "Authorization": f"Api-Key {self.s.yandex_api_key}",
            "x-folder-id": self.s.yandex_folder_id,
        }
        try:
            r = self.client.post(self.URL, json=body, headers=headers)
            r.raise_for_status()
            data = r.json()["result"]
            text = data["alternatives"][0]["message"]["text"]
            tokens = int(data.get("usage", {}).get("totalTokens", 0))
        except (httpx.HTTPError, KeyError, IndexError, ValueError) as e:
            raise LLMError(f"yandex: {type(e).__name__}") from e
        return LLMResult(text.strip(), tokens)


class OpenAICompatProvider:
    name = "openai"

    def __init__(self, s: Settings, client: httpx.Client | None = None) -> None:
        if not (s.openai_base_url and s.openai_api_key and s.openai_model):
            raise LLMError("OPENAI_BASE_URL / OPENAI_API_KEY / OPENAI_MODEL are not configured")
        self.s = s
        self.client = client or httpx.Client(timeout=s.llm_timeout_seconds)

    def generate(self, business: str, question: str, fragments: list[str], tone: str = "friendly") -> LLMResult:
        body = {
            "model": self.s.openai_model,
            "temperature": 0.1,
            "max_tokens": 400,
            "messages": [
                {"role": "system", "content": system_prompt(business, tone)},
                {"role": "user", "content": build_prompt(question, fragments)},
            ],
        }
        url = self.s.openai_base_url.rstrip("/") + "/chat/completions"
        try:
            r = self.client.post(url, json=body, headers={"Authorization": f"Bearer {self.s.openai_api_key}"})
            r.raise_for_status()
            data = r.json()
            text = data["choices"][0]["message"]["content"]
            tokens = int(data.get("usage", {}).get("total_tokens", 0))
        except (httpx.HTTPError, KeyError, IndexError, ValueError) as e:
            raise LLMError(f"openai: {type(e).__name__}") from e
        return LLMResult(text.strip(), tokens)


_override = None


def set_provider_override(p) -> None:
    """Tests inject a fake provider here."""
    global _override
    _override = p


def get_provider():
    if _override is not None:
        return _override
    s = get_settings()
    try:
        if s.llm_provider == "yandex":
            return YandexProvider(s)
        if s.llm_provider == "openai":
            return OpenAICompatProvider(s)
    except LLMError:
        return ExtractiveProvider()  # misconfigured -> safe fallback, logged by caller
    return ExtractiveProvider()
