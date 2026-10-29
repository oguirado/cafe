#!/usr/bin/env bash
# One admission card per owned worker; no machine paths in the published wrapper.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
: "${CAMP:?campaign required}"
job=${1:?job required}; brief=${2:?brief required}; wall=${3:-1500}
[[ "$job" =~ ^cf_[a-zA-Z0-9_]+$ ]] || exit 2
[[ "$wall" =~ ^[1-9][0-9]*$ && -f "$brief" ]] || exit 2
"${CAFE_PYTHON:-python3}" - "$brief" <<'PY'
from pathlib import Path
import re
import sys
text = Path(sys.argv[1]).read_text()
if re.search(r'(?i)\bpid\s+[0-9]+\b|\b[0-9]{4}-[0-9]{2}-[0-9]{2}(?:[T ][0-9]{2}:[0-9]{2})?', text):
    raise SystemExit(67)
PY
if [[ -z "${CAFE_CAPPED_ACTIVE:-}" ]]; then
    role=(); [[ "${CAFE_LANE_ROLE:-context}" != writer ]] || role=(--writer)
    exec "${CAFE_PYTHON:-python3}" -B "$root/ctl/run_capped.py" --kind muse "${role[@]}" -- bash "$0" "$@"
fi
"${CAFE_PYTHON:-python3}" -B "$root/ctl/run_capped.py" --verify-active "$CAFE_CAPPED_ACTIVE"

if [[ "${CAFE_DRY_RUN:-0}" == 1 ]]; then
    printf '%s\n' 'admitted dry-run; no worker started'
else
    # On interruption retain the card until the owned launcher actually exits.
    child_pid=''
    finish_owned() {
        trap '' INT TERM
        if [[ -n "$child_pid" ]]; then wait "$child_pid" || true; fi
        exit 143
    }
    trap finish_owned INT TERM
    bash "$CAMP/launch_muse.sh" "$job" "$brief" "$wall" &
    child_pid=$!
    wait "$child_pid"
fi
