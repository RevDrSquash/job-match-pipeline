"""ATS adapter parsing tests against checked-in fixture JSON."""

from __future__ import annotations

import json
from pathlib import Path

from app.ats.ashby import AshbyAdapter
from app.ats.greenhouse import GreenhouseAdapter
from app.ats.lever import LeverAdapter
from app.ats.normalize import html_to_text, sanitize_jd_html

FIXTURES = Path(__file__).parent / "fixtures" / "ats"


def test_greenhouse_parses_fixture() -> None:
    payload = json.loads((FIXTURES / "greenhouse_jobs.json").read_text(encoding="utf-8"))
    postings = GreenhouseAdapter().parse_jobs_payload(payload)
    assert len(postings) == 2
    first = postings[0]
    assert first.url.startswith("http")
    assert first.title
    assert first.department or first.location
    assert first.raw_jd  # content=true fixture includes HTML JD
    assert "<" not in first.raw_jd
    assert first.raw_jd_html
    assert "content-intro" in first.raw_jd_html


def test_lever_parses_fixture() -> None:
    payload = json.loads((FIXTURES / "lever_postings.json").read_text(encoding="utf-8"))
    postings = LeverAdapter().parse_postings_payload(payload)
    assert len(postings) == 2
    first = postings[0]
    assert "lever.co" in first.url
    assert first.title
    assert first.employment_type  # categories.commitment
    assert first.work_arrangement in {None, "remote", "hybrid", "onsite", "unspecified"}
    assert first.raw_jd
    assert first.raw_jd_html
    assert "<div>" in first.raw_jd_html or "<p" in first.raw_jd_html
    assert "<h3>" in first.raw_jd_html
    assert "<ul>" in first.raw_jd_html


def test_ashby_parses_fixture() -> None:
    payload = json.loads((FIXTURES / "ashby_jobs.json").read_text(encoding="utf-8"))
    postings = AshbyAdapter().parse_jobs_payload(payload)
    assert len(postings) == 2
    first = postings[0]
    assert "ashbyhq.com" in first.url or first.url.startswith("http")
    assert first.title
    assert first.raw_jd
    assert first.raw_jd_html
    assert "<p" in first.raw_jd_html


def test_html_to_text_strips_tags_and_boilerplate() -> None:
    html = """
    <html><body>
      <style>.x{}</style>
      <h1>Engineer</h1>
      <p>Build APIs.</p>
      <p>Apply Now</p>
      <p>Powered by Greenhouse</p>
    </body></html>
    """
    text = html_to_text(html)
    assert text is not None
    assert "Engineer" in text
    assert "Build APIs." in text
    assert "Apply Now" not in text
    assert "Powered by" not in text
    assert "<" not in text


def test_sanitize_jd_html_strips_scripts_handlers_and_styles() -> None:
    raw = """
    <p class="lead" style="color:red" onclick="alert(1)">Hello<script>alert(1)</script></p>
    <a href="https://example.test/apply" style="color:blue">Apply</a>
    """
    cleaned = sanitize_jd_html(raw)
    assert cleaned is not None
    assert "Hello" in cleaned
    assert "<script" not in cleaned.lower()
    assert "onclick" not in cleaned
    assert "style=" not in cleaned
    assert "class=" not in cleaned
    assert "https://example.test/apply" in cleaned
    assert "noopener" in cleaned
    assert "noreferrer" in cleaned


def test_sanitize_jd_html_keeps_structure() -> None:
    raw = "<h2>Role</h2><p>Build APIs.</p><ul><li>Python</li></ul>"
    cleaned = sanitize_jd_html(raw)
    assert cleaned is not None
    assert "<h2>" in cleaned
    assert "<p>" in cleaned
    assert "<ul>" in cleaned
    assert "<li>" in cleaned
    assert "Build APIs." in cleaned


def test_sanitize_jd_html_plain_text_returns_none() -> None:
    assert sanitize_jd_html(None) is None
    assert sanitize_jd_html("") is None
    assert sanitize_jd_html("   ") is None
    assert sanitize_jd_html("Just a paragraph of text.") is None


def test_sanitize_jd_html_unescapes_entity_escaped_input() -> None:
    raw = "&lt;p&gt;Build APIs.&lt;/p&gt;"
    cleaned = sanitize_jd_html(raw)
    assert cleaned is not None
    assert "<p>" in cleaned
    assert "Build APIs." in cleaned
