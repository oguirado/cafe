"""Execute the real owned-window probe; unavailable is never native capture pass."""
import argparse
import json
import os
from pathlib import Path
import re
import sys
import tempfile

from ci_support import ROOT,command,executable,prepared_inventory,source_environment


def validate_result(rc,text):
    data=json.loads(text)
    if not isinstance(data,dict) or set(data)!={'status','reason','profile','frames','first_source_time','last_source_time'}:raise ValueError('invalid_probe_fields')
    if data['profile']!='synthetic_test' or not isinstance(data['reason'],str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,79}',data['reason']):raise ValueError('invalid_probe_identity')
    if type(data['frames']) is not int or data['frames']<0:raise ValueError('invalid_probe_frames')
    values=[]
    for key in ['first_source_time','last_source_time']:
        value=data[key]
        if value is not None and (not isinstance(value,str) or not re.fullmatch(r'0|[1-9][0-9]*',value) or int(value)>2**64-1):raise ValueError('invalid_source_clock')
        values.append(None if value is None else int(value))
    if (rc,data['status']) not in [(0,'pass'),(78,'not_available'),(1,'fail')]:raise ValueError('probe_exit_status_mismatch')
    if rc==0 and (data['frames']<2 or any(v is None for v in values) or values[1]<=values[0]):raise ValueError('missing_real_capture_evidence')
    if rc==78 and (data['frames']!=0 or values!=[None,None]):raise ValueError('late_failure_cannot_be_unavailable')
    return data


def execute(root=ROOT):
    root=Path(root).resolve();env=source_environment(root,prepared_inventory(root));records=[]
    with tempfile.TemporaryDirectory(prefix='cafe-wgc-probe-') as temp:
        target=Path(temp)/'target-probe';env['CARGO_TARGET_DIR']=str(target)
        build=command([executable('cargo'),'build','--locked','--offline','--bin','cafe-wgc-synthetic'],root,env);records.append({'case':'build','rc':build['rc']})
        if build['rc']:return {'status':'fail','reason':'probe_build_failed','records':records}
        binary=target/'debug'/('cafe-wgc-synthetic.exe' if os.name=='nt' else 'cafe-wgc-synthetic')
        if not binary.is_file():return {'status':'fail','reason':'probe_binary_missing','records':records}
        result=command([str(binary)],root,env,timeout=60);records.append({'case':'execute','rc':result['rc']})
        try:data=validate_result(result['rc'],result['stdout'])
        except (ValueError,TypeError,KeyError):return {'status':'fail','reason':'probe_protocol_invalid','records':records}
        return {'status':data['status'],'reason':data['reason'],'probe':data,'records':records}


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--assess',action='store_true',help='Publish availability assessment; not a native pass gate.');a=p.parse_args(argv)
    try:report=execute()
    except (OSError,ValueError,TypeError,KeyError):report={'status':'fail','reason':'probe_unavailable_or_failed'}
    report['schema']='cafe.wgc-probe-controller.v1';report['assessment_only']=a.assess
    if report['status']=='not_available':report['hosted_runner_status']='not available on hosted runner'
    output=os.environ.get('GITHUB_OUTPUT');summary=os.environ.get('GITHUB_STEP_SUMMARY')
    if output:
        with open(output,'a',encoding='utf-8') as f:f.write('status='+report['status']+'\n')
    if summary:
        with open(summary,'a',encoding='utf-8') as f:
            f.write('Synthetic WGC: '+('not available on hosted runner; native test is skipped, not passed' if report['status']=='not_available' else report['status'])+'\n')
    print(json.dumps(report,sort_keys=True))
    if report['status']=='fail':return 1
    if report['status']=='not_available':return 0 if a.assess else 78
    return 0


if __name__=='__main__':raise SystemExit(main())
