"""
Qwen3Guard moderation service.

- Qwen3Guard-Gen-0.6B    : whole prompt / whole response classification.
- Qwen3Guard-Stream-0.6B : token-level moderation of a response while it streams.

Both models are loaded from local folders (no downloads). The UI talks to this
service over the internal Docker network only.
"""

import copy
import logging
import os
import re
import threading
import time
import uuid
from contextlib import asynccontextmanager
from typing import List, Literal, Optional

import torch
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from transformers import AutoModel, AutoModelForCausalLM, AutoTokenizer

# =========================================
# Configuration
# =========================================

GEN_PATH = os.getenv("GUARD_GEN_PATH", "/models/Qwen3Guard-Gen-0.6B")
STREAM_PATH = os.getenv("GUARD_STREAM_PATH", "/models/Qwen3Guard-Stream-0.6B")
DEVICE = os.getenv("GUARD_DEVICE", "cpu")
THREADS = int(os.getenv("GUARD_THREADS", "4"))
SESSION_TTL = int(os.getenv("GUARD_SESSION_TTL", "600"))
# Optional on CPU: int8 dynamic quantization of the Stream model. Faster per token, but
# in testing it changed one prompt-level verdict, so full precision is the default.
STREAM_INT8 = os.getenv("GUARD_STREAM_INT8", "false").lower() == "true" and DEVICE == "cpu"

# Qwen3Guard-Stream supports 8192 positions; keep a small margin.
STREAM_MAX_TOKENS = 8000
# Longest single message passed to the Gen model (head + tail are kept).
GEN_MAX_CHARS = 12000

log = logging.getLogger("guard")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def pick_dtype() -> torch.dtype:
    forced = os.getenv("GUARD_DTYPE", "").lower()
    if forced in ("float32", "float16", "bfloat16"):
        return getattr(torch, forced)
    if DEVICE.startswith("cuda"):
        # bf16 needs Ampere (sm_80) or newer; older cards (e.g. RTX 20xx) use fp16.
        major, _ = torch.cuda.get_device_capability()
        return torch.bfloat16 if major >= 8 else torch.float16
    return torch.float32


DTYPE = pick_dtype()

# =========================================
# Text hygiene
# =========================================

# Qwen control tokens such as <|im_start|> must never be parsed out of user or
# model text, otherwise a message could forge extra conversation turns.
SPECIAL_TOKEN_RE = re.compile(r"<\|([a-z_]+)\|>")

# The Gen template renders turns as "USER: ..." / "ASSISTANT: ..."; neutralise
# look-alike lines so content cannot fake a new turn inside the template.
FAKE_TURN_RE = re.compile(r"(?m)^(\s*)(USER|ASSISTANT)\s*:")


def escape_special(text: str) -> str:
    return SPECIAL_TOKEN_RE.sub(r"< |\1| >", text)


def clip(text: str, limit: int = GEN_MAX_CHARS) -> str:
    if len(text) <= limit:
        return text
    head = limit * 2 // 3
    return text[:head] + "\n...\n" + text[-(limit - head):]


# =========================================
# Models
# =========================================

class Models:
    gen_tok = None
    gen = None
    stream_tok = None
    stream = None
    ready = False


M = Models()
gen_lock = threading.Lock()
stream_lock = threading.Lock()


def load_models():
    torch.set_num_threads(THREADS)
    t0 = time.time()

    M.gen_tok = AutoTokenizer.from_pretrained(GEN_PATH, local_files_only=True)
    M.gen = AutoModelForCausalLM.from_pretrained(
        GEN_PATH, dtype=DTYPE, local_files_only=True
    ).to(DEVICE).eval()

    # trust_remote_code loads modeling_qwen3_guard.py from the local model folder.
    M.stream_tok = AutoTokenizer.from_pretrained(
        STREAM_PATH, local_files_only=True, trust_remote_code=True
    )
    M.stream = AutoModel.from_pretrained(
        STREAM_PATH, dtype=DTYPE, local_files_only=True, trust_remote_code=True
    ).to(DEVICE).eval()
    if STREAM_INT8:
        M.stream = torch.ao.quantization.quantize_dynamic(M.stream, {torch.nn.Linear}, dtype=torch.qint8)

    build_gen_prefixes()

    # Warm up both models so the first real request is not slow.
    gen_moderate([{"role": "user", "content": "How do I reverse a list in Python?"}])
    session = open_stream("How do I reverse a list in Python?")
    append_stream(session, "Use slicing: items[::-1]", final=True)
    close_stream(session.sid)

    M.ready = True
    log.info("Guard models ready on %s (%s) in %.1fs", DEVICE, DTYPE, time.time() - t0)


