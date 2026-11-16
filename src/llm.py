"""Shared LLM backend gateway — Claude CLI, Claude API (anthropic), Ollama, and DeepSeek API."""
import asyncio
import os
import re

_CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5-20251001")
_OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3:8b")
_OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
_OLLAMA_CTX = int(os.environ.get("OLLAMA_CTX", "8192"))  # 32768 thrashed/hung the runner; 8192 fits batch=1 RU pages (calibrated 2026-06-20)
_DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
_DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
_DEEPSEEK_URL = os.environ.get("DEEPSEEK_URL", "https://api.deepseek.com")
_ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5-5")


def current_model_label(backend: str | None = None) -> str:
    """Return short label ('haiku', 'qwen', or 'deepseek') for the active LLM backend."""
    if backend is None:
        backend = os.environ.get("LLM_BACKEND") or os.environ.get("EXTRACTION_BACKEND", "ollama")
    backend = backend.lower()
    if backend == "claude":
        return "haiku"
    if backend == "deepseek":
        return "deepseek"
    if backend in ("anthropic", "claude-api"):
        return "claude-api"
    return "qwen"


class LLMConfigError(RuntimeError):
    """Misconfiguration (missing API key / CLI) — retrying cannot help."""


async def run_llm(
    prompt: str,
    *,
    backend: str | None = None,
    model: str | None = None,
    max_output_tokens: int | None = None,
    timeout: float = 300.0,
    retries: int = 5,
) -> str:
    if backend is None:
        backend = os.environ.get("LLM_BACKEND") or os.environ.get("EXTRACTION_BACKEND", "ollama")
    backend = backend.lower()
    last_exc: Exception | None = None
    for attempt in range(max(1, retries)):
        try:
            if backend == "ollama":
                return await _run_ollama_async(prompt, model=model, max_output_tokens=max_output_tokens, timeout=timeout)
            if backend == "deepseek":
                return await _run_deepseek_async(prompt, model=model, max_output_tokens=max_output_tokens, timeout=timeout)
            if backend in ("anthropic", "claude-api"):
                return await _run_anthropic_async(prompt, model=model, max_output_tokens=max_output_tokens, timeout=timeout)
            return await _run_claude_async(prompt, model=model, timeout=timeout)
        except (LLMConfigError, FileNotFoundError):
            raise
        except Exception as e:
            last_exc = e
            if attempt < retries - 1:
                # exponential backoff — lets transient rate limits recover
                await asyncio.sleep(min(60.0, 4.0 * (2 ** attempt)))
    raise last_exc  # type: ignore[misc]


async def _run_claude_async(prompt: str, *, model: str | None = None, timeout: float = 300.0) -> str:
    if model is None:
        model = _CLAUDE_MODEL
    proc = await asyncio.create_subprocess_exec(
        "claude", "-p", prompt, "--model", model,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,  # capture, don't swallow — silent rate-limit empties were zeroing whole batches
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        # also on cancel (outer wait_for): don't leak a running claude subprocess
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        try:
            await asyncio.shield(proc.wait())
        except BaseException:
            pass
        raise
    out = stdout.decode().strip()
    if proc.returncode != 0:
        raise RuntimeError(f"claude CLI exit {proc.returncode}: {stderr.decode()[:300].strip()}")
    if not out:
        raise RuntimeError(f"claude CLI returned empty output (stderr: {stderr.decode()[:300].strip()})")
    return out


USAGE = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "cache_hit_tokens": 0}   # deepseek, per process


async def _run_deepseek_async(
    prompt: str,
    *,
    model: str | None = None,
    max_output_tokens: int | None = None,
    timeout: float = 300.0,
) -> str:
    import httpx
    import json
    if not _DEEPSEEK_API_KEY:
        raise LLMConfigError("DEEPSEEK_API_KEY not set")
    if model is None:
        model = _DEEPSEEK_MODEL
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": max_output_tokens or 8192,
        "stream": False,
    }
    headers = {
        "Authorization": f"Bearer {_DEEPSEEK_API_KEY}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, read=timeout)) as client:
        r = await client.post(f"{_DEEPSEEK_URL}/v1/chat/completions", json=payload, headers=headers)
        r.raise_for_status()
        data = r.json()
    u = data.get("usage") or {}
    USAGE["calls"] += 1
    USAGE["prompt_tokens"] += u.get("prompt_tokens", 0)
    USAGE["completion_tokens"] += u.get("completion_tokens", 0)
    USAGE["cache_hit_tokens"] += u.get("prompt_cache_hit_tokens", 0)
    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    if not content:
        raise RuntimeError(f"DeepSeek returned empty content (finish_reason={data.get('choices', [{}])[0].get('finish_reason')})")
    return content.strip()


async def _run_anthropic_async(
    prompt: str,
    *,
    model: str | None = None,
    max_output_tokens: int | None = None,
    timeout: float = 300.0,
) -> str:
    """Claude via the Anthropic Messages API (needs ANTHROPIC_API_KEY)."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise LLMConfigError("ANTHROPIC_API_KEY not set")
    import anthropic
    client = anthropic.AsyncAnthropic(timeout=timeout, max_retries=0)  # run_llm owns retries
    msg = await client.messages.create(
        model=model or _ANTHROPIC_MODEL,
        max_tokens=max_output_tokens or 4096,
        messages=[{"role": "user", "content": prompt}],
    )
    out = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()
    if not out:
        raise RuntimeError(f"Claude API returned empty content (stop_reason={msg.stop_reason})")
    return out


async def _run_ollama_async(
    prompt: str,
    *,
    model: str | None = None,
    max_output_tokens: int | None = None,
    timeout: float = 300.0,
) -> str:
    import httpx
    import json
    if model is None:
        model = _OLLAMA_MODEL
    # qwen3 "thinks" by default, and the ollama server's chatml template ignores
    # the "/no_think" prompt hack — so the model burned thousands of reasoning
    # tokens before answering (slow) and, once num_predict was capped, truncated
    # mid-think → empty output. /api/chat with think=False disables reasoning at
    # the API level: output is the JSON answer directly (faster on light venues,
    # no mid-think hangs/empties). num_ctx must hold prompt (≤~4.8k tok) + the full
    # JSON (dense venues ~4-5k tok) without overflowing → 12288. num_predict is a
    # ceiling only (think=False means generation stops at the JSON's EOS).
    options: dict = {
        "temperature": 0,
        "num_ctx": max(_OLLAMA_CTX, int(os.environ.get("OLLAMA_CTX_EXTRACT", "12288"))),
        "num_predict": max_output_tokens or int(os.environ.get("OLLAMA_NUM_PREDICT", "6144")),
    }
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "think": False,                       # disable qwen3 reasoning — the real fix
        "stream": True,
        # keep the ~9.8GB model resident between venues (avoid 15-30s reloads).
        "keep_alive": os.environ.get("OLLAMA_KEEP_ALIVE", "30m"),
        "options": options,
    }
    chunks: list[str] = []
    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, read=timeout)) as client:
        async with client.stream("POST", f"{_OLLAMA_URL}/api/chat", json=payload) as r:
            r.raise_for_status()
            async for line in r.aiter_lines():
                if line:
                    try:
                        chunks.append(json.loads(line).get("message", {}).get("content", ""))
                    except json.JSONDecodeError:
                        pass
    raw = "".join(chunks)
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
    return raw
