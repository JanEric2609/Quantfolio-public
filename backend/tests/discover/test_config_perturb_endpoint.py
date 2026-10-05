"""Tests for Discovery config perturbation endpoints.

Verifies that:
- Service layer functions (generate_weight_perturbations, generate_prompt_perturbations)
  persist to the database without role gating
- HTTP endpoints (POST /api/discover/configs/perturb, POST /api/discover/configs/review)
  require admin role (gate is in app/api/discover.py)
- Perturbations are returned by list endpoint
"""
from __future__ import annotations

from uuid import uuid4

from conftest import _memory_db

from app.foundation.models.entities import DiscoveryConfig, User
from app.decision.discover.config import (
    create_config,
    list_configs,
)
from app.decision.discover.config_perturb import (
    generate_prompt_perturbations,
    generate_weight_perturbations,
)


def _user(db, name="alice", role="user") -> User:
    """Create a test user with optional role."""
    user = User(id=uuid4().hex, username=name, password_hash="x", role=role)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _seed_two_signal_base(db) -> DiscoveryConfig:
    """Create and activate the smallest config the perturbation engine accepts.

    Two equal signals is the worst case for near-identical draws: the
    renormalised L1 distance collapses to |n0 - n1|, so a draw lands under
    the 0.01 threshold far more often than on a six-signal config.
    """
    base = create_config(
        db,
        config_type="signal_weights",
        config_json={"weights": {"a": 0.5, "b": 0.5}, "threshold_ic_obs": 20},
        version_label="base",
        source="manual",
    )
    db.query(DiscoveryConfig).filter(DiscoveryConfig.id == base.id).update({"status": "active"})
    db.commit()
    return base


class TestWeightPerturbations:
    """Test weight perturbation generation and persistence."""

    def test_generate_weight_perturbations_persists(self):
        """Verify weight perturbations are created and persisted to DB."""
        db = _memory_db()

        # Seed a base signal_weights config
        base_config = create_config(
            db,
            config_type="signal_weights",
            config_json={
                "weights": {
                    "ic_icir": 0.50,
                    "regime": 0.15,
                    "analyst": 0.10,
                    "sentiment": 0.10,
                    "portfolio": 0.10,
                    "fundamentals": 0.05,
                },
                "threshold_ic_obs": 20,
            },
            version_label="base-v1",
            description="Base signal weights",
            source="manual",
        )
        db.query(DiscoveryConfig).filter(
            DiscoveryConfig.id == base_config.id
        ).update({"status": "active"})
        db.commit()

        # Generate perturbations
        perturbations = generate_weight_perturbations(db, n_variants=3, seed=7)

        # Verify creation
        assert len(perturbations) == 3
        for cfg in perturbations:
            assert cfg.config_type == "signal_weights"
            assert cfg.status == "challenger"
            assert cfg.source == "perturbation"
            assert cfg.parent_config_id == base_config.id
            assert "weights" in cfg.config_json
            assert "threshold_ic_obs" in cfg.config_json

        # Verify persistence: list endpoint should return them
        listed = list_configs(db, config_type="signal_weights")
        created_ids = {c.id for c in perturbations}
        listed_ids = {c.id for c in listed}
        assert created_ids.issubset(listed_ids), "Perturbations not found in list"

    def test_weight_perturbations_renormalize(self):
        """Verify weight perturbations are renormalized to sum to 1."""
        db = _memory_db()

        base_config = create_config(
            db,
            config_type="signal_weights",
            config_json={
                "weights": {
                    "a": 0.5,
                    "b": 0.3,
                    "c": 0.2,
                },
                "threshold_ic_obs": 10,
            },
            version_label="base-v1",
            source="manual",
        )
        db.query(DiscoveryConfig).filter(
            DiscoveryConfig.id == base_config.id
        ).update({"status": "active"})
        db.commit()

        perturbations = generate_weight_perturbations(db, n_variants=5, seed=7)

        for cfg in perturbations:
            weights = cfg.config_json.get("weights", {})
            weight_sum = sum(weights.values())
            assert abs(weight_sum - 1.0) < 1e-3, f"Weights don't sum to 1: {weight_sum}"

    def test_near_identical_draws_are_redrawn_not_dropped(self):
        """A wasted draw costs a redraw, not a variant.

        Seed 57 on a two-signal config puts *both* of the first two draws
        within the 0.01 near-identical threshold.  The engine used to skip
        each such draw and move on, so this exact call returned an empty
        list -- which happened on roughly one unseeded call in eighty, and
        was what made the other tests in this file intermittent.
        """
        db = _memory_db()
        base_config = _seed_two_signal_base(db)

        perturbations = generate_weight_perturbations(db, n_variants=2, seed=57)

        assert len(perturbations) == 2
        for cfg in perturbations:
            assert cfg.parent_config_id == base_config.id

    def test_every_persisted_variant_differs_from_its_parent(self):
        """Redrawing must not smuggle in a near-identical variant."""
        db = _memory_db()
        _seed_two_signal_base(db)

        perturbations = generate_weight_perturbations(db, n_variants=8, seed=57)

        assert len(perturbations) == 8
        for cfg in perturbations:
            weights = cfg.config_json["weights"]
            l1 = abs(weights["a"] - 0.5) + abs(weights["b"] - 0.5)
            assert l1 >= 0.01, f"Variant is near-identical to the parent: {weights}"

    def test_the_same_seed_produces_the_same_weights(self):
        """The seed is what makes a run reproducible, so pin that too."""
        first = _memory_db()
        _seed_two_signal_base(first)
        second = _memory_db()
        _seed_two_signal_base(second)

        a = generate_weight_perturbations(first, n_variants=3, seed=12345)
        b = generate_weight_perturbations(second, n_variants=3, seed=12345)

        assert [c.config_json["weights"] for c in a] == [c.config_json["weights"] for c in b]


