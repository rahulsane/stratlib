#!/usr/bin/env bash
# Install the newest release from the S3 bucket and restart the public screener.
# deploy/refresh.ps1 runs this as root over SSM; you can also run it by hand: sudo stratlib-refresh
#
# The bucket holds:
#   release/app.tar.gz   code, config.yaml, research/reports.json, research/output, deploy/server
#   release/stratlib.db   the snapshot from `stratlib publish`
#   release/refresh.sh   this script
#   config/Caddyfile     written by Terraform
set -euo pipefail

BUCKET="${1:-$(cat /etc/stratlib-bucket)}"
ROOT="${STRATLIB_ROOT:-/opt/stratlib}"
APP="$ROOT/app"
VENV="$ROOT/venv"
DB="$ROOT/public/stratlib.db"
PORT=8601
AWS="${AWS_CLI:-/usr/local/bin/aws}"

STAGE="$(mktemp -d "$ROOT/stage.XXXXXX")"
trap 'rm -rf "$STAGE"' EXIT

step() { echo "== $*"; }

step "Downloading release from s3://$BUCKET"
"$AWS" s3 cp "s3://$BUCKET/release/app.tar.gz" "$STAGE/app.tar.gz" --only-show-errors
"$AWS" s3 cp "s3://$BUCKET/release/stratlib.db" "$STAGE/stratlib.db" --only-show-errors
"$AWS" s3 cp "s3://$BUCKET/config/Caddyfile" "$STAGE/Caddyfile" --only-show-errors

mkdir "$STAGE/app"
tar -xzf "$STAGE/app.tar.gz" -C "$STAGE/app"
for required in pyproject.toml config.yaml src/stratlib deploy/server/screener.service; do
  [ -e "$STAGE/app/$required" ] || { echo "release is missing $required" >&2; exit 1; }
done
# Log files outlive releases.
ln -s "$ROOT/logs" "$STAGE/app/logs"
chown -R stratlib:stratlib "$STAGE/app"

# Reinstall the package only when the dependencies change. The install is editable because the app finds
# config.yaml and research/ relative to its source tree.
deps_changed=1
if [ -x "$VENV/bin/stratlib" ] && [ -f "$APP/pyproject.toml" ] && cmp -s "$APP/pyproject.toml" "$STAGE/app/pyproject.toml"; then
  deps_changed=0
fi

step "Stopping the app"
systemctl stop screener 2>/dev/null || true

step "Installing code and snapshot"
rm -rf "$APP.old"
if [ -d "$APP" ]; then
  mv "$APP" "$APP.old"
fi
mv "$STAGE/app" "$APP"
install -o stratlib -g stratlib -m 0444 "$STAGE/stratlib.db" "$DB.new"
mv -f "$DB.new" "$DB"

if [ "$deps_changed" = 1 ]; then
  step "Installing Python dependencies"
  [ -x "$VENV/bin/python" ] || runuser -u stratlib -- python3 -m venv "$VENV"
  runuser -u stratlib -- env PIP_NO_CACHE_DIR=1 "$VENV/bin/pip" install --quiet --upgrade pip
  runuser -u stratlib -- env PIP_NO_CACHE_DIR=1 "$VENV/bin/pip" install --quiet -e "$APP"
fi

if ! cmp -s "$APP/deploy/server/screener.service" /etc/systemd/system/screener.service; then
  step "Updating the systemd unit"
  install -m 0644 "$APP/deploy/server/screener.service" /etc/systemd/system/screener.service
  systemctl daemon-reload
  systemctl enable screener
fi

step "Starting the app"
systemctl start screener

# Reload on every refresh, so a reload that failed last time is retried. Caddy runs as the caddy user, so it
# must own its log directory. A failed reload leaves Caddy on its previous config.
step "Reloading Caddy"
install -m 0644 "$STAGE/Caddyfile" /etc/caddy/Caddyfile
install -d -o caddy -g caddy /var/log/caddy
chown -R caddy:caddy /var/log/caddy
if ! systemctl reload caddy; then
  echo "Caddy did not accept /etc/caddy/Caddyfile. Its log:" >&2
  journalctl -u caddy -n 30 --no-pager >&2 || true
  exit 1
fi

step "Waiting for the app on port $PORT"
for _ in $(seq 1 60); do
  if curl -fs -o /dev/null "http://127.0.0.1:$PORT/"; then
    rm -rf "$APP.old"
    step "Live. $("$VENV/bin/python" - "$DB" <<'PY'
import json, sqlite3, sys
conn = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
row = conn.execute("SELECT body FROM screening_data WHERE key = 'snapshot'").fetchone()
s = json.loads(row[0]) if row else {}
print(f"Snapshot published {s.get('published_at', '?')}, prices through {s.get('prices_through', '?')}.")
PY
)"
    exit 0
  fi
  sleep 1
done

echo "The app did not answer on port $PORT within 60 seconds. Recent log:" >&2
journalctl -u screener -n 40 --no-pager >&2 || true
echo "The previous code is in $APP.old if you need to roll back." >&2
exit 1
