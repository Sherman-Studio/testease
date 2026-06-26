"""Pytest bootstrap for the review-UI API tests.

The API imports the harness's **persona catalog** (``qa_agents.personas``) to
build ``KNOWN_PERSONAS`` and to seed the persona library in ``create_app`` —
see ``qa_review_api.runs._load_known_personas``. ``qa_agents.personas`` is a
pure-stdlib module, so the runtime image installs the harness ``--no-deps``
(skipping playwright / claude-agent-sdk) purely to expose that catalog.

The API's own ``pyproject.toml`` deliberately does NOT depend on ``qa-agents``
(we don't want its heavy transitive deps pulled into the API env). So for the
test suite we reproduce the image's ``--no-deps`` exposure the same lightweight
way: put the sibling ``harness/`` source on ``sys.path`` when ``qa_agents``
isn't already importable. Without this, ``KNOWN_PERSONAS`` is empty and every
persona-dependent path (scenario validation, the multi-pod roster, transcript
persona filters) fails — the 74-test breakage seen right after extraction.
"""

from __future__ import annotations

import sys
from pathlib import Path

try:  # already installed (e.g. the runtime image, or an editable install)?
    import qa_agents  # noqa: F401
except ImportError:
    # tests/ -> app/api -> app -> <repo root>; the harness lives at repo/harness.
    harness_src = Path(__file__).resolve().parents[3] / "harness"
    if (harness_src / "qa_agents").is_dir():
        sys.path.insert(0, str(harness_src))
