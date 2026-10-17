"""One function: ``complete(messages) -> str``.

Providers (pick with LOCALDASH_PROVIDER, model with LOCALDASH_MODEL):

  ollama      local, no key. Uses Ollama's native /api/chat so the context
              window can be raised (num_ctx); its OpenAI-compatible /v1
              endpoint silently truncates long prompts to the model default.
  openrouter  OPENROUTER_API_KEY, OpenAI-compatible chat API
  openai      OPENAI_API_KEY, OpenAI chat completions
  anthropic   ANTHROPIC_API_KEY, native Messages API

LOCALDASH_BASE_URL overrides the endpoint (e.g. LM Studio / vLLM with
provider=openai). If LOCALDASH_PROVIDER is unset, the first provider with a
key in the environment wins (anthropic, openrouter, openai), else ollama.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass

import httpx

PROVIDERS: dict[str, dict[str, str | None]] = {
    "ollama": {"base_url": "http://localhost:11434", "key_env": None, "model": "qwen2.5-coder:7b"},
    "openrouter": {"base_url": "https://openrouter.ai/api/v1", "key_env": "OPENROUTER_API_KEY", "model": "anthropic/claude-sonnet-5.5"},
    "openai": {"base_url": "https://api.openai.com/v1", "key_env": "OPENAI_API_KEY", "model": "gpt-5-mini"},
    "anthropic": {"base_url": "https://api.anthropic.com", "key_env": "ANTHROPIC_API_KEY", "model": "claude-sonnet-5-5"},
}
AUTO_ORDER = ("anthropic", "openrouter", "openai")

Message = dict[str, str]


class LLMError(RuntimeError):
    pass


@dataclass
class LLMConfig:
    provider: str
    model: str
    base_url: str
    api_key: str | None = None
    timeout: float = 600.0
    num_ctx: int = 16384

    @property
    def label(self) -> str:
        return f"{self.provider}/{self.model}"

    @classmethod
    def from_env(cls, provider: str | None = None, model: str | None = None) -> "LLMConfig":
        provider = (provider or os.environ.get("LOCALDASH_PROVIDER") or "").strip().lower()
        if not provider:
            provider = next((p for p in AUTO_ORDER if os.environ.get(PROVIDERS[p]["key_env"] or "")), "ollama")
        if provider not in PROVIDERS:
            raise LLMError(f"unknown provider {provider!r}; choose one of {', '.join(PROVIDERS)}")
        info = PROVIDERS[provider]
        key_env = info["key_env"]
        api_key = os.environ.get(key_env) if key_env else None
        if key_env and not api_key:
            raise LLMError(f"{provider} needs {key_env} in the environment")
        base_url = os.environ.get("LOCALDASH_BASE_URL") or (
            os.environ.get("OLLAMA_HOST") if provider == "ollama" and os.environ.get("OLLAMA_HOST") else info["base_url"]
        )
        if provider == "ollama" and not base_url.startswith("http"):
            base_url = "http://" + base_url
        return cls(
            provider=provider,
            model=model or os.environ.get("LOCALDASH_MODEL") or info["model"],
            base_url=base_url.rstrip("/"),
            api_key=api_key,
            timeout=float(os.environ.get("LOCALDASH_TIMEOUT", 600)),
            num_ctx=int(os.environ.get("LOCALDASH_NUM_CTX", 16384)),
        )


def complete(messages: list[Message], config: LLMConfig | None = None, *, json_mode: bool = True) -> str:
    """Send chat ``messages`` (system/user/assistant) and return the reply text."""
    config = config or LLMConfig.from_env()
    t0 = time.time()
    if config.provider == "ollama":
        reply = _ollama(messages, config, json_mode)
    elif config.provider == "anthropic":
        reply = _anthropic(messages, config)
    else:
        reply = _openai_compatible(messages, config, json_mode)
    _debug_log(config, messages, reply, time.time() - t0)
    return reply


def _debug_log(config: LLMConfig, messages: list[Message], reply: str, seconds: float) -> None:
    """LOCALDASH_DEBUG=1 (or a file path) appends every exchange to a JSONL file."""
    target = os.environ.get("LOCALDASH_DEBUG")
    if not target:
        return
    path = "localdash-debug.jsonl" if target in ("1", "true", "yes") else target
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps({"model": config.label, "seconds": round(seconds, 1), "messages": messages, "reply": reply}) + "\n")


def _post(config: LLMConfig, url: str, headers: dict, body: dict) -> dict:
    last: Exception | None = None
    for attempt in range(3):
        try:
            r = httpx.post(url, headers=headers, json=body, timeout=config.timeout)
        except httpx.ConnectError as e:
            hint = " Is Ollama running? Start it with `ollama serve`." if config.provider == "ollama" else ""
            raise LLMError(f"could not connect to {url}.{hint}") from e
        except httpx.TimeoutException as e:
            raise LLMError(f"{config.label} timed out after {config.timeout:.0f}s (set LOCALDASH_TIMEOUT)") from e
        if r.status_code in (429, 500, 502, 503, 504, 529) and attempt < 2:
            last = LLMError(f"{config.label}: HTTP {r.status_code}")
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code >= 400:
            detail = r.text[:500]
            if r.status_code == 404 and config.provider == "ollama":
                detail += f"\nPull the model first: ollama pull {config.model}"
            raise LLMError(f"{config.label}: HTTP {r.status_code}: {detail}")
        try:
            return r.json()
        except ValueError as e:
            raise LLMError(f"{config.label}: non-JSON response: {r.text[:200]}") from e
    raise last or LLMError("request failed")


def _ollama(messages: list[Message], config: LLMConfig, json_mode: bool) -> str:
    body = {
        "model": config.model,
        "messages": messages,
        "stream": False,
        "options": {"temperature": 0.1, "num_ctx": config.num_ctx},
    }
    if json_mode:
        body["format"] = "json"
    data = _post(config, f"{config.base_url}/api/chat", {}, body)
    return (data.get("message") or {}).get("content", "")


def _openai_compatible(messages: list[Message], config: LLMConfig, json_mode: bool) -> str:
    headers = {"Authorization": f"Bearer {config.api_key}"} if config.api_key else {}
    if config.provider == "openrouter":
        headers.update({"HTTP-Referer": "https://github.com/localdash", "X-Title": "localdash"})
    body: dict = {"model": config.model, "messages": messages}
    reasoning_model = config.provider == "openai" and config.model.startswith(("gpt-5", "o1", "o3", "o4"))
    if not reasoning_model:
        body["temperature"] = 0.1  # reasoning models only accept the default
    if json_mode and config.provider == "openai":
        body["response_format"] = {"type": "json_object"}
    data = _post(config, f"{config.base_url}/chat/completions", headers, body)
    try:
        return data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError) as e:
        raise LLMError(f"{config.label}: unexpected response: {str(data)[:300]}") from e


def _anthropic(messages: list[Message], config: LLMConfig) -> str:
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    chat = [{"role": m["role"], "content": m["content"]} for m in messages if m["role"] != "system"]
    body = {"model": config.model, "max_tokens": 8192, "temperature": 0.1, "messages": chat}
    if system:
        body["system"] = system
    headers = {"x-api-key": config.api_key or "", "anthropic-version": "2023-06-01"}
    data = _post(config, f"{config.base_url}/v1/messages", headers, body)
    return "".join(block.get("text", "") for block in data.get("content", []) if block.get("type") == "text")
