"""Tests for ``K8sRunControl.stream_logs`` (issue #849).

The previous implementation wrapped ``read_namespaced_pod_log`` in
``kubernetes.watch.Watch().stream(...)``, which on kubernetes-python 36.0.0
raises ``ApiTypeError: Got an unexpected keyword argument 'watch'`` because
``Watch.stream`` forces ``watch=True`` onto the wrapped call but pod-log
reads only accept ``follow``. We switched to the documented
``_preload_content=False`` + ``response.stream()`` pattern; these tests pin
that contract — including the retry-on-400 loop that waits out the
short-lived ``wipe-and-seed`` init container.
"""

from __future__ import annotations

import types
from unittest.mock import MagicMock, patch

import pytest

from qa_review_api.runs import K8sRunControl


# ---------------------------------------------------------------------------
# Helpers — install a minimal fake ``kubernetes`` module so ``K8sRunControl``
# can ``import kubernetes`` inside ``_kubernetes`` without a real cluster.
# We monkeypatch ``_kubernetes`` directly to return a SimpleNamespace whose
# ``client.CoreV1Api`` is the mock we want to drive.
# ---------------------------------------------------------------------------
@pytest.fixture
def fake_k8s(monkeypatch):
    """Yield (k8s_module_stub, core_v1_mock); patch ``_kubernetes`` to return it."""
    # Use the *real* ApiException so behaviour matches production at runtime.
    from kubernetes.client.exceptions import ApiException

    core = MagicMock(name="CoreV1Api")
    client_mod = types.SimpleNamespace(
        CoreV1Api=lambda: core,
        ApiException=ApiException,
    )
    # ``k8s.watch`` is intentionally absent — if the implementation tries to
    # reach for ``watch.Watch`` again the AttributeError will fail the test.
    k8s_stub = types.SimpleNamespace(client=client_mod)
    return k8s_stub, core


def _make_control(k8s_stub) -> K8sRunControl:
    rc = K8sRunControl(namespace="slyreply-sandbox", cronjob_name="qa-agents")
    rc._configured = True  # skip cluster config — we patch _kubernetes
    rc._kubernetes = lambda: k8s_stub  # type: ignore[method-assign]
    # The active_run() path also hits CoreV1Api.list_namespaced_pod; we
    # short-circuit it so each test only mocks the log call it cares about.
    rc.active_run = lambda: {  # type: ignore[method-assign]
        "job_name": "qa-ui-1",
        "pod_name": "qa-ui-1-abc",
        "phase": "Running",
        "started_at": None,
    }
    return rc


class _FakeLogResponse:
    """Mimics the urllib3.HTTPResponse you get from
    ``read_namespaced_pod_log(..., _preload_content=False)``.

    Records whether ``release_conn`` was called so we can assert cleanup.
    """

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks
        self.released = False

    def stream(self, decode_content: bool = False):  # noqa: ARG002 - matches API
        yield from self._chunks

    def release_conn(self) -> None:
        self.released = True


# ---------------------------------------------------------------------------
# Happy path — byte chunks come back as decoded strings, in order.
# ---------------------------------------------------------------------------
def test_stream_logs_yields_decoded_chunks(fake_k8s):
    k8s_stub, core = fake_k8s
    response = _FakeLogResponse(
        [b"==> run started\n", b"[daniel] hello\n", b"[margaret] hi\n"]
    )
    core.read_namespaced_pod_log.return_value = response

    rc = _make_control(k8s_stub)

    chunks = list(rc.stream_logs())

    assert chunks == ["==> run started\n", "[daniel] hello\n", "[margaret] hi\n"]
    # Exactly one call — no retries on the happy path.
    assert core.read_namespaced_pod_log.call_count == 1
    # Critical: the call uses ``follow=True`` + ``_preload_content=False`` and
    # does NOT pass ``watch=True`` (which was the kubernetes==36 ApiTypeError
    # at the root of #849).
    call = core.read_namespaced_pod_log.call_args
    assert call.kwargs["name"] == "qa-ui-1-abc"
    assert call.kwargs["namespace"] == "slyreply-sandbox"
    assert call.kwargs["follow"] is True
    assert call.kwargs["_preload_content"] is False
    assert "watch" not in call.kwargs
    # And the urllib3 conn was released.
    assert response.released is True


# ---------------------------------------------------------------------------
# Decoding never raises — bad UTF-8 is replaced, not exceptioned.
# ---------------------------------------------------------------------------
def test_stream_logs_decodes_invalid_utf8_with_replacement(fake_k8s):
    k8s_stub, core = fake_k8s
    # 0xff is not valid UTF-8 — must be replaced, not crash the generator.
    response = _FakeLogResponse([b"ok\n", b"\xff\xfebad\n"])
    core.read_namespaced_pod_log.return_value = response

    rc = _make_control(k8s_stub)

    chunks = list(rc.stream_logs())

    assert chunks[0] == "ok\n"
    assert chunks[1].endswith("bad\n")  # the prefix is replacement chars
    assert response.released is True


