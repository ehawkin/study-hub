#!/usr/bin/env python3
"""R48: a lesson as something one student can hand to another.

EH, 2026-08-16: "it would be a good feature for students to be able to export
a lesson and for other students to be able to import one or more lessons from a
directory or a file … you can import a lesson with or without the links … this
way I could share a lesson with someone that they could load onto their machine
without having to run their own scan and download the KEATS module, as long as
they have access to KEATS."

    python3 server/lesson_packs.py --export W3-T3-P4 --to ~/Desktop/share
    python3 server/lesson_packs.py --export-all --to ~/Desktop/share
    python3 server/lesson_packs.py --import ~/Desktop/share          # a folder
    python3 server/lesson_packs.py --import one.lesson.html --no-links

**A pack is one file, and it is the lesson.** The content/reader split already
made a lesson file the note and nothing else, so a pack is that file with one
extra JSON block carrying its links: the KEATS lecture permalink, the Kaltura
entry id, the slide and transcript URLs. It opens in a browser on its own, it
imports by being copied into a module folder, and its links merge into that
module's `materials.json` so the reader's Materials pane works with nothing
downloaded.

🔴 **What a pack deliberately does NOT carry.**

* **No materials.** Not the slides, not the transcripts, not the video. Those are
  KCL's, and EH's constraint from the start is that the downloaded folder is
  never shared. A pack carries the ADDRESSES of those things; each recipient
  fetches them with their own KEATS login.
* **Nobody's marks.** No highlights, notes, cards, bookmarks or chats. A lesson
  is the material; the reading of it is the reader's own.

🔴 **The honest limitation, printed by --export**: the KEATS lecture link works
for anyone enrolled on the module, and so does the Kaltura video the reader
plays in its own pane. The slide and transcript links are **Google Drive** links
into a shared folder, so they only work for someone that folder is shared with.
`--no-links` on either side drops the block entirely, which is the "without the
links" half of what he asked for.
"""

import argparse
import json
import os
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import split_lessons as SPLIT

CONFIG_PATH = Path(os.environ.get("KCL_STUDY_CONFIG", "~/.kcl-study/config.json")).expanduser()

PACK_SUFFIX = ".lesson.html"
LINKS_OPEN = '<script type="application/json" id="lesson-links">'
LINKS_CLOSE = "</script>"
PACK_VERSION = 1

# Which link is usable by whom, which is the thing a recipient needs told.
KEATS_KEYS = ("video", "video_kind", "minutes", "entry")
DRIVE_KEYS = ("slides", "slides_embed", "transcript", "transcript_embed")


class Problem(Exception):
    pass


def load_config(path=CONFIG_PATH):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise Problem("cannot read %s: %s" % (path, exc))


def module_folder(cfg):
    return Path(cfg["notes_dir"]).expanduser().resolve()


def lessons_in(folder):
    return sorted(p for p in SPLIT.lessons_in(folder)
                  if ".bak" not in p.name and not p.name.endswith(PACK_SUFFIX))


def lesson_for(folder, doc_id):
    hits = [p for p in lessons_in(folder) if p.name.startswith(doc_id + "-")]
    if not hits:
        raise Problem("no lesson for %s in %s" % (doc_id, folder))
    return hits[0]


def materials_of(folder, doc_id):
    try:
        docs = json.loads((folder / "materials.json").read_text(encoding="utf-8")).get("docs", {})
    except (OSError, ValueError):
        return {}
    entry = docs.get(doc_id)
    return dict(entry) if isinstance(entry, dict) else {}


def read_links(text):
    i = text.find(LINKS_OPEN)
    if i < 0:
        return None
    j = text.find(LINKS_CLOSE, i)
    if j < 0:
        return None
    try:
        return json.loads(text[i + len(LINKS_OPEN):j])
    except ValueError:
        return None


