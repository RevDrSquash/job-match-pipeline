"""Skill-graph explorer API: search, detail, neighborhood projection, stats."""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.models import (
    Concept,
    ConceptAlias,
    ConceptEdge,
    SourceConcept,
    SourceEdge,
    SourceMapping,
)
from app.main import create_app
from app.skills.importers.common import source_concept_id
from tests.conftest import requires_db

PYTHON_ID = uuid.uuid5(uuid.NAMESPACE_URL, "skill-api-python")
PYTHONIUM_ID = uuid.uuid5(uuid.NAMESPACE_URL, "skill-api-pythonium")
SHARED_A_ID = uuid.UUID("00000000-0000-5000-8000-00000000000a")
SHARED_B_ID = uuid.UUID("00000000-0000-5000-8000-00000000000b")
DOCKER_ID = uuid.uuid5(uuid.NAMESPACE_URL, "skill-api-docker")
K8S_ID = uuid.uuid5(uuid.NAMESPACE_URL, "skill-api-k8s")
HELM_ID = uuid.uuid5(uuid.NAMESPACE_URL, "skill-api-helm")
PROGRAMMING_ID = uuid.uuid5(uuid.NAMESPACE_URL, "skill-api-programming")
MISSING_ID = uuid.uuid5(uuid.NAMESPACE_URL, "skill-api-missing")


def _session_override(db_session: Session):
    from contextlib import contextmanager

    @contextmanager
    def _override():
        yield db_session

    return _override


