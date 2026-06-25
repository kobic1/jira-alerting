"""Condition evaluators — each operator maps to a pure comparison function.

Standard operators work on scalar field values.
Role-aware operators (days_since_role_comment) are handled separately in the
engine because they require access to the Jira client and the people registry.
"""
from __future__ import annotations

from typing import Any, Callable

ConditionFn = Callable[[Any, Any], bool]

# ---------------------------------------------------------------------------
# Standard operators (no external I/O, fully pure)
# ---------------------------------------------------------------------------

_OPERATORS: dict[str, ConditionFn] = {
    "gt":           lambda actual, threshold: actual is not None and actual > threshold,
    "gte":          lambda actual, threshold: actual is not None and actual >= threshold,
    "lt":           lambda actual, threshold: actual is not None and actual < threshold,
    "lte":          lambda actual, threshold: actual is not None and actual <= threshold,
    "eq":           lambda actual, threshold: actual == threshold,
    "neq":          lambda actual, threshold: actual != threshold,
    "is_null":      lambda actual, _: actual is None,
    "is_not_null":  lambda actual, _: actual is not None,
    "contains":     lambda actual, threshold: threshold in (actual or ""),
    "in":           lambda actual, threshold: actual in threshold,
    "not_in":       lambda actual, threshold: actual not in threshold,
}

# Operators that require special handling in the engine (not evaluated here)
ROLE_AWARE_OPERATORS = {"days_since_role_comment"}


def is_role_aware(operator: str) -> bool:
    return operator in ROLE_AWARE_OPERATORS


def evaluate_condition(field_value: Any, operator: str, threshold: Any) -> bool:
    fn = _OPERATORS.get(operator)
    if fn is None:
        raise ValueError(
            f"Unknown operator: {operator!r}. "
            f"Standard: {sorted(_OPERATORS)}  |  Role-aware (engine-handled): {sorted(ROLE_AWARE_OPERATORS)}"
        )
    return fn(field_value, threshold)
