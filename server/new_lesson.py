#!/usr/bin/env python3
"""Scaffold a new lesson, so writing one is writing prose.

R10, "generate a lesson from a set of files". The *writing* is the
`write-lesson` skill and `NOTE-SPEC.md`; this is the part of it that is
mechanical and therefore should not be done by hand or by an agent guessing at
boilerplate. It creates a structurally correct, empty lesson: the content
marker, the title tag, a valid `lesson-meta` block, the house stylesheet, and an
empty `.wrap` waiting for the lesson.

    python3 server/new_lesson.py --doc W6-T1-P1 --title "Something true"
    python3 server/new_lesson.py --module PSY101 --doc L07 --title "..." \
        --week 3 --topic "Memory" --part 1

🔴 **The stylesheet is taken from the course the lesson is joining**, not from a
copy kept here, so a course whose look has moved on stays consistent with itself
and nothing has to be kept in step by hand. A course with no lessons yet gets
`server/reader/lesson-base.css`, which is the house style. That is the only
copy, and it is a starting template rather than a shared sheet: a lesson embeds
its own CSS because a lesson has to stand alone.

What this does NOT do is write the lesson. It prints what to read and which gate
to run, and stops.
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import split_lessons as SPLIT

CONFIG_PATH = Path(os.environ.get("KCL_STUDY_CONFIG", "~/.kcl-study/config.json")).expanduser()
REPO = Path(__file__).resolve().parent.parent
BASE_CSS = Path(__file__).resolve().parent / "reader" / "lesson-base.css"

# Same rule as the server's, because this writes a filename the server has to
# accept and a doc id that becomes part of every sidecar's name.
DOC_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,15}(?:-[A-Za-z0-9]{1,16}){0,5}\Z")


class Problem(Exception):
    pass


def load_config(path=CONFIG_PATH):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise Problem("cannot read %s: %s" % (path, exc))


def module_folder(cfg, module):
    if module:
        root = str(cfg.get("courses_dir") or "").strip()
        if not root:
            raise Problem("--module needs a courses_dir in the config")
        return Path(root).expanduser() / module
    return Path(cfg["notes_dir"]).expanduser().resolve()


def backup(path, move=False):
    """`<name>.YYYYMMDD-HHMMSS.bak` beside the original. Copy by default; move
    when the original must stop being a lesson. Returns (original, backup)."""
    from datetime import datetime
    import shutil
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = path.with_name(path.name + "." + stamp + ".bak")
    n = 1
    while dest.exists():
        dest = path.with_name("%s.%s-%d.bak" % (path.name, stamp, n))
        n += 1
    (shutil.move if move else shutil.copy2)(str(path), str(dest))
    return (path, dest)


SLUG_MAX = 60


def slugify(title):
    """A filename tail from the title, at whatever length the title gives.
    Lowercase words joined by dashes, which is what every lesson in this project
    already looks like."""
    s = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return re.sub(r"-{2,}", "-", s) or "lesson"


def shorten(slug, cap=SLUG_MAX):
    """`slug` cut to `cap` characters ON A WORD BOUNDARY.

    🔴 This used to be a bare `[:60]` inside slugify, which cut mid-word and said
    nothing. Two lessons in this repo are still named for it: one ends
    `...whether-anyone-can-have-it-unt` and another `...what-the-trials-actually-fou`.
    ⚠️ The cosmetic half is the smaller half. The truncation also produced a
    filename the caller had not computed, the write went to the untruncated path,
    and the lesson was never created at all while the gate reported ALL CHECKS
    PASS over the scaffold. main() now prints what it used whenever the two differ,
    because a filename the writer did not choose is one they will not recognise.
    """
    if len(slug) <= cap:
        return slug
    # cap + 1 so a slug that happens to break exactly at the cap keeps its last
    # whole word instead of losing it.
    cut = slug[:cap + 1]
    return cut.rsplit("-", 1)[0] if "-" in cut[1:] else slug[:cap]


def house_css(folder):
    """The stylesheet a new lesson in this course should start from.

    The one the most lessons in the course already carry, so a course that has
    evolved its own look keeps it. Falls back to the shipped template when the
    course is empty, which is every recipient's first lesson."""
    import collections
    seen = collections.Counter()
    first = {}
    for p in SPLIT.lessons_in(folder):
        m = re.search(r"(?s)<style>(.*?)</style>", p.read_text(encoding="utf-8"))
        if not m:
            continue
        css = m.group(1).strip()
        seen[css] += 1
        first.setdefault(css, p.name)
    if seen:
        css, n = seen.most_common(1)[0]
        return css, "%d of %d lessons in this course" % (n, sum(seen.values()))
    try:
        text = BASE_CSS.read_text(encoding="utf-8")
    except OSError as exc:
        raise Problem("no lessons to copy a stylesheet from, and %s is missing: %s"
                      % (BASE_CSS.name, exc))
    # Drop the template's own explanatory header; it is about the template, and
    # a lesson carries no commentary about how it was made.
    text = re.sub(r"(?s)\A\s*/\*.*?\*/\s*", "", text)
    return text.strip(), "the house template (this course has no lessons yet)"


