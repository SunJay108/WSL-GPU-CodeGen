import json
from typing import Callable, Iterator, List, Optional, Sequence

import requests

from config import (
    CHAT_SLOT,
    CLASSIFIER_SLOT,
    LLAMA_MODEL,
    LLAMA_URL,
    MAX_OUTPUT_TOKENS,
    MIN_OUTPUT_TOKENS,
    SLOT_CTX,
    SystemPrompt,
)


class PromptTooLong(Exception):
    pass


def estimate_tokens(text: str) -> int:
    # Code averages ~3 characters per token; err on the high side.
    return len(text) // 3 + 4


def build_messages(turns: Sequence[tuple], query: str):
    """
    Fit the system prompt, the current query and as much recent history as possible
    into one llama.cpp slot. Returns (messages, max_tokens).
    """
    used = estimate_tokens(SystemPrompt) + estimate_tokens(query)
    if used + MIN_OUTPUT_TOKENS > SLOT_CTX:
        raise PromptTooLong(
            f"Your message is too long (about {used} tokens). "
            f"Please keep it under about {SLOT_CTX - MIN_OUTPUT_TOKENS - estimate_tokens(SystemPrompt)} tokens."
        )

    history_budget = SLOT_CTX - MAX_OUTPUT_TOKENS - used
    kept: List[dict] = []
    for question, answer in reversed(turns):
        cost = estimate_tokens(question) + estimate_tokens(answer)
        if cost > history_budget:
            break
        kept[:0] = [{"role": "user", "content": question}, {"role": "assistant", "content": answer}]
        history_budget -= cost
        used += cost

    messages = [{"role": "system", "content": SystemPrompt}, *kept, {"role": "user", "content": query}]
    max_tokens = max(MIN_OUTPUT_TOKENS, min(MAX_OUTPUT_TOKENS, SLOT_CTX - used - 32))
    return messages, max_tokens


def stream_chat(messages: List[dict], max_tokens: int, should_stop: Callable[[], bool]) -> Iterator[str]:
    """Yield content chunks from llama.cpp's OpenAI-compatible SSE stream."""
    payload = {
        "model": LLAMA_MODEL,
        "messages": messages,
        "stream": True,
        "temperature": 0.2,
        "max_tokens": max_tokens,
        "id_slot": CHAT_SLOT,
        "cache_prompt": True,
    }

    with requests.post(
        f"{LLAMA_URL}/v1/chat/completions", json=payload, stream=True, timeout=(10, 600)
    ) as response:
        response.raise_for_status()

        for line in response.iter_lines(decode_unicode=True):
            if should_stop():
                return
            if not line or not line.startswith("data:"):
                continue

            event_data = line[5:].strip()
            if event_data == "[DONE]":
                return

            data = json.loads(event_data)
            if "error" in data:
                raise RuntimeError(data["error"].get("message", "llama.cpp error"))

            choices = data.get("choices", [])
            if choices:
                chunk = choices[0].get("delta", {}).get("content") or ""
                if chunk:
                    yield chunk


def classify(system: str, examples: Sequence[tuple], content: str, labels: Sequence[str]) -> Optional[str]:
    """
    Single-label classification with the coder model. Output is constrained to
    `labels` by a GBNF grammar. Returns None if the answer cannot be parsed.
    """
    messages = [{"role": "system", "content": system}]
    for example, label in examples:
        messages += [{"role": "user", "content": example}, {"role": "assistant", "content": label}]
    messages.append({"role": "user", "content": content})

    payload = {
        "model": LLAMA_MODEL,
        "messages": messages,
        "temperature": 0,
        "max_tokens": 8,
        "grammar": "root ::= " + " | ".join(f'"{label}"' for label in labels),
        "id_slot": CLASSIFIER_SLOT,
        "cache_prompt": True,
    }
    response = requests.post(f"{LLAMA_URL}/v1/chat/completions", json=payload, timeout=(10, 300))
    response.raise_for_status()

    answer = (response.json()["choices"][0]["message"].get("content") or "").strip()
    return answer if answer in labels else None
