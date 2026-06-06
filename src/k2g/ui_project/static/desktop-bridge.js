/*
 * Desktop (no-HTTP) fetch bridge.
 *
 * The desktop app (k2g.desktop) loads this page from disk via file:// and runs
 * the FastAPI backend in-process behind pywebview's js_api — there is no HTTP
 * server. This shim intercepts the app's relative-path fetch() calls and routes
 * them through window.pywebview.api.request(...) into that in-process app.
 *
 * It is a NO-OP in the browser (web Manager): the gate below only fires under
 * file://, so the hosted/loopback path is byte-for-byte unchanged. Loaded before
 * app.js (first <script> in <head>) so the override is in place before any fetch.
 */
(function () {
  if (location.protocol !== 'file:') return;            // browser/web path → untouched

  // pywebview injects window.pywebview.api asynchronously; queue until ready.
  const ready = new Promise(function (resolve) {
    if (window.pywebview && window.pywebview.api) return resolve();
    window.addEventListener('pywebviewready', resolve, { once: true });
  });

  const orig = window.fetch.bind(window);

  function bytesToB64(bytes) {
    let s = '';
    for (let i = 0; i < bytes.length; i++) s += String.fromCharCode(bytes[i]);
    return btoa(s);
  }

  async function bodyToB64(body) {
    if (body == null) return null;
    let bytes;
    if (typeof body === 'string') {
      bytes = new TextEncoder().encode(body);
    } else if (body instanceof ArrayBuffer) {
      bytes = new Uint8Array(body);
    } else if (ArrayBuffer.isView(body)) {
      bytes = new Uint8Array(body.buffer, body.byteOffset, body.byteLength);
    } else {
      // Blob / URLSearchParams / FormData → normalize via Response.
      bytes = new Uint8Array(await new Response(body).arrayBuffer());
    }
    return bytesToB64(bytes);
  }

  // Only backend-served paths go through the bridge; everything else (CDN
  // cytoscape over https, etc.) hits the real network unchanged.
  const BACKEND = /^\/(api|locales|health|warmup)\b/;

  window.fetch = async function (input, init) {
    init = init || {};
    const url = (typeof input === 'string') ? input : input.url;
    if (!BACKEND.test(url)) return orig(input, init);

    await ready;
    const method = (init.method
      || (typeof input !== 'string' && input && input.method)
      || 'GET').toUpperCase();
    const headers = {};
    new Headers(init.headers || (typeof input !== 'string' && input && input.headers) || {})
      .forEach(function (v, k) { headers[k] = v; });

    const r = await window.pywebview.api.request(method, url, headers, await bodyToB64(init.body));
    const bytes = Uint8Array.from(atob(r.body_b64), function (c) { return c.charCodeAt(0); });
    return new Response(bytes, { status: r.status, headers: r.headers });
  };
})();
