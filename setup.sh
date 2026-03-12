#!/usr/bin/env bash
set -euo pipefail

FORGE_ROOT="$(cd "$(dirname "$0")" && pwd)"
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
NC='\033[0m'

ok()   { echo -e "  ${GREEN}✓${NC} $1"; }
warn() { echo -e "  ${YELLOW}!${NC} $1"; }
fail() { echo -e "  ${RED}✗${NC} $1"; }

errors=0

echo "=== forge setup ==="
echo

# 1. OS チェック
echo "[1/6] OS チェック"
if grep -qi microsoft /proc/version 2>/dev/null; then
    ok "WSL2 detected"
else
    ok "Linux detected"
fi

# 2. 必須コマンド
echo "[2/6] 必須コマンドの確認"
for cmd in python3 claude gh git; do
    if command -v "$cmd" &>/dev/null; then
        case "$cmd" in
            python3) ver=$(python3 --version 2>&1) ;;
            claude)  ver=$(claude --version 2>&1) ;;
            gh)      ver=$(gh --version 2>&1 | head -1) ;;
            git)     ver=$(git --version 2>&1) ;;
        esac
        ok "$cmd ($ver)"
    else
        fail "$cmd が見つかりません"
        errors=$((errors + 1))
    fi
done

# 3. sandbox 依存（bubblewrap, socat）
echo "[3/6] sandbox 依存パッケージ"
for cmd in bwrap socat; do
    if command -v "$cmd" &>/dev/null; then
        ok "$cmd"
    else
        warn "$cmd が見つかりません — sudo apt-get install bubblewrap socat"
        errors=$((errors + 1))
    fi
done

# 4. 設定ファイル
echo "[4/6] 設定ファイル"
if [ -f "$FORGE_ROOT/config/forge.env" ]; then
    ok "config/forge.env"

    # 必須キーのチェック
    for key in FORGE_TEAM_ID LINEAR_API_KEY FORGE_LOG_DIR FORGE_LOCK_DIR FORGE_WORKTREE_DIR; do
        val=$(grep "^${key}=" "$FORGE_ROOT/config/forge.env" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '"' | tr -d "'")
        if [ -z "$val" ]; then
            fail "  $key が未設定"
            errors=$((errors + 1))
        fi
    done
else
    warn "config/forge.env が見つかりません — example からコピーします"
    cp "$FORGE_ROOT/config/forge.env.example" "$FORGE_ROOT/config/forge.env"
    ok "config/forge.env を作成しました（値を設定してください）"
    errors=$((errors + 1))
fi

if [ -f "$FORGE_ROOT/config/repos.conf" ]; then
    ok "config/repos.conf"
else
    warn "config/repos.conf が見つかりません — example からコピーします"
    cp "$FORGE_ROOT/config/repos.conf.example" "$FORGE_ROOT/config/repos.conf"
    ok "config/repos.conf を作成しました（リポジトリを設定してください）"
fi

# 5. ディレクトリ作成
echo "[5/6] ディレクトリ作成"
for dir_key in FORGE_LOG_DIR FORGE_LOCK_DIR FORGE_WORKTREE_DIR; do
    dir=$(grep "^${dir_key}=" "$FORGE_ROOT/config/forge.env" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '"' | tr -d "'")
    if [ -n "$dir" ]; then
        mkdir -p "$dir"
        ok "$dir"
    fi
done

# 6. Linear MCP 接続確認
echo "[6/6] Linear MCP 接続確認"
if claude mcp list 2>/dev/null | grep -q linear; then
    ok "linear-server MCP が設定済み"
else
    warn "linear-server MCP が見つかりません — claude mcp add で設定してください"
    errors=$((errors + 1))
fi

echo
if [ "$errors" -eq 0 ]; then
    echo -e "${GREEN}セットアップ完了${NC}"
else
    echo -e "${YELLOW}${errors} 件の問題があります。上記を確認してください${NC}"
    exit 1
fi
