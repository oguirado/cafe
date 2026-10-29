// @ts-check
'use strict';
const test=require('node:test');const assert=require('node:assert/strict');const path=require('node:path');const {pathToFileURL}=require('node:url');
async function load(){return import(pathToFileURL(path.resolve(__dirname,'../../src/renderer/control.js')).href);}
function initial(){return {availability:'available',authority_epoch:'9007199254740993',revision:'3',state:{runtime:'authority_ready',platform:'windows',folder_configured:true,recording_configured:true,obs_available:false,obs_configurable:false,chat_available:false,chat_configurable:false,capsules:{'cap-one':{capsule_id:'cap-one',person_label:'Guest',feeds:{}}},windows:[{handle:'opaque-one',generation:'1',layout_revision:'2',title:'Synthetic call',class:'Synthetic',executable:'synthetic.exe',rect:{width:100,height:80}}],window_page:{offset:0,next_offset:null,total:1},recordings:[],last_result:{state:'observed',code:'snapshot'}}};}
async function fixture(value=initial()){
 const {createControls}=await load();let listener=()=>{},off=0,seq=0;const sent=[];
 const bridge={getSnapshot:async()=>value,onSnapshot:fn=>{listener=fn;return ()=>{off++;};},submitIntent:async request=>{sent.push(request);return {queued:true};}};
 const controls=createControls(bridge,()=>`request-${++seq}`);await controls.connect();
 return {controls,sent,bridge,publish:next=>listener(next),off:()=>off};
}
test('closed named actions use exact clocks and never insert optimistic capsules',async()=>{
 const f=await fixture();await f.controls.createCapsule('Second guest');
 assert.deepEqual(f.sent[0],{schema:'cafe.operator.v1',kind:'operator_intent',request_id:'request-2',authority_epoch:'9007199254740993',expected_revision:'3',body:{op:'create_capsule',capsule_id:'cap-request-1',person_label:'Second guest'}});
 assert.equal(f.controls.getState().capsules.length,1);assert.equal(Object.isFrozen(f.controls),true);f.controls.dispose();
});
test('file and folder requests contain only the named native chooser operation',async()=>{
 const f=await fixture();await f.controls.chooseFolder('ignored untrusted path');await f.controls.chooseFfmpeg('ignored untrusted executable','ignored hash');
 assert.deepEqual(f.sent.map(row=>row.body),[{op:'choose_folder'},{op:'choose_ffmpeg'}]);f.controls.dispose();
});
test('pairing uses the listed opaque identity and current person, with closed crop data',async()=>{
 const f=await fixture();await f.controls.pairWindow({capsuleId:'cap-one',role:'camera',windowToken:'opaque-one',windowGeneration:'1',layoutRevision:'2',crop:{x:1,y:2,width:10,height:12}});
 assert.deepEqual(f.sent[0].body,{op:'pair_window',capsule_id:'cap-one',person_label:'Guest',feed_id:'feed-request-1',role:'camera',window_token:'opaque-one',window_generation:'1',layout_revision:'2',crop:{x:1,y:2,width:10,height:12}});f.controls.dispose();
});
test('stale window generation, unknown token and unsafe crop never submit',async()=>{
 const f=await fixture();const value={capsuleId:'cap-one',role:'camera',windowToken:'opaque-one',windowGeneration:'1',layoutRevision:'2',crop:{x:0,y:0,width:10,height:10}};
 for(const changed of [{windowGeneration:'2'},{windowToken:'unknown'},{layoutRevision:'3'},{crop:{x:4294967295,y:0,width:2,height:10}},{crop:{x:0,y:0,width:10,height:10,extra:1}}])await assert.rejects(f.controls.pairWindow({...value,...changed}));
 assert.equal(f.sent.length,0);f.controls.dispose();
});
test('unsafe clock values and changed epochs fail closed, never become revision zero',async()=>{
 for(const changed of [{authority_epoch:9007199254740992},{authority_epoch:'0'},{revision:'03'},{revision:'18446744073709551616'}]){const f=await fixture({...initial(),...changed});assert.equal(f.controls.getState().availability,'unavailable');await assert.rejects(f.controls.chooseFolder());assert.equal(f.sent.length,0);f.controls.dispose();}
 const f=await fixture();f.publish({...initial(),authority_epoch:'8'});assert.equal(f.controls.getState().availability,'unavailable');f.controls.dispose();
});
test('older same-epoch snapshots cannot regress the action revision',async()=>{
 const f=await fixture();f.publish({...initial(),revision:'5'});f.publish({...initial(),revision:'4'});await f.controls.refreshWindows(0);assert.equal(f.sent[0].expected_revision,'5');f.controls.dispose();
});
test('unsupported capture, unpaired feeds, OBS and Twitch pending never become available effects',async()=>{
 const f=await fixture();await assert.rejects(f.controls.startRecording('cap-one'));await assert.rejects(f.controls.stopRecording('cap-one'));await assert.rejects(f.controls.sendToObs('cap-one'));await assert.rejects(f.controls.configureObs(4455,'SYNTHETIC_PRIVATE_VALUE'));await assert.rejects(f.controls.configureTwitch('synthetic_channel'));assert.equal(f.sent.length,0);f.controls.dispose();
 const v=initial();v.state.platform='unsupported';v.state.capsules['cap-one'].feeds={game:{feed_id:'game',role:'game',generation:'2'}};const linux=await fixture(v);await assert.rejects(linux.controls.startRecording('cap-one'));assert.equal(linux.sent.length,0);linux.controls.dispose();
});
test('request errors and snapshot result details cannot reflect a synthetic secret',async()=>{
 const secret='SYNTHETIC_ERROR_SECRET_CANARY';const value=initial();value.state.last_result={state:'rejected',error:secret,password:secret};const f=await fixture(value);
 assert.equal(JSON.stringify(f.controls.getState()).includes(secret),false);f.bridge.submitIntent=async()=>{throw Error(secret);};
 await assert.rejects(f.controls.chooseFolder(),error=>!error.message.includes(secret)&&error.message.includes('Request rejected'));f.controls.dispose();
});
test('dispose removes renderer subscriptions without submitting a recording stop',async()=>{
 const f=await fixture();await f.controls.connect();f.controls.dispose();f.controls.dispose();assert.equal(f.off(),1);assert.equal(f.sent.length,0);await assert.rejects(f.controls.createCapsule('Guest'));assert.equal(f.sent.length,0);
});

test('paired ready fixture submits start and only authoritative Running state admits stop',async()=>{
 const value=initial();value.state.capsules['cap-one'].feeds={game:{feed_id:'game-one',role:'game',generation:'2'}};const f=await fixture(value);
 await f.controls.startRecording('cap-one');assert.deepEqual(f.sent[0].body,{op:'start_recording',capsule_id:'cap-one'});assert.equal(f.controls.getState().recordings.length,0);
 const running=initial();running.revision='4';running.state.capsules=value.state.capsules;running.state.recordings=[{capsule_id:'cap-one',state:'Running',feeds:[{feed_id:'game-one',state:'Recording',written_frames:'60',backpressure:'1'}]}];f.publish(running);
 await f.controls.stopRecording('cap-one');assert.deepEqual(f.sent[1].body,{op:'stop_recording',capsule_id:'cap-one'});assert.equal(f.sent[1].expected_revision,'4');assert.equal(f.controls.getState().recordings[0].state,'Running');f.controls.dispose();
});
