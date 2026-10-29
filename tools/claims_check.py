"""Discover actionable path/command claims in every new or changed public document.

Exact inherited document digests are reported as deferred normative evidence.
They never prove implementation. All new bytes are checked, including documents
not named in a curated manifest. Commands are parsed, never executed by this gate.
"""
import argparse
import ast
import hashlib
import json
from pathlib import Path,PurePosixPath
import re
import shlex
import sys

from ci_support import ROOT,prepared_inventory

ACTION_PINS={'actions/checkout':'11d5960a326750d5838078e36cf38b85af677262','actions/setup-node':'49933ea5288caeca8642d1e84afbd3f7d6820020','actions/setup-python':'a26af69be951a213d495a4c3e4e4022e16d87065'}
BASELINE_SHA256='375182e4d7fe463d48518e51cae5d38346758e1dcec1508e372f1b49e38f58a2'
GITLEAKS_SHA='ba6dbb656933921c775ee5a2d1c13a91046e7952e9d919f9bac4cec61d628e7d'
STAGES=('rust','python','node','checkjs','integration','router','publication','claims')
EXTERNAL_FLAGS={
 'cargo':{'--offline','--locked','--target','--tests','--bin'},
 'npm':{'--ignore-scripts'},
 'python3':{'-B','-m'},'python':{'-B','-m'},
 'gitleaks':{'--source','--config','--log-opts','--redact','--no-banner','--no-git','--exit-code','--report-format','--report-path'},
 'rustup':{'--profile'},'curl':{'--fail','--location','--output'},
 'tar':{'--extract','--gzip','--file','--directory'},'sha256sum':{'--check','--status'},
}
GENERATED={'params.json':'calibration absent and explicitly unqualified','node_modules/':'lock-derived dependency cache','target/debug/cafe-wgc-synthetic.exe':'built native probe, not shipped executable','.git/cafe-clean-clone.json':'generated only after actual first-commit clean clone'}


def argument_flags(path):
 tree=ast.parse(Path(path).read_text());out=set()
 for node in ast.walk(tree):
  if isinstance(node,ast.Call) and isinstance(node.func,ast.Attribute) and node.func.attr=='add_argument':
   for value in node.args:
    if isinstance(value,ast.Constant) and isinstance(value.value,str) and value.value.startswith('-'):out.add(value.value)
 return out


def resolve_claim(root,origin,value,names):
 value=value.split('#',1)[0]
 if not value:return
 if '://' in value or value.startswith(('mailto:','@')):return
 if value in GENERATED:return
 path=PurePosixPath(value)
 if path.is_absolute() or '\\' in value or ':' in value:raise ValueError('unsafe_document_path')
 # A Markdown link is relative to its document; shell/root code spans use repo paths.
 choices=[(root/origin).parent/value,root/value]
 for choice in choices:
  resolved=choice.resolve()
  if resolved.is_relative_to(root) and (resolved.is_dir() or resolved.relative_to(root).as_posix() in names):return
 raise ValueError('missing_document_path')


def command_claim(root,line,names):
 try:args=shlex.split(line)
 except ValueError:raise ValueError('malformed_document_command')
 if not args:return
 tool=args[0]
 if tool not in EXTERNAL_FLAGS:raise ValueError('unknown_document_tool')
 if tool in ['python','python3']:
  if '-m' in args:
   # Only the pinned public installer and standard unittest invocation are documented.
   i=args.index('-m');module=args[i+1] if i+1<len(args) else ''
   if module not in ['pip','unittest']:raise ValueError('unknown_document_module')
   allowed={'-m','-B','--disable-pip-version-check','--require-hashes','-r','-s','-p'}
  else:
   scripts=[v for v in args[1:] if v.endswith('.py')]
   if len(scripts)!=1:raise ValueError('document_python_entrypoint_absent')
   script=scripts[0];resolve_claim(root,Path('README.md'),script,names)
   allowed=argument_flags(root/script)|{'-B'}
   if script=='tools/run_ci.py' and '--stage' in args:
    i=args.index('--stage')
    if i+1>=len(args) or args[i+1] not in STAGES:raise ValueError('unknown_ci_stage_claim')
 else:allowed=EXTERNAL_FLAGS[tool]
 for arg in args[1:]:
  if arg.startswith('-') and arg.split('=',1)[0] not in allowed:raise ValueError('unknown_document_flag')
 if tool=='npm' and len(args)>2 and args[1]=='run':
  if args[2] not in json.loads((root/'package.json').read_text()).get('scripts',{}):raise ValueError('unknown_npm_script')


