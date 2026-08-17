"""Extract plain text from a resume file. Never log the extracted text."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

from app.privacy import PrivacySafeError, input_kind, safe_exc


def read_resume_file(path: Path) -> tuple[str, str]:
    """Return (plain_text, input_kind). Raises PrivacySafeError on failure."""
    kind = input_kind(str(path))
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise safe_exc("failed to read resume file", exc) from None
    return extract_resume_text(data, kind), kind


def extract_resume_text(data: bytes, kind: str) -> str:
    if not data:
        raise PrivacySafeError("resume file is empty")
    if kind == "pdf":
        return _extract_pdf(data)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("utf-8", errors="replace")
    text = text.strip()
    if not text:
        raise PrivacySafeError("resume file is empty")
    return text


def _extract_pdf(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise safe_exc("pypdf is not installed", exc) from None
    try:
        reader = PdfReader(BytesIO(data))
        pages = [(page.extract_text() or "") for page in reader.pages]
    except Exception as exc:  # noqa: BLE001 — wrap any pypdf failure without leaking
        raise safe_exc("PDF text extraction failed", exc) from None
    text = "\n".join(pages).strip()
    if not text:
        raise PrivacySafeError("PDF contained no extractable text")
    return text
