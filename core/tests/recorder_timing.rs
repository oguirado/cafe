//! Synthetic recorder/preview timing oracles (REC-PARITY).
//!
//! All deadlines use hand-written literals and synthetic `Instant` offsets;
//! no sleeps, processes, FFmpeg, or OS I/O. Expected values below are manual
//! oracles, never computed with the helpers under test.
#[path="../src/capsule_registry.rs"] mod capsule_registry;
#[path="../src/capture_frame_pool.rs"] mod capture_frame_pool;
#[path="../src/recorder_pacer.rs"] mod recorder_pacer;
use std::sync::Arc;
use std::time::{Duration,Instant};
use capsule_registry::Clock;
use capture_frame_pool::{FramePool,PoolLimits,BorrowedFrame,OwnedFrame};
use recorder_pacer::{Pacer,PacerProfile,PacerError,OutputFrame,PreviewSelector,PreviewCounters};

fn pool(slots:usize)->Arc<FramePool>{FramePool::new(PoolLimits{slots,max_frame_bytes:64,max_width:4,max_height:4}).unwrap()}
fn frame(pool:&Arc<FramePool>,clock:u64)->OwnedFrame{let bytes=[7u8,8,9,255].repeat(16);pool.copy_from(BorrowedFrame{data:&bytes,width:4,height:4,source_stride:16,source_time:Clock(clock)}).unwrap()}
fn record60()->PacerProfile{PacerProfile::sixty_fps(5_000_000,60_000_000_000).unwrap()}
/// Drive recording slots 0..60 with a hand-written upper-bound schedule:
/// `i * 16_666_667ns >= floor(i * 1e9 / 60)` because `16_666_667 >= 1e9/60`.
fn drive_sixty(origin:Instant,pool:&Arc<FramePool>,record:PacerProfile)->(Pacer,Vec<OutputFrame>){
    let mut pacer=Pacer::new(record,origin).unwrap();
    pacer.offer(frame(pool,1),origin).unwrap();
    let mut outputs=Vec::new();
    for i in 0u64..60{
        let now=origin+Duration::from_nanos(i*16_666_667);
        let slot=pacer.select(now).unwrap().unwrap();
        assert_eq!(slot.index,i);
        outputs.push(slot);
    }
    (pacer,outputs)
}

#[test]
fn manual_sixty_fps_first_slots_match_hand_oracle(){
    let origin=Instant::now();let pool=pool(1);
    let mut pacer=Pacer::new(PacerProfile::sixty_fps(5_000_000,100_000_000).unwrap(),origin).unwrap();
    pacer.offer(frame(&pool,1),origin).unwrap();
    let expected=[0u64,16_666_666,33_333_333,50_000_000,66_666_666,83_333_333,100_000_000];
    for (i,nanos) in expected.iter().enumerate(){
        let deadline=origin+Duration::from_nanos(*nanos);
        if i>0{assert!(pacer.select(deadline-Duration::from_nanos(1)).unwrap().is_none());}
        let got=pacer.select(deadline).unwrap();
        assert!(got.is_some(),"slot {i} must be due at its hand deadline");
        let slot=got.unwrap();
        assert_eq!(slot.index,i as u64);
        assert_eq!(slot.deadline,deadline);
        assert_eq!(slot.duplicate,i>0);
    }
    let deltas:Vec<u64>=expected.windows(2).map(|w|w[1]-w[0]).collect();
    assert_eq!(deltas,[16_666_666,16_666_667,16_666_667,16_666_666,16_666_667,16_666_667]);
    assert_eq!(pacer.counters().selected,7);
}

