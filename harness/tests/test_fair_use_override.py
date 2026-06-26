"""Tests for the fair-use override utility — pure logic + a mocked Mongo call.

No real MongoDB and no pymongo import: ``override_cooldown`` takes the
collection as an argument, so a mock stands in for the ``users`` collection.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from qa_agents.fair_use_override import compute_cooldown_until, override_cooldown


# --- compute_cooldown_until ------------------------------------------------
def test_compute_cooldown_until_adds_minutes():
    now = datetime(2026, 5, 19, 12, 0, 0, tzinfo=UTC)
    assert compute_cooldown_until(now, 60) == now + timedelta(minutes=60)


def test_compute_cooldown_until_is_in_the_future():
    now = datetime.now(UTC)
    assert compute_cooldown_until(now, 30) > now


def test_compute_cooldown_until_rejects_non_positive_minutes():
    now = datetime.now(UTC)
    for bad in (0, -1, -60):
        with pytest.raises(ValueError):
            compute_cooldown_until(now, bad)


# --- override_cooldown -----------------------------------------------------
def test_override_cooldown_sets_field_on_matched_user():
    collection = MagicMock()
    collection.update_one.return_value = MagicMock(matched_count=1)
    cooldown_until = datetime(2026, 5, 19, 13, 0, 0, tzinfo=UTC)

    matched = override_cooldown(
        collection,
        email="Priya.Raghunathan@example.com",
        cooldown_until=cooldown_until,
    )

    assert matched == 1
    collection.update_one.assert_called_once_with(
        # email is lower-cased to match registered_emails storage.
        {"registered_emails": "priya.raghunathan@example.com"},
        {"$set": {"fair_use.cooldown_until": cooldown_until}},
    )


def test_override_cooldown_returns_zero_when_no_user_matched():
    collection = MagicMock()
    collection.update_one.return_value = MagicMock(matched_count=0)

    matched = override_cooldown(
        collection,
        email="nobody@example.com",
        cooldown_until=datetime.now(UTC),
    )

    assert matched == 0


def test_override_cooldown_tolerates_result_without_matched_count():
    # Defensive: a result object lacking matched_count is treated as 0.
    collection = MagicMock()
    collection.update_one.return_value = object()

    matched = override_cooldown(
        collection,
        email="someone@example.com",
        cooldown_until=datetime.now(UTC),
    )

    assert matched == 0
