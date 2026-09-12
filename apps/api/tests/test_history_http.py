"""Actual HTTP clients, cookies and SSE against isolated Postgres; provider keys disabled."""

import httpx
from test_http_postgres import server as isolated_server
from test_http_postgres import visitor

server = isolated_server


def next_revision(lines):
    for line in lines:
        if line.startswith("data: "):
            return line.removeprefix("data: ")
    raise AssertionError("History stream ended without a revision")


def test_second_tab_and_reopened_browser_keep_owned_history_and_receive_database_changes(server):
    url, _ = server
    with visitor(url) as first:
        owner = first.post("/api/v1/session").json()["id"]
        with httpx.Client(base_url=url, cookies=first.cookies, headers={"Origin": url}) as second:
            assert second.post("/api/v1/session").json()["id"] == owner
            with first.stream("GET", "/api/v1/history/events", timeout=5) as events:
                assert events.status_code == 200
                lines = events.iter_lines()
                before = next_revision(lines)
                created = second.post("/api/v1/conversations", json={"title": "Saved conversation"})
                assert created.status_code == 201
                cid = created.json()["id"]
                assert next_revision(lines) != before
                updated = second.patch("/api/v1/conversations/" + cid, json={"title": "Policy notes"})
                assert updated.status_code == 200
                assert next_revision(lines)
            assert first.get("/api/v1/conversations/" + cid).json()["title"] == "Policy notes"
        # Reconstruct the entire HTTP client using the persistent browser cookie.
        with httpx.Client(base_url=url, cookies=first.cookies, headers={"Origin": url}) as reopened:
            assert reopened.post("/api/v1/session").json()["id"] == owner
            assert any(item["id"] == cid for item in reopened.get("/api/v1/conversations").json()["items"])
        with visitor(url) as separate_browser:
            assert separate_browser.get("/api/v1/conversations/" + cid).status_code == 404
