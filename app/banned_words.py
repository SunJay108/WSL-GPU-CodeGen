"""
Configurable banned-words blocklist, applied to both questions and answers.

The operator supplies the list (this file ships none): one word or short phrase
per line in ${BANNED_WORDS_FILE} (default ${DATA_DIR}/banned_words.txt), '#' for
comments, plus anything in the BANNED_WORDS environment variable. The file is
re-read when it changes, so edits take effect without a restart.

Matching is built to be hard to slip past yet precise:

  * case-insensitive and accent-folded ("Forbïdden" == "forbidden");
  * common digit leetspeak is undone ("f0rb1dden" == "forbidden");
  * for words of >= FUZZY_MIN_LEN letters, punctuation or spaces between the
    letters are ignored, so "f.o.r.b.i.d.d.e.n" and "f o r b i d d e n" match;
  * only *separator* characters may sit between a word's letters, never other
    letters or digits, and every match needs a word boundary on both sides, so
    a banned "cat" never fires inside "concatenate", "category" or "scatter".
"""

import logging
import os
import re
import threading
import time
import unicodedata
from typing import List, Optional, Tuple

from config import DATA_DIR

log = logging.getLogger("banned")

BANNED_WORDS_FILE = os.getenv("BANNED_WORDS_FILE", os.path.join(DATA_DIR, "banned_words.txt"))
# Short words are matched exactly (letters adjacent); longer words also match
# through punctuation/space between their letters. This keeps short entries from
# firing on coincidental letter sequences.
FUZZY_MIN_LEN = 4
# Separator characters allowed between the letters of a longer word.
MAX_GAP = max(0, int(os.getenv("BANNED_WORDS_MAX_GAP", "2")))

# Undo the usual digit/symbol leetspeak. Only characters that rarely carry
# meaning between letters are mapped, so ordinary code punctuation (+ | ! etc.)
# is treated as a separator rather than a letter.
LEET = str.maketrans({
    "0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "6": "g", "7": "t", "8": "b", "@": "a",
})

_lock = threading.Lock()
_compiled: List[Tuple[str, "re.Pattern"]] = []   # (original entry, matcher)
_combined: Optional["re.Pattern"] = None         # fast "any match?" over all entries
_signature = None                                # (file mtime, inline value) last built from


def normalize(text: str) -> str:
    """Lower-case, strip accents and undo digit leetspeak, preserving length roughly."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    return text.casefold().translate(LEET)


def _token_pattern(token: str) -> str:
    letters = [re.escape(c) for c in token]
    if len(token) >= FUZZY_MIN_LEN and MAX_GAP:
        return ("[^a-z0-9]{0,%d}" % MAX_GAP).join(letters)
    return "".join(letters)


def _entry_pattern(normalized_entry: str) -> str:
    # A phrase's tokens must be separated by at least one non-alphanumeric char.
    body = r"[^a-z0-9]+".join(_token_pattern(t) for t in normalized_entry.split())
    return r"(?<![a-z0-9])(?:%s)(?![a-z0-9])" % body


def _read_entries() -> List[str]:
    raw = os.getenv("BANNED_WORDS", "").replace(",", "\n")
    try:
        with open(BANNED_WORDS_FILE, encoding="utf-8") as f:
            raw += "\n" + f.read()
    except FileNotFoundError:
        pass
    except OSError as error:
        log.warning("Could not read %s: %s", BANNED_WORDS_FILE, error)

    seen, entries = set(), []
    for line in raw.splitlines():
        entry = line.split("#", 1)[0].strip()
        if not entry:
            continue
        key = normalize(entry)
        if key and key not in seen:
            seen.add(key)
            entries.append(entry)
    return entries


def _current_signature():
    try:
        mtime = os.path.getmtime(BANNED_WORDS_FILE)
    except OSError:
        mtime = None
    return (mtime, os.getenv("BANNED_WORDS", ""))


def _ensure_loaded():
    global _compiled, _combined, _signature
    signature = _current_signature()
    if signature == _signature and _signature is not None:
        return
    with _lock:
        if signature == _signature and _signature is not None:
            return
        entries = _read_entries()
        _compiled = [(entry, re.compile(_entry_pattern(normalize(entry)))) for entry in entries]
        _combined = re.compile("|".join(_entry_pattern(normalize(e)) for e in entries)) if entries else None
        _signature = signature
        log.info("Loaded %d banned word(s)", len(entries))


def find(text: str) -> List[str]:
    """Return the banned entries that match `text` (empty if none or the list is empty)."""
    try:
        _ensure_loaded()
        if _combined is None:
            return []
        norm = normalize(text)
        if not _combined.search(norm):
            return []
        return [entry for entry, matcher in _compiled if matcher.search(norm)]
    except Exception:  # never let a blocklist problem crash a request
        log.exception("Banned-words check failed")
        return []


def contains(text: str) -> bool:
    return bool(find(text))


def count() -> int:
    _ensure_loaded()
    return len(_compiled)
