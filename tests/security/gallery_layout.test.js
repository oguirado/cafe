'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { load, element, nodeOps, settle, findAll, textOf } = require('../fixtures/capsule_control.js');

const empty = { columns: 0, rows: 0, width: 0, height: 0 };

test('gallery layout rejects missing, invalid and impossible bounds without invented sizes', async () => {
  const { galleryLayout } = await load();
  for (const args of [[], [0,1280,720], [-1,1280,720], [1.5,1280,720],
    [NaN,1280,720], [Infinity,1280,720], [1,NaN,720], [1,Infinity,720],
    [1,1280,undefined], [1,0,720], [1,1280,-1], [1,1280,720,-1],
    [1,1280,720,NaN], [1,1280,720,Infinity], [12,1,1,8]]) {
    assert.deepEqual(galleryLayout(...args), empty, String(args));
  }
});

test('gallery maximizes equal widescreen tiles over every possible row and column count', async () => {
  const { galleryLayout } = await load();
  for (let count = 1; count <= 12; count++) {
    for (const [width,height] of [[1920,1080],[1280,500],[720,1280],[640,360],[320,640],[3840,300]]) {
      for (const gap of [0,8,16]) {
        const actual = galleryLayout(count,width,height,gap);
        assert.ok(actual.width > 0);
        assert.equal(actual.rows,Math.ceil(count/actual.columns));
        assert.ok(Math.abs(actual.width / actual.height - 16/9) < 1e-10);
        assert.ok(actual.columns*actual.width+(actual.columns-1)*gap <= width+1e-8);
        assert.ok(actual.rows*actual.height+(actual.rows-1)*gap <= height+1e-8);
        for (let rows = 1; rows <= count; rows++) {
          for (let columns = 1; columns <= count; columns++) {
            if (rows*columns < count) continue;
            const candidateHeight = Math.min((height-(rows-1)*gap)/rows,
              (width-(columns-1)*gap)/columns*9/16);
            assert.ok(actual.height >= candidateHeight-1e-8,
              JSON.stringify({count,width,height,gap,rows,columns,actual}));
          }
        }
        assert.deepEqual(galleryLayout(count,width,height,gap),actual,'deterministic');
      }
    }
  }
  assert.deepEqual(galleryLayout(1,1280,720),{ columns:1, rows:1, width:1280, height:720 });
  assert.equal(galleryLayout(12,1280,720).columns,4);
  assert.equal(galleryLayout(12,720,1280).columns,2);
});

test('gallery rejects malformed and unavailable identities instead of showing an empty wall or tiles', async () => {
  const ui = await load();
  for (const ids of [['cap-a','cap-a'],[' cap-a'],['1'],['cap a'],[null]]) {
    const root = element('root');
    const props = ui.shallowRef({ capsuleIDs: ids, available: true });
    const app = ui.createRenderer(nodeOps).createApp({ render: () => ui.h(ui.GalleryGrid, props.value) });
    app.config.warnHandler = () => {}; // Invalid props are deliberately exercised.
    app.mount(root);
    try {
      await settle(ui.nextTick);
      assert.match(textOf(root),/expected exact, unique host-issued identifiers/);
      assert.equal(findAll(root,n=>n.props['data-capsule-id'] !== undefined).length,0);
      props.value = { capsuleIDs: [], available: false };
      await settle(ui.nextTick);
      assert.match(textOf(root),/Capsules unavailable/);
      assert.doesNotMatch(textOf(root),/No capsules yet/);
    } finally { app.unmount(); }
  }
});
