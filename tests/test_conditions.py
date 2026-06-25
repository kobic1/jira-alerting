import pytest
from src.rules.conditions import evaluate_condition


def test_gt_passes():
    assert evaluate_condition(15, "gt", 10) is True


def test_gt_fails():
    assert evaluate_condition(5, "gt", 10) is False


def test_gt_none():
    assert evaluate_condition(None, "gt", 10) is False


def test_is_null():
    assert evaluate_condition(None, "is_null", None) is True
    assert evaluate_condition("something", "is_null", None) is False


def test_in_operator():
    assert evaluate_condition("High", "in", ["High", "Critical"]) is True
    assert evaluate_condition("Low", "in", ["High", "Critical"]) is False


def test_unknown_operator():
    with pytest.raises(ValueError, match="Unknown operator"):
        evaluate_condition(1, "banana", 2)
