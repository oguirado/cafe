'use strict';
const test=require('node:test');const assert=require('node:assert/strict');const path=require('node:path');const{pathToFileURL}=require('node:url');
const load=()=>import(pathToFileURL(path.resolve(__dirname,'../../src/renderer/control.js')).href);
const value=()=>({availability:'available',state:{last_result:{state:'rejected',code:'pair_window_ambiguous'},capsules:{cap:{person_label:'PRIVATE_ACCOUNT_CANARY',feeds:{}}},windows:[{visible:true,minimized:false,title:'PRIVATE_TITLE_CANARY',password:'PRIVATE_PASSWORD_CANARY'}]}});
test('diagnostics expose fixed code and counts without private fields',async()=>{const{buildDiagnosticReport}=await load();const r=buildDiagnosticReport(value());assert.equal(r.result_code,'pair_window_ambiguous');assert.equal(r.paired_feed_count,0);assert.equal(r.listed_window_count,1);assert.equal(Object.isFrozen(r),true);assert.equal(JSON.stringify(r).includes('PRIVATE_'),false);assert.deepEqual(Object.keys(r).sort(),['schema','bridge','result_state','result_code','capsule_count','paired_feed_count','listed_window_count','minimized_window_count','visible_window_count'].sort());});
test('unknown error and state are never echoed',async()=>{const{buildDiagnosticReport}=await load();const s=value();s.state.last_result={state:'PRIVATE_STATE',code:'PRIVATE_ERROR'};const r=buildDiagnosticReport(s);assert.equal(r.result_state,'unknown');assert.equal(r.result_code,'unknown_result');assert.equal(JSON.stringify(r).includes('PRIVATE_'),false);});
test('missing evidence stays null rather than zero',async()=>{const{buildDiagnosticReport}=await load();const r=buildDiagnosticReport(null);assert.equal(r.bridge,'unknown');assert.equal(r.paired_feed_count,null);assert.equal(r.listed_window_count,null);});
test('read action calls getSnapshot only and never mutates',async()=>{const{readDiagnostics}=await load();let reads=0,writes=0;const r=await readDiagnostics({getSnapshot:async()=>{reads++;return value();},submitIntent:()=>{writes++;throw new Error('MUTATION');}});assert.equal(reads,1);assert.equal(writes,0);assert.equal(r.result_code,'pair_window_ambiguous');});
test('bridge exceptions are replaced without leaking messages',async()=>{const{readDiagnostics}=await load();const r=await readDiagnostics({getSnapshot:async()=>{throw new Error('PRIVATE_PASSWORD_CANARY');}});assert.equal(r.bridge,'unknown');assert.equal(JSON.stringify(r).includes('PRIVATE_'),false);});

test('missing per-window evidence is null instead of a healthy zero',async()=>{const{buildDiagnosticReport}=await load();const s=value();s.state.windows=[{}];const r=buildDiagnosticReport(s);assert.equal(r.listed_window_count,1);assert.equal(r.minimized_window_count,null);assert.equal(r.visible_window_count,null);});

test('recording and startup diagnostics preserve fixed codes only',async()=>{const{buildDiagnosticReport}=await load();for(const code of ['recording_ffmpeg_not_approved','recording_hardware_preflight_blocked','recording_first_frame_timeout','recording_crop_invalid','recording_effect_interrupted','ffmpeg_startup_selection_blocked']){const s=value();s.state.last_result={state:'interrupted',code,error:'PRIVATE_ERROR_CANARY'};const r=buildDiagnosticReport(s);assert.equal(r.result_code,code);assert.equal(JSON.stringify(r).includes('PRIVATE_'),false);}});

test('capture state rejection diagnostics preserve only approved closed codes',async()=>{
  const{buildDiagnosticReport}=await load();
  for(const code of [
    'recording_capture_unhealthy',
    'recording_capture_minimized',
    'recording_capture_frozen',
    'recording_capture_closed',
    'recording_capture_device_lost',
    'recording_capture_resize_required',
    'recording_capture_generation_mismatch',
    'recording_capture_backpressure',
    'recording_capture_border_unavailable',
    'recording_capture_source_failed',
    'recording_capture_clock_regression',
  ]){
    const s=value();s.state.last_result={state:'interrupted',code,error:'PRIVATE_ERROR_CANARY'};
    const r=buildDiagnosticReport(s);assert.equal(r.result_code,code);
    assert.equal(JSON.stringify(r).includes('PRIVATE_'),false);
    assert.ok(Buffer.byteLength(JSON.stringify(r))<4096);
  }
  for(const code of ['recording_capture_PRIVATE_ERROR_CANARY','recording_capture_source_failed:PRIVATE_ERROR_CANARY']){
    const s=value();s.state.last_result={state:'interrupted',code};
    const r=buildDiagnosticReport(s);assert.equal(r.result_code,'unknown_result');
    assert.equal(JSON.stringify(r).includes('PRIVATE_'),false);
  }
});
