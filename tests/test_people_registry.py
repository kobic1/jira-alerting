"""Tests for PeopleRegistry — role lookups and Jira user matching."""
import pytest
from src.ingestion.people_registry import Person, PeopleRegistry
from src.models import JiraUser


def _registry(*people):
    return PeopleRegistry(list(people))


def _person(name="Alice", email="alice@co.com", roles=None, account_id=""):
    return Person(
        display_name=name,
        email=email,
        jira_account_id=account_id,
        roles=roles or [],
    )


# ── Role lookups ──────────────────────────────────────────────────────────────

def test_get_by_role_returns_matching():
    pm = _person("Shikha", roles=["product_manager"])
    em = _person("Avi", roles=["engineering_manager"])
    reg = _registry(pm, em)
    result = reg.get_by_role("product_manager")
    assert len(result) == 1
    assert result[0].display_name == "Shikha"


def test_get_by_role_empty_when_no_match():
    reg = _registry(_person(roles=["engineering_manager"]))
    assert reg.get_by_role("product_manager") == []


def test_get_by_roles_deduplicates_multi_role_person():
    person = _person("Alice", roles=["product_manager", "engineering_manager"])
    reg = _registry(person)
    result = reg.get_by_roles(["product_manager", "engineering_manager"])
    assert len(result) == 1


def test_get_by_roles_union_of_roles():
    pm = _person("Shikha", email="s@co.com", roles=["product_manager"])
    em = _person("Avi", email="a@co.com", roles=["engineering_manager"])
    reg = _registry(pm, em)
    result = reg.get_by_roles(["product_manager", "engineering_manager"])
    assert len(result) == 2


# ── Jira user matching ────────────────────────────────────────────────────────

def test_matches_by_account_id():
    person = _person(account_id="abc123")
    reg = _registry(person)
    jira_user = JiraUser(account_id="abc123", display_name="Alice")
    assert reg.resolve_jira_user(jira_user) is person


def test_matches_by_email_case_insensitive():
    person = _person(email="Alice@Co.Com")
    reg = _registry(person)
    jira_user = JiraUser(account_id="", display_name="Alice", email="alice@co.com")
    assert reg.resolve_jira_user(jira_user) is person


def test_matches_by_display_name_fallback():
    person = _person(name="Alice Smith", email="")
    reg = _registry(person)
    jira_user = JiraUser(account_id="", display_name="alice smith")
    assert reg.resolve_jira_user(jira_user) is person


def test_no_match_returns_none():
    person = _person(name="Bob", email="bob@co.com", account_id="b1")
    reg = _registry(person)
    jira_user = JiraUser(account_id="xyz", display_name="Zara", email="zara@co.com")
    assert reg.resolve_jira_user(jira_user) is None


# ── to_jira_user ─────────────────────────────────────────────────────────────

def test_to_jira_user_uses_account_id_when_present():
    person = _person(name="Shikha", email="s@co.com", account_id="acc99")
    ju = person.to_jira_user()
    assert ju.account_id == "acc99"
    assert ju.display_name == "Shikha"


def test_to_jira_user_falls_back_to_email_key():
    person = _person(name="Shikha", email="s@co.com", account_id="")
    ju = person.to_jira_user()
    assert "s@co.com" in ju.account_id
