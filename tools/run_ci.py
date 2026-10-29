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



def node_test_evidence(text):
    counts={}
    for label in ['tests','pass','fail','cancelled','skipped']:
        matches=re.findall(r'(?m)^(?:#|ℹ)\s+'+label+r'\s+([0-9]+)\s*$',text)
        if len(matches)!=1:raise ValueError('node_evidence_absent_or_ambiguous')
        counts[label]=int(matches[0])
    if counts['tests']<=0 or counts['pass']!=counts['tests'] or any(counts[label] for label in ['fail','cancelled','skipped']):
        raise ValueError('empty_failed_or_skipped_node_tests')
    return counts


def _chat_mcp_e2e(root, python, run):
    """Real producer child, durable files, then no-site MCP stdio child."""
    import asyncio
    import calendar
    from collections import Counter
    from datetime import datetime
    from websockets.asyncio.server import serve

    async def exercise(data):
        received = []
        async def peer(socket):
            for _ in range(3):
                received.append(await asyncio.wait_for(socket.recv(), 5))
            if (not re.fullmatch(r'NICK justinfan[0-9]+\r\n', received[0]) or
                    received[1:] != ['CAP REQ :twitch.tv/tags twitch.tv/commands\r\n', 'JOIN #test_channel\r\n']):
                raise ValueError('producer_anonymous_handshake_failed')
            await socket.send('PING :integration\r\n')
            received.append(await asyncio.wait_for(socket.recv(), 5))
            if received[-1] != 'PONG :integration\r\n':
                raise ValueError('producer_pong_failed')
            lines = [
                '@id=one;tmi-sent-ts=7;display-name=Alpha;badges=subscriber/1 :alpha!alpha@synthetic PRIVMSG #test_channel :literal .* marker',
                '@id=two;tmi-sent-ts=8;display-name=Beta;badges= :beta!beta@synthetic PRIVMSG #test_channel :needle second',
                '@id=three;tmi-sent-ts=9;display-name=Alpha;badges= :alpha!alpha@synthetic PRIVMSG #test_channel :needle third',
            ]
            await socket.send('\r\n'.join(lines) + '\r\n')
            await socket.close()
        async with serve(peer, '127.0.0.1', 0, close_timeout=1) as server:
            port = server.sockets[0].getsockname()[1]
            program = ("import asyncio,json,sys; from connectors.cafe.twitch_chat import run_chat; "
                       "r=asyncio.run(run_chat('test_channel',sys.argv[1],url=sys.argv[2],max_connections=1)); "
                       "print(json.dumps(r,separators=(',',':')))")
            result = await asyncio.to_thread(run, [python, '-B', '-c', program,
                                            str(data), 'ws://127.0.0.1:' + str(port)],
                                            'chat_producer_real_peer_child', 30)
        if len(result['stdout'].encode()) > 4096 or result['stderr']:
            raise ValueError('producer_actor_output_invalid')
        report = json.loads(result['stdout'])
        if report != {'messages': 3, 'malformed': 0, 'reconnects': 0, 'connections': 1}:
            raise ValueError('producer_actor_counters_invalid')
        fields = {'schema', 'channel', 'received_at', 'sent_at', 'message_id',
                  'user_login', 'display_name', 'badges', 'text', 'kind', 'session_id'}
        records = []
        paths = sorted((data / 'twitch/test_channel').glob('*.jsonl'))
        for path in paths:
            raw = path.read_bytes()
            if not raw.endswith(b'\n') or len(raw) > 16384:
                raise ValueError('producer_durable_lines_invalid')
            for line in raw.splitlines():
                value = json.loads(line)
                if (set(value) != fields or value['schema'] != 'cafe.chat_event.v1' or
                        value['channel'] != 'test_channel' or value['kind'] != 'message' or
                        set(value['received_at']) != {'monotonic_ns', 'utc'} or
                        not re.fullmatch(r'[0-9]+', value['received_at']['monotonic_ns']) or
                        value['received_at']['utc'][:10] != path.stem):
                    raise ValueError('producer_schema_fields_invalid')
                records.append(value)
        order = {'one': 0, 'two': 1, 'three': 2}
        records.sort(key=lambda row: order[row['message_id']])
        if ([row['message_id'] for row in records] != ['one', 'two', 'three'] or
                [row['text'] for row in records] != ['literal .* marker', 'needle second', 'needle third'] or
                [row['sent_at'] for row in records] != ['7', '8', '9'] or
                [row['user_login'] for row in records] != ['alpha', 'beta', 'alpha'] or
                records[0]['badges'] != [{'name': 'subscriber', 'version': '1'}] or
                len({row['session_id'] for row in records}) != 1):
            raise ValueError('producer_payload_fields_invalid')
        def millis(row):
            instant = datetime.fromisoformat(row['received_at']['utc'].replace('Z', '+00:00'))
            return calendar.timegm(instant.utctimetuple()) * 1000 + instant.microsecond // 1000
        days = sorted({row['received_at']['utc'][:10] for row in records})
        day = days[0]
        methods = [
            ('list_channels_and_days', {}),
            ('recent_messages', {'channel': 'test_channel', 'limit': 2}),
            ('search_messages', {'channel': 'test_channel', 'query': '.*'}),
            ('top_chatters', {'channel': 'test_channel', 'window_minutes': 1}),
            ('message_rate', {'channel': 'test_channel', 'bucket_seconds': 60}),
            ('session_summary', {'channel': 'test_channel', 'day': day}),
        ]
        requests = [{'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
                        'protocolVersion': '2025-11-25', 'capabilities': {},
                        'clientInfo': {'name': 'cafe-ci', 'version': '1'}}},
                    {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
                    {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'}]
        for number, (name, arguments) in enumerate(methods, 3):
            requests.append({'jsonrpc': '2.0', 'id': number, 'method': 'tools/call',
                             'params': {'name': name, 'arguments': arguments}})
        inputs = data / 'requests.jsonl'
        inputs.write_text(''.join(json.dumps(value, separators=(',', ':')) + '\n' for value in requests), encoding='utf-8')
        # Redirect only this owned wrapper's stdin; the guarded helper launches
        # and reaps the actual no-site MCP child with inherited request input.
        program = ("import os,sys; from pathlib import Path; from tools.run_integration_gate import run_owned; "
                   "assert sys.flags.no_site==1; "
                   "f=open(sys.argv[2],'rb'); os.dup2(f.fileno(),0); "
                   "r=run_owned([sys.executable,'-S','-B','-m','connectors.cafe.mcp_server','--data',sys.argv[3]],"
                   "Path(sys.argv[1]),dict(os.environ),15); f.close(); "
                   "assert len(r.stdout.encode())<=131072 and len(r.stderr.encode())<=4096; "
                   "sys.stdout.write(r.stdout); sys.stderr.write(r.stderr); raise SystemExit(r.returncode)")
        result = await asyncio.to_thread(run, [python, '-S', '-B', '-c', program,
                                             str(root), str(inputs), str(data)],
                                             'mcp_stdlib_no_site_six_query_child', 30)
        if result['stderr'] or len(result['stdout'].encode()) > 131072:
            raise ValueError('mcp_actor_output_invalid')
        responses = [json.loads(line) for line in result['stdout'].splitlines()]
        if [value.get('id') for value in responses] != list(range(1, 9)):
            raise ValueError('mcp_actor_response_correlation_failed')
        if (responses[0].get('result', {}).get('protocolVersion') != '2025-11-25' or
                {value['name'] for value in responses[1]['result']['tools']} != {name for name, _ in methods}):
            raise ValueError('mcp_actor_negotiation_failed')
        results = []
        for response in responses[2:]:
            value = response.get('result', {})
            if value.get('isError') is not False or value['structuredContent']['scan']['corrupt_lines'] or value['structuredContent']['scan']['truncated_lines']:
                raise ValueError('mcp_actor_read_error')
            results.append(value['structuredContent']['data'])
        if results[0] != [{'channel': 'test_channel', 'days': days}]:
            raise ValueError('mcp_channel_day_oracle_failed')
        if ([row['text'] for row in results[1]] != ['needle second', 'needle third'] or
                [row['text'] for row in results[2]] != ['literal .* marker'] or
                results[3] != [{'user_login': 'alpha', 'messages': 2}, {'user_login': 'beta', 'messages': 1}]):
            raise ValueError('mcp_message_query_oracle_failed')
        counts = Counter((millis(row) // 60000) * 60000 for row in records)
        if results[4] != [{'bucket_start_ms': key, 'messages': counts[key]} for key in sorted(counts)]:
            raise ValueError('mcp_rate_oracle_failed')
        selected = [row for row in records if row['received_at']['utc'][:10] == day]
        minutes = Counter(millis(row) // 60000 for row in selected)
        expected = {'messages': len(selected), 'unique_chatters': len({row['user_login'] for row in selected}),
                    'peak_per_minute': max(minutes.values()), 'first_message_ms': min(map(millis, selected)),
                    'last_message_ms': max(map(millis, selected))}
        if results[5] != expected:
            raise ValueError('mcp_summary_numeric_oracle_failed')
        return {'schema': 'cafe.chat-mcp-e2e.v1', 'producer_messages': 3,
                'mcp_tools': 6, 'mcp_no_site': True, 'real_loopback_peer': True}
    with tempfile.TemporaryDirectory(prefix='cafe-data-e2e-') as temporary:
        report = asyncio.run(exercise(Path(temporary)))
    if Path(temporary).exists():
        raise ValueError('chat_mcp_e2e_cleanup_failed')
    report['cleanup'] = True
    return report


def run_stage(stage,root=ROOT):
    root=Path(root).resolve();names=prepared_inventory(root);env=source_environment(root,names);records=[]
    def run(argv,label,timeout=900):
        result=command(argv,root,env,timeout);result['case']=label;records.append(result)
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
            selected=sorted(name for name in names if name.startswith('tests/security/') and name.endswith('.test.js'))
            required={'tests/security/backendSupervisor.test.js','tests/security/electronShell.test.js','tests/security/shellFuses.test.js'}
            if not selected or not required.issubset(selected):raise ValueError('security_test_inventory_incomplete')
            run([executable('npm'),'run','build:preload'],'sandbox_preload_build')
            run([executable('npm'),'run','build:renderer'],'renderer_build')
            for name in selected:
                result=run([node,'--test',name],name)
                result['test_counts']=node_test_evidence(result['stdout'])
        elif stage=='checkjs':run([executable('npm'),'run','check'],'javascript_jsdoc_check')
        elif stage=='integration':
            result=run([python,'-B','tools/run_integration_gate.py'],'integration_one_command')
            data=json.loads(result['stdout'])
            if data.get('status')!='pass' or not data.get('cleanup') or not isinstance(data.get('tests'),int) or data['tests']<=0:raise ValueError('integration_evidence_invalid')
            if importlib.metadata.version('websockets')!='17.2':raise ValueError('wrong_obs_transport_version')
            result=run([python,'-B','tools/run_integration_gate.py','--services'],'service_private_child_one_command')
            data=json.loads(result['stdout'])
            if data.get('status')!='pass' or not data.get('cleanup') or not isinstance(data.get('tests'),int) or data['tests']<=0:raise ValueError('service_integration_evidence_invalid')
            result=run([python,'-B','-m','unittest','discover','-s','tests/integration','-p','test_obs_crop.py'],'obs_hidden_crop_tests')
            text=result['stdout']+result['stderr']
            if not re.search(r'(?m)^Ran [1-9][0-9]* tests? in ',text) or not re.search(r'(?m)^OK\s*$',text) or 'skipped=' in text:raise ValueError('empty_or_skipped_obs_crop_tests')
            result=run([python,'-B','-m','unittest','discover','-s','tests/integration','-p','test_obs_bridge.py'],'obs_bridge_fake_server_tests')
            text=result['stdout']+result['stderr']
            if not re.search(r'(?m)^Ran [1-9][0-9]* tests? in ',text) or not re.search(r'(?m)^OK\s*$',text) or 'skipped=' in text:raise ValueError('empty_or_skipped_obs_tests')

            result=run([python,'-B','-m','unittest','discover','-s','tests/integration','-p','test_twitch_chat.py'],'twitch_real_peer_tests')
            text=result['stdout']+result['stderr']
            if not re.search(r'(?m)^Ran [1-9][0-9]* tests? in ',text) or not re.search(r'(?m)^OK\s*$',text) or 'skipped=' in text:raise ValueError('empty_or_skipped_twitch_chat_tests')

            result=run([python,'-B','-m','unittest','discover','-s','tests/integration','-p','test_mcp_server.py'],'mcp_stdio_query_tests')
            text=result['stdout']+result['stderr']
            if not re.search(r'(?m)^Ran [1-9][0-9]* tests? in ',text) or not re.search(r'(?m)^OK\s*$',text) or 'skipped=' in text:raise ValueError('empty_or_skipped_mcp_server_tests')

            report=_chat_mcp_e2e(root,python,run)
            records.append({'argv':[],'rc':0,'stdout':json.dumps(report,sort_keys=True),'stderr':'','case':'chat_jsonl_mcp_e2e'})

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
