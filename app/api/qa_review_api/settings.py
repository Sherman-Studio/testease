"""Environment-driven settings for the review UI API.

Kept tiny and explicit — the API has only a handful of knobs. ``GITHUB_TOKEN``
is the one that must be present for the file-issue endpoint to work; everything
else has a working default.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    """Resolved API configuration."""

    qa_store_url: str
    qa_store_db: str
    github_token: str
    github_repo: str
    # Where the QA harness runs — the namespace holding the `qa-agents`
    # CronJob the run-trigger endpoint builds Jobs from. The review UI itself
    # lives in a different namespace (slyreply-qa); see runs.py. Defaulted so
    # tests can build Settings without naming them.
    sandbox_namespace: str = "qa-sandbox"
    # The single Max-only CronJob the trigger endpoint builds Jobs from.
    # It scrubs ANTHROPIC_API_KEY so every run bills Claude Code Max.
    qa_cronjob_name: str = "qa-agents"
    # #894 — name of the k8s Secret holding the long-lived Claude Code
    # OAuth token (CLAUDE_CODE_OAUTH_TOKEN). The trigger endpoint
    # checks this Secret exists before creating a Max-billed Job so
    # the operator sees a clean 422 ("token not provisioned") instead
    # of a half-created Job that pod-starts and 401s.
    qa_claude_code_secret_name: str = "qa-claude-code-credentials"
    # #1108 — Mailpit admin API base, used by the /admin nuclear button
    # when the operator opts into the "wipe Mailpit" toggle. Default is
    # the cross-namespace Service URL; the qa-review pod lives in
    # slyreply-qa, Mailpit lives in slyreply-sandbox, so the FQDN form
    # is required. /mailpit webroot per #979.
    mailpit_admin_url: str = (
        "http://mailpit.qa-sandbox.svc.cluster.local:8025/mailpit"
    )
    # How runs are dispatched: ``k8s`` (default — build a Job from the CronJob)
    # or ``local`` (launch the harness as a sibling Docker container, for the
    # local-first ``docker compose`` stack with no cluster). The compose file
    # sets ``QA_RUN_BACKEND=local`` + mounts the Docker socket.
    run_backend: str = "k8s"
    # Local backend: the harness image to run and the Docker network to join so
    # the container reaches ``atlas`` (the compose project network).
    harness_image: str = "testease-harness"
    run_network: str = "testease_default"
    # Threaded to the harness container env on a local run.
    embedding_provider: str = "local"
    credential_key: str = ""

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            qa_store_url=os.environ.get("QA_STORE_URL", "mongodb://localhost:27017"),
            qa_store_db=os.environ.get("QA_STORE_DB", "testease"),
            github_token=os.environ.get("GITHUB_TOKEN", ""),
            github_repo=os.environ.get("GITHUB_REPO", ""),
            sandbox_namespace=os.environ.get("QA_SANDBOX_NAMESPACE", "qa-sandbox"),
            qa_cronjob_name=os.environ.get("QA_CRONJOB_NAME", "qa-agents"),
            qa_claude_code_secret_name=os.environ.get(
                "QA_CLAUDE_CODE_SECRET_NAME", "qa-claude-code-credentials"
            ),
            mailpit_admin_url=os.environ.get(
                "QA_MAILPIT_ADMIN_URL",
                "http://mailpit.qa-sandbox.svc.cluster.local:8025/mailpit",
            ),
            run_backend=os.environ.get("QA_RUN_BACKEND", "k8s"),
            harness_image=os.environ.get("QA_HARNESS_IMAGE", "testease-harness"),
            run_network=os.environ.get("QA_RUN_NETWORK", "testease_default"),
            embedding_provider=os.environ.get("QA_EMBEDDING_PROVIDER", "local"),
            credential_key=os.environ.get("QA_CREDENTIAL_KEY", ""),
        )
