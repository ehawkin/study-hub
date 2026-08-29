#!/usr/bin/env python3
"""Fix a PDF's page orientation, then give it a trustworthy text layer.

R32 and R33, greenlit 2026-08-22: slides arrive with a few pages rotated and
with OCR bad enough that search and highlighting miss real words. EH's own
proposed method (try orientations, keep the one that reads best) is exactly
what tesseract's orientation detection does, so this wraps `ocrmypdf` rather
than reinventing it.

    python3 server/pdf_fix.py <file-or-folder> [...]     # writes <name>.pdf in place
    python3 server/pdf_fix.py --check <file-or-folder>   # report only, write nothing

Two passes, because ocrmypdf will not combine them:

1. Orientation, EH's own method, measured not guessed: render each page,
   OCR it as-is, and if it reads poorly try the other three rotations and
   keep whichever yields the most confident words. (ocrmypdf's own
   `--rotate-pages` was tried first and rejected by test: tesseract's OSD
   gave a sideways page of dense text confidence 1.59 against a threshold
   of 14, so it silently refused, and slides with sparse text score even
   lower. The yield method decides on evidence the page actually offers.)
   Rotations are applied losslessly with qpdf.
2. `--redo-ocr`: replaces text that came from OCR with fresh OCR, while
   leaving born-digital text alone. Much of this material is already OCR'd,
   badly, so "add text only where missing" would keep the bad text; redo is
   the point. If a file defeats `--redo-ocr` (some encrypted or exotic PDFs
   do), fall back to `--skip-text` and say so, rather than failing the file.

The original is kept as a dated copy in `backups/` beside the file, like every
other backup in a course (the backup_target convention).
"""
import argparse
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import split_lessons as SPLIT

OCRMYPDF = shutil.which("ocrmypdf")


def run(args_list):
    return subprocess.run(args_list, capture_output=True, text=True)


TESSERACT = shutil.which("tesseract")
QPDF = shutil.which("qpdf")
PDFTOPPM = shutil.which("pdftoppm")
PDFTOTEXT = shutil.which("pdftotext")


def page_text_words(path, pageno):
    """Words in the page's EXISTING text layer, from pdftotext. Cheap and
    decisive: a born-digital page reads fine by construction and must never
    be rotated on pixel evidence."""
    if not PDFTOTEXT:
        return 0
    out = run([PDFTOTEXT, "-f", str(pageno), "-l", str(pageno), str(path), "-"])
    return len((out.stdout or "").split())


def word_yield(png):
    """(confident words, fraction of the page their bounding box covers).

    🔴 --psm 6, and it is the whole method. At its default segmentation
    tesseract quietly auto-corrects orientation, so a sideways page yields as
    many "confident" words as an upright one (measured: 589 both ways) and no
    rotation can ever win. psm 6 reads the pixels as they lie; on the same
    page the yields became 106 sideways against 593 upright. A sideways dense
    page still fakes three digits of confident words, which is why there is
    no reads-fine-already shortcut for scans: every scanned page pays for all
    four measurements.

    The area fraction exists because of a real casualty: a publisher's
    download stamp running up the margin of an otherwise sparse page
    out-yielded the page once rotated, and a correct page went sideways. A
    genuine page of text occupies a broad box; a stamp occupies one thin
    strip, and a win whose words all live in a strip is not a win.
    """
    out = run([TESSERACT, str(png), "stdout", "--psm", "6", "tsv"])
    n = 0
    x0 = y0 = float("inf")
    x1 = y1 = 0.0
    page_w = page_h = 0.0
    for line in (out.stdout or "").splitlines()[1:]:
        cols = line.split("\t")
        if len(cols) < 12:
            continue
        try:
            level = int(cols[0])
            left, top = float(cols[6]), float(cols[7])
            width, height = float(cols[8]), float(cols[9])
        except ValueError:
            continue
        if level == 1:
            page_w, page_h = width, height
            continue
        if not cols[11].strip():
            continue
        try:
            if float(cols[10]) > 60:
                n += 1
                x0, y0 = min(x0, left), min(y0, top)
                x1, y1 = max(x1, left + width), max(y1, top + height)
        except ValueError:
            pass
    if n == 0 or not page_w or not page_h:
        return n, 0.0
    frac = max(0.0, (x1 - x0)) * max(0.0, (y1 - y0)) / (page_w * page_h)
    return n, frac


