"""Tests for `ragledger.connectors.qdrant`, using `httpx.MockTransport` (no network).

Covers: scroll pagination with checkpoint resume, concurrent-mutation
count drift, authentication failure, malformed payload handling, and
the section 42.2 mutation guard.
"""

from __future__ import annotations

import json

import httpx
import pytest

from ragledger.connectors.base import (
    ConnectorConfigError,
    ConnectorConnectionError,
    ConnectorMutationBlockedError,
    SnapshotCompleteness,
    hash_vector,
)
from ragledger.connectors.config import QdrantTargetConfig
from ragledger.connectors.qdrant import (
    QdrantConnector,
    _coerce_acl,
    _hash_raw_vector,
    _is_allowed_request,
    _parse_vector_fields,
    _resolve_payload_path,
)

COLLECTION = "support_kb"


def _config(**overrides: object) -> QdrantTargetConfig:
    fields: dict[str, object] = {
        "endpoint": "https://qdrant.test",
        "collection": COLLECTION,
        "vector_name": "dense",
        "payload_mapping": {
            "source_id": "ragledger.source_id",
            "chunk_id": "ragledger.chunk_id",
            "tenant": "tenant_id",
            "acl": "allowed_groups",
        },
        "snapshot": {"page_size": 2},
        "max_retries": 0,
    }
    fields.update(overrides)
    return QdrantTargetConfig.model_validate(fields)


def _collection_info_response(points_count: int) -> dict[str, object]:
    return {
        "status": "ok",
        "time": 0.001,
        "result": {
            "status": "green",
            "points_count": points_count,
            "config": {"params": {"vectors": {"dense": {"size": 3, "distance": "Cosine"}}}},
            "payload_schema": {"tenant_id": {"data_type": "keyword"}},
        },
    }


def _point(point_id: int, *, source_id: str | None = "src_a") -> dict[str, object]:
    payload: dict[str, object] = {"tenant_id": "acme", "allowed_groups": ["group:support"]}
    if source_id is not None:
        payload["ragledger"] = {"source_id": source_id, "chunk_id": f"chk_{point_id}"}
    return {"id": point_id, "payload": payload}


class FakeQdrantServer:
    """A minimal, stateful fake of the Qdrant REST endpoints this connector calls."""

    def __init__(
        self,
        *,
        pages: list[list[dict[str, object]]],
        points_counts: list[int],
        require_api_key: str | None = None,
    ) -> None:
        self.pages = pages
        self.points_counts = list(points_counts)
        self.require_api_key = require_api_key
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if (
            self.require_api_key is not None
            and request.headers.get("api-key") != self.require_api_key
        ):
            return httpx.Response(401, json={"status": {"error": "unauthorized"}, "result": None})

        if request.method == "GET" and request.url.path == f"/collections/{COLLECTION}":
            count = (
                self.points_counts.pop(0) if len(self.points_counts) > 1 else self.points_counts[0]
            )
            return httpx.Response(200, json=_collection_info_response(count))

        if request.method == "GET" and request.url.path == "/aliases":
            return httpx.Response(200, json={"status": "ok", "result": {"aliases": []}})

        if (
            request.method == "POST"
            and request.url.path == f"/collections/{COLLECTION}/points/scroll"
        ):
            body = json.loads(request.content)
            offset = body.get("offset")
            index = 0 if offset is None else offset
            points = self.pages[index] if index < len(self.pages) else []
            next_offset = index + 1 if index + 1 < len(self.pages) else None
            return httpx.Response(
                200,
                json={
                    "status": "ok",
                    "result": {"points": points, "next_page_offset": next_offset},
                },
            )

        return httpx.Response(404, json={"status": "not_found", "result": None})


def _connector(
    server: FakeQdrantServer,
    config: QdrantTargetConfig | None = None,
    *,
    env: dict[str, str] | None = None,
) -> QdrantConnector:
    transport = httpx.MockTransport(server.handler)
    return QdrantConnector(config or _config(), transport=transport, env=env)


# --------------------------------------------------------------------------
# Pagination and checkpoint resume
# --------------------------------------------------------------------------


def test_iterate_points_streams_all_pages_in_order() -> None:
    server = FakeQdrantServer(
        pages=[[_point(1), _point(2)], [_point(3)]],
        points_counts=[3, 3],
    )
    connector = _connector(server)
    points = list(connector.iterate_points())

    assert [point.point_id for point in points] == [1, 2, 3]
    assert points[0].source_id == "src_a"
    assert points[0].chunk_id == "chk_1"
    assert points[0].tenant == "acme"
    assert points[0].acl == ["group:support"]

    consistency = connector.get_consistency_info()
    assert consistency.completeness is SnapshotCompleteness.COMPLETE
    assert consistency.observed_count == 3
    connector.close()