#[test]
fn sixty_fps_216001_slots_reach_exact_3600s_without_drift(){
    let origin=Instant::now();let pool=pool(1);
    let mut pacer=Pacer::new(PacerProfile::sixty_fps(5_000_000,7_200_000_000_000).unwrap(),origin).unwrap();
    pacer.offer(frame(&pool,1),origin).unwrap();
    let manual=[0u64,16_666_666,33_333_333,50_000_000,66_666_666,83_333_333,100_000_000];
    for i in 0u64..=216_000{
        let now=origin+Duration::from_nanos(i*16_666_667);
        let slot=pacer.select(now).unwrap().unwrap();
        assert_eq!(slot.index,i);
        if i<7{assert_eq!(slot.deadline,origin+Duration::from_nanos(manual[i as usize]));}
        if i==216_000{assert_eq!(slot.deadline,origin+Duration::from_secs(3600));}
        drop(slot);
    }
    assert_eq!(pacer.counters().selected,216_001);
}

#[test]
fn sixty_fps_constructor_validates_budgets(){
    let ok=PacerProfile::sixty_fps(5_000_000,100_000_000).unwrap();
    assert_eq!((ok.fps_num,ok.fps_den,ok.max_lateness_ns,ok.max_hold_ns),(60,1,5_000_000,100_000_000));
    assert!(matches!(PacerProfile::sixty_fps(0,100_000_000),Err(PacerError::InvalidProfile)));
    assert!(matches!(PacerProfile::sixty_fps(5_000_000,0),Err(PacerError::InvalidProfile)));
}

#[test]
fn preview_rates_emit_exact_counts_over_sixty_slots(){
    let origin=Instant::now();let pool=pool(1);
    let (pacer,outputs)=drive_sixty(origin,&pool,record60());
    assert_eq!(pacer.counters().selected,60);
    for rate in [1u32,10,15,20,24,30,60]{
        let mut selector=PreviewSelector::new(record60(),rate,1).unwrap();
        for out in &outputs{let _=selector.observe(out).unwrap();}
        let counters=selector.counters();
        assert_eq!(counters.emitted,u64::from(rate),"rate {rate}");
        assert_eq!(counters.decimated,60-u64::from(rate),"rate {rate}");
    }
    assert_eq!(pacer.counters().selected,60);
}

#[test]
fn preview_24fps_emits_hand_inventory_with_shared_arcs(){
    let origin=Instant::now();let pool=pool(1);
    let (_pacer,outputs)=drive_sixty(origin,&pool,record60());
    let mut selector=PreviewSelector::new(record60(),24,1).unwrap();
    let mut emitted=Vec::new();
    for out in &outputs{
        if let Some(prev)=selector.observe(out).unwrap(){
            assert!(Arc::ptr_eq(&prev,&out.frame));
            emitted.push(out.index);
        }
    }
    assert_eq!(emitted,[0,3,5,8,10,13,15,18,20,23,25,28,30,33,35,38,40,43,45,48,50,53,55,58]);
    assert_eq!(selector.counters(),PreviewCounters{emitted:24,decimated:36});
}

#[test]
fn preview_rejects_invalid_rates_and_accepts_boundaries(){
    let record60=record60();
    assert!(matches!(PreviewSelector::new(record60,0,1),Err(PacerError::InvalidProfile)));
    assert!(matches!(PreviewSelector::new(record60,60,0),Err(PacerError::InvalidProfile)));
    assert!(matches!(PreviewSelector::new(record60,61,1),Err(PacerError::InvalidProfile)));
    let record30=PacerProfile{fps_num:30,fps_den:1,max_lateness_ns:5_000_000,max_hold_ns:60_000_000_000}.validate().unwrap();
    assert!(matches!(PreviewSelector::new(record30,60,1),Err(PacerError::InvalidProfile)));
    // 120/1 stays valid under unchanged P11 validation; preview above 60 still rejected.
    let record120=PacerProfile{fps_num:120,fps_den:1,max_lateness_ns:5_000_000,max_hold_ns:60_000_000_000}.validate().unwrap();
    assert!(matches!(PreviewSelector::new(record120,61,1),Err(PacerError::InvalidProfile)));
    assert!(matches!(PreviewSelector::new(record120,120,1),Err(PacerError::InvalidProfile)));
    let bad_record=PacerProfile{fps_num:0,fps_den:1,max_lateness_ns:5_000_000,max_hold_ns:60_000_000_000};
    assert!(matches!(PreviewSelector::new(bad_record,30,1),Err(PacerError::InvalidProfile)));
    assert!(PreviewSelector::new(record60,60,1).is_ok());
    assert!(PreviewSelector::new(record60,1,1).is_ok());
    assert!(PreviewSelector::new(record60,60_000,1001).is_ok());
    assert!(PreviewSelector::new(record120,60,1).is_ok());
}

