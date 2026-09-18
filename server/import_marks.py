#!/usr/bin/env python3
"""Import highlights exported from the artifact version into the web reader.

The export carries anchors (block index, start, end) computed by the artifact's
own highlight engine. The web notes are those same files with the local layer
installed, so the numbering *should* agree, but two things could have moved it:
apply_layer.py added td and th to the block selector, and link_parts.py added
section ids. Nothing here trusts the exported index. Every mark is verified
against the text actually on the page, and re-anchored by search when it does
not land.

Reads the export, writes notes/<DOC>-marks.json in the server's format.
Refuses to write anything if any mark cannot be placed.
"""

import html as html_mod
import json
import pathlib
import re
import sys
import time
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent   # the repo, wherever it lives
def _module_dir(repo):
    """The lessons folder, wherever it is. See split_lessons.default_module_dir:
    the folder was `notes/` until 2026-08-16 and is `courses/<MODULE>/` now."""
    import sys
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    from split_lessons import default_module_dir
    return default_module_dir(repo)


NOTES = _module_dir(ROOT)

BLOCK_TAGS = {"p", "li", "h1", "h2", "h3", "h4", "dd", "dt", "figcaption",
              "blockquote", "td", "th"}

# The artifacts were published before apply_layer.py added td and th to the
# selector, so every table in a note shifts the numbering between the two.
# The export was made against the older scale and has to be read on it.
LEGACY_BLOCK_TAGS = BLOCK_TAGS - {"td", "th"}
SKIP_CLASSES = ("hl-panel", "sv-add", "sv-pop", "sv-edit")
VOID = {"br", "img", "hr", "input", "meta", "link", "source", "col", "area",
        "base", "embed", "param", "track", "wbr"}


class Blocks(HTMLParser):
    """The page's rule, in Python.

        blocks = querySelectorAll('.wrap p, .wrap li, ...')
                 .filter(not inside .hl-panel or .sv-add)
                 .filter(textContent.trim() non-empty)
                 .filter(no ancestor also matches)

    So: outermost block-tag elements inside .wrap, skipping the layer's own
    furniture. Same rule the server's BlockFinder implements, kept separate
    here on purpose: a third implementation that agreed with neither would be
    worth knowing about before it wrote anything.
    """

    def __init__(self, tags=BLOCK_TAGS):
        super().__init__(convert_charrefs=False)
        self.tags = tags
        self.stack = []           # (tag, is_wrap, is_skip)
        self.blocks = []          # {"tag", "inner": [str parts]}
        self.open = None
        self.depth = 0
        self.in_wrap = 0
        self.in_skip = 0

    def _emit(self, s):
        if self.open is not None:
            self.blocks[self.open]["inner"].append(s)

    def handle_starttag(self, tag, attrs):
        cls = ""
        for k, v in attrs:
            if k == "class":
                cls = v or ""
        names = cls.split()
        is_wrap = "wrap" in names
        is_skip = any(c in names for c in SKIP_CLASSES)

        if tag in VOID:
            self._emit("")
            return

        candidate = (tag in self.tags and self.in_wrap > 0
                     and self.in_skip == 0 and self.open is None)
        if candidate:
            self.blocks.append({"tag": tag, "inner": [], "at": self.getpos()})
            self.open = len(self.blocks) - 1
            self.depth = 0
        elif self.open is not None and tag == self.blocks[self.open]["tag"]:
            self.depth += 1

        self.stack.append((tag, is_wrap, is_skip))
        if is_wrap:
            self.in_wrap += 1
        if is_skip:
            self.in_skip += 1

    def handle_startendtag(self, tag, attrs):
        self._emit("")

    def handle_endtag(self, tag):
        if self.open is not None and tag == self.blocks[self.open]["tag"]:
            if self.depth == 0:
                self.open = None
            else:
                self.depth -= 1
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                _, was_wrap, was_skip = self.stack[i]
                if was_wrap:
                    self.in_wrap -= 1
                if was_skip:
                    self.in_skip -= 1
                del self.stack[i:]
                break

    # textContent keeps whitespace exactly as written, and tags contribute
    # nothing at all, not even a space. The exported anchors carry newlines and
    # runs of indent spaces, which is the proof of it.
    def handle_data(self, data):
        self._emit(data)

    def handle_entityref(self, name):
        self._emit(html_mod.unescape("&" + name + ";"))

    def handle_charref(self, name):
        self._emit(html_mod.unescape("&#" + name + ";"))

    def handle_comment(self, data):
        pass


def block_list(source, tags=BLOCK_TAGS):
    """[(source_position, textContent)] for the blocks the page would number."""
    p = Blocks(tags)
    p.feed(source)
    out = []
    for b in p.blocks:
        t = "".join(b["inner"])
        if not t.strip():
            continue
        out.append((b["at"], t))
    return out


def block_texts(source, tags=BLOCK_TAGS):
    return [t for _, t in block_list(source, tags)]


def squash(s):
    return re.sub(r"\s+", " ", s).strip()


def parse_export(path):
    text = Path(path).read_text(encoding="utf-8")
    m = re.search(r"```json\s*\n(.*?)\n```", text, re.S)
    if not m:
        raise SystemExit(f"no highlight-data block in {path}")
    return json.loads(m.group(1))


def note_file(doc_id):
    hits = sorted(NOTES.glob(f"{doc_id}-*.html"))
    hits = [h for h in hits if not h.name.endswith(".bak")]
    if len(hits) != 1:
        raise SystemExit(f"{doc_id}: expected one note file, found {hits}")
    return hits[0]


