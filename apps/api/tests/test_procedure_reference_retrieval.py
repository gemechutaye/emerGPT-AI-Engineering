"""Source-based second-stage retrieval; no generated answer or provider calls are used."""

import json
from hashlib import sha256
from pathlib import Path

import pytest
from emer.services.answering import model_context
from emer.services.ingestion import Bundle, ingest
from emer.services.retrieval import RetrievalService, procedure_title_aliases

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="module")
def original():
    return ingest(ROOT / "config/corpus.json")


def source(identifier, title, text, category="procedure"):
    return {
        "doc_id": identifier,
        "title": title,
        "text": text,
        "category": category,
        "version": "1.0",
        "effective_date": "2026-09-01",
        "authority": "Synthetic Test Source",
    }


def fixture_bundle(tmp_path, records):
    raw = b"".join(json.dumps(r).encode() + b"\n" for r in records)
    (tmp_path / "records.jsonl").write_bytes(raw)
    manifest = {
        "schema_version": 1,
        "corpus": {
            "path": "records.jsonl",
            "sha256": sha256(raw).hexdigest(),
            "bytes": len(raw),
            "records": len(records),
        },
        "preserve": [],
        "policies": [],
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    return ingest(path)


def parts(packet):
    return packet.retrieval["scoped_selection"]


@pytest.mark.parametrize(
    "question",
    [
        "Briefly remind me what treatment PT-006 discussed.",
        "What does the treatment discussed for patient PT-006 combine?",
        "For patient PT-006, what exact downtime in days and procedure price are documented for the treatment discussed?",
        "What does that procedure combine?",
        "What does their reviewed treatment involve?",
        "Explain the documented procedure for this patient.",
    ],
)
def test_identified_record_links_procedure_in_referential_sequence(original, question):
    packet = RetrievalService(original).evidence(question, patient_id="PT-006", mode="lexical", top_k=1)
    assert {"PT-006", "PROC-102"} <= {s.doc_id for s in packet.sources}
    assert [s.doc_id for s in packet.sources if s.category == "synthetic_patient"] == ["PT-006"]
    assert len(packet.sources) <= 3
    assert "PROC-102" in parts(packet)[0]["procedure_expansion_ids"]
    assert "PROC-102" not in packet.retrieval["raw_top_k_ids"]
    for candidate in parts(packet)[0]["procedure_reference_candidates"]:
        record = next(s for s in original.documents if s.doc_id == candidate["patient_doc_id"])
        assert record.text[candidate["start"] : candidate["end"]] == candidate["quote"]
    context = model_context(packet, question)
    assert "retrieval" not in context["evidence"]
    assert any("mention does not establish" in w for w in context["evidence"]["scopes"][0]["warnings"])


@pytest.mark.parametrize(
    "question,patient",
    [
        ("What does that procedure combine?", None),
        ("What treatment did PT-999 discuss?", "PT-006"),
        ("What treatment did PT-005 discuss?", "PT-006"),
        ("Which patient discussed RF microneedling?", None),
        ("List every patient's discussed treatments.", None),
        ("What does PT-006's record say?", None),
        ("Is PT-006's consultation complete?", None),
        ("Explain common filler effects in general.", "PT-006"),
        ("Separate issue: what does that procedure combine?", "PT-006"),
        ("New topic: what treatment was discussed?", "PT-006"),
        ("What procedure should PT-006 choose?", None),
        ("Compare the treatment discussed by PT-006 and PT-005.", None),
    ],
)
def test_no_reference_expansion_without_unambiguous_authorized_record(original, question, patient):
    packet = RetrievalService(original).evidence(question, patient_id=patient, mode="lexical", top_k=1)
    assert all(
        not p["procedure_reference_candidates"] and not p["procedure_expansion_ids"] for p in parts(packet)
    )


@pytest.mark.parametrize(
    "title,text,expected",
    [
        ("Microfocused Ultrasound (MFU) - General Overview", "The provider reviewed MFU.", True),
        (
            "Microfocused Ultrasound (MFU) - General Overview",
            "The provider reviewed microfocused-ultrasound.",
            True,
        ),
        ("IPL / Photofacial - General Overview", "A PHOTOFACIAL was discussed.", True),
        ("IPL / Photofacial - General Overview", "The provider reviewed IPL.", True),
        ("Novel Skin Treatment - General Overview", "Novel_skin treatment was mentioned.", True),
        ("RF Microneedling - General Overview", "No prior RF-microneedling was reported.", True),
        ("RF Microneedling - General Overview", "Microneedling was discussed.", False),
        ("Microfocused Ultrasound (MFU) - General Overview", "XMFUX is mentioned.", False),
        ("IPL / Photofacial - General Overview", "A triple consultation occurred.", False),
        ("Treatment - General Overview", "Treatment was discussed.", False),
        ("Skin Treatment - General Overview", "Skin treatment was discussed.", False),
        ("Laser - General Overview", "Laser was discussed.", False),
    ],
)
def test_aliases_are_exact_source_title_names_not_assumed_synonyms(tmp_path, title, text, expected):
    bundle = fixture_bundle(
        tmp_path,
        [
            source("EDU-901", title, "Educational source only."),
            source(
                "PT-105", "Record PT-105", "Patient ID: PT-105\nCurrent status: " + text, "synthetic_patient"
            ),
        ],
    )
    packet = RetrievalService(bundle).evidence(
        "What does the treatment discussed by PT-105 combine?", mode="lexical", top_k=1
    )
    candidates = parts(packet)[0]["procedure_reference_candidates"]
    assert bool(candidates) is expected
    if expected:
        assert {c["doc_id"] for c in candidates} == {"EDU-901"}
        assert "EDU-901" in packet.scopes[0].allowed_source_ids


@pytest.mark.parametrize("count", [2, 3, 4])
def test_multiple_reference_candidates_are_visible_and_bounded(tmp_path, count):
    titles = ["Microfocused Ultrasound", "Fractional Rejuvenation", "Thermal Remodeling", "Collagen Renewal"][
        :count
    ]
    records = [
        source(f"EDU-{i}", title + " - General Overview", title + " educational source.")
        for i, title in enumerate(titles)
    ]
    records.append(
        source(
            "PT-105",
            "Record PT-105",
            "Patient ID: PT-105\nCurrent status: Provider reviewed " + ", ".join(titles) + ".",
            "synthetic_patient",
        )
    )
    packet = RetrievalService(fixture_bundle(tmp_path, records)).evidence(
        "What does the treatment discussed by PT-105 combine?", mode="lexical", top_k=1
    )
    part = parts(packet)[0]
    assert {c["doc_id"] for c in part["procedure_reference_candidates"]} == {f"EDU-{i}" for i in range(count)}
    assert part["procedure_expansion_limit_exceeded"] is (count > 3)
    if count > 3:
        assert part["procedure_expansion_ids"] == []
        assert [s.doc_id for s in packet.sources] == ["PT-105"]
    else:
        assert len(part["procedure_expansion_ids"]) == count
    assert any("multiple procedure topics" in w for w in packet.scopes[0].warnings)
    assert any("selection, completion, eligibility" in w for w in packet.scopes[0].warnings)


def test_patient_identity_and_changed_source_reference_control_expansion(tmp_path):
    bundle = fixture_bundle(
        tmp_path,
        [
            source(
                "EDU-901",
                "Microfocused Ultrasound (MFU) - General Overview",
                "Educational ultrasound information.",
            ),
            source(
                "EDU-902",
                "Fractional Rejuvenation - General Overview",
                "Educational rejuvenation information.",
            ),
            source(
                "PT-105",
                "Record PT-105",
                "Patient ID: PT-105\nCurrent status: MFU discussed; no treatment selected.",
                "synthetic_patient",
            ),
            source(
                "PT-106",
                "Record PT-106",
                "Patient ID: PT-106\nCurrent status: Fractional Rejuvenation discussed.",
                "synthetic_patient",
            ),
        ],
    )
    retriever = RetrievalService(bundle)
    for patient, expected in [("PT-105", "EDU-901"), ("PT-106", "EDU-902")]:
        packet = retriever.evidence(
            f"What does the treatment discussed by {patient} combine?", mode="lexical", top_k=1
        )
        assert parts(packet)[0]["procedure_expansion_ids"] == [expected]
        assert {s.doc_id for s in packet.sources} == {patient, expected}
        assert all(c["patient_doc_id"] == patient for c in parts(packet)[0]["procedure_reference_candidates"])


@pytest.mark.parametrize(
    "date,applicable", [("2026-03-01", "OPS-306-V1"), ("2026-09-01", "OPS-306-V2"), ("2025-12-01", None)]
)
def test_policy_selection_and_historical_gaps_unchanged(original, date, applicable):
    packet = RetrievalService(original).evidence(
        "What is the cancellation policy?", patient_id="PT-006", as_of=date, mode="lexical", top_k=1
    )
    assert all(not p["procedure_reference_candidates"] for p in parts(packet))
    assert {s.doc_id for s in packet.sources if s.category == "policy"} == {"OPS-306-V1", "OPS-306-V2"}
    allowed = set(packet.scopes[0].allowed_source_ids) & {"OPS-306-V1", "OPS-306-V2"}
    assert allowed == ({applicable} if applicable else set())


@pytest.mark.parametrize("mode", ["semantic", "hybrid"])
def test_reference_expansion_survives_unhelpful_vector_ranking(mode):
    path = ROOT / "apps/api/tests/fixtures/recorded-retrieval/fts-v2-large.sqlite"
    if not path.exists():
        pytest.skip("Local recorded embedding bundle unavailable")
    retriever = RetrievalService(Bundle.from_bytes(path.read_bytes()))
    # Deliberately use an irrelevant vector: this verifies the source link independently
    # of semantic recall. It is not presented as a real query embedding or quality metric.
    vector = retriever.matrix[retriever.vector_ids.index("OPS-307")].tolist()
    packet = retriever.evidence(
        "For patient PT-006, what exact downtime in days and procedure price are documented for the treatment discussed?",
        mode=mode,
        top_k=1,
        query_vector=vector,
        query_model=retriever.bundle.config["embedding"]["model"],
    )
    assert "PROC-102" in parts(packet)[0]["procedure_expansion_ids"]
    assert [s.doc_id for s in packet.sources if s.category == "synthetic_patient"] == ["PT-006"]
    assert "PROC-102" not in packet.retrieval["raw_top_k_ids"]


def test_alias_generation_does_not_create_acronyms_or_shorten_clinical_names():
    assert [a for a, _ in procedure_title_aliases("Radiofrequency Microneedling - General Overview")] == [
        "radiofrequency microneedling"
    ]
    assert [a for a, _ in procedure_title_aliases("Dermal Filler - General Overview")] == ["dermal filler"]
