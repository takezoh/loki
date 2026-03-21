from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from pydantic import BaseModel, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class PhaseConfig(BaseModel):
    model: str = ""
    budget: Decimal = Decimal("3.00")
    max_turns: int = 30
    timeout: int = 1800
    idle_timeout: int = 180


class WebhookConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 3000


def _load_repos_conf(path: Path) -> dict[str, Path]:
    repos = {}
    if not path.exists():
        return repos
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        label, repo_path = line.split("=", 1)
        repos[label.strip()] = Path(repo_path.strip())
    return repos


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file="loki2.env",
        env_file_encoding="utf-8",
        env_prefix="LOKI_",
        extra="ignore",
    )

    linear_team: str
    linear_oauth_token: SecretStr
    linear_webhook_secret: SecretStr = SecretStr("")

    default_model: str = "sonnet"
    max_concurrent: int = 3
    max_retries: int = 2
    poll_interval: int = 300

    log_dir: Path = Path("/workspace/loki/logs")
    worktree_dir: Path = Path("/worktrees")
    db_path: Path = Path("/workspace/loki/loki2.db")

    repos_conf: Path = Path("/workspace/loki/repos.conf")
    repos: dict[str, Path] = {}
    phases: dict[str, PhaseConfig] = {}
    webhook: WebhookConfig | None = None

    def model_post_init(self, __context):
        if not self.repos:
            self.repos = _load_repos_conf(self.repos_conf)

    def phase_config(self, phase: str) -> PhaseConfig:
        return self.phases.get(phase, PhaseConfig())

    def model_for_phase(self, phase: str) -> str:
        pc = self.phase_config(phase)
        return pc.model or self.default_model
