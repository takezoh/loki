#!/usr/bin/env bash
set -euo pipefail

FORGE_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
UNIT_DIR="$HOME/.config/systemd/user"

usage() {
    cat <<EOF
Usage: $0 <command>

Polling daemon (forge):
  register-polling, unregister-polling, enable-polling, disable-polling,
  start-polling, stop-polling, restart-polling, status-polling, logs-polling

Webhook server (forge-webhook):
  register-webhook, unregister-webhook, enable-webhook, disable-webhook,
  start-webhook, stop-webhook, restart-webhook, status-webhook, logs-webhook

EOF
    exit 1
}

_register_service() {
    local name="$1" description="$2" exec_start="$3" restart_sec="${4:-30}"
    local unit_file="${UNIT_DIR}/${name}.service"
    mkdir -p "$UNIT_DIR"
    cat > "$unit_file" <<EOF
[Unit]
Description=${description}
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=${exec_start}
WorkingDirectory=${FORGE_ROOT}
EnvironmentFile=${FORGE_ROOT}/config/secrets.env
Restart=on-failure
RestartSec=${restart_sec}

[Install]
WantedBy=default.target
EOF
    systemctl --user daemon-reload
    echo "Registered ${name} service"
}

_unregister_service() {
    local name="$1"
    local unit_file="${UNIT_DIR}/${name}.service"
    if [ -f "$unit_file" ]; then
        systemctl --user stop "$name" 2>/dev/null || true
        systemctl --user disable "$name" 2>/dev/null || true
        rm "$unit_file"
        systemctl --user daemon-reload
        echo "Unregistered ${name} service"
    else
        echo "Service ${name} not registered"
    fi
}

_systemctl_cmd() {
    local action="$1" name="$2"
    systemctl --user "$action" "$name"
    echo "${action^} ${name}"
}

_logs() {
    local name="$1"
    journalctl --user -u "$name" -f
}

# --- forge (polling daemon) ---

cmd_register_polling()   { _register_service "forge" "Forge - Linear-driven AI agent" "${FORGE_ROOT}/bin/forge.sh" 30; }
cmd_unregister_polling() { _unregister_service "forge"; }
cmd_enable_polling()     { _systemctl_cmd enable forge; }
cmd_disable_polling()    { _systemctl_cmd disable forge; }
cmd_start_polling()      { _systemctl_cmd start forge; }
cmd_stop_polling()       { _systemctl_cmd stop forge; }
cmd_restart_polling()    { _systemctl_cmd restart forge; }
cmd_status_polling()     { systemctl --user status forge; }
cmd_logs_polling()       { _logs forge; }

# --- forge-webhook ---

cmd_register_webhook()   { _register_service "forge-webhook" "Forge Webhook - Linear Agent API webhook server" "${FORGE_ROOT}/bin/webhook.sh" 10; }
cmd_unregister_webhook() { _unregister_service "forge-webhook"; }
cmd_enable_webhook()     { _systemctl_cmd enable forge-webhook; }
cmd_disable_webhook()    { _systemctl_cmd disable forge-webhook; }
cmd_start_webhook()      { _systemctl_cmd start forge-webhook; }
cmd_stop_webhook()       { _systemctl_cmd stop forge-webhook; }
cmd_restart_webhook()    { _systemctl_cmd restart forge-webhook; }
cmd_status_webhook()     { systemctl --user status forge-webhook; }
cmd_logs_webhook()       { _logs forge-webhook; }

[ $# -lt 1 ] && usage

case "$1" in
    register-polling)   cmd_register_polling ;;
    unregister-polling) cmd_unregister_polling ;;
    enable-polling)     cmd_enable_polling ;;
    disable-polling)    cmd_disable_polling ;;
    start-polling)      cmd_start_polling ;;
    stop-polling)       cmd_stop_polling ;;
    restart-polling)    cmd_restart_polling ;;
    status-polling)     cmd_status_polling ;;
    logs-polling)       cmd_logs_polling ;;
    register-webhook)   cmd_register_webhook ;;
    unregister-webhook) cmd_unregister_webhook ;;
    enable-webhook)     cmd_enable_webhook ;;
    disable-webhook)    cmd_disable_webhook ;;
    start-webhook)      cmd_start_webhook ;;
    stop-webhook)       cmd_stop_webhook ;;
    restart-webhook)    cmd_restart_webhook ;;
    status-webhook)     cmd_status_webhook ;;
    logs-webhook)       cmd_logs_webhook ;;
    *)                  usage ;;
esac
