"""Real gitleaks controls; no binary is downloaded or missing scan counted as pass."""
import argparse
import json
from pathlib import Path
import re
import secrets
import shutil
import sys
import tempfile

from ci_support import ROOT,command


def run_controls(binary,root=ROOT):
    root=Path(root).resolve();env={}
    import os
    env.update(os.environ)
    version=command([binary,'version'],root,env)
    if version['rc'] or version['stdout'].strip().lstrip('v')!='8.18.4':raise ValueError('wrong_gitleaks_version')
    cases=[('github_token','gh'+'p_'+secrets.token_hex(18)),('secret_key','secret_'+'key = "'+secrets.token_hex(32)+'"'),('appended_token','SCHEMA_SHA256 = "'+secrets.token_hex(32)+'" '+'gh'+'p_'+secrets.token_hex(18))];records=[]
    with tempfile.TemporaryDirectory(prefix='cafe-secret-controls-') as temp:
        temp=Path(temp)
        for label,text in cases:
            source=temp/label;source.mkdir();(source/'credential.txt').write_text(text+'\n');report=temp/(label+'.json')
            result=command([binary,'detect','--no-git','--source',str(source),'--config',str(root/'.gitleaks.toml'),'--redact=100','--no-banner','--exit-code','1','--report-format','json','--report-path',str(report)],root,env)
            if result['rc']!=1 or not report.is_file():raise ValueError('invalid_secret_negative')
            raw=report.read_text();findings=json.loads(raw)
            if not isinstance(findings,list) or not findings:raise ValueError('missing_secret_negative_finding')
            # Verify both the CLI and its saved evidence are redacted.
            secret=text.split()[-1].strip('"')
            if secret in raw+result['stdout']+result['stderr']:raise ValueError('secret_negative_not_redacted')
            records.append({'case':label,'rc':result['rc'],'findings':len(findings),'redacted':True})
    return records


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--gitleaks');a=p.parse_args(argv);binary=a.gitleaks or shutil.which('gitleaks')
    if binary is None:
        print(json.dumps({'schema':'cafe.secret-controls.v1','status':'pending','reason':'gitleaks_binary_absent_no_download_authorized'}));return 2
    try:report={'schema':'cafe.secret-controls.v1','status':'pass','records':run_controls(binary)}
    except (OSError,ValueError,TypeError,KeyError):report={'schema':'cafe.secret-controls.v1','status':'fail','reason':'secret_scan_controls_failed'}
    print(json.dumps(report,sort_keys=True));return 0 if report['status']=='pass' else 1


if __name__=='__main__':raise SystemExit(main())
