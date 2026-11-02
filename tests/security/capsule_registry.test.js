import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { createRegistry, MAX_SLOTS, ERR_INVALID_ID, ERR_OVERFLOW, ERR_INVALID_OPTIONS } from '../../ui/src/capsule/registry.js';

describe('capsule registry', () => {
  it('assigns stable aliases and dedupes duplicates', () => {
    const r = createRegistry();
    assert.deepEqual(r.assign(['a', 'b', 'a']), ['a', 'b']);
    assert.equal(r.displayFor('a'), 'slot-1');
    assert.equal(r.displayFor('b'), 'slot-2');
  });
  it('reorder and delete-readd retain aliases', () => {
    const r = createRegistry();
    r.assign(['a', 'b']);
    r.assign(['b', 'a']);
    assert.equal(r.aliasFor('a'), 1);
    r.remove('a');
    assert.deepEqual(r.activeIds(), ['b']);
    r.assign(['b', 'a']);
    assert.equal(r.aliasFor('a'), 1);
  });
  it('overflow and invalid ids reject atomically', () => {
    const r = createRegistry({ maxSlots: 2 });
    r.assign(['a', 'b']);
    assert.throws(() => r.assign(['c']), { code: ERR_OVERFLOW });
    assert.deepEqual(r.activeIds(), ['a', 'b']);
    assert.throws(() => r.assign(['ok', '  ']), { code: ERR_INVALID_ID });
    assert.deepEqual(r.activeIds(), ['a', 'b']);
  });
  it('NEGATIVE scalar assign rejects before mutation', () => {
    const r = createRegistry();
    r.assign(['a']);
    assert.throws(() => r.assign('capsule-valid'), { code: ERR_INVALID_ID });
    for (const bad of [null, {}, 42, undefined]) {
      assert.throws(() => r.assign(bad), { code: ERR_INVALID_ID });
    }
    assert.deepEqual(r.activeIds(), ['a']);
    assert.equal(r.aliasFor('a'), 1);
  });
  it('NEGATIVE negative maxSlots rejects instead of defaulting', () => {
    assert.throws(() => createRegistry({ maxSlots: -1 }), { code: ERR_INVALID_OPTIONS });
  });
  it('NEGATIVE invalid/NaN/fractional/overflow/corrupt options reject', () => {
    for (const bad of [0, NaN, 1.5, Infinity, Number.MAX_SAFE_INTEGER + 1, '2', null]) {
      assert.throws(() => createRegistry({ maxSlots: bad }), { code: ERR_INVALID_OPTIONS });
    }
    for (const badOpts of [null, [], 'x', 5]) {
      assert.throws(() => createRegistry(badOpts), { code: ERR_INVALID_OPTIONS });
    }
    assert.equal(MAX_SLOTS, 1000);
  });
});