def build(meta, css, title_tag):
    """The content file, in the shape `split_lessons.read_content` requires."""
    return (
        SPLIT.CONTENT_MARK + "\n"
        + "<title>" + title_tag + "</title>\n"
        + SPLIT.META_OPEN + "\n"
        + json.dumps(meta, indent=2, ensure_ascii=False) + "\n"
        + SPLIT.META_CLOSE + "\n\n"
        + "<style>\n" + css + "\n</style>\n\n"
        + '<div class="wrap">\n'
        + "  <!-- The lesson goes here. Read NOTE-SPEC.md before writing it:\n"
        + "       transmit rather than point, stand alone, and verify every DOI. -->\n"
        + "</div>\n")


def register(folder, doc, filename):
    """Point this course's materials table at the new lesson, when it has a row
    for it. Additive and never overwriting: a wrong href is worse than none."""
    path = folder / "materials.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    docs = data.get("docs")
    if not isinstance(docs, dict) or doc not in docs:
        return None
    entry = docs[doc]
    if not isinstance(entry, dict) or entry.get("href"):
        return None
    entry["href"] = filename
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")
    return path


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--config", default=str(CONFIG_PATH))
    ap.add_argument("--module", default="", help="which course (default: the configured one)")
    ap.add_argument("--doc", required=True, help="the lesson's id, unique in the course")
    ap.add_argument("--title", required=True, help="the lesson's title")
    ap.add_argument("--slug", default="", help="filename tail (default: from the title)")
    ap.add_argument("--week", default="")
    ap.add_argument("--week-title", default="")
    ap.add_argument("--topic-no", default="")
    ap.add_argument("--topic", default="")
    ap.add_argument("--part", default="")
    ap.add_argument("--force", action="store_true", help="overwrite an existing lesson")
    args = ap.parse_args()

    try:
        doc = args.doc.strip()
        if not DOC_ID_RE.match(doc):
            raise Problem("%r is not a usable id. Letters, digits and single "
                          "dashes: W6-T1-P1, L07, unit3-2." % doc)
        cfg = load_config(args.config)
        folder = module_folder(cfg, args.module)
        if not folder.is_dir():
            raise Problem("no course at %s. Import a lesson pack into it first, "
                          "or make the folder." % folder)

        # 🔴 Two lessons sharing a doc id share every sidecar, so one's
        # highlights would appear on the other. Checked against ids, not
        # filenames, because the slug comes from the title and a retitled lesson
        # would otherwise slip past as a new file.
        existing = [p for p in SPLIT.lessons_in(folder)
                    if p.name.startswith(doc + "-")]
        if existing and not args.force:
            raise Problem("%s already exists (%s). Use --force to replace it, "
                          "and back it up first if it has been read."
                          % (doc, existing[0].name))

        full = slugify(args.title)
        slug = args.slug.strip() or shorten(full)
        long_title = not args.slug.strip() and slug != full
        filename = "%s-%s.html" % (doc, slug)
        target = folder / filename

        # 🔴 --force REPLACES the lesson holding this id; it does not add a
        # second one. The slug comes from the title, so forcing with a new title
        # wrote a new filename and left the old file in place, and the course
        # then had two lessons answering to one doc id and sharing its marks.
        # Renames rather than deletes, and only after the new file is written,
        # so a failure leaves the old one where it was.
        stale = [p for p in existing if p.resolve() != target.resolve()]

        meta = {"doc": doc, "title": args.title.strip()}
        for key, val in (("week", args.week), ("topicNo", args.topic_no),
                         ("weekTitle", args.week_title), ("topic", args.topic),
                         ("part", args.part)):
            if str(val).strip():
                meta[key] = str(val).strip()

        css, whence = house_css(folder)
        text = build(meta, css, args.title.strip())

        # Prove it before writing it: the file has to satisfy the same reader
        # that will serve it, and finding out later means finding out from a
        # blank page.
        SPLIT.read_content(text, filename)

        if target.exists() and not args.force:
            raise Problem("%s already exists" % target)
        if target.exists():
            # Overwriting a lesson is destructive, so the old one is kept beside
            # it. Backups in this project stay; they are free.
            backup(target)
        target.write_text(text, encoding="utf-8")

        moved = []
        for p in stale:
            moved.append(backup(p, move=True))

        print("Wrote %s" % target)
        if long_title:
            print("  ⚠️ the title is longer than a filename tail, so this lesson is")
            print("     named %r, cut at a word from %r" % (slug, full))
        for m in moved:
            print("  🔴 replaced %s, kept as %s" % (m[0].name, m[1].name))
        print("  stylesheet from: %s" % whence)
        print("  meta: %s" % ", ".join("%s=%s" % (k, v) for k, v in meta.items()))
        touched = register(folder, doc, filename)
        if touched:
            print("  materials.json now points at it")
        print("")
        print("Next: read NOTE-SPEC.md, write the lesson inside .wrap, then")
        print("  python3 server/verify_notes.py --notes %s" % folder)
    except Problem as exc:
        sys.exit("%s" % exc)


if __name__ == "__main__":
    main()
