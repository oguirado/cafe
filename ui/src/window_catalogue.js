// @ts-check
/** Presentation-only cache. Entries never authorize pairing: refresh pageOffset
 * and use the controller's current listed window and pairWindow validation. */
const MAX_CLOCK = 18446744073709551615n;
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const unsigned = value => Number.isSafeInteger(value) && value >= 0 && value <= 4294967295;
const clock = value => typeof value === 'string' && /^[1-9][0-9]{0,19}$/.test(value) && BigInt(value) <= MAX_CLOCK;
const token = value => typeof value === 'string' && value.length > 0 && !value.includes('\0') && new TextEncoder().encode(value).length <= 128;
const copyEntry = row => ({ ...row, rect: { ...row.rect } });

/** Collect serial native pages without changing their bounded envelope.
 * reset() explicitly starts a new scan; accept() also permits an offset-zero
 * refresh. Only an identical replay of the latest accepted page is idempotent.
 * A cap stops collection with incomplete, never a successful empty result.
 * @param {number} [limit]
 */
export function createWindowCatalogue(limit = 256) {
  if (!Number.isSafeInteger(limit) || limit < 1 || limit > 256) throw new RangeError('Invalid catalogue limit');
  let entries = [], total = null, nextOffset = null, complete = false, status = 'idle';
  let covered = 0, lastPage = null;
  function clear(nextStatus) {
    entries = []; total = null; nextOffset = null; complete = false;
    covered = 0; lastPage = null; status = nextStatus;
  }
  function reset() { clear('idle'); }
  function getState() { return { entries: entries.map(copyEntry), total, nextOffset, complete, status }; }
  /** @param {any} view */
  function accept(view) {
    try {
      if (!object(view)) throw new Error();
      if (view.availability === 'unavailable' || view.availability === 'starting') {
        clear('unavailable'); return getState();
      }
      if (view.availability !== 'available' || !Array.isArray(view.windows) || view.windows.length > 3 || !object(view.windowPage)) throw new Error();
      const page = view.windowPage;
      if (!unsigned(page.offset) || !unsigned(page.total) || !(page.nextOffset === null || unsigned(page.nextOffset))) throw new Error();
      const end = page.offset + view.windows.length;
      if (page.offset > page.total || end > page.total ||
          (page.nextOffset === null ? end !== page.total :
            view.windows.length === 0 || page.nextOffset !== end || page.nextOffset <= page.offset || page.nextOffset >= page.total)) throw new Error();
      const rows = view.windows.map(row => {
        if (!object(row) || !token(row.token) || !clock(row.generation) || !clock(row.layout_revision) ||
            typeof row.title !== 'string' || typeof row.exe !== 'string' || !object(row.rect) ||
            !unsigned(row.rect.width) || !unsigned(row.rect.height)) throw new Error();
        return { token: row.token, generation: row.generation, layout_revision: row.layout_revision,
          title: row.title, exe: row.exe, rect: { width: row.rect.width, height: row.rect.height }, pageOffset: page.offset };
      });
      if (new Set(rows.map(row => row.token)).size !== rows.length) throw new Error();
      const signature = JSON.stringify({ rows, offset: page.offset, nextOffset: page.nextOffset, total: page.total });
      if (lastPage === signature) return getState();
      if (page.offset === 0) clear('loading');
      else if (total === null || complete || status !== 'loading' || page.offset !== nextOffset || page.offset !== covered || page.total !== total) throw new Error();
      if (entries.some(old => rows.some(row => row.token === old.token))) throw new Error();
      total = page.total;
      const remaining = limit - entries.length;
      entries.push(...rows.slice(0, remaining));
      covered = end;
      lastPage = signature;
      if (rows.length > remaining || (entries.length === limit && end < total)) {
        nextOffset = null; complete = false; status = 'incomplete';
      } else {
        nextOffset = page.nextOffset;
        complete = nextOffset === null && covered === total && entries.length === total;
        status = complete ? 'ready' : 'loading';
      }
    } catch { clear('failed'); }
    return getState();
  }
  return Object.freeze({ reset, accept, getState });
}
