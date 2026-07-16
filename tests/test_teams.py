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


def test_supports_cards_reflects_snooze_url():
    assert TeamsPowerAutomateSender("https://flow/x").supports_cards is False
    assert TeamsPowerAutomateSender("https://flow/x", snooze_flow_url="https://snz/y").supports_cards is True


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