def test_iterate_points_resumes_from_checkpoint() -> None:
    server = FakeQdrantServer(
        pages=[[_point(1), _point(2)], [_point(3)]],
        points_counts=[3, 3],
    )
    connector = _connector(server)
    resumed = list(connector.iterate_points(checkpoint=1))

    assert [point.point_id for point in resumed] == [3]
    scroll_requests = [
        request
        for request in server.requests
        if request.method == "POST" and request.url.path.endswith("/points/scroll")
    ]
    assert len(scroll_requests) == 1
    body = json.loads(scroll_requests[0].content)
    assert body["offset"] == 1
    connector.close()


def test_iterate_points_applies_projection() -> None:
    server = FakeQdrantServer(pages=[[_point(1)]], points_counts=[1, 1])
    connector = _connector(server)
    points = list(connector.iterate_points(projection=["source_id"]))

    assert points[0].payload_projection == {"source_id": "src_a"}
    assert points[0].chunk_id is None
    connector.close()


# --------------------------------------------------------------------------
# Consistency: concurrent mutation drift
# --------------------------------------------------------------------------


def test_iterate_points_marks_snapshot_incomplete_on_count_drift() -> None:
    server = FakeQdrantServer(
        pages=[[_point(1), _point(2)], [_point(3)]],
        points_counts=[10, 8],
    )
    connector = _connector(server)
    list(connector.iterate_points())

    consistency = connector.get_consistency_info()
    assert consistency.completeness is SnapshotCompleteness.INCOMPLETE
    assert consistency.start_count == 10
    assert consistency.end_count == 8
    assert consistency.detail is not None
    connector.close()


def test_get_consistency_info_before_iteration_raises() -> None:
    server = FakeQdrantServer(pages=[[]], points_counts=[0, 0])
    connector = _connector(server)
    with pytest.raises(RuntimeError):
        connector.get_consistency_info()
    connector.close()


# --------------------------------------------------------------------------
# Authentication failure
# --------------------------------------------------------------------------


def test_test_connection_reports_auth_failure() -> None:
    server = FakeQdrantServer(pages=[[]], points_counts=[0], require_api_key="expected-key")
    connector = _connector(
        server,
        _config(api_key_env="QDRANT_API_KEY"),
        env={"QDRANT_API_KEY": "wrong-key"},
    )
    result = connector.test_connection()

    assert result.ok is False
    assert "authentication" in result.message
    connector.close()


def test_test_connection_succeeds_with_correct_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QDRANT_API_KEY", "expected-key")
    server = FakeQdrantServer(pages=[[]], points_counts=[5], require_api_key="expected-key")
    connector = _connector(server, _config(api_key_env="QDRANT_API_KEY"))
    result = connector.test_connection()

    assert result.ok is True
    connector.close()


def test_test_connection_reports_missing_collection() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"status": "not_found", "result": None})

    connector = QdrantConnector(_config(), transport=httpx.MockTransport(handler))
    result = connector.test_connection()
    assert result.ok is False
    assert "not found" in result.message
    connector.close()


# --------------------------------------------------------------------------
# Malformed payload handling
# --------------------------------------------------------------------------


def test_normalize_point_handles_non_dict_payload() -> None:
    server = FakeQdrantServer(pages=[[]], points_counts=[0, 0])
    connector = _connector(server)
    connector.inspect_target_schema()

    raw = {"id": "abc", "payload": "not-a-dict"}
    point = connector.normalize_point(raw)

    assert point.payload_projection == {}
    assert "payload_not_object" in point.normalization_warnings
    connector.close()


def test_normalize_point_warns_on_missing_mapped_fields() -> None:
    server = FakeQdrantServer(pages=[[]], points_counts=[0, 0])
    connector = _connector(server)
    connector.inspect_target_schema()

    raw = {"id": 1, "payload": {}}
    point = connector.normalize_point(raw)

    assert any(w.startswith("missing_mapped_field:") for w in point.normalization_warnings)
    assert point.source_id is None
    connector.close()


