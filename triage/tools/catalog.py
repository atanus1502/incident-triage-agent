from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from triage.tools.base import Tool, ToolError


class ServiceCatalog:
    def __init__(self, services: dict[str, dict[str, Any]]):
        self.services = services

    @classmethod
    def load(cls, path: Path) -> "ServiceCatalog":
        if not path.exists():
            return cls({})
        data = yaml.safe_load(path.read_text()) or {}
        return cls(data.get("services") or {})

    def find(self, name: str) -> tuple[str, dict[str, Any]] | None:
        needle = name.strip().lower()
        for key, svc in self.services.items():
            names = [key, *svc.get("aliases", [])]
            if any(needle == n.lower() for n in names):
                return key, svc
        for key, svc in self.services.items():
            names = [key, *svc.get("aliases", [])]
            if any(needle in n.lower() or n.lower() in needle for n in names):
                return key, svc
        return None

    @property
    def repos(self) -> set[str]:
        return {s["repo"] for s in self.services.values() if s.get("repo")}

    def lookup_service(self, name: str) -> dict[str, Any]:
        match = self.find(name)
        if match is None:
            known = ", ".join(sorted(self.services)) or "(catalog is empty)"
            raise ToolError(f"No service matching {name!r}. Known services: {known}")
        key, svc = match
        return {"service": key, **svc}

    def tools(self) -> list[Tool]:
        return [
            Tool(
                name="lookup_service",
                description=(
                    "Look up a service in the service catalog by name or alias. Returns its GitHub repo, "
                    "deploy environment, Datadog service/env tags, ServiceNow CI, PagerDuty service id, "
                    "owner team, runbook URL and dependencies. Call this first."
                ),
                properties={"name": {"type": "string", "description": "Service name as it appears in the alert"}},
                required=("name",),
                handler=self.lookup_service,
            )
        ]
