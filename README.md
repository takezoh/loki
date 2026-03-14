# linear-autopilot

Linear-driven AI agent. Automatically plans and implements tasks triggered by issue status changes.

## Linear Setup

### API Key

Create at Settings → API → Personal API keys.

### GitHub Integration

Connect at Settings → Integrations → GitHub. Required for automatic PR syncing.

### Issue Statuses

Add the following statuses at Settings → Teams → Issue statuses & automations:
- Planning, Pending Approval, Plan Changes Requested, Implementing, Changes Requested, Failed

(Backlog, In Review, Done, Cancelled exist by default in Linear)

### OAuth App (Agent API / Webhook)

1. Settings → Account → API → OAuth applications → Create
2. Enter application name and redirect URL
3. Webhook URL: `https://<server>:3000/webhook`
4. Set webhook secret → `LINEAR_WEBHOOK_SECRET`
5. Generate actor token (actor=application) → `LINEAR_OAUTH_TOKEN`
6. Enable agent features: Manage → Enable agent features

## Prerequisites

- Python 3.10+
- Node.js (for Linear MCP server via `npx`)
- [Claude Code](https://claude.com/claude-code) CLI
- [GitHub CLI](https://cli.github.com/) (`gh`)
- [Linear](https://linear.app/) account
- `bubblewrap` and `socat` (for sandbox)

```bash
# Ubuntu/Debian
sudo apt-get install bubblewrap socat
```

### Configuration

1. Copy example configs:
   ```bash
   cp config/settings.json.example config/settings.json
   cp config/secrets.env.example config/secrets.env
   cp config/repos.conf.example config/repos.conf
   ```

2. Edit `config/settings.json`:
   - `team` — Linear team name (required; `team_id` is resolved automatically via API)
   - `log_dir`, `lock_dir`, `worktree_dir` — directory paths (required)
   - Optional: `model`, `budget`, `max_turns`, `max_concurrent`, `sandbox`

3. Edit `config/secrets.env` — set `LINEAR_API_KEY` (and `LINEAR_OAUTH_TOKEN`, `LINEAR_WEBHOOK_SECRET` if using Webhook)

4. Edit `config/repos.conf` — map labels to repository paths:
   ```
   myproject=/home/user/dev/myproject
   ```

5. Add the Linear MCP server to Claude Code:
   ```bash
   claude mcp add -s user linear-server -- npx -y @anthropic-ai/linear-mcp-server
   ```

6. Authenticate the Linear MCP server:
   Launch Claude Code and run `/mcp` to open the MCP authentication flow, then authorize the `linear-server` connection with your Linear account.

## Usage

```bash
bin/forge.sh --check             # check environment
bin/forge.sh                     # single run
bin/forge.sh --interval 300      # polling daemon (300s interval)
bin/webhook.sh                   # webhook server
```

### systemd (Linux)

```bash
# Polling service
bin/service-systemd.sh register-polling
bin/service-systemd.sh start-polling
bin/service-systemd.sh logs-polling

# Webhook service
bin/service-systemd.sh register-webhook
bin/service-systemd.sh start-webhook
bin/service-systemd.sh logs-webhook
```

### launchd (macOS)

```bash
bin/service-launchd.sh register
bin/service-launchd.sh enable
bin/service-launchd.sh start
bin/service-launchd.sh logs
```

## Workflow

```
Backlog → Planning → Pending Approval ⇄ Plan Changes Requested → Implementing → In Review ⇄ Changes Requested → Done
```

| Status | Category | Actor | Description |
|--------|----------|-------|-------------|
| Backlog | Backlog | Human | Not started |
| Planning | Started | Agent | Creating sub-issues and plan |
| Pending Approval | Started | Human | Reviewing the plan |
| Plan Changes Requested | Started | Agent | Revising plan based on feedback |
| Implementing | Started | Agent | Building + PR creation |
| In Review | Started | Human | Reviewing PRs |
| Changes Requested | Started | Agent | Fixing PR review feedback |
| Failed | Started | Auto | Execution failed |
| Done | Completed | Auto | Completed |
| Cancelled | Cancelled | Human | Cancelled |

## Models

| Role | Model | Rationale |
|------|-------|-----------|
| Planner | Sonnet + Opus subagent | Sonnet orchestrates, Opus subagent for codebase analysis |
| Plan Reviewer | Sonnet + Opus subagent | Sonnet orchestrates, Opus subagent for re-investigation |
| Conductor | Sonnet | Orchestrates implementer + reviewer, commits results |
| Implementer | Sonnet | Code generation, speed and cost balance |
| Reviewer | Opus | Deep reasoning for bug and design issue detection |
| PR Description | Haiku | Simple text generation, low cost |

## Sandbox

Each Claude CLI execution runs with [Claude Code's native sandbox](https://code.claude.com/docs/en/sandboxing).
Configure sandbox settings in `claude.sandbox` in `settings.json` (see `settings.json.example`).
`setup_sandbox` dynamically adds log directory and parent repo's `.git/worktrees` to `allowWrite`. Sub-issue execution also adds the parent issue's worktree directory.
