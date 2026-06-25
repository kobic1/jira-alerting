"""People registry — resolves roles to persons and matches Jira users to registry entries.

Registry entries are loaded from config/people.yaml.  A Person can be matched
against a JiraUser by account_id (exact), email (case-insensitive), or
display_name (case-insensitive) — in that priority order.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from src.models import JiraUser

logger = logging.getLogger(__name__)


@dataclass
class Person:
    display_name: str
    email: str
    roles: list[str]
    jira_account_id: str = ""
    teams_user_id: str = ""
    # When True, this person only receives digests from rules that explicitly
    # target their role (group_by: notify_role).  They are skipped when the
    # engine routes assignee/reporter-based rules.
    role_alerts_only: bool = False

    def has_role(self, role: str) -> bool:
        return role in self.roles

    def matches_jira_user(self, user: JiraUser) -> bool:
        if self.jira_account_id and user.account_id:
            return self.jira_account_id == user.account_id
        if self.email and user.email:
            return self.email.lower() == user.email.lower()
        return self.display_name.lower() == user.display_name.lower()

    def to_jira_user(self) -> JiraUser:
        """Project this Person as a JiraUser so it can be used as an AlertGroup owner."""
        return JiraUser(
            account_id=self.jira_account_id or f"person::{self.email}",
            display_name=self.display_name,
            email=self.email,
        )


class PeopleRegistry:
    def __init__(self, people: list[Person]):
        self._people = people
        logger.info(
            "People registry loaded: %d people, roles=%s",
            len(people),
            sorted({r for p in people for r in p.roles}),
        )

    # ------------------------------------------------------------------
    # Lookups
    # ------------------------------------------------------------------

    def get_by_role(self, role: str) -> list[Person]:
        """Return all people that hold the given role."""
        return [p for p in self._people if p.has_role(role)]

    def get_by_roles(self, roles: list[str]) -> list[Person]:
        """Return people holding ANY of the given roles (deduplicated)."""
        seen: set[str] = set()
        result = []
        for role in roles:
            for person in self.get_by_role(role):
                key = person.email or person.display_name
                if key not in seen:
                    seen.add(key)
                    result.append(person)
        return result

    def resolve_jira_user(self, user: JiraUser) -> Person | None:
        """Find the registry entry that corresponds to a Jira user, or None."""
        for person in self._people:
            if person.matches_jira_user(user):
                return person
        return None

    def find_by_email(self, email: str) -> Person | None:
        """Find a registry entry by email (case-insensitive), or None."""
        if not email:
            return None
        for person in self._people:
            if person.email and person.email.lower() == email.lower():
                return person
        return None

    @classmethod
    def from_yaml(cls, data: dict) -> PeopleRegistry:
        people = []
        for entry in data.get("people", []):
            people.append(
                Person(
                    display_name=entry["display_name"],
                    email=entry.get("email", ""),
                    jira_account_id=entry.get("jira_account_id", ""),
                    teams_user_id=entry.get("teams_user_id", ""),
                    roles=entry.get("roles", []),
                    role_alerts_only=bool(entry.get("role_alerts_only", False)),
                )
            )
        return cls(people)
