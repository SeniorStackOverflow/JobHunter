#!/usr/bin/env bash
set -Eeuo pipefail

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
COMPOSE="$ROOT/deploy/prod-phone-compose.sh"
PHONEGATE_SOURCE_ENV=${PHONEGATE_SOURCE_ENV:-/srv/phonegate/.env}
PHONEGATE_SECRET_DIR=${PHONEGATE_SECRET_DIR:-/etc/jobhunter/secrets}
PHONEGATE_SECRET_FILE=${PHONEGATE_SECRET_FILE:-$PHONEGATE_SECRET_DIR/phonegate-auth-token}
PHONEGATE_ENABLE_MARKER=${PHONEGATE_ENABLE_MARKER:-/etc/jobhunter/phone-agent-enabled}
PHONEGATE_URL=${PHONEGATE_URL:-https://phonegate.46-225-103-75.sslip.io}
export PHONEGATE_URL PHONEGATE_AUTH_TOKEN_FILE_HOST=$PHONEGATE_SECRET_FILE

phase=preflight
services_started=false
marker_preexisting=false
marker_created=false

fail() {
  printf 'ERROR phase=%s: %s\n' "$phase" "$*" >&2
  exit 1
}

rollback_on_error() {
  rc=$?
  if [ "$rc" -ne 0 ]; then
    printf 'Phone integration activation failed safely in phase=%s (rc=%s).\n' "$phase" "$rc" >&2
    if [ "$marker_created" = true ] && [ "$marker_preexisting" = false ]; then
      rm -f "$PHONEGATE_ENABLE_MARKER"
    fi
    if [ "$services_started" = true ]; then
      printf 'Restoring api/control-worker/call-agent with the base PROD configuration.\n' >&2
      "$ROOT/deploy/prod-compose.sh" up -d --no-deps --force-recreate \
        api control-worker call-agent || true
    fi
  fi
}
trap rollback_on_error EXIT

[ "$(id -u)" -eq 0 ] || fail "run as root"
[ ! -e "$PHONEGATE_ENABLE_MARKER" ] || {
  marker_preexisting=true
  fail "PhoneGate integration is already enabled"
}
[ -r "$PHONEGATE_SOURCE_ENV" ] || fail "PhoneGate source env is not readable"
[ -x "$COMPOSE" ] || fail "production PhoneGate compose wrapper is missing"
[ -x /srv/phonegate/venv/bin/python ] || fail "PhoneGate Python environment is missing"
[ -z "$(git -C "$ROOT" status --porcelain)" ] || fail "PROD checkout is not clean"

phase=secret
install -d -o root -g root -m 0700 "$PHONEGATE_SECRET_DIR"
/srv/phonegate/venv/bin/python - "$PHONEGATE_SOURCE_ENV" "$PHONEGATE_SECRET_FILE" <<'PY'
import os
import secrets
import sys
from pathlib import Path

from dotenv import dotenv_values

source, destination = map(Path, sys.argv[1:])
token = str(dotenv_values(source).get("PHONEGATE_AUTH_TOKEN") or "").strip()
if len(token) < 24 or "\n" in token or "\r" in token or "\0" in token:
    raise SystemExit("PhoneGate auth token is missing or invalid")
temporary = destination.with_name(f".{destination.name}.{secrets.token_hex(8)}.tmp")
descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o440)
try:
    os.write(descriptor, (token + "\n").encode())
    os.fsync(descriptor)
finally:
    os.close(descriptor)
os.chown(temporary, 0, 10001)
os.chmod(temporary, 0o440)
os.replace(temporary, destination)
directory = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
try:
    os.fsync(directory)
finally:
    os.close(directory)
PY
[ "$(stat -c '%u:%g:%a' "$PHONEGATE_SECRET_FILE")" = "0:10001:440" ] || \
  fail "unexpected secret ownership or mode"

phase=compose
"$COMPOSE" config --quiet

phase=phonegate-probe
"$COMPOSE" run --rm --no-deps --entrypoint python call-agent -c '
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