def strip_links(text):
    i = text.find(LINKS_OPEN)
    if i < 0:
        return text
    j = text.find(LINKS_CLOSE, i)
    if j < 0:
        return text
    end = j + len(LINKS_CLOSE)
    if text[end:end + 1] == "\n":
        end += 1
    return text[:i] + text[end:]


def make_pack(content, links):
    """The pack: the content file with its links block after the meta block."""
    body = strip_links(content)
    if links is None:
        return body
    marker = SPLIT.META_CLOSE
    i = body.find(marker)
    if i < 0:
        raise Problem("this lesson has no lesson-meta block, so it is not a "
                      "content file; run split_lessons.py on it first")
    at = i + len(marker)
    block = ("\n" + LINKS_OPEN + "\n"
             + json.dumps(links, indent=2, ensure_ascii=False) + "\n" + LINKS_CLOSE)
    return body[:at] + block + body[at:]


# --------------------------------------------------------------------------

def export(cfg, doc_ids, out_dir, with_links=True, quiet=False):
    folder = module_folder(cfg)
    out_dir = Path(out_dir).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    written, drive_linked = [], 0
    for doc in doc_ids:
        path = lesson_for(folder, doc)
        content = path.read_text(encoding="utf-8")
        if not SPLIT.is_content_file(content):
            raise Problem("%s still carries the old stamped reader; run "
                          "split_lessons.py --split first" % path.name)
        links = None
        if with_links:
            mats = materials_of(folder, doc)
            if mats:
                links = {
                    "pack": PACK_VERSION,
                    "doc": doc,
                    "from": {"module": cfg.get("module") or folder.name,
                             "class": cfg.get("class_name") or ""},
                    "made": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "keats": {k: mats[k] for k in KEATS_KEYS if k in mats},
                    "drive": {k: mats[k] for k in DRIVE_KEYS if k in mats},
                }
                if links["drive"]:
                    drive_linked += 1
        target = out_dir / (path.stem + PACK_SUFFIX)
        target.write_text(make_pack(content, links), encoding="utf-8")
        written.append(target)
        if not quiet:
            print("  %-58s %6d bytes%s"
                  % (target.name[:58], target.stat().st_size,
                     "" if links else "   (no links)"))
    if not quiet:
        print("\n%d pack%s in %s" % (len(written), "" if len(written) == 1 else "s", out_dir))
        if drive_linked:
            print("🔴 %d of them carry Google Drive links to the slides and transcripts.\n"
                  "   Those work only for someone the Drive folder is shared with. The KEATS\n"
                  "   lecture link and the video work for anyone enrolled on the module.\n"
                  "   Use --no-links to share the lesson with no addresses at all."
                  % drive_linked)
        # 🔴 Said at export, because it is invisible until the recipient clicks.
        # A lesson cross-links to its siblings ("Part 2", "see W2-T3-P5"), and
        # a link to a lesson that was not shared resolves to nothing on their
        # machine. Found by installing the kit on a clean account and importing
        # a single lesson out of a topic (plan 02 §9).
        missing = dangling_xrefs(folder, [p.stem.replace(PACK_SUFFIX, "")
                                          for p in written], doc_ids)
        if missing:
            one = len(missing) == 1
            print("🔴 %d cross-reference%s in these lessons %s at %s you are\n"
                  "   NOT sharing, so it will not resolve for the recipient:\n"
                  "     %s\n"
                  "   Export %s too, or tell them which parts are missing."
                  % (len(missing), "" if one else "s",
                     "points" if one else "point",
                     "a lesson" if one else "lessons",
                     "\n     ".join(sorted(missing)[:8]),
                     "it" if one else "those"))
    return written


COURSE_PACK_NAME = "%s.course.json"


