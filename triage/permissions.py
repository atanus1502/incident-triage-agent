"""Read-only enforcement for every outbound call the agent's tools make.

Three layers keep the agent from changing anything:
  1. The model is only ever offered read tools (see triage/tools/).
  2. Each tool's HTTP client is wrapped in ReadOnlyTransport, which rejects any
     request that is not GET/HEAD unless it matches an explicit read-only POST
     rule (e.g. Datadog's log search endpoint is a POST).
  3. Credentials are scoped read-only at the source (see .env.example).
Layer 2 means a bug in a tool, or a prompt-injected argument, still cannot write.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import httpx


class WriteAttemptBlocked(Exception):
    pass


@dataclass(frozen=True)
class ReadPolicy:
    # Regexes matched against the request path for POSTs that only read data.
    read_only_posts: tuple[str, ...] = ()
    safe_methods: frozenset[str] = field(default_factory=lambda: frozenset({"GET", "HEAD"}))

    def check(self, request: httpx.Request) -> None:
        method = request.method.upper()
        if method in self.safe_methods:
            return
        if method == "POST" and any(re.fullmatch(p, request.url.path) for p in self.read_only_posts):
            return
        raise WriteAttemptBlocked(f"{method} {request.url.host}{request.url.path} is not on the read-only allowlist")


class ReadOnlyTransport(httpx.BaseTransport):
    def __init__(self, policy: ReadPolicy, inner: httpx.BaseTransport | None = None):
        self.policy = policy
        self.inner = inner or httpx.HTTPTransport(retries=2)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.policy.check(request)
        return self.inner.handle_request(request)

    def close(self) -> None:
        self.inner.close()


def read_only_client(
    base_url: str,
    policy: ReadPolicy = ReadPolicy(),
    *,
    headers: dict[str, str] | None = None,
    auth: httpx.Auth | tuple[str, str] | None = None,
    timeout: float = 15.0,
    transport: httpx.BaseTransport | None = None,
) -> httpx.Client:
    return httpx.Client(
        base_url=base_url,
        headers=headers,
        auth=auth,
        timeout=timeout,
        transport=ReadOnlyTransport(policy, transport),
    )
