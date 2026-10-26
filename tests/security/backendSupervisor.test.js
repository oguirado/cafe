// @ts-check
'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { spawn } = require('node:child_process');
const { once } = require('node:events');
const { createBackendSupervisor } = require('../../src/main/backendSupervisor.js');
const { createCafeBridge } = require('../../src/preload/cafeBridge.js');
const { replaceRenderer } = require('./rendererReplacement.js');
const EPOCH = '9007199254740993';
// Independent backend fixture: state transitions below are hand-authored test data.
const CHILD = String.raw`
const mode=process.argv[1];
let revision=0n;
function output(value){process.stdout.write(JSON.stringify(value)+'\n');}
function snapshot(){output({kind:'snapshot',authority_epoch:'9007199254740993',revision:String(revision),state:{recording:true,events:Number(revision)}});}
if(mode==='eof'){process.stdout.end();setTimeout(()=>process.exit(0),25);}
else if(mode==='giant'){process.stdout.write('x'.repeat(200000));setTimeout(()=>process.exit(0),100);}
else {
 snapshot();
 if(mode==='stderr') process.stderr.write('e'.repeat(200000));
 if(mode!=='blocked'){
 let pending='';process.stdin.setEncoding('utf8');process.stdin.on('data',chunk=>{
 pending+=chunk;let n;
 while((n=pending.indexOf('\n'))>=0){let request=JSON.parse(pending.slice(0,n));pending=pending.slice(n+1);
 if(request.request_id==='stale'){output({kind:'delta',authority_epoch:'9007199254740993',base_revision:'0',revision:'1',state:{recording:false,events:99}});}
 else if(request.request_id==='clock'){output({kind:'snapshot',authority_epoch:9007199254740992,revision:'1',state:{recording:false}});}
 else {let base=revision;revision++;output({kind:'delta',authority_epoch:'9007199254740993',base_revision:String(base),revision:String(revision),state:{recording:true,events:Number(revision)}});}
 }});}
 setInterval(()=>{},1000);
}`;
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
async function until(predicate) {
 for (let attempt=0;attempt<200;attempt++) {if(predicate())return;await delay(10);}
 assert.fail('Expected observable state was not reached');
}
function intent(request_id='next',revision='0',padding='') {
 return {schema:'cafe.control.v1',kind:'intent',request_id,authority_epoch:EPOCH,expected_revision:revision,
 body:{op:'create_capsule',capsule_id:'guest',person_label:padding||'Guest',generation:'1'}};
}
function setup(t,mode='normal',limits={}) {
 const children=[];let frame={};
 const supervisor=createBackendSupervisor({command:process.execPath,args:['-e',CHILD,mode],authorizedOrigin:'app://cafe',getAuthorizedFrame:()=>frame,...limits,
 spawn:(command,args,options)=>{const child=spawn(command,args,options);children.push(child);return child;}});
 t.after(async()=>{supervisor.dispose();await Promise.all(children.map(async child=>{
 if(child.exitCode===null&&child.signalCode===null) await Promise.race([once(child,'exit'),delay(2000).then(()=>{throw Error('Owned child did not exit');})]);
 }));});
 const caller=()=>({origin:'app://cafe',frame,isMainFrame:true});
 return {supervisor,children,caller,replace:()=>{frame={};},bridge:()=>{const identity=caller();return createCafeBridge({
 invoke:async(channel,payload)=>{
 const method=({'cafe:getSnapshot':'getSnapshot','cafe:submitIntent':'submitIntent'})[channel];
 if(!method)throw Error('Unknown channel');return supervisor.handle(identity,method,payload);},
 subscribe:(channel,listener)=>{assert.equal(channel,'cafe:snapshot');return supervisor.subscribe(identity,listener);}});}};
}
test('real child PID is owned, start is idempotent and decimal clocks remain exact',async t=>{
 const s=setup(t);s.supervisor.start();await until(()=>s.supervisor.getStatus().availability==='available');
 const status=s.supervisor.getStatus();assert.equal(status.pid,s.children[0].pid);assert.equal(status.authority_epoch,EPOCH);assert.equal(status.revision,'0');assert.deepEqual(status.state,{recording:true,events:0});
 s.supervisor.start();assert.equal(s.children.length,1);
});
test('unauthorized origin and non-main or foreign frame are rejected',async t=>{
 const s=setup(t);s.supervisor.start();await until(()=>s.supervisor.getStatus().availability==='available');
 for(const caller of [{...s.caller(),origin:'https://untrusted.invalid'},{...s.caller(),frame:{}},{...s.caller(),isMainFrame:false}]){
 assert.throws(()=>s.supervisor.handle(caller,'getSnapshot'));assert.throws(()=>s.supervisor.handle(caller,'submitIntent',intent()));assert.throws(()=>s.supervisor.subscribe(caller,()=>{}));}
 assert.equal(s.supervisor.getStatus().revision,'0');
});
test('arbitrary channels and process lifecycle methods are unavailable to renderer',async t=>{
 const s=setup(t);s.supervisor.start();await until(()=>s.supervisor.getStatus().availability==='available');
 for(const method of ['invoke','arbitrary','dispose','start','spawn','kill','__proto__'])assert.throws(()=>s.supervisor.handle(s.caller(),method,{}));
 const bridge=s.bridge();assert.deepEqual(Object.keys(bridge).sort(),['getSnapshot','onSnapshot','submitIntent']);assert.equal(Object.isFrozen(bridge),true);assert.equal(bridge.ipcRenderer,undefined);
});
test('oversized unterminated line becomes visibly unavailable without parsing',async t=>{
 const s=setup(t,'giant',{maxLineBytes:1024,maxQueueBytes:8192});const seen=[];s.supervisor.subscribe(s.caller(),v=>seen.push(v));s.supervisor.start();
 await until(()=>s.supervisor.getStatus().availability==='unavailable');assert.match(s.supervisor.getStatus().reason,/line/i);assert.equal(s.supervisor.getStatus().state,null);assert.ok(seen.some(v=>v.availability==='unavailable'));
});
test('stale delta cannot replace a valid snapshot or recording state',async t=>{
 const s=setup(t);s.supervisor.start();await until(()=>s.supervisor.getStatus().availability==='available');
 s.supervisor.handle(s.caller(),'submitIntent',intent('advance'));await until(()=>s.supervisor.getStatus().revision==='1');
 s.supervisor.handle(s.caller(),'submitIntent',intent('stale','1'));await until(()=>s.supervisor.getStatus().availability==='unavailable');
 const status=s.supervisor.getStatus();assert.equal(status.revision,'1');assert.deepEqual(status.state,{recording:true,events:1});assert.throws(()=>s.supervisor.handle(s.caller(),'submitIntent',intent('again')));
});
test('backend EOF and death publish unavailable and never silently restart',async t=>{
 const s=setup(t,'eof');const seen=[];s.supervisor.subscribe(s.caller(),v=>seen.push(v));s.supervisor.start();
 await until(()=>s.supervisor.getStatus().availability==='unavailable');assert.ok(seen.some(v=>v.availability==='unavailable'));assert.throws(()=>s.supervisor.handle(s.caller(),'submitIntent',intent()));s.supervisor.start();assert.equal(s.children.length,1);
});
test('renderer replacement preserves real authority PID and recording, revokes old frame',async t=>{
 const s=setup(t);s.supervisor.start();await until(()=>s.supervisor.getStatus().availability==='available');
 const old=s.caller(),first=s.bridge(),events=[];const unsubscribe=first.onSnapshot(v=>events.push(v));const pid=s.supervisor.getStatus().pid;
 unsubscribe();s.replace();const replacement=replaceRenderer(s.bridge());s.supervisor.start();const snapshot=await replacement.getSnapshot();assert.equal(snapshot.pid,pid);assert.equal(snapshot.state.recording,true);assert.equal(s.children.length,1);assert.throws(()=>s.supervisor.handle(old,'getSnapshot'));await assert.rejects(()=>first.getSnapshot());
 await replacement.submitIntent(intent());await until(()=>s.supervisor.getStatus().revision==='1');assert.deepEqual(s.supervisor.getStatus().state,{recording:true,events:1});assert.equal(s.supervisor.getStatus().pid,pid);
});
test('stderr flood is drained separately while stdout protocol continues',async t=>{
 const s=setup(t,'stderr');s.supervisor.start();await until(()=>s.supervisor.getStatus().stderrBytes>=200000&&s.supervisor.getStatus().availability==='available');s.supervisor.handle(s.caller(),'submitIntent',intent());await until(()=>s.supervisor.getStatus().revision==='1');assert.equal(s.supervisor.getStatus().state.events,1);
});
test('real blocked child applies bounded write backpressure and rejects queue overflow',async t=>{
 const s=setup(t,'blocked',{maxLineBytes:1024,maxQueueBytes:8192});s.supervisor.start();await until(()=>s.supervisor.getStatus().availability==='available');
 let overflow=false;for(let i=0;i<10000;i++){try{s.supervisor.handle(s.caller(),'submitIntent',intent('q'+i,'0','p'.repeat(400)));}catch(error){assert.match(String(error),/queue|backpressure|capacity/i);overflow=true;break;}}
 assert.equal(overflow,true);assert.ok(s.children[0].stdin.writableLength<=8192+1024);assert.equal(s.children.length,1);
});
test('unsafe numeric clock and unknown envelope key are rejected before write',async t=>{
 const s=setup(t);s.supervisor.start();await until(()=>s.supervisor.getStatus().availability==='available');
 for(const payload of [{...intent(),authority_epoch:9007199254740992},{...intent(),expected_revision:'18446744073709551616'},{...intent(),extra:true},{...intent(),expected_revision:'1'}])assert.throws(()=>s.supervisor.handle(s.caller(),'submitIntent',payload));
 s.supervisor.handle(s.caller(),'submitIntent',intent('clock'));await until(()=>s.supervisor.getStatus().availability==='unavailable');assert.equal(s.supervisor.getStatus().revision,'0');
});
test('preload never exposes raw transport and snapshots cannot mutate authority',async t=>{
 const s=setup(t);s.supervisor.start();await until(()=>s.supervisor.getStatus().availability==='available');const bridge=s.bridge();const snapshot=await bridge.getSnapshot();try{snapshot.state.recording=false;}catch{}assert.equal(s.supervisor.getStatus().state.recording,true);
 const seen=[];const unsubscribe=bridge.onSnapshot(v=>seen.push(v));await bridge.submitIntent(intent());await until(()=>seen.some(v=>v.revision==='1'));unsubscribe();assert.equal(bridge.invoke,undefined);assert.equal(bridge.send,undefined);
});