def course_sidecars(cfg, folder):
    """The course's own writing beyond the lessons: glossary, readings
    summaries, mistakes ledger. All three are OUR text (EH's or a build's),
    which is what makes them shareable where the downloaded materials never
    are. Returns the course-pack dict, or None when there is nothing to carry.

    🔴 Readings lose their `file` fields on the way out: the field points at a
    PDF on THIS machine, under THIS enrolment, which the recipient does not
    have. Without it the reading card falls back to its DOI link, which is the
    correct behaviour on their machine."""
    out = {"course_pack": 1,
           "module": cfg.get("module") or folder.name,
           "name": cfg.get("module_name") or "",
           "made": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    n = 0
    try:
        g = json.loads((folder / "glossary.json").read_text(encoding="utf-8"))
        if isinstance(g, dict) and g:
            out["glossary"] = g
            n += 1
    except (OSError, ValueError):
        pass
    try:
        r = json.loads((folder / "readings.json").read_text(encoding="utf-8"))
        docs = r.get("readings") if isinstance(r, dict) else None
        if isinstance(docs, dict) and docs:
            for rec in docs.values():
                if isinstance(rec, dict):
                    rec.pop("file", None)
            out["readings"] = r
            n += 1
    except (OSError, ValueError):
        pass
    try:
        m = json.loads((folder / "mistakes.json").read_text(encoding="utf-8"))
        if isinstance(m, dict) and m.get("mistakes"):
            out["mistakes"] = m
            n += 1
    except (OSError, ValueError):
        pass
    return out if n else None


def export_course(cfg, out_dir, with_links=True, quiet=True):
    """Everything a friend needs, as one folder and one zip: every lesson as a
    pack, plus the course pack (glossary, readings, mistakes). Returns
    (zip_path, report dict)."""
    folder = module_folder(cfg)
    code = cfg.get("module") or folder.name
    out_dir = Path(out_dir).expanduser() / ("%s - Study Hub lessons" % code)
    docs = sorted(SPLIT.read_content(p.read_text(encoding="utf-8"), p.name)[0]
                  .get("doc") or "" for p in lessons_in(folder))
    docs = [d for d in docs if d]
    if not docs:
        raise Problem("this course has no lessons to share")
    written = export(cfg, docs, out_dir, with_links=with_links, quiet=quiet)
    report = {"lessons": len(written), "glossary": 0, "readings": 0,
              "mistakes": 0}
    pack = course_sidecars(cfg, folder)
    if pack:
        (out_dir / (COURSE_PACK_NAME % code)).write_text(
            json.dumps(pack, ensure_ascii=False) + "\n", encoding="utf-8")
        report["glossary"] = len(pack.get("glossary") or {})
        report["readings"] = len((pack.get("readings") or {}).get("readings")
                                 or {})
        report["mistakes"] = len((pack.get("mistakes") or {}).get("mistakes")
                                 or {})
    zip_path = shutil.make_archive(str(out_dir), "zip",
                                   out_dir.parent, out_dir.name)
    # One file to send, not a file and its staging twin. The zip is complete
    # (the roundtrip test imports from it), so the folder is scaffolding.
    shutil.rmtree(out_dir)
    return Path(zip_path), report


def import_course_pack(folder, data, quiet=True):
    """Fold a course pack's glossary, readings and mistakes into this course.

    🔴 Additive on the glossary, same philosophy as merge_links: a term the
    recipient already defined keeps THEIR definition. Readings and mistakes go
    through their own mergers, which already protect hand-edited entries."""
    counts = {"glossary": 0, "readings": 0, "mistakes": 0}
    g = data.get("glossary")
    if isinstance(g, dict) and g:
        path = folder / "glossary.json"
        try:
            have = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(have, dict):
                have = {}
        except (OSError, ValueError):
            have = {}
        added = 0
        for term, rec in g.items():
            if term not in have and isinstance(rec, dict):
                have[term] = rec
                added += 1
        if added:
            if path.exists():
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                shutil.copy2(path, SPLIT.backup_target(
                    path, "glossary.json.%s.bak" % stamp))
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(have, indent=1, ensure_ascii=False,
                                      sort_keys=True) + "\n", encoding="utf-8")
            os.replace(tmp, path)
        counts["glossary"] = added
    r = data.get("readings")
    if isinstance(r, dict) and isinstance(r.get("readings"), dict):
        import readings as R
        try:
            records, _ = R.parse(json.dumps(r))
            got = R.merge(folder, records)
            counts["readings"] = got.get("added", 0) + got.get("replaced", 0)
        except (R.Problem, ValueError):
            pass
    m = data.get("mistakes")
    if isinstance(m, dict) and isinstance(m.get("mistakes"), dict):
        import mistakes as M
        try:
            records, _ = M.parse(json.dumps(m))
            got = M.merge(folder, records)
            counts["mistakes"] = got.get("added", 0) + got.get("replaced", 0)
        except (M.Problem, ValueError):
            pass
    return counts


