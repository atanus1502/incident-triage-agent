from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    # No comment stripping: secrets can contain '#'. Shells already drop inline comments when sourcing .env.
    return os.environ.get(name, default).strip()


@dataclass(frozen=True)
class Settings:
    model: str = "claude-sonnet-5-5"
    effort: str = "medium"
    time_budget_s: float = 100.0
    # When this many seconds remain, the agent is told to stop investigating and submit.
    wrapup_s: float = 25.0
    max_turns: int = 14
    max_tool_output_chars: int = 20_000
    service_catalog: Path = Path("config/services.yaml")

    dd_site: str = "datadoghq.com"
    dd_api_key: str = ""
    dd_app_key: str = ""

    github_token: str = ""
    github_api_url: str = "https://api.github.com"

    snow_instance: str = ""
    snow_user: str = ""
    snow_password: str = ""
    snow_webhook_token: str = ""

    pagerduty_api_key: str = ""
    pagerduty_webhook_secret: str = ""

    slack_bot_token: str = ""
    slack_channel: str = "#oncall-triage"
    slack_signing_secret: str = ""

    feedback_log: Path = Path("data/feedback.jsonl")
    audit_log: Path = Path("data/audit.jsonl")
    # While this file exists, new incidents are not triaged. Takes effect without a restart.
    kill_switch_file: Path = Path("data/TRIAGE_DISABLED")
    max_concurrent_triages: int = 4
    max_queued_triages: int = 20
    max_triages_per_day: int = 500

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            model=_env("TRIAGE_MODEL", cls.model),
            effort=_env("TRIAGE_EFFORT", cls.effort),
            time_budget_s=float(_env("TRIAGE_TIME_BUDGET_S", str(cls.time_budget_s))),
            max_turns=int(_env("TRIAGE_MAX_TURNS", str(cls.max_turns))),
            service_catalog=Path(_env("TRIAGE_SERVICE_CATALOG", str(cls.service_catalog))),
            dd_site=_env("DD_SITE", cls.dd_site),
            dd_api_key=_env("DD_API_KEY"),
            dd_app_key=_env("DD_APP_KEY"),
            github_token=_env("GITHUB_TOKEN"),
            github_api_url=_env("GITHUB_API_URL", cls.github_api_url),
            snow_instance=_env("SNOW_INSTANCE"),
            snow_user=_env("SNOW_USER"),
            snow_password=_env("SNOW_PASSWORD"),
            snow_webhook_token=_env("SNOW_WEBHOOK_TOKEN"),
            pagerduty_api_key=_env("PAGERDUTY_API_KEY"),
            pagerduty_webhook_secret=_env("PAGERDUTY_WEBHOOK_SECRET"),
            slack_bot_token=_env("SLACK_BOT_TOKEN"),
            slack_channel=_env("SLACK_CHANNEL", cls.slack_channel),
            slack_signing_secret=_env("SLACK_SIGNING_SECRET"),
            feedback_log=Path(_env("TRIAGE_FEEDBACK_LOG", str(cls.feedback_log))),
            audit_log=Path(_env("TRIAGE_AUDIT_LOG", str(cls.audit_log))),
            kill_switch_file=Path(_env("TRIAGE_KILL_SWITCH_FILE", str(cls.kill_switch_file))),
            max_concurrent_triages=int(_env("TRIAGE_MAX_CONCURRENT", str(cls.max_concurrent_triages))),
            max_queued_triages=int(_env("TRIAGE_MAX_QUEUED", str(cls.max_queued_triages))),
            max_triages_per_day=int(_env("TRIAGE_MAX_PER_DAY", str(cls.max_triages_per_day))),
        )
