"""Tests for TeamsPowerAutomateSender card delivery + HTML fallback.

Guards the regression where a card-only path with no bearer token (e.g. a CI
runner that can't do the interactive login) dropped every alert instead of
falling back to the HTML digest.
"""
from unittest.mock import MagicMock, patch

from src.delivery.teams import TeamsPowerAutomateSender


def _resp(status=202):
    r = MagicMock()
    r.status_code = status
    r.text = ""
    return r


def test_supports_cards_requires_url_and_enable_flag():
    # No snooze URL → off
    assert TeamsPowerAutomateSender("https://flow/x").supports_cards is False
    # Snooze URL but not explicitly enabled → still OFF (master off-switch)
    assert TeamsPowerAutomateSender("https://flow/x", snooze_flow_url="https://snz/y").supports_cards is False
    # Only when BOTH the URL is set and cards are explicitly enabled
    assert TeamsPowerAutomateSender(
        "https://flow/x", snooze_flow_url="https://snz/y", enable_cards=True
    ).supports_cards is True


def test_send_card_posts_card_when_token_available():
    sender = TeamsPowerAutomateSender("https://flow/x", snooze_flow_url="https://snz/y")
    payload = {"card": {"type": "AdaptiveCard"}, "message": "<b>hi</b>"}
    with patch.object(sender, "_bearer_token", return_value="TOK"), \
         patch("src.delivery.teams.requests.post", return_value=_resp()) as post:
        ok = sender.send_card(payload, recipient_email="a@b.com")
    assert ok is True
    url, kwargs = post.call_args[0][0], post.call_args[1]
    assert url == "https://snz/y"                                  # posted to the snooze flow
    assert kwargs["json"]["card"] == {"type": "AdaptiveCard"}      # card carried
    assert kwargs["headers"]["Authorization"] == "Bearer TOK"      # authenticated


def test_send_card_falls_back_to_html_when_no_token():
    """No token → send the HTML digest via the normal flow, never drop the alert."""
    sender = TeamsPowerAutomateSender("https://flow/x", snooze_flow_url="https://snz/y")
    payload = {"card": {"type": "AdaptiveCard"}, "message": "<b>hi</b>"}
    with patch.object(sender, "_bearer_token", return_value=None), \
         patch("src.delivery.teams.requests.post", return_value=_resp()) as post:
        ok = sender.send_card(payload, recipient_email="a@b.com")
    assert ok is True
    url, kwargs = post.call_args[0][0], post.call_args[1]
    assert url == "https://flow/x"                                 # HTML flow, not the snooze flow
    assert kwargs["json"] == {"recipient": "a@b.com", "message": "<b>hi</b>"}
    assert "card" not in kwargs["json"]


def test_send_does_not_attach_bearer_to_sas_url():
    """A SAS-signed flow URL self-authenticates; adding a bearer token too would
    cause HTTP 401 (two auth schemes). Ensure send() posts SAS URLs unauthenticated
    even when a token is available."""
    sender = TeamsPowerAutomateSender("https://flow/x?api-version=1&sig=SECRET")
    with patch.object(sender, "_bearer_token", return_value="TOK"), \
         patch("src.delivery.teams.requests.post", return_value=_resp()) as post:
        ok = sender.send({"message": "hi"}, recipient_email="a@b.com")
    assert ok is True
    assert "Authorization" not in post.call_args[1]["headers"]


def test_send_attaches_bearer_to_direct_api_url():
    """Non-SAS (direct-API) URLs need the bearer token."""
    sender = TeamsPowerAutomateSender("https://x.powerplatform.com/.../workflows/abc?api-version=1")
    with patch.object(sender, "_bearer_token", return_value="TOK"), \
         patch("src.delivery.teams.requests.post", return_value=_resp()) as post:
        sender.send({"message": "hi"}, recipient_email="a@b.com")
    assert post.call_args[1]["headers"]["Authorization"] == "Bearer TOK"
