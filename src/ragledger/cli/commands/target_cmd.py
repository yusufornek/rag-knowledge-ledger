"""`ragledger target add`, per the design specification section 17.1 and 35.

Validates a target config file against the declared type's schema, then
(by default) runs `ragledger.connectors.config.run_preflight` against
the live target: reachability, auth, and -- when `--expected-dimension`
is given -- an embedding-dimension-vs-collection-dimension check. Pass
`--no-check` to validate the config file's shape only, with no network
call (used by this release's own tests, which must not depend on a
live Qdrant/pgvector service).
"""

from __future__ import annotations

from pathlib import Path

import typer

from ragledger.cli._exit import EXIT_CONFIG_ERROR, EXIT_TARGET_FAILURE, CliError, run_command
from ragledger.cli._output import log
from ragledger.cli._target import TargetConfigError, build_connector, load_target_config
from ragledger.connectors.base import ConnectorConfigError
from ragledger.connectors.config import run_preflight

app = typer.Typer(help="Manage vector target configurations.", no_args_is_help=True)

_ADDABLE_TYPES = ("qdrant", "pgvector")


@app.command("add")
def add(
    target_type: str = typer.Argument(..., help="Target type: qdrant or pgvector."),
    config: Path = typer.Option(  # noqa: B008
        ..., "--config", help="Path to the target's YAML config file."
    ),
    check: bool = typer.Option(
        True,
        "--check/--no-check",
        help="Run reachability/auth/dimension preflight after validating the config shape.",
    ),
    expected_dimension: int | None = typer.Option(
        None,
        "--expected-dimension",
        help="Expected embedding dimension to check the target's vector field against.",
    ),
) -> None:
    """Validate --config against the target type's schema, then (by default) preflight-check it."""
    run_command(lambda: _add_impl(target_type, config, check, expected_dimension))


def _add_impl(
    target_type: str, config_path: Path, check: bool, expected_dimension: int | None
) -> None:
    if target_type not in _ADDABLE_TYPES:
        raise CliError(
            f"unsupported target type {target_type!r}; expected one of {_ADDABLE_TYPES}",
            exit_code=EXIT_CONFIG_ERROR,
        )
    try:
        target_config = load_target_config(config_path)
    except TargetConfigError as exc:
        raise CliError(str(exc), exit_code=EXIT_CONFIG_ERROR) from exc
    if target_config.type != target_type:
        raise CliError(
            f"{config_path} declares type {target_config.type!r}, expected {target_type!r}",
            exit_code=EXIT_CONFIG_ERROR,
        )
    log(f"{config_path}: valid {target_type} target configuration")
    if not check:
        return

    connector = build_connector(target_config)
    try:
        try:
            connector.validate_configuration()
        except ConnectorConfigError as exc:
            raise CliError(
                f"target configuration invalid: {exc}", exit_code=EXIT_CONFIG_ERROR
            ) from exc
        result = run_preflight(connector, expected_dimension=expected_dimension)
    finally:
        connector.close()

    log(f"reachable={result.reachable} auth_ok={result.auth_ok} message={result.message}")
    if result.schema is not None:
        names = ", ".join(
            f"{field.name}:{field.dimension}" for field in result.schema.vector_fields
        )
        log(f"vector fields: {names or '(none)'}")
    if expected_dimension is not None:
        log(
            f"expected_dimension={expected_dimension} "
            f"observed_dimension={result.observed_dimension} "
            f"dimension_match={result.dimension_match}"
        )

    if not result.reachable or not result.auth_ok:
        raise CliError(f"preflight failed: {result.message}", exit_code=EXIT_TARGET_FAILURE)
    if expected_dimension is not None and result.dimension_match is False:
        raise CliError(f"preflight failed: {result.message}", exit_code=EXIT_TARGET_FAILURE)