# Two guards, either of which alone would have saved the stamped page: a page
# whose text layer already reads is born-digital (READS_ALREADY words from
# pdftotext) and is never judged on pixels; and a rotation may only win with
# words that cover a real area of the page, not one strip (MIN_WIN_AREA).
#
# 🔴 The queue entry's third candidate, an absolute yield floor on the
# upright original, was implemented and REMOVED by test: a sideways dense
# scan fakes 149 "confident" upright words (the docstring below predicted
# exactly this), so a floor of 100 quietly un-fixed the true positives this
# tool exists for. Do not reintroduce it.
READS_ALREADY = 25
MIN_WIN_AREA = 0.10


def page_rotations(path, tmpdir):
    """{page_number: degrees_clockwise} for pages that read better rotated."""
    run([PDFTOPPM, "-r", "150", "-png", str(path), str(tmpdir / "pg")])
    fixes = {}
    for png in sorted(tmpdir.glob("pg*.png")):
        pageno = int(png.stem.rsplit("-", 1)[-1])
        if page_text_words(path, pageno) >= READS_ALREADY:
            continue
        y0, _ = word_yield(png)
        best_deg, best, best_frac = 0, y0, 0.0
        for deg in (90, 180, 270):
            rot = tmpdir / ("r%d-%s" % (deg, png.name))
            run(["sips", "-r", str(deg), str(png), "--out", str(rot)])
            y, frac = word_yield(rot)
            if y > best:
                best_deg, best, best_frac = deg, y, frac
        # Rotate only on clear evidence: a decisive win, not a coin flip, and
        # never a win made of one thin strip of stamp text.
        if (best_deg and best >= 10 and best >= 2 * max(y0, 1)
                and best_frac >= MIN_WIN_AREA):
            fixes[pageno] = best_deg
    return fixes


def fix_one(path, check=False):
    """Returns a one-line report for this file."""
    if not OCRMYPDF:
        return "SKIP %s: ocrmypdf is not installed (brew install ocrmypdf)" % path.name
    if not (TESSERACT and QPDF and PDFTOPPM):
        return "SKIP %s: needs tesseract, qpdf and pdftoppm on PATH" % path.name
    import tempfile
    tmp1 = path.with_name(path.stem + ".rot.tmp.pdf")
    tmp2 = path.with_name(path.stem + ".ocr.tmp.pdf")
    try:
        with tempfile.TemporaryDirectory() as td:
            fixes = page_rotations(path, Path(td))
        rotated = bool(fixes)
        if fixes:
            args = [QPDF, str(path), str(tmp1)]
            for pageno, deg in sorted(fixes.items()):
                args.append("--rotate=+%d:%d" % (deg, pageno))
            r0 = run(args)
            if r0.returncode not in (0, 3) or not tmp1.exists():
                return "FAIL %s: qpdf rotate: %s" % (path.name,
                                                    (r0.stderr or "").strip()[-120:])
        src = tmp1 if tmp1.exists() else path

        r2 = run([OCRMYPDF, "--redo-ocr", str(src), str(tmp2)])
        mode = "redo-ocr"
        if r2.returncode != 0 or not tmp2.exists():
            r2 = run([OCRMYPDF, "--skip-text", str(src), str(tmp2)])
            mode = "skip-text fallback"
        if r2.returncode != 0 or not tmp2.exists():
            return "FAIL %s: %s" % (path.name, (r2.stderr or "").strip()[-160:])

        if check:
            return "would fix %s (%s%s)" % (path.name, mode,
                                            ", rotating pages %s" % sorted(fixes) if rotated else "")
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.copy2(path, SPLIT.backup_target(path, "%s.%s.bak" % (path.name, stamp)))
        shutil.move(str(tmp2), str(path))
        return "fixed %s (%s%s)" % (path.name, mode,
                                    ", rotated pages %s" % sorted(fixes) if rotated else "")
    finally:
        for t in (tmp1, tmp2):
            if t.exists():
                t.unlink()


def main():
    ap = argparse.ArgumentParser(description="fix PDF orientation and OCR")
    ap.add_argument("targets", nargs="+", help="PDF files or folders of them")
    ap.add_argument("--check", action="store_true", help="report, write nothing")
    args = ap.parse_args()

    pdfs = []
    for t in args.targets:
        p = Path(t).expanduser()
        if p.is_dir():
            pdfs.extend(sorted(q for q in p.glob("*.pdf") if q.is_file()))
        elif p.is_file():
            pdfs.append(p)
        else:
            print("no such file: %s" % p)
    if not pdfs:
        print("nothing to do")
        return 1
    for p in pdfs:
        print(fix_one(p, check=args.check))
    return 0


if __name__ == "__main__":
    sys.exit(main())
