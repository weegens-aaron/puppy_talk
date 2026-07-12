"""Turn agent markdown output into something worth speaking aloud.

Strips code blocks, markdown syntax, URLs, and table noise so the TTS
voice reads prose instead of punctuation soup.
"""

import re

DEFAULT_MAX_CHARS = 1500

_FENCED_CODE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE = re.compile(r"`([^`\n]*)`")
_MD_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_BARE_URL = re.compile(r"https?://\S+")
_HEADING = re.compile(r"^#{1,6}\s+", re.MULTILINE)
_EMPHASIS = re.compile(r"(\*{1,3}|_{1,3})(\S(?:.*?\S)?)\1")
_BULLET = re.compile(r"^\s*[-*+]\s+", re.MULTILINE)
_NUMBERED = re.compile(r"^\s*\d+[.)]\s+", re.MULTILINE)
_BLOCKQUOTE = re.compile(r"^\s*>\s?", re.MULTILINE)
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$", re.MULTILINE)
_HORIZONTAL_RULE = re.compile(r"^\s*([-*_]\s*){3,}$", re.MULTILINE)
_MULTI_WS = re.compile(r"[ \t]+")
_MULTI_NEWLINE = re.compile(r"\n{2,}")
_SENTENCE_END = re.compile(r"[.!?][\"')\]]?\s")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])[\"')\]]*\s+")


def sanitize(text: str, max_chars: int = DEFAULT_MAX_CHARS) -> str:
    """Return a speakable version of *text*, or an empty string."""
    if not text or not text.strip():
        return ""

    out = _FENCED_CODE.sub(" Code block omitted. ", text)
    out = _TABLE_ROW.sub(" ", out)
    out = _HORIZONTAL_RULE.sub(" ", out)
    out = _MD_IMAGE.sub(" ", out)
    out = _MD_LINK.sub(r"\1", out)
    out = _BARE_URL.sub(" a link ", out)
    out = _INLINE_CODE.sub(r"\1", out)
    out = _HEADING.sub("", out)
    # Run emphasis twice: handles bold nested inside italics and friends.
    out = _EMPHASIS.sub(r"\2", out)
    out = _EMPHASIS.sub(r"\2", out)
    out = _BULLET.sub("", out)
    out = _NUMBERED.sub("", out)
    out = _BLOCKQUOTE.sub("", out)
    out = _MULTI_WS.sub(" ", out)
    out = _MULTI_NEWLINE.sub("\n", out)
    out = out.strip()

    return _truncate(out, max_chars)


def speakable_cut(text: str, min_chars: int = 60) -> int:
    """Index just past the last complete sentence safe to speak, else 0.

    Used for live streaming: only cut at sentence boundaries that fall
    outside unclosed code fences, and only once enough text accumulated.
    """
    best = 0
    for match in _SENTENCE_END.finditer(text):
        end = match.end()
        if text.count("```", 0, end) % 2 == 0:
            best = end
    return best if best >= min_chars else 0


def split_speech_chunks(text: str, target_chars: int = 260) -> list[str]:
    """Group sentences into chunks near *target_chars* for pipelined TTS.

    Playing chunk N while chunk N+1 synthesizes keeps time-to-first-audio
    close to one short sentence instead of the whole response.
    """
    pieces: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if line:
            pieces.extend(
                p.strip() for p in _SENTENCE_SPLIT.split(line) if p.strip()
            )

    chunks: list[str] = []
    current = ""
    for piece in pieces:
        for part in _hard_split(piece, target_chars * 2):
            if current and len(current) + len(part) + 1 > target_chars:
                chunks.append(current)
                current = part
            else:
                current = f"{current} {part}".strip()
    if current:
        chunks.append(current)
    return chunks


def _hard_split(piece: str, limit: int):
    """Yield word-boundary slices of a pathologically long sentence."""
    while len(piece) > limit:
        cut = piece.rfind(" ", 0, limit)
        cut = cut if cut > 0 else limit
        yield piece[:cut]
        piece = piece[cut:].strip()
    if piece:
        yield piece


def _truncate(text: str, max_chars: int) -> str:
    """Cut at the last sentence boundary before *max_chars*."""
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    head = text[:max_chars]
    matches = list(_SENTENCE_END.finditer(head))
    if matches:
        return head[: matches[-1].end()].strip()
    return head.rsplit(" ", 1)[0].strip() + "."
