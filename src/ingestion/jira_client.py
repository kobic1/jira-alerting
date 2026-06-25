"""Jira REST API client — data ingestion layer."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from src.models import JiraIssue

logger = logging.getLogger(__name__)


class JiraClient:
    def __init__(self, base_url: str, email: str, api_token: str, timeout: int = 30):
        self.base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._session = self._build_session(email, api_token)

    def _build_session(self, email: str, api_token: str) -> requests.Session:
        session = requests.Session()
        session.auth = (email, api_token)
        session.headers.update({"Accept": "application/json"})
        retry = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"],
        )
        session.mount("https://", HTTPAdapter(max_retries=retry))
        return session

    def search_issues(self, jql: str, max_results: int = 100) -> list[JiraIssue]:
        """Fetch all issues matching JQL using cursor-based pagination (Jira Cloud v3)."""
        issues: list[JiraIssue] = []
        page_size = min(50, max_results)
        next_page_token: str | None = None

        while len(issues) < max_results:
            data = self._fetch_page(jql, page_size=page_size, next_page_token=next_page_token)
            for item in data.get("issues", []):
                issue = JiraIssue.from_api(item, self.base_url)
                self._enrich_cycle_time(issue, item)
                issues.append(issue)

            next_page_token = data.get("nextPageToken")
            if data.get("isLast", True) or not next_page_token:
                break

        logger.info("Fetched %d issues for JQL: %.120s", len(issues), jql)
        return issues[:max_results]

    def _fetch_page(
        self, jql: str, page_size: int, next_page_token: str | None
    ) -> dict:
        url = f"{self.base_url}/rest/api/3/search/jql"
        params: dict = {
            "jql": jql,
            "maxResults": page_size,
            "fields": (
                "summary,status,issuetype,priority,assignee,reporter,"
                "created,updated,labels,changelog,customfield_10055"
            ),
            "expand": "changelog",
        }
        if next_page_token:
            params["nextPageToken"] = next_page_token

        resp = self._session.get(url, params=params, timeout=self._timeout)
        resp.raise_for_status()
        return resp.json()

    def _enrich_cycle_time(self, issue: JiraIssue, raw: dict) -> None:
        """Extract first 'In Progress' transition date from changelog."""
        for history in raw.get("changelog", {}).get("histories", []):
            for item in history.get("items", []):
                if item.get("field") == "status" and item.get("toString") == "In Progress":
                    from src.models import _parse_dt
                    issue.raw["_in_progress_date"] = _parse_dt(history.get("created"))
                    return

    def get_comments(self, issue_key: str) -> list[IssueComment]:
        """Return all comments for an issue, newest first."""
        url = f"{self.base_url}/rest/api/3/issue/{issue_key}/comment"
        params = {"orderBy": "-created", "maxResults": 50}
        resp = self._session.get(url, params=params, timeout=self._timeout)
        resp.raise_for_status()
        comments = []
        for item in resp.json().get("comments", []):
            author = item.get("author") or {}
            comments.append(
                IssueComment(
                    account_id=author.get("accountId", ""),
                    display_name=author.get("displayName", ""),
                    email=author.get("emailAddress", ""),
                    created_at=_parse_comment_dt(item.get("created")),
                )
            )
        return comments

    def get_project_lead(self, project_key: str) -> "JiraUser | None":
        """Return the project lead as a JiraUser, or None if not found."""
        from src.models import JiraUser
        url = f"{self.base_url}/rest/api/3/project/{project_key}"
        try:
            resp = self._session.get(url, timeout=self._timeout)
            resp.raise_for_status()
            lead = resp.json().get("lead")
            if lead:
                return JiraUser(
                    account_id=lead.get("accountId", ""),
                    display_name=lead.get("displayName", "Unknown"),
                    email=lead.get("emailAddress"),
                )
        except Exception as exc:
            logger.warning("Could not fetch project lead for %s: %s", project_key, exc)
        return None

    def get_filter_url(self, filter_id: str | None, jql: str) -> str:
        if filter_id:
            return f"{self.base_url}/issues/?filter={filter_id}"
        import urllib.parse
        return f"{self.base_url}/issues/?jql={urllib.parse.quote(jql)}"


@dataclass
class IssueComment:
    account_id: str
    display_name: str
    email: str
    created_at: datetime


def _parse_comment_dt(value: str | None) -> datetime:
    if not value:
        return datetime.utcnow()
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return datetime.utcnow()
