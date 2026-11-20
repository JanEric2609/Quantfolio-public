"""Tests for quant_factors: Fama-French data download + factor attribution."""
from __future__ import annotations

import io
import zipfile
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _make_ff3_zip_bytes() -> bytes:
    """Build a minimal Kenneth French FF3 CSV ZIP payload in-memory."""
    csv_content = (
        "F-F Research Data Factors Daily\n"
        ",Mkt-RF,SMB,HML,RF\n"
        "20220103,0.12,-0.05,0.08,0.001\n"
        "20220104,-0.23,0.10,-0.03,0.001\n"
        "20220105,0.35,0.07,0.12,0.001\n"
        "20220106,0.05,-0.01,0.04,0.001\n"
        "20220107,-0.15,0.03,-0.07,0.001\n"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("F-F_Research_Data_Factors_daily.CSV", csv_content)
    return buf.getvalue()


def _mock_httpx_response(content: bytes) -> MagicMock:
    resp = MagicMock()
    resp.content = content
    resp.raise_for_status = MagicMock()
    return resp


def _make_europe5_zip_bytes(include_missing_row: bool = False) -> bytes:
    """Build a Kenneth French Developed Europe 5-Factor CSV ZIP payload.

    Mirrors the real file's shape: Mkt-RF, SMB, HML, RMW, CMA, RF columns,
    optionally including a -99.99 missing-data sentinel row (Dartmouth's
    documented convention for a data gap).
    """
    lines = [
        "This file was created using the 202606 Bloomberg database.",
        "Missing data are indicated by -99.99.",
        "",
        "",
        ",Mkt-RF,SMB,HML,RMW,CMA,RF",
        "20220103,0.12,-0.05,0.08,0.03,-0.02,0.001",
        "20220104,-0.23,0.10,-0.03,0.01,0.04,0.001",
    ]
    if include_missing_row:
        lines.append("20220105,-99.99,-99.99,-99.99,-99.99,-99.99,0.001")
    lines += [
        "20220106,0.05,-0.01,0.04,-0.02,0.01,0.001",
        "20220107,-0.15,0.03,-0.07,0.05,-0.03,0.001",
    ]
    csv_content = "\n".join(lines) + "\n"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("Europe_5_Factors_Daily.csv", csv_content)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# list_factor_zoo
# ---------------------------------------------------------------------------

class TestListFactorZoo:
    def test_returns_list(self) -> None:
        from app.foundation.quant_factors import list_factor_zoo

        result = list_factor_zoo()
        assert isinstance(result, list)
        assert len(result) >= 1

    def test_each_entry_has_id(self) -> None:
        from app.foundation.quant_factors import list_factor_zoo

        for entry in list_factor_zoo():
            assert "id" in entry
            assert "name" in entry
            assert "factors" in entry


# ---------------------------------------------------------------------------
# get_ff3_returns — happy path (mocked HTTP + no on-disk cache)
# ---------------------------------------------------------------------------

class TestGetFf3Returns:
    def _patch_download(self, zip_bytes: bytes):
        """Context manager that patches httpx.get and forces a fresh download."""
        import unittest.mock as mock
        resp = _mock_httpx_response(zip_bytes)
        # Also patch Path.exists so the cache is always considered stale
        return mock.patch("httpx.get", return_value=resp)

    def test_happy_path_returns_completed_status(self, tmp_path) -> None:
        zip_bytes = _make_ff3_zip_bytes()
        resp = _mock_httpx_response(zip_bytes)
        import unittest.mock as mock

        with mock.patch("httpx.get", return_value=resp), \
             mock.patch("app.foundation.quant_factors._CACHE_DIR", tmp_path):
            from app.foundation import quant_factors
            # Force fresh download by pointing cache dir to tmp_path (no parquet)
            # Reload to pick up patched constant — but it's easier to patch _load_ff3
            with mock.patch.object(quant_factors, "_CACHE_DIR", tmp_path):
                result = quant_factors.get_ff3_returns()

        assert result["status"] == "completed"

    def test_factors_dict_has_mkt_rf_smb_hml(self, tmp_path) -> None:
        import unittest.mock as mock

        zip_bytes = _make_ff3_zip_bytes()
        resp = _mock_httpx_response(zip_bytes)

        with mock.patch("httpx.get", return_value=resp), \
             mock.patch("app.foundation.quant_factors._CACHE_DIR", tmp_path):
            from app.foundation import quant_factors
            with mock.patch.object(quant_factors, "_CACHE_DIR", tmp_path):
                result = quant_factors.get_ff3_returns()

        if result["status"] == "completed":
            factors = result["factors"]
            assert "mkt_rf" in factors
            assert "smb" in factors
            assert "hml" in factors

    def test_unavailable_when_http_fails(self, tmp_path) -> None:
        import unittest.mock as mock

        def _fail(*a, **kw):
            raise ConnectionError("network error")

        with mock.patch("httpx.get", side_effect=_fail), \
             mock.patch("app.foundation.quant_factors._CACHE_DIR", tmp_path):
            from app.foundation import quant_factors
            with mock.patch.object(quant_factors, "_CACHE_DIR", tmp_path):
                result = quant_factors.get_ff3_returns()

        assert result["status"] == "unavailable"

    def test_factor_values_are_floats(self, tmp_path) -> None:
        import unittest.mock as mock

        zip_bytes = _make_ff3_zip_bytes()
        resp = _mock_httpx_response(zip_bytes)

        with mock.patch("httpx.get", return_value=resp), \
             mock.patch("app.foundation.quant_factors._CACHE_DIR", tmp_path):
            from app.foundation import quant_factors
            with mock.patch.object(quant_factors, "_CACHE_DIR", tmp_path):
                result = quant_factors.get_ff3_returns()

        if result["status"] == "completed":
            for factor_name, series in result["factors"].items():
                for date_str, val in series.items():
                    assert isinstance(val, float), (
                        f"Factor {factor_name} has non-float value at {date_str}: {val!r}"
                    )


# ---------------------------------------------------------------------------
# Europe factor cutover (docs/adr/0007-fama-french-europe-factors.md):
# the loader must source Developed Europe data, not US, and must expose the
# full 5-factor set (mkt_rf, smb, hml, rmw, cma), not just 3.
# ---------------------------------------------------------------------------

class TestEuropeFactorCutover:
    def test_ff5_url_points_to_europe_not_us(self) -> None:
        from app.foundation import quant_factors

        assert "Europe" in quant_factors._FF5_URL
        assert "F-F_Research_Data" not in quant_factors._FF5_URL

    def test_momentum_url_points_to_europe_not_us(self) -> None:
        from app.foundation import quant_factors

        assert "Europe" in quant_factors._MOM_URL
        assert "F-F_Momentum_Factor" not in quant_factors._MOM_URL

    def test_load_ff3_exposes_full_five_factor_set(self, tmp_path) -> None:
        import unittest.mock as mock

        zip_bytes = _make_europe5_zip_bytes()
        resp = _mock_httpx_response(zip_bytes)

        with mock.patch("httpx.get", return_value=resp):
            from app.foundation import quant_factors
            with mock.patch.object(quant_factors, "_CACHE_DIR", tmp_path):
                result = quant_factors.get_ff3_returns()

        assert result["status"] == "completed"
        for factor in ("mkt_rf", "smb", "hml", "rmw", "cma"):
            assert factor in result["factors"], f"missing {factor} in {result['factors'].keys()}"

    def test_missing_data_sentinel_masked_not_poisoned(self, tmp_path) -> None:
        """Dartmouth marks a missing observation as -99.99; that row's values
        must not enter the series as -0.9999 after percent-to-decimal scaling."""
        import unittest.mock as mock

        zip_bytes = _make_europe5_zip_bytes(include_missing_row=True)
        resp = _mock_httpx_response(zip_bytes)

        with mock.patch("httpx.get", return_value=resp):
            from app.foundation import quant_factors
            with mock.patch.object(quant_factors, "_CACHE_DIR", tmp_path):
                result = quant_factors.get_ff3_returns()

        assert result["status"] == "completed"
        for factor in ("mkt_rf", "smb", "hml", "rmw", "cma"):
            values = result["factors"][factor].values()
            assert all(v != -0.9999 for v in values), f"{factor} contains unmasked sentinel"

    def test_cache_filenames_are_europe_scoped(self, tmp_path) -> None:
        """Cache files must use Europe-scoped names so a stale US-cache file
        left over from before the cutover is never accidentally served."""
        import unittest.mock as mock

        zip_bytes = _make_europe5_zip_bytes()
        resp = _mock_httpx_response(zip_bytes)

        with mock.patch("httpx.get", return_value=resp):
            from app.foundation import quant_factors
            with mock.patch.object(quant_factors, "_CACHE_DIR", tmp_path):
                quant_factors.get_ff3_returns()

        assert (tmp_path / "ff5_europe.parquet").exists()
        assert not (tmp_path / "ff3.parquet").exists()


# ---------------------------------------------------------------------------
# get_momentum_returns
# ---------------------------------------------------------------------------

def _make_momentum_zip_bytes() -> bytes:
    """Build a minimal Kenneth French momentum-factor CSV ZIP payload in-memory."""
    csv_content = (
        "F-F Momentum Factor Daily\n"
        ",Mom   \n"
        "20220103,0.15\n"
        "20220104,-0.08\n"
        "20220105,0.22\n"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("F-F_Momentum_Factor_daily.CSV", csv_content)
    return buf.getvalue()


class TestGetMomentumReturns:
    def test_happy_path_returns_completed_status(self, tmp_path) -> None:
        import unittest.mock as mock

        zip_bytes = _make_momentum_zip_bytes()
        resp = _mock_httpx_response(zip_bytes)

        with mock.patch("httpx.get", return_value=resp):
            from app.foundation import quant_factors
            with mock.patch.object(quant_factors, "_CACHE_DIR", tmp_path):
                result = quant_factors.get_momentum_returns()

        assert result["status"] == "completed"
        assert "mom" in result["factors"]

    def test_unavailable_when_http_fails(self, tmp_path) -> None:
        import unittest.mock as mock

        def _fail(*a, **kw):
            raise ConnectionError("network error")

        with mock.patch("httpx.get", side_effect=_fail):
            from app.foundation import quant_factors
            with mock.patch.object(quant_factors, "_CACHE_DIR", tmp_path):
                result = quant_factors.get_momentum_returns()

        assert result["status"] == "unavailable"


# ---------------------------------------------------------------------------
# compute_factor_attribution
# ---------------------------------------------------------------------------

class TestComputeFactorAttribution:
    def _make_returns(self, n: int = 60, seed: int = 0) -> list[float]:
        rng = np.random.default_rng(seed)
        return rng.normal(0.001, 0.01, n).tolist()

    def test_returns_completed_status(self) -> None:
        from app.foundation.quant_factors import compute_factor_attribution

        n = 60
        port = self._make_returns(n, seed=1)
        factors = {
            "mkt_rf": self._make_returns(n, seed=2),
            "smb": self._make_returns(n, seed=3),
            "hml": self._make_returns(n, seed=4),
        }
        result = compute_factor_attribution(port, factors)
        assert result["status"] == "completed"

    def test_result_has_alpha_r_squared_exposures(self) -> None:
        from app.foundation.quant_factors import compute_factor_attribution

        n = 60
        port = self._make_returns(n, seed=10)
        factors = {
            "mkt_rf": self._make_returns(n, seed=11),
            "smb": self._make_returns(n, seed=12),
        }
        result = compute_factor_attribution(port, factors)
        assert "alpha_daily" in result
        assert "r_squared" in result
        assert "exposures" in result

    def test_r_squared_bounded(self) -> None:
        from app.foundation.quant_factors import compute_factor_attribution

        n = 80
        mkt = self._make_returns(n, seed=20)
        # Portfolio is a noisy version of market
        port = [m + 0.001 * np.random.default_rng(99).normal() for m in mkt]
        result = compute_factor_attribution(port, {"mkt_rf": mkt})
        if result["status"] == "completed":
            assert 0.0 <= result["r_squared"] <= 1.0

    def test_exposures_keys_match_factor_names(self) -> None:
        from app.foundation.quant_factors import compute_factor_attribution

        n = 50
        port = self._make_returns(n, seed=5)
        factor_names = ["mkt_rf", "smb", "hml"]
        factors = {f: self._make_returns(n, seed=i + 30) for i, f in enumerate(factor_names)}
        result = compute_factor_attribution(port, factors, factor_names=factor_names)
        if result["status"] == "completed":
            assert set(result["exposures"].keys()) == set(factor_names)

    def test_unavailable_when_too_few_observations(self) -> None:
        from app.foundation.quant_factors import compute_factor_attribution

        port = [0.01, 0.02]
        factors = {"mkt_rf": [0.01, 0.02]}
        result = compute_factor_attribution(port, factors)
        assert result["status"] == "unavailable"

    def test_perfect_factor_gives_r_squared_one(self) -> None:
        """If portfolio = 2 * mkt_rf (plus intercept), R² should be 1."""
        from app.foundation.quant_factors import compute_factor_attribution

        n = 60
        rng = np.random.default_rng(42)
        mkt = rng.normal(0.001, 0.01, n).tolist()
        port = [2.0 * m for m in mkt]
        result = compute_factor_attribution(port, {"mkt_rf": mkt})
        if result["status"] == "completed":
            assert result["r_squared"] > 0.99


# ---------------------------------------------------------------------------
# newey_west_se
# ---------------------------------------------------------------------------


class TestNeweyWestSE:
    def test_returns_expected_keys(self) -> None:
        import numpy as np

        from app.foundation.quant_factors import newey_west_se

        n = 50
        residuals = np.random.default_rng(42).normal(0, 0.01, n).tolist()
        X = np.column_stack([np.ones(n), np.random.default_rng(43).normal(0, 1, n)])
        result = newey_west_se(residuals, X)
        assert "hac_cov" in result
        assert "hac_se" in result
        assert "lags" in result
        assert "n" in result

    def test_hac_se_non_negative(self) -> None:
        import numpy as np

        from app.foundation.quant_factors import newey_west_se

        n = 100
        residuals = np.random.default_rng(42).normal(0, 0.01, n).tolist()
        X = np.column_stack([np.ones(n), np.random.default_rng(43).normal(0, 1, n)])
        result = newey_west_se(residuals, X)
        for se in result["hac_se"]:
            assert se >= 0

    def test_hac_cov_symmetric(self) -> None:
        import numpy as np

        from app.foundation.quant_factors import newey_west_se

        n = 80
        residuals = np.random.default_rng(42).normal(0, 0.01, n).tolist()
        X = np.column_stack([np.ones(n), np.random.default_rng(43).normal(0, 1, n)])
        result = newey_west_se(residuals, X)
        cov = np.array(result["hac_cov"])
        assert np.allclose(cov, cov.T)

    def test_single_observation_returns_none(self) -> None:
        from app.foundation.quant_factors import newey_west_se

        result = newey_west_se([0.01], [[1.0, 0.5]])
        assert result["hac_cov"] is None
        assert result["hac_se"] is None
        assert result["n"] == 1

    def test_custom_lags(self) -> None:
        import numpy as np

        from app.foundation.quant_factors import newey_west_se

        n = 50
        residuals = np.random.default_rng(42).normal(0, 0.01, n).tolist()
        X = np.column_stack([np.ones(n), np.random.default_rng(43).normal(0, 1, n)])
        result = newey_west_se(residuals, X, lags=5)
        assert result["lags"] == 5


# ---------------------------------------------------------------------------
# get_factor_definitions
# ---------------------------------------------------------------------------


class TestGetFactorDefinitions:
    def test_returns_list(self) -> None:
        from app.foundation.quant_factors import get_factor_definitions

        result = get_factor_definitions()
        assert isinstance(result, list)
        assert len(result) > 0

    def test_each_has_name_and_category(self) -> None:
        from app.foundation.quant_factors import get_factor_definitions

        for factor in get_factor_definitions():
            assert "name" in factor
            assert "category" in factor
            assert "formula" in factor


# ---------------------------------------------------------------------------
# compute_factor
# ---------------------------------------------------------------------------


class TestComputeFactor:
    def _make_prices(self, n: int = 300) -> pd.DataFrame:
        rng = np.random.default_rng(42)
        dates = pd.date_range("2023-01-01", periods=n, freq="B")
        prices = 100.0 * np.cumprod(1 + rng.normal(0.0005, 0.01, n))
        return pd.DataFrame({"close": prices}, index=dates)

    def test_rsi_14_bounded(self) -> None:
        from app.foundation.quant_factors import compute_factor

        prices = self._make_prices(300)
        rsi = compute_factor("rsi_14", prices)
        valid = rsi.dropna()
        assert (valid >= 0).all()
        assert (valid <= 100).all()

    def test_volatility_21d_non_negative(self) -> None:
        from app.foundation.quant_factors import compute_factor

        prices = self._make_prices(300)
        vol = compute_factor("volatility_21d", prices)
        valid = vol.dropna()
        assert (valid >= 0).all()

    def test_unknown_factor_raises(self) -> None:
        from app.foundation.quant_factors import compute_factor

        prices = self._make_prices(100)
        with pytest.raises(ValueError, match="Unknown factor"):
            compute_factor("nonexistent_factor", prices)

    def test_volume_21d_mean_with_missing_column(self) -> None:
        from app.foundation.quant_factors import compute_factor

        prices = self._make_prices(100)
        result = compute_factor("volume_21d_mean", prices)
        assert result.isna().all()

    def test_value_ep_with_missing_pe_ratio(self) -> None:
        from app.foundation.quant_factors import compute_factor

        prices = self._make_prices(100)
        result = compute_factor("value_ep", prices)
        assert result.isna().all()


class TestDateAlignedFactorAttribution:
    """Ken French factor files lag the price history by a month or more.

    Pairing a return series ending today with a factor series ending weeks
    earlier by tail position regresses each return on a different day's
    factors. ``compute_factor_attribution_by_date`` joins on the date instead.
    """

    @staticmethod
    def _lagged_fixture():
        import numpy as np
        from datetime import date, timedelta

        rng = np.random.default_rng(11)
        days = [(date(2025, 1, 1) + timedelta(days=i)).isoformat() for i in range(120)]
        mkt = rng.normal(0.0, 0.01, len(days))
        returns = [float(1.5 * m) for m in mkt]  # true market beta: 1.5
        factors_by_date = {"mkt_rf": {d: float(m) for d, m in zip(days[:-20], mkt[:-20])}}
        return returns, days, factors_by_date

    def test_tail_alignment_on_lagged_factors_loses_the_beta(self):
        from app.foundation.quant_factors import compute_factor_attribution

        returns, _days, factors_by_date = self._lagged_fixture()
        tail = compute_factor_attribution(returns, {"mkt_rf": list(factors_by_date["mkt_rf"].values())})
        assert tail["status"] == "completed"
        assert abs(tail["exposures"]["mkt_rf"] - 1.5) > 0.5

    def test_date_join_recovers_the_beta(self):
        from app.foundation.quant_factors import compute_factor_attribution_by_date

        returns, days, factors_by_date = self._lagged_fixture()
        result = compute_factor_attribution_by_date(returns, days, factors_by_date)
        assert result["status"] == "completed"
        assert result["exposures"]["mkt_rf"] == pytest.approx(1.5, abs=1e-9)
        assert result["n_observations"] == 100
        assert result["aligned_dates"][-1] == days[-21]

    def test_length_mismatch_raises(self):
        from app.foundation.quant_factors import align_returns_to_factors

        with pytest.raises(ValueError):
            align_returns_to_factors([0.01, 0.02], ["2025-01-01"], {"mkt_rf": {}})
