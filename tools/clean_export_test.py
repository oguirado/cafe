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


NODE_PROFILE_SHA256 = '30b9ab4eea4dbad87db437f23cc27e448648067241300d2deb3077ce88c6ed0c'
FUSES_INTEGRITY = 'sha512-LoKJUXNiJ4JM8IIrUltSHI+8pkogaGj5wmJx81jE/Wk3g2w1/kfMbTEKNoY5kitGE8hiC12h32R/1SlywFtxXg=='


def validate_node_profile(source):
    source=Path(source)
    manifest=json.loads((source/'package.json').read_text())
    lock_raw=(source/'package-lock.json').read_bytes();lock=json.loads(lock_raw)
    if manifest.get('optionalDependencies') or manifest.get('peerDependencies'):
        raise ValueError('unapproved_npm_root')
    packages=lock.get('packages',{});declared=manifest.get('dependencies',{})
    dev=manifest.get('devDependencies',{})
    if (set(packages)=={'','node_modules/typescript'} and declared=={'typescript':'5.9.3'} and not dev and
        packages['node_modules/typescript'].get('version')=='5.9.3' and
        packages[''].get('dependencies',{})=={'typescript':'5.9.3'} and not packages[''].get('devDependencies',{})):
        return 'legacy-typescript-only'
    import hashlib
    from urllib.parse import urlparse
    policy_raw=(source/'tools/node_profile.json').read_bytes()
    if hashlib.sha256(policy_raw).hexdigest()!=NODE_PROFILE_SHA256:raise ValueError('node_profile_changed')
    policy=json.loads(policy_raw)
    if policy.get('schema')!='cafe.node-profile.v1' or manifest.get('name')!=policy['name']:
        raise ValueError('unapproved_npm_profile')
    if declared!=policy['dependencies'] or dev!=policy['devDependencies']:
        raise ValueError('unapproved_npm_root')
    root=packages.get('',{})
    if root.get('dependencies',{})!=declared or root.get('devDependencies',{})!=dev:
        raise ValueError('npm_root_lock_mismatch')
    if hashlib.sha256(lock_raw).hexdigest()!=policy['lock_sha256'] or len(packages)!=policy['locked_packages']+1:
        raise ValueError('unapproved_npm_lock')
    for package,row in packages.items():
        if not package:continue
        name=row.get('name') or package.rsplit('node_modules/',1)[-1]
        if name.startswith('@xterm/') or name in ['node-pty','vue-tsc']:raise ValueError('excluded_npm_package')
        parsed=urlparse(row.get('resolved',''))
        if row.get('link') or parsed.scheme!='https' or parsed.netloc!='registry.npmjs.org' or parsed.query or parsed.fragment or not parsed.path.endswith('.tgz'):
            raise ValueError('unapproved_npm_resolution')
        if not row.get('integrity','').startswith('sha512-'):raise ValueError('npm_integrity_absent')
    fuses=packages.get('node_modules/@electron/fuses',{})
    if fuses.get('version')!='2.1.3' or fuses.get('integrity')!=FUSES_INTEGRITY or policy['fuses_integrity']!=FUSES_INTEGRITY:
        raise ValueError('unapproved_fuses_integrity')
    return 'frozen-shell-profile'


def verify_installed_node_profile(destination):
    destination=Path(destination);lock=json.loads((destination/'package-lock.json').read_text())
    expected={name:row for name,row in lock['packages'].items() if name};seen=set()
    def modules(directory,prefix):
        if directory.is_symlink() or not directory.is_dir():raise ValueError('installed_node_graph_invalid')
        candidates=[]
        for child in directory.iterdir():
            if child.name.startswith('.'):continue
            if child.is_symlink() or not child.is_dir():raise ValueError('installed_node_graph_invalid')
            if child.name.startswith('@'):
                for package in child.iterdir():candidates.append((package,prefix+child.name+'/'+package.name))
            else:candidates.append((child,prefix+child.name))
        for package,name in candidates:
            if package.is_symlink() or name not in expected:raise ValueError('installed_unapproved_node_package')
            row=expected[name];metadata=json.loads((package/'package.json').read_text())
            actual_name=row.get('name') or name.rsplit('node_modules/',1)[-1]
            if metadata.get('name')!=actual_name or metadata.get('version')!=row['version']:raise ValueError('installed_node_package_mismatch')
            seen.add(name)
            nested=package/'node_modules'
            if nested.exists():modules(nested,name+'/node_modules/')
    modules(destination/'node_modules','node_modules/')
    if any(name not in seen and not row.get('optional') for name,row in expected.items()):raise ValueError('installed_node_package_absent')