# ---------------------------------------------------------------------------
# Retry loop on 400 — the harness container only exists after wipe-and-seed
# completes. The initial call 400s; we yield a waiting message; retry; then
# succeed. The 400 happens at call time, not during streaming — that's why
# the ApiException is caught around ``read_namespaced_pod_log`` itself, not
# around the ``.stream()`` iteration.
# ---------------------------------------------------------------------------
def test_stream_logs_retries_on_400_then_succeeds(fake_k8s, monkeypatch):
    k8s_stub, core = fake_k8s
    from kubernetes.client.exceptions import ApiException

    response = _FakeLogResponse([b"finally up\n"])
    core.read_namespaced_pod_log.side_effect = [
        ApiException(status=400, reason="container not ready"),
        ApiException(status=400, reason="container not ready"),
        response,
    ]
    # Don't actually sleep through the retry pause in tests.
    monkeypatch.setattr("qa_review_api.runs.time.sleep", lambda _s: None)

    rc = _make_control(k8s_stub)

    chunks = list(rc.stream_logs())

    # Two waiting nudges, then the real chunk.
    assert chunks == [
        "(waiting for the run container to start…)",
        "(waiting for the run container to start…)",
        "finally up\n",
    ]
    assert core.read_namespaced_pod_log.call_count == 3
    assert response.released is True


# ---------------------------------------------------------------------------
# A non-retryable status (404 — pod is gone) does NOT loop; we emit a single
# error line and stop.
# ---------------------------------------------------------------------------
def test_stream_logs_does_not_retry_on_404(fake_k8s):
    k8s_stub, core = fake_k8s
    from kubernetes.client.exceptions import ApiException

    core.read_namespaced_pod_log.side_effect = ApiException(
        status=404, reason="Not Found"
    )

    rc = _make_control(k8s_stub)

    chunks = list(rc.stream_logs())

    assert chunks == ["(log stream error: HTTP 404)"]
    assert core.read_namespaced_pod_log.call_count == 1


# ---------------------------------------------------------------------------
# No active run → friendly note, no Kubernetes calls at all.
# ---------------------------------------------------------------------------
def test_stream_logs_when_no_active_run(fake_k8s):
    k8s_stub, core = fake_k8s
    rc = K8sRunControl(namespace="slyreply-sandbox", cronjob_name="qa-agents")
    rc._configured = True
    rc._kubernetes = lambda: k8s_stub  # type: ignore[method-assign]
    rc.active_run = lambda: None  # type: ignore[method-assign]

    chunks = list(rc.stream_logs())

    assert chunks == ["(no QA run is currently active)"]
    core.read_namespaced_pod_log.assert_not_called()


# ---------------------------------------------------------------------------
# #1822 — a MID-STREAM exception (the K8s API server resetting the log
# connection, a urllib3 ProtocolError, …) must NOT propagate out of the
# generator. Pre-fix it killed the SSE StreamingResponse mid-flight and the
# browser saw net::ERR_HTTP2_PROTOCOL_ERROR. Post-fix: the chunks already
# streamed are kept, one in-band error line is appended, the connection is
# released, and the generator returns cleanly.
# ---------------------------------------------------------------------------
class _ExplodingLogResponse(_FakeLogResponse):
    """Yields its chunks, then raises mid-stream like a dropped connection."""

    def __init__(self, chunks: list[bytes], exc: Exception) -> None:
        super().__init__(chunks)
        self._exc = exc

    def stream(self, decode_content: bool = False):  # noqa: ARG002
        yield from self._chunks
        raise self._exc


def test_stream_logs_mid_stream_exception_yields_error_line_not_raise(fake_k8s):
    k8s_stub, core = fake_k8s
    response = _ExplodingLogResponse(
        [b"==> run started\n"],
        ConnectionResetError("Connection reset by peer"),
    )
    core.read_namespaced_pod_log.return_value = response

    rc = _make_control(k8s_stub)

    # Must NOT raise — exhausting the generator is the assertion.
    chunks = list(rc.stream_logs())

    assert chunks[0] == "==> run started\n"
    assert len(chunks) == 2
    assert chunks[1].startswith("(log stream error: ")
    assert "ConnectionResetError" in chunks[1]
    # The urllib3 conn is still released on the error path.
    assert response.released is True


def test_stream_logs_mid_stream_error_line_is_single_line(fake_k8s):
    """The error reason is whitespace-collapsed so the SSE ``data:`` framing
    in the app layer can't be corrupted by a multi-line exception repr."""
    k8s_stub, core = fake_k8s
    response = _ExplodingLogResponse(
        [], RuntimeError("first line\nsecond line\n\nthird")
    )
    core.read_namespaced_pod_log.return_value = response

    rc = _make_control(k8s_stub)

    chunks = list(rc.stream_logs())

    assert len(chunks) == 1
    assert "\n" not in chunks[0]
    assert "first line second line third" in chunks[0]


