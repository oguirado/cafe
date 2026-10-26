"""Run actual portable CI stages without converting unavailable runtime paths to pass."""
import argparse
import importlib.metadata
import json
import os
from pathlib import Path
import re
import sys
import tempfile

from ci_support import ROOT,command,executable,prepared_inventory,source_environment

STAGES=('rust','python','node','checkjs','integration','router','publication','claims')


def run_stage(stage,root=ROOT):
    root=Path(root).resolve();names=prepared_inventory(root);env=source_environment(root,names);records=[]
    def run(argv,label):
        result=command(argv,root,env);result['case']=label;records.append(result)
        if result['rc']:raise ValueError(label+'_failed')
        return result
    try:
        python=sys.executable
        if stage=='rust':
            cargo=executable('cargo')
            with tempfile.TemporaryDirectory(prefix='cafe-ci-rust-') as temp:
                env['CARGO_TARGET_DIR']=str(Path(temp)/'target-linux' if os.name!='nt' else Path(temp)/'target-windows-runtime')
                result=run([cargo,'test','--offline','--locked'],'rust_runtime_tests')
                if not re.search(r'test result: ok\. [1-9][0-9]* passed;',result['stdout']):raise ValueError('empty_rust_tests')
                env['CARGO_TARGET_DIR']=str(Path(temp)/'target-windows-check')
                run([cargo,'check','--target','x86_64-pc-windows-msvc','--offline','--locked','--tests'],'windows_crate_and_tests_typecheck')
        elif stage=='python':
            if importlib.metadata.version('tiktoken')!='0.13.0':raise ValueError('wrong_tokenizer_tool_version')
            result=run([python,'-B','-m','unittest','discover','-s','tests/bootstrap'],'python_bootstrap_tests')
            text=result['stdout']+result['stderr']
            if not re.search(r'(?m)^Ran [1-9][0-9]* tests? in ',text) or not re.search(r'(?m)^OK\s*$',text) or 'skipped=' in text:raise ValueError('empty_or_skipped_python_tests')
        elif stage=='node':
            node=executable('node');version=run([node,'--version'],'node_tool_version')
            if version['stdout'].strip()!='v24.14.0':raise ValueError('wrong_node_tool_version')
            result=run([node,'--test','tests/security/backendSupervisor.test.js'],'node_security_tests')
            if not re.search(r'(?:#|ℹ)\s+tests\s+[1-9][0-9]*\b',result['stdout']):raise ValueError('empty_node_test_selection')
        elif stage=='checkjs':run([executable('npm'),'run','check'],'javascript_jsdoc_check')
        elif stage=='integration':
            result=run([python,'-B','tools/run_integration_gate.py'],'integration_one_command')
            data=json.loads(result['stdout'])
            if data.get('status')!='pass' or not data.get('cleanup') or not isinstance(data.get('tests'),int) or data['tests']<=0:raise ValueError('integration_evidence_invalid')
        elif stage=='router':
            result=run([python,'-B','router/tools/build_index_v2.py','--nodes','catalogue/nodes.jsonl','--out','catalogue/router','--corpus','cafe','--check'],'router_byte_identical')
            if result['stdout'].strip()!='byte-identical':raise ValueError('router_check_evidence_invalid')
            result=run([python,'-B','tools/qualify_cafe_router.py'],'router_qualification')
            data=json.loads(result['stdout'])
            if data['verdict']!='router available (uncalibrated)' or data.get('calibrated') is not False or any(row['status']!='pass' for row in data['legs'].values()):raise ValueError('router_not_structurally_available')
        elif stage=='publication':
            result=run([python,'-B','tools/publication_lint.py'],'public_tree_privacy')
            if json.loads(result['stdout']).get('count')!=0:raise ValueError('publication_findings')
        elif stage=='claims':run([python,'-B','tools/claims_check.py'],'operational_claims')
        else:raise ValueError('unknown_ci_stage')
        return {'schema':'cafe.ci-stage.v1','stage':stage,'status':'pass','records':records}
    except (OSError,ValueError,TypeError,KeyError):
        return {'schema':'cafe.ci-stage.v1','stage':stage,'status':'fail','error':'ci_stage_failed_or_unavailable','records':records}


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--stage',choices=STAGES);a=p.parse_args(argv);results=[]
    for stage in [a.stage] if a.stage else STAGES:
        result=run_stage(stage);results.append(result)
        if result['status']!='pass':break
    status='pass' if len(results)==(1 if a.stage else len(STAGES)) and all(row['status']=='pass' for row in results) else 'fail'
    print(json.dumps({'schema':'cafe.ci-run.v1','status':status,'stages':results},sort_keys=True))
    return 0 if status=='pass' else 1


if __name__=='__main__':raise SystemExit(main())
