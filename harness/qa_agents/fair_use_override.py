"""Fair-use override — a harness UTILITY, not an agent tool.

The Persona QA epic (#616) settled on a "both" approach for exercising the
fair-use cooldown:

1. The ORGANIC path — ``priya`` sends a deliberate burst of ~8 emails and the
   Slice 2 sandbox's LOW thresholds (``fair_use_burst_threshold: 5``) trip the
   cooldown for real. That is what the agent loop does; no code here.

2. The FAST path — this module. It connects straight to the sandbox MongoDB
   and force-sets a user's ``fair_use.cooldown_until`` to a near-future time,
   so the *cooldown messaging* (the "agents are resting" wall the user sees)
   can be exercised in seconds without sending any mail.

It is NOT exposed to any persona as a tool — a persona must never be able to
mutate the system it is reviewing. It is a developer/operator convenience,
run by hand or from CI:

    python -m qa_agents.fair_use_override priya.raghunathan@example.com
    python -m qa_agents.fair_use_override --minutes 30 someone@example.com

The user is matched on ``registered_emails`` (sender-is-auth identity). Mongo
connection settings are reused from ``config.py`` (``QA_MONGODB_URL``).
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime, timedelta

from .config import Config

# Default lead time for the forced cooldown — far enough out that the wall is
# clearly active when the persona looks, short enough not to wedge the sandbox.
_DEFAULT_MINUTES = 60


def compute_cooldown_until(now: datetime, minutes: int) -> datetime:
    """Return the ``cooldown_until`` timestamp ``minutes`` after ``now``.

    Pure and unit-tested directly — the only non-trivial logic in this module.
    ``minutes`` must be positive; a non-positive value would set a cooldown in
    the past, which the backend treats as already expired.
    """
    if minutes <= 0:
        raise ValueError(f"minutes must be positive, got {minutes}")
    return now + timedelta(minutes=minutes)


def override_cooldown(
    collection,
    *,
    email: str,
    cooldown_until: datetime,
) -> int:
    """Force ``fair_use.cooldown_until`` on the user matching ``email``.

    ``collection`` is a pymongo ``users`` collection (or any object with a
    compatible ``update_one``) — injected so this is trivially testable with a
    mock. The user is matched on ``registered_emails``. Returns the number of
    documents matched (0 means no such user — the caller should treat that as
    an error).
    """
    result = collection.update_one(
        {"registered_emails": email.lower()},
        {"$set": {"fair_use.cooldown_until": cooldown_until}},
    )
    return int(getattr(result, "matched_count", 0))


def _users_collection(mongodb_url: str):
    """Open the sandbox MongoDB and return its ``users`` collection.

    Imported lazily so the rest of the harness (and its tests, which mock the
    collection) never need ``pymongo`` installed.
    """
    from pymongo import MongoClient

    client = MongoClient(mongodb_url, serverSelectionTimeoutMS=10000)
    db = client.get_default_database()
    return db.users


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint: force a fair-use cooldown on one user."""
    argv = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser(
        prog="qa_agents.fair_use_override",
        description=(
            "Force-set a sandbox user's fair-use cooldown so the cooldown "
            "messaging can be exercised without sending a burst of email. "
            "A harness utility — NOT an agent tool."
        ),
    )
    parser.add_argument(
        "email",
        help="Registered email of the user to put into cooldown.",
    )
    parser.add_argument(
        "--minutes",
        type=int,
        default=_DEFAULT_MINUTES,
        help=f"How far in the future to set the cooldown (default: {_DEFAULT_MINUTES}).",
    )
    args = parser.parse_args(argv)

    config = Config.from_env()
    try:
        cooldown_until = compute_cooldown_until(datetime.now(UTC), args.minutes)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    try:
        users = _users_collection(config.mongodb_url)
        matched = override_cooldown(
            users, email=args.email, cooldown_until=cooldown_until
        )
    except Exception as exc:  # noqa: BLE001 - report cleanly to the operator
        print(f"ERROR: could not set cooldown: {exc!r}", file=sys.stderr)
        return 1

    if matched == 0:
        print(
            f"ERROR: no user with registered email {args.email!r} "
            f"(checked {config.mongodb_url}).",
            file=sys.stderr,
        )
        return 1

    print(
        f"OK: {args.email} fair_use.cooldown_until set to "
        f"{cooldown_until.isoformat()} (in {args.minutes} min)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
