"""Portable CI contracts and explicit prepared-inventory metadata, no tree discovery."""
from pathlib import Path
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys

from run_integration_gate import run_owned

ROOT = Path(__file__).resolve().parents[1]
IGNORED_ROOTS = {'.git','local','node_modules','target','_scratch','_target'}


def canonical_json(value):
    return json.dumps(value,sort_keys=True,separators=(',',':')).encode()


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def validate_inventory(root,names):
    root=Path(root).resolve()
    if not isinstance(names,list) or not names or any(not isinstance(n,str) for n in names) or names!=sorted(set(names)):
        raise ValueError('invalid_prepared_inventory')
    for name in names:
        parts=name.split('/')
        if not name or name.startswith('/') or '\\' in name or ':' in name or '\0' in name or parts[0] in IGNORED_ROOTS or any(p in ['', '.', '..'] or p.startswith('.env') or p in ['operator.token','launch.token','auth.json'] or p.endswith('.key') for p in parts):
            raise ValueError('invalid_prepared_inventory')
        path=root/name
        if path.resolve()!=path or not path.is_file():
            raise ValueError('invalid_prepared_inventory')
    return names


def prepared_inventory(root=ROOT):
    root=Path(root).resolve()
    if (root/'.git').exists():
        result=subprocess.run(['git','-C',str(root),'ls-files','--cached','--others','--exclude-standard','-z'],check=True,capture_output=True)
        names=sorted(set(result.stdout.decode().split('\0'))-{''})
    else:
        value=os.environ.get('CAFE_PREPARED_INVENTORY_JSON')
        if value is None:raise ValueError('prepared_inventory_absent')
        names=json.loads(value)
    return validate_inventory(root,names)


def tree_digest(root,names):
    root=Path(root).resolve();validate_inventory(root,names);h=hashlib.sha256()
    for name in names:
        h.update(name.encode()+b'\0'+hashlib.sha256((root/name).read_bytes()).digest())
    return h.hexdigest()


def ci_profile(root=ROOT):
    data=json.loads((Path(root)/'.github/workflows/ci.yml').read_text())
    steps=data['jobs']['verify']['steps']
    commands=[step['run'] for step in steps if step.get('id','').startswith('gate_')]
    if not commands or len(commands)!=len(set(commands)):raise ValueError('empty_or_duplicate_ci_profile')
    return commands


def profile_digest(root=ROOT):
    root=Path(root)
    return sha(canonical_json({'commands':ci_profile(root),'workflow':sha((root/'.github/workflows/ci.yml').read_bytes()),'npm_lock':sha((root/'package-lock.json').read_bytes()),'cargo_lock':sha((root/'Cargo.lock').read_bytes()),'python_tools':sha((root/'requirements-ci.txt').read_bytes())}))


def executable(name):
    result=shutil.which(name)
    if result is None:raise ValueError('required_tool_unavailable')
    return result


def command(argv,root,env,timeout=900):
    argv=[str(v) for v in argv]
    if os.name=='nt' and Path(argv[0]).suffix.casefold() in ['.cmd','.bat']:
        argv=[os.environ.get('COMSPEC','cmd.exe'),'/d','/s','/c',subprocess.list2cmdline(argv)]
    result=run_owned(argv,Path(root),env,timeout)
    return {'argv':argv,'rc':result.returncode,'stdout':result.stdout,'stderr':result.stderr}


def source_environment(root,names):
    env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1',CARGO_NET_OFFLINE='true',CAFE_PREPARED_INVENTORY_JSON=canonical_json(names).decode())
    for key in ['CAFE_ROUTER_INVENTORY','CAFE_INTEGRATION_CONFIG','CAFE_AUTHORITY_FIXTURE','CAFE_PRIVATE_POLICY','ROUTER_PRIVATE_POLICY','PYTHONPATH','PYTHONHOME','PYTHONSTARTUP','NODE_PATH','NODE_OPTIONS','RUSTC','RUSTFLAGS','CARGO_ENCODED_RUSTFLAGS','CARGO_TARGET_DIR','TIKTOKEN_CACHE_DIR','DATA_GYM_CACHE_DIR','RUSTC_WRAPPER','RUSTC_WORKSPACE_WRAPPER']:
        env.pop(key,None)
    return env
