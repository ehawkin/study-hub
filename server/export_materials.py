#!/usr/bin/env python3
"""A course's whole shape as a spreadsheet: one row per part, nothing decided.

    python3 server/export_materials.py --module PSY101
    python3 server/export_materials.py --module PSY101 --out ~/Desktop/course.csv
    python3 server/export_materials.py --module PSY101 --no-words   # skip the PDF scan

**EH's ask, 2026-09-09**, while reading the ingest prompt written for a friend:
a table of every lesson, week, topic and part, the length of each video, and a
link to the video, the transcript and the slide file. 🟢 **And what it is for,
which is not only us:** *"that's all of that information that we actually need
in order to build the course later, but this can be useful for people who are
not actually building a course."*

🔴 **THIS COLLECTS NOTHING AND DECIDES NOTHING.** `materials.json` already holds
every field and `verify_course.py` already checks it; this is a serialiser over
data that is complete. ⚠️ **It is a VIEW, never a source of truth**: it is
regenerated on demand and nothing anywhere reads the CSV back in.

**The shape, and each rule earns its place:**

- **One row per PART. Weeks and topics are COLUMNS, never rows**, with no
  subtotal, separator or merged cells, so the file stays sortable and
  filterable in anything that opens it.
- **Numbers and titles in SEPARATE columns** (`week` and `week_title`): you sort
  on one and read the other. `materials.json` already models it that way.
- **Both the course-site URL and the local file**, for slides and transcript.
  The local file is what you read; the URL is how you re-fetch it, check it
  against source, or find out the course replaced it.
- 🔴 **A part with something missing still gets a ROW, with an empty cell.** A
  missing row hides a gap, which is the failure this table exists to expose.
- ⚠️ **`combined` says when `slides` and `transcript` point at the SAME url**,
  which is the combined-handout case: **a course can publish its deck and its
  transcript inside one weekly package**, and one course onboarded here does.
  Without that column the duplication reads as a bug in this script.
- 🟢 **Extractable word counts for each local PDF**, which is the cheapest
  possible check that the OCR pass did anything: a transcript for a 15-minute
  lecture showing 100 words is obvious in a column and invisible everywhere
  else.

**CSV only.** The server is stdlib-only by design and a test enforces it, so an
`.xlsx` would mean a dependency. Every spreadsheet opens CSV.
"""
import argparse
import csv
import json
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import study_server as S            # noqa: E402

REPO = HERE.parent

# The per-part local files, named by `build_part_assets.py` and by the download
# skills: `<DOC> - <Kind> (<original>).pdf`. 🔴 Handout is checked FIRST and the
# reason is in `test_consolidate_pdfs.py`: a handout's original name routinely
# contains the word "slides", so anything reading the whole filename first
# classifies it as a deck.
KIND_WORDS = (("handout", "handout"), ("slide", "slides"),
              ("transcript", "transcript"))

COLUMNS = [
    "doc", "week", "week_title", "topic_no", "topic", "part", "title",
    "minutes", "media_bytes", "lesson_file",
    "video_url", "video_kind", "entry",
    "slides_url", "transcript_url", "combined",
    "slides_file", "slides_words",
    "transcript_file", "transcript_words",
    "handout_file", "handout_words",
]


def kind_of(filename):
    """Which of the three a local PDF is, or None.

    Reads the STANDARD part before the parenthetical, for the reason above.
    """
    stem = filename[:-4] if filename.lower().endswith(".pdf") else filename
    head = re.sub(r"\s*\([^()]*\)\s*$", "", stem).lower()
    for name in (head, filename.lower()):
        for word, kind in KIND_WORDS:
            if word in name:
                return kind
    return None


def local_files(materials_dir):
    """{doc: {kind: Path}} for a folder of `<DOC> - <Kind> (<orig>).pdf`.

    ⚠️ Deliberately tolerant: a folder that is not there, or holds something
    else, yields nothing rather than raising. The table's job is to show what
    is present, and "no local copy" is a legitimate answer for a course whose
    material was never downloaded.
    """
    out = {}
    d = Path(materials_dir) if materials_dir else None
    if not d or not d.is_dir():
        return out
    for f in sorted(d.glob("*.pdf")):
        doc = f.name.split(" - ", 1)[0].strip()
        kind = kind_of(f.name)
        if kind:
            out.setdefault(doc, {})[kind] = f
    return out


def words_in(pdf):
    """Extractable words, or -1 when it cannot be read.

    🔴 -1 rather than 0, and the distinction is the point: 0 is a real answer
    (a scanned PDF nobody has OCR'd) and it is the answer this column exists to
    make visible. Reporting "could not read it" as 0 would hide the tool being
    missing behind a finding about the file.
    """
    try:
        r = subprocess.run(["pdftotext", str(pdf), "-"],
                           capture_output=True, text=True, timeout=180)
    except (OSError, subprocess.SubprocessError):
        return -1
    if r.returncode != 0:
        return -1
    return len((r.stdout or "").split())


