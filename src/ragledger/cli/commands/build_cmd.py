"""`ragledger build`, per the design specification section 17.1 and 10.2.

Runs the full discover -> parse -> chunk -> scan -> embed -> manifest
pipeline (`ragledger.pipeline.build.build_pipeline`) against a
`ragledger.yml` config and writes a canonical manifest.

Exit code interpretation (the design specification section 17.1's table has no
row specifically for "build produced an incomplete manifest"): a
config/input error (bad YAML, unknown chunker, missing model-revisions
lock) is `1`; a successful `complete` build is `0`; a `build.status ==
"incomplete"` manifest (one or more sources failed to parse) is `3`
("Policy fail"), matching section 10.2's own words -- "Partial build
manifest üretilebilir fakat status incomplete; policy default fail" --
unless the caller passes `--allow-incomplete`, in which case the
partial manifest is still written and the command exits `0`.
"""

from __future__ import annotations

from pathlib import Path

import typer

from ragledger.cli._build_support import (
    build_config_from_ragledger_config,
    resolve_epoch,
    resolve_timing,
)
from ragledger.cli._config import ConfigError, load_config
from ragledger.cli._exit import EXIT_CONFIG_ERROR, EXIT_POLICY_FAIL, CliError, run_command
from ragledger.cli._output import log
from ragledger.core.artifacts import ArtifactStore
from ragledger.core.manifest import write_manifest
from ragledger.pipeline.build import build_pipeline
from ragledger.pipeline.cache import StageCache
from ragledger.pipeline.discovery import DiscoveryError


def build(
    path: Path = typer.Argument(..., help="Source root directory to build from."),  # noqa: B008
    config: Path = typer.Option(  # noqa: B008
        Path("ragledger.yml"), "--config", help="Path to ragledger.yml."
    ),
    output: Path = typer.Option(  # noqa: B008
        Path("manifest.json"), "--output", help="Where to write the manifest."
    ),
    artifacts_dir: Path = typer.Option(  # noqa: B008
        Path(".ragledger/artifacts"),
        "--artifacts",
        help="Content-addressed artifact store directory.",
    ),
    cache_dir: Path = typer.Option(  # noqa: B008
        Path(".ragledger/cache"), "--cache", help="Pipeline stage cache directory."
    ),
    epoch: int | None = typer.Option(
        None,
        "--epoch",
        help=(
            "Unix timestamp for created_at/build_id; falls back to SOURCE_DATE_EPOCH. "
            "Fixing this (and the source tree/config) is what makes two builds byte-identical."
        ),
    ),
    reproducible: bool | None = typer.Option(
        None,
        "--reproducible/--no-reproducible",
        help=(
            "Force reproducible mode (fixed parse duration_seconds=0.0). Defaults to true "
            "when an epoch is resolved, false (honest wall-clock timing) otherwise."
        ),
    ),
    allow_incomplete: bool = typer.Option(
        False,
        "--allow-incomplete",
        help="Exit 0 even if the build status is 'incomplete' (parse failures occurred).",
    ),
) -> None:
    """Run discover -> parse -> chunk -> scan -> embed -> manifest and write the result."""
    run_command(
        lambda: _build_impl(
            path, config, output, artifacts_dir, cache_dir, epoch, reproducible, allow_incomplete
        )
    )


def _build_impl(
    path: Path,
    config_path: Path,
    output: Path,
    artifacts_dir: Path,
    cache_dir: Path,
    epoch: int | None,
    reproducible_flag: bool | None,
    allow_incomplete: bool,
) -> None:
    if not path.is_dir():
        raise CliError(f"source root is not a directory: {path}", exit_code=EXIT_CONFIG_ERROR)

    try:
        cfg = load_config(config_path)
    except ConfigError as exc:
        raise CliError(str(exc), exit_code=EXIT_CONFIG_ERROR) from exc

    try:
        resolved_epoch = resolve_epoch(epoch)
    except ConfigError as exc:
        raise CliError(str(exc), exit_code=EXIT_CONFIG_ERROR) from exc
    timing = resolve_timing(resolved_epoch, force_reproducible=reproducible_flag)

    try:
        build_config = build_config_from_ragledger_config(
            cfg,
            root=path.resolve(),
            config_dir=config_path.resolve().parent,
            build_id=timing.build_id,
            created_at=timing.created_at,
            reproducible=timing.reproducible,
            log=log,
        )
    except ConfigError as exc:
        raise CliError(str(exc), exit_code=EXIT_CONFIG_ERROR) from exc

    log(
        f"building namespace={cfg.namespace!r} root={path} build_id={timing.build_id} "
        f"reproducible={timing.reproducible}"
    )
    store = ArtifactStore(artifacts_dir.resolve())
    cache = StageCache(cache_dir.resolve())
    try:
        manifest = build_pipeline(build_config, store, cache)
    except DiscoveryError as exc:
        raise CliError(f"source discovery failed: {exc}", exit_code=EXIT_CONFIG_ERROR) from exc

    output.parent.mkdir(parents=True, exist_ok=True)
    write_manifest(output, manifest)

    log(
        f"wrote {output}: sources={manifest.statistics.source_count} "
        f"chunks={manifest.statistics.chunk_count} "
        f"embeddings={manifest.statistics.embedding_count} "
        f"assertions={manifest.statistics.assertion_count} "
        f"warnings={manifest.statistics.warning_count} status={manifest.build.status} "
        f"manifest_hash={manifest.integrity.manifest_hash}"
    )

    if manifest.build.status == "incomplete" and not allow_incomplete:
        raise CliError(
            f"{output}: build status is 'incomplete' (one or more sources failed to parse); "
            "the manifest was still written -- pass --allow-incomplete to accept it as exit 0",
            exit_code=EXIT_POLICY_FAIL,
        )
