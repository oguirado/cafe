 'use strict';
const test=require('node:test'),assert=require('node:assert/strict');
const {mount,button,textOf,squash,findAll,settle}=require('../fixtures/capsule_control.js');
function snapshot(ids=['cap-a'],revision='1') {
 const capsules={};for(const id of ids)capsules[id]={capsule_id:id,person_label:id==='cap-a'?'Synthetic Alpha':'Synthetic Beta',feeds:{game:{feed_id:'feed-game',role:'game',generation:'9007199254740993'}}};
 return {availability:'available',authority_epoch:'9007199254740993',revision,state:{runtime:'authority_ready',platform:'windows',capsules,windows:[],window_page:{offset:0,total:0,next_offset:null},recordings:[],folder_configured:true,recording_configured:true,obs_available:true,obs_configurable:true,chat_available:false,chat_configurable:true}};
}
test('one desktop exposes pairing and help navigation never starts or stops',async()=>{
 const m=await mount(async()=>snapshot());try{
 assert.equal(button(m.tree,'Capsules'),undefined);assert.equal(button(m.tree,'Diagnostics'),undefined);
 for(const label of ['Nueva cápsula','Ventanas','Carpeta de grabación','Ayuda'])assert.ok(button(m.tree,label),label);
 assert.ok(button(m.tree,'REC'));assert.ok(button(m.tree,'Elegir ventana'));assert.ok(squash(textOf(m.tree)).includes('Preview not available'));
 assert.equal(findAll(m.tree,n=>n.props.id==='cafe-window-browser').length,0);assert.equal(m.ui.probe.reads,0);
 await m.click('Ayuda');await m.click('Diagnóstico');assert.ok(button(m.tree,'Mostrar diagnóstico'));
 assert.equal(m.calls.intents,0);assert.equal(m.calls.detached,0);assert.equal(m.ui.probe.reads,0);
 }finally{m.done();}
});
test('slot display aliases remain stable after authoritative reorder',async()=>{
 const m=await mount(async()=>snapshot(['cap-a','cap-b']));try{
 const cards=()=>findAll(m.tree,n=>n.tag==='article').map(n=>squash(textOf(n)));
 assert.ok(cards().find(t=>t.includes('Synthetic Alpha')).includes('slot-1'));assert.ok(cards().find(t=>t.includes('Synthetic Beta')).includes('slot-2'));
 await m.push(snapshot(['cap-b','cap-a'],'2'));
 assert.ok(cards().find(t=>t.includes('Synthetic Alpha')).includes('slot-1'));assert.ok(cards().find(t=>t.includes('Synthetic Beta')).includes('slot-2'));assert.equal(m.calls.intents,0);
 }finally{m.done();}
});
test('REC sends authoritative capsule id and lossless clocks, queued is not Running',async()=>{
 const m=await mount(async()=>snapshot(),{submit:()=>Promise.resolve({queued:true})});try{await m.click('REC');assert.equal(m.calls.intents,1);assert.equal(m.calls.requests[0].body.capsule_id,'cap-a');assert.equal(m.calls.requests[0].body.op,'start_recording');assert.equal(m.calls.requests[0].authority_epoch,'9007199254740993');assert.equal(m.calls.requests[0].expected_revision,'1');assert.doesNotMatch(squash(textOf(m.tree)),/Recording state: Running/);}finally{m.done();}
});
test('stale readiness is revalidated before controller action even before DOM update',async()=>{
 const m=await mount(async()=>snapshot(),{submit:()=>Promise.resolve({queued:true})});try{
 const oldHandler=button(m.tree,'REC').props.onClick;const stale=snapshot(['cap-a'],'2');stale.state.recording_configured=false;
 await m.push(stale,false);oldHandler();await settle(m.ui.nextTick);assert.equal(m.ui.probe.actions.length,0);assert.equal(m.calls.intents,0);
 }finally{m.done();}
});
test('stale removed capsule cannot trigger a controller action',async()=>{
 const m=await mount(async()=>snapshot(),{submit:()=>Promise.resolve({queued:true})});try{const oldHandler=button(m.tree,'REC').props.onClick;await m.push(snapshot([],'2'),false);oldHandler();await settle(m.ui.nextTick);assert.equal(m.ui.probe.actions.length,0);assert.equal(m.calls.intents,0);}finally{m.done();}
});
test('pending REC rejects duplicate clicks, and no completion is invented',async()=>{
 let resolve;const pending=new Promise(done=>{resolve=done;});const m=await mount(async()=>snapshot(),{submit:()=>pending});try{const handler=button(m.tree,'REC').props.onClick;handler();handler();await settle(m.ui.nextTick);assert.equal(m.calls.intents,1);assert.equal(m.ui.probe.actions.length,1);assert.doesNotMatch(squash(textOf(m.tree)),/Recording state: Running/);resolve({queued:true});await settle(m.ui.nextTick);}finally{m.done();}
});
test('unavailable and known empty workspace are distinct and emit no mutation',async()=>{
 const empty=await mount(async()=>snapshot([]));try{assert.match(squash(textOf(empty.tree)),/No capsules/);assert.equal(empty.calls.intents,0);}finally{empty.done();}
 const absent=await mount(async()=>({availability:'unavailable'}));try{assert.match(squash(textOf(absent.tree)),/unavailable/i);assert.equal(button(absent.tree,'REC'),undefined);assert.equal(absent.calls.intents,0);}finally{absent.done();}
});

test('closing a presentation panel restores the same authoritative capsule without effects',async()=>{
 const m=await mount(async()=>snapshot());try{
 assert.equal(m.ui.presentations.length,1);const api=m.ui.presentations[0];
 assert.equal(api.panels.length,1);assert.equal(api.panels[0].params.capsuleId,'cap-a');
 api.removePanel(api.panels[0]);await settle(m.ui.nextTick);
 assert.equal(api.panels.length,1);assert.equal(api.panels[0].params.capsuleId,'cap-a');
 assert.ok(button(m.tree,'REC'));assert.equal(m.calls.intents,0);assert.equal(m.ui.probe.actions.length,0);
 }finally{m.done();}
});
