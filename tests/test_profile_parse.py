"""Resume parse, span-ID stability, synthesis, and default filters."""

from __future__ import annotations

from pathlib import Path

from app.embeddings import hash_embed
from app.llm import FakeLlmClient
from app.profile.filters import derive_default_filters
from app.profile.parse import FallbackResumeParser, LlmResumeParser, assign_span_ids
from app.profile.schema import LlmParsePayload
from app.profile.synthesize import synthesize_profile_doc
from app.profile.text import extract_resume_text
from app.skills.linker import SkillLinker

FIXTURE = Path(__file__).parent / "fixtures" / "sample_resume.md"

LLM_JSON = """
{
  "work_history": [
    {
      "employer": "Contoso",
      "title": "Software Engineer",
      "start_date": "2018-06",
      "end_date": "2020-12",
      "location": "Toronto, ON",
      "bullets": ["Implemented REST APIs in Python and Docker"]
    },
    {
      "employer": "Northwind Labs",
      "title": "Senior Software Engineer",
      "start_date": "2021-01",
      "end_date": null,
      "location": "Vancouver, BC",
      "bullets": ["Built a Python and PostgreSQL ingestion service"]
    }
  ],
  "skill_spans": ["Python", "PostgreSQL", "Docker"],
  "locations": ["Vancouver, BC", "Toronto, ON"],
  "seniority": "senior",
  "title_families": ["Backend Engineering"],
  "work_arrangement": ["remote"],
  "comp_floor": null,
  "summary": "Backend engineer focused on data pipelines."
}
"""


def test_span_ids_stable_across_role_reorder() -> None:
    a = LlmParsePayload.model_validate(
        {
            "work_history": [
                {
                    "employer": "Contoso",
                    "title": "Software Engineer",
                    "start_date": "2018-06",
                    "bullets": ["Did the thing"],
                },
                {
                    "employer": "Northwind Labs",
                    "title": "Senior Software Engineer",
                    "start_date": "2021-01",
                    "bullets": ["Did the other thing"],
                },
            ]
        }
    )
    b = LlmParsePayload.model_validate(
        {
            "work_history": list(reversed(a.work_history)),
        }
    )
    parsed_a = assign_span_ids(a)
    parsed_b = assign_span_ids(b)
    assert [e.employer for e in parsed_a.work_history] == [
        e.employer for e in parsed_b.work_history
    ]
    assert parsed_a.work_history[0].bullets[0].span_id == "wh:0:b:0"
    assert parsed_a.work_history[1].bullets[0].span_id == "wh:1:b:0"
    assert parsed_a.work_history[0].source == "parsed"
    assert parsed_a.model_dump() == parsed_b.model_dump()


def test_llm_parser_assigns_ids_and_provenance() -> None:
    parser = LlmResumeParser(FakeLlmClient(LLM_JSON))
    parsed = parser.parse("ignored")
    assert parsed.work_history[0].employer == "Contoso"
    assert parsed.work_history[0].source == "parsed"
    assert parsed.work_history[0].bullets[0].span_id == "wh:0:b:0"
    assert parsed.work_history[1].is_current is True


def test_fallback_parser_on_fixture_resume() -> None:
    text = FIXTURE.read_text(encoding="utf-8")
    parsed = FallbackResumeParser(SkillLinker()).parse(text)
    employers = [role.employer for role in parsed.work_history]
    assert employers == ["Contoso", "Northwind Labs"]
    assert parsed.work_history[1].is_current is True
    assert parsed.work_history[0].bullets[0].span_id == "wh:0:b:0"
    assert "Python" in parsed.skill_spans
    assert "remote" in parsed.work_arrangement
    assert parsed.seniority == "senior"
    assert parsed.locations == ["Vancouver, BC", "Toronto, ON"]


def test_synthesized_doc_is_job_description_shaped() -> None:
    parsed = LlmResumeParser(FakeLlmClient(LLM_JSON)).parse("ignored")
    linker = SkillLinker()
    skill_ids = [hit.skill_id for hit in linker.link_spans(parsed.skill_spans)]
    doc = synthesize_profile_doc(parsed, skill_ids, linker)
    assert doc.startswith("Title: Senior Software Engineer")
    assert "Seniority: senior" in doc
    assert "Skills: Python, PostgreSQL, Docker" in doc
    assert "Northwind Labs" in doc
    assert "Experience:" in doc


def test_default_filters_are_generous() -> None:
    parsed = LlmResumeParser(FakeLlmClient(LLM_JSON)).parse("ignored")
    filters = derive_default_filters(parsed)
    assert filters["seniority_band"] == "mid,senior,staff"
    assert filters["work_arrangement"] == ["remote"]
    assert "Vancouver, BC" in (filters["locations"] or [])
    assert filters["comp_floor"] is None


def test_hash_embed_is_768_and_stable() -> None:
    a = hash_embed("same document")
    b = hash_embed("same document")
    c = hash_embed("different document")
    assert len(a) == 768
    assert a == b
    assert a != c
    assert abs(sum(x * x for x in a) - 1.0) < 1e-6


def test_extract_text_and_markdown() -> None:
    text = extract_resume_text(b"# Hello\n\nWorld", "markdown")
    assert "Hello" in text


def test_extract_pdf_text() -> None:
    pdf = _minimal_pdf("Senior Engineer - Contoso")
    text = extract_resume_text(pdf, "pdf")
    assert "Contoso" in text


def _minimal_pdf(message: str) -> bytes:
    safe = message.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream = f"BT /F1 12 Tf 72 720 Td ({safe}) Tj ET\n".encode("latin-1")
    objects = [
        b"1 0 obj<< /Type /Catalog /Pages 2 0 R >>endobj\n",
        b"2 0 obj<< /Type /Pages /Kids [3 0 R] /Count 1 >>endobj\n",
        (
            b"3 0 obj<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>endobj\n"
        ),
        b"4 0 obj<< /Length %d >>stream\n" % len(stream) + stream + b"endstream\nendobj\n",
        b"5 0 obj<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>endobj\n",
    ]
    header = b"%PDF-1.1\n"
    body = b"".join(objects)
    offsets = []
    cursor = len(header)
    for obj in objects:
        offsets.append(cursor)
        cursor += len(obj)
    xref_pos = len(header) + len(body)
    xref = ["xref", "0 6", "0000000000 65535 f "]
    xref.extend(f"{offset:010d} 00000 n " for offset in offsets)
    xref_bytes = ("\n".join(xref) + "\n").encode("ascii")
    trailer = (
        f"trailer<< /Size 6 /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n"
    ).encode("ascii")
    return header + body + xref_bytes + trailer