import httpx

url = os.environ["PHONEGATE_URL"].rstrip("/")
parts = urlsplit(url)
if (
    parts.scheme != "https"
    or not parts.hostname
    or parts.username is not None
    or parts.password is not None
    or parts.query
    or parts.fragment
    or parts.path
):
    raise SystemExit("PHONEGATE_URL must be a clean HTTPS origin")
token = Path(os.environ["PHONEGATE_AUTH_TOKEN_FILE"]).read_text().strip()
headers = {"Authorization": f"Bearer {token}"}
with httpx.Client(timeout=15, follow_redirects=False) as client:
    health = client.get(f"{url}/api/health")
    health.raise_for_status()
    status = client.get(f"{url}/api/device/status", headers=headers)
    status.raise_for_status()
    payload = status.json()
    if payload.get("connected") is not True:
        raise SystemExit("PhoneGate device is not connected")
    if payload.get("call_state") != "IDLE":
        raise SystemExit("PhoneGate must be IDLE during activation")
print(json.dumps({"connected": True, "call_state": "IDLE", "auth": "ok"}))
'

phase=services
services_started=true
"$COMPOSE" up -d --no-deps --force-recreate api control-worker call-agent

phase=health
deadline=$((SECONDS + 180))
for service in api control-worker call-agent; do
  while :; do
    container_id=$("$COMPOSE" ps -q "$service")
    [ -n "$container_id" ] || fail "$service container is missing"
    state=$(docker inspect --format '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}' "$container_id")
    case "$state" in
      "running healthy"|"running ") break ;;
      exited*|dead*) fail "$service entered state: $state" ;;
    esac
    [ "$SECONDS" -lt "$deadline" ] || fail "$service health timeout: $state"
    sleep 2
  done
done

phase=secret-audit
for service in api control-worker call-agent; do
  container_id=$("$COMPOSE" ps -q "$service")
  docker inspect "$container_id" --format '{{json .Config.Env}}' | python3 -c '
import json
import sys

values = json.load(sys.stdin)
raw = [value for value in values if value.startswith("PHONEGATE_AUTH_TOKEN=")]
if raw != ["PHONEGATE_AUTH_TOKEN="]:
    raise SystemExit("raw PhoneGate token found in container environment")
'
done
"$COMPOSE" exec -T call-agent python -c '
import os
from pathlib import Path

p = Path(os.environ["PHONEGATE_AUTH_TOKEN_FILE"])
assert p.is_file() and len(p.read_text().strip()) >= 24
'

phase=runtime-probe
"$COMPOSE" exec -T call-agent python -c '
import json
import os
from pathlib import Path

import httpx

url = os.environ["PHONEGATE_URL"].rstrip("/")
token = Path(os.environ["PHONEGATE_AUTH_TOKEN_FILE"]).read_text().strip()
r = httpx.get(
    f"{url}/api/device/status",
    headers={"Authorization": f"Bearer {token}"},
    timeout=15,
)
r.raise_for_status()
payload = r.json()
if payload.get("connected") is not True:
    raise SystemExit("PhoneGate device disconnected after activation")
print(json.dumps({"connected": True, "call_state": payload.get("call_state")}))
'

phase=enable-marker
python3 - "$PHONEGATE_ENABLE_MARKER" <<'PY'
import os
import secrets
import sys
from pathlib import Path

destination = Path(sys.argv[1])
temporary = destination.with_name(f".{destination.name}.{secrets.token_hex(8)}.tmp")
descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
try:
    os.write(descriptor, b"enabled\n")
    os.fsync(descriptor)
finally:
    os.close(descriptor)
os.chown(temporary, 0, 0)
os.chmod(temporary, 0o600)
os.replace(temporary, destination)
directory = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
try:
    os.fsync(directory)
finally:
    os.close(directory)
PY
marker_created=true

phase=complete
printf 'Phone integration activated. PROD env unchanged; secret is file-mounted.\n'
services_started=false