def test_stream_logs_client_disconnect_propagates_generator_exit(fake_k8s):
    """GeneratorExit (the client hanging up) must NOT be swallowed by the
    mid-stream guard — closing the generator has to work normally."""
    k8s_stub, core = fake_k8s
    response = _FakeLogResponse([b"one\n", b"two\n", b"three\n"])
    core.read_namespaced_pod_log.return_value = response

    rc = _make_control(k8s_stub)

    gen = rc.stream_logs()
    assert next(gen) == "one\n"
    gen.close()  # raises GeneratorExit inside the generator — must not error
    # The conn is released by the finally even on early close.
    assert response.released is True


# ---------------------------------------------------------------------------
# #1822 — the SSE endpoint must terminate with the ``event: end`` frame on
# EVERY exit path, otherwise the SPA's EventSource auto-reconnect-loops.
# These drive the FastAPI route with a scripted run-control.
# ---------------------------------------------------------------------------
def _sse_client(run_control):
    import mongomock
    from fastapi.testclient import TestClient
    from qa_store.schema import Store

    from qa_review_api.app import create_app
    from qa_review_api.settings import Settings

    client = mongomock.MongoClient()
    store = Store(client=client, db_name="slyreply_qa_test")
    settings = Settings(
        qa_store_url="mongodb://x",
        qa_store_db="slyreply_qa_test",
        github_token="t",
        github_repo="mccullya/slyreply",
    )
    return TestClient(
        create_app(
            settings=settings,
            store=store,
            run_control=run_control,
            seed_personas=False,
        )
    )


def test_sse_endpoint_ends_stream_after_mid_stream_exception():
    """(a) — a mid-stream exception surfaces as an in-band error line plus
    the ``event: end`` terminator; the response completes instead of
    aborting with a protocol error."""

    class _MidStreamBoomControl:
        def stream_logs(self):
            yield "==> run started"
            raise ConnectionResetError("Connection reset by peer")

    resp = _sse_client(_MidStreamBoomControl()).get("/api/runs/active/logs")

    assert resp.status_code == 200
    body = resp.text
    assert "data: ==> run started\n\n" in body
    assert "data: (log stream error: " in body
    assert "ConnectionResetError" in body
    # The terminator frame closes the EventSource — no reconnect loop.
    assert body.rstrip().endswith("event: end\ndata: done")


def test_sse_endpoint_ends_stream_on_non_400_api_exception(fake_k8s, monkeypatch):
    """(b) — the non-400 ApiException path inside ``stream_logs`` (pod gone,
    RBAC revoked, …) yields its error line AND the endpoint still emits the
    ``event: end`` terminator."""
    k8s_stub, core = fake_k8s
    from kubernetes.client.exceptions import ApiException

    core.read_namespaced_pod_log.side_effect = ApiException(
        status=404, reason="Not Found"
    )
    rc = _make_control(k8s_stub)

    resp = _sse_client(rc).get("/api/runs/active/logs")

    assert resp.status_code == 200
    body = resp.text
    assert "data: (log stream error: HTTP 404)\n\n" in body
    assert body.rstrip().endswith("event: end\ndata: done")


def test_sse_endpoint_ends_stream_on_timed_out_waiting_path(fake_k8s, monkeypatch):
    """The retry loop exhausting itself (container never started) also
    terminates with the end frame."""
    k8s_stub, core = fake_k8s
    from kubernetes.client.exceptions import ApiException

    core.read_namespaced_pod_log.side_effect = ApiException(
        status=400, reason="container not ready"
    )
    monkeypatch.setattr("qa_review_api.runs.time.sleep", lambda _s: None)
    rc = _make_control(k8s_stub)

    resp = _sse_client(rc).get("/api/runs/active/logs")

    assert resp.status_code == 200
    body = resp.text
    assert "data: (timed out waiting for the run container to start)\n\n" in body
    assert body.rstrip().endswith("event: end\ndata: done")


# ---------------------------------------------------------------------------
# Regression for #849 itself: explicitly assert the fix does NOT route through
# ``Watch.stream`` any more. If a future refactor brings it back, the
# kubernetes==36 ApiTypeError will silently break live logs again — so a
# patched ``Watch`` that records calls must never be invoked.
# ---------------------------------------------------------------------------
def test_stream_logs_never_calls_watch_stream(fake_k8s):
    k8s_stub, core = fake_k8s
    response = _FakeLogResponse([b"hi\n"])
    core.read_namespaced_pod_log.return_value = response

    # Patch the real kubernetes.watch.Watch so any accidental use blows up
    # the test loudly instead of silently regressing #849.
    with patch("kubernetes.watch.Watch") as watch_cls:
        rc = _make_control(k8s_stub)
        list(rc.stream_logs())
        watch_cls.assert_not_called()
