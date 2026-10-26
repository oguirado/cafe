"""Install only this repository's reviewed fail-closed pre-push guard."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

ROOT=Path(__file__).resolve().parents[1]


def install(root=ROOT):
    root=Path(root).resolve()
    result=subprocess.run(['git','-C',str(root),'rev-parse','--git-path','hooks/pre-push'],check=True,capture_output=True,text=True)
    destination=Path(result.stdout.strip());destination=destination if destination.is_absolute() else root/destination
    if not (root/'.git').is_dir() or not destination.resolve().is_relative_to((root/'.git').resolve()) or destination.resolve()!=destination:
        raise ValueError('hook_destination_not_local_git_directory')
    source=root/'tools/hooks/pre-push';raw=source.read_bytes()
    if destination.exists() and destination.read_bytes()!=raw:raise ValueError('different_existing_hook_requires_review')
    destination.parent.mkdir(parents=True,exist_ok=True);destination.write_bytes(raw);destination.chmod(0o755)
    return {'schema':'cafe.hook-install.v1','status':'pass','source_sha256':hashlib.sha256(raw).hexdigest()}


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.parse_args(argv)
    try:result=install()
    except (OSError,ValueError,subprocess.SubprocessError):result={'schema':'cafe.hook-install.v1','status':'fail','reason':'hook_install_failed_or_conflicting'}
    print(json.dumps(result,sort_keys=True));return 0 if result['status']=='pass' else 1


if __name__=='__main__':raise SystemExit(main())