def provision_node_modules(source,destination):
    source=Path(source);destination=Path(destination)
    profile=validate_node_profile(source)
    if (destination/'package-lock.json').read_bytes()!=(source/'package-lock.json').read_bytes():raise ValueError('npm_lock_changed')
    if profile=='legacy-typescript-only':
        verify_installed_node_profile(source)
        node=source/'node_modules'
        if not node.is_dir() or json.loads((node/'typescript/package.json').read_text()).get('version')!='5.9.3':raise ValueError('approved_node_modules_unavailable')
        if (destination/'.git').exists():
            shutil.copytree(node,destination/'node_modules',symlinks=True)
        else:
            (destination/'node_modules').symlink_to(node.resolve(),target_is_directory=True)
        return profile
    if (destination/'package.json').read_bytes()!=(source/'package.json').read_bytes() or (destination/'tools/node_profile.json').read_bytes()!=(source/'tools/node_profile.json').read_bytes():
        raise ValueError('npm_profile_input_changed')
    if (source/'.npmrc').exists() or (destination/'.npmrc').exists():raise ValueError('project_npm_config_not_opened')
    cache_value=os.environ.get('CAFE_NPM_CACHE')
    if not cache_value:raise ValueError('approved_npm_cache_unavailable')
    cache=Path(cache_value).resolve(strict=True)
    if not cache.is_dir():raise ValueError('approved_npm_cache_unavailable')
    node=shutil.which('node');npm=shutil.which('npm')
    if node is None or npm is None:raise ValueError('approved_node_runtime_unavailable')
    keep={'PATH','HOME','USERPROFILE','LANG','LC_ALL','SYSTEMROOT','SYSTEMDRIVE','WINDIR','COMSPEC','TEMP','TMP','TMPDIR','XDG_RUNTIME_DIR','DBUS_SESSION_BUS_ADDRESS'}
    env={key:value for key,value in os.environ.items() if key.upper() in keep}
    with tempfile.TemporaryDirectory(prefix='cafe-npm-config-') as temp:
        directory=Path(temp);user_config=directory/'user.conf';global_config=directory/'global.conf'
        user_config.write_text('');global_config.write_text('')
        env.update(NPM_CONFIG_USERCONFIG=str(user_config),NPM_CONFIG_GLOBALCONFIG=str(global_config),
                   NPM_CONFIG_CACHE=str(cache),NPM_CONFIG_OFFLINE='true',NPM_CONFIG_IGNORE_SCRIPTS='true',
                   NPM_CONFIG_AUDIT='false',NPM_CONFIG_FUND='false',NPM_CONFIG_REGISTRY='https://registry.npmjs.org/')
        version=command([node,'--version'],destination,env)
        import re
        if version['rc'] or not re.fullmatch(r'v24\.\d+\.\d+\s*',version['stdout']):raise ValueError('approved_node_runtime_unavailable')
        result=command([npm,'ci','--offline','--ignore-scripts','--no-audit','--no-fund'],destination,env)
        if result['rc']:raise ValueError('approved_npm_cache_provision_failed')
    validate_node_profile(destination)
    verify_installed_node_profile(destination)
    if (destination/'package-lock.json').read_bytes()!=(source/'package-lock.json').read_bytes():raise ValueError('npm_lock_changed')
    return profile


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
        result.update(schema='cafe.clean-export.v1',mode='clean-export test',files=len(names),content_manifest_sha256=before,ci_profile_sha256=profile_digest(root),node_dependency_mode='approved exact-profile dependency provisioning',literal_clean_clone='PENDING after operator first commit and before push',gitleaks_local='pending if binary absent; no download',external_inventory_or_machine_config=False)
    result['cleanup']=not Path(temp).exists()
    return result


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.parse_args(argv)
    try:result=clean_export()
    except (OSError,ValueError,TypeError,KeyError):result={'schema':'cafe.clean-export.v1','status':'fail','reason':'clean_export_failed_or_prerequisite_unavailable'}
    print(json.dumps(result,sort_keys=True));return 0 if result.get('status')=='pass' and result.get('cleanup') else 1


if __name__=='__main__':raise SystemExit(main())
