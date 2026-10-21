import re
from langdetect import detect

# =========================================================
# 1. CODE INTENT KEYWORDS (EXPANDED - ChatGPT style)
# =========================================================

CODE_KEYWORDS = {
    # Core programming
    "core": [
        "code", "program", "function", "class", "object", "method",
        "variable", "loop", "condition", "syntax", "compile", "runtime"
    ],

    # Languages
    "languages": [
        "python", "java", "javascript", "typescript", "c++", "c#",
        "golang", "rust", "php", "kotlin", "swift", "ruby"
    ],

    # Web / backend
    "web": [
        "api", "rest", "graphql", "http", "request", "response",
        "backend", "frontend", "fullstack", "node", "express", "fastapi"
    ],

    # Data / DB
    "data": [
        "sql", "database", "mongodb", "redis", "postgres", "mysql",
        "query", "index", "schema"
    ],

    # DevOps / infra
    "devops": [
        "docker", "kubernetes", "linux", "bash", "shell", "terminal",
        "ci/cd", "pipeline", "deployment", "cloud", "cuda", "gpu"
    ],

    # AI / ML
    "ai": [
        "machine learning", "llm", "ai", "neural", "model",
        "training", "inference", "embedding", "llama", "llama.cpp",
        "ollama", "gradio", "gguf"
    ],

    # Debugging
    "debug": [
        "bug", "error", "exception", "fix", "debug", "issue",
        "stack trace", "crash"
    ]
}


# =========================================================
# 2. BLOCKED TOPICS (STRICT SAFETY / NON-CODE FILTER)
# =========================================================

BLOCKED_TOPICS = {
    "lifestyle": [
        "love", "relationship", "dating", "marriage", "breakup"
    ],
    "entertainment": [
        "movie", "film", "song", "music", "lyrics", "anime", "game"
    ],
    "news": [
        "politics", "election", "war", "government", "president"
    ],
    "daily_life": [
        "weather", "food", "recipe", "travel", "hotel", "flight"
    ],
    "nsfw": [
        "sex", "adult", "porn", "nude"
    ]
}


# =========================================================
# 3. SPAN / PATTERN DETECTION (ChatGPT-style safety layer)
# =========================================================

PATTERNS = {
    "url": r"https?://\S+|www\.\S+",
    "email": r"\b[\w\.-]+@[\w\.-]+\.\w+\b",
    "phone": r"\b\d{10,15}\b",

    # jailbreak / prompt injection attempts
    "jailbreak": r"(ignore previous instructions|act as|pretend you are|system prompt|developer mode)",

    # injection / manipulation attempts
    "injection": r"(reveal|show me your prompt|hidden instructions|internal policy)"
}


# =========================================================
# 4. CORE HELPERS
# =========================================================

def is_english(text: str) -> bool:
    try:
        return detect(text) == "en"
    except:
        return True


def flatten_keywords():
    """Convert nested keyword dict into single list"""
    all_words = []
    for group in CODE_KEYWORDS.values():
        all_words.extend(group)
    return all_words


CODE_FLAT = flatten_keywords()


def has_code_intent(text: str) -> bool:
    text = text.lower()

    return any(k in text for k in CODE_FLAT)


def has_blocked_topic(text: str) -> bool:
    text = text.lower()

    for group in BLOCKED_TOPICS.values():
        for item in group:
            if item in text:
                return True

    return False


def detect_patterns(text: str):
    """Return list of matched unsafe patterns"""
    matches = []

    for name, pattern in PATTERNS.items():
        if re.search(pattern, text, re.IGNORECASE):
            matches.append(name)

    return matches


# =========================================================
# 5. FINAL VALIDATION (MAIN ENTRY)
# =========================================================

def validate_prompt(text: str):
    text = text.strip()

    if not text:
        return False, "❌ Prompt cannot be empty."

    # 1. Language filter
    if not is_english(text):
        return False, "❌ Only English language is supported."

    # 2. Pattern detection (URLs, jailbreak, injection)
    pattern_hits = detect_patterns(text)
    if pattern_hits:
        return False, f"❌ Unsafe pattern detected: {', '.join(pattern_hits)}"

    # 3. Blocked topics
    if has_blocked_topic(text):
        return False, "❌ This topic is not allowed. Ask programming-related questions only."

    # 4. Code intent requirement
    if not has_code_intent(text):
        return False, "❌ Only programming / software engineering questions are allowed."

    # 5. Minimum quality check
    if len(text) < 6:
        return False, "❌ Prompt too short."

    return True, ""