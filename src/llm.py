"""Shared LLM backend gateway — Claude CLI and Ollama."""
import asyncio
import os
import re

_CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5-20251001")
_OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3:8b")
_OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
_OLLAMA_CTX = int(os.environ.get("OLLAMA_CTX", "32768"))


async def run_llm(
    prompt: str,
    *,
    backend: str | None = None,
    model: str | None = None,
    max_output_tokens: int | None = None,
    timeout: float = 300.0,
) -> str:
    if backend is None:
        backend = os.environ.get("LLM_BACKEND") or os.environ.get("EXTRACTION_BACKEND", "ollama")
    backend = backend.lower()
    if backend == "ollama":
        return await _run_ollama_async(prompt, model=model, max_output_tokens=max_output_tokens, timeout=timeout)
    return await _run_claude_async(prompt, model=model, timeout=timeout)


async def _run_claude_async(prompt: str, *, model: str | None = None, timeout: float = 300.0) -> str:
    if model is None:
        model = _CLAUDE_MODEL
    proc = await asyncio.create_subprocess_exec(
        "claude", "-p", prompt, "--model", model,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    return stdout.decode().strip()


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
    options: dict = {"temperature": 0, "num_ctx": _OLLAMA_CTX}
    if max_output_tokens is not None:
        options["num_predict"] = max_output_tokens
    payload = {
        "model": model,
        "prompt": "/no_think\n" + prompt,
        "stream": True,
        "options": options,
    }
    chunks: list[str] = []
    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, read=timeout)) as client:
        async with client.stream("POST", f"{_OLLAMA_URL}/api/generate", json=payload) as r:
            r.raise_for_status()
            async for line in r.aiter_lines():
                if line:
                    try:
                        chunks.append(json.loads(line).get("response", ""))
                    except json.JSONDecodeError:
                        pass
    raw = "".join(chunks)
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
    return raw
