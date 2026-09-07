/**
 * The arm-<branch> alias.
 *
 * Upstream serves arm as a separate tree, so a pacman.conf copied from a
 * Manjaro ARM mirror asks for arm-stable/core/aarch64/. We store
 * stable/core/aarch64/. A wrong rewrite here does not error - it serves
 * the wrong package or 404s a path that exists.
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';

import worker, { resolveArmAlias } from '../src/index.js';

test('an upstream arm path resolves to where we store it', () => {
  assert.equal(
    resolveArmAlias('arm-stable/core/aarch64/acl-2.3.2-1-aarch64.pkg.tar.zst'),
    'stable/core/aarch64/acl-2.3.2-1-aarch64.pkg.tar.zst',
  );
  assert.equal(
    resolveArmAlias('arm-unstable/extra/aarch64/extra.db.tar.gz'),
    'unstable/extra/aarch64/extra.db.tar.gz',
  );
});

test('the arch segment is optional, since the tree implies it', () => {
  // upstream's own listing has no arch in the path above the repository
  assert.equal(resolveArmAlias('arm-testing/core/'), 'testing/core/aarch64/');
  assert.equal(
    resolveArmAlias('arm-testing/core/foo-1-1-aarch64.pkg.tar.zst'),
    'testing/core/aarch64/foo-1-1-aarch64.pkg.tar.zst',
  );
});

test('every branch is aliased, and nothing else is', () => {
  for (const branch of ['unstable', 'testing', 'stable']) {
    assert.equal(
      resolveArmAlias(`arm-${branch}/extra/aarch64/x.pkg.tar.zst`),
      `${branch}/extra/aarch64/x.pkg.tar.zst`,
    );
  }
  // not a branch we serve
  assert.equal(resolveArmAlias('arm-kde-unstable/extra/aarch64/x'), null);
});

test('a canonical path is left alone', () => {
  // the alias must not touch the layout it aliases, or a rewrite loop
  // would move requests out of the tree they already name
  assert.equal(resolveArmAlias('stable/core/aarch64/acl.pkg.tar.zst'), null);
  assert.equal(resolveArmAlias('unstable/extra/x86_64/zeit.pkg.tar.zst'), null);
  assert.equal(resolveArmAlias(''), null);
  assert.equal(resolveArmAlias('state'), null);
});

test('an arm path naming x86_64 is not invented', () => {
  // arm-stable/core/x86_64 does not exist upstream either
  assert.equal(resolveArmAlias('arm-stable/core/x86_64/zeit.pkg.tar.zst'), null);
});

function env(keys) {
  return {
    BUCKET: {
      list: async ({ prefix, delimiter }) => {
        const under = keys.filter((k) => k.startsWith(prefix));
        const dirs = new Set();
        const objects = [];
        for (const k of under) {
          const rest = k.slice(prefix.length);
          const cut = rest.indexOf(delimiter);
          if (cut >= 0) dirs.add(prefix + rest.slice(0, cut + 1));
          else objects.push({ key: k, size: 10 });
        }
        return { objects, delimitedPrefixes: [...dirs] };
      },
      get: async (k) =>
        keys.includes(k)
          ? { body: 'bytes', writeHttpMetadata: () => {}, httpEtag: '"e"' }
          : null,
    },
  };
}

const KEYS = [
  'unstable/extra/aarch64/zeit-1.0.0-1-aarch64.pkg.tar.zst',
  'unstable/extra/aarch64/extra.db.tar.gz',
  'unstable/extra/x86_64/zeit-1.0.0-1-x86_64.pkg.tar.zst',
];

const get = (p) => new Request(`https://packages.manjaro.download/${p}`);

test('fetching through the alias serves the stored object', async () => {
  const res = await worker.fetch(
    get('arm-unstable/extra/aarch64/zeit-1.0.0-1-aarch64.pkg.tar.zst'),
    env(KEYS),
  );
  assert.equal(res.status, 200);
});

test('an alias for something absent still 404s', async () => {
  const res = await worker.fetch(
    get('arm-unstable/extra/aarch64/nope.pkg.tar.zst'),
    env(KEYS),
  );
  assert.equal(res.status, 404);
});

test('a listing through the alias keeps links inside the alias', async () => {
  // a link that jumped to the canonical tree would strand a client that
  // followed it, since its pacman.conf names the arm path
  const res = await worker.fetch(get('arm-unstable/extra/'), env(KEYS));
  const html = await res.text();
  // the arch segment stays hidden, as it is upstream: arm-stable/core/
  // lists packages directly rather than an aarch64/ directory
  assert.match(html, /href="\/arm-unstable\/extra\/zeit-1\.0\.0-1-aarch64/);
  assert.doesNotMatch(
    html,
    /href="\/unstable\//,
    'no link may leave the alias',
  );
  assert.doesNotMatch(html, /x86_64/, 'the arm tree carries only aarch64');
});

test('the canonical listing is unchanged', async () => {
  const res = await worker.fetch(get('unstable/extra/'), env(KEYS));
  const html = await res.text();
  // both arches are directories here, which is the layout we store
  assert.match(html, /href="\/unstable\/extra\/aarch64\/"/);
  assert.match(html, /href="\/unstable\/extra\/x86_64\/"/);
  assert.doesNotMatch(html, /arm-unstable/);
});

test('the favicon is served, and declared in the listing', async () => {
  const res = await worker.fetch(get('favicon.svg'), env(KEYS));
  assert.equal(res.status, 200);
  assert.match(res.headers.get('content-type'), /image\/svg\+xml/);
  const body = await res.text();
  assert.match(body, /^<svg /);
  assert.doesNotMatch(body, /<!--/, 'comments are stripped from the inlined copy');

  // a browser that is not told will only guess /favicon.ico
  const html = await (await worker.fetch(get('unstable/extra/'), env(KEYS))).text();
  assert.match(html, /rel="icon" href="\/favicon\.svg"/);
});

test('an .ico request gets the same svg', async () => {
  const res = await worker.fetch(get('favicon.ico'), env(KEYS));
  assert.equal(res.status, 200);
  assert.match(await res.text(), /^<svg /);
});

test('the favicon route does not shadow a package named the same', async () => {
  // packages live under <branch>/<repo>/<arch>/, so the only favicon.svg
  // the route can claim is the one at the root
  const e = env(['unstable/extra/x86_64/favicon.svg']);
  const res = await worker.fetch(get('unstable/extra/x86_64/favicon.svg'), e);
  assert.equal(res.status, 200);
  assert.equal(await res.text(), 'bytes', 'the bucket object wins at a nested path');
});
