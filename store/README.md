# qa-store

The shared MongoDB store for the SlyReply Persona QA Agents epic ([#616]).

It owns the `slyreply_qa` Atlas database — the schema, the indexes, and the
access functions — and nothing else. It depends on **pymongo only**: both the
harness (`qa-agents/harness`) and the review UI API (`qa-agents/review-ui/api`)
import it, and neither should have to pull the Claude Agent SDK in just to read
or write a finding.

[#616]: https://github.com/mccullya/slyreply/issues/616

## Schema

Two collections in `slyreply_qa`:

### `qa_runs` — one document per orchestrated harness run

```
run_id        str    — shared id for the whole run (all personas in one job)
started_at    datetime
finished_at   datetime | null
status        str    — new | reviewed | filed | dismissed
personas      [str]  — persona ids included in the run
reviews       [ { persona, review_markdown, verdict, accounting } ]
totals        { input_tokens, output_tokens, cache_tokens, backend }
              — pre-#1822 docs may also carry cost_usd / real_cost_usd;
                readers pass them through, new runs never write them
gh_issue_url  str | null   — set once a human files the GitHub issue
discord_url   str | null
```

### `qa_findings` — one document per finding

```
finding_id    str    — stable id (run_id : persona : ordinal)
run_id        str
persona       str
category      str    — bug | confusion | copy | missing-feature | worry | surprise
severity      str    — blocker | major | minor | nit
title         str
body          str
status        str    — open | included | dismissed
created_at    datetime
```

## Usage

```python
from qa_store import connect, create_run, add_persona_result, finish_run

store = connect()  # reads QA_STORE_URL / QA_STORE_DB from the environment

create_run(store, "qa-20260519T120000Z", ["margaret", "daniel"])
add_persona_result(
    store, "qa-20260519T120000Z", "margaret",
    review_markdown="## First impressions ...",
    verdict="Cautiously would use it.",
    accounting={"total_input_tokens": 1000, ...},
    findings=[{"category": "confusion", "severity": "major", "title": "...", "body": "..."}],
)
finish_run(store, "qa-20260519T120000Z",
           {"input_tokens": 1000, "output_tokens": 500, "cache_tokens": 0})
```

All write functions are idempotent where it makes sense: `create_run` upserts,
`add_persona_result` replaces a persona's slice rather than duplicating it.

## Environment

| Variable        | Default       | Purpose                          |
|-----------------|---------------|----------------------------------|
| `QA_STORE_URL`  | `mongodb://localhost:27017` | Atlas SRV URI       |
| `QA_STORE_DB`   | `slyreply_qa` | Database name                    |
