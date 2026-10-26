"""Test an exact prepared-file export, never describe it as a committed clean clone."""
import argparse
import json
import os
from pathlib import Path
import shlex
import shutil
import sys
import tempfile

from ci_support import ROOT,ci_profile,command,prepared_inventory,profile_digest,source_environment,tree_digest,validate_inventory
from run_ci import STAGES


def provision_node_modules(source,destination):
    source=Path(source);destination=Path(destination)
    lock=json.loads((source/'package-lock.json').read_text())
    packages=lock.get('packages',{})
    if set(packages)!={'','node_modules/typescript'} or packages['node_modules/typescript'].get('version')!='5.9.3':raise ValueError('unapproved_npm_lock')
    node=source/'node_modules'
    if not node.is_dir() or json.loads((node/'typescript/package.json').read_text()).get('version')!='5.9.3':raise ValueError('approved_node_modules_unavailable')
    if (destination/'package-lock.json').read_bytes()!=(source/'package-lock.json').read_bytes():raise ValueError('npm_lock_changed')
    (destination/'node_modules').symlink_to(node.resolve(),target_is_directory=True)


def run_profile(root,names):
    root=Path(root).resolve();validate_inventory(root,names);env=source_environment(root,names);records=[]
    for text in ci_profile(root):
        args=shlex.split(text)
        if len(args)!=5 or args[:3] not in [['python3','-B','tools/run_ci.py'],['python','-B','tools/run_ci.py']] or args[3]!='--stage' or args[4] not in STAGES:
            raise ValueError('unknown_ci_gate_command')
        result=command([sys.executable,*args[1:]],root,env);records.append({'command':text,'rc':result['rc'],'stdout':result['stdout'],'stderr':result['stderr']})
        if result['rc']:return {'status':'fail','records':records}
        data=json.loads(result['stdout'])
        if data.get('status')!='pass':return {'status':'fail','records':records}
    if len(records)!=len(STAGES):raise ValueError('incomplete_ci_gate_profile')
    return {'status':'pass','records':records}


def clean_export(source=ROOT):
    source=Path(source).resolve();names=prepared_inventory(source);before=tree_digest(source,names)
    with tempfile.TemporaryDirectory(prefix='cafe-clean-export-') as temp:
        root=Path(temp)/'repo';root.mkdir()
        for name in names:
            target=root/name;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(source/name,target)
            target.chmod(0o755 if os.access(source/name,os.X_OK) else 0o644)
        if (root/'.git').exists() or any((root/n).exists() for n in ['local','target','_scratch','_target']):raise ValueError('ignored_input_copied')
        if tree_digest(root,names)!=before:raise ValueError('export_hash_mismatch')
        provision_node_modules(source,root)
        result=run_profile(root,names)
        if tree_digest(root,names)!=before:raise ValueError('export_inputs_changed')
        result.update(schema='cafe.clean-export.v1',mode='clean-export test',files=len(names),content_manifest_sha256=before,ci_profile_sha256=profile_digest(root),node_dependency_mode='approved lock-matched offline symlink',literal_clean_clone='PENDING after operator first commit and before push',gitleaks_local='pending if binary absent; no download',external_inventory_or_machine_config=False)
    result['cleanup']=not Path(temp).exists()
    return result


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.parse_args(argv)
    try:result=clean_export()
    except (OSError,ValueError,TypeError,KeyError):result={'schema':'cafe.clean-export.v1','status':'fail','reason':'clean_export_failed_or_prerequisite_unavailable'}
    print(json.dumps(result,sort_keys=True));return 0 if result.get('status')=='pass' and result.get('cleanup') else 1


if __name__=='__main__':raise SystemExit(main())
