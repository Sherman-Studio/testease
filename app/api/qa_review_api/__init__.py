"""qa_review_api — FastAPI backend for the Persona QA review UI (#626).

Reads the shared ``slyreply_qa`` store (via the light ``qa-store`` package —
no Agent SDK), exposes the run + finding REST surface the Vue SPA consumes,
and composes one GitHub issue per run when a human decides to file.
"""

__version__ = "0.1.0"