def row_for(doc, rec, files, counts):
    """One part's row. Pure, so every rule above is testable without a course."""
    rec = rec or {}
    got = files.get(doc, {})
    slides_url = str(rec.get("slides") or "")
    transcript_url = str(rec.get("transcript") or "")
    row = {
        "doc": doc,
        "week": rec.get("week", ""),
        "week_title": rec.get("weekTitle", ""),
        "topic_no": rec.get("topicNo", ""),
        "topic": rec.get("topic", ""),
        "part": rec.get("part", ""),
        "title": rec.get("title", ""),
        "minutes": rec.get("minutes", ""),
        "media_bytes": rec.get("media_bytes", ""),
        "lesson_file": rec.get("href", ""),
        "video_url": rec.get("video", ""),
        "video_kind": rec.get("video_kind", ""),
        "entry": rec.get("entry", ""),
        "slides_url": slides_url,
        "transcript_url": transcript_url,
        # ⚠️ Only when BOTH are present. Two empty fields are not "the same
        # document", they are two gaps, and calling that combined would report a
        # course that published nothing as a course that published one thing.
        "combined": ("yes" if (slides_url and slides_url == transcript_url)
                     else ""),
    }
    for kind, col in (("slides", "slides"), ("transcript", "transcript"),
                      ("handout", "handout")):
        f = got.get(kind)
        row[col + "_file"] = f.name if f else ""
        n = counts.get(f) if f else None
        row[col + "_words"] = "" if n is None else n
    return row


def rows_for(data, files, counts):
    """Every part, in the course's own order, with anything extra after it.

    🔴 `order` is the course's sequence and is what the reader sees; a doc
    present in `docs` but missing from `order` is a real inconsistency, so it is
    APPENDED rather than dropped. Dropping it would make this table quietly
    disagree with the course it describes.
    """
    docs = (data or {}).get("docs") or {}
    order = [d for d in ((data or {}).get("order") or []) if d in docs]
    extra = sorted(d for d in docs if d not in set(order))
    return [row_for(d, docs[d], files, counts) for d in order + extra]


def main():
    ap = argparse.ArgumentParser(
        description="write a course's materials.json out as a CSV table")
    # Required, for the reason consolidate_pdfs states: a defaulted course is
    # how the wrong course gets read.
    ap.add_argument("--module", required=True, help="which course")
    ap.add_argument("--out", help="where to write it "
                                  "(default: <course>/materials.csv)")
    ap.add_argument("--materials", help="the folder of local per-part PDFs "
                                        "(default: materials/<CODE>)")
    ap.add_argument("--no-words", action="store_true",
                    help="skip the PDF word counts, which is the slow part")
    ap.add_argument("--config", default=str(S.CONFIG_PATH))
    args = ap.parse_args()

    cfg = S.load_config(Path(args.config).expanduser())
    mods = S.resolve_modules(cfg)
    if args.module not in mods:
        raise SystemExit("no course called %r. There %s: %s"
                         % (args.module, "is" if len(mods) == 1 else "are",
                            ", ".join(sorted(mods)) or "none yet"))
    course = mods[args.module]
    src = course / "materials.json"
    if not src.is_file():
        raise SystemExit("%s has no materials.json, so there is nothing to "
                         "export yet." % args.module)
    try:
        data = json.loads(src.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise SystemExit("%s will not parse: %s" % (src, exc))

    mdir = Path(args.materials).expanduser() if args.materials \
        else REPO / "materials" / args.module
    files = local_files(mdir)

    counts = {}
    if not args.no_words:
        every = sorted({f for kinds in files.values() for f in kinds.values()})
        for i, f in enumerate(every, 1):
            print("[%d/%d] reading %s" % (i, len(every), f.name))
            counts[f] = words_in(f)

    rows = rows_for(data, files, counts)
    out = Path(args.out).expanduser() if args.out else course / "materials.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS)
        w.writeheader()
        for r in rows:
            w.writerow(r)

    # 🔴 A POSITIVE RESULT, even at zero, so a run that found nothing is
    # distinguishable from a check that never ran. This project has been bitten
    # by silence-on-success more than once.
    with_local = sum(1 for r in rows if r["slides_file"] or r["transcript_file"]
                     or r["handout_file"])
    combined = sum(1 for r in rows if r["combined"])
    print("\n%s: %d parts written to %s" % (args.module, len(rows), out))
    print("  %d of %d have a local PDF beside them%s"
          % (with_local, len(rows),
             "" if files else " (no local materials folder at %s)" % mdir))
    if combined:
        print("  %d publish slides and transcript at the SAME url, which is the "
              "combined-handout case rather than a duplicate" % combined)
    if not args.no_words:
        empty = [r["doc"] for r in rows
                 if r["transcript_words"] not in ("", -1)
                 and int(r["transcript_words"]) < 200]
        print("  word counts read for %d files%s"
              % (len(counts),
                 ("; \U0001f534 %d transcripts hold under 200 words: %s"
                  % (len(empty), ", ".join(empty[:6]))) if empty else
                 ", and every transcript holds more than 200 words"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
