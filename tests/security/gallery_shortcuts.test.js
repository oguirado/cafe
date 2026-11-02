'use strict';
const test=require('node:test'),assert=require('node:assert/strict');
const {load,element,nodeOps,findAll}=require('../fixtures/capsule_control.js');

test('gallery number keys focus current tiles and Escape returns without recreating views',async()=>{
 const ui=await load();const previous=globalThis.document;const listeners=new Map();
 globalThis.document={addEventListener:(name,fn)=>listeners.set(name,fn),removeEventListener:(name,fn)=>{if(listeners.get(name)===fn)listeners.delete(name);}};
 const root=element('root');const props=ui.shallowRef({capsuleIDs:['cap-a','cap-b'],available:true,keyboardEnabled:true});
 const app=ui.createRenderer(nodeOps).createApp({render:()=>ui.h(ui.GalleryGrid,props.value)});
 try{
  app.mount(root);await ui.nextTick();
  assert.equal(typeof listeners.get('keydown'),'function');
  const tiles=()=>findAll(root,n=>n.props['data-capsule-id']!==undefined);
  const original=tiles();const region=()=>findAll(root,n=>n.props['aria-label']==='Capsules')[0];
  function press(key,extra={}){let prevented=false;listeners.get('keydown')({key,target:{closest:()=>null},preventDefault:()=>{prevented=true;},...extra});return prevented;}
  assert.equal(press('2'),true);await ui.nextTick();assert.equal(region().props['data-focused-id'],'cap-b');
  assert.equal(press('2'),true);await ui.nextTick();assert.equal(region().props['data-focused-id'],'cap-b');
  for(const modifier of ['ctrlKey','altKey','metaKey','shiftKey'])assert.equal(press('1',{[modifier]:true}),false);
  assert.equal(press('1',{defaultPrevented:true}),false);
  assert.equal(press('1',{repeat:true}),false);
  for(const selector of ['input','textarea','select','contenteditable','dialog','inert']){
   assert.equal(press('1',{target:{closest:()=>({selector})}}),false);
  }
  assert.equal(press('9'),false);await ui.nextTick();assert.equal(region().props['data-focused-id'],'cap-b');
  props.value={...props.value,keyboardEnabled:false};await ui.nextTick();
  assert.equal(press('1'),false);assert.equal(press('Escape'),false);
  props.value={...props.value,keyboardEnabled:true};await ui.nextTick();
  assert.equal(press('Escape'),true);await ui.nextTick();assert.equal(region().props['data-focused-id'],'');
  assert.deepEqual(tiles(),original);
  props.value={...props.value,capsuleIDs:['cap-b','cap-a']};await ui.nextTick();
  assert.equal(press('1'),true);await ui.nextTick();assert.equal(region().props['data-focused-id'],'cap-b');
  props.value={...props.value,available:false};await ui.nextTick();assert.equal(press('1'),false);
 }finally{app.unmount();assert.equal(listeners.has('keydown'),false);if(previous===undefined)delete globalThis.document;else globalThis.document=previous;}
});
