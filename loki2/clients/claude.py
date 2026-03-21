from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path


_POLL_INTERVAL = 10


def run(prompt: str, work_dir: Path, *,
        model: str, max_turns: str, budget: str = "1.00",
        log_file: Path | None = None,
        capture_output: bool = False,
        timeout: int | None = None,
        idle_timeout: int | None = None) -> dict:
    output_format = "json" if capture_output else "stream-json"
    cmd = [
        "claude", "--print",
        "--no-session-persistence",
        "--max-budget-usd", budget,
        "--max-turns", max_turns,
        "--model", model,
        "-p", "-",
        "--output-format", output_format,
    ]
    if not capture_output:
        cmd.append("--verbose")

    if capture_output:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True,
            cwd=work_dir,
            start_new_session=True,
        )
        try:
            stdout, stderr = proc.communicate(input=prompt, timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_process_group(proc.pid)
            proc.wait()
            return {"returncode": -1, "error": f"timed out after {timeout}s", "stdout": "", "stderr": ""}

        try:
            result = json.loads(stdout)
        except (json.JSONDecodeError, ValueError):
            result = {"result": stdout}

        return {
            "returncode": proc.returncode,
            "result": result.get("result", ""),
            "stdout": stdout,
            "stderr": stderr,
            **{k: v for k, v in result.items() if k != "result"},
        }
    else:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        with open(log_file, "w") as log_fh:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=log_fh, stderr=subprocess.STDOUT,
                text=True,
                cwd=work_dir,
                start_new_session=True,
            )
            proc.stdin.write(prompt)
            proc.stdin.close()

            try:
                _wait_with_idle_check(proc, log_fh, timeout, idle_timeout)
            except subprocess.TimeoutExpired:
                _kill_process_group(proc.pid)
                proc.wait()
                return {"returncode": -1, "error": "timed out", "log_file": str(log_file)}

        result = _parse_log(log_file)
        result["returncode"] = proc.returncode
        result["log_file"] = str(log_file)
        return result


def _wait_with_idle_check(proc, log_fh, timeout, idle_timeout):
    if not idle_timeout:
        proc.wait(timeout=timeout)
        return

    now = time.monotonic()
    deadline = now + timeout if timeout else None
    idle_deadline = now + idle_timeout
    last_size = 0

    while proc.poll() is None:
        time.sleep(_POLL_INTERVAL)
        now = time.monotonic()

        if deadline and now >= deadline:
            raise subprocess.TimeoutExpired(proc.args, timeout)

        cur_size = os.fstat(log_fh.fileno()).st_size
        if cur_size != last_size:
            last_size = cur_size
            idle_deadline = now + idle_timeout
        elif now >= idle_deadline:
            raise subprocess.TimeoutExpired(proc.args, idle_timeout)


def _kill_process_group(pid: int):
    try:
        os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    time.sleep(5)
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _parse_log(log_file: Path) -> dict:
    try:
        content = log_file.read_text()
    except FileNotFoundError:
        return {"result": "", "error": "log file not found"}

    lines = content.strip().splitlines()
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
            if "result" in data:
                return data
        except (json.JSONDecodeError, ValueError):
            continue

    return {"result": "\n".join(lines[-20:]) if lines else ""}


_CODE_EDITING_PHASES = {"implementing", "review"}

_ALLOWED_DOMAINS = [
    "api.linear.app",
    "github.com",
    "*.github.com",
    "*.githubusercontent.com",
    "api.anthropic.com",
    "registry.npmjs.org",
    "*.npmjs.org",
    "proxy.golang.org",
    "sum.golang.org",
    "storage.googleapis.com",
    "pypi.org",
    "files.pythonhosted.org",
]

_BASE_ALLOW_TOOLS = ["Read", "Glob", "Grep", "Bash", "mcp__linear-server__*"]
_EDIT_ALLOW_TOOLS = _BASE_ALLOW_TOOLS + ["Edit", "Write"]


def _normalize_path(p: str | Path) -> str:
    s = str(p)
    if s.startswith("/"):
        return s
    return "/" + s


def setup_settings(work_dir: Path, *, phase: str = "",
                   log_dir: Path | None = None,
                   extra_write_paths: list[str] | None = None,
                   allowed_tools: list[str] | None = None,
                   denied_tools: list[str] | None = None):
    from loki2.core.state import PHASE_DENIED_TOOLS

    write_paths = [_normalize_path(work_dir), "/tmp"]
    if log_dir:
        write_paths.append(_normalize_path(log_dir))
    for p in (extra_write_paths or []):
        write_paths.append(_normalize_path(p))

    sandbox = {
        "enabled": True,
        "autoAllowBashIfSandboxed": True,
        "filesystem": {
            "allowWrite": write_paths,
            "denyRead": ["/home", "/root", "/etc", "/mnt/c"],
            "allowRead": ["~/.claude", "~/.local"],
        },
        "network": {
            "allowManagedDomainsOnly": True,
            "allowedDomains": _ALLOWED_DOMAINS,
        },
    }

    if allowed_tools:
        allow = allowed_tools
    elif phase in _CODE_EDITING_PHASES:
        allow = list(_EDIT_ALLOW_TOOLS)
    else:
        allow = list(_BASE_ALLOW_TOOLS)
    deny = denied_tools if denied_tools is not None else PHASE_DENIED_TOOLS.get(phase, [])

    settings = {
        "sandbox": sandbox,
        "permissions": {"allow": allow, "deny": deny},
    }

    claude_dir = work_dir / ".claude"
    claude_dir.mkdir(exist_ok=True)
    settings_file = claude_dir / "settings.local.json"
    settings_file.write_text(json.dumps(settings, indent=2))

    local_md = claude_dir / "CLAUDE.local.md"
    local_md.write_text(
        "# Loki Autonomous Agent\n\n"
        "You are running as an autonomous agent. "
        "Do not wait for user input.\n\n"
        "## Git operations\n\n"
        "Always commit and push without asking for confirmation. "
        "Code review happens on the PR, not here. "
        "Never end your turn with questions like \"コミットしますか？\" or \"Should I commit?\". "
        "Just do it.\n\n"
        "## Running tests\n\n"
        "Always run tests in non-interactive mode. "
        "Never use watch mode or commands that wait for interactive input.\n\n"
        "- vitest: use `npx vitest run` (not `npx vitest`)\n"
        "- jest: use `npx jest` (already non-interactive by default)\n"
        "- general: pass `--watch=false` or `--watchAll=false` if needed\n"
    )
