"""Restricted expression DSL for AlphaCrafter factor formulas.

Factor formulas (from the seed pool **and** LLM proposals) are evaluated over a
cross-sectional *panel*: a mapping ``{variable_name -> DataFrame}`` where each
DataFrame is indexed by date (rows) with symbols as columns. A formula evaluates
to a single ``DataFrame`` of factor values aligned to that same (date x symbol)
grid.

Safety is the whole point of this module: formulas are parsed with :mod:`ast`
and evaluated by an explicit tree-walking interpreter that only permits a fixed
whitelist of variable names, operators and functions. Anything outside the
whitelist — attribute access (``x.__class__``), dunders, imports, subscripting,
comprehensions, lambdas, arbitrary calls — is rejected with
:class:`FactorDSLError`. There is **no** ``eval``/``exec`` anywhere.

Example::

    panel = {"close": close_df, "volume": volume_df}
    values = evaluate("rank(delta(close, 5))", panel)
"""

from __future__ import annotations

import ast
from typing import Callable

import numpy as np
import pandas as pd

# Variables a formula may reference. These are the only names the parser accepts
# as bare identifiers; the caller is responsible for populating the panel.
WHITELISTED_VARIABLES: frozenset[str] = frozenset(
    {
        "open",
        "high",
        "low",
        "close",
        "volume",
        "pe_ratio",
        "market_cap",
        "roe",
        # WRDS/JKP factor characteristics (gvkey-verified) -- see
        # app.lab.alphacrafter.panel.WRDS_FACTOR_FIELDS.
        "me",
        "mom_12_1",
        "be_me",
        "gp_at",
        "at_gr1",
        # IBES estimate-revision signal (ticker-matched, no verified
        # crosswalk) -- see app.lab.alphacrafter.panel.IBES_ESTIMATE_FIELDS.
        "sue",
        "revision_momentum",
        "dispersion",
        # SEC insider-trading signal (ticker-matched) -- see
        # app.lab.alphacrafter.panel.INSIDER_SIGNAL_FIELDS.
        "cluster_buy_score",
        "net_insider_flow_usd",
    }
)


class FactorDSLError(ValueError):
    """Raised when a formula is syntactically invalid or uses a disallowed construct."""


# --- Function implementations -------------------------------------------------
#
# Cross-sectional ops act per-row (axis=1, across symbols on a given date).
# Time-series ops act per-column (down the time axis for each symbol).


