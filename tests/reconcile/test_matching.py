"""Matching order precedence tests, per the design specification section 9.1."""

from __future__ import annotations

from ragledger.reconcile.matching import (
    ExpectedPoint,
    MatchLevel,
    match_all_levels,
    normalize_point_id,
)
from tests.reconcile.builders import make_scenario


def _expected_from_scenario(scenario) -> ExpectedPoint:
    from ragledger.reconcile.matching import resolve_expected_points

    points = resolve_expected_points(
        scenario.manifest, target=scenario.target, scope=scenario.scope
    )
    assert len(points) == 1
    return points[0]


def test_level_1_point_id_beats_everything_else() -> None:
    """A point whose id matches, but whose embedding/chunk id does NOT,
    still matches at level 1 (point id is checked first)."""
    scenario = make_scenario()
    expected = _expected_from_scenario(scenario)
    observed = scenario.matching_point.model_copy(
        update={"embedding_id": "emb_something_else", "chunk_id": "chk_something_else"}
    )

    outcome = match_all_levels([expected], [observed])

    assert len(outcome.matched) == 1
    assert outcome.matched[0].level is MatchLevel.POINT_ID
    assert outcome.matched[0].confidence == "high"
    assert not outcome.missing_expected
    assert not outcome.orphan_observed


def test_level_2_embedding_id_used_when_point_id_differs() -> None:
    scenario = make_scenario()
    expected = _expected_from_scenario(scenario)
    observed = scenario.matching_point.model_copy(
        update={"point_id": "a-totally-different-point-id"}
    )

    outcome = match_all_levels([expected], [observed])

    assert len(outcome.matched) == 1
    assert outcome.matched[0].level is MatchLevel.EMBEDDING_ID
    assert outcome.matched[0].confidence == "high"


def test_level_3_chunk_id_used_when_point_and_embedding_id_differ() -> None:
    scenario = make_scenario()
    expected = _expected_from_scenario(scenario)
    observed = scenario.matching_point.model_copy(
        update={"point_id": "different-point", "embedding_id": "emb_different"}
    )

    outcome = match_all_levels([expected], [observed])

    assert len(outcome.matched) == 1
    assert outcome.matched[0].level is MatchLevel.CHUNK_ID
    assert outcome.matched[0].confidence == "high"


def test_level_4_source_version_suggestion_when_only_source_version_matches() -> None:
    """A point that only shares source_version_id (level 4) with an expected
    binding is still reported missing/orphan; section 9.1: low-confidence
    match does not auto-close, only suggests."""
    scenario = make_scenario()
    expected = _expected_from_scenario(scenario)
    # Same source_version_id, but every identity field levels 1-3 check
    # (and the payload hash level 5 checks) is different.
    observed = scenario.matching_point.model_copy(
        update={
            "point_id": "unrelated-point",
            "embedding_id": "emb_unrelated",
            "chunk_id": "chk_unrelated",
            "payload_hash": "0" * 64,
        }
    )

    outcome = match_all_levels([expected], [observed])

    assert not outcome.matched
    assert len(outcome.missing_expected) == 1
    assert len(outcome.orphan_observed) == 1
    # But a relocation suggestion IS attached (level 4: source_version_id).
    observed_key = normalize_point_id(observed.point_id)
    assert outcome.relocation_candidates.get(expected.binding_id) == [observed_key]
    assert outcome.orphan_candidates.get(observed_key) == [expected.binding_id]


def test_level_5_content_hash_suggestion_when_only_payload_hash_matches() -> None:
    """A point that only shares the payload hash (level 5) with an expected
    binding is still reported missing/orphan, with a low-confidence
    suggestion attached."""
    scenario = make_scenario()
    expected = _expected_from_scenario(scenario)
    observed = scenario.matching_point.model_copy(
        update={
            "point_id": "unrelated-point",
            "embedding_id": "emb_unrelated",
            "chunk_id": "chk_unrelated",
            "source_version_id": "ver_" + "0" * 52,
        }
    )

    outcome = match_all_levels([expected], [observed])

    assert not outcome.matched
    assert len(outcome.missing_expected) == 1
    assert len(outcome.orphan_observed) == 1
    observed_key = normalize_point_id(observed.point_id)
    assert outcome.relocation_candidates.get(expected.binding_id) == [observed_key]


def test_no_shared_identity_produces_missing_and_orphan_with_no_suggestion() -> None:
    scenario = make_scenario()
    expected = _expected_from_scenario(scenario)
    other = make_scenario(uri="file:documents/unrelated.md", point_id="other-point")
    observed = other.matching_point

    outcome = match_all_levels([expected], [observed])

    assert not outcome.matched
    assert len(outcome.missing_expected) == 1
    assert len(outcome.orphan_observed) == 1
    assert expected.binding_id not in outcome.relocation_candidates


def test_duplicate_point_id_reported_as_duplicate_group() -> None:
    scenario = make_scenario()
    expected = _expected_from_scenario(scenario)
    duplicate_observed = scenario.matching_point.model_copy(
        update={"chunk_id": "chk_other", "embedding_id": "emb_other"}
    )

    outcome = match_all_levels([expected], [scenario.matching_point, duplicate_observed])

    # One point_id-level match consumes the pair; the extra observed point
    # with the SAME point_id becomes a leftover AND a duplicate group.
    assert len(outcome.matched) == 1
    assert len(outcome.orphan_observed) == 1
    assert any(
        level is MatchLevel.POINT_ID for level, _key, _group in outcome.duplicate_observed_groups
    )


def test_matching_is_order_independent_for_disjoint_scenarios() -> None:
    """Two unrelated scenarios reconciled together match independently,
    regardless of input list order."""
    first = make_scenario(uri="file:documents/a.md", point_id="point-a")
    second = make_scenario(uri="file:documents/b.md", point_id="point-b")
    expected = [_expected_from_scenario(first), _expected_from_scenario(second)]
    observed = [second.matching_point, first.matching_point]

    outcome = match_all_levels(expected, observed)

    assert len(outcome.matched) == 2
    assert not outcome.missing_expected
    assert not outcome.orphan_observed
