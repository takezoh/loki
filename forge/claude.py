import json
import os
import subprocess
from pathlib import Path

from .config import FORGE_ROOT
from .constants import (PHASE_PLANNING, PHASE_IMPLEMENTING,
                        PHASE_PLAN_REVIEW, PHASE_REVIEW)
from .git import detect_default_branch, diff_stat
from .linear import fetch_issue_detail, fetch_sub_issues

DEFAULT_SANDBOX_SETTINGS = {
    "sandbox": {
        "enabled": True,
        "autoAllowBashIfSandboxed": True,
        "allowUnsandboxedCommands": False,
        "filesystem": {
            "denyRead": [
                "~/.ssh",
                "~/.aws",
                "~/.gnupg",
                "~/.bash_history",
                "~/.zsh_history",
                "~/.netrc",
                "~/.docker",
                "~/.kube",
                "~/.local/share/atuin",
                "~/.secrets",
                "~/.1password",
                "~/.codex",
                "~/.pki",
                "~/.config/gcloud",
                "~/.config/op",
                "~/.terraform.d",
                "~/.gsutil",
                "~/.local/config",
                "~/.antigravity-server",
            ],
        },
        "network": {
            "allowManagedDomainsOnly": True,
            "allowedDomains": [
                "api.linear.app",
                "github.com",
                "*.github.com",
                "*.githubusercontent.com",
                "api.anthropic.com",
            ],
        },
    }
}

DEFAULT_DISALLOWED_TOOLS_MAP = {
    PHASE_PLANNING: [
        "mcp__linear-server__get_issue",
        "mcp__linear-server__list_issue_statuses",
    ],
    PHASE_IMPLEMENTING: [
        "mcp__linear-server__get_issue",
        "mcp__linear-server__list_documents",
        "mcp__linear-server__list_comments",
        "mcp__linear-server__save_issue",
    ],
    PHASE_PLAN_REVIEW: [
        "mcp__linear-server__get_issue",
        "mcp__linear-server__list_issue_statuses",
    ],
    PHASE_REVIEW: [
        "mcp__linear-server__save_issue",
        "mcp__linear-server__get_issue",
        "mcp__linear-server__list_documents",
    ],
}


def _deep_merge(base: dict, override: dict) -> dict:
    result = base.copy()
    for key, val in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = _deep_merge(result[key], val)
        else:
            result[key] = val
    return result


def _load_config() -> dict:
    settings_path = FORGE_ROOT / "config" / "settings.json"
    if not settings_path.exists():
        return {}
    with open(settings_path) as f:
        return json.load(f)


def setup_sandbox(work_dir: Path, log_dir: Path | None = None,
                   extra_write_paths: list[str] | None = None):
    cfg = _load_config()
    settings = json.loads(json.dumps(DEFAULT_SANDBOX_SETTINGS))
    if "sandbox" in cfg:
        settings["sandbox"] = _deep_merge(settings["sandbox"], cfg["sandbox"])
    allow_write = [str(log_dir)] if log_dir else []
    if extra_write_paths:
        allow_write.extend(extra_write_paths)
    settings["sandbox"]["filesystem"]["allowWrite"] = allow_write
    settings["sandbox"]["filesystem"]["denyRead"].append(str(FORGE_ROOT / "config"))

    claude_dir = work_dir / ".claude"
    claude_dir.mkdir(exist_ok=True)
    settings_file = claude_dir / "settings.local.json"
    settings_file.write_text(json.dumps(settings, indent=2))


def resolve_config(phase: str, env: dict) -> dict:
    model_key = f"FORGE_MODEL_{phase.upper()}"
    budget_key = f"FORGE_BUDGET_{phase.upper()}"
    turns_key = f"FORGE_MAX_TURNS_{phase.upper()}"
    model = env.get(model_key, env["FORGE_MODEL"])
    budget = env.get(budget_key, "1.00")
    max_turns = env[turns_key]

    disallowed = DEFAULT_DISALLOWED_TOOLS_MAP.get(phase, [])

    return {
        "model": model,
        "budget": budget,
        "max_turns": max_turns,
        "disallowed_tools": disallowed,
    }


def run(prompt: str, work_dir: Path, *,
        model: str, max_turns: str, budget: str = "1.00",
        disallowed_tools: list[str] | None = None,
        log_dir: Path | None = None, log_file: Path | None = None,
        extra_write_paths: list[str] | None = None,
        capture_output: bool = False):
    setup_sandbox(work_dir, log_dir, extra_write_paths=extra_write_paths)

    run_env = {**os.environ}
    run_env.pop("CLAUDECODE", None)

    cmd = [
        "claude", "--print",
        "--no-session-persistence",
        "--max-budget-usd", budget,
        "--max-turns", max_turns,
        "--model", model,
        "-p", prompt,
    ]
    if disallowed_tools:
        cmd.extend(["--disallowedTools", ",".join(disallowed_tools)])

    if capture_output:
        ret = subprocess.run(
            cmd,
            capture_output=True, text=True,
            cwd=work_dir, env=run_env,
        )
    else:
        with open(log_file, "w") as log:
            ret = subprocess.run(
                cmd,
                stdout=log, stderr=subprocess.STDOUT,
                cwd=work_dir, env=run_env,
            )

    return ret


def generate_pr_body(parent_id: str, parent_identifier: str, repo_path: str,
                     sub_issues: list[dict], env: dict) -> tuple[str, str]:
    prompt_file = FORGE_ROOT / "prompts" / "pr.md"
    prompt = prompt_file.read_text()

    parent_detail = fetch_issue_detail(parent_id)
    prompt = prompt.replace("{{PARENT_ISSUE_DETAIL}}", json.dumps(parent_detail, indent=2, ensure_ascii=False))

    parent_data = fetch_sub_issues(parent_id)
    prompt = prompt.replace("{{PLAN_DOCUMENTS}}", json.dumps(parent_data.get("documents", []), indent=2, ensure_ascii=False))

    sub_summary = []
    for s in sub_issues:
        sub_summary.append(f"- {s['identifier']}: {s['title']} ({s.get('state', '')})")
    prompt = prompt.replace("{{SUB_ISSUES}}", "\n".join(sub_summary))

    default_branch = detect_default_branch(repo_path)
    prompt = prompt.replace("{{DIFF_STAT}}", diff_stat(repo_path, default_branch, parent_identifier))

    ret = run(prompt, Path(repo_path),
              model=env.get("FORGE_MODEL_PR", env["FORGE_MODEL"]),
              max_turns="1", capture_output=True)
    if ret.returncode != 0:
        return parent_detail.get("title", parent_identifier), f"Parent issue: {parent_identifier}\n\nAll sub-issues completed."

    output = ret.stdout.strip()
    title = parent_detail.get("title", parent_identifier)
    body = output

    if "TITLE:" in output and "---" in output:
        parts = output.split("---", 1)
        for line in parts[0].splitlines():
            if line.startswith("TITLE:"):
                title = line.removeprefix("TITLE:").strip()
                break
        body = parts[1].strip()
        if body.startswith("```"):
            body = body.split("\n", 1)[1] if "\n" in body else body
        if body.endswith("```"):
            body = body.rsplit("\n", 1)[0] if "\n" in body else body

    return title, body
