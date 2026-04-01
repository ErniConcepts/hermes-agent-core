#!/usr/bin/env bash
set -euo pipefail

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
BOOTSTRAP_DIR="$HERMES_HOME/product/bootstrap"
USERS_PATH="$BOOTSTRAP_DIR/users.json"
INVITES_PATH="$BOOTSTRAP_DIR/signup_invites.json"

if [[ ! -d "$BOOTSTRAP_DIR" ]]; then
  echo "No product bootstrap state found at $BOOTSTRAP_DIR" >&2
  exit 1
fi

python3 - "$USERS_PATH" "$INVITES_PATH" <<'PY'
import json
import sys
from pathlib import Path

users_path = Path(sys.argv[1])
invites_path = Path(sys.argv[2])

markers = ("e2e", "debug invite", "debug-")
user_fields = ("username", "display_name", "email", "tailscale_login", "tailscale_subject")
invite_fields = ("display_name", "tailscale_login", "invite_id", "token")


def _matches_any_marker(value: object) -> bool:
    text = str(value or "").strip().lower()
    return any(marker in text for marker in markers)


def _load_json(path: Path) -> list[dict]:
    if not path.exists():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    return raw if isinstance(raw, list) else []


def _keep_user(row: dict) -> bool:
    return not any(_matches_any_marker(row.get(field, "")) for field in user_fields)


def _keep_invite(row: dict) -> bool:
    return not any(_matches_any_marker(row.get(field, "")) for field in invite_fields)


users = _load_json(users_path)
invites = _load_json(invites_path)

kept_users = [row for row in users if _keep_user(row)]
kept_invites = [row for row in invites if _keep_invite(row)]

users_path.write_text(json.dumps(kept_users, indent=2) + "\n", encoding="utf-8")
invites_path.write_text(json.dumps(kept_invites, indent=2) + "\n", encoding="utf-8")

print(
    json.dumps(
        {
            "removed_users": len(users) - len(kept_users),
            "removed_invites": len(invites) - len(kept_invites),
            "remaining_users": len(kept_users),
            "remaining_invites": len(kept_invites),
        }
    )
)
PY
