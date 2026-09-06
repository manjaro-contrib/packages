/**
 * Serves the pacman repository from R2 and renders directory listings.
 *
 * R2 has no notion of directories: a listing is a delimited list() over the
 * key prefix. Object reads stream straight through so pacman still gets
 * plain bytes with working range requests.
 */

const REPO_NAME = 'manjaro-contrib';

const escapeHtml = (s) => s.replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);

function humanSize(bytes) {
  const units = ['B', 'KiB', 'MiB', 'GiB'];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${unit === 0 ? value : value.toFixed(1)} ${units[unit]}`;
}

export function renderListing(prefix, dirs, files) {
  const parent = prefix.replace(/[^/]+\/$/, '');
  const rows = [
    ...(prefix ? [`<a class="row" href="/${escapeHtml(parent)}">../</a>`] : []),
    ...dirs.map(
      (d) =>
        `<a class="row" href="/${escapeHtml(d)}">${escapeHtml(
          d.slice(prefix.length),
        )}</a>`,
    ),
    ...files.map(
      (f) =>
        `<a class="row" href="/${escapeHtml(f.key)}">${escapeHtml(
          f.key.slice(prefix.length),
        )}<i>${humanSize(f.size)}</i></a>`,
    ),
  ].join('\n');

  return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>/${escapeHtml(prefix)} — ${REPO_NAME}</title>
<style>
:root { color-scheme: light dark; }
body { font: 14px/1.6 ui-monospace, SFMono-Regular, Menlo, monospace;
       max-width: 60rem; margin: 2rem auto; padding: 0 1rem; }
h1 { font-size: 1.1rem; font-weight: 600; }
.row { display: flex; justify-content: space-between; gap: 1rem;
       padding: .15rem 0; text-decoration: none; }
.row:hover { text-decoration: underline; }
i { opacity: .6; font-style: normal; }
footer { margin-top: 2rem; opacity: .7; }
</style>
</head>
<body>
<h1>/${escapeHtml(prefix)}</h1>
${rows || '<p>empty</p>'}
<footer>ISOs are at <a href="https://manjaro.download">manjaro.download</a> &middot; built by <a href="https://github.com/manjaro-contrib/packages">manjaro-contrib/packages</a></footer>
</body>
</html>
`;
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const key = decodeURIComponent(url.pathname.slice(1));

    if (request.method !== 'GET' && request.method !== 'HEAD') {
      return new Response('method not allowed', { status: 405 });
    }

    if (key === '' || key.endsWith('/')) {
      const listed = await env.BUCKET.list({ prefix: key, delimiter: '/' });
      const files = listed.objects.filter((o) => o.key !== key);
      if (!files.length && !listed.delimitedPrefixes.length) {
        return new Response('not found', { status: 404 });
      }
      return new Response(
        renderListing(key, listed.delimitedPrefixes, files),
        { headers: { 'content-type': 'text/html; charset=utf-8' } },
      );
    }

    const object = await env.BUCKET.get(key, {
      range: request.headers,
      onlyIf: request.headers,
    });
    if (!object) return new Response('not found', { status: 404 });

    const headers = new Headers();
    object.writeHttpMetadata(headers);
    headers.set('etag', object.httpEtag);
    // packages are immutable once published; the database changes in place
    headers.set(
      'cache-control',
      key.endsWith('.pkg.tar.zst')
        ? 'public, max-age=31536000, immutable'
        : 'no-cache',
    );

    const status = object.body ? (request.headers.get('range') ? 206 : 200) : 304;
    return new Response(request.method === 'HEAD' ? null : object.body, {
      status,
      headers,
    });
  },
};