# =========================================
# Qwen3Guard-Gen: prompt / response moderation
# =========================================

GEN_SAFETY_RE = re.compile(r"Safety:\s*(Safe|Unsafe|Controversial)")
GEN_CATEGORY_RE = re.compile(
    r"(Violent|Non-violent Illegal Acts|Sexual Content or Sexual Acts|PII|"
    r"Suicide & Self-Harm|Unethical Acts|Politically Sensitive Topics|"
    r"Copyright Violation|Jailbreak|None)"
)
GEN_REFUSAL_RE = re.compile(r"Refusal:\s*(Yes|No)")

# The Gen templates start with ~300 tokens of fixed instructions. Their KV cache is
# computed once per template kind and reused, so each call only processes the conversation.
GEN_PREFIXES: dict = {}
CONVERSATION_START = "<BEGIN CONVERSATION>\n\n"


def build_gen_prefixes():
    samples = {
        "prompt": [{"role": "user", "content": "x"}],
        "response": [{"role": "user", "content": "x"}, {"role": "assistant", "content": "x"}],
    }
    for kind, messages in samples.items():
        text = M.gen_tok.apply_chat_template(messages, tokenize=False)
        head = text[: text.index(CONVERSATION_START) + len(CONVERSATION_START)]
        ids = M.gen_tok(head, add_special_tokens=False, return_tensors="pt").input_ids.to(DEVICE)
        with torch.inference_mode():
            cache = M.gen(input_ids=ids, use_cache=True).past_key_values
        GEN_PREFIXES[kind] = (ids[0].tolist(), cache)


def gen_moderate(messages: List[dict]) -> dict:
    msgs = [
        {"role": m["role"], "content": FAKE_TURN_RE.sub(r"\1\2 :", escape_special(clip(m["content"])))}
        for m in messages
    ]
    text = M.gen_tok.apply_chat_template(msgs, tokenize=False)
    inputs = M.gen_tok([text], return_tensors="pt").to(DEVICE)

    # Reuse the cached instruction header only if it tokenized identically.
    extra = {}
    prefix = GEN_PREFIXES.get("prompt" if msgs[-1]["role"] == "user" else "response")
    if prefix and inputs.input_ids[0, :len(prefix[0])].tolist() == prefix[0]:
        extra["past_key_values"] = copy.deepcopy(prefix[1])

    # "Safety: Safe" needs no category line, so stop there; other verdicts run on.
    with gen_lock, torch.inference_mode():
        output = M.gen.generate(
            **inputs, **extra, max_new_tokens=32, do_sample=False, stop_strings=["Safe\n"], tokenizer=M.gen_tok
        )

    raw = M.gen_tok.decode(output[0][inputs.input_ids.shape[1]:], skip_special_tokens=True)
    safety = GEN_SAFETY_RE.search(raw)
    refusal = GEN_REFUSAL_RE.search(raw)
    category_line = raw.split("Categories:", 1)[1].split("\n", 1)[0] if "Categories:" in raw else ""

    return {
        # An unparseable verdict is treated as Unsafe (fail closed).
        "safety": safety.group(1) if safety else "Unsafe",
        "categories": [c for c in GEN_CATEGORY_RE.findall(category_line) if c != "None"],
        "refusal": refusal.group(1) if refusal else None,
        "raw": raw.strip(),
    }


# =========================================
# Qwen3Guard-Stream: token-level moderation
# =========================================

USER_MARK = "@@GUARD_USER@@"
ASSISTANT_MARK = "@@GUARD_ASSISTANT@@"


class StreamSession:
    def __init__(self, sid: str):
        self.sid = sid
        self.state = None
        self.text = ""
        self.fed: List[int] = []
        self.position = 0
        self.truncated = False
        self.query = {}
        self.last_used = time.time()
        self.lock = threading.Lock()


SESSIONS: dict = {}
SESSIONS_LOCK = threading.Lock()


def template_parts(prompt: str):
    """Token ids for the user turn and for the assistant header that precedes the reply."""
    text = M.stream_tok.apply_chat_template(
        [{"role": "user", "content": USER_MARK}, {"role": "assistant", "content": ASSISTANT_MARK}],
        tokenize=False,
        add_generation_prompt=False,
        enable_thinking=False,
    )
    content = escape_special(prompt)
    start = text.index(USER_MARK)
    text = text[:start] + content + text[start + len(USER_MARK):]
    split_at = text.index("<|im_end|>", start + len(content)) + len("<|im_end|>")
    user_text, header = text[:split_at], text[split_at:text.index(ASSISTANT_MARK)]

    encode = lambda s: M.stream_tok(s, add_special_tokens=False).input_ids
    return encode(user_text), encode(header)


