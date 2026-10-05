"""Tests for the lab's purge + embargo walk-forward splits (app.lab.quant_lab.cv)."""
import numpy as np
import pytest

from app.lab.quant_lab.cv import purged_embargo_splits


def test_purged_embargo_splits_rejects_too_few_folds():
    with pytest.raises(ValueError, match="n_folds >= 5"):
        purged_embargo_splits(1000, n_folds=3)


def test_purged_embargo_splits_rejects_too_small_a_panel():
    with pytest.raises(ValueError, match="too small"):
        purged_embargo_splits(10, n_folds=5, purge_days=5)


def test_purged_embargo_splits_produces_chronological_disjoint_test_blocks():
    folds = purged_embargo_splits(1000, n_folds=5, purge_days=5, embargo_frac=0.01)

    assert len(folds) == 5
    for i in range(len(folds) - 1):
        assert folds[i].test_positions[-1] < folds[i + 1].test_positions[0]
        # No overlap between consecutive test blocks.
        assert not set(folds[i].test_positions) & set(folds[i + 1].test_positions)


def test_purged_embargo_splits_purge_gap_respected():
    purge_days = 7
    folds = purged_embargo_splits(1000, n_folds=5, purge_days=purge_days, embargo_frac=0.01)

    for f in folds:
        if len(f.train_positions) == 0:
            continue
        gap = f.test_positions[0] - f.train_positions[-1]
        assert gap >= purge_days


def test_purged_embargo_splits_embargo_excludes_rows_from_later_training():
    folds = purged_embargo_splits(1000, n_folds=5, purge_days=5, embargo_frac=0.05)

    for earlier_idx in range(len(folds) - 1):
        test_end = folds[earlier_idx].test_positions[-1] + 1  # exclusive bound, matches internal `test_end`
        embargo_end = test_end + max(1, int(round(0.05 * 1000)))
        for later_fold in folds[earlier_idx + 1:]:
            embargoed_rows = np.arange(test_end, min(embargo_end, 1000))
            assert not set(later_fold.train_positions.tolist()) & set(embargoed_rows.tolist())


def test_purged_embargo_splits_train_never_reaches_into_or_past_test_block():
    folds = purged_embargo_splits(1000, n_folds=5, purge_days=5, embargo_frac=0.01)

    for f in folds:
        if len(f.train_positions) == 0:
            continue
        assert f.train_positions[-1] < f.test_positions[0]
