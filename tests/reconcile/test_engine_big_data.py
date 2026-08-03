"""Big-data (external sort/merge) engine tests: scale, and cancel/restart
idempotence, per the design specification section 14.2 and acceptance scenario H.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from ragledger.reconcile import engine
from ragledger.reconcile.engine import reconcile_big_data
from ragledger.reconcile.taxonomy import FindingCode
from tests.reconcile.builders import SCOPE, TARGET, list_connector, make_bulk_dataset


def test_100k_points_bounded_memory_path_completes_within_budget(tmp_path: Path) -> None:
    """Acceptance scenario H: bounded-memory reconciliation over a
    100k+-point synthetic dataset, generated in-stream, well under the
    30-second runtime budget.
    """
    manifest, points = make_bulk_dataset(
        100_000, orphan_count=50, missing_count=50, stale_every=1000, acl_leak_every=500
    )
    connector = list_connector(points, vector_dimensions={"default": 4})

    started = time.monotonic()
    result = reconcile_big_data(
        manifest,
        connector,
        target=TARGET,
        scope=SCOPE,
        work_dir=tmp_path / "work",
        chunk_size=20_000,
    )
    elapsed = time.monotonic() - started

    assert elapsed < 30.0, f"reconcile_big_data took {elapsed:.1f}s, exceeding the 30s budget"
    assert result.summary.expected_bindings == 100_050
    assert result.summary.observed_points == 100_050
    assert result.summary.matched_points == 100_000
    assert len(result.findings) > 0
    assert result.ratios.missing_ratio == pytest.approx(50 / 100_050)
    assert result.ratios.orphan_ratio == pytest.approx(50 / 100_050)
    stale_findings = [f for f in result.findings if f.code is FindingCode.STALE_SOURCE]
    assert len(stale_findings) == 100  # every 1000th of 100_000 matched points
    acl_findings = [f for f in result.findings if f.code is FindingCode.ACL_BROADER_THAN_SOURCE]
    assert len(acl_findings) == 200  # every 500th of 100_000 matched points


def test_work_dir_is_empty_after_a_successful_big_data_run(tmp_path: Path) -> None:
    manifest, points = make_bulk_dataset(200, orphan_count=5, missing_count=5)
    connector = list_connector(points, vector_dimensions={"default": 4})
    work_dir = tmp_path / "work"

    reconcile_big_data(
        manifest, connector, target=TARGET, scope=SCOPE, work_dir=work_dir, chunk_size=50
    )

    assert work_dir.exists()
    assert list(work_dir.iterdir()) == []


def test_cancel_restart_idempotence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A simulated process kill mid-merge leaves spill files behind; a
    rerun against the SAME work directory cleans them up (idempotent
    restart) and produces a correct result -- section 14.2's "cleanup
    cancel/failure".
    """
    manifest, points = make_bulk_dataset(80, orphan_count=4, missing_count=4)
    work_dir = tmp_path / "work"

    real_stream_merge_join = engine.matching.stream_merge_join
    calls = {"count": 0}

    def flaky_stream_merge_join(*args: object, **kwargs: object) -> None:
        calls["count"] += 1
        if calls["count"] == 2:
            raise RuntimeError("simulated process kill mid-merge")
        return real_stream_merge_join(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(engine.matching, "stream_merge_join", flaky_stream_merge_join)

    connector = list_connector(points, vector_dimensions={"default": 4})
    with pytest.raises(RuntimeError, match="simulated process kill mid-merge"):
        reconcile_big_data(
            manifest, connector, target=TARGET, scope=SCOPE, work_dir=work_dir, chunk_size=10
        )

    # The crash happened mid-round-2: round-2 spill files are left on disk,
    # not cleaned up, because cleanup only ever happens at the START of the
    # next attempt or the END of a successful one.
    leftover = list(work_dir.iterdir())
    assert leftover, "expected the simulated crash to leave spill files behind"

    monkeypatch.undo()

    connector_retry = list_connector(points, vector_dimensions={"default": 4})
    result = reconcile_big_data(
        manifest, connector_retry, target=TARGET, scope=SCOPE, work_dir=work_dir, chunk_size=10
    )

    assert result.summary.matched_points == 80
    assert result.summary.expected_bindings == 84
    assert result.summary.observed_points == 84
    assert list(work_dir.iterdir()) == []  # cleaned up after the successful rerun


def test_reset_work_dir_creates_directory_with_restrictive_permissions(tmp_path: Path) -> None:
    work_dir = tmp_path / "fresh-work-dir"
    assert not work_dir.exists()
    engine._reset_work_dir(work_dir)
    assert work_dir.exists()
    assert oct(work_dir.stat().st_mode)[-3:] == "700"