def feed(session: StreamSession, token_id: int, role: str) -> dict:
    result, session.state = M.stream.stream_moderate_from_ids(
        torch.tensor([token_id]), role=role, stream_state=session.state
    )
    session.position += 1
    return {"risk": result["risk_level"][-1], "category": result["category"][-1]}


def open_stream(prompt: str) -> StreamSession:
    session = StreamSession(uuid.uuid4().hex)
    user_ids, header_ids = template_parts(clip(prompt, 20000))
    user_ids = user_ids[-(STREAM_MAX_TOKENS // 2):]

    with stream_lock, torch.inference_mode():
        result, session.state = M.stream.stream_moderate_from_ids(
            torch.tensor(user_ids), role="user", stream_state=None
        )
        session.position = len(user_ids)
        for token_id in header_ids:
            feed(session, token_id, "assistant")

    session.query = {"risk": result["risk_level"][-1], "category": result["category"][-1]}
    with SESSIONS_LOCK:
        SESSIONS[session.sid] = session
    return session


def append_stream(session: StreamSession, text: str, final: bool) -> dict:
    session.last_used = time.time()
    session.text += text

    ids = M.stream_tok(escape_special(session.text), add_special_tokens=False).input_ids
    # The last token can still merge with text that has not arrived yet.
    stable = ids if final else ids[:-1]
    new_ids = stable[len(session.fed):]

    risks = []
    with stream_lock, torch.inference_mode():
        for token_id in new_ids:
            if session.position >= STREAM_MAX_TOKENS:
                session.truncated = True
                break
            verdict = feed(session, token_id, "assistant")
            session.fed.append(token_id)
            risks.append(verdict)

    return {"risks": risks, "fed": len(session.fed), "truncated": session.truncated}


def close_stream(sid: str):
    with SESSIONS_LOCK:
        session = SESSIONS.pop(sid, None)
    if session is not None and session.state is not None:
        with stream_lock:
            M.stream.close_stream(session.state)


def reap_idle_sessions():
    while True:
        time.sleep(60)
        cutoff = time.time() - SESSION_TTL
        with SESSIONS_LOCK:
            stale = [sid for sid, s in SESSIONS.items() if s.last_used < cutoff]
        for sid in stale:
            close_stream(sid)


def get_session(sid: str) -> StreamSession:
    with SESSIONS_LOCK:
        session = SESSIONS.get(sid)
    if session is None:
        raise HTTPException(status_code=404, detail="Unknown or expired stream session")
    return session


# =========================================
# API
# =========================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    threading.Thread(target=load_models, daemon=True).start()
    threading.Thread(target=reap_idle_sessions, daemon=True).start()
    yield


app = FastAPI(title="CodeGen Guard", lifespan=lifespan)


class Message(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=200_000)


class ModerateRequest(BaseModel):
    messages: List[Message] = Field(min_length=1, max_length=2)


class StreamOpenRequest(BaseModel):
    prompt: str = Field(max_length=200_000)


class StreamAppendRequest(BaseModel):
    text: str = Field(default="", max_length=200_000)
    final: bool = False


def require_ready():
    if not M.ready:
        raise HTTPException(status_code=503, detail="Guard models are still loading")


@app.get("/health")
def health():
    if not M.ready:
        return JSONResponse({"status": "loading"}, status_code=503)
    return {
        "status": "ok",
        "device": DEVICE,
        "dtype": str(DTYPE).replace("torch.", ""),
        "stream_int8": STREAM_INT8,
        "streams": len(SESSIONS),
    }


@app.post("/v1/moderate")
def moderate(req: ModerateRequest):
    require_ready()
    t0 = time.time()
    result = gen_moderate([m.model_dump() for m in req.messages])
    result["ms"] = int((time.time() - t0) * 1000)
    return result


@app.post("/v1/stream")
def stream_open(req: StreamOpenRequest):
    require_ready()
    t0 = time.time()
    session = open_stream(req.prompt)
    return {"session_id": session.sid, **session.query, "ms": int((time.time() - t0) * 1000)}


@app.post("/v1/stream/{sid}")
def stream_append(sid: str, req: StreamAppendRequest):
    require_ready()
    session = get_session(sid)
    t0 = time.time()
    with session.lock:
        result = append_stream(session, req.text, req.final)
    result["ms"] = int((time.time() - t0) * 1000)
    return result


@app.delete("/v1/stream/{sid}")
def stream_close(sid: str):
    close_stream(sid)
    return {"closed": True}