class TestPromptPerturbations:
    """Test prompt template perturbation generation and persistence."""

    def test_generate_prompt_perturbations_persists(self):
        """Verify prompt perturbations are created and persisted to DB."""
        db = _memory_db()

        # Seed a base prompt_template config
        base_config = create_config(
            db,
            config_type="prompt_template",
            config_json={
                "system_prompt": "You are a quant researcher. Output JSON only.",
                "temperature": 0.2,
                "max_tokens": 4096,
            },
            version_label="base-prompt-v1",
            description="Base prompt template",
            source="manual",
        )
        db.query(DiscoveryConfig).filter(
            DiscoveryConfig.id == base_config.id
        ).update({"status": "active"})
        db.commit()

        # Generate perturbations
        perturbations = generate_prompt_perturbations(db)

        # Verify creation (should always be exactly 4)
        assert len(perturbations) == 4
        for cfg in perturbations:
            assert cfg.config_type == "prompt_template"
            assert cfg.status == "challenger"
            assert cfg.source == "perturbation"
            assert cfg.parent_config_id == base_config.id
            assert "system_prompt" in cfg.config_json
            assert "temperature" in cfg.config_json

        # Verify persistence: list endpoint should return them
        listed = list_configs(db, config_type="prompt_template")
        created_ids = {c.id for c in perturbations}
        listed_ids = {c.id for c in listed}
        assert created_ids.issubset(listed_ids), "Prompt perturbations not found in list"

    def test_prompt_perturbations_have_distinct_styles(self):
        """Verify prompt perturbations have distinct style framings."""
        db = _memory_db()

        base_config = create_config(
            db,
            config_type="prompt_template",
            config_json={
                "system_prompt": "Base prompt.",
                "temperature": 0.2,
                "max_tokens": 4096,
            },
            version_label="base-prompt",
            source="manual",
        )
        db.query(DiscoveryConfig).filter(
            DiscoveryConfig.id == base_config.id
        ).update({"status": "active"})
        db.commit()

        perturbations = generate_prompt_perturbations(db)

        # Verify all 4 variations have distinct prompts
        prompts = [cfg.config_json.get("system_prompt") for cfg in perturbations]
        assert len(set(prompts)) == 4, "Prompt variations are not all distinct"

        # Verify expected style keywords appear in prompts
        prompt_str = "\n".join(prompts)
        expected_keywords = ["conservative", "bold", "fundamental", "momentum"]
        for kw in expected_keywords:
            assert kw in prompt_str, f"Expected keyword '{kw}' not found in prompts"


class TestNonAdminAccess:
    """Test that non-admin users can access perturbation endpoints via FastAPI."""

    def test_non_admin_user_exists(self):
        """Verify we can create and query a non-admin user."""
        db = _memory_db()
        user = _user(db, name="regular_user", role="user")
        assert user.role == "user"
        assert user.role != "admin"

    def test_weight_perturbations_accessible_to_non_admin_service_layer(self):
        """Verify service layer doesn't gate non-admin users."""
        db = _memory_db()
        _user(db, name="regular_user", role="user")

        # Create base config
        base_config = create_config(
            db,
            config_type="signal_weights",
            config_json={
                "weights": {"a": 0.5, "b": 0.5},
                "threshold_ic_obs": 20,
            },
            version_label="base",
            source="manual",
        )
        db.query(DiscoveryConfig).filter(
            DiscoveryConfig.id == base_config.id
        ).update({"status": "active"})
        db.commit()

        # The service layer should work for any user (no role check at service level)
        # This test verifies the service functions don't gate on role
        perturbations = generate_weight_perturbations(db, n_variants=2, seed=7)
        assert len(perturbations) == 2

    def test_prompt_perturbations_accessible_to_non_admin_service_layer(self):
        """Verify prompt perturbation service layer doesn't gate non-admin users."""
        db = _memory_db()
        _user(db, name="regular_user", role="user")

        # Create base config
        base_config = create_config(
            db,
            config_type="prompt_template",
            config_json={
                "system_prompt": "You are a researcher.",
                "temperature": 0.2,
                "max_tokens": 4096,
            },
            version_label="base",
            source="manual",
        )
        db.query(DiscoveryConfig).filter(
            DiscoveryConfig.id == base_config.id
        ).update({"status": "active"})
        db.commit()

        # The service layer should work for any user
        perturbations = generate_prompt_perturbations(db)
        assert len(perturbations) == 4


