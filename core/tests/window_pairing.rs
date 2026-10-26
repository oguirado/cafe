//! Independent handwritten fixture oracles; no native Windows success is implied.
#[path = "../src/capsule_registry.rs"] mod capsule_registry;
#[path = "../src/windows_window_registry.rs"] mod windows_window_registry;
#[path = "../src/capsule_pairing.rs"] mod capsule_pairing;
use capsule_registry::{Clock,Registry,Envelope,Command,Role,Outcome,SCHEMA};
use windows_window_registry::{WindowRegistry,FixtureWindow,Rect,WindowError,NativeAdapter};
use capsule_pairing::{PairingBook,PairRequest,PairError,Crop};
fn authority() -> Registry {
 let mut r=Registry::new(Clock(7));
 for (i,id,name) in [(1,"alice","Alice"),(2,"bob","Bob")] {
  assert_eq!(r.apply(Envelope{schema:SCHEMA.into(),kind:"intent".into(),request_id:format!("create-{id}"),authority_epoch:Clock(7),expected_revision:Clock(i-1),body:Command::CreateCapsule{capsule_id:id.into(),person_label:name.into(),generation:Clock(i)}}).state,Outcome::Admitted);
 }
 r
}
fn observed() -> FixtureWindow { FixtureWindow {slot:"private-fixture-slot-canary".into(),incarnation:987654321,process_id:42,process_birth:111222333,executable:"Discord.exe".into(),title:"Alice game".into(),class:"Chrome_WidgetWin_1".into(),rect:Rect{x:0,y:0,width:1000,height:600},visible:true,minimized:false,layout:"layout-one".into()} }
fn setup()->(Registry,WindowRegistry,PairingBook,PairRequest) {
 let r=authority();let mut w=WindowRegistry::new(Clock(7));let v=w.refresh_fixtures(&[observed()]).unwrap().remove(0);
 let q=PairRequest{request_id:"pair-alice-game".into(),authority_epoch:Clock(7),expected_revision:Clock(2),capsule_id:"alice".into(),person_label:"Alice".into(),feed_id:"alice-game".into(),role:Role::Game,window:v.handle,window_generation:Clock(1),layout_revision:Clock(1),crop:Crop{x:0,y:0,width:1000,height:600}};
 (r,w,PairingBook::new(),q)
}
fn unchanged(r:&Registry,b:&PairingBook) { let s=r.snapshot();assert_eq!(s.revision,Clock(2));assert_eq!(s.highest_generation,Clock(2));assert_eq!(s.seen_requests.len(),2);assert!(s.capsules["alice"].feeds.is_empty());assert!(s.capsules["bob"].feeds.is_empty());assert!(b.bindings().is_empty()); }
fn remove(r:&mut Registry) { let reply=r.apply(Envelope{schema:SCHEMA.into(),kind:"intent".into(),request_id:"remove-old".into(),authority_epoch:Clock(7),expected_revision:Clock(3),body:Command::RemoveFeed{capsule_id:"alice".into(),feed_id:"alice-game".into(),generation:Clock(3)}});assert_eq!(reply.state,Outcome::Admitted);assert_eq!(reply.revision,Clock(4)); }
#[test] #[cfg(not(windows))]
fn linux_native_is_explicitly_unsupported() { let mut adapter=NativeAdapter::new(Clock(7));let mut w=WindowRegistry::new(Clock(7));assert_eq!(adapter.enumerate(&mut w),Err(WindowError::Unsupported));assert!(w.snapshots().is_empty());assert_eq!(w.refresh_fixtures(&[observed()]).unwrap().len(),1); }
#[test]
fn opaque_views_do_not_expose_native_identity_or_witnesses() { let (_,w,_,q)=setup();let view=&w.snapshots()[0];let value=serde_json::to_value(view).unwrap();assert_eq!(value["handle"],"window-7-1");assert_eq!(value["generation"],"1");let mut keys=value.as_object().unwrap().keys().map(String::as_str).collect::<Vec<_>>();keys.sort();assert_eq!(keys,vec!["class","executable","generation","handle","layout_revision","minimized","process_id","rect","title","visible"]);let wire=serde_json::to_string(view).unwrap();let debug=format!("{:?} {:?}",view,q);for secret in ["private-fixture-slot-canary","987654321","111222333"] {assert!(!wire.contains(secret));assert!(!debug.contains(secret));} }
#[test]
fn manual_pairing_commits_expected_p05_feed_and_crop() { let(mut r,w,mut b,q)=setup();let result=b.pair(&mut r,&w,q).unwrap();assert_eq!(result.feed_generation,Clock(3));assert_eq!(result.window_generation,Clock(1));assert_eq!(result.person_label,"Alice");assert_eq!(result.crop,Crop{x:0,y:0,width:1000,height:600});let state=r.snapshot();assert_eq!(state.revision,Clock(3));assert_eq!(state.highest_generation,Clock(3));assert_eq!(state.capsules["alice"].feeds["alice-game"].role,Role::Game);assert!(state.capsules["bob"].feeds.is_empty());assert_eq!(b.current("alice-game",&r,&w).unwrap(),result); }
#[test]
fn title_change_preserves_identity_generation_and_feed() { let(mut r,mut w,mut b,q)=setup();let result=b.pair(&mut r,&w,q).unwrap();let mut f=observed();f.title="Alice renamed stream".into();let view=w.refresh_fixtures(&[f]).unwrap().remove(0);assert_eq!(view.handle,result.window);assert_eq!(view.generation,Clock(1));assert_eq!(view.layout_revision,Clock(1));assert_eq!(b.current("alice-game",&r,&w).unwrap(),result);assert_eq!(r.snapshot().revision,Clock(3)); }
#[test]
fn recycled_slot_in_same_process_never_silently_rebinds() { let(mut r,mut w,mut b,q)=setup();let old=b.pair(&mut r,&w,q.clone()).unwrap();let mut f=observed();f.incarnation+=1;let new=w.refresh_fixtures(&[f]).unwrap().remove(0);assert_ne!(new.handle,old.window);assert_eq!(new.generation,Clock(2));assert_eq!(w.resolve(&old.window,Clock(1)),Err(WindowError::StaleWindow));assert_eq!(b.current("alice-game",&r,&w),Err(PairError::Window(WindowError::StaleWindow)));let mut attempt=q;attempt.request_id="rebind-attempt".into();attempt.feed_id="new-feed".into();assert_eq!(b.pair(&mut r,&w,attempt),Err(PairError::Window(WindowError::StaleWindow)));assert_eq!(r.snapshot().revision,Clock(3));assert_eq!(b.bindings(),vec![old]); }
#[test]
fn close_recreate_requires_explicit_remove_and_fresh_confirmation() { let(mut r,mut w,mut b,q)=setup();let old=b.pair(&mut r,&w,q.clone()).unwrap();w.refresh_fixtures(&[]).unwrap();assert!(b.current("alice-game",&r,&w).is_err());let view=w.refresh_fixtures(&[observed()]).unwrap().remove(0);assert_eq!(view.generation,Clock(2));assert_ne!(view.handle,old.window);remove(&mut r);let mut confirmation=q;confirmation.request_id="confirm-new-window".into();confirmation.feed_id="alice-new-game".into();confirmation.expected_revision=Clock(4);confirmation.window=view.handle;confirmation.window_generation=Clock(2);let new=b.pair(&mut r,&w,confirmation).unwrap();assert_eq!(new.feed_generation,Clock(4));assert_eq!(r.snapshot().revision,Clock(5));assert_eq!(r.snapshot().capsules["alice"].feeds.len(),1);assert!(b.current("alice-game",&r,&w).is_err());assert_eq!(b.current("alice-new-game",&r,&w).unwrap(),new); }
#[test]
fn changed_process_birth_invalidates_even_identical_descriptors() { let(_,mut w,_,q)=setup();let mut f=observed();f.process_birth+=1;let new=w.refresh_fixtures(&[f]).unwrap().remove(0);assert_eq!(new.generation,Clock(2));assert_ne!(new.handle,q.window);assert_eq!(w.resolve(&q.window,q.window_generation),Err(WindowError::StaleWindow)); }
#[test]
fn wrong_person_is_rejected_before_any_effect() { let(mut r,w,mut b,mut q)=setup();q.person_label="Bob".into();assert_eq!(b.pair(&mut r,&w,q),Err(PairError::WrongPerson));unchanged(&r,&b); }
#[test]
fn wrong_crop_is_rejected_before_any_effect() { for crop in [Crop{x:999,y:0,width:2,height:1},Crop{x:0,y:600,width:1,height:1},Crop{x:0,y:0,width:0,height:1},Crop{x:u32::MAX,y:0,width:2,height:1}] {let(mut r,w,mut b,mut q)=setup();q.crop=crop;assert_eq!(b.pair(&mut r,&w,q),Err(PairError::InvalidCrop));unchanged(&r,&b);} }
#[test]
fn identical_descriptors_are_ambiguous_even_with_distinct_opaque_handles() { let(mut r,mut w,mut b,q)=setup();let mut other=observed();other.slot="second-private-slot".into();other.incarnation+=1;let views=w.refresh_fixtures(&[observed(),other]).unwrap();assert_eq!(views.len(),2);assert_ne!(views[0].handle,views[1].handle);assert_eq!(b.pair(&mut r,&w,q),Err(PairError::Window(WindowError::AmbiguousDescriptor)));unchanged(&r,&b); }
#[test]
fn executable_only_selection_cannot_reassign_to_another_window() { let(mut r,mut w,mut b,q)=setup();let old=b.pair(&mut r,&w,q).unwrap();let mut replacement=observed();replacement.slot="different-slot".into();replacement.title="Other person".into();replacement.incarnation+=1;w.refresh_fixtures(&[replacement]).unwrap();assert_eq!(b.pair_by_executable("Discord.exe"),Err(PairError::ExecutableOnlyForbidden));assert_eq!(r.snapshot().revision,Clock(3));assert_eq!(b.bindings(),vec![old]);assert!(b.current("alice-game",&r,&w).is_err()); }
#[test]
fn resize_invalidates_crop_without_recycling_window_identity() { let(mut r,mut w,mut b,q)=setup();let old=b.pair(&mut r,&w,q.clone()).unwrap();let mut f=observed();f.rect.width=800;let view=w.refresh_fixtures(&[f]).unwrap().remove(0);assert_eq!(view.handle,old.window);assert_eq!(view.generation,Clock(1));assert_eq!(view.layout_revision,Clock(2));assert_eq!(b.current("alice-game",&r,&w),Err(PairError::StaleCrop));assert_eq!(r.snapshot().revision,Clock(3)); }
#[test]
fn call_layout_change_requires_crop_reconfirmation() { let(mut r,mut w,mut b,q)=setup();b.pair(&mut r,&w,q.clone()).unwrap();w.invalidate_layout(&q.window).unwrap();assert_eq!(b.current("alice-game",&r,&w),Err(PairError::StaleCrop));assert_eq!(r.snapshot().revision,Clock(3)); }
#[test]
fn camera_crop_can_share_the_explicit_call_window() { let(mut r,w,mut b,q)=setup();let game=b.pair(&mut r,&w,q.clone()).unwrap();let mut camera=q;camera.request_id="pair-camera".into();camera.expected_revision=Clock(3);camera.feed_id="alice-camera".into();camera.role=Role::Camera;camera.crop=Crop{x:20,y:10,width:200,height:150};let cam=b.pair(&mut r,&w,camera).unwrap();assert_eq!(cam.window,game.window);assert_eq!(cam.feed_generation,Clock(4));assert_eq!(cam.crop,Crop{x:20,y:10,width:200,height:150});assert_eq!(r.snapshot().revision,Clock(4));assert_eq!(r.snapshot().capsules["alice"].feeds.len(),2); }
#[test]
fn stale_revision_rejection_does_not_commit_pair_metadata() { let(mut r,w,mut b,mut q)=setup();q.expected_revision=Clock(1);assert_eq!(b.pair(&mut r,&w,q),Err(PairError::AuthorityRejected("stale_revision".into())));unchanged(&r,&b); }
#[test]
fn replayed_request_id_never_creates_a_second_feed() { let(mut r,w,mut b,q)=setup();let old=b.pair(&mut r,&w,q.clone()).unwrap();let mut replay=q;replay.feed_id="second-feed".into();replay.role=Role::Camera;replay.expected_revision=Clock(3);assert_eq!(b.pair(&mut r,&w,replay),Err(PairError::AuthorityRejected("duplicate_request_id".into())));assert_eq!(r.snapshot().revision,Clock(3));assert_eq!(r.snapshot().capsules["alice"].feeds.len(),1);assert_eq!(b.bindings(),vec![old]); }
#[test]
fn minimized_or_hidden_window_is_not_a_healthy_binding() { for hidden in [false,true] {let(mut r,mut w,mut b,q)=setup();b.pair(&mut r,&w,q).unwrap();let mut f=observed();if hidden {f.visible=false;}else{f.minimized=true;}w.refresh_fixtures(&[f]).unwrap();assert_eq!(b.current("alice-game",&r,&w),Err(PairError::Window(WindowError::Unavailable)));assert_eq!(r.snapshot().revision,Clock(3));} }
#[test]
fn invalid_fixture_batch_is_atomic() { let(_,mut w,_,q)=setup();assert_eq!(w.refresh_fixtures(&[observed(),observed()]),Err(WindowError::DuplicateObservation));let mut invalid=observed();invalid.process_birth=0;assert_eq!(w.refresh_fixtures(&[invalid]),Err(WindowError::InvalidDescriptor));assert_eq!(w.snapshots().len(),1);assert_eq!(w.resolve(&q.window,Clock(1)).unwrap().generation,Clock(1)); }

/// Fixtures must never turn missing native scenarios into a Windows product pass.
#[cfg(windows)]
#[test]
#[ignore = "Manual native Discord acceptance remains pending; synthetic WGC CI reports availability separately"]
fn native_windows_verification_is_pending() {
    panic!("windows_verification: pending; replace this guard only after native Discord discovery, title/close/recreate, recycled-slot, ambiguity, crop/resize and permission scenarios are executed and evidenced");
}
