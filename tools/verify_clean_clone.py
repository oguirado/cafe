"""Require a real post-first-commit clone test before push; exports cannot mint receipts."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from ci_support import ROOT,prepared_inventory,profile_digest,tree_digest
from clean_export_test import provision_node_modules,run_profile


def git(root,*args):
    result=subprocess.run(['git','-C',str(root),*args],check=True,capture_output=True,text=True)
    return result.stdout.strip()


def head(root):
    if not (Path(root)/'.git').exists():raise ValueError('literal_clean_clone_pending_no_git')
    value=git(root,'rev-parse','--verify','HEAD')
    if len(value)!=40 or any(c not in '0123456789abcdef' for c in value):raise ValueError('literal_clean_clone_pending_no_commit')
    return value


def receipt_path(root):
    value=Path(git(root,'rev-parse','--git-path','cafe-clean-clone.json'))
    return value if value.is_absolute() else Path(root)/value


def expected_receipt(root):
    root=Path(root).resolve();revision=head(root);names=prepared_inventory(root)
    return {'schema':'cafe.clean-clone-receipt.v1','head':revision,'tree_sha256':tree_digest(root,names),'ci_profile_sha256':profile_digest(root),'mode':'real-git-clone','linux_ci_status':'pass'}


def verify_receipt(root=ROOT):
    root=Path(root).resolve();expected=expected_receipt(root);path=receipt_path(root)
    if not path.is_file():raise ValueError('literal_clean_clone_pending_receipt_absent')
    actual=json.loads(path.read_text())
    if actual!=expected:raise ValueError('literal_clean_clone_receipt_stale_or_invalid')
    return {'schema':'cafe.clean-clone-check.v1','status':'pass','mode':'real-git-clone','head':expected['head']}


def create_receipt(root=ROOT):
    root=Path(root).resolve();expected=expected_receipt(root);names=prepared_inventory(root)
    # The prepared tree must equal the committed HEAD; uncommitted changes cannot be certified.
    tracked=git(root,'ls-tree','-r','--name-only','HEAD').splitlines()
    if sorted(tracked)!=names or git(root,'status','--porcelain','--untracked-files=normal'):
        raise ValueError('commit_and_clean_tree_required_before_clone')
    with tempfile.TemporaryDirectory(prefix='cafe-clean-clone-') as temp:
        clone=Path(temp)/'repo'
        subprocess.run(['git','clone','--no-local','--quiet',str(root),str(clone)],check=True,capture_output=True)
        if head(clone)!=expected['head']:raise ValueError('clone_revision_mismatch')
        clone_names=prepared_inventory(clone)
        if tree_digest(clone,clone_names)!=expected['tree_sha256']:raise ValueError('clone_tree_mismatch')
        provision_node_modules(root,clone)
        report=run_profile(clone,clone_names)
        if report['status']!='pass':raise ValueError('real_clone_ci_gate_failed')
    if expected_receipt(root)!=expected:raise ValueError('source_changed_during_clone')
    path=receipt_path(root);path.write_text(json.dumps(expected,sort_keys=True)+'\n')
    return verify_receipt(root)


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--verify-receipt',action='store_true');a=p.parse_args(argv)
    try:result=verify_receipt() if a.verify_receipt else create_receipt()
    except (OSError,ValueError,TypeError,KeyError,subprocess.SubprocessError):result={'schema':'cafe.clean-clone-check.v1','status':'pending','reason':'first_commit_clean_tree_and_real_clone_receipt_required_before_push'}
    print(json.dumps(result,sort_keys=True));return 0 if result['status']=='pass' else 1


if __name__=='__main__':raise SystemExit(main())
