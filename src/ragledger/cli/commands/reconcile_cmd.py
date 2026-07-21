"""`ragledger reconcile`, per PROJECT_SPEC.md section 17.1 and `ragledger.reconcile`.

Wires the reconciliation/policy/remediation engine (`ragledger.reconcile.engine`,
`.policy`, `.remediation`, `.report`) into the CLI: reads a manifest and an
already-captured `.ndjson.zst` snapshot (see `ragledger snapshot`), reconciles
them, evaluates an optional policy v1 document, and prints a plain-text CI
summary to stdout (`ragledger.reconcile.report.render_ci_summary`) -- the same
one a CI pipeline would grep for `exit_code=`/`verdict=`. `--output`/`--html`
additionally write the full canonical-JSON or self-contained-HTML report to
a file; neither is required.

Target/scope are not separate CLI flags: they come straight from the
snapshot's own header (`NdjsonConnector.inspect_target_schema()`), i.e.
whatever `ragledger snapshot` recorded when the snapshot was captured. A
snapshot always describes exactly one (target, scope) pass, so there is
nothing for a flag to disambiguate.

Exit codes deliberately do NOT reuse `ragledger.cli._exit`'s generic
section-17.1 table: `ragledger.reconcile.report` already defines its own
three-value scheme (`EXIT_PASS=0`, `EXIT_POLICY_FAIL=1`,
`EXIT_EXECUTION_ERROR=2`), documented there as matching
`RECONCILIATION_INCONCLUSIVE` in section 37.6's error-code list. This
command reuses that scheme verbatim rather than forcing reconciliation's
PASS/WARN/FAIL/INCONCLUSIVE verdict into the six-value generic table: `0`
for a PASS or WARN verdict (or when no `--policy` was given at all -- an
unevaluated policy can never fail), `1` for FAIL, and `2` for every
input/config/connectivity problem this command anticipates (a missing or
invalid manifest/snapshot/policy file, an unreadable snapshot, a target
schema no expected embedding dimension can possibly match) as well as an
INCONCLUSIVE policy verdict, matching `exit_code_for`'s own "execution-class
problem, not a policy decision" reasoning for that verdict. An entirely
unanticipated exception still falls through `run_command`'s last-resort
boundary to exit `6`, never a silent success or a bare traceback.
"""

from __future__ import annotations

import contextlib
import tempfile
from pathlib import Path
from typing import Literal

import typer
import yaml
from pydantic import ValidationError

from ragledger.cli._exit import CliError, run_command
from ragledger.cli._output import emit_text, log
from ragledger.connectors.base import ConnectorConfigError, ConnectorConnectionError
from ragledger.connectors.ndjson import NdjsonConnector, SnapshotIntegrityError
from ragledger.core.manifest import load_manifest
from ragledger.reconcile.engine import reconcile_big_data, reconcile_small_data
from ragledger.reconcile.policy import (
    PolicyDocument,
    PolicyValidationError,
    evaluate_policy,
    load_policy_document,
)
from ragledger.reconcile.remediation import build_remediation_plan
from ragledger.reconcile.report import (
    EXIT_EXECUTION_ERROR,
    EXIT_PASS,
    PolicyVerdict,
    ReconciliationReport,
    exit_code_for,
    render_ci_summary,
    to_json_bytes,
)
from ragledger.reports.reconciliation_report import render_reconciliation_report_html

_NO_POLICY_NAME = "(none)"
"""`PolicyVerdict.policy_name` used when `--policy` is omitted -- an
unevaluated policy always PASSes (per this module's docstring), so the
report still carries a well-formed, always-present `PolicyVerdict` rather
than an optional field every downstream reader would need to null-check."""

_SMALL_DATA_MAX_POINTS = 100_000
"""Mirrors `ragledger.reconcile.engine.reconcile_small_data`'s own default
`max_in_memory_points`: the point count above which `--auto` (the default)
falls back to the big-data external-merge path."""


def reconcile(
    manifest: Path = typer.Argument(  # noqa: B008
        ..., help="Path to the manifest JSON file."
    ),
    snapshot: Path = typer.Argument(  # noqa: B008
        ..., help="Path to the .ndjson.zst snapshot file (see `ragledger snapshot`)."
    ),
    policy: Path | None = typer.Option(  # noqa: B008
        None, "--policy", help="Path to a policy v1 YAML/JSON document."
    ),
    output: Path | None = typer.Option(  # noqa: B008
        None, "--output", help="Where to write the canonical-JSON reconciliation report."
    ),
    html: Path | None = typer.Option(  # noqa: B008
        None, "--html", help="Where to write a self-contained HTML reconciliation report."
    ),
    work_dir: Path | None = typer.Option(  # noqa: B008
        None,
        "--work-dir",
        help="Scratch directory for the big-data external-merge path "
        "(default: a fresh temp directory, used only if that path runs).",
    ),
    big_data: bool = typer.Option(
        False,
        "--big-data/--auto",
        help="Force the big-data external-merge path instead of auto-detecting it.",
    ),
) -> None:
    """Reconcile a manifest's expected index state against an observed snapshot."""
    run_command(
        lambda: _reconcile_impl(manifest, snapshot, policy, output, html, work_dir, big_data)
    )


