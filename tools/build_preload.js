// @ts-check
'use strict';
const path = require('node:path');
const fs = require('node:fs');
const { createHash } = require('node:crypto');

/** @param {string} [root] */
async function buildPreload(root = path.resolve(__dirname, '..')) {
  if (Number(process.versions.node.split('.')[0]) !== 24) throw new Error('NODE_RUNTIME_REJECTED');
  const { build } = await import('vite');
  const outDir = path.join(root, 'dist', 'preload');
  await build({
    root, configFile: false, publicDir: false, logLevel: 'silent',
    build: {
      outDir, emptyOutDir: false, minify: false, sourcemap: false,
      lib: { entry: path.join(root, 'src', 'preload', 'index.js'), formats: ['cjs'], fileName: () => 'index.cjs' },
      rolldownOptions: { external: ['electron'] },
    },
  });
  const output = path.join(outDir, 'index.cjs');
  const bytes = fs.readFileSync(output);
  return { schema: 'cafe.preload-build.v1', status: 'pass',
    output: 'dist/preload/index.cjs', sha256: createHash('sha256').update(bytes).digest('hex') };
}

exports.buildPreload = buildPreload;
if (require.main === module) buildPreload().then((report) => {
  process.stdout.write(JSON.stringify(report) + '\n');
}).catch(() => {
  process.stdout.write(JSON.stringify({ schema: 'cafe.preload-build.v1', status: 'fail', code: 'PRELOAD_BUILD_REJECTED' }) + '\n');
  process.exitCode = 1;
});
