/* Mirror one narrated slide package from the course site, in page context.
   Substitute %DOC% and %VIEW%, run on any keats.kcl.ac.uk page, then poll
   window.__mirror for {phase, files, bytes, errors}. When phase is "done",
   run the download step. Fetches ride the signed-in page session; nothing
   here reads or stores any credential. */
window.__mirror = { phase: 'starting', doc: '%DOC%', files: 0, bytes: 0, errors: [] };
(async () => {
  const S = window.__mirror;
  try {
    let r = await fetch('%VIEW%', { credentials: 'same-origin' });
    let base = null;
    if (r.url.includes('/pluginfile.php/')) {
      base = r.url.replace(/[^/]*$/, '');
    } else {
      /* A display=embed resource stays on view.php; the real file link is in
         the page. Parsed, not regexed: the page also carries OTHER pluginfile
         URLs (found the hard way: the first regex match was a stale link to a
         different resource entirely). */
      const txt = await r.text();
      const pdoc = new DOMParser().parseFromString(txt, 'text/html');
      const a = pdoc.querySelector('.resourceworkaround a, .resourcecontent a');
      const obj = pdoc.querySelector('object#resourceobject, object[data*="pluginfile"], iframe#resourceobject');
      const link = (a && a.getAttribute('href')) || (obj && (obj.getAttribute('data') || obj.getAttribute('src'))) || '';
      if (link.includes('/pluginfile.php/')) base = new URL(link, r.url).href.replace(/[^/]*$/, '');
    }
    if (!base) { S.phase = 'failed'; S.errors.push('no pluginfile base; final URL ' + r.url); return; }
    S.base = base;
    S.phase = 'crawling';

    const seen = new Map();               // path -> Uint8Array
    const queued = new Set(['index.html']);
    const queue = ['index.html'];
    const textExt = /\.(html?|js|css|xml|json|svg)$/i;
    const refRe = /[A-Za-z0-9_][A-Za-z0-9_\-./]*\.(?:js|css|png|jpe?g|gif|svg|woff2?|ttf|otf|eot|mp3|m4a|wav|mp4|webm|ico|xml|json)\b/g;

    const grab = async (path) => {
      let rr;
      try { rr = await fetch(base + path, { credentials: 'same-origin' }); }
      catch (e) { S.errors.push(path + ': ' + e.message); return null; }
      if (!rr.ok) { try { rr.body && rr.body.cancel(); } catch (e) {} return null; }
      return new Uint8Array(await rr.arrayBuffer());
    };

    while (queue.length) {
      const path = queue.shift();
      const buf = await grab(path);
      if (!buf) {
        /* The entry point failing is the whole mirror failing; an asset
           failing is a probe miss. */
        if (path === 'index.html') { S.phase = 'failed'; S.errors.push('index.html did not fetch from ' + base); return; }
        continue;
      }
      seen.set(path, buf);
      S.files = seen.size; S.bytes += buf.length;
      if (textExt.test(path)) {
        const text = new TextDecoder('utf-8', { fatal: false }).decode(buf);
        const dir = path.includes('/') ? path.replace(/[^/]*$/, '') : '';
        for (const m of text.matchAll(refRe)) {
          const ref = m[0].replace(/^\.\//, '');
          if (ref.startsWith('/') || ref.includes('//') || ref.includes('..')) continue;
          for (const cand of [ref, dir + ref]) {
            if (!queued.has(cand)) { queued.add(cand); queue.push(cand); }
          }
        }
      }
    }

    /* Backstop for names built at runtime rather than written in a file:
       every numbered family the format uses, probed upward until three
       consecutive misses. */
    S.phase = 'probing';
    const fams = ['data/sound%.mp3', 'data/img%.png', 'data/img%.jpg',
                  'data/slide%.js', 'data/slide%.css', 'data/fnt%.woff',
                  'data/fnt%.woff2', 'data/video%.mp4'];
    for (const fam of fams) {
      let misses = 0;
      for (let n = 0; n < 400 && misses < 3; n++) {
        const path = fam.replace('%', n);
        if (seen.has(path)) { misses = 0; continue; }
        const buf = await grab(path);
        if (buf) { seen.set(path, buf); S.files = seen.size; S.bytes += buf.length; misses = 0; }
        else misses++;
      }
    }

    /* Tar it: ustar, one entry per file, paths as fetched. */
    S.phase = 'packing';
    const enc = new TextEncoder();
    const blocks = [];
    for (const [name, bytes] of seen) {
      const h = new Uint8Array(512);
      const put = (s, off, len) => { const b = enc.encode(s).slice(0, len); h.set(b, off); };
      put(name, 0, 100);
      put('0000644\0', 100, 8); put('0000000\0', 108, 8); put('0000000\0', 116, 8);
      put(bytes.length.toString(8).padStart(11, '0') + '\0', 124, 12);
      put('00000000000\0', 136, 12);
      h.fill(32, 148, 156);
      h[156] = 48;
      put('ustar\0', 257, 6); put('00', 263, 2);
      let sum = 0; for (let i = 0; i < 512; i++) sum += h[i];
      put(sum.toString(8).padStart(6, '0') + '\0 ', 148, 8);
      blocks.push(h, bytes);
      const pad = (512 - (bytes.length % 512)) % 512;
      if (pad) blocks.push(new Uint8Array(pad));
    }
    blocks.push(new Uint8Array(1024));
    S.blob = new Blob(blocks, { type: 'application/x-tar' });
    S.tarBytes = S.blob.size;
    S.phase = 'done';
  } catch (e) {
    S.phase = 'failed';
    S.errors.push(String(e && e.message || e));
  }
})();
'started'