def workflow_claims(root,names):
 data=json.loads((root/'.github/workflows/ci.yml').read_text())
 if data.get('permissions')!={'contents':'read'}:raise ValueError('workflow_permissions')
 jobs=data['jobs'];verify=jobs['verify']
 if verify['strategy']['matrix']['os']!=['ubuntu-latest','windows-latest']:raise ValueError('workflow_matrix')
 gates=[]
 for job in jobs.values():
  for step in job.get('steps',[]):
   if 'uses' in step:
    action,sep,revision=step['uses'].partition('@')
    if not sep or ACTION_PINS.get(action)!=revision:raise ValueError('unpinned_or_unreviewed_action')
    if action=='actions/checkout' and step.get('with',{}).get('fetch-depth')!=0:raise ValueError('history_not_complete')
   if step.get('id','').startswith('gate_'):gates.append(step['run'])
   for line in step.get('run','').splitlines():
    if line.strip().startswith(('python ','python3 ','cargo ','npm ')):command_claim(root,line.strip(),names)
 if gates!=['python -B tools/run_ci.py --stage '+stage for stage in STAGES]:raise ValueError('missing_or_unknown_ci_stage')
 secret=json.dumps(jobs['secret-scan'])
 for required in ['v8.18.4',GITLEAKS_SHA,'--log-opts=--all','tools/secret_scan_controls.py','sha256sum --check --status']:
  if required not in secret:raise ValueError('secret_ci_contract_missing')
 if jobs['wgc-synthetic'].get('if')!="needs.wgc-availability.outputs.status == 'pass'":raise ValueError('unavailable_capture_must_not_pass')
 if 'tools/run_wgc_probe.py --assess' not in json.dumps(jobs['wgc-availability']):raise ValueError('availability_evidence_missing')
 return len(gates)


def inspect(root=ROOT):
 root=Path(root).resolve();names=prepared_inventory(root);selected=set(names);findings=[];deferred=[];checked=[]
 baseline_raw=(root/'tools/claims_baseline.json').read_bytes()
 if hashlib.sha256(baseline_raw).hexdigest()!=BASELINE_SHA256:raise ValueError('claim_baseline_changed')
 baseline=json.loads(baseline_raw)
 if baseline.get('schema')!='cafe.claims-baseline.v1':raise ValueError('invalid_claim_baseline')
 inherited={row['path']:row for row in baseline['documents']}
 known_flags=set().union(*EXTERNAL_FLAGS.values())|{'--no-verify','--disable-pip-version-check','--require-hashes'}
 for name in names:
  if name.startswith(('tools/','router/tools/')) and name.endswith('.py'):known_flags.update(argument_flags(root/name))
 for name in names:
  if not name.endswith('.md'):continue
  raw=(root/name).read_bytes();row=inherited.get(name)
  if row and hashlib.sha256(raw).hexdigest()==row['sha256']:
   deferred.append({'path':name,'reason':row['scope']});continue
  checked.append(name);text=raw.decode('utf-8');fenced=False;language=''
  for number,line in enumerate(text.splitlines(),1):
   try:
    if line.strip().startswith('```'):
     fenced=not fenced;language=line.strip()[3:].strip() if fenced else '';continue
    for bare in re.findall(r'(?<![A-Za-z0-9_:/])((?:tools|core|src|connectors|docs|catalogue|router|tests|\.github)/[A-Za-z0-9_./-]*)',line):resolve_claim(root,Path(name),bare.rstrip('.,'),selected)
    for flag in re.findall(r'(?<![A-Za-z0-9_-])--[a-z][a-z0-9-]*',line):
     if flag not in known_flags:raise ValueError('unknown_document_flag')
    for link in re.findall(r'\[[^\]]*\]\(([^)]+)\)',line):resolve_claim(root,Path(name),link,selected)
    # All code spans that look like concrete file paths are claims, even in new documents.
    for span in re.findall(r'`([^`\n]+)`',line):
     if re.match(r'^(?:python3?|cargo|npm|gitleaks|rustup)\s|^\S+\s+--',span):command_claim(root,span,selected)
     elif '/' in span and ' ' not in span and not span.startswith(('http:', 'https:')):resolve_claim(root,Path(name),span,selected)
     elif re.fullmatch(r'[A-Za-z_.-]+\.(?:py|rs|js|json|toml|md|yml|txt)',span):resolve_claim(root,Path(name),span,selected)
    if fenced and line.strip() and not line.lstrip().startswith('#') and (language in ['sh','bash','shell'] or re.match(r'\s*(?:python3?|cargo|npm|gitleaks|rustup)\s|^\S+\s+--',line)):command_claim(root,line.strip(),selected)
   except (OSError,ValueError,KeyError,TypeError):findings.append({'path':name,'line':number,'rule':'unproven_path_tool_or_flag'})
 try:stage_count=workflow_claims(root,selected)
 except (OSError,ValueError,KeyError,TypeError):findings.append({'path':'.github/workflows/ci.yml','line':1,'rule':'unproven_ci_contract'});stage_count=0
 return {'schema':'cafe.claims-check.v1','status':'pass' if not findings else 'fail','findings':findings,'checked_documents':checked,'deferred':deferred,'ci_stages':stage_count}


def main(argv=None):
 p=argparse.ArgumentParser(description=__doc__);p.parse_args(argv)
 try:result=inspect()
 except (OSError,ValueError,KeyError,TypeError):result={'schema':'cafe.claims-check.v1','status':'fail','findings':[{'rule':'claims_evidence_absent_or_invalid'}]}
 print(json.dumps(result,sort_keys=True));return 0 if result['status']=='pass' else 1


if __name__=='__main__':raise SystemExit(main())
