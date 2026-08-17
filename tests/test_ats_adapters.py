"""ATS adapter parsing tests against checked-in fixture JSON."""

from __future__ import annotations

import json
from pathlib import Path

from app.ats.ashby import AshbyAdapter
from app.ats.greenhouse import GreenhouseAdapter
from app.ats.lever import LeverAdapter
from app.ats.normalize import html_to_text

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


def test_ashby_parses_fixture() -> None:
    payload = json.loads((FIXTURES / "ashby_jobs.json").read_text(encoding="utf-8"))
    postings = AshbyAdapter().parse_jobs_payload(payload)
    assert len(postings) == 2
    first = postings[0]
    assert "ashbyhq.com" in first.url or first.url.startswith("http")
    assert first.title
    assert first.raw_jd


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