def legacy_map(source):
    """Map an artifact-era block index onto the current one.

    Both numberings are the same walk of the same document with a different tag
    set, so a block is identified by where its start tag sits in the file, not
    by its position in either list. A legacy block with no current twin is one
    the table cells now swallow: a <p> inside a <td> used to be the outermost
    match and is not any more. Those fall back to the cell that now contains
    them, found by source position.
    """
    now = block_list(source, BLOCK_TAGS)
    old = block_list(source, LEGACY_BLOCK_TAGS)
    by_pos = {pos: i for i, (pos, _) in enumerate(now)}
    out = []
    for pos, _ in old:
        if pos in by_pos:
            out.append(by_pos[pos])
        else:
            enclosing = [i for i, (p2, _) in enumerate(now) if p2 <= pos]
            out.append(enclosing[-1] if enclosing else None)
    return out, [t for _, t in old]


def place(item, texts, lmap, ltexts):
    """Return (b, s, e, how) or None.

    Read the anchor on the scale it was written on first. Only if that fails
    does this fall back to searching, the same self-healing the page does.
    """
    t = item["t"]
    b, s, e = item.get("b"), item.get("s"), item.get("e")

    # 1. The exported anchor, read on the artifact's numbering.
    if isinstance(b, int) and 0 <= b < len(ltexts) and ltexts[b][s:e] == t:
        nb = lmap[b]
        if nb is not None:
            # The offsets are inside the block, so they survive the renumbering
            # untouched whenever the block's own text is unchanged. Keep them:
            # searching afresh picks the first occurrence of a repeated word and
            # quietly moves the mark, which is how "polymorphism" ended up on
            # the wrong half of its own sentence.
            if texts[nb][s:e] == t:
                return nb, s, e, ("as exported" if nb == b else f"artifact b{b}")
            at = texts[nb].find(t)
            if at >= 0:
                return nb, at, at + len(t), f"artifact b{b}, offset moved"

    # 2. The exported anchor read on the current numbering, in case a note was
    #    written after the selector changed.
    if isinstance(b, int) and 0 <= b < len(texts) and texts[b][s:e] == t:
        return b, s, e, "as exported"

    order = sorted(range(len(texts)),
                   key=lambda i: (abs(i - b) if isinstance(b, int) else i, i))

    for i in order:
        at = texts[i].find(t)
        if at >= 0:
            return i, at, at + len(t), ("re-anchored" if i != b else "shifted")

    # Whitespace inside a mark can differ if the paragraph was re-indented.
    want = squash(t)
    for i in order:
        flat = squash(texts[i])
        at = flat.find(want)
        if at < 0:
            continue
        # Walk the raw text counting non-space characters to recover offsets.
        raw = texts[i]
        seen = 0
        start = None
        target_start = len(squash(flat[:at]))  # non-normalised prefix length
        nonspace = 0
        want_nonspace_start = len(re.sub(r"\s+", "", flat[:at]))
        want_nonspace_len = len(re.sub(r"\s+", "", want))
        for j, ch in enumerate(raw):
            if not ch.isspace():
                if nonspace == want_nonspace_start and start is None:
                    start = j
                nonspace += 1
                if start is not None and nonspace - want_nonspace_start == want_nonspace_len:
                    return i, start, j + 1, "re-anchored, whitespace differs"
        _ = (seen, target_start)
    return None


def main(argv):
    if len(argv) < 2:
        raise SystemExit("usage: import_marks.py <export.md> [export.md ...] [--write]")
    write = "--write" in argv
    paths = [a for a in argv[1:] if a != "--write"]

    plans = []
    ok = True
    for p in paths:
        data = parse_export(p)
        doc = data["doc"]
        f = note_file(doc)
        source = f.read_text(encoding="utf-8")
        texts = block_texts(source)
        lmap, ltexts = legacy_map(source)
        print(f"\n{doc}  ({f.name})  {len(texts)} blocks now, "
              f"{len(ltexts)} in the artifact, "
              f"{len(data['items'])} highlights, {len(data.get('notes', []))} notes")

        items = []
        for it in data["items"]:
            got = place(it, texts, lmap, ltexts)
            label = squash(it["t"])[:58]
            if not got:
                ok = False
                print(f"  ✗ COULD NOT PLACE  b{it.get('b')}  {label!r}")
                continue
            b, s, e, how = got
            flag = "  " if how == "as exported" else "→ "
            print(f"  {flag}b{it.get('b')}→b{b} [{s}:{e}]  {how:<28} {label!r}")
            items.append({"id": len(items), "b": b, "s": s, "e": e,
                          "t": it["t"], "c": it.get("c", "k"),
                          "n": it.get("n", ""), "orphan": False})

        notes = []
        for i, n in enumerate(data.get("notes", [])):
            notes.append({"id": i, "text": n.get("text", ""),
                          "ts": n.get("ts") or int(time.time() * 1000)})

        plans.append((doc, items, notes))

    if not ok:
        raise SystemExit("\nRefusing to write: some marks could not be placed.")

    if not write:
        print("\nDry run. Re-run with --write to save.")
        return

    for doc, items, notes in plans:
        out = NOTES / f"{doc}-marks.json"
        if out.exists():
            stamp = time.strftime("%Y%m%d-%H%M%S")
            bak = out.with_name(out.name + f".{stamp}.bak")
            bak.write_bytes(out.read_bytes())
            print(f"backed up {bak.name}")
        doc_json = {
            "doc": doc,
            "updated": int(time.time() * 1000),
            "saved": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
            "items": items,
            "notes": notes,
        }
        out.write_text(json.dumps(doc_json, indent=2, ensure_ascii=False) + "\n",
                       encoding="utf-8")
        print(f"wrote {out.name}: {len(items)} highlights, {len(notes)} notes")


if __name__ == "__main__":
    main(sys.argv)
