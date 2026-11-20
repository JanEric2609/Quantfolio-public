"""Tests for config source-of-truth staleness (Phase 5,
unified-portfolio-engine-implementation.md).

Acceptance: a row seeded from (or last approved against) the current code
default is not stale; once the code default changes, it is — until the row
is re-approved via activate_config.
"""
from unittest.mock import patch

import pytest
from conftest import _memory_db

from app.decision.discover.config import (
    CODE_DEFAULTS,
    activate_config,
    code_default_hash,
    create_config,
    get_or_seed_active_config,
    is_config_stale,
    sync_config_to_default,
)


def test_seeded_config_is_not_stale():
    db = _memory_db()
    cfg = get_or_seed_active_config(db, "signal_weights")
    assert cfg.code_default_hash == code_default_hash("signal_weights")
    assert is_config_stale(cfg) is False


def test_config_becomes_stale_when_code_default_changes():
    db = _memory_db()
    cfg = get_or_seed_active_config(db, "universe")
    assert is_config_stale(cfg) is False

    changed_default = {**CODE_DEFAULTS["universe"], "universe_max_size": 999}
    with patch.dict(CODE_DEFAULTS, {"universe": changed_default}):
        assert is_config_stale(cfg) is True

    # Reverting the code default (patch.dict context exit) clears staleness
    # again — this is a live hash comparison, not a one-time flag.
    assert is_config_stale(cfg) is False


def test_manual_never_promoted_challenger_is_never_flagged_stale():
    """A fresh manual challenger has code_default_hash=None (never reviewed
    against a code default yet) — that must not render as "stale", or every
    brand-new challenger would show the badge."""
    db = _memory_db()
    cfg = create_config(db, config_type="prompt_template", config_json={"system_prompt": "custom"})
    assert cfg.code_default_hash is None
    assert is_config_stale(cfg) is False


def test_activate_config_clears_staleness():
    db = _memory_db()
    get_or_seed_active_config(db, "universe")  # seeds+activates the system default
    challenger = create_config(db, config_type="universe", config_json={"universe_max_size": 300})
    assert is_config_stale(challenger) is False  # never reviewed, not flagged

    changed_default = {**CODE_DEFAULTS["universe"], "universe_max_size": 999}
    with patch.dict(CODE_DEFAULTS, {"universe": changed_default}):
        activated = activate_config(db, challenger.id)
        assert activated.code_default_hash == code_default_hash("universe")
        assert is_config_stale(activated) is False


def test_unknown_config_type_hash_is_none():
    assert code_default_hash("not_a_real_type") is None


def test_sync_config_to_default_clears_staleness_and_updates_payload():
    """The active row's config_json/version_label/code_default_hash must be
    overwritten to match the *new* code default, not just have its stale
    flag silenced — a one-shot manual DB edit is exactly what this endpoint
    replaces."""
    db = _memory_db()
    active = get_or_seed_active_config(db, "universe")
    original_payload = dict(active.config_json)

    changed_default = {**CODE_DEFAULTS["universe"], "universe_max_size": 999}
    with patch.dict(CODE_DEFAULTS, {"universe": changed_default}):
        assert is_config_stale(active) is True

        synced = sync_config_to_default(db, active.id)

        assert synced.config_json == changed_default
        assert synced.config_json != original_payload
        assert synced.code_default_hash == code_default_hash("universe")
        assert is_config_stale(synced) is False


def test_sync_config_to_default_unknown_id_raises():
    db = _memory_db()
    with pytest.raises(ValueError):
        sync_config_to_default(db, "not-a-real-id")


def test_sync_config_to_default_unknown_config_type_raises():
    db = _memory_db()
    cfg = create_config(db, config_type="custom_manual_type", config_json={"foo": "bar"})
    with pytest.raises(ValueError):
        sync_config_to_default(db, cfg.id)
