"""Tests for `ragledger.connectors.base`: `NormalizedPoint` and its helpers."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from ragledger.connectors.base import (
    NormalizedPoint,
    apply_projection,
    compute_payload_hash,
    hash_vector,
)
from ragledger.core.hashing import hash_canonical

OBSERVED_AT = datetime(2026, 1, 1, tzinfo=UTC)


def _make_point(**overrides: object) -> NormalizedPoint:
    fields: dict[str, object] = {
        "target_id": "support_kb",
        "scope": "support_kb",
        "point_id": "pt-1",
        "vector_names": ["dense"],
        "vector_dimensions": {"dense": 3},
        "payload_projection": {
            "source_id": "src_a",
            "chunk_id": "chk_a",
            "tenant": "acme",
            "acl": ["group:support"],
        },
        "payload_hash": compute_payload_hash(
            {
                "source_id": "src_a",
                "chunk_id": "chk_a",
                "tenant": "acme",
                "acl": ["group:support"],
            }
        ),
        "source_id": "src_a",
        "chunk_id": "chk_a",
        "tenant": "acme",
        "acl": ["group:support"],
        "observed_at": OBSERVED_AT,
        "raw_locator": "qdrant:support_kb#pt-1",
    }
    fields.update(overrides)
    return NormalizedPoint.model_validate(fields)


def test_normalized_point_round_trips_through_json() -> None:
    point = _make_point()
    dumped = point.model_dump(mode="json", exclude_none=True)
    restored = NormalizedPoint.model_validate(dumped)
    assert restored == point


def test_normalized_point_rejects_unknown_field() -> None:
    with pytest.raises(ValidationError):
        _make_point(unexpected_field="nope")


def test_normalized_point_accepts_composite_point_id() -> None:
    point = _make_point(point_id={"id": 1, "tenant": "acme"})
    assert point.point_id == {"id": 1, "tenant": "acme"}


def test_normalized_point_defaults_are_independent_between_instances() -> None:
    a = _make_point()
    b = _make_point()
    a.normalization_warnings.append("mutated")
    assert b.normalization_warnings == []


def test_hash_vector_matches_hash_canonical_of_float_array() -> None:
    components = [0.5, -1.0, 2.25]
    assert hash_vector(components) == hash_canonical([0.5, -1.0, 2.25])


def test_hash_vector_coerces_int_components_to_float() -> None:
    assert hash_vector([1, 2, 3]) == hash_vector([1.0, 2.0, 3.0])


def test_compute_payload_hash_matches_hash_canonical() -> None:
    projection = {"source_id": "src_a", "tenant": "acme"}
    assert compute_payload_hash(projection) == hash_canonical(projection)


def test_apply_projection_none_returns_same_object() -> None:
    point = _make_point()
    assert apply_projection(point, None) is point


def test_apply_projection_restricts_fields_and_recomputes_hash() -> None:
    point = _make_point()
    restricted = apply_projection(point, ["source_id"])
    assert restricted.payload_projection == {"source_id": "src_a"}
    assert restricted.payload_hash == compute_payload_hash({"source_id": "src_a"})
    assert restricted.chunk_id is None
    assert restricted.tenant is None
    assert restricted.acl is None
    assert restricted.source_id == "src_a"


def test_apply_projection_with_empty_list_clears_projection() -> None:
    point = _make_point()
    restricted = apply_projection(point, [])
    assert restricted.payload_projection == {}
    assert restricted.payload_hash == compute_payload_hash({})
    assert restricted.source_id is None
