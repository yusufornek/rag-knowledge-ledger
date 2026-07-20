"""Qdrant REST connector, per PROJECT_SPEC.md sections 8.11, 13.3, and 35.1.

Talks to Qdrant's HTTP API directly with `httpx` (no `qdrant-client`
dependency). Every request this connector issues goes through a single
`httpx.Client` whose ``request`` event hook (`_guard_request`) is the
section 42.2 mutation guard: it whitelists the exact small set of read
endpoints this module calls (`GET /collections/{name}`, `GET /aliases`,
and `POST /collections/{name}/points/scroll`, which despite the POST
verb is Qdrant's read-only paginated query operation) and raises
`ConnectorMutationBlockedError` for anything else -- any other verb,
or a POST to any other path -- before the request is ever sent over
the wire. There is no method on `QdrantConnector` that could construct
a write request in the first place; the guard exists as the section
42.2 runtime backstop the milestone requires proof of.

Consistency (section 13.3): Qdrant's scroll API gives no point-in-time
snapshot guarantee, so every pass is `ConsistencyMode.BEST_EFFORT_LIVE`.
To detect (not prevent) a concurrent mutation, `iterate_points` records
the collection's `points_count` before the first scroll page and again
after the last, and marks the pass `SnapshotCompleteness.INCOMPLETE`
when the two disagree.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

import httpx

from ragledger.connectors.base import (
    Checkpoint,
    ConnectionTestResult,
    ConnectorCapabilities,
    ConnectorConfigError,
    ConnectorConnectionError,
    ConnectorMutationBlockedError,
    ConsistencyInfo,
    ConsistencyMode,
    NormalizedPoint,
    SnapshotCompleteness,
    TargetSchema,
    VectorFieldSchema,
    VectorTargetConnector,
    apply_projection,
    compute_payload_hash,
    hash_vector,
)
from ragledger.connectors.config import QdrantTargetConfig
from ragledger.core.models import PointId

__all__ = ["QdrantConnector"]

_USER_AGENT = "ragledger-connectors-qdrant/1"

# --------------------------------------------------------------------------
# Section 42.2 mutation guard
# --------------------------------------------------------------------------

_COLLECTION_INFO_PATH_RE = re.compile(r"^/collections/[^/]+$")
_SCROLL_PATH_RE = re.compile(r"^/collections/[^/]+/points/scroll$")
_ALIASES_PATH = "/aliases"
_ROOT_PATHS = ("", "/")


def _is_allowed_request(method: str, path: str) -> bool:
    """The exact read-only whitelist this connector is allowed to issue.

    Anything not matching one of these three shapes -- regardless of
    how plausible-looking the path is -- is rejected. In particular
    every HTTP verb other than GET and the one specific scroll POST is
    always rejected, so an accidental or adversarial PUT/DELETE/PATCH
    to any Qdrant endpoint never reaches the transport.
    """
    if method == "GET":
        return bool(_COLLECTION_INFO_PATH_RE.match(path)) or path in (
            _ALIASES_PATH,
            *_ROOT_PATHS,
        )
    if method == "POST":
        return bool(_SCROLL_PATH_RE.match(path))
    return False


def _guard_request(request: httpx.Request) -> None:
    """The `httpx` request event hook enforcing `_is_allowed_request`.

    Registered as `event_hooks={"request": [_guard_request]}` on the
    client: `httpx` calls request hooks before dispatching through the
    transport, so raising here means the blocked request is never
    actually sent.
    """
    path = request.url.path
    if not _is_allowed_request(request.method, path):
        raise ConnectorMutationBlockedError(
            f"blocked non-read Qdrant request: {request.method} {path}"
        )


# --------------------------------------------------------------------------
# Raw record helpers
# --------------------------------------------------------------------------


def _coerce_point_id(value: Any) -> PointId:
    if isinstance(value, str | int):
        return value
    return str(value)


def _resolve_payload_path(payload: Mapping[str, Any], path: str) -> Any:
    current: Any = payload
    for segment in path.split("."):
        if not isinstance(current, Mapping) or segment not in current:
            return None
        current = current[segment]
    return current


def _coerce_acl(value: Any, warnings: list[str]) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    warnings.append("acl_not_list")
    return [str(value)]


def _is_float_sequence(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, int | float) for item in value)


def _hash_raw_vector(raw_vector: Any, vector_name: str | None) -> dict[str, str] | None:
    if _is_float_sequence(raw_vector):
        key = vector_name or "default"
        return {key: hash_vector([float(item) for item in raw_vector])}
    if isinstance(raw_vector, dict):
        hashes = {
            name: hash_vector([float(item) for item in components])
            for name, components in raw_vector.items()
            if _is_float_sequence(components)
        }
        return hashes or None
    return None


def _parse_vector_fields(vectors_config: Any) -> tuple[VectorFieldSchema, ...]:
    if isinstance(vectors_config, dict) and "size" in vectors_config:
        return (
            VectorFieldSchema(
                name="",
                dimension=int(vectors_config["size"]),
                distance=vectors_config.get("distance"),
            ),
        )
    if isinstance(vectors_config, dict):
        fields = []
        for name, params in vectors_config.items():
            if isinstance(params, dict) and "size" in params:
                fields.append(
                    VectorFieldSchema(
                        name=name,
                        dimension=int(params["size"]),
                        distance=params.get("distance"),
                    )
                )
        return tuple(fields)
    return ()


# --------------------------------------------------------------------------
# Connector
# --------------------------------------------------------------------------


class QdrantConnector(VectorTargetConnector[dict[str, Any]]):
    """A read-only Qdrant REST connector, per PROJECT_SPEC.md section 8.11."""

    def __init__(
        self,
        config: QdrantTargetConfig,
        *,
        env: Mapping[str, str] | None = None,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._config = config
        self._clock = clock
        headers = {"user-agent": _USER_AGENT}
        api_key = config.resolve_api_key(env=env)
        if api_key is not None:
            headers["api-key"] = api_key
        self._client = httpx.Client(
            base_url=config.endpoint,
            headers=headers,
            timeout=httpx.Timeout(
                connect=config.connect_timeout_seconds,
                read=config.read_timeout_seconds,
                write=config.read_timeout_seconds,
                pool=config.connect_timeout_seconds,
            ),
            transport=transport,
            event_hooks={"request": [_guard_request]},
        )
        self._schema_cache: TargetSchema | None = None
        self._consistency: ConsistencyInfo | None = None

    # -- interface ---------------------------------------------------

    def validate_configuration(self) -> None:
        try:
            type(self._config).model_validate(self._config.model_dump(by_alias=True))
        except Exception as exc:  # pydantic.ValidationError, defensively broad
            raise ConnectorConfigError(f"invalid Qdrant target configuration: {exc}") from exc

    def test_connection(self) -> ConnectionTestResult:
        started = time.monotonic()
        try:
            response = self._request("GET", f"/collections/{self._config.collection}")
        except ConnectorConnectionError as exc:
            return ConnectionTestResult(ok=False, message=str(exc))
        latency_ms = (time.monotonic() - started) * 1000
        if response.status_code in (401, 403):
            return ConnectionTestResult(
                ok=False,
                message=f"authentication failed: HTTP {response.status_code}",
                latency_ms=latency_ms,
            )
        if response.status_code == 404:
            return ConnectionTestResult(
                ok=False,
                message=f"collection {self._config.collection!r} not found",
                latency_ms=latency_ms,
            )
        if response.status_code >= 400:
            return ConnectionTestResult(
                ok=False, message=f"unexpected HTTP {response.status_code}", latency_ms=latency_ms
            )
        return ConnectionTestResult(ok=True, message="reachable", latency_ms=latency_ms)

    def inspect_target_schema(self) -> TargetSchema:
        response = self._request("GET", f"/collections/{self._config.collection}")
        if response.status_code == 404:
            raise ConnectorConfigError(f"collection {self._config.collection!r} not found")
        if response.status_code >= 400:
            raise ConnectorConnectionError(
                f"failed to inspect collection {self._config.collection!r}: "
                f"HTTP {response.status_code}"
            )
        body = response.json()
        result = body.get("result", {}) or {}
        config_section = result.get("config", {}) or {}
        params = config_section.get("params", {}) or {}
        vector_fields = _parse_vector_fields(params.get("vectors"))
        payload_schema = result.get("payload_schema", {}) or {}
        resolved_scope = self._resolve_alias()
        schema = TargetSchema(
            target_id=self._config.collection,
            scope=self._config.collection,
            vector_fields=vector_fields,
            point_id_type="string_or_integer",
            payload_indexes=tuple(sorted(payload_schema.keys())),
            approx_point_count=result.get("points_count"),
            resolved_scope=resolved_scope,
            extra={"status": result.get("status")},
        )
        self._schema_cache = schema
        return schema

    def capabilities(self) -> ConnectorCapabilities:
        return ConnectorCapabilities(
            target_type="qdrant",
            supports_resume=True,
            supports_vector_hash=True,
            supports_sampling=False,
            max_page_size=10_000,
            consistency_modes=(ConsistencyMode.BEST_EFFORT_LIVE,),
        )

    def estimate_count(self) -> int | None:
        schema = self._schema_cache or self.inspect_target_schema()
        return schema.approx_point_count

    def normalize_point(
        self, raw: dict[str, Any], *, include_vectors: bool = False
    ) -> NormalizedPoint:
        warnings: list[str] = []
        point_id = _coerce_point_id(raw.get("id"))
        payload = raw.get("payload")
        if payload is None:
            payload = {}
        elif not isinstance(payload, dict):
            warnings.append("payload_not_object")
            payload = {}

        mapping = self._config.payload_mapping
        projection: dict[str, Any] = {}
        for logical_name, path in (
            ("source_id", mapping.source_id),
            ("source_version_id", mapping.source_version_id),
            ("chunk_id", mapping.chunk_id),
            ("embedding_id", mapping.embedding_id),
            ("tenant", mapping.tenant),
            ("acl", mapping.acl),
        ):
            if path is None:
                continue
            value = _resolve_payload_path(payload, path)
            if value is None:
                warnings.append(f"missing_mapped_field:{logical_name}")
                continue
            if logical_name == "acl":
                value = _coerce_acl(value, warnings)
            elif logical_name == "tenant":
                value = str(value)
            projection[logical_name] = value

        vector_name = self._config.vector_name
        vector_names: list[str] = []
        vector_dimensions: dict[str, int] = {}
        if self._schema_cache is not None:
            for field_schema in self._schema_cache.vector_fields:
                if vector_name is None or field_schema.name == vector_name:
                    key = field_schema.name or "default"
                    vector_names.append(key)
                    vector_dimensions[key] = field_schema.dimension

        vector_hashes: dict[str, str] | None = None
        if include_vectors:
            # FR-102: enabling vector retrieval is a resource-cost opt-in
            # (every scroll page now carries full vector payloads, not
            # just point ids/metadata), so it is always flagged here
            # regardless of whether this particular point's vector
            # happened to be present and hashable.
            warnings.append("vector_retrieval_enabled")
            raw_vector = raw.get("vector")
            if raw_vector is None:
                warnings.append("vector_missing")
            else:
                vector_hashes = _hash_raw_vector(raw_vector, vector_name)
                if vector_hashes is None:
                    warnings.append("vector_shape_unrecognized")

        scope = self._config.collection
        return NormalizedPoint(
            target_id=self._config.collection,
            scope=scope,
            point_id=point_id,
            vector_names=vector_names,
            vector_dimensions=vector_dimensions,
            vector_hashes=vector_hashes,
            payload_projection=projection,
            payload_hash=compute_payload_hash(projection),
            source_id=projection.get("source_id"),
            source_version_id=projection.get("source_version_id"),
            chunk_id=projection.get("chunk_id"),
            embedding_id=projection.get("embedding_id"),
            acl=projection.get("acl"),
            tenant=projection.get("tenant"),
            observed_at=self._clock(),
            raw_locator=f"qdrant:{scope}#{point_id}",
            normalization_warnings=warnings,
        )

    def iterate_points(
        self,
        *,
        checkpoint: Checkpoint | None = None,
        projection: Sequence[str] | None = None,
        include_vectors: bool = False,
    ) -> Iterator[NormalizedPoint]:
        if self._schema_cache is None:
            self.inspect_target_schema()
        assert self._schema_cache is not None  # populated by inspect_target_schema, just above
        start_count = self._schema_cache.approx_point_count

        want_vectors = include_vectors or self._config.snapshot.include_vectors
        with_vector: bool | list[str] = want_vectors
        if want_vectors and self._config.vector_name is not None:
            with_vector = [self._config.vector_name]

        offset: Any = checkpoint
        yielded = 0
        page_size = self._config.snapshot.page_size
        collection_path = f"/collections/{self._config.collection}/points/scroll"

        while True:
            body: dict[str, Any] = {
                "limit": page_size,
                "with_payload": True,
                "with_vector": with_vector,
            }
            if offset is not None:
                body["offset"] = offset
            response = self._request("POST", collection_path, json_body=body)
            if response.status_code >= 400:
                raise ConnectorConnectionError(
                    f"scroll failed with HTTP {response.status_code}: {response.text[:200]}"
                )
            result = response.json().get("result", {}) or {}
            points = result.get("points", []) or []
            for raw in points:
                yielded += 1
                point = self.normalize_point(raw, include_vectors=want_vectors)
                yield apply_projection(point, projection)
            next_offset = result.get("next_page_offset")
            if next_offset is None or not points:
                break
            offset = next_offset

        end_count = self._fetch_points_count()
        completeness = SnapshotCompleteness.COMPLETE
        detail: str | None = None
        if start_count is None or end_count is None:
            detail = "point count unavailable for one or both consistency probes"
        elif start_count != end_count:
            completeness = SnapshotCompleteness.INCOMPLETE
            detail = f"collection point count drifted from {start_count} to {end_count}"

        self._consistency = ConsistencyInfo(
            mode=ConsistencyMode.BEST_EFFORT_LIVE,
            completeness=completeness,
            start_count=start_count,
            end_count=end_count,
            observed_count=yielded,
            detail=detail,
        )

    def get_consistency_info(self) -> ConsistencyInfo:
        if self._consistency is None:
            raise RuntimeError("iterate_points has not completed a pass yet")
        return self._consistency

    def close(self) -> None:
        self._client.close()

    # -- internals -----------------------------------------------------

    def _fetch_points_count(self) -> int | None:
        response = self._request("GET", f"/collections/{self._config.collection}")
        if response.status_code >= 400:
            return None
        result: int | None = response.json().get("result", {}).get("points_count")
        return result

    def _resolve_alias(self) -> str | None:
        try:
            response = self._request("GET", _ALIASES_PATH)
        except ConnectorConnectionError:
            return None
        if response.status_code >= 400:
            return None
        aliases = response.json().get("result", {}).get("aliases", []) or []
        for entry in aliases:
            if entry.get("alias_name") == self._config.collection:
                collection_name = entry.get("collection_name")
                return str(collection_name) if collection_name is not None else None
        return None

    def _request(
        self, method: str, path: str, *, json_body: dict[str, Any] | None = None
    ) -> httpx.Response:
        attempt = 0
        delay = 0.5
        while True:
            try:
                response = self._client.request(method, path, json=json_body)
            except httpx.TransportError as exc:
                attempt += 1
                if attempt > self._config.max_retries:
                    raise ConnectorConnectionError(
                        f"{method} {path} failed after {attempt} attempt(s): {exc}"
                    ) from exc
                time.sleep(delay)
                delay = min(delay * 2, 8.0)
                continue
            if response.status_code >= 500 and attempt < self._config.max_retries:
                attempt += 1
                time.sleep(delay)
                delay = min(delay * 2, 8.0)
                continue
            return response