def test_normalize_point_warns_on_missing_vector_when_requested() -> None:
    server = FakeQdrantServer(pages=[[]], points_counts=[0, 0])
    connector = _connector(server)
    connector.inspect_target_schema()

    raw = {"id": 1, "payload": {}}
    point = connector.normalize_point(raw, include_vectors=True)

    assert "vector_missing" in point.normalization_warnings
    connector.close()


def test_normalize_point_hashes_named_vector() -> None:
    server = FakeQdrantServer(pages=[[]], points_counts=[0, 0])
    connector = _connector(server)
    connector.inspect_target_schema()

    raw = {"id": 1, "payload": {}, "vector": {"dense": [0.1, 0.2, 0.3]}}
    point = connector.normalize_point(raw, include_vectors=True)

    assert point.vector_hashes is not None
    assert "dense" in point.vector_hashes
    connector.close()


def test_normalize_point_hashes_unnamed_default_vector() -> None:
    server = FakeQdrantServer(pages=[[]], points_counts=[0, 0])
    connector = _connector(server, _config(vector_name=None))
    connector.inspect_target_schema()

    raw = {"id": 1, "payload": {}, "vector": [0.1, 0.2, 0.3]}
    point = connector.normalize_point(raw, include_vectors=True)

    assert point.vector_hashes == {"default": hash_vector([0.1, 0.2, 0.3])}
    connector.close()


# --------------------------------------------------------------------------
# Section 42.2 mutation guard
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("PUT", f"/collections/{COLLECTION}/points"),
        ("DELETE", f"/collections/{COLLECTION}/points/delete"),
        ("POST", f"/collections/{COLLECTION}/points"),
        ("PATCH", f"/collections/{COLLECTION}"),
        ("DELETE", f"/collections/{COLLECTION}"),
        ("PUT", f"/collections/{COLLECTION}"),
        ("POST", f"/collections/{COLLECTION}"),
    ],
)
def test_guard_blocks_mutating_requests(method: str, path: str) -> None:
    assert _is_allowed_request(method, path) is False


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", f"/collections/{COLLECTION}"),
        ("GET", "/aliases"),
        ("POST", f"/collections/{COLLECTION}/points/scroll"),
    ],
)
def test_guard_allows_read_requests(method: str, path: str) -> None:
    assert _is_allowed_request(method, path) is True


def test_guard_raises_before_request_reaches_transport() -> None:
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={"result": {}})

    connector = QdrantConnector(_config(), transport=httpx.MockTransport(handler))
    with pytest.raises(ConnectorMutationBlockedError):
        connector._client.request("DELETE", f"/collections/{COLLECTION}")

    assert called is False
    connector.close()


def test_guard_raises_for_put_upsert_attempt() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("mutating request must never reach the transport")

    connector = QdrantConnector(_config(), transport=httpx.MockTransport(handler))
    with pytest.raises(ConnectorMutationBlockedError):
        connector._client.put(f"/collections/{COLLECTION}/points", json={"points": []})
    connector.close()


# --------------------------------------------------------------------------
# Retries / connection errors
# --------------------------------------------------------------------------


def test_transport_error_raises_connector_connection_error_after_retries() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    connector = QdrantConnector(_config(max_retries=1), transport=httpx.MockTransport(handler))
    # inspect_target_schema does not itself catch ConnectorConnectionError
    # (unlike test_connection, which reports it as a graceful ok=False
    # result), so it is the right entry point for asserting the retry
    # loop's terminal error propagates as expected.
    with pytest.raises(ConnectorConnectionError):
        connector.inspect_target_schema()
    connector.close()


def test_test_connection_reports_transport_error_as_unreachable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    connector = QdrantConnector(_config(max_retries=0), transport=httpx.MockTransport(handler))
    result = connector.test_connection()
    assert result.ok is False
    connector.close()


def test_transient_500_is_retried_then_succeeds() -> None:
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        if attempts["count"] == 1:
            return httpx.Response(500, json={"status": "error"})
        return httpx.Response(200, json=_collection_info_response(3))

    connector = QdrantConnector(_config(max_retries=1), transport=httpx.MockTransport(handler))
    result = connector.test_connection()
    assert result.ok is True
    assert attempts["count"] == 2
    connector.close()


# --------------------------------------------------------------------------
# Raw record helper functions
# --------------------------------------------------------------------------


def test_coerce_acl_warns_on_non_list_value() -> None:
    warnings: list[str] = []
    assert _coerce_acl("solo-group", warnings) == ["solo-group"]
    assert warnings == ["acl_not_list"]


