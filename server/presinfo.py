"""Read the timing blob out of an iSpring package, and say what is in it.

🔴 READ-ONLY INSPECTION. Nothing here writes to a package, and the server never
imports it (pinned by `test_presinfo.py`). It exists because two separate pieces of
work need facts that are locked inside `index.html`:

- **the timeline rebuild**: every timing a lecture is driven by is an explicit number
  in this blob, so a faster copy is made by scaling them (probe, 2026-09-02);
- **the captions aligner**: the blob says exactly which audio clip belongs to which
  slide, which an earlier finding of mine wrongly said was nowhere on disk.

What the blob is: `index.html` carries `var presInfo = "eNrt..."`, base64 of a zlib
stream, decompressing to JSON with a top-level `s` array, one object per slide. It is
handed to `PresentationPlayer.start()` once, at boot, and never re-read.

Reproduce the corpus facts with `python3 server/presinfo.py --survey`, after
`--self-test` proves the reader still fires. Run the self-test FIRST every time: a
decoder that returns nothing looks exactly like a package that says nothing.

🟢 **THE BLOB FORMAT AND THE SCALER MOVED TO `timeline.py` on 2026-09-03**, and
are re-exported here so every caller and every test keeps working. **The server
serves a rebuilt package now, and it may not import this file** (pinned:
`TheSERVERMUSTNEVERIMPORTTHIS`), because this one globs the course tree, shells out
to `ffprobe` and parses arguments. 🔴 **The split is by AUDIENCE, not by topic**:
`timeline.py` is what a REQUEST needs, this file is what a PERSON needs. `TIME_FIELDS`
still has exactly one home, which is the property that mattered.
"""

import argparse
import glob
import json
import os
import subprocess
import sys

# 🔴 Re-exported, deliberately: every caller and every test names these on this
# module, and the mutation-proved tests written against `presinfo.scale` now
# cover the code the SERVER runs. One home for `TIME_FIELDS`, two audiences.
from timeline import (  # noqa: F401
    BLOB,
    NOT_TIME_FIELDS,
    TIME_FIELDS,
    decode,
    encode,
    positive_rate,
    rebuild,
    scale,
)

def read_blob(index_html):
    """Return (whole file text, the regex match over the base64 blob)."""
    with open(index_html, encoding="utf-8", errors="replace") as fh:
        src = fh.read()
    m = BLOB.search(src)
    if not m:
        raise ValueError("no presInfo blob in " + index_html)
    return src, m


def load(index_html):
    return decode(read_blob(index_html)[1].group(1))


def sound_for_slide(pres):
    """[(slide index, audio file name)] for every narrated slide, in slide order.

    🔴 THE OFF-BY-ONE IS REAL AND IT IS UNIVERSAL. The blob calls the first clip
    `sound0` and the file on disk is `sound1.mp3`. Proved against the media rather
    than assumed: across all 38 packages, 514 of 514 clip lengths match the +1 file
    and 0 of 514 match the same-numbered one. A join built on the name as written
    would be off by one slide everywhere and would still look plausible.
    """
    out = []
    for n, slide in enumerate(pres.get("s", [])):
        for snd in slide.get("S", []):
            k = int(str(snd["i"]).replace("sound", ""))
            out.append((n, "sound%d.mp3" % (k + 1), snd.get("d")))
    return out


def times_in(pres):
    """Every timing value in the blob, as (path, value), for auditing a package."""
    found = []

    def walk(node, path):
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, path + "." + k)
        elif isinstance(node, list):
            for v in node:
                walk(v, path + "[]")
        elif isinstance(node, (int, float)) and not isinstance(node, bool):
            found.append((path, node))

    walk(pres.get("s", []), "s")
    names = {p for p, _ in TIME_FIELDS}
    return [(p, v) for p, v in found if p in names]


TRAP = {
    "s": [
        # a slide whose clip is named sound0 and whose file is sound1.mp3
        {"e": [{"p": 4.0, "a": 0}], "S": [{"i": "sound0", "d": 4.0}],
         "q": {"d": 0.7}, "i": {"m": {"s": [{"d": 4.0, "a": []}]}}},
        # a silent slide: no S at all, and it is not an error
        {"e": [{"p": 2.0, "a": 0}], "q": {"d": 0.7}, "i": {"m": {"s": [{"d": 2.0, "a": []}]}}},
        # a slide carrying TWO clips, which the corpus really contains
        {"e": [{"p": 9.0, "a": 0}],
         "S": [{"i": "sound1", "d": 5.0}, {"i": "sound2", "d": 4.0}],
         "q": {"d": 0.7}, "i": {"m": {"s": [{"d": 9.0, "a": []}]}}},
    ]
}


def self_test():
    """Fail loudly if the reader has stopped reading. A broken decoder returns an
    empty survey, which is indistinguishable from a corpus that says nothing."""
    bad = []
    round_tripped = decode(encode(TRAP))
    if round_tripped != TRAP:
        bad.append("encode/decode round trip is not lossless")
    join = sound_for_slide(TRAP)
    if [(n, f) for n, f, _ in join] != [(0, "sound1.mp3"), (2, "sound2.mp3"), (2, "sound3.mp3")]:
        bad.append("sound_for_slide got the +1 join wrong: %r" % (join,))
    ts = dict(times_in(TRAP))
    if "s[].e[].p" not in ts or "s[].q.d" not in ts:
        bad.append("times_in missed a field it names: %r" % (sorted(ts),))
    if "s[].S[].d" in ts:
        bad.append("times_in returned the audio length, which is NOT a timeline value")
    for line in bad:
        print("FAIL: " + line)
    print("self-test: %s" % ("FAILED" if bad else "all checks pass"))
    return 1 if bad else 0


def survey(root="courses"):
    pkgs = sorted(glob.glob(os.path.join(root, "*", "packages", "*", "index.html")))
    print("packages: %d" % len(pkgs))
    in_pkgs, values, nonzero = {}, {}, {}
    slides = clips = silent = 0
    for p in pkgs:
        pres = load(p)
        slides += len(pres.get("s", []))
        silent += sum(1 for slide in pres.get("s", []) if not slide.get("S"))
        clips += len(sound_for_slide(pres))
        here = set()
        for path, v in times_in(pres):
            here.add(path)
            values[path] = values.get(path, 0) + 1
            if v:
                nonzero[path] = nonzero.get(path, 0) + 1
        for path in here:
            in_pkgs[path] = in_pkgs.get(path, 0) + 1
    print("slides: %d   narrated clips: %d   silent slides: %d" % (slides, clips, silent))
    print()
    print("%-28s %5s %7s %8s  %s" % ("timing field", "pkgs", "values", "non-zero", "what it is"))
    for path, why in TIME_FIELDS:
        print("  %-26s %5d %7d %8d  %s"
              % (path, in_pkgs.get(path, 0), values.get(path, 0), nonzero.get(path, 0), why))
    print()
    print("A field present everywhere but never non-zero still has to be carried by a")
    print("rebuild: it is zero in THIS corpus, not zero by construction.")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--survey", action="store_true")
    ap.add_argument("--sounds", metavar="INDEX_HTML")
    a = ap.parse_args(argv)
    if a.self_test:
        return self_test()
    if a.survey:
        return survey()
    if a.sounds:
        for n, f, d in sound_for_slide(load(a.sounds)):
            print("slide %-3d %-14s %.3fs" % (n, f, d if d is not None else -1))
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
