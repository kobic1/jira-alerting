"""Microsoft Teams delivery — Incoming Webhook, Power Automate flow, and Graph API."""
from __future__ import annotations

import logging
import os
from typing import Any

import requests

logger = logging.getLogger(__name__)


class TeamsWebhookSender:
    """Sends messages via an Incoming Webhook URL (simplest setup)."""

    def __init__(self, webhook_url: str, timeout: int = 15):
        self._url = webhook_url
        self._timeout = timeout

    def send(self, payload: dict[str, Any]) -> bool:
        try:
            resp = requests.post(self._url, json=payload, timeout=self._timeout)
            resp.raise_for_status()
            logger.info("Teams webhook delivery succeeded (status %d)", resp.status_code)
            return True
        except requests.HTTPError as exc:
            logger.error("Teams webhook delivery failed: %s — %s", exc, exc.response.text[:200])
            return False
        except requests.RequestException as exc:
            logger.error("Teams webhook request error: %s", exc)
            return False


class TeamsGraphSender:
    """Sends messages via the Microsoft Graph API (supports per-user DMs)."""

    _TOKEN_URL = "https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    _GRAPH_URL = "https://graph.microsoft.com/v1.0"

    def __init__(
        self,
        tenant_id: str,
        client_id: str,
        client_secret: str,
        channel_id: str,
        team_id: str | None = None,
        timeout: int = 15,
    ):
        self._tenant_id = tenant_id
        self._client_id = client_id
        self._client_secret = client_secret
        self._channel_id = channel_id
        self._team_id = team_id
        self._timeout = timeout
        self._token: str | None = None

    def _get_token(self) -> str:
        if self._token:
            return self._token
        url = self._TOKEN_URL.format(tenant_id=self._tenant_id)
        resp = requests.post(
            url,
            data={
                "grant_type": "client_credentials",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "scope": "https://graph.microsoft.com/.default",
            },
            timeout=self._timeout,
        )
        resp.raise_for_status()
        self._token = resp.json()["access_token"]
        return self._token

    def send_to_channel(self, payload: dict[str, Any]) -> bool:
        if not self._team_id:
            logger.error("team_id required for Graph API channel delivery")
            return False
        try:
            token = self._get_token()
            url = f"{self._GRAPH_URL}/teams/{self._team_id}/channels/{self._channel_id}/messages"
            resp = requests.post(
                url,
                json={"body": {"contentType": "html", "content": self._card_to_html(payload)}},
                headers={"Authorization": f"Bearer {token}"},
                timeout=self._timeout,
            )
            resp.raise_for_status()
            return True
        except requests.RequestException as exc:
            logger.error("Graph API delivery failed: %s", exc)
            return False

    def send_direct_message(self, user_id: str, payload: dict[str, Any]) -> bool:
        """Send an adaptive card as a DM to a specific user."""
        try:
            token = self._get_token()
            # 1. Create or get a 1:1 chat
            chat_resp = requests.post(
                f"{self._GRAPH_URL}/chats",
                json={
                    "chatType": "oneOnOne",
                    "members": [
                        {
                            "@odata.type": "#microsoft.graph.aadUserConversationMember",
                            "roles": ["owner"],
                            "user@odata.bind": f"{self._GRAPH_URL}/users/{user_id}",
                        }
                    ],
                },
                headers={"Authorization": f"Bearer {token}"},
                timeout=self._timeout,
            )
            chat_resp.raise_for_status()
            chat_id = chat_resp.json()["id"]

            # 2. Post the adaptive card
            msg_resp = requests.post(
                f"{self._GRAPH_URL}/chats/{chat_id}/messages",
                json={
                    "body": {"contentType": "html", "content": "<attachment id='card'></attachment>"},
                    "attachments": [
                        {
                            "id": "card",
                            "contentType": "application/vnd.microsoft.card.adaptive",
                            "content": str(payload["attachments"][0]["content"]),
                        }
                    ],
                },
                headers={"Authorization": f"Bearer {token}"},
                timeout=self._timeout,
            )
            msg_resp.raise_for_status()
            logger.info("Sent DM to user %s", user_id)
            return True
        except requests.RequestException as exc:
            logger.error("Graph DM delivery failed for user %s: %s", user_id, exc)
            return False

    def _card_to_html(self, payload: dict) -> str:
        import json
        card_json = json.dumps(payload["attachments"][0]["content"])
        return '<attachment id="1"></attachment>'


class TeamsPowerAutomateSender:
    """Sends personalised DMs via a single Power Automate HTTP-trigger flow.

    One flow handles every recipient -- the recipient email is passed
    dynamically in each request body, so each person gets their own message.

    Flow setup (one-time, ~3 minutes)
    ------------------------------------
    1. Power Automate -> Create -> Instant cloud flow -> start from blank
    2. Trigger: "When an HTTP request is received"
       Paste this schema into "Request Body JSON Schema":
       {
         "type": "object",
         "properties": {
           "recipient": {"type": "string"},
           "message": {"type": "string"}
         },
         "required": ["recipient", "message"]
       }
    3. Add step -> Microsoft Teams -> "Post message in a chat or channel"
       Post as: Flow bot
       Post in: Chat with Flow bot
       Recipient: Expression -> triggerBody()?['recipient']
       Message: Expression -> triggerBody()?['message']
    4. Save -> copy the HTTP POST URL -> export TEAMS_FLOW_URL=<url>

    Authentication
    --------------
    The Power Automate HTTP trigger URL is self-authenticated via a SAS token
    embedded in the URL (the sig= query parameter). No additional bearer token
    is required -- just POST directly to the URL.
    """

    def __init__(self, flow_url: str, timeout: int = 30):
        self._url = flow_url
        self._timeout = timeout

    def send(self, payload: dict[str, Any], recipient_email: str) -> bool:
        """POST message + recipient to the flow. The flow routes the DM."""
        body = {
            "recipient": recipient_email,
            "message":   payload.get("message", ""),
        }
        try:
            resp = requests.post(self._url, json=body, timeout=self._timeout)
            if resp.status_code in (200, 202):
                logger.info("Flow DM accepted -> %s (HTTP %d)", recipient_email, resp.status_code)
                return True
            logger.error(
                "Flow DM rejected for %s -- HTTP %d: %s",
                recipient_email, resp.status_code, resp.text[:200],
            )
            return False
        except requests.RequestException as exc:
            logger.error("Flow DM failed for %s: %s", recipient_email, exc)
            return False
