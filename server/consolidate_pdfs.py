#!/usr/bin/env python3
"""Consolidated PDFs: one per week and one for the whole course, twice over.

EH's design, 2026-08-23: an option a person can select during setup, and later
like everything, that generates consolidated weekly transcripts, weekly slide
decks, one whole-course transcript PDF and one whole-course deck PDF. The use
is reading and printing: a week's slides as one document instead of nine tabs.

**Run this AFTER pdf_fix**, never before. The merge copies pages as they are,
so whatever orientation and OCR the parts carry is what the consolidated PDF
inherits. The wizard's prompt says this in order; if you are here another way,
fix the folder first.

Input is a folder of per-part PDFs named the way the download-keats skill
actually files them, which is `<DOC> - <Kind> (<original name>).pdf`, as in
`W2-T3-P1 - Slides (7XYZ_W2_T3_P1_Accessible_Slides).pdf`. The week is read from
the `W<n>` in the DOC. ⚠️ **This paragraph described a convention nobody uses
(`<DOC>-slides.pdf`) until 2026-09-08. The CODE was always right**: it strips the
trailing parenthetical and classifies on the standard part, so a transcript whose
original name mentions slides is still a transcript. Only the description was
wrong, which is the kind of wrong that sends somebody renaming files that were
already correct. Output goes to `courses/<CODE>/consolidated/`. The outputs
are DERIVED: re-running regenerates them, and nothing else may edit them, so
they are overwritten without ceremony (the parts are the originals).

Merging is qpdf (`--empty --pages ... --`), which copies pages losslessly.
Every output is verified: its page count must equal the sum of its parts'
counts, counted by pdfinfo, or the run reports the failure and keeps going.
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import study_server as S                                        # noqa: E402

WEEK_RE = re.compile(r"(?:^|[^A-Za-z0-9])W(\d{1,2})(?=[^0-9]|$)", re.I)
NUM_RE = re.compile(r"\d+")


def week_of(name):
    m = WEEK_RE.search(name)
    return int(m.group(1)) if m else None


def natural(name):
    """Sort W2-T3-P1 before W2-T10-P1: numbers as numbers, the rest as text."""
    return [int(x) if x.isdigit() else x.lower()
            for x in re.split(r"(\d+)", name)]


def pages_of(pdf):
    try:
        out = subprocess.run(["pdfinfo", str(pdf)], capture_output=True,
                             text=True, timeout=60).stdout
        m = re.search(r"^Pages:\s+(\d+)", out, re.M)
        return int(m.group(1)) if m else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def merge(parts, out):
    """qpdf merge, then the page-count check. Returns (ok, message)."""
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["qpdf", "--empty", "--pages"] + [str(p) for p in parts] \
        + ["--", str(out)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    # qpdf exit 3 is "succeeded with warnings", which damaged-but-readable
    # source streams produce; the page-count check below is the real gate.
    if r.returncode not in (0, 3):
        return False, "qpdf failed: %s" % (r.stderr.strip()[:200] or
                                           "exit %d" % r.returncode)
    want = 0
    for p in parts:
        n = pages_of(p)
        if n is None:
            return False, "could not count pages in %s" % p.name
        want += n
    got = pages_of(out)
    if got != want:
        return False, ("page count wrong: parts sum to %d, output has %s"
                       % (want, got))
    return True, "%d part%s, %d pages" % (len(parts), "" if len(parts) == 1
                                          else "s", got)


# 🔴 HANDOUT IS FIRST, and the order is the whole guard. A handout's original
# filename routinely contains the word "slides", so a file named
# `W1-T1-P1 - Handout (W1_T1 Slides Handout).pdf` classifies as a SLIDE the
# moment anything consults the full filename before deciding it is a handout,
# and it would then be merged into the week's slide deck with nothing saying so.
# Added 2026-09-09 with the third kind, because until then a handout matched
# nothing and was skipped, which was safe by accident rather than by design.
KIND_WORDS = (("handout", "handouts"), ("slide", "slides"),
              ("transcript", "transcripts"))
KINDS = {kind for _, kind in KIND_WORDS}


def kind_of(stem, filename):
    """Which of the three kinds this file is, or None.

    The standard part decides; the original name in the trailing parenthetical
    is only consulted when the standard part says nothing.
    `W1-T1-P1 - Transcript (Lecture 1 slides talk-through).pdf` is a transcript,
    whatever its old name mentioned.
    """
    head = re.sub(r"\s*\([^()]*\)\s*$", "", stem).lower()
    for name in (head, filename.lower()):
        for word, kind in KIND_WORDS:
            if word in name:
                return kind
    return None


def collect(folder):
    """{kind: {week or None: [paths]}} for the kinds this consolidates."""
    kinds = {"slides": {}, "transcripts": {}, "handouts": {}}
    for p in sorted(Path(folder).glob("*.pdf"), key=lambda q: natural(q.name)):
        kind = kind_of(p.stem, p.name)
        if kind is None:
            continue
        kinds[kind].setdefault(week_of(p.stem), []).append(p)
    return kinds


def main():
    ap = argparse.ArgumentParser(description="merge per-part PDFs into weekly "
                                             "and whole-course PDFs")
    ap.add_argument("folder", help="the folder of downloaded per-part PDFs "
                                   "(after pdf_fix)")
    # 🔴 Required, same reasoning as mistakes.py and glossary_extend: the
    # output lands inside a course, and a defaulted course is how a wrong
    # course gets written.
    ap.add_argument("--module", required=True, help="which course")
    ap.add_argument("--out", help="output folder (default: the course's "
                                  "consolidated/)")
    ap.add_argument("--config", default=str(S.CONFIG_PATH))
    ap.add_argument("--dry-run", action="store_true")
    # 🟢 Added 2026-09-09 for the transcript repair, which had to rebuild the
    # transcripts and handouts while leaving 291MB of slide decks alone: their
    # inputs had not changed, so re-merging them would have been churn with a
    # nonzero chance of breaking something that was already right.
    ap.add_argument("--kind", action="append", choices=sorted(KINDS),
                    help="only this kind; repeatable. Default: all of them.")
    args = ap.parse_args()

    cfg = S.load_config(Path(args.config).expanduser())
    mods = S.resolve_modules(cfg)
    if args.module not in mods:
        raise SystemExit("no course called %r. There %s: %s"
                         % (args.module, "is" if len(mods) == 1 else "are",
                            ", ".join(sorted(mods)) or "none yet"))
    src = Path(args.folder).expanduser()
    if not src.is_dir():
        raise SystemExit("%s is not a folder" % src)
    outdir = Path(args.out).expanduser() if args.out \
        else mods[args.module] / "consolidated"

    kinds = collect(src)
    if args.kind:
        kinds = {k: v for k, v in kinds.items() if k in set(args.kind)}
    total = sum(len(v) for byweek in kinds.values() for v in byweek.values())
    if not total:
        raise SystemExit("nothing to consolidate in %s: no PDF here has "
                         "'Slides', 'Transcript' or 'Handout' in its name, "
                         "which is the form download-keats files them under "
                         "(`<DOC> - Slides (<original>).pdf`)" % src)

    label = {"slides": "All Slides", "transcripts": "All Transcripts",
             "handouts": "All Handouts"}
    jobs = []
    for kind, byweek in kinds.items():
        weekless = byweek.pop(None, [])
        for wk in sorted(byweek):
            jobs.append(("W%d - %s.pdf" % (wk, label[kind]), byweek[wk]))
        everything = [p for wk in sorted(byweek) for p in byweek[wk]] + weekless
        if everything:
            jobs.append(("%s.pdf" % label[kind], everything))
        if weekless:
            print("note: %d %s file%s carr%s no W<n> week and join%s only the "
                  "whole-course PDF: %s"
                  % (len(weekless), kind, "" if len(weekless) == 1 else "s",
                     "ies" if len(weekless) == 1 else "y",
                     "s" if len(weekless) == 1 else "",
                     ", ".join(p.name for p in weekless[:6])))

    failures = 0
    for name, parts in jobs:
        if args.dry_run:
            print("would write %s from %d part%s" % (name, len(parts),
                                                     "" if len(parts) == 1
                                                     else "s"))
            continue
        ok, msg = merge(parts, outdir / name)
        print("%s %s (%s)" % ("wrote" if ok else "FAILED", name, msg))
        failures += 0 if ok else 1
    if not args.dry_run:
        print("done: %d PDF%s in %s%s"
              % (len(jobs) - failures, "" if len(jobs) - failures == 1 else "s",
                 outdir,
                 "; %d FAILED" % failures if failures else ""))
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
