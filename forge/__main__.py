import sys


def check():
    import json
    import shutil
    import subprocess
    from pathlib import Path

    RED = "\033[0;31m"
    GREEN = "\033[0;32m"
    YELLOW = "\033[0;33m"
    NC = "\033[0m"

    def ok(msg):
        print(f"  {GREEN}✓{NC} {msg}")

    def warn(msg):
        print(f"  {YELLOW}!{NC} {msg}")

    def fail(msg):
        print(f"  {RED}✗{NC} {msg}")

    errors = 0
    root = Path(__file__).resolve().parent.parent

    print("=== forge check ===")
    print()

    # 1. Required commands
    print("[1/4] Required commands")
    for cmd, ver_args in [("claude", ["--version"]), ("gh", ["--version"]), ("git", ["--version"])]:
        if shutil.which(cmd):
            try:
                ver = subprocess.run([cmd] + ver_args, capture_output=True, text=True).stdout.strip().split("\n")[0]
            except Exception:
                ver = "unknown"
            ok(f"{cmd} ({ver})")
        else:
            fail(f"{cmd} not found")
            errors += 1

    # 2. Sandbox dependencies
    print("[2/4] Sandbox dependencies")
    for cmd in ["bwrap", "socat"]:
        if shutil.which(cmd):
            ok(cmd)
        else:
            warn(f"{cmd} not found — sudo apt-get install bubblewrap socat")

    # 3. Configuration files
    print("[3/4] Configuration files")
    config_dir = root / "config"

    settings_path = config_dir / "settings.json"
    if settings_path.exists():
        ok("config/settings.json")
        try:
            settings = json.loads(settings_path.read_text())
        except Exception:
            settings = {}
        for key in ["team", "log_dir", "lock_dir", "worktree_dir"]:
            if not settings.get(key):
                fail(f"  {key} is not set in settings.json")
                errors += 1
    else:
        warn("config/settings.json not found — copying from example")
        shutil.copy(config_dir / "settings.json.example", settings_path)
        ok("Created config/settings.json (please fill in the values)")
        errors += 1

    secrets_path = config_dir / "secrets.env"
    if secrets_path.exists():
        ok("config/secrets.env")
        api_key = ""
        for line in secrets_path.read_text().splitlines():
            if line.startswith("LINEAR_OAUTH_TOKEN="):
                api_key = line.split("=", 1)[1].strip().strip("\"'")
        if not api_key:
            fail("  LINEAR_OAUTH_TOKEN is not set in secrets.env")
            errors += 1
    else:
        warn("config/secrets.env not found — copying from example")
        shutil.copy(config_dir / "secrets.env.example", secrets_path)
        ok("Created config/secrets.env (please fill in LINEAR_OAUTH_TOKEN)")
        errors += 1

    repos_path = config_dir / "repos.conf"
    if repos_path.exists():
        ok("config/repos.conf")
    else:
        warn("config/repos.conf not found — copying from example")
        shutil.copy(config_dir / "repos.conf.example", repos_path)
        ok("Created config/repos.conf (please configure repositories)")

    # 4. Linear MCP connection
    print("[4/4] Linear MCP connection")
    try:
        result = subprocess.run(["claude", "mcp", "get", "linear-server"], capture_output=True, text=True)
        if result.returncode != 0:
            fail("linear-server MCP not found — configure with: claude mcp add -s user linear-server")
            errors += 1
        else:
            scope_line = next((l for l in result.stdout.splitlines() if "Scope:" in l), "")
            if "User" in scope_line:
                ok("linear-server MCP configured (user scope)")
            else:
                fail("linear-server MCP must be user-scoped — reinstall with: claude mcp add -s user linear-server")
                errors += 1
    except Exception:
        warn("Could not check MCP configuration")
        errors += 1

    print()
    if errors == 0:
        print(f"{GREEN}All checks passed{NC}")
        return 0
    else:
        print(f"{YELLOW}{errors} issue(s) found. Please review above{NC}")
        return 1


import argparse

parser = argparse.ArgumentParser(prog="forge")
parser.add_argument("--check", action="store_true")
parser.add_argument("--interval", type=int, default=300,
                    help="polling interval in seconds (default: 300)")
args = parser.parse_args()

if args.check:
    sys.exit(check())
else:
    from .orchestrator import main
    main(interval=args.interval)
