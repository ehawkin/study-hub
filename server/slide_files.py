#!/usr/bin/env python3
"""List a part's slide images, in slide order, and refuse a glob that misses any.

    python3 server/slide_files.py PSY101 W1-T2-P1
    python3 server/slide_files.py PSY101 W1-T2-P1 --check-glob 'Slide*.jpg'
    python3 server/slide_files.py PSY101 --course

🔴 WHY THIS EXISTS. `study-hub-content` OCR'd a part with `Slide*.jpg` and got
19 of 20 files: `Slide09` is a `.png`. No error, no warning, one fewer slide.
Counted across the course, **27 of 34 parts mix the two extensions, and in
Weeks 1 to 3 the `.png` is the MAJORITY** - one part matched 3 of its 21 files.
So this is not an edge case being missed, it is most of the deck being missed,
silently, **in the direction that reads as success**.

🔴 AND WHY IT IS NOT A WIDER GLOB. `*.{jpg,png}` treats this instance and leaves
the mechanism: the next course ships a `.jpeg` or a `.webp` and it fails again
the same silent way. **A slide is recognised by its NUMBERING, `SlideNN.<any>`,
never by a list of extensions this project happens to have seen.** An extension
list is the bug with a longer table.

⚠️ AND NOTHING IS DROPPED QUIETLY AT THE OTHER END EITHER. A file in the
directory that does NOT look like a numbered slide is reported out loud rather
than filtered away, because "I ignored something" and "there was nothing to
ignore" must not look the same.
"""
import argparse
import os
import pathlib
import re
import sys

# `SlideNN.<ext>`: the number is what identifies it, the extension is whatever
# the exporter happened to write. Case-insensitive because an exporter that
# writes `slide01.JPG` is the same exporter having a different day.
SLIDE = re.compile(r"^slide(\d+)\.([^.]+)$", re.I)


def classify(names):
    """(slides, others): slides as (number, name, extension), sorted by NUMBER.

    🔴 Sorted numerically and not lexicographically, or `Slide10` sorts before
    `Slide9` and a caller reading "in order" gets a deck out of order.
    """
    slides, others = [], []
    for name in names:
        m = SLIDE.match(name)
        if m:
            slides.append((int(m.group(1)), name, m.group(2).lower()))
        else:
            others.append(name)
    return sorted(slides), sorted(others)


def tally(slides):
    """{extension: count}, so a mixed directory says so in its own result line."""
    out = {}
    for _, _, ext in slides:
        out[ext] = out.get(ext, 0) + 1
    return out


def describe(slides, others):
    """The positive result. 🔴 A check that is silent on success is
    indistinguishable from a check that never ran, which is this project's own
    rule and the reason this line exists at all."""
    if not slides:
        return "no numbered slides here"
    kinds = ", ".join("%s %d" % (e, n) for e, n in sorted(tally(slides).items()))
    span = "Slide%02d to Slide%02d" % (slides[0][0], slides[-1][0])
    said = "%d slides, %s, %s" % (len(slides), span, kinds)
    if others:
        said += "; %d file(s) that are not numbered slides: %s" % (
            len(others), ", ".join(others[:4]))
    return said


def gaps(slides):
    """Numbers missing from the run. A deck numbered 1..20 with 19 files has a
    hole, and the hole is a better thing to report than the count alone."""
    if not slides:
        return []
    have = set(n for n, _, _ in slides)
    return [n for n in range(slides[0][0], slides[-1][0] + 1) if n not in have]


def missed_by(pattern, slides):
    """Which numbered slides a glob would NOT collect, by name.

    ⚠️ Names, not a count: "the glob matched 19 of 20" tells you to look, and
    "the glob missed Slide09.png" tells you what to look at.
    """
    import fnmatch
    return [name for _, name, _ in slides if not fnmatch.fnmatch(name, pattern)]


def read_dir(path):
    """Names in a directory, or None when it is not one. Dotfiles are skipped
    (`.DS_Store` is not a missing slide), and that is the ONLY name-based
    exclusion in this module."""
    p = pathlib.Path(path)
    if not p.is_dir():
        return None
    return [n for n in os.listdir(p) if not n.startswith(".")]


def slides_dir(root, module, doc):
    return pathlib.Path(root) / "materials" / module / "source" / doc / "slides"


def course_dirs(root, module):
    base = pathlib.Path(root) / "materials" / module / "source"
    if not base.is_dir():
        return []
    return sorted((d.name, d / "slides") for d in base.iterdir()
                  if (d / "slides").is_dir())


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("module")
    ap.add_argument("doc", nargs="?")
    ap.add_argument("--course", action="store_true",
                    help="sweep every part of the course instead of one doc")
    ap.add_argument("--check-glob", metavar="PATTERN",
                    help="refuse if this glob would miss any numbered slide")
    ap.add_argument("--dir", help="a slides directory, instead of deriving one")
    ap.add_argument("--root", default=str(pathlib.Path(__file__).resolve().parent.parent))
    args = ap.parse_args(argv)

    if args.course:
        rows = course_dirs(args.root, args.module)
        if not rows:
            print("no source parts with a slides directory under %s"
                  % (pathlib.Path(args.root) / "materials" / args.module / "source"),
                  file=sys.stderr)
            return 1
        mixed = 0
        for doc, path in rows:
            slides, others = classify(read_dir(path) or [])
            kinds = tally(slides)
            if len(kinds) > 1:
                mixed += 1
            print("%-12s %s" % (doc, describe(slides, others)))
        print("\n%d part(s) with slides, %d of them mixing extensions. "
              "A glob on ONE extension is wrong for %d of them."
              % (len(rows), mixed, mixed), file=sys.stderr)
        return 0

    if not (args.doc or args.dir):
        ap.error("give a doc id, or --dir, or --course")
    path = pathlib.Path(args.dir) if args.dir else slides_dir(
        args.root, args.module, args.doc)
    names = read_dir(path)
    if names is None:
        print("REFUSING: no such slides directory: %s" % path, file=sys.stderr)
        return 1
    slides, others = classify(names)
    print(describe(slides, others), file=sys.stderr)
    missing = gaps(slides)
    if missing:
        print("⚠️  numbering has holes at: %s"
              % ", ".join(str(n) for n in missing), file=sys.stderr)

    if args.check_glob:
        missed = missed_by(args.check_glob, slides)
        if missed:
            print("REFUSING: %d file(s) in %s, the glob %r would collect %d. "
                  "It misses: %s"
                  % (len(slides), path, args.check_glob,
                     len(slides) - len(missed), ", ".join(missed)),
                  file=sys.stderr)
            return 1
        print("the glob %r collects all %d" % (args.check_glob, len(slides)),
              file=sys.stderr)

    for _, name, _ in slides:
        print(str(path / name))
    return 0


if __name__ == "__main__":
    sys.exit(main())
