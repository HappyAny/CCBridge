from __future__ import annotations

import re

from ..config import MARKDOWN_V2_PLAIN_ESCAPE_CHARS, TELEGRAM_CHUNK, TELEGRAM_MAX_TEXT, TELEGRAM_PARSE_MODE

FENCED_CODE_RE = re.compile(r"```([^\n`]*)\n?(.*?)```", re.DOTALL)
INLINE_MARKDOWN_RE = re.compile(
    r"`([^`\n]+)`|\[([^\]\n]+)\]\(([^)\n]+)\)|\*\*([^\n*]+?)\*\*|\*([^\n*]+?)\*"
)

def clamp_telegram_text(text: str) -> str:
    if len(text) <= TELEGRAM_MAX_TEXT:
        return text
    return text[: TELEGRAM_MAX_TEXT - 40] + "\n\n[truncated; full text continues later]"

def format_telegram_text_for_parse_mode(text: str) -> str:
    if TELEGRAM_PARSE_MODE == "MarkdownV2":
        return markdown_to_telegram_markdown_v2(text)
    return text

def markdown_to_telegram_markdown_v2(text: str) -> str:
    parts: list[str] = []
    cursor = 0
    for match in FENCED_CODE_RE.finditer(text):
        parts.append(inline_markdown_to_telegram_markdown_v2(text[cursor : match.start()]))
        language = sanitize_code_language(match.group(1))
        code = escape_markdown_v2_code(match.group(2))
        if language:
            parts.append(f"```{language}\n{code}```")
        else:
            parts.append(f"```\n{code}```")
        cursor = match.end()
    parts.append(inline_markdown_to_telegram_markdown_v2(text[cursor:]))
    return "".join(parts)

def inline_markdown_to_telegram_markdown_v2(text: str) -> str:
    parts: list[str] = []
    cursor = 0
    for match in INLINE_MARKDOWN_RE.finditer(text):
        parts.append(escape_markdown_v2_plain(text[cursor : match.start()]))
        inline_code, link_label, link_url, bold_text, italic_text = match.groups()
        if inline_code is not None:
            parts.append(f"`{escape_markdown_v2_code(inline_code)}`")
        elif link_label is not None and link_url is not None:
            label = escape_markdown_v2_plain(link_label)
            url = escape_markdown_v2_link_url(link_url)
            parts.append(f"[{label}]({url})")
        elif bold_text is not None:
            parts.append(f"*{escape_markdown_v2_plain(bold_text)}*")
        elif italic_text is not None:
            parts.append(f"_{escape_markdown_v2_plain(italic_text)}_")
        cursor = match.end()
    parts.append(escape_markdown_v2_plain(text[cursor:]))
    return "".join(parts)

def escape_markdown_v2_plain(text: str) -> str:
    return "".join(f"\\{char}" if char in MARKDOWN_V2_PLAIN_ESCAPE_CHARS else char for char in text)

def escape_markdown_v2_code(text: str) -> str:
    return "".join(f"\\{char}" if char in "\\`" else char for char in text)

def escape_markdown_v2_link_url(text: str) -> str:
    return "".join(f"\\{char}" if char in "\\)" else char for char in text)

def sanitize_code_language(language: str) -> str:
    token = language.strip().split(maxsplit=1)[0] if language.strip() else ""
    return re.sub(r"[^A-Za-z0-9_+-]", "", token)[:32]

def is_telegram_parse_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return "can't parse entities" in message or "parse entities" in message

def split_telegram_text(text: str) -> list[str]:
    if not text:
        return []
    chunks: list[str] = []
    remaining = text
    while remaining:
        chunks.append(remaining[:TELEGRAM_CHUNK])
        remaining = remaining[TELEGRAM_CHUNK:]
    return chunks

