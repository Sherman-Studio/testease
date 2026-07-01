"""Local run control — execute a persona run as a sibling Docker container.

The cluster path (:class:`qa_review_api.runs.K8sRunControl`) dispatches each run
as a Kubernetes Job. That's unavailable on the **local-first** ``docker compose``
stack, which has no cluster. This backend closes that gap: it launches the
**harness image** as a one-shot sibling container over the mounted Docker socket
(docker-out-of-docker), joined to the compose network so it reaches ``atlas``.

**One-shot and attended by design.** Each trigger starts exactly one container,
single-flight (refused while one is active), with no restart/loop — staying on
the right side of the Claude-subscription automation ToS (see CLAUDE.md). The
operator's BYOK token drives it; without one, the run can't proceed, so
:meth:`availability` reports that up front.

It mirrors the ``K8sRunControl`` surface the API depends on — ``trigger`` /
``active_run`` / ``secret_exists`` / ``stream_logs`` — plus ``availability`` for
the New Run pre-flight banner.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator

from .runs import ClusterUnavailable, RunAlreadyActive

log = logging.getLogger(__name__)

_RUN_LABEL = "testease.run-id"


class LocalRunControl:
    """Run the harness as a one-shot sibling container via the Docker socket."""

    def __init__(
        self,
        *,
        settings,
        harness_image: str,
        network: str,
        token_resolver: Callable[[], str | None] | None = None,
        docker_client=None,
    ) -> None:
        self._settings = settings
        self._image = harness_image
        self._network = network
        self._token_resolver = token_resolver
        self._docker = docker_client  # injectable for tests

    # -- docker plumbing ---------------------------------------------------
    def _client(self):
        if self._docker is not None:
            return self._docker
        try:
            import docker  # noqa: PLC0415 — optional; only the local backend needs it
        except ImportError as exc:  # pragma: no cover - dependency missing
            raise ClusterUnavailable(
                "the Docker SDK isn't installed in the control-room image",
            ) from exc
        try:
            self._docker = docker.from_env()
            return self._docker
        except Exception as exc:  # noqa: BLE001 — surface as the common error type
            raise ClusterUnavailable(f"Docker isn't reachable: {exc}") from exc

    def _token(self) -> str | None:
        if self._token_resolver is not None:
            return self._token_resolver()
        return None

    # -- availability / pre-flight ----------------------------------------
    def availability(self) -> tuple[bool, str | None]:
        """Whether a local run can actually start: Docker reachable, the harness
        image built, and a Claude token configured."""
        try:
            client = self._client()
            client.ping()
        except Exception as exc:  # noqa: BLE001
            return False, (
                "The control room can't reach Docker to launch a run. Mount the "
                f"Docker socket into the app container to enable local runs. ({exc})"
            )
        try:
            client.images.get(self._image)
        except Exception:  # noqa: BLE001 — image missing / not built yet
            return False, (
                f"The harness image '{self._image}' isn't built yet — run "
                "`docker compose build harness`, then retry."
            )
        if not self._token():
            return False, (
                "Set your Claude Code token in Settings first — local runs use it "
                "to drive the personas (no token, nothing to run with)."
            )
        return True, None

    # -- interface the API depends on --------------------------------------
    def secret_exists(self, name: str) -> bool:  # noqa: ARG002 — k8s-shaped contract
        """For local runs the 'credential' is the BYOK Claude token, not a k8s
        Secret. Returns whether one is configured."""
        return bool(self._token())

    def active_run(self) -> dict | None:
        client = self._client()
        try:
            running = client.containers.list(
                filters={"label": _RUN_LABEL, "status": "running"},
            )
        except Exception as exc:  # noqa: BLE001
            raise ClusterUnavailable(f"Docker isn't reachable: {exc}") from exc
        if not running:
            return None
        c = running[0]
        run_id = c.labels.get(_RUN_LABEL, c.name)
        started = (c.attrs.get("State", {}) or {}).get("StartedAt")
        return {
            "run_id": run_id,
            "job_name": c.name,
            "pod_name": c.name,
            "phase": "running",
            "started_at": started,
            "pod_count": 1,
        }

    def _run_env(self, *, target_url, enabled_mcp_servers, capability_env, target_id) -> dict:
        s = self._settings
        env = {
            "QA_STORE_URL": s.qa_store_url,
            "QA_MONGODB_URL": s.qa_store_url,
            "QA_STORE_DB": s.qa_store_db,
            "QA_LLM_BACKEND": "claude-code",
            "QA_EMBEDDING_PROVIDER": getattr(s, "embedding_provider", "local") or "local",
            # Mirror the k8s pod: a writable HOME on the world-writable /tmp, so
            # the non-root run user (below) has somewhere for the claude CLI's
            # config — the bundled CLI refuses --dangerously-skip-permissions as
            # root, so the local run must NOT be root.
            "HOME": "/tmp",
        }
        token = self._token()
        if token:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = token
        if target_url:
            env["QA_WEB_BASE_URL"] = target_url
        if target_id:
            env["QA_TARGET_ID"] = target_id
        if enabled_mcp_servers:
            env["QA_ENABLED_MCPS"] = ",".join(enabled_mcp_servers)
        if capability_env:
            env.update(capability_env)
        cred_key = getattr(s, "credential_key", "") or ""
        if cred_key:
            env["QA_CREDENTIAL_KEY"] = cred_key
        return env

    def trigger(
        self,
        personas: list[str],
        *,
        target_url: str | None = None,
        enabled_mcp_servers: list[str] | None = None,
        capability_env: dict[str, str] | None = None,
        target_id: str | None = None,
        store=None,  # noqa: ARG002 — harness writes its own run doc via create_run
        **_ignored,  # tolerate cluster-only kwargs (pod_count, models, …)
    ) -> dict:
        """Launch ONE harness container for ``personas`` (empty = every persona).
        Single-flight: refuses while a run is active."""
        client = self._client()
        if self.active_run() is not None:
            raise RunAlreadyActive("a local run is already in progress")
        run_id = f"local-{int(time.time())}"
        command = ["--personas", ",".join(personas)] if personas else ["--all"]
        env = self._run_env(
            target_url=target_url, enabled_mcp_servers=enabled_mcp_servers,
            capability_env=capability_env, target_id=target_id,
        )
        log.info("local run %s: launching harness container (%d persona(s))",
                 run_id, len(personas))
        client.containers.run(
            self._image,
            command=command,
            environment=env,
            network=self._network,
            labels={_RUN_LABEL: run_id},
            name=run_id,
            # Non-root: the bundled claude CLI rejects --dangerously-skip-
            # permissions under root. Any non-zero uid works (it writes only to
            # HOME=/tmp, which is world-writable); 1000 is the conventional first
            # user. Mirrors the cluster's non-root securityContext.
            user="1000:1000",
            detach=True,
            remove=False,
        )
        return {
            "job_name": run_id,
            "job_names": [run_id],
            "run_id": run_id,
            "pod_count": 1,
            "personas": list(personas),
        }

    def stream_logs(self) -> Iterator[str]:
        active = self.active_run()
        if active is None:
            yield "(no QA run is currently active)"
            return
        client = self._client()
        try:
            container = client.containers.get(active["job_name"])
        except Exception:  # noqa: BLE001
            yield "(run container is no longer available)"
            return
        for chunk in container.logs(stream=True, follow=True):
            yield chunk.decode("utf-8", "replace").rstrip("\n")
