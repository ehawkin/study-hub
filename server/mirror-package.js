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

    /* Every reference seen anywhere, and the candidate paths it could mean.
       Kept so the mirror can say at the end which references it never
       satisfied, instead of reporting success while incomplete. */
    const refCands = new Map();

    /* Store a file AND follow what it references. 🔴 This is one function
       because the bug it fixes was the two halves being separate: the probe
       stored files without ever parsing them, so a slide file arrived and the
       images IT references were never queued. Anything that adds a file to
       `seen` goes through here. */
    const absorb = (path, buf) => {
      seen.set(path, buf);
      S.files = seen.size; S.bytes += buf.length;
      if (!textExt.test(path)) return;
      const text = new TextDecoder('utf-8', { fatal: false }).decode(buf);
      const dir = path.includes('/') ? path.replace(/[^/]*$/, '') : '';
      for (const m of text.matchAll(refRe)) {
        const ref = m[0].replace(/^\.\//, '');
        if (ref.startsWith('/') || ref.includes('//') || ref.includes('..')) continue;
        const cands = [ref, dir + ref];
        if (!refCands.has(ref)) refCands.set(ref, cands);
        for (const cand of cands) {
          if (!queued.has(cand)) { queued.add(cand); queue.push(cand); }
        }
      }
    };

    const drain = async () => {
      while (queue.length) {
        const path = queue.shift();
        if (seen.has(path)) continue;
        const buf = await grab(path);
        if (!buf) {
          /* The entry point failing is the whole mirror failing; an asset
             failing is a probe miss, and is judged at the end instead. */
          if (path === 'index.html') { S.phase = 'failed'; S.errors.push('index.html did not fetch from ' + base); return false; }
          continue;
        }
        absorb(path, buf);
      }
      return true;
    };

    if (!(await drain())) return;

    /* Backstop for names built at runtime rather than written in a file:
       every numbered family the format uses, probed upward until three
       consecutive misses. */
    const fams = ['data/sound%.mp3', 'data/img%.png', 'data/img%.jpg',
                  'data/slide%.js', 'data/slide%.css', 'data/fnt%.woff',
                  'data/fnt%.woff2', 'data/video%.mp4'];
    const probe = async () => {
      for (const fam of fams) {
        let misses = 0;
        for (let n = 0; n < 400 && misses < 3; n++) {
          const path = fam.replace('%', n);
          if (seen.has(path)) { misses = 0; continue; }
          const buf = await grab(path);
          if (buf) { absorb(path, buf); misses = 0; }
          else misses++;
        }
      }
    };

    /* 🔴 Crawl and probe ALTERNATE until neither adds anything, and that is the
       whole fix. `index.html` references no slide files, so every
       `data/slideN.js` arrives through the probe; the `<img src="data/imgN.jpg">`
       inside them is only discoverable once that slide has been parsed. Before
       this, jpgs survived only if the numeric probe happened to reach them, and
       it never did: images are numbered in ONE sequence shared across formats,
       so a package whose first jpg sits at index 7 misses img0/1/2.jpg, stops at
       three consecutive misses, and silently ships without a single jpg. Eight
       mirrored packages across two courses were damaged exactly that way.

       Widening the probe would have treated this instance and left the
       mechanism: anything referenced from a slide whose name is outside the
       known families stays invisible. */
    S.phase = 'probing';
    for (let pass = 0; pass < 12; pass++) {
      const before = seen.size;
      await probe();
      if (!(await drain())) return;
      if (seen.size === before) break;
    }

    /* 🔴 Say what was referenced and never found. Without this a package
       mirrors "successfully" while incomplete, which is exactly how the jpg
       loss survived for weeks: a broken <img> logs nothing, the player does not
       complain, and the mirror counted only what it fetched.

       A reference with no folder in it is assembled at runtime from a base this
       cannot know (`wj(this.Kh, "btn_play_big.svg")` in the minified player is
       written in every package and present in none), so those are not reported
       or the report would be noise everybody learns to skip. */
    S.missing = [];
    for (const [ref, cands] of refCands) {
      if (!ref.includes('/')) continue;
      if (cands.some((c) => seen.has(c))) continue;
      S.missing.push(ref);
    }
    if (S.missing.length) {
      S.errors.push('referenced but never fetched: ' + S.missing.join(', '));
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