def dangling_xrefs(folder, exported_files, exported_docs):
    """Links in the exported lessons pointing at lessons not being exported.

    Only href targets that are themselves lessons in this course count: an
    external link, an anchor within the page, and a materials URL are all
    fine."""
    here = {p.name for p in lessons_in(folder)}
    going = set()
    for doc in exported_docs:
        try:
            going.add(lesson_for(folder, doc).name)
        except Problem:
            continue
    out = set()
    for doc in exported_docs:
        try:
            text = lesson_for(folder, doc).read_text(encoding="utf-8")
        except (Problem, OSError):
            continue
        for href in re.findall(r'href="([^"#?]+\.html)[^"]*"', text):
            target = href.rsplit("/", 1)[-1]
            if target in here and target not in going:
                out.add(target)
    return out


def import_packs(cfg, sources, with_links=True, force=False, quiet=False):
    folder = module_folder(cfg)
    files = []
    for src in sources:
        p = Path(src).expanduser()
        if p.is_dir():
            files.extend(sorted(p.glob("*" + PACK_SUFFIX)))
            # Bare lesson files in the same folder, recognised by carrying a
            # lesson-meta block rather than by being named the way this module
            # names things. A sender whose course is `L01-…` was otherwise told
            # the folder held no packs.
            files.extend(SPLIT.lessons_in(p))
        elif p.is_file():
            files.append(p)
        else:
            raise Problem("nothing at %s" % p)
    if not files:
        raise Problem("no lesson packs found in %s"
                      % ", ".join(str(Path(s).expanduser()) for s in sources))

    staged, skipped, links_seen = [], [], {}
    for f in files:
        text = f.read_text(encoding="utf-8")
        try:
            meta, _, _ = SPLIT.read_content(text, f.name)
        except SPLIT.Problem as exc:
            raise Problem("%s is not a lesson pack: %s" % (f.name, exc))
        doc = meta.get("doc")
        links = read_links(text)
        name = f.name[:-len(PACK_SUFFIX)] + ".html" if f.name.endswith(PACK_SUFFIX) else f.name
        target = folder / name
        if target.exists() and not force:
            skipped.append((f.name, "a lesson is already at %s" % name))
            continue
        staged.append((f, target, doc, strip_links(text) if not with_links else strip_links(text)))
        if with_links and links:
            links_seen[doc] = links

    for f, target, doc, body in staged:
        if target.exists():
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            shutil.copy2(target, SPLIT.backup_target(
                target, "%s.%s.bak" % (target.name, stamp)))
        target.write_text(body, encoding="utf-8")
        if not quiet:
            print("  imported %-50s as %s" % (f.name[:50], target.name))

    merged = merge_links(folder, links_seen) if links_seen else 0
    if not quiet:
        for name, why in skipped:
            print("  skipped  %-50s %s" % (name[:50], why))
        print("\n%d lesson%s imported into %s"
              % (len(staged), "" if len(staged) == 1 else "s", folder))
        if merged:
            print("%d set%s of links merged into materials.json, so Materials works "
                  "without a KEATS scan." % (merged, "" if merged == 1 else "s"))
        elif not with_links:
            print("Links were dropped (--no-links): the notes read, and Materials has "
                  "nothing to point at.")
        if skipped:
            print("Nothing was overwritten. Re-run with --force to replace what is there.")
    return staged, skipped, merged


