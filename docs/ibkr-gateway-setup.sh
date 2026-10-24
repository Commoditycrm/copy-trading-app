#!/usr/bin/env bash
# Kopyya — IBKR gateway setup for a subscriber's own Mac or Linux machine.
#
# Does, once:  installs Java if missing (Mac via Homebrew), downloads IBKR's
# Client Portal Gateway, allows the Kopyya server's Tailscale address in the
# gateway's config, and starts the gateway detached. Re-running it is safe.
#
# Usage:
#   bash ibkr-gateway-setup.sh <Kopyya server Tailscale address>
#   e.g.  bash ibkr-gateway-setup.sh 100.118.121.100
#
# Afterwards: open https://localhost:5000 in a browser, sign in with your IBKR
# username, close the tab. Do that again every trading day — IBKR ends the
# session at midnight New York time.
set -euo pipefail

SERVER_IP="${1:-}"
if [[ ! "$SERVER_IP" =~ ^100\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "Usage: bash $0 <Kopyya server Tailscale address, e.g. 100.118.121.100>" >&2
  exit 1
fi

GW_DIR="$HOME/clientportal.gw"
ZIP_URL="https://download2.interactivebrokers.com/portal/clientportal.gw.zip"

say() { printf '\n==> %s\n' "$*"; }

# ── Java ────────────────────────────────────────────────────────────────────
if ! command -v java >/dev/null 2>&1; then
  if [[ "$(uname)" == "Darwin" ]] && command -v brew >/dev/null 2>&1; then
    say "Installing Java (Temurin) with Homebrew — you may be asked for your Mac password"
    brew install --cask temurin
  else
    echo "Java is not installed. Install it from https://adoptium.net and run this script again." >&2
    exit 1
  fi
fi
say "Java: $(java -version 2>&1 | head -1)"

# ── Gateway download ────────────────────────────────────────────────────────
if [[ ! -x "$GW_DIR/bin/run.sh" ]]; then
  say "Downloading IBKR Client Portal Gateway"
  mkdir -p "$GW_DIR"
  curl -fsSL -o "$GW_DIR/gw.zip" "$ZIP_URL"
  unzip -oq "$GW_DIR/gw.zip" -d "$GW_DIR"
  rm -f "$GW_DIR/gw.zip"
  chmod +x "$GW_DIR/bin/run.sh"
else
  say "Gateway already present in $GW_DIR"
fi

# ── Allow-list: only the Kopyya server (plus this machine) may call the API ──
CONF="$GW_DIR/root/conf.yaml"
cp -n "$CONF" "$CONF.orig" 2>/dev/null || true
python3 - "$CONF" "$SERVER_IP" <<'PY'
import re, sys
path, ip = sys.argv[1], sys.argv[2]
s = open(path).read()
# Replace the whole ips: block with a strict one: localhost + the Kopyya server.
new_block = (
    "    ips:\n"
    "      allow:\n"
    "        - 127.0.0.1\n"
    f"        - {ip}\n"
    "      deny: []\n"
)
s2, n = re.subn(r"^    ips:\n(?:      .*\n|        .*\n)+", new_block, s, count=1, flags=re.M)
if n != 1:
    sys.exit("could not find the ips: block in conf.yaml — edit it by hand")
open(path, "w").write(s2)
print(f"allow-list set: 127.0.0.1 and {ip}")
PY

# ── Start (or restart) the gateway detached ────────────────────────────────
if pgrep -f "clientportal.gw" >/dev/null 2>&1; then
  say "Stopping the running gateway so the new allow-list applies"
  pkill -f "clientportal.gw" || true
  sleep 2
fi
say "Starting the gateway"
( cd "$GW_DIR" && nohup bin/run.sh root/conf.yaml > "$GW_DIR/gateway.log" 2>&1 & )
for _ in $(seq 1 20); do
  sleep 1
  if curl -sk -m 3 -o /dev/null https://localhost:5000/sso/Login; then break; fi
done

TS_IP="$(tailscale ip -4 2>/dev/null || /Applications/Tailscale.app/Contents/MacOS/Tailscale ip -4 2>/dev/null || true)"
say "Done. Next steps:"
cat <<EOF
  1. Open https://localhost:5000 in your browser, accept the certificate warning,
     sign in with your IBKR username, and close the tab when it says
     "Client login succeeds". Repeat this every trading day.
  2. In Kopyya → Broker → IBKR, use this gateway address:
       https://${TS_IP:-<your Tailscale 100.x address>}:5000
     (your Tailscale address is shown in the Tailscale app if blank above)

  Gateway log: $GW_DIR/gateway.log
  Stop:        pkill -f clientportal.gw
  Start again: bash $0 $SERVER_IP
EOF