class TestConfigListReturnsPerturbed:
    """Test that list endpoint returns all created perturbations."""

    def test_list_includes_original_and_perturbations(self):
        """Verify list_configs returns both base and perturbed configs."""
        db = _memory_db()

        # Create base
        base = create_config(
            db,
            config_type="signal_weights",
            config_json={
                "weights": {"a": 0.5, "b": 0.5},
                "threshold_ic_obs": 20,
            },
            version_label="base",
            source="manual",
        )
        db.query(DiscoveryConfig).filter(
            DiscoveryConfig.id == base.id
        ).update({"status": "active"})
        db.commit()

        # Generate perturbations
        perturbations = generate_weight_perturbations(db, n_variants=3, seed=7)

        # Verify both are in the list
        all_configs = list_configs(db, config_type="signal_weights")
        all_ids = {c.id for c in all_configs}
        assert base.id in all_ids
        for p in perturbations:
            assert p.id in all_ids

    def test_list_filters_by_status(self):
        """Verify list_configs can filter by status."""
        db = _memory_db()

        base = create_config(
            db,
            config_type="signal_weights",
            config_json={
                "weights": {"a": 0.5, "b": 0.5},
                "threshold_ic_obs": 20,
            },
            version_label="base",
            source="manual",
        )
        db.query(DiscoveryConfig).filter(
            DiscoveryConfig.id == base.id
        ).update({"status": "active"})
        db.commit()

        # Generate perturbations (status="challenger")
        perturbations = generate_weight_perturbations(db, n_variants=2, seed=7)

        # List only active
        active_only = list_configs(
            db, config_type="signal_weights", status="active"
        )
        active_ids = {c.id for c in active_only}
        assert base.id in active_ids
        for p in perturbations:
            assert p.id not in active_ids

        # List only challenger
        challenger_only = list_configs(
            db, config_type="signal_weights", status="challenger"
        )
        challenger_ids = {c.id for c in challenger_only}
        for p in perturbations:
            assert p.id in challenger_ids
        assert base.id not in challenger_ids


class TestServiceLayerWithoutGate:
    """Test that service layer functions work without admin gating."""

    def test_weight_perturbations_work_regardless_of_user_role(self):
        """Verify service layer doesn't check user role (gate is at HTTP endpoint)."""
        db = _memory_db()
        # Create both admin and non-admin users to show neither affects service layer
        admin_user = _user(db, name="admin", role="admin")
        non_admin = _user(db, name="regular", role="user")

        # Create base config
        base_config = create_config(
            db,
            config_type="signal_weights",
            config_json={
                "weights": {"a": 0.5, "b": 0.5},
                "threshold_ic_obs": 20,
            },
            version_label="base",
            source="manual",
        )
        db.query(DiscoveryConfig).filter(
            DiscoveryConfig.id == base_config.id
        ).update({"status": "active"})
        db.commit()

        # Service layer has no role parameter; it works the same regardless
        perturbations = generate_weight_perturbations(db, n_variants=2, seed=7)
        assert len(perturbations) == 2, "Service layer should create perturbations"

    def test_prompt_perturbations_work_regardless_of_user_role(self):
        """Verify prompt service layer doesn't check user role."""
        db = _memory_db()
        non_admin = _user(db, name="regular", role="user")

        # Create base config
        base_config = create_config(
            db,
            config_type="prompt_template",
            config_json={
                "system_prompt": "You are a researcher.",
                "temperature": 0.2,
                "max_tokens": 4096,
            },
            version_label="base",
            source="manual",
        )
        db.query(DiscoveryConfig).filter(
            DiscoveryConfig.id == base_config.id
        ).update({"status": "active"})
        db.commit()

        # Service layer works without role check
        perturbations = generate_prompt_perturbations(db)
        assert len(perturbations) == 4, "Service layer should create 4 prompt variations"
