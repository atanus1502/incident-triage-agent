import httpx
import pytest

from triage.config import Settings
from triage.permissions import ReadPolicy, WriteAttemptBlocked, read_only_client
from triage.tools.base import Tool, ToolSet
from triage.tools.datadog import DatadogTools
from triage.tools.github import GitHubTools


def ok_transport() -> httpx.MockTransport:
    return httpx.MockTransport(lambda request: httpx.Response(200, json={"data": []}))


@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE", "POST"])
def test_writes_are_blocked_by_default(method):
    client = read_only_client("https://example.test", transport=ok_transport())
    with pytest.raises(WriteAttemptBlocked):
        client.request(method, "/api/now/table/incident")


def test_reads_pass_through():
    client = read_only_client("https://example.test", transport=ok_transport())
    assert client.get("/anything").status_code == 200


def test_allowlisted_read_post_passes_but_lookalike_does_not():
    policy = ReadPolicy(read_only_posts=(r"/api/v2/logs/events/search",))
    client = read_only_client("https://example.test", policy, transport=ok_transport())
    assert client.post("/api/v2/logs/events/search", json={}).status_code == 200
    with pytest.raises(WriteAttemptBlocked):
        client.post("/api/v2/logs/events/search/../../config/archives", json={})


def test_datadog_log_search_is_allowed():
    dd = DatadogTools(Settings(), transport=ok_transport())
    assert dd.search_logs("service:x", "2026-04-02T13:00:00Z", "2026-04-02T14:00:00Z") == []


def test_blocked_write_surfaces_to_model_as_error():
    client = read_only_client("https://example.test", transport=ok_transport())
    tools = ToolSet([Tool("rogue", "", {}, (), lambda: client.delete("/x"))])
    content, is_error = tools.call("rogue", {})
    assert is_error and "read-only" in content


def test_github_rejects_repos_outside_catalog():
    gh = GitHubTools(Settings(), {"yourbank/payments-api"}, transport=ok_transport())
    tools = ToolSet(gh.tools())
    content, is_error = tools.call(
        "github_commits", {"repo": "yourbank/secrets", "start": "2026-04-02T13:00:00Z", "end": "2026-04-02T14:00:00Z"}
    )
    assert is_error and "not in the service catalog" in content


def test_long_tool_output_is_truncated_with_notice():
    tools = ToolSet([Tool("big", "", {}, (), lambda: "x" * 50)], max_output_chars=10)
    content, is_error = tools.call("big", {})
    assert not is_error and content.startswith("x" * 10) and "truncated: 40 more characters" in content
