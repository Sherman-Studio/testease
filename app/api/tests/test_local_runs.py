"""Tests for the local run backend (LocalRunControl) — harness-as-container.

A fake Docker client stands in for docker-py so we assert the run spec
(image / command / env) and the availability/single-flight contracts without a
real daemon or the harness image.
"""

from __future__ import annotations

import pytest

from qa_review_api.local_runs import _RUN_LABEL, LocalRunControl
from qa_review_api.runs import RunAlreadyActive
from qa_review_api.settings import Settings


class FakeContainer:
    def __init__(self, name, labels, state="running"):
        self.name = name
        self.labels = labels
        self.attrs = {"State": {"StartedAt": "2026-06-30T00:00:00Z"}}
        self._state = state

    def logs(self, stream=False, follow=False):  # noqa: ARG002
        return iter([b"persona started\n", b"finding filed\n"])


def _settings(**over):
    base = dict(
        qa_store_url="mongodb://atlas:27017", qa_store_db="testease",
        github_token="", github_repo="", run_backend="local",
        harness_image="testease-harness", run_network="testease_default",
        embedding_provider="local", credential_key="fernet-key",
    )
    base.update(over)
    return Settings(**base)


# A small hand-rolled docker-py double exposing .ping/.images/.containers.
class _Containers:
    def __init__(self):
        self._items: list[FakeContainer] = []
        self.run_calls: list[dict] = []

    def list(self, filters=None):  # noqa: ARG002
        return [c for c in self._items if c._state == "running"]

    def get(self, name):
        for c in self._items:
            if c.name == name:
                return c
        raise KeyError(name)

    def run(self, image, **kwargs):
        self.run_calls.append({"image": image, **kwargs})
        c = FakeContainer(kwargs["name"], kwargs.get("labels", {}))
        self._items.append(c)
        return c


class _Images:
    def __init__(self, present):
        self._present = set(present)

    def get(self, name):
        if name not in self._present:
            raise KeyError(name)
        return object()


class Docker:
    def __init__(self, *, images=("testease-harness",), pingable=True):
        self.containers = _Containers()
        self.images = _Images(images)
        self._pingable = pingable

    def ping(self):
        if not self._pingable:
            raise RuntimeError("cannot connect to docker")
        return True


def _control(docker, *, token="byok-token"):
    return LocalRunControl(
        settings=_settings(), harness_image="testease-harness",
        network="testease_default", token_resolver=lambda: token,
        docker_client=docker,
    )


# ── availability ──
def test_available_when_socket_image_and_token_present():
    assert _control(Docker()).availability() == (True, None)


def test_unavailable_without_token():
    ok, reason = _control(Docker(), token=None).availability()
    assert ok is False and "Settings" in reason


def test_unavailable_when_image_missing():
    ok, reason = _control(Docker(images=())).availability()
    assert ok is False and "harness image" in reason


def test_unavailable_when_docker_unreachable():
    ok, reason = _control(Docker(pingable=False)).availability()
    assert ok is False and "Docker" in reason


# ── trigger builds the right container spec ──
def test_trigger_launches_harness_with_personas_and_env():
    d = Docker()
    out = _control(d).trigger(
        ["maya", "priya"], target_url="https://acme.test", target_id="acme",
        enabled_mcp_servers=["email", "openapi"],
        capability_env={"QA_OPENAPI_URL": "https://acme.test/openapi.json"},
    )
    assert out["run_id"].startswith("local-")
    assert out["pod_count"] == 1 and out["personas"] == ["maya", "priya"]
    call = d.containers.run_calls[0]
    assert call["image"] == "testease-harness"
    assert call["command"] == ["--personas", "maya,priya"]
    assert call["network"] == "testease_default"
    assert call["detach"] is True
    # Must run non-root (the claude CLI rejects --dangerously-skip-permissions
    # as root) with a writable HOME.
    assert call["user"] == "1000:1000"
    env = call["environment"]
    assert env["HOME"] == "/tmp"
    assert env["QA_WEB_BASE_URL"] == "https://acme.test"
    assert env["QA_TARGET_ID"] == "acme"
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "byok-token"
    assert env["QA_ENABLED_MCPS"] == "email,openapi"
    assert env["QA_OPENAPI_URL"] == "https://acme.test/openapi.json"
    assert env["QA_CREDENTIAL_KEY"] == "fernet-key"
    assert call["labels"][_RUN_LABEL] == out["run_id"]


def test_trigger_empty_personas_runs_all():
    d = Docker()
    _control(d).trigger([])
    assert d.containers.run_calls[0]["command"] == ["--all"]


def test_trigger_is_single_flight():
    d = Docker()
    ctl = _control(d)
    ctl.trigger(["maya"])
    with pytest.raises(RunAlreadyActive):
        ctl.trigger(["priya"])


# ── active_run + secret_exists + logs ──
def test_active_run_reflects_running_container():
    d = Docker()
    ctl = _control(d)
    assert ctl.active_run() is None
    out = ctl.trigger(["maya"])
    active = ctl.active_run()
    assert active is not None
    assert active["run_id"] == out["run_id"]
    assert active["phase"] == "running"


def test_secret_exists_tracks_token():
    assert _control(Docker()).secret_exists("anything") is True
    assert _control(Docker(), token=None).secret_exists("anything") is False


def test_stream_logs_follows_the_container():
    d = Docker()
    ctl = _control(d)
    ctl.trigger(["maya"])
    lines = list(ctl.stream_logs())
    assert "persona started" in lines
