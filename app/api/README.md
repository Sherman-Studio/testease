# qa-review-api

The FastAPI backend for the SlyReply Persona QA review UI — it reads the
`slyreply_qa` findings store, serves the run / finding triage API, and files one
GitHub issue per run on demand.

This package is one half of the review UI; the Vue 3 SPA lives in [`../web/`](../web)
and the two ship as a single image. See [`../README.md`](../README.md) for the
full picture — running it locally, the environment variables, and the API
contract.
