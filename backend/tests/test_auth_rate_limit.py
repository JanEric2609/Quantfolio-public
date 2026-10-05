"""Tests for LoginTracker sliding-window rate limiting and bounded ChallengeStore."""
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from conftest import _memory_db
from fastapi import HTTPException

from app.foundation.core.security import hash_password
from app.foundation.models.entities import User
from app.foundation.auth import (
    CHALLENGE_STORE_MAX_SIZE,
    LOGIN_MAX_ATTEMPTS,
    LOGIN_WINDOW_SECONDS,
    ChallengeStore,
    LoginTracker,
    authenticate_password,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _make_user(db, username="alice", password="SecurePass1!"):
    user = User(username=username, password_hash=hash_password(password))
    db.add(user)
    db.commit()
    return user


# ---------------------------------------------------------------------------
# LoginTracker — unit tests (no DB)
# ---------------------------------------------------------------------------

class TestLoginTrackerUnit:
    def test_allows_attempts_within_limit(self):
        """4 failures for the same user should NOT trigger a block."""
        tracker = LoginTracker()
        for _ in range(LOGIN_MAX_ATTEMPTS - 1):
            tracker.record_failure("bob")
        assert not tracker.is_blocked("bob")

    def test_blocks_at_limit(self):
        """The 5th failure should trigger is_blocked."""
        tracker = LoginTracker()
        for _ in range(LOGIN_MAX_ATTEMPTS):
            tracker.record_failure("bob")
        assert tracker.is_blocked("bob")

    def test_prunes_old_attempts(self):
        """Attempts older than LOGIN_WINDOW_SECONDS are ignored."""
        tracker = LoginTracker()
        now = datetime.now(UTC)
        stale = now - timedelta(seconds=LOGIN_WINDOW_SECONDS + 1)
        # Inject 4 stale attempts + 1 fresh → still under limit
        tracker._attempts["bob"] = [stale, stale, stale, stale]
        tracker.record_failure("bob")  # 1 fresh
        assert not tracker.is_blocked("bob")

    def test_clear_removes_all_attempts(self):
        """clear() should reset the user back to zero attempts."""
        tracker = LoginTracker()
        for _ in range(LOGIN_MAX_ATTEMPTS):
            tracker.record_failure("bob")
        assert tracker.is_blocked("bob")
        tracker.clear("bob")
        assert not tracker.is_blocked("bob")

    def test_clear_nonexistent_user_is_safe(self):
        """clear() on a user with no record should not raise."""
        tracker = LoginTracker()
        tracker.clear("nobody")  # must not raise

    def test_users_are_independent(self):
        """Blocking one user must not affect another."""
        tracker = LoginTracker()
        for _ in range(LOGIN_MAX_ATTEMPTS):
            tracker.record_failure("alice")
        assert tracker.is_blocked("alice")
        assert not tracker.is_blocked("bob")

    def test_prune_updates_stored_list(self):
        """After pruning, the internal list should only contain fresh entries."""
        tracker = LoginTracker()
        now = datetime.now(UTC)
        stale = now - timedelta(seconds=LOGIN_WINDOW_SECONDS + 1)
        tracker._attempts["eve"] = [stale, stale, stale, now, now]
        tracker._prune("eve")
        assert len(tracker._attempts["eve"]) == 2

    def test_prune_keeps_attempts_just_inside_window(self):
        """An attempt at exactly the window edge should still be kept."""
        tracker = LoginTracker()
        now = datetime.now(UTC)
        edge = now - timedelta(seconds=LOGIN_WINDOW_SECONDS - 1)
        tracker._attempts["dan"] = [edge]
        result = tracker._prune("dan")
        assert len(result) == 1

    def test_prune_safe_for_unknown_user(self):
        """_prune on an unknown user returns empty list, no KeyError."""
        tracker = LoginTracker()
        result = tracker._prune("ghost")
        assert result == []


# ---------------------------------------------------------------------------
# ChallengeStore — unit tests (no DB)
# ---------------------------------------------------------------------------

class TestChallengeStoreUnit:
    def test_put_and_pop_roundtrip(self):
        store = ChallengeStore()
        store.put("abc", {"kind": "registration"})
        assert store.pop("abc") == {"kind": "registration"}

    def test_pop_unknown_key_returns_none(self):
        store = ChallengeStore()
        assert store.pop("missing") is None

    def test_pop_twice_returns_none_second_time(self):
        store = ChallengeStore()
        store.put("xyz", {"kind": "auth"})
        assert store.pop("xyz") == {"kind": "auth"}
        assert store.pop("xyz") is None

    def test_expired_challenge_returns_none(self):
        store = ChallengeStore()
        # Inject an already-expired entry
        store._memory["exp"] = (
            datetime.now(UTC) - timedelta(seconds=1),
            {"kind": "stale"},
        )
        assert store.pop("exp") is None

    def test_capacity_evicts_oldest(self):
        """After CHALLENGE_STORE_MAX_SIZE inserts, the store must not exceed the cap."""
        store = ChallengeStore()
        # Pre-fill to capacity
        for i in range(CHALLENGE_STORE_MAX_SIZE):
            store._memory[f"key_{i:04d}"] = (
                datetime.now(UTC) + timedelta(hours=1),
                {"i": i},
            )
        assert len(store._memory) == CHALLENGE_STORE_MAX_SIZE

        # Insert one more — should evict key_0000 (the oldest)
        store.put("over_capacity", {"kind": "overflow"})
        assert len(store._memory) == CHALLENGE_STORE_MAX_SIZE

        # Oldest key must be gone
        assert "key_0000" not in store._memory
        # Newest key must be present
        assert "over_capacity" in store._memory
        assert store._memory["over_capacity"] is not None

    def test_capacity_below_limit_allows_growth(self):
        store = ChallengeStore()
        for i in range(500):
            store._memory[f"k_{i}"] = (
                datetime.now(UTC) + timedelta(hours=1),
                {"i": i},
            )
        assert len(store._memory) == 500
        store.put("safe", {"kind": "ok"})
        assert len(store._memory) == 501

    def test_redis_fallback_not_used_when_redis_unavailable(self):
        """_redis() returns None when redis module is absent or unreachable,
        so put/pop must use the in-memory path transparently."""
        store = ChallengeStore()
        # _redis() already returns None in test environment — verify fallback works
        store.put("fallback-test", {"kind": "test"})
        result = store.pop("fallback-test")
        assert result == {"kind": "test"}

    def test_redis_client_not_called_when_redis_module_missing(self):
        """When _redis() returns None, put/pop fall back to in-memory storage."""
        store = ChallengeStore()
        with patch.object(store, "_redis", return_value=None):
            store.put("no-redis", {"kind": "mem"})
            assert store.pop("no-redis") == {"kind": "mem"}


# ---------------------------------------------------------------------------
# authenticate_password — integration tests (with DB)
# ---------------------------------------------------------------------------

class TestAuthenticatePassword:
    def test_success_clears_tracker(self):
        """A successful login must clear previous failures from the tracker."""
        db = _memory_db()
        _make_user(db, "alice", "Correct1!")

        # Ingest some failures first
        from app.foundation.auth import login_tracker

        login_tracker.clear("alice")
        for _ in range(LOGIN_MAX_ATTEMPTS - 1):
            login_tracker.record_failure("alice")
        assert not login_tracker.is_blocked("alice")

        user = authenticate_password(db, "alice", "Correct1!")
        assert user.username == "alice"
        # Tracker must be clean after success
        assert not login_tracker.is_blocked("alice")
        login_tracker.clear("alice")  # keep test isolation

    def test_failure_increments_tracker(self):
        """A bad-password attempt must record a failure."""
        db = _memory_db()
        _make_user(db, "alice", "Correct1!")
        from app.foundation.auth import login_tracker

        login_tracker.clear("alice")

        with pytest.raises(HTTPException) as exc:
            authenticate_password(db, "alice", "WrongPass!")
        assert exc.value.status_code == 401
        assert login_tracker._attempts.get("alice", [])  # non-empty
        login_tracker.clear("alice")

    def test_failure_for_nonexistent_user_increments_tracker(self):
        """Even attempts against unknown usernames should be tracked."""
        db = _memory_db()
        from app.foundation.auth import login_tracker

        login_tracker.clear("ghost")

        with pytest.raises(HTTPException) as exc:
            authenticate_password(db, "ghost", "any")
        assert exc.value.status_code == 401
        assert len(login_tracker._attempts.get("ghost", [])) >= 1
        login_tracker.clear("ghost")

    def test_blocks_after_limit(self):
        """After LOGIN_MAX_ATTEMPTS failures the endpoint must return 429."""
        db = _memory_db()
        _make_user(db, "alice", "Correct1!")
        from app.foundation.auth import login_tracker

        login_tracker.clear("alice")

        # Exhaust the limit
        for _ in range(LOGIN_MAX_ATTEMPTS):
            with pytest.raises(HTTPException) as exc:
                authenticate_password(db, "alice", "WrongPass!")
            assert exc.value.status_code == 401

        # Now it must be blocked
        with pytest.raises(HTTPException) as exc:
            authenticate_password(db, "alice", "Correct1!")
        assert exc.value.status_code == 429
        assert "Too many login attempts" in exc.value.detail
        login_tracker.clear("alice")

    def test_unblocks_after_window_passes(self):
        """When the oldest failure falls outside the window, user is unblocked."""
        db = _memory_db()
        _make_user(db, "alice", "Correct1!")
        from app.foundation.auth import login_tracker

        login_tracker.clear("alice")

        # Fill exactly to the limit with stale attempts that are just outside
        # the window — record_failure uses UTC now, so we need to manipulate
        # the internal list directly for the stale case.
        stale = datetime.now(UTC) - timedelta(seconds=LOGIN_WINDOW_SECONDS + 1)
        login_tracker._attempts["alice"] = [stale] * LOGIN_MAX_ATTEMPTS

        # The stale attempts should be pruned, so alice is no longer blocked
        assert not login_tracker.is_blocked("alice")

        user = authenticate_password(db, "alice", "Correct1!")
        assert user.username == "alice"
        login_tracker.clear("alice")

    def test_reset_via_success_after_some_failures(self):
        """Success after 3 failures should reset the counter (not just clear it)."""
        db = _memory_db()
        _make_user(db, "alice", "Correct1!")
        from app.foundation.auth import login_tracker

        login_tracker.clear("alice")

        # 3 failures
        for _ in range(3):
            with pytest.raises(HTTPException):
                authenticate_password(db, "alice", "WrongPass!")

        # Success resets
        authenticate_password(db, "alice", "Correct1!")

        # Now we should be able to have 5 new failures before blocking
        for _ in range(LOGIN_MAX_ATTEMPTS - 1):
            with pytest.raises(HTTPException):
                authenticate_password(db, "alice", "WrongPass!")
        assert not login_tracker.is_blocked("alice")

        # 5th failure blocks
        with pytest.raises(HTTPException):
            authenticate_password(db, "alice", "WrongPass!")
        assert login_tracker.is_blocked("alice")
        login_tracker.clear("alice")
