"""Small-data vs big-data path equivalence, per PROJECT_SPEC.md section 14.

Both `reconcile_small_data` and `reconcile_big_data` are built on the exact
same `ragledger.reconcile.matching.stream_merge_join` primitive (see that
module's docstring); this test drives the same synthetic dataset -- a mix of
clean matches, staleness, ACL leaks, missing bindings, and orphan points --
through both paths and asserts they produce identical findings, ratios, and
summaries.
"""

from __future__ import annotations

from pathlib import Path

from tests.reconcile.builders import SCOPE, TARGET, list_connector, make_bulk_dataset

from ragledger.reconcile.engine import reconcile_big_data, reconcile_small_data
from ragledger.reconcile.report import ReconciliationResult


def _fingerprint_severity_pairs(result: ReconciliationResult) -> list[tuple[str, str]]:
    return sorted((finding.fingerprint, finding.severity.value) for finding in result.findings)


def test_small_and_big_paths_agree_on_a_mixed_dataset(tmp_path: Path) -> None:
    manifest, points = make_bulk_dataset(
        600, orphan_count=15, missing_count=15, stale_every=20, acl_leak_every=13
    )

    small_connector = list_connector(points, vector_dimensions={"default": 4})
    small_result = reconcile_small_data(manifest, small_connector, target=TARGET, scope=SCOPE)

    big_connector = list_connector(points, vector_dimensions={"default": 4})
    big_result = reconcile_big_data(
        manifest,
        big_connector,
        target=TARGET,
        scope=SCOPE,
        work_dir=tmp_path / "work",
        chunk_size=37,
    )

    assert _fingerprint_severity_pairs(small_result) == _fingerprint_severity_pairs(big_result)
    assert small_result.summary.model_dump(
        exclude={"manifest_signed"}
    ) == big_result.summary.model_dump(exclude={"manifest_signed"})
    assert small_result.ratios == big_result.ratios


def test_small_and_big_paths_agree_with_no_mismatches(tmp_path: Path) -> None:
    manifest, points = make_bulk_dataset(250)

    small_connector = list_connector(points, vector_dimensions={"default": 4})
    small_result = reconcile_small_data(manifest, small_connector, target=TARGET, scope=SCOPE)

    big_connector = list_connector(points, vector_dimensions={"default": 4})
    big_result = reconcile_big_data(
        manifest,
        big_connector,
        target=TARGET,
        scope=SCOPE,
        work_dir=tmp_path / "work",
        chunk_size=64,
    )

    assert small_result.findings == []
    assert big_result.findings == []
    assert small_result.ratios == big_result.ratios


def test_small_and_big_paths_agree_on_duplicate_point_ids(tmp_path: Path) -> None:
    manifest, points = make_bulk_dataset(100)
    # Introduce a duplicate observed point id by cloning one point with a
    # different chunk/embedding id (an observed-side identity collision).
    duplicate_source = points[10]
    duplicate = duplicate_source.model_copy(
        update={"chunk_id": "chk_duplicate_variant", "embedding_id": "emb_duplicate_variant"}
    )
    points_with_duplicate = [*points, duplicate]

    small_connector = list_connector(points_with_duplicate, vector_dimensions={"default": 4})
    small_result = reconcile_small_data(manifest, small_connector, target=TARGET, scope=SCOPE)

    big_connector = list_connector(points_with_duplicate, vector_dimensions={"default": 4})
    big_result = reconcile_big_data(
        manifest,
        big_connector,
        target=TARGET,
        scope=SCOPE,
        work_dir=tmp_path / "work",
        chunk_size=17,
    )

    assert _fingerprint_severity_pairs(small_result) == _fingerprint_severity_pairs(big_result)
    assert any(f.code.value == "DUPLICATE_POINT_ID" for f in small_result.findings)
