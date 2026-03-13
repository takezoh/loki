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

3. Edit `config/secrets.env` — set `LINEAR_API_KEY`

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
python -m forge --check  # check environment
python -m forge          # run
```

Or via the wrapper script:

```bash
bin/main.sh
```

### Daemon / systemd

```bash
# Run as daemon (FORGE_POLL_INTERVAL sets interval, default 300s)
FORGE_POLL_INTERVAL=300 bin/daemon.sh

# Register and manage as systemd service
bin/service-systemd.sh register
bin/service-systemd.sh enable
bin/service-systemd.sh start
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
| Conductor | Sonnet | Procedural orchestration, cost-efficient |
| Implementer | Sonnet | Code generation, speed and cost balance |
| Reviewer | Opus | Deep reasoning for bug and design issue detection |
| PR Description | Haiku | Simple text generation, low cost |

## Sandbox

Each claude CLI execution runs with Claude Code's native sandbox:

- **Filesystem**: Write restricted to work directory + logs. `~/.ssh`, `~/.aws`, `~/.gnupg` denied.
- **Network**: `allowManagedDomainsOnly` — only `api.linear.app`, `github.com`, `api.anthropic.com` allowed.
- **Escape hatch disabled**: `allowUnsandboxedCommands: false`
