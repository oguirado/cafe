#!/usr/bin/env bash
# Create an isolated writer checkout from the last fetched integration branch.
set -euo pipefail
if [[ $# != 1 || ! $1 =~ ^[a-z][a-z0-9-]{0,47}$ ]]; then
  printf '%s\n' 'usage: unit.sh <lowercase-slug>' >&2
  exit 2
fi
slug=$1
repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
: "${CAMP:?CAMP must name the external campaign directory}"
worktrees=${CAFE_WORKTREE_ROOT:-"$(dirname -- "$repo")/$(basename -- "$repo")-wt"}
export CAFE_UNIT_REPO=$repo CAFE_UNIT_SLUG=$slug CAFE_UNIT_WT=$worktrees
python3 -B - <<'PY'
import json, os, pathlib, re, subprocess
repo=pathlib.Path(os.environ['CAFE_UNIT_REPO']).resolve()
slug=os.environ['CAFE_UNIT_SLUG']
parent=pathlib.Path(os.environ['CAFE_UNIT_WT']).expanduser()
camp=pathlib.Path(os.environ['CAMP']).expanduser().resolve(strict=True)
if not camp.is_dir() or camp == repo or repo in camp.parents or parent.is_symlink():raise SystemExit('unsafe_worktree_or_campaign_directory')
if parent.resolve()==repo or repo in parent.resolve().parents:raise SystemExit('worktree_root_must_be_external')
parent.mkdir(parents=True,exist_ok=True)
parent=parent.resolve()
destination=parent/('wt-'+slug)
if destination.exists() or destination.is_symlink():raise SystemExit('worktree_already_exists')
state=camp/'out'/'worktrees'/slug
if state.exists() or state.is_symlink():raise SystemExit('unit_state_already_exists')
subprocess.run(['git','-C',str(repo),'fetch','origin','main'],check=True)
revision=subprocess.run(['git','-C',str(repo),'rev-parse','--verify','origin/main^{commit}'],check=True,capture_output=True,text=True).stdout.strip()
if not re.fullmatch('[0-9a-f]{40,64}',revision):raise SystemExit('invalid_fetched_revision')
state.mkdir(parents=True)
target=state/'target-writer'
try:
    subprocess.run(['git','-C',str(repo),'worktree','add','--detach',str(destination),revision],check=True)
except BaseException:
    state.rmdir()
    raise
record={'schema':'cafe.writer-worktree.v1','slug':slug,'base':revision,'worktree':str(destination),'CARGO_TARGET_DIR':str(target),'CARGO_HOME':str(camp/'out'/'P09'/'cargo-home')}
(state/'worktree.json').write_text(json.dumps(record,sort_keys=True)+'\n')
print(json.dumps(record,sort_keys=True))
PY
