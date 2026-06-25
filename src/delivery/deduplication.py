"""Deduplication store — prevents re-alerting on the same issue within a time window."""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


class DeduplicationStore:
    def __init__(self, window_hours: int = 24, store_path: str = ".alert_cache.json"):
        self._window = timedelta(hours=window_hours)
        self._path = store_path
        self._cache: dict[str, str] = self._load()

    def _load(self) -> dict[str, str]:
        if os.path.exists(self._path):
            try:
                with open(self._path) as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                logger.warning("Could not read dedup cache; starting fresh")
        return {}

    def _save(self) -> None:
        with open(self._path, "w") as f:
            json.dump(self._cache, f, indent=2)

    def _key(self, rule_id: str, issue_key: str) -> str:
        return f"{rule_id}::{issue_key}"

    def is_duplicate(self, rule_id: str, issue_key: str) -> bool:
        k = self._key(rule_id, issue_key)
        alerted_at_str = self._cache.get(k)
        if alerted_at_str is None:
            return False
        alerted_at = datetime.fromisoformat(alerted_at_str)
        return datetime.utcnow() - alerted_at < self._window

    def mark_alerted(self, rule_id: str, issue_key: str) -> None:
        k = self._key(rule_id, issue_key)
        self._cache[k] = datetime.utcnow().isoformat()
        self._prune()
        self._save()

    def _prune(self) -> None:
        cutoff = datetime.utcnow() - self._window
        self._cache = {
            k: v
            for k, v in self._cache.items()
            if datetime.fromisoformat(v) > cutoff
        }
