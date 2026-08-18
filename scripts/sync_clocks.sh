#!/usr/bin/env bash
set -euo pipefail

# Read-only clock verification.  The frozen protocol keeps the sender on its
# ordinary upstream NTP sources and has it serve time to the receiver over the
# direct /30 link (see docs/dietpi_operator_runbook.md section 5).  Nothing here
# restarts chrony or repoints it at a public pool: doing that mid-campaign would
# change the frozen environment and invalidate the session.

TIMEOUT=${NLL_WAITSYNC_SECONDS:-60}
TOLERANCE=${NLL_WAITSYNC_TOLERANCE:-0.01}
EXPECTED_SOURCE=${NLL_EXPECTED_SOURCE:-}

command -v chronyc >/dev/null || { echo "chronyc is unavailable" >&2; exit 1; }
systemctl is-active --quiet chrony || { echo "chrony is not running" >&2; exit 1; }

chronyc waitsync "${TIMEOUT}" "${TOLERANCE}" >/dev/null ||
  { echo "chrony did not reach ${TOLERANCE} ppm within ${TIMEOUT} attempts" >&2; exit 1; }

tracking=$(chronyc tracking)
printf '%s\n' "${tracking}"
grep -q 'Leap status *: Normal' <<<"${tracking}" ||
  { echo "chrony leap status is not Normal" >&2; exit 1; }

sources=$(chronyc sources -v)
printf '%s\n' "${sources}"
if [[ -n ${EXPECTED_SOURCE} ]]; then
  grep -qE "^\^\*.*${EXPECTED_SOURCE}" <<<"${sources}" ||
    { echo "${EXPECTED_SOURCE} is not the selected (^*) time source" >&2; exit 1; }
fi
echo "Clock verification passed."
