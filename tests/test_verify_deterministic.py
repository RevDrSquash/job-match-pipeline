"""Stage 1 deterministic checks: planted fabrications must be caught."""

from __future__ import annotations

from app.skills.linker import InMemorySkillLinker, SkillRecord
from app.verify.deterministic import run_deterministic_checks

PYTHON_ID = "esco:python"
AWS_ID = "esco:aws"
K8S_ID = "esco:kubernetes"

WORK_HISTORY = [
    {
        "employer": "Prior Co",
        "title": "Backend Engineer",
        "start_date": "2020-01",
        "end_date": "2023-06",
        "source": "parsed",
        "bullets": [
            {"span_id": "wh:0:b:0", "text": "Built APIs in Python for a team of 5"},
            {"span_id": "wh:0:b:1", "text": "Operated services on Amazon Web Services"},
        ],
    }
]

CLEAN_RESUME = """\
# Backend Engineer

## Prior Co — Backend Engineer (2020-01 – 2023-06)
- Built APIs in Python for a team of 5
- Operated services on Amazon Web Services
"""

CLEAN_MAP = {
    "attempt": 1,
    "employers": ["Prior Co"],
    "titles": ["Backend Engineer"],
    "date_ranges": ["2020-01 – 2023-06"],
    "claimed_skill_ids": [PYTHON_ID, AWS_ID],
    "claims": [
        {"text": "Prior Co", "span_ids": ["wh:0"], "kind": "employer"},
        {"text": "Backend Engineer", "span_ids": ["wh:0"], "kind": "title"},
        {"text": "2020-01 – 2023-06", "span_ids": ["wh:0"], "kind": "date_range"},
        {"text": "5", "span_ids": ["wh:0:b:0"], "kind": "number"},
        {
            "text": "Python",
            "span_ids": ["wh:0:b:0"],
            "kind": "skill",
            "canonical_skill_id": PYTHON_ID,
        },
    ],
}


def _linker() -> InMemorySkillLinker:
    return InMemorySkillLinker(
        [
            SkillRecord(id=PYTHON_ID, canonical_label="Python", alt_labels=("python3",)),
            SkillRecord(
                id=AWS_ID,
                canonical_label="Amazon Web Services",
                alt_labels=("AWS",),
            ),
            SkillRecord(
                id=K8S_ID,
                canonical_label="Kubernetes",
                alt_labels=("k8s",),
            ),
        ]
    )


def _codes(failures: list) -> set[str]:
    return {item.code for item in failures}


def test_clean_resume_passes_deterministic_checks() -> None:
    failures = run_deterministic_checks(
        resume_doc=CLEAN_RESUME,
        work_history=WORK_HISTORY,
        claim_source_map=CLEAN_MAP,
        user_skill_ids=[PYTHON_ID, AWS_ID],
        linker=_linker(),
    )
    assert failures == []


def test_fabricated_number_is_caught() -> None:
    resume = CLEAN_RESUME.replace("team of 5", "team of 50")
    failures = run_deterministic_checks(
        resume_doc=resume,
        work_history=WORK_HISTORY,
        claim_source_map=CLEAN_MAP,
        user_skill_ids=[PYTHON_ID, AWS_ID],
        linker=_linker(),
    )
    assert "fabricated_number" in _codes(failures)


def test_unknown_employer_is_caught() -> None:
    mapping = {
        **CLEAN_MAP,
        "employers": ["Completely Fake Corp"],
        "claims": [
            {
                "text": "Completely Fake Corp",
                "span_ids": ["wh:0"],
                "kind": "employer",
            }
        ],
    }
    resume = CLEAN_RESUME.replace("Prior Co", "Completely Fake Corp")
    failures = run_deterministic_checks(
        resume_doc=resume,
        work_history=WORK_HISTORY,
        claim_source_map=mapping,
        user_skill_ids=[PYTHON_ID, AWS_ID],
        linker=_linker(),
    )
    assert "unknown_employer" in _codes(failures)


def test_out_of_set_skill_is_caught() -> None:
    resume = CLEAN_RESUME + "\n- Managed Kubernetes clusters\n"
    mapping = {
        **CLEAN_MAP,
        "claimed_skill_ids": [PYTHON_ID, AWS_ID, K8S_ID],
    }
    failures = run_deterministic_checks(
        resume_doc=resume,
        work_history=WORK_HISTORY,
        claim_source_map=mapping,
        user_skill_ids=[PYTHON_ID, AWS_ID],
        linker=_linker(),
    )
    assert "out_of_set_skill" in _codes(failures)


def test_unknown_title_and_date_are_caught() -> None:
    mapping = {
        **CLEAN_MAP,
        "titles": ["Chief Fabrication Officer"],
        "date_ranges": ["1999-01 – 1999-12"],
    }
    failures = run_deterministic_checks(
        resume_doc=CLEAN_RESUME,
        work_history=WORK_HISTORY,
        claim_source_map=mapping,
        user_skill_ids=[PYTHON_ID, AWS_ID],
        linker=_linker(),
    )
    assert "unknown_title" in _codes(failures)
    assert "unknown_date_range" in _codes(failures)
