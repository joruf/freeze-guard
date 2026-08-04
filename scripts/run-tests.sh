#!/bin/bash
# Runs the Freeze Guard test suite.
#
#   scripts/run-tests.sh            everything
#   scripts/run-tests.sh unit       no display, no subprocess flows
#   scripts/run-tests.sh e2e        recorder, kill and dashboard flows
#   scripts/run-tests.sh os         the platform matrix plus this machine's report
#
# GUI tests need a display; without one they are run under xvfb-run when that is
# available and skipped otherwise.
set -uo pipefail

cd "$(dirname "$(readlink -f "$0")")/.." || exit 1
MODE="${1:-all}"

case "$MODE" in
    unit) SELECT=(tests/test_alert_levels.py tests/test_proc_readers.py
                  tests/test_victim_ranking.py tests/test_history_csv.py
                  tests/test_i18n_and_settings.py tests/test_widgets.py) ;;
    e2e)  SELECT=(tests/test_e2e_dashboard.py tests/test_e2e_recorder_and_kill.py
                  tests/test_cli.py) ;;
    os)   SELECT=(tests/test_platform_matrix.py) ;;
    all)  SELECT=(tests) ;;
    *)    echo "usage: $0 [unit|e2e|os|all]"; exit 2 ;;
esac

RUNNER=(python3 -m pytest "${SELECT[@]}")
command -v pytest >/dev/null || RUNNER=(python3 -m unittest discover -s tests -v)

if [ -z "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ] && command -v xvfb-run >/dev/null; then
    echo "==> no display, using xvfb-run"
    xvfb-run -a "${RUNNER[@]}"
else
    "${RUNNER[@]}"
fi
STATUS=$?

if [ "$MODE" = "os" ] || [ "$MODE" = "all" ]; then
    echo
    echo "==> platform report for this machine"
    python3 fg_platform.py || true
fi
exit "$STATUS"