def _client(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    application = create_app(settings=Settings(embedding_provider="hashing"))
    monkeypatch.setattr("app.api.router.db_session", _session_override(db_session))
    return TestClient(application)


def _clear_graph(session: Session) -> None:
    for model in (
        ConceptEdge,
        SourceEdge,
        SourceMapping,
        ConceptAlias,
        SourceConcept,
        Concept,
    ):
        session.execute(delete(model))
    session.flush()


def _add_concept(
    session: Session,
    concept_id: uuid.UUID,
    name: str,
    *,
    concept_type: str = "skill",
    aliases: tuple[tuple[str, str], ...] = (),
    description: str | None = None,
) -> Concept:
    from app.skills.normalize import normalize_label

    concept = Concept(
        id=concept_id,
        canonical_name=name,
        normalized_name=normalize_label(name),
        concept_type=concept_type,
        description=description,
        status="active",
    )
    session.add(concept)
    session.flush()
    for alias, alias_type in aliases:
        session.add(
            ConceptAlias(
                concept_id=concept_id,
                normalized_alias=normalize_label(alias),
                alias=alias,
                alias_type=alias_type,
            )
        )
    session.flush()
    return concept


def _add_source(
    session: Session,
    *,
    external_id: str,
    name: str,
    source_type: str,
    source: str = "onet",
    version: str = "31.0",
) -> SourceConcept:
    row = SourceConcept(
        id=source_concept_id(source, version, external_id),
        source=source,
        source_version=version,
        external_id=external_id,
        name=name,
        source_type=source_type,
    )
    session.add(row)
    session.flush()
    return row


def _map(
    session: Session,
    source_concept: SourceConcept,
    concept_id: uuid.UUID,
    *,
    mapping_type: str = "exact",
    mapping_method: str = "import",
    confidence: float = 1.0,
) -> None:
    session.add(
        SourceMapping(
            source_concept_id=source_concept.id,
            concept_id=concept_id,
            mapping_type=mapping_type,
            mapping_method=mapping_method,
            confidence=confidence,
        )
    )
    session.flush()


def _source_edge(
    session: Session,
    subject: SourceConcept,
    obj: SourceConcept,
    *,
    predicate: str = "IS_A",
) -> None:
    session.add(
        SourceEdge(
            subject_id=subject.id,
            predicate=predicate,
            object_id=obj.id,
            confidence=1.0,
        )
    )
    session.flush()


@requires_db
def test_skill_stats_shape(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_graph(db_session)
    _add_concept(
        db_session,
        PYTHON_ID,
        "Python",
        aliases=(("Python", "preferred"), ("python3", "alt")),
    )
    _add_concept(db_session, DOCKER_ID, "Docker", concept_type="technology")
    docker_src = _add_source(
        db_session, external_id="software:docker", name="Docker", source_type="technology"
    )
    _map(db_session, docker_src, DOCKER_ID)
    category = _add_source(
        db_session,
        external_id="category:containerization",
        name="Containerization",
        source_type="technology_category",
    )
    _source_edge(db_session, docker_src, category)

    client = _client(db_session, monkeypatch)
    response = client.get("/api/skills/stats")
    assert response.status_code == 200
    body = response.json()
    assert body["concepts_by_type"] == {"skill": 1, "technology": 1}
    assert body["aliases_by_type"] == {"alt": 1, "preferred": 1}
    assert body["source_concepts"] == [
        {"source": "onet", "source_version": "31.0", "count": 2}
    ]
    assert body["edges"] == {"canonical": 0, "source": 1}


@requires_db
def test_skill_search_exact_precedes_trgm_and_orders_by_id(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_graph(db_session)
    # SHARED_A_ID < SHARED_B_ID; both exact-match "shared".
    _add_concept(
        db_session,
        SHARED_B_ID,
        "Shared B",
        aliases=(("shared", "preferred"),),
    )
    _add_concept(
        db_session,
        SHARED_A_ID,
        "Shared A",
        aliases=(("shared", "preferred"),),
    )
    _add_concept(
        db_session,
        PYTHON_ID,
        "Python",
        aliases=(("Python", "preferred"),),
    )
    _add_concept(
        db_session,
        PYTHONIUM_ID,
        "Pythonium",
        aliases=(("Pythonium", "preferred"),),
    )

    client = _client(db_session, monkeypatch)
    exact = client.get("/api/skills/search", params={"q": "shared"})
    assert exact.status_code == 200
    shared_ids = [row["id"] for row in exact.json()["results"]]
    assert shared_ids[:2] == [str(SHARED_A_ID), str(SHARED_B_ID)]

    fuzzy = client.get("/api/skills/search", params={"q": "python"})
    assert fuzzy.status_code == 200
    labels = [row["label"] for row in fuzzy.json()["results"]]
    assert labels[0] == "Python"
    assert "Pythonium" in labels
    assert labels.index("Python") < labels.index("Pythonium")
    python_hit = fuzzy.json()["results"][0]
    assert python_hit["matched_alias"] == "Python"
    assert python_hit["concept_type"] == "skill"


@requires_db
def test_skill_search_empty_query(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_graph(db_session)
    _add_concept(db_session, PYTHON_ID, "Python", aliases=(("Python", "preferred"),))
    client = _client(db_session, monkeypatch)
    response = client.get("/api/skills/search", params={"q": "   "})
    assert response.status_code == 200
    assert response.json() == {"results": []}


@requires_db
def test_skill_detail_and_404(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_graph(db_session)
    _add_concept(
        db_session,
        PYTHON_ID,
        "Python",
        description="A programming language.",
        aliases=(
            ("Python", "preferred"),
            ("python3", "alt"),
            ("py", "curated"),
            ("python language", "derived"),
        ),
    )
    src = _add_source(
        db_session,
        external_id="http://data.europa.eu/esco/skill/python",
        name="Python",
        source_type="skill",
        source="esco",
        version="1.2.1",
    )
    _map(
        db_session,
        src,
        PYTHON_ID,
        mapping_type="exact",
        mapping_method="import",
        confidence=1.0,
    )

    client = _client(db_session, monkeypatch)
    missing = client.get(f"/api/skills/{MISSING_ID}")
    assert missing.status_code == 404
    assert missing.json()["detail"] == "skill not found"

    response = client.get(f"/api/skills/{PYTHON_ID}")
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == str(PYTHON_ID)
    assert body["canonical_name"] == "Python"
    assert body["concept_type"] == "skill"
    assert body["status"] == "active"
    assert body["description"] == "A programming language."
    assert body["aliases"]["preferred"] == ["Python"]
    assert body["aliases"]["alt"] == ["python3"]
    assert body["aliases"]["curated"] == ["py"]
    assert body["aliases"]["derived"] == ["python language"]
    assert body["sources"] == [
        {
            "source": "esco",
            "source_version": "1.2.1",
            "external_id": "http://data.europa.eu/esco/skill/python",
            "name": "Python",
            "mapping_type": "exact",
            "mapping_method": "import",
            "confidence": 1.0,
        }
    ]


@requires_db
def test_skill_graph_projects_synthetic_category_and_siblings(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_graph(db_session)
    _add_concept(db_session, DOCKER_ID, "Docker", concept_type="technology")
    _add_concept(db_session, K8S_ID, "Kubernetes", concept_type="technology")
    _add_concept(db_session, HELM_ID, "Helm", concept_type="technology")
    _add_concept(db_session, PROGRAMMING_ID, "computer programming", concept_type="knowledge")
    db_session.add(
        ConceptEdge(
            subject_id=DOCKER_ID,
            predicate="IS_A",
            object_id=PROGRAMMING_ID,
            confidence=1.0,
        )
    )
    db_session.flush()

    category = _add_source(
        db_session,
        external_id="category:containerization",
        name="Containerization",
        source_type="technology_category",
    )
    docker_src = _add_source(
        db_session, external_id="software:docker", name="Docker", source_type="technology"
    )
    k8s_src = _add_source(
        db_session,
        external_id="software:kubernetes",
        name="Kubernetes",
        source_type="technology",
    )
    helm_src = _add_source(
        db_session, external_id="software:helm", name="Helm", source_type="technology"
    )
    _map(db_session, docker_src, DOCKER_ID)
    _map(db_session, k8s_src, K8S_ID)
    _map(db_session, helm_src, HELM_ID)
    _source_edge(db_session, docker_src, category)
    _source_edge(db_session, k8s_src, category)
    _source_edge(db_session, helm_src, category)

    client = _client(db_session, monkeypatch)
    missing = client.get(f"/api/skills/{MISSING_ID}/graph")
    assert missing.status_code == 404

    response = client.get(f"/api/skills/{DOCKER_ID}/graph", params={"depth": 1})
    assert response.status_code == 200
    body = response.json()
    nodes = {node["id"]: node for node in body["nodes"]}
    category_id = "source:onet:category:containerization"
    assert nodes[str(DOCKER_ID)]["layer"] == "canonical"
    assert nodes[str(DOCKER_ID)]["label"] == "Docker"
    assert nodes[category_id]["layer"] == "source"
    assert nodes[category_id]["concept_type"] == "technology_category"
    assert nodes[category_id]["member_count"] == 3
    assert str(K8S_ID) in nodes
    assert str(HELM_ID) in nodes
    assert str(PROGRAMMING_ID) in nodes
    assert nodes[str(PROGRAMMING_ID)]["layer"] == "canonical"

    edges = {(edge["source"], edge["target"], edge["layer"]) for edge in body["edges"]}
    assert (str(DOCKER_ID), str(PROGRAMMING_ID), "canonical") in edges
    assert (str(DOCKER_ID), category_id, "source") in edges
    assert (str(K8S_ID), category_id, "source") in edges
    assert body["truncated"] is False


@requires_db
def test_skill_graph_caps_category_members(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_graph(db_session)
    from app.skills.normalize import normalize_label

    category = _add_source(
        db_session,
        external_id="category:tools",
        name="Tools",
        source_type="technology_category",
    )
    member_ids: list[uuid.UUID] = []
    for index in range(1, 31):
        name = f"Member {index:02d}"
        concept_id = uuid.uuid5(uuid.NAMESPACE_URL, f"skill-api-member-{index:02d}")
        member_ids.append(concept_id)
        db_session.add(
            Concept(
                id=concept_id,
                canonical_name=name,
                normalized_name=normalize_label(name),
                concept_type="technology",
            )
        )
        src = _add_source(
            db_session,
            external_id=f"software:member-{index:02d}",
            name=name,
            source_type="technology",
        )
        _map(db_session, src, concept_id)
        _source_edge(db_session, src, category)
    db_session.flush()

    selected = member_ids[0]
    client = _client(db_session, monkeypatch)
    response = client.get(f"/api/skills/{selected}/graph")
    assert response.status_code == 200
    body = response.json()
    category_id = "source:onet:category:tools"
    nodes = {node["id"]: node for node in body["nodes"]}
    assert nodes[category_id]["member_count"] == 30
    member_nodes = [
        node
        for node in body["nodes"]
        if node["layer"] == "canonical" and node["id"] != category_id
    ]
    assert len(member_nodes) == 25
    labels = sorted(node["label"] for node in member_nodes)
    assert labels == [f"Member {index:02d}" for index in range(1, 26)]
    assert body["truncated"] is True
    assert str(member_ids[29]) not in nodes
