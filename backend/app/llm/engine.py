"""LLM engine abstraction over an OpenAI-compatible local server.

Reuses the existing local install — no model downloads:
  * Ollama        → http://localhost:11434/v1   (default)
  * llama-server  → http://localhost:8080/v1    (C:\\llama.cpp\\llama-server.exe)

The OpenAI Python client speaks the same protocol to both, so swapping providers
or models is just a config change.
"""
from __future__ import annotations

from dataclasses import dataclass

import httpx
from openai import OpenAI

from app.config import settings


@dataclass
class ChatMessage:
    role: str
    content: str


class LLMEngine:
    """Thin wrapper around an OpenAI-compatible chat completions endpoint."""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
    ) -> None:
        self.base_url = base_url or settings.llm_base_url
        self.api_key = api_key or settings.llm_api_key
        self.model = model or settings.llm_model
        self._client = OpenAI(base_url=self.base_url, api_key=self.api_key)

    def generate(
        self,
        messages: list[ChatMessage],
        *,
        model: str | None = None,
        temperature: float | None = None,
        json_mode: bool = False,
    ) -> str:
        """Run a chat completion and return the assistant text content."""
        kwargs: dict = {
            "model": model or self.model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": (
                settings.llm_temperature if temperature is None else temperature
            ),
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        completion = self._client.chat.completions.create(**kwargs)
        return completion.choices[0].message.content or ""

    def list_models(self) -> list[str]:
        """List models available on the local server (provider-native)."""
        if settings.llm_provider == "ollama":
            url = f"{settings.ollama_native_url}/api/tags"
            resp = httpx.get(url, timeout=5.0)
            resp.raise_for_status()
            return [m["name"] for m in resp.json().get("models", [])]
        # OpenAI-compatible /models endpoint (works for llama-server too)
        return [m.id for m in self._client.models.list().data]

    def health(self) -> dict:
        """Return a small dict describing engine reachability."""
        try:
            models = self.list_models()
            return {
                "ok": True,
                "provider": settings.llm_provider,
                "base_url": self.base_url,
                "default_model": self.model,
                "available_models": models,
            }
        except Exception as exc:  # noqa: BLE001 — surfaced to the health endpoint
            return {
                "ok": False,
                "provider": settings.llm_provider,
                "base_url": self.base_url,
                "error": str(exc),
            }


# Module-level singleton for convenience.
engine = LLMEngine()