def _policy_format(path: Path) -> Literal["yaml", "json"]:
    return "json" if path.suffix.lower() == ".json" else "yaml"


def _load_policy(policy_path: Path | None) -> PolicyDocument | None:
    if policy_path is None:
        return None
    try:
        text = policy_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CliError(
            f"cannot read policy {policy_path}: {exc}", exit_code=EXIT_EXECUTION_ERROR
        ) from exc
    try:
        return load_policy_document(text, document_format=_policy_format(policy_path))
    except (PolicyValidationError, yaml.YAMLError, ValueError, ValidationError) as exc:
        raise CliError(
            f"{policy_path}: invalid policy document: {exc}", exit_code=EXIT_EXECUTION_ERROR
        ) from exc


def _load_snapshot_connector(snapshot_path: Path) -> NdjsonConnector:
    connector = NdjsonConnector(snapshot_path)
    try:
        connector.validate_configuration()
    except ConnectorConfigError as exc:
        raise CliError(str(exc), exit_code=EXIT_EXECUTION_ERROR) from exc
    test_result = connector.test_connection()
    if not test_result.ok:
        raise CliError(
            f"cannot read snapshot {snapshot_path}: {test_result.message}",
            exit_code=EXIT_EXECUTION_ERROR,
        )
    return connector


def _resolve_work_dir(work_dir: Path | None) -> tuple[Path, bool]:
    if work_dir is not None:
        return work_dir, False
    return Path(tempfile.mkdtemp(prefix="ragledger-reconcile-")), True


def _reconcile_impl(
    manifest_path: Path,
    snapshot_path: Path,
    policy_path: Path | None,
    output: Path | None,
    html: Path | None,
    work_dir: Path | None,
    force_big_data: bool,
) -> None:
    try:
        envelope = load_manifest(manifest_path)
    except (OSError, ValueError) as exc:
        raise CliError(
            f"cannot load manifest {manifest_path}: {exc}", exit_code=EXIT_EXECUTION_ERROR
        ) from exc

    policy_document = _load_policy(policy_path)

    connector = _load_snapshot_connector(snapshot_path)
    try:
        try:
            schema = connector.inspect_target_schema()
        except (ConnectorConnectionError, SnapshotIntegrityError) as exc:
            raise CliError(
                f"cannot read snapshot header {snapshot_path}: {exc}",
                exit_code=EXIT_EXECUTION_ERROR,
            ) from exc
        target, scope = schema.target_id, schema.scope

        created_work_dir: Path | None = None
        try:
            if force_big_data:
                resolved_work_dir, created = _resolve_work_dir(work_dir)
                created_work_dir = resolved_work_dir if created else None
                result = reconcile_big_data(
                    envelope,
                    connector,
                    target=target,
                    scope=scope,
                    work_dir=resolved_work_dir,
                    policy=policy_document,
                )
            else:
                try:
                    result = reconcile_small_data(
                        envelope,
                        connector,
                        target=target,
                        scope=scope,
                        policy=policy_document,
                        max_in_memory_points=_SMALL_DATA_MAX_POINTS,
                    )
                except ValueError:
                    log(
                        f"snapshot exceeds {_SMALL_DATA_MAX_POINTS} points in memory; "
                        "falling back to the big-data external-merge path"
                    )
                    resolved_work_dir, created = _resolve_work_dir(work_dir)
                    created_work_dir = resolved_work_dir if created else None
                    result = reconcile_big_data(
                        envelope,
                        connector,
                        target=target,
                        scope=scope,
                        work_dir=resolved_work_dir,
                        policy=policy_document,
                    )
        except (ConnectorConnectionError, SnapshotIntegrityError, OSError) as exc:
            raise CliError(f"reconciliation failed: {exc}", exit_code=EXIT_EXECUTION_ERROR) from exc
        finally:
            if created_work_dir is not None:
                with contextlib.suppress(OSError):
                    created_work_dir.rmdir()
    finally:
        connector.close()

    if policy_document is not None:
        policy_verdict = evaluate_policy(policy_document, result)
    else:
        policy_verdict = PolicyVerdict(policy_name=_NO_POLICY_NAME, verdict="PASS", rule_results=[])

    remediation_plan = build_remediation_plan(result.findings)
    report = ReconciliationReport(
        result=result, policy=policy_verdict, remediation=remediation_plan
    )

    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(to_json_bytes(report))
        log(f"wrote {output}")
    if html is not None:
        html.parent.mkdir(parents=True, exist_ok=True)
        html.write_text(render_reconciliation_report_html(report), encoding="utf-8")
        log(f"wrote {html}")

    emit_text(render_ci_summary(report).rstrip("\n"))

    exit_code = exit_code_for(report)
    if exit_code != EXIT_PASS:
        raise CliError(
            f"reconciliation policy verdict {policy_verdict.verdict}", exit_code=exit_code
        )
