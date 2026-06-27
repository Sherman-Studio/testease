"""The explorer's per-target questionnaire — questions as DATA.

The explorer probes a site, then asks the operator the questions it can't answer
itself (test-card numbers, sandbox logins, "is there an API?", "may I have
read-only DB access?"). Every site's questions differ, so the questionnaire is
an unbounded list of rows keyed by ``(tenant_id, target_id, question_id)`` —
not a fixed schema. This module is the access layer; the schema constants,
``Store.site_questions`` accessor and indexes live in ``schema.py``.

The questionnaire is the product's hinge: it unifies consent/authorization,
configuration (creds/scope), and knowledge-elicitation in one adaptive artifact.

**Secrets never live here.** A ``kind="secret"`` answer is written to the vault
(:mod:`qa_store.vault`) and the row keeps only the ``credential_ref`` pointer —
the raw value is never persisted on the question. Reading a question back never
returns a secret value; a consumer that needs it calls ``vault.get_secret`` with
the pointer.

Same conventions as ``site_model.py``: idempotent upserts, ``_strip_id`` on
read. Re-running the explorer re-``upsert``s questions — metadata (text,
rationale, …) is refreshed but an operator's existing answer is preserved.
"""

from __future__ import annotations

from typing import Any

from pymongo import ASCENDING

from qa_store.schema import (
    DEFAULT_TENANT,
    SITE_QUESTION_KINDS,
    Store,
    _now,
    _strip_id,
)
from qa_store.vault import delete_secret, put_secret


def upsert_site_question(
    store: Store,
    *,
    target_id: str,
    question_id: str,
    text: str,
    tenant_id: str = DEFAULT_TENANT,
    kind: str = "free_text",
    category: str = "general",
    rationale: str = "",
    options: list | None = None,
    required: bool = False,
    order: int = 0,
    generated_by: str = "explorer",
) -> dict:
    """Create or refresh a question.

    The QUESTION (text, kind, category, rationale, options, required, order,
    generated_by) is ``$set`` on every call so a re-exploring agent keeps the
    wording current; the ANSWER state (``status``/``answer``/``credential_ref``/
    ``answered_at``) is ``$setOnInsert`` only, so re-asking never clobbers an
    operator's existing answer. ``question_id`` must be a slug (no ``/``) — it
    doubles as the vault ref for secret answers.
    """
    if kind not in SITE_QUESTION_KINDS:
        raise ValueError(
            f"unknown question kind {kind!r}; expected one of "
            f"{', '.join(SITE_QUESTION_KINDS)}",
        )
    if "/" in question_id or not question_id:
        raise ValueError(f"question_id must be a non-empty slug without '/': {question_id!r}")
    now = _now()
    key = {"tenant_id": tenant_id, "target_id": target_id, "question_id": question_id}
    store.site_questions.update_one(
        key,
        {
            "$set": {
                "text": text,
                "kind": kind,
                "category": category,
                "rationale": rationale,
                "options": list(options or []),
                "required": bool(required),
                "order": int(order),
                "generated_by": generated_by,
                "updated_at": now,
            },
            "$setOnInsert": {
                **key,
                "status": "open",
                "answer": None,
                "credential_ref": None,
                "answered_at": None,
                "created_at": now,
            },
        },
        upsert=True,
    )
    return _strip_id(store.site_questions.find_one(key))


def get_site_question(
    store: Store, tenant_id: str, target_id: str, question_id: str,
) -> dict | None:
    return _strip_id(
        store.site_questions.find_one(
            {"tenant_id": tenant_id, "target_id": target_id, "question_id": question_id},
        ),
    )


def list_questions_by_target(
    store: Store,
    tenant_id: str,
    target_id: str,
    *,
    status: str | None = None,
) -> list[dict]:
    """The target's questionnaire, ordered by ``(order, question_id)``.
    Optionally filtered to one ``status`` (``open`` / ``answered`` / ``skipped``).
    Secret answers appear as a ``credential_ref`` pointer — never a value."""
    query: dict = {"tenant_id": tenant_id, "target_id": target_id}
    if status is not None:
        query["status"] = status
    cur = store.site_questions.find(query).sort(
        [("order", ASCENDING), ("question_id", ASCENDING)],
    )
    return [_strip_id(d) for d in cur]


def answer_site_question(
    store: Store,
    *,
    target_id: str,
    question_id: str,
    answer: str,
    tenant_id: str = DEFAULT_TENANT,
    label: str = "",
) -> dict | None:
    """Record the operator's answer. Returns the updated question, or ``None``
    if the question doesn't exist.

    For a ``secret`` question the value is written to the vault and the row
    keeps only the ``credential_ref`` pointer (``answer`` stays ``None``); for
    every other kind the value is stored inline. Either way ``status`` becomes
    ``answered``.
    """
    q = get_site_question(store, tenant_id, target_id, question_id)
    if q is None:
        return None

    fields: dict[str, Any] = {"status": "answered", "answered_at": _now()}
    if q["kind"] == "secret":
        # Raw value → vault; the row holds only the pointer. The vault ref is
        # the question_id (a slug), so re-answering replaces the stored value.
        credential_ref = put_secret(
            store,
            target_id=target_id,
            tenant_id=tenant_id,
            value=answer,
            ref=f"q-{question_id}",
            label=label or q.get("text", ""),
        )
        fields["credential_ref"] = credential_ref
        fields["answer"] = None
    else:
        fields["answer"] = answer
        fields["credential_ref"] = None

    return _update(store, tenant_id, target_id, question_id, fields)


def skip_site_question(
    store: Store,
    *,
    target_id: str,
    question_id: str,
    tenant_id: str = DEFAULT_TENANT,
) -> dict | None:
    """Mark a question skipped (the operator declined / it's not applicable)."""
    if get_site_question(store, tenant_id, target_id, question_id) is None:
        return None
    return _update(
        store, tenant_id, target_id, question_id,
        {"status": "skipped", "answer": None, "answered_at": _now()},
    )


def delete_site_question(
    store: Store, tenant_id: str, target_id: str, question_id: str,
) -> bool:
    """Delete a question. If it pointed at a vaulted secret, drop that too so
    the vault doesn't accrue orphans."""
    q = get_site_question(store, tenant_id, target_id, question_id)
    if q is None:
        return False
    if q.get("credential_ref"):
        delete_secret(store, q["credential_ref"])
    res = store.site_questions.delete_one(
        {"tenant_id": tenant_id, "target_id": target_id, "question_id": question_id},
    )
    return res.deleted_count == 1


def questionnaire_status(
    store: Store, tenant_id: str, target_id: str,
) -> dict:
    """Roll-up the questionnaire for the lifecycle/UI: total, answered, open,
    skipped, and how many *required* questions are still open (the gate for
    moving a target to ``configured``)."""
    qs = list_questions_by_target(store, tenant_id, target_id)
    return {
        "total": len(qs),
        "answered": sum(1 for q in qs if q["status"] == "answered"),
        "open": sum(1 for q in qs if q["status"] == "open"),
        "skipped": sum(1 for q in qs if q["status"] == "skipped"),
        "required_open": sum(
            1 for q in qs if q.get("required") and q["status"] == "open"
        ),
    }


def _update(
    store: Store, tenant_id: str, target_id: str, question_id: str, fields: dict,
) -> dict | None:
    store.site_questions.update_one(
        {"tenant_id": tenant_id, "target_id": target_id, "question_id": question_id},
        {"$set": {**fields, "updated_at": _now()}},
    )
    return get_site_question(store, tenant_id, target_id, question_id)
