#!/bin/sh
# Runs the real Hotspot Guard app through tauri-driver, inside an isolated network namespace.
#
# Needs: a debug build of the app (cargo build in src-tauri), tauri-driver (cargo install tauri-driver
# --locked), WebKitWebDriver (webkit2gtk-driver), polkit (pkexec, polkitd, dbus), iproute2, nftables,
# python3, node 18+, and an X display (DISPLAY, default :99).
#
# Everything runs in a new network namespace, so blocking changes only that namespace's firewall.
# The app's config goes to a temporary HOME. Run as root, which the namespace and firewall need.
set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../.." && pwd)
export HG_APP_BINARY="${HG_APP_BINARY:-$HERE/../src-tauri/target/debug/hotspot-guard-app}"
export HG_ENGINE="$ROOT/hotspot_guard.py"
export DISPLAY="${DISPLAY:-:99}"

if [ ! -x "$HG_APP_BINARY" ]; then
  echo "app binary not found: $HG_APP_BINARY (build it with: cargo build in app/src-tauri)" >&2
  exit 2
fi
if ! command -v tauri-driver >/dev/null 2>&1; then
  echo "tauri-driver not found (cargo install tauri-driver --locked)" >&2
  exit 2
fi

# pkexec asks polkit, which talks over the system D-Bus. Start both if they are not already running.
# A socket file can outlive its daemon, so check the processes, not the file.
if ! pgrep -x dbus-daemon >/dev/null 2>&1; then
  mkdir -p /run/dbus
  rm -f /run/dbus/system_bus_socket /run/dbus/pid
  dbus-daemon --system --fork
fi
if ! pgrep -x polkitd >/dev/null 2>&1; then
  /usr/lib/polkit-1/polkitd --no-debug >/dev/null 2>&1 &
  sleep 1
fi

WORK=$(mktemp -d)
export HOME="$WORK/home" XDG_DATA_HOME="$WORK/data" XDG_CONFIG_HOME="$WORK/config"
mkdir -p "$HOME" "$XDG_DATA_HOME" "$XDG_CONFIG_HOME"
export HERE WORK

# The state folder is a throwaway tmpfs inside the namespace. Elevated processes started by the app
# share it, and it disappears with the namespace, so real hotspot history is never touched.
exec unshare -n -m sh -c '
  set -eu
  ip link set lo up
  mkdir -p /var/lib/hotspot-guard
  mount -t tmpfs tmpfs /var/lib/hotspot-guard
  tauri-driver --port 4444 >"$WORK/driver.log" 2>&1 &
  DRIVER=$!
  trap "kill $DRIVER 2>/dev/null || true; rm -rf \"$WORK\"" EXIT
  sleep 2
  cd "$HERE"
  node --test --test-concurrency=1 tests/app.test.mjs
'
