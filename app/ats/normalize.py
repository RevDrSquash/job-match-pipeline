"""JD HTML normalization — strip tags and collapse boilerplate whitespace."""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser

import nh3


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript"}:
            self._skip_depth += 1
            return
        if tag in {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"}:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"} and self._skip_depth:
            self._skip_depth -= 1
            return
        if tag in {"p", "div", "li", "tr"}:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        self._chunks.append(data)


_WHITESPACE_RE = re.compile(r"[ \t\f\v]+")
_BLANK_LINES_RE = re.compile(r"\n{3,}")
# Common ATS chrome / apply-button leftovers that add no JD signal.
_BOILERPLATE_RE = re.compile(
    r"(?im)^(?:apply now|submit application|powered by (?:greenhouse|lever|ashby)"
    r"|equal opportunity employer\.?)\s*$"
)


def html_to_text(raw: str | None) -> str | None:
    """Convert HTML (or plain text) JD body to normalized plain text."""
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None
    # Greenhouse often returns entity-escaped HTML (&lt;div&gt;...); unescape first.
    text = html.unescape(text)
    if "<" in text and ">" in text:
        parser = _TextExtractor()
        try:
            parser.feed(text)
            parser.close()
        except Exception:
            text = re.sub(r"<[^>]+>", " ", text)
        else:
            text = "".join(parser._chunks)
        text = html.unescape(text)
    lines = []
    for line in text.splitlines():
        cleaned = _WHITESPACE_RE.sub(" ", line).strip()
        if not cleaned or _BOILERPLATE_RE.match(cleaned):
            continue
        lines.append(cleaned)
    normalized = _BLANK_LINES_RE.sub("\n\n", "\n".join(lines)).strip()
    return normalized or None


# Display-only allowlist. Prompts and search still use html_to_text() / raw_jd.
_JD_HTML_TAGS = frozenset(
    {
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "p",
        "ul",
        "ol",
        "li",
        "strong",
        "em",
        "b",
        "i",
        "a",
        "br",
        "table",
        "thead",
        "tbody",
        "tfoot",
        "tr",
        "th",
        "td",
    }
)
_JD_HTML_ATTRIBUTES = {
    "a": {"href", "title"},
    "th": {"colspan", "rowspan"},
    "td": {"colspan", "rowspan"},
}
_HTML_TAG_RE = re.compile(r"<[a-zA-Z/!?]")


def sanitize_jd_html(raw: str | None) -> str | None:
    """Sanitize ATS HTML for UI display. Returns None for empty or plain-text input."""
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None
    # Greenhouse often returns entity-escaped HTML (&lt;div&gt;...); unescape first.
    text = html.unescape(text)
    if not _HTML_TAG_RE.search(text):
        return None
    cleaned = nh3.clean(
        text,
        tags=_JD_HTML_TAGS,
        attributes=_JD_HTML_ATTRIBUTES,
        link_rel="noopener noreferrer",
    ).strip()
    if not cleaned or not _HTML_TAG_RE.search(cleaned):
        return None
    return cleaned
