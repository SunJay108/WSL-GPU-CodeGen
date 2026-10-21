"""
Answer one question with moderation before, during and after generation.
Runs in a worker thread; `progress` reports stages and the moderated partial
answer, `should_stop` is polled while the model streams.
"""

import time
from typing import Callable, List

import filter
import llm

# Text is released only after Qwen3Guard-Stream has checked it, in batches of
# roughly this many characters.
FEED_CHARS = 48


def fix_markdown(text: str) -> str:
    """Close an unfinished code fence so partial output renders correctly."""
    if text.count("```") % 2 != 0:
        text += "\n```"
    return text


def blocked_result(verdict: filter.Verdict, seconds: float, partial: str = "") -> dict:
    status = "unavailable" if verdict.checker == "unavailable" else "blocked"
    return {
        "status": status,
        "markdown": verdict.message,
        "message": verdict.message,
        "seconds": seconds,
        "guard": {"stage": verdict.stage, "checker": verdict.checker,
                  "verdict": verdict.verdict, "reason": verdict.detail},
        "partial": partial,
    }


def answer_question(question: str, turns: List[tuple], progress: Callable,
                    should_stop: Callable[[], bool]) -> dict:
    started = time.time()
    elapsed = lambda: round(time.time() - started, 1)

    progress("stage", stage="checking", text="Checking your request…")
    verdict, monitor = filter.screen_prompt(question, turns[-1][1] if turns else "")
    if verdict.allowed:
        try:
            messages, max_tokens = llm.build_messages(turns, question)
        except llm.PromptTooLong as error:
            verdict = filter.Verdict(False, "prompt", "input", "TOO_LONG", str(error), f"📏 **{error}**")

    answer = ""
    try:
        if verdict.allowed:
            progress("stage", stage="writing", text="Writing the answer…")
            pending = ""
            for chunk in llm.stream_chat(messages, max_tokens, should_stop):
                pending += chunk
                if len(pending) < FEED_CHARS:
                    continue
                verdict = monitor.feed(pending)
                if not verdict.allowed:
                    break
                answer += pending
                pending = ""
                progress("delta", text=fix_markdown(answer))

            if verdict.allowed:
                verdict = monitor.feed(pending, final=True)
            if verdict.allowed:
                answer += pending
                progress("delta", text=fix_markdown(answer))
                progress("stage", stage="reviewing", text="Reviewing the answer…")
                verdict = monitor.finish(question, answer)
    finally:
        if monitor is not None:
            monitor.close()

    if not verdict.allowed:
        return blocked_result(verdict, elapsed(), answer)

    stopped = should_stop()
    text = fix_markdown(answer.strip()) or "_(no response)_"
    return {"status": "answered", "markdown": text, "seconds": elapsed(), "stopped": stopped}