def _rank(x: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional percentile rank across symbols, per date."""
    return x.rank(axis=1, pct=True)


def _zscore(x: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional z-score across symbols, per date."""
    mean = pd.Series(x.mean(axis=1))
    std = pd.Series(x.std(axis=1))
    return x.sub(mean, axis=0).div(std.replace(0.0, np.nan), axis=0)


def _ensure_frame(value: pd.DataFrame | pd.Series | np.ndarray) -> pd.DataFrame:
    """Narrow a pandas/numpy result to a DataFrame without changing values."""
    if isinstance(value, pd.DataFrame):
        return value
    if isinstance(value, pd.Series):
        return value.to_frame()
    return pd.DataFrame(value)


def _rolling_mean(x: pd.DataFrame, n: float) -> pd.DataFrame:
    return _ensure_frame(x.rolling(int(n), min_periods=1).mean())


def _rolling_std(x: pd.DataFrame, n: float) -> pd.DataFrame:
    return _ensure_frame(x.rolling(int(n), min_periods=2).std())


def _rolling_sum(x: pd.DataFrame, n: float) -> pd.DataFrame:
    return _ensure_frame(x.rolling(int(n), min_periods=1).sum())


def _shift(x: pd.DataFrame, n: float) -> pd.DataFrame:
    return _ensure_frame(x.shift(int(n)))


def _delta(x: pd.DataFrame, n: float) -> pd.DataFrame:
    return _ensure_frame(x - x.shift(int(n)))


def _log(x: pd.DataFrame) -> pd.DataFrame:
    # Guard against log of non-positive values -> NaN rather than -inf/complex.
    safe = x.where(x > 0)
    return _ensure_frame(np.log(safe))


def _abs(x: pd.DataFrame) -> pd.DataFrame:
    return _ensure_frame(x.abs())


def _ts_min(x: pd.DataFrame, n: float) -> pd.DataFrame:
    return _ensure_frame(x.rolling(int(n), min_periods=1).min())


def _ts_max(x: pd.DataFrame, n: float) -> pd.DataFrame:
    return _ensure_frame(x.rolling(int(n), min_periods=1).max())


def _corr(x: pd.DataFrame, y: pd.DataFrame, n: float) -> pd.DataFrame:
    """Rolling time-series correlation between two panels, per symbol."""
    return _ensure_frame(x.rolling(int(n), min_periods=2).corr(y))


# (function name) -> (callable, expected positional arity)
WHITELISTED_FUNCTIONS: dict[str, tuple[Callable[..., pd.DataFrame], int]] = {
    "rank": (_rank, 1),
    "zscore": (_zscore, 1),
    "rolling_mean": (_rolling_mean, 2),
    "rolling_std": (_rolling_std, 2),
    "rolling_sum": (_rolling_sum, 2),
    "shift": (_shift, 2),
    "delta": (_delta, 2),
    "log": (_log, 1),
    "abs": (_abs, 1),
    "ts_min": (_ts_min, 2),
    "ts_max": (_ts_max, 2),
    "corr": (_corr, 3),
}

# AST node -> numpy/pandas binary operator implementation.
_BIN_OPS: dict[type[ast.operator], Callable] = {
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b,
}

_CMP_OPS: dict[type[ast.cmpop], Callable] = {
    ast.Lt: lambda a, b: a < b,
    ast.LtE: lambda a, b: a <= b,
    ast.Gt: lambda a, b: a > b,
    ast.GtE: lambda a, b: a >= b,
    ast.Eq: lambda a, b: a == b,
    ast.NotEq: lambda a, b: a != b,
}


def validate(formula: str) -> None:
    """Parse and structurally validate a formula without evaluating it.

    Raises :class:`FactorDSLError` if the formula references unknown names,
    calls non-whitelisted functions, or uses any disallowed syntax construct.
    """
    try:
        tree = ast.parse(formula, mode="eval")
    except SyntaxError as exc:
        raise FactorDSLError(f"Syntax error: {exc}") from exc
    _validate_node(tree.body)


def evaluate(formula: str, panel: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Evaluate ``formula`` over ``panel`` and return a (date x symbol) DataFrame.

    Args:
        formula: A DSL expression over whitelisted variables/functions.
        panel: Mapping of variable name -> DataFrame (index=date, columns=symbols).

    Raises:
        FactorDSLError: If the formula is invalid, unsafe, or references a
            variable that is not present in ``panel``.
    """
    try:
        tree = ast.parse(formula, mode="eval")
    except SyntaxError as exc:
        raise FactorDSLError(f"Syntax error: {exc}") from exc

    _validate_node(tree.body)
    result = _eval_node(tree.body, panel)
    if not isinstance(result, pd.DataFrame):
        # A bare constant or comparison can collapse to a scalar/Series; broadcast
        # it onto the panel grid so callers always receive a DataFrame.
        reference = next(iter(panel.values()))
        result = pd.DataFrame(result, index=reference.index, columns=reference.columns)
    return result


# --- Validation ---------------------------------------------------------------


def _validate_node(node: ast.AST, depth: int = 1, max_depth: int = 6) -> None:
    if depth > max_depth:
        raise FactorDSLError(f"Expression exceeds maximum nesting depth of {max_depth}")

    if isinstance(node, ast.BinOp):
        if type(node.op) not in _BIN_OPS:
            raise FactorDSLError(f"Operator not allowed: {type(node.op).__name__}")
        _validate_node(node.left, depth=depth + 1, max_depth=max_depth)
        _validate_node(node.right, depth=depth + 1, max_depth=max_depth)
        return

    if isinstance(node, ast.UnaryOp):
        if not isinstance(node.op, (ast.UAdd, ast.USub)):
            raise FactorDSLError(f"Unary operator not allowed: {type(node.op).__name__}")
        _validate_node(node.operand, depth=depth + 1, max_depth=max_depth)
        return

    if isinstance(node, ast.Compare):
        if len(node.ops) != 1 or len(node.comparators) != 1:
            raise FactorDSLError("Chained comparisons are not allowed")
        if type(node.ops[0]) not in _CMP_OPS:
            raise FactorDSLError(f"Comparison not allowed: {type(node.ops[0]).__name__}")
        _validate_node(node.left, depth=depth + 1, max_depth=max_depth)
        _validate_node(node.comparators[0], depth=depth + 1, max_depth=max_depth)
        return

    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name):
            raise FactorDSLError("Only direct calls to whitelisted functions are allowed")
        name = node.func.id
        if name not in WHITELISTED_FUNCTIONS:
            raise FactorDSLError(f"Function not allowed: {name}")
        if node.keywords:
            raise FactorDSLError("Keyword arguments are not allowed")
        _, arity = WHITELISTED_FUNCTIONS[name]
        if len(node.args) != arity:
            raise FactorDSLError(f"{name}() expects {arity} argument(s), got {len(node.args)}")

        if name in ("rolling_mean", "rolling_std", "rolling_sum", "shift", "delta"):
            if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
                val = node.args[1].value
                if isinstance(val, (int, float)):
                    if val <= 0 or val > 252:
                        raise FactorDSLError(f"{name}() window must be between 1 and 252 days (got {val})")

        for arg in node.args:
            _validate_node(arg, depth=depth + 1, max_depth=max_depth)
        return

    if isinstance(node, ast.Name):
        if node.id not in WHITELISTED_VARIABLES:
            raise FactorDSLError(f"Unknown variable: {node.id}")
        return

    if isinstance(node, ast.Constant):
        if not isinstance(node.value, (int, float)) or isinstance(node.value, bool):
            raise FactorDSLError("Only numeric constants are allowed")
        return

    raise FactorDSLError(f"Disallowed expression: {type(node).__name__}")


# --- Evaluation ---------------------------------------------------------------


def _eval_node(node: ast.AST, panel: dict[str, pd.DataFrame]):
    if isinstance(node, ast.BinOp):
        left = _eval_node(node.left, panel)
        right = _eval_node(node.right, panel)
        return _BIN_OPS[type(node.op)](left, right)

    if isinstance(node, ast.UnaryOp):
        operand = _eval_node(node.operand, panel)
        return operand if isinstance(node.op, ast.UAdd) else -operand

    if isinstance(node, ast.Compare):
        left = _eval_node(node.left, panel)
        right = _eval_node(node.comparators[0], panel)
        return _CMP_OPS[type(node.ops[0])](left, right).astype(float)

    if isinstance(node, ast.Call):
        assert isinstance(node.func, ast.Name)
        func, _ = WHITELISTED_FUNCTIONS[node.func.id]
        args = [_eval_node(arg, panel) for arg in node.args]
        return func(*args)

    if isinstance(node, ast.Name):
        if node.id not in panel:
            raise FactorDSLError(f"Variable not available in panel: {node.id}")
        return panel[node.id]

    if isinstance(node, ast.Constant):
        return node.value

    raise FactorDSLError(f"Disallowed expression: {type(node).__name__}")