#[test]
fn preview_rejects_nonzero_start_duplicate_and_regression_without_counter_effect(){
    let origin=Instant::now();let pool=pool(1);
    let (_pacer,outputs)=drive_sixty(origin,&pool,record60());
    let mut selector=PreviewSelector::new(record60(),30,1).unwrap();
    assert!(matches!(selector.observe(&outputs[5]),Err(PacerError::ClockRegression)));
    assert_eq!(selector.counters(),PreviewCounters{emitted:0,decimated:0});
    assert!(selector.observe(&outputs[0]).unwrap().is_some());
    assert!(matches!(selector.observe(&outputs[0]),Err(PacerError::ClockRegression)));
    assert_eq!(selector.counters(),PreviewCounters{emitted:1,decimated:0});
    assert!(selector.observe(&outputs[1]).unwrap().is_none());
    assert_eq!(selector.counters(),PreviewCounters{emitted:1,decimated:1});
    assert!(matches!(selector.observe(&outputs[1]),Err(PacerError::ClockRegression)));
    assert!(matches!(selector.observe(&outputs[0]),Err(PacerError::ClockRegression)));
    assert_eq!(selector.counters(),PreviewCounters{emitted:1,decimated:1});
}

#[test]
fn preview_shares_arc_and_retains_no_ownership(){
    let origin=Instant::now();let pool=pool(1);
    let mut pacer=Pacer::new(record60(),origin).unwrap();
    pacer.offer(frame(&pool,1),origin).unwrap();
    let out=pacer.select(origin).unwrap().unwrap();
    let mut selector=PreviewSelector::new(record60(),30,1).unwrap();
    let prev=selector.observe(&out).unwrap().unwrap();
    assert!(Arc::ptr_eq(&prev,&out.frame));
    assert_eq!(Arc::strong_count(&prev),3);
    drop(prev);drop(out);drop(selector);drop(pacer);
    assert_eq!(pool.available(),1);
}

#[test]
fn preview_observation_leaves_recorder_counters_untouched(){
    let origin=Instant::now();let pool=pool(1);
    let (pacer,outputs)=drive_sixty(origin,&pool,record60());
    let before=pacer.counters();
    let mut selector=PreviewSelector::new(record60(),10,1).unwrap();
    for out in &outputs{let _=selector.observe(out).unwrap();}
    assert_eq!(pacer.counters(),before);
    assert_eq!(before.selected,60);
    assert_eq!(selector.counters(),PreviewCounters{emitted:10,decimated:50});
}

#[test]
fn preview_ntsc_60000_1001_emits_1000_over_1001_slots(){
    // Hand oracle: tick(i)=floor(1000*i/1001); the single flat step in
    // 0..=1000 is 0->1 (flat iff i==0 mod 1001), so only index 1 decimates.
    let origin=Instant::now();let pool=pool(1);
    let mut pacer=Pacer::new(record60(),origin).unwrap();
    pacer.offer(frame(&pool,1),origin).unwrap();
    let mut selector=PreviewSelector::new(record60(),60_000,1001).unwrap();
    let mut emitted=Vec::new();
    for i in 0u64..=1000{
        let now=origin+Duration::from_nanos(i*16_666_667);
        let slot=pacer.select(now).unwrap().unwrap();
        assert_eq!(slot.index,i);
        if selector.observe(&slot).unwrap().is_some(){emitted.push(i);}
        drop(slot);
    }
    let expected:Vec<u64>=(0u64..=1000).filter(|&i|i!=1).collect();
    assert_eq!(emitted,expected);
    assert_eq!(selector.counters(),PreviewCounters{emitted:1000,decimated:1});
}
