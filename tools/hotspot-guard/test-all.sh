#!/bin/sh
# Runs every test tier and prints a summary. Each tier that cannot run here says why and is skipped.
#
#   1. Engine unit tests            python3 only
#   2. Namespace firewall tests     root, iproute2, nftables (applies rules, but only in namespaces)
#   3. Browser interface tests      node 18+, Playwright's Chromium (app/e2e)
#   4. Real-app tests               root, a debug build of the app, tauri-driver, WebKitWebDriver,
#                                   polkit (app/e2e-app)
#
# Usage: sudo ./test-all.sh           runs all tiers that can run here
#        ./test-all.sh --no-root      runs tiers 1 and 3 only, without touching the firewall
set -u

HERE=$(cd "$(dirname "$0")" && pwd)
RUN_ROOT=1
[ "${1:-}" = "--no-root" ] && RUN_ROOT=0
[ "$(id -u)" -eq 0 ] || RUN_ROOT=0
RESULTS=""
DISPLAY="${DISPLAY:-:99}"  # the same default run-tests.sh uses
export DISPLAY
FAILED=0

report() {
  RESULTS="$RESULTS
$1: $2"
  [ "$2" = "FAILED" ] && FAILED=1
}

echo "== 1. engine unit tests"
if (cd "$HERE" && python3 -m unittest 2>&1 | tail -3; rm -rf __pycache__) >/tmp/hg-unit.log 2>&1 && grep -q "^OK" /tmp/hg-unit.log; then
  report "engine unit tests" "passed ($(grep -o 'Ran [0-9]* tests' /tmp/hg-unit.log | head -1))"
else
  report "engine unit tests" "FAILED"; cat /tmp/hg-unit.log
fi

echo "== 2. namespace firewall tests"
if [ "$RUN_ROOT" -eq 1 ] && command -v ip >/dev/null && command -v nft >/dev/null; then
  if (cd "$HERE" && python3 -m unittest discover -s e2e 2>&1 | tail -3) > /tmp/hg-ns.log 2>&1 && grep -q "^OK" /tmp/hg-ns.log; then
    report "namespace firewall tests" "passed ($(grep -o 'skipped=[0-9]*' /tmp/hg-ns.log || echo 'all ran'))"
  else
    report "namespace firewall tests" "FAILED"; cat /tmp/hg-ns.log
  fi
else
  report "namespace firewall tests" "skipped (needs root, ip and nft)"
fi

echo "== 3. browser interface tests"
if command -v npx >/dev/null && [ -d "$HERE/app/e2e/node_modules" ]; then
  if (cd "$HERE/app/e2e" && timeout 900 npx playwright test 2>&1 | tail -3) > /tmp/hg-ui.log 2>&1 && grep -q "passed" /tmp/hg-ui.log && ! grep -q "failed" /tmp/hg-ui.log; then
    report "browser interface tests" "passed ($(grep -o '[0-9]* passed' /tmp/hg-ui.log | head -1))"
  else
    report "browser interface tests" "FAILED"; cat /tmp/hg-ui.log
  fi
else
  report "browser interface tests" "skipped (run: cd app/e2e && npm install)"
fi

echo "== 4. real-app tests"
APP="$HERE/app/src-tauri/target/debug/hotspot-guard-app"
if [ "$RUN_ROOT" -eq 1 ] && [ -x "$APP" ] && command -v tauri-driver >/dev/null && command -v WebKitWebDriver >/dev/null \
   && command -v pkexec >/dev/null && [ -n "${DISPLAY:-}" ] && [ -d "$HERE/app/e2e-app/node_modules" ]; then
  if (cd "$HERE/app/e2e-app" && timeout 900 ./run-tests.sh 2>&1 | grep -E "^# (pass|fail)") > /tmp/hg-app.log 2>&1 && grep -q "fail 0" /tmp/hg-app.log; then
    report "real-app tests" "passed ($(grep '^# pass' /tmp/hg-app.log | sed 's/# //'))"
  else
    report "real-app tests" "FAILED"; cat /tmp/hg-app.log
  fi
else
  report "real-app tests" "skipped (needs root, a debug build, tauri-driver, WebKitWebDriver, pkexec, DISPLAY and npm install in app/e2e-app)"
fi

echo
echo "== summary$RESULTS"
exit $FAILED