def test_resolve_payload_path_missing_intermediate_segment_returns_none() -> None:
    assert _resolve_payload_path({"ragledger": "not-a-dict"}, "ragledger.source_id") is None
    assert _resolve_payload_path({}, "ragledger.source_id") is None


def test_hash_raw_vector_unrecognized_shape_returns_none() -> None:
    assert _hash_raw_vector("not-a-vector", None) is None
    assert _hash_raw_vector({"dense": "also-not-a-vector"}, None) is None


def test_parse_vector_fields_unnamed_default_vector() -> None:
    fields = _parse_vector_fields({"size": 384, "distance": "Cosine"})
    assert len(fields) == 1
    assert fields[0].name == ""
    assert fields[0].dimension == 384


def test_parse_vector_fields_unrecognized_shape_returns_empty() -> None:
    assert _parse_vector_fields(None) == ()
    assert _parse_vector_fields("garbage") == ()


# --------------------------------------------------------------------------
# Additional connector coverage
# --------------------------------------------------------------------------


def test_validate_configuration_rejects_mutated_invalid_config() -> None:
    server = FakeQdrantServer(pages=[[]], points_counts=[0])
    connector = _connector(server)
    connector._config.collection = "bad name"  # type: ignore[misc]
    with pytest.raises(ConnectorConfigError):
        connector.validate_configuration()
    connector.close()


def test_capabilities_reports_qdrant_target_type() -> None:
    server = FakeQdrantServer(pages=[[]], points_counts=[0])
    connector = _connector(server)
    capabilities = connector.capabilities()
    assert capabilities.target_type == "qdrant"
    assert capabilities.supports_vector_hash is True
    connector.close()


def test_estimate_count_uses_cached_schema() -> None:
    server = FakeQdrantServer(pages=[[]], points_counts=[7, 7])
    connector = _connector(server)
    assert connector.estimate_count() == 7
    connector.close()


def test_normalize_point_handles_missing_payload_key() -> None:
    server = FakeQdrantServer(pages=[[]], points_counts=[0, 0])
    connector = _connector(server)
    connector.inspect_target_schema()

    point = connector.normalize_point({"id": 1})
    assert point.payload_projection == {}
    connector.close()


def test_normalize_point_warns_on_unrecognized_vector_shape() -> None:
    server = FakeQdrantServer(pages=[[]], points_counts=[0, 0])
    connector = _connector(server)
    connector.inspect_target_schema()

    raw = {"id": 1, "payload": {}, "vector": "not-a-vector"}
    point = connector.normalize_point(raw, include_vectors=True)
    assert "vector_shape_unrecognized" in point.normalization_warnings
    connector.close()


def test_iterate_points_raises_on_scroll_http_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=_collection_info_response(1))
        return httpx.Response(500, json={"status": "error"})

    connector = QdrantConnector(_config(max_retries=0), transport=httpx.MockTransport(handler))
    with pytest.raises(ConnectorConnectionError):
        list(connector.iterate_points())
    connector.close()


def test_iterate_points_reports_detail_when_count_probe_unavailable() -> None:
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == f"/collections/{COLLECTION}":
            call_count["n"] += 1
            if call_count["n"] == 1:
                return httpx.Response(200, json=_collection_info_response(1))
            return httpx.Response(500, json={"status": "error"})
        if request.method == "GET" and request.url.path == "/aliases":
            return httpx.Response(200, json={"status": "ok", "result": {"aliases": []}})
        empty_page = {"points": [], "next_page_offset": None}
        return httpx.Response(200, json={"status": "ok", "result": empty_page})

    connector = QdrantConnector(_config(max_retries=0), transport=httpx.MockTransport(handler))
    list(connector.iterate_points())
    consistency = connector.get_consistency_info()
    assert consistency.end_count is None
    assert consistency.detail is not None
    connector.close()


def test_resolve_alias_returns_resolved_collection_name() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/collections/{COLLECTION}":
            return httpx.Response(200, json=_collection_info_response(0))
        if request.url.path == "/aliases":
            return httpx.Response(
                200,
                json={
                    "status": "ok",
                    "result": {"aliases": [{"alias_name": COLLECTION, "collection_name": "kb_v2"}]},
                },
            )
        return httpx.Response(404, json={"status": "not_found"})

    connector = QdrantConnector(_config(), transport=httpx.MockTransport(handler))
    schema = connector.inspect_target_schema()
    assert schema.resolved_scope == "kb_v2"
    connector.close()