def merge_links(folder, links_by_doc):
    """Fold a pack's links into this module's materials.json.

    🔴 Additive, and it never overwrites an entry that is already there: a
    recipient who has scanned KEATS themselves has better data than a pack made
    on someone else's machine, and a shared lesson must not quietly replace it."""
    path = folder / "materials.json"
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        doc = {}
    if not isinstance(doc, dict):
        doc = {}
    docs = doc.get("docs")
    if not isinstance(docs, dict):
        docs = {}
    added = 0
    for did, links in links_by_doc.items():
        if did in docs and docs[did]:
            continue
        entry = {}
        entry.update(links.get("keats") or {})
        entry.update(links.get("drive") or {})
        if entry:
            docs[did] = entry
            added += 1
    if not added:
        return 0
    doc["docs"] = docs
    doc.setdefault("built", "")
    doc["merged"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if path.exists():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.copy2(path, SPLIT.backup_target(
            path, "%s.%s.bak" % (path.name, stamp)))
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return added


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--config", default=str(CONFIG_PATH))
    ap.add_argument("--module", default="", help="which module (default: the configured one)")
    ap.add_argument("--export", nargs="+", metavar="DOC", help="doc ids to export")
    ap.add_argument("--export-all", action="store_true")
    ap.add_argument("--to", default="", help="where the packs go")
    ap.add_argument("--import", dest="imp", nargs="+", metavar="PATH",
                    help="a pack, or a folder of them")
    ap.add_argument("--no-links", action="store_true", help="no KEATS or Drive addresses")
    ap.add_argument("--force", action="store_true", help="replace lessons that are already there")
    args = ap.parse_args()

    try:
        raw = load_config(args.config)
        cfg = dict(raw)
        cfg["notes_dir"] = raw["notes_dir"]
        if args.module:
            root = str(raw.get("courses_dir") or "").strip()
            if not root:
                raise Problem("--module needs a courses_dir in the config")
            cfg["notes_dir"] = str(Path(root).expanduser() / args.module)
            cfg["module"] = args.module
            folder = Path(cfg["notes_dir"])
            if not folder.is_dir():
                # 🔴 An import into a course that does not exist yet CREATES it,
                # and this is the kit's whole first-run path (plan 02 §9): a
                # recipient installs, is handed a pack, and has nowhere to put
                # it. Refusing here made the first thing they ever do fail with
                # a message about a folder they were never told to make.
                #
                # Only on --import. Exporting from a course that is not there is
                # a mistake with nothing to create.
                if not args.imp:
                    raise Problem("no module folder at %s" % folder)
                # 🔴 The server owns what a new course IS, so the home page's
                # "Add a course" and this cannot disagree about `store_prefix`.
                import study_server as S
                try:
                    S.create_module(root, args.module)
                except ValueError as exc:
                    raise Problem(str(exc))
                # Said loudly, because a typo would otherwise create a second
                # course silently and the lessons would vanish into it.
                print("Created a new course: %s" % args.module)
                siblings = sorted(p.name for p in Path(root).expanduser().iterdir()
                                  if p.is_dir() and not p.name.startswith((".", "_")))
                if len(siblings) > 1:
                    print("  Courses on this machine now: %s"
                          % ", ".join(siblings))
                    print("  If that was a typo, delete %s and run it again."
                          % folder)

        if args.export or args.export_all:
            if not args.to:
                raise Problem("--to <folder> says where the packs go")
            docs = args.export or [p.name.split("-")[0] + "-" + p.name.split("-")[1]
                                   + "-" + p.name.split("-")[2]
                                   for p in lessons_in(module_folder(cfg))]
            export(cfg, docs, args.to, with_links=not args.no_links)
        elif args.imp:
            import_packs(cfg, args.imp, with_links=not args.no_links, force=args.force)
        else:
            ap.print_help()
    except Problem as exc:
        sys.exit("\n%s\n" % exc)


if __name__ == "__main__":
    main()
