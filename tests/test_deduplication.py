import json
import os
import tempfile
from datetime import datetime, timedelta

from src.delivery.deduplication import DeduplicationStore


def test_not_duplicate_initially(tmp_path):
    store = DeduplicationStore(window_hours=24, store_path=str(tmp_path / "cache.json"))
    assert store.is_duplicate("rule1", "PROJ-1") is False


def test_duplicate_after_mark(tmp_path):
    store = DeduplicationStore(window_hours=24, store_path=str(tmp_path / "cache.json"))
    store.mark_alerted("rule1", "PROJ-1")
    assert store.is_duplicate("rule1", "PROJ-1") is True


def test_not_duplicate_after_window_expires(tmp_path):
    path = str(tmp_path / "cache.json")
    store = DeduplicationStore(window_hours=1, store_path=path)
    # Manually write an old entry
    old_time = (datetime.utcnow() - timedelta(hours=2)).isoformat()
    with open(path, "w") as f:
        json.dump({"rule1::PROJ-1": old_time}, f)
    store2 = DeduplicationStore(window_hours=1, store_path=path)
    assert store2.is_duplicate("rule1", "PROJ-1") is False


def test_different_rules_are_independent(tmp_path):
    store = DeduplicationStore(window_hours=24, store_path=str(tmp_path / "cache.json"))
    store.mark_alerted("rule1", "PROJ-1")
    assert store.is_duplicate("rule2", "PROJ-1") is False
