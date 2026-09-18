#!/usr/bin/env python3
"""R48: a lesson as something one student can hand to another.

EH, 2026-08-16: "it would be a good feature for students to be able to export
a lesson and for other students to be able to import one or more lessons from a
directory or a file … you can import a lesson with or without the links … this
way I could share a lesson with someone that they could load onto their machine
without having to run their own scan and download the KEATS module, as long as
they have access to KEATS."

    python3 server/lesson_packs.py --export W3-T3-P4 --to ~/Desktop/share
    python3 server/lesson_packs.py --export-all --to ~/Desktop/share   # one zip: every lesson
                                                    # plus the course pack, like the Share button
    python3 server/lesson_packs.py --import ~/Desktop/share          # a folder, or a .zip
    python3 server/lesson_packs.py --import one.lesson.html --no-links
    python3 server/lesson_packs.py --module PSY101 --export-all --preset captioned --to ~/Desktop/share

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
import material_names

CONFIG_PATH = Path(os.environ.get("KCL_STUDY_CONFIG", "~/.kcl-study/config.json")).expanduser()

PACK_SUFFIX = ".lesson.html"
LINKS_OPEN = '<script type="application/json" id="lesson-links">'
LINKS_CLOSE = "</script>"
PACK_VERSION = 1

# Which link is usable by whom, which is the thing a recipient needs told.
KEATS_KEYS = ("video", "video_kind", "minutes", "entry")
DRIVE_KEYS = ("slides", "slides_embed", "transcript", "transcript_embed")


def count_drive(cfg, doc_ids):
    """How many of these lessons have Drive links that an export is withholding."""
    folder = module_folder(cfg)
    n = 0
    for doc in doc_ids:
        mats = materials_of(folder, doc) or {}
        if any(k in mats for k in DRIVE_KEYS):
            n += 1
    return n


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

def export(cfg, doc_ids, out_dir, with_links=True, with_drive=False, quiet=False):
    """Write one pack per lesson. Returns the files written.

    🔴 `with_drive` defaults to FALSE, and that default is EH's ruling of 2026-08-28:
    "they definitely should not be pulling it from our Google Drive." The `drive` keys are
    addresses on the exporter's OWN Drive mirror, so a recipient either cannot open them or
    is reading files out of somebody else's account; the KEATS keys stay, because they gate
    on the recipient's own enrolment, which is the boundary working. Pass True only for
    machine-to-machine copies between your own installs."""
    folder = module_folder(cfg)
    out_dir = Path(out_dir).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    written, drive_linked, drive_withheld = [], 0, 0
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
                }
                # The key is omitted rather than emptied when it is withheld, so a pack
                # says nothing about a Drive it is not offering. Older readers already
                # read it as `links.get("drive") or {}`, so nothing has to change to
                # accept one.
                drive = {k: mats[k] for k in DRIVE_KEYS if k in mats}
                if drive and with_drive:
                    links["drive"] = drive
                    drive_linked += 1
                elif drive:
                    drive_withheld += 1
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
            print("🔴 %d of them carry Google Drive links to the slides and transcripts,\n"
                  "   because you asked for them with --with-drive. Those are addresses on\n"
                  "   YOUR Drive: they work only for someone that folder is shared with, and\n"
                  "   sharing it hands them your account's files. Meant for copying between\n"
                  "   your own installs, not for sending to a coursemate."
                  % drive_linked)
        elif drive_withheld:
            print("%d of them had Google Drive links to the slides and transcripts, and\n"
                  "   those were NOT included: they are addresses on your own Drive. The\n"
                  "   KEATS lecture link and the video ARE included and work for anyone\n"
                  "   enrolled on the module. The recipient points their own copy at their\n"
                  "   own materials folder. (--with-drive keeps them, for your own installs.)"
                  % drive_withheld)
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

CORE_IDEAS_SUFFIX = "-core-ideas.md"
# The week/topic ids core ideas are keyed on, matched HERE rather than trusted,
# because an imported pack is a stranger's text and these become filenames.
CORE_IDEAS_ID = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,15}(?:-[A-Za-z0-9]{1,16}){0,5}\Z")


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
    # 🔴 Core ideas ride the COURSE pack, not a lesson pack, and the reason is
    # measured: `make_pack` returns one file per LESSON, so a document that
    # belongs to a week has nowhere to sit in that shape. This pack already
    # carries the course-level writing that is ours (glossary, readings,
    # mistakes) and merges additively, which is exactly what these need.
    #
    # ⚠️ Note the asymmetry with OUTLINES, which share the file convention and
    # are deliberately NOT exported by anything: an outline is a working
    # document, a plan for writing the lesson. Core ideas are reader-facing
    # content. Taking the convention without re-asking the export question
    # would have got this wrong in the quiet direction.
    ideas = {}
    for f in sorted(folder.glob("*" + CORE_IDEAS_SUFFIX)):
        unit = f.name[:-len(CORE_IDEAS_SUFFIX)]
        try:
            text = f.read_text(encoding="utf-8")
        except OSError:
            continue
        if unit and CORE_IDEAS_ID.match(unit) and text.strip():
            ideas[unit] = text
    if ideas:
        out["core_ideas"] = ideas
        n += 1
    return out if n else None


def local_materials_root(cfg):
    """Where this machine keeps the downloaded slides and transcripts.

    🔴 Asks `study_server` rather than deriving it. The server already owns this
    answer (`local_materials_dir`), it can be overridden in settings, and a
    second implementation here would disagree with the reader the first time
    somebody moved the folder. Imported lazily, as the rest of this module
    imports it, so the CLI does not pay for the server on every run."""
    try:
        import study_server as S
        return Path(S.local_materials_dir(cfg))
    except Exception:
        # The fallback is the server's own default shape, and it is only
        # reached when the server cannot be imported at all.
        return (Path(cfg["notes_dir"]).expanduser().parent.parent
                / "materials" / str(cfg.get("module") or ""))


def copy_captions(folder, out_dir, docs=None):
    """Fold this course's cues into an export. Returns (lectures, cues)."""
    found = caption_inventory(folder, docs)
    lectures = cues = 0
    for doc, files in sorted(found.items()):
        dest = Path(out_dir) / "captions" / doc
        dest.mkdir(parents=True, exist_ok=True)
        for src in files:
            shutil.copy2(src, dest / src.name)
            if src.suffix == ".vtt":
                cues += cue_count(src)
        lectures += 1
    return lectures, cues


# The word the downloader writes after the part id, per kind: `<DOC> - Slides
# (...).pdf`, `<DOC> - Transcript (...).pdf`. Whether a file IS of a kind is
# `material_names.is_kind`'s question, the same test the reader's pane makes.
# 🔴 Until 2026-09-18 this table held the substring `" - Transcript ("` and the
# copier matched it: a naming convention (the course's own title following the
# word immediately) smuggled into a type test. A `<DOC> - Transcript v2 (...)`
# name was served by the pane and used by the captions and left out of the pack,
# which reported one transcript fewer with no error. Currency is a separate
# question (`material_names.is_superseded`, asked since 2026-09-17), and the
# two are asked one after the other so neither can hide inside the other.
MATERIAL_WORD = {"slides": "Slides", "transcripts": "Transcript"}


def copy_local_materials(cfg, out_dir, kinds, docs=None):
    """Copy the KCL slide and transcript PDFs themselves into an export.

    🔴🔴 THIS IS THE ONE COMPONENT THAT PUTS KCL'S OWN FILES ON SOMEBODY ELSE'S
    DISK, and it is why it is itemised, defaults off, and prints a warning at
    export. This module's founding rule was that the downloaded folder is never
    shared; EH's 2026-09-04 component ruling makes it a switch he can throw
    rather than a thing that cannot exist. Off unless asked for, every time."""
    root = local_materials_root(cfg)
    wanted = [k for k in MATERIAL_KINDS if kinds.get(k)]
    if not wanted or not root.is_dir():
        return {}
    # The parts in the pack, matched whole (`W1-T1-P1` is not `W1-T1-P10`), as
    # the captions copier already matched them; None means every part.
    parts = list(docs) if docs is not None else [None]
    counts = {}
    for kind in wanted:
        word = MATERIAL_WORD[kind]
        dest = Path(out_dir) / "materials" / kind
        n = 0
        for src in sorted(root.iterdir()):
            if not src.is_file() or material_names.is_superseded(src.name):
                continue
            if not any(material_names.is_kind(src.name, word, d) for d in parts):
                continue
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest / src.name)
            n += 1
        if n:
            counts[kind] = n
    return counts


def copy_knowledge_packs(names, out_dir, repo_root=None):
    """Copy named knowledge packs in whole. Returns {name: files}.

    ⚠️ A LIST, because he said "different knowledge packs": the pack records
    WHICH, so two exports a month apart cannot both claim to carry "the
    knowledge packs" and mean different sets."""
    root = Path(repo_root or Path(__file__).resolve().parent.parent) / "knowledge-packs"
    counts = {}
    for name in names:
        src = root / name
        if not src.is_dir():
            raise Problem(
                "no knowledge pack called %r in %s\n  Available: %s"
                % (name, root,
                   ", ".join(sorted(p.name for p in root.iterdir() if p.is_dir()))
                   if root.is_dir() else "(none: no knowledge-packs folder)"))
        dest = Path(out_dir) / "knowledge-packs" / name
        shutil.copytree(src, dest)
        counts[name] = sum(1 for p in dest.rglob("*") if p.is_file())
    return counts


def contents_from_args(args):
    """Turn the CLI flags into a component checklist.

    🔴 The preset is expanded FIRST and the explicit flags are applied over it,
    so a preset can always be overridden by naming a component. The preset's
    NAME does not survive this function: what a pack records is the resulting
    components.

    🔴 `--with-personal` is read here and nowhere else, and it is never taken
    from a preset or carried over from a previous build. It has to be typed."""
    base = {}
    if getattr(args, "no_links", False):
        base["material_links"] = False
    if getattr(args, "preset", ""):
        base = dict(contents_for_preset(args.preset, base))
    if getattr(args, "with_captions", False):
        base["captions"] = True
    kinds = str(getattr(args, "with_materials_local", "") or "").strip()
    if kinds:
        base["materials_local"] = {k: True for k in
                                   [s.strip() for s in kinds.split(",") if s.strip()]}
    if getattr(args, "knowledge_packs", None):
        base["knowledge_packs"] = list(args.knowledge_packs)
    personal = str(getattr(args, "with_personal", "") or "").strip()
    if personal:
        base["personal"] = {k: True for k in
                            [s.strip() for s in personal.split(",") if s.strip()]}
    return normalise_contents(base)


def apply_components(cfg, folder, out_dir, contents, docs=None, report=None):
    """Copy every component beyond the prose that `contents` asks for.

    🔴 ONE implementation, called by `export_course` and by the CLI. Two would
    disagree the first time a component was added to either, and that is exactly
    the change the component model is supposed to make cheap."""
    report = report if report is not None else {}
    for key, zero in (("captions", 0), ("cues", 0),
                      ("materials_local", {}), ("knowledge_packs", {})):
        report.setdefault(key, zero)
    if contents["captions"]:
        report["captions"], report["cues"] = copy_captions(folder, out_dir, docs)
    if any(contents["materials_local"].values()):
        report["materials_local"] = copy_local_materials(
            cfg, out_dir, contents["materials_local"], docs)
    if contents["knowledge_packs"]:
        report["knowledge_packs"] = copy_knowledge_packs(
            contents["knowledge_packs"], out_dir)
    return report


def refuse_personal(contents):
    """🔴 REFUSED RATHER THAN SILENTLY DROPPED.

    The model accepts personal kinds because EH ruled they are possible
    ("in theory they could be"), but nothing on this side can collect them:
    marks, notes, chats and cards live in the reader's own per-device storage,
    not in the course folder. Recording `personal` in a manifest while copying
    nothing would make the pack lie about itself, and **a pack's own claim about
    what it carries is the one thing a recipient cannot check.**"""
    if contents["personal"]:
        raise Problem(
            "personal data (%s) cannot be exported yet: it lives in the "
            "reader's own storage, not in the course folder, and there is no "
            "collector for it here.\n"
            "  The component model accepts it so that building one is a new key "
            "rather than a new tier, but a pack must not claim to carry what it "
            "does not." % ", ".join(sorted(contents["personal"])))


def export_course(cfg, out_dir, with_links=True, with_drive=False, quiet=True,
                  contents=None):
    """Everything a friend needs, as one folder and one zip: every lesson as a
    pack, plus the course pack (glossary, readings, mistakes), plus whatever
    else `contents` asks for. Returns (zip_path, report dict).

    🟢 `contents` is the component checklist of §4 (see `default_contents`). It
    is optional and defaults to today's behaviour, which is what keeps the
    server's own two-argument call working unchanged."""
    folder = module_folder(cfg)
    code = cfg.get("module") or folder.name
    # 🔴 ONE source of truth downstream. `with_links` is the old spelling of
    # `material_links`; when a caller passes components, they win, and when it
    # does not, the old argument is read into the new shape and nothing else in
    # here has to know which way it arrived.
    contents = normalise_contents(
        contents if contents is not None else {"material_links": with_links})
    with_links = contents["material_links"]

    refuse_personal(contents)

    out_dir = Path(out_dir).expanduser() / ("%s - Study Hub lessons" % code)
    docs = sorted(SPLIT.read_content(p.read_text(encoding="utf-8"), p.name)[0]
                  .get("doc") or "" for p in lessons_in(folder))
    docs = [d for d in docs if d]
    if not docs:
        raise Problem("this course has no lessons to share")
    written = export(cfg, docs, out_dir, with_links=with_links,
                     with_drive=with_drive, quiet=quiet)
    report = {"lessons": len(written), "glossary": 0, "readings": 0,
              "mistakes": 0, "core_ideas": 0,
              # What the recipient will NOT be able to open, so the page can say so
              # rather than leaving them to discover an empty Materials pane.
              "drive_withheld": count_drive(cfg, docs) if not with_drive else 0,
              "captions": 0, "cues": 0, "materials_local": {},
              "knowledge_packs": {}}

    apply_components(cfg, folder, out_dir, contents, docs, report)

    pack = course_sidecars(cfg, folder) or {
        "course_pack": 1, "module": code,
        "name": cfg.get("module_name") or "",
        "made": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    # 🔴 The checklist is recorded in EVERY pack, including one carrying only
    # the prose. A recipient should never have to infer what they were given by
    # looking for absences; "captions: false" is an answer and a missing folder
    # is not.
    pack["contents"] = contents
    # ⚠️ Deliberately OUTSIDE `contents`: the Drive addresses are a modifier on
    # material_links, not a component EH named, and inventing a component here
    # would put a key in the checklist that his ruling does not contain.
    pack["material_links_include_drive"] = bool(with_drive and with_links)
    (out_dir / (COURSE_PACK_NAME % code)).write_text(
        json.dumps(pack, ensure_ascii=False) + "\n", encoding="utf-8")
    report["glossary"] = len(pack.get("glossary") or {})
    report["readings"] = len((pack.get("readings") or {}).get("readings") or {})
    report["mistakes"] = len((pack.get("mistakes") or {}).get("mistakes") or {})
    report["core_ideas"] = len(pack.get("core_ideas") or {})
    report["contents"] = contents

    zip_path = shutil.make_archive(str(out_dir), "zip",
                                   out_dir.parent, out_dir.name)
    # One file to send, not a file and its staging twin. The zip is complete
    # (the roundtrip test imports from it), so the folder is scaffolding.
    shutil.rmtree(out_dir)
    if not quiet and report["materials_local"]:
        print("🔴 This pack carries %s: KCL's own files, leaving this machine.\n"
              "   They are not yours to publish, and the recipient should be "
              "told so."
              % ", ".join("%d %s" % (n, k)
                          for k, n in sorted(report["materials_local"].items())))
    return Path(zip_path), report


def import_course_pack(folder, data, quiet=True):
    """Fold a course pack's glossary, readings and mistakes into this course.

    🔴 Additive on the glossary, same philosophy as merge_links: a term the
    recipient already defined keeps THEIR definition. Readings and mistakes go
    through their own mergers, which already protect hand-edited entries."""
    counts = {"glossary": 0, "readings": 0, "mistakes": 0, "core_ideas": 0}
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
    ideas = data.get("core_ideas")
    if isinstance(ideas, dict):
        for unit, text in sorted(ideas.items()):
            # 🔴 Additive, the same philosophy as the glossary above: a unit the
            # recipient has already written keeps THEIR words, and nothing here
            # overwrites or backs up, because it never replaces.
            # 🔴 And the id is matched before it becomes a filename: `..` or a
            # slash in a stranger's pack would otherwise write outside the
            # course folder.
            if (not isinstance(unit, str) or not isinstance(text, str)
                    or not CORE_IDEAS_ID.match(unit) or not text.strip()):
                continue
            path = folder / (unit + CORE_IDEAS_SUFFIX)
            if path.exists():
                continue
            try:
                path.write_text(text, encoding="utf-8")
            except OSError:
                continue
            counts["core_ideas"] += 1
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


def is_zip_file(path):
    """A zip by its first bytes, never by its name: the server recognises a
    dropped one the same way, and a shared course saved as `course.dat` is
    still the course."""
    try:
        with open(path, "rb") as fh:
            return fh.read(4) == b"PK\x03\x04"
    except OSError:
        return False


def unpack_shared_zip(zip_file, into, max_bytes=None):
    """Unpack a shared course's zip into `into`, ready for `import_packs`.
    Returns the relative paths written.

    🔴 The zip is a stranger's, so no member name is trusted as a path. A
    lesson, a course pack or a sidecar lands FLAT under its basename, whatever
    folder the zip put it in; a cue file keeps exactly one level,
    `captions/<DOC>/<file>`, and only when both parts match the shapes this
    file accepts everywhere else (`DOC_ID`, `CAPTION_FILE`). A directory entry,
    a dot-name and an empty basename are skipped. A member over `max_bytes`
    refuses the whole zip, because a lesson is never that big and a zip bomb
    is the thing that is. `zipfile.BadZipFile` is the caller's to catch."""
    import zipfile
    into = Path(into)
    written = []
    with zipfile.ZipFile(zip_file) as z:
        for m in z.infolist():
            parts = [p for p in m.filename.replace("\\", "/").split("/") if p]
            base = parts[-1] if parts else ""
            if m.is_dir() or not base or base.startswith("."):
                continue
            if max_bytes is not None and m.file_size > max_bytes:
                raise Problem("%s inside the zip is too big to be a lesson"
                              % base[:60])
            rel = Path(base)
            if (len(parts) >= 3 and parts[-3] == CAPTIONS_DIRNAME
                    and DOC_ID.match(parts[-2]) and CAPTION_FILE.match(base)):
                rel = Path(CAPTIONS_DIRNAME) / parts[-2] / base
            target = into / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(z.read(m))
            written.append(str(rel))
    return written


def caption_sources(dirs):
    """{doc: [paths]} for every `captions/<DOC>/` folder under the given
    folders: a lecture id by `DOC_ID`, cue files by `CAPTION_FILE`, and at
    least one `.vtt` among them or the folder is not cues. A shared course's
    unpacked zip and a caption pack's own folder both have this layout, so one
    reader serves both."""
    found = {}
    for d in dirs:
        root = Path(d) / CAPTIONS_DIRNAME
        if not root.is_dir():
            continue
        for sub in sorted(root.iterdir()):
            if not sub.is_dir() or not DOC_ID.match(sub.name):
                continue
            files = sorted(p for p in sub.iterdir()
                           if p.is_file() and CAPTION_FILE.match(p.name))
            if any(p.suffix == ".vtt" for p in files):
                found[sub.name] = files
    return found


def course_packs_in(dirs):
    """Every course pack (`*.json` carrying `course_pack`) at the top of the
    given folders, parsed. Recognised by its marker, never by its name."""
    out = []
    for d in dirs:
        for f in sorted(Path(d).glob("*.json")):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(data, dict) and data.get("course_pack"):
                out.append(data)
    return out


def import_captions(folder, found, force=False, quiet=True):
    """Put shared cues into this course at `captions/<DOC>/`. Returns
    (doc ids landed, [(doc, why it was skipped)]).

    🔴 A lecture that already has cues here keeps them unless `force`: a
    recipient who built their own captions has cues matched to THEIR audio,
    and a pack made on somebody else's machine must not quietly replace them.
    The same rule `import_packs` applies to a lesson and `merge_links` to a
    link. With `force`, every file about to be replaced is copied to
    `backups/` beside it first, dated, the way a replaced lesson is."""
    root = Path(folder) / CAPTIONS_DIRNAME
    imported, skipped = [], []
    for doc, files in sorted(found.items()):
        dest = root / doc
        have = dest.is_dir() and any(
            p.suffix == ".vtt" for p in dest.iterdir() if p.is_file())
        if have and not force:
            skipped.append((doc, "this course already has cues for %s" % doc))
            continue
        dest.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        for src in files:
            target = dest / src.name
            if target.exists():
                shutil.copy2(target, SPLIT.backup_target(
                    target, "%s.%s.bak" % (target.name, stamp)))
            shutil.copy2(src, target)
        imported.append(doc)
        if not quiet:
            print("  captions %-50s %d file%s"
                  % (doc[:50], len(files), "" if len(files) == 1 else "s"))
    return imported, skipped


def import_packs(cfg, sources, with_links=True, force=False, quiet=False):
    """Put shared lessons, and whatever rode beside them, into this course.
    Returns (staged, skipped, merged, extras).

    A source is a lesson pack, a bare lesson, a folder of them, or a `.zip`
    the share button made. Beside the lessons, a folder or zip may carry a
    `captions/<DOC>/` tree and a course pack (`<CODE>.course.json`: glossary,
    readings summaries, mistakes ledger), and both land too, through the same
    functions the page's drop target uses. `extras` says what:
    `{"captions": [doc ids], "captions_skipped": [(doc, why)],
      "course": {"glossary": n, "readings": n, "mistakes": n, "core_ideas": n}}`.

    🔴 Until 2026-09-17 this read the lessons and nothing else, so a course
    exported WITH its captions arrived without them and nothing said so: the
    `captions/` folder sat in the zip, the recipient's reader looked in
    `courses/<CODE>/captions/` and found nothing. The reproduction is in the
    queue entry of that date."""
    import contextlib
    import tempfile
    import zipfile
    folder = module_folder(cfg)
    files, dirs = [], []
    extras = {"captions": [], "captions_skipped": [],
              "course": {"glossary": 0, "readings": 0, "mistakes": 0,
                         "core_ideas": 0}}
    with contextlib.ExitStack() as stack:
        for src in sources:
            p = Path(src).expanduser()
            if p.is_dir():
                dirs.append(p)
            elif p.is_file() and is_zip_file(p):
                tmp = Path(stack.enter_context(tempfile.TemporaryDirectory()))
                try:
                    unpack_shared_zip(p, tmp)
                except zipfile.BadZipFile:
                    raise Problem("%s is not a zip this can read" % p.name)
                dirs.append(tmp)
            elif p.is_file():
                files.append(p)
            else:
                raise Problem("nothing at %s" % p)
        for d in dirs:
            files.extend(sorted(d.glob("*" + PACK_SUFFIX)))
            # Bare lesson files in the same folder, recognised by carrying a
            # lesson-meta block rather than by being named the way this module
            # names things. A sender whose course is `L01-…` was otherwise told
            # the folder held no packs.
            files.extend(SPLIT.lessons_in(d))
        captions = caption_sources(dirs)
        course_packs = course_packs_in(dirs)
        if not files and not captions and not course_packs:
            raise Problem("no lesson packs, captions or course pack found in %s"
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
        if captions:
            extras["captions"], extras["captions_skipped"] = import_captions(
                folder, captions, force=force, quiet=quiet)
        for data in course_packs:
            got = import_course_pack(folder, data)
            for k in extras["course"]:
                extras["course"][k] += got.get(k, 0)

    if not quiet:
        for name, why in skipped + extras["captions_skipped"]:
            print("  skipped  %-50s %s" % (name[:50], why))
        print("\n%d lesson%s imported into %s"
              % (len(staged), "" if len(staged) == 1 else "s", folder))
        if merged:
            print("%d set%s of links merged into materials.json, so Materials works "
                  "without a KEATS scan." % (merged, "" if merged == 1 else "s"))
        elif not with_links:
            print("Links were dropped (--no-links): the notes read, and Materials has "
                  "nothing to point at.")
        if extras["captions"]:
            print("Captions for %d lecture%s are in %s."
                  % (len(extras["captions"]),
                     "" if len(extras["captions"]) == 1 else "s",
                     folder / CAPTIONS_DIRNAME))
        c = extras["course"]
        if any(c.values()):
            print("Course pack: %d glossary terms, %d readings, %d mistakes, "
                  "core ideas for %d." % (c["glossary"], c["readings"],
                                          c["mistakes"], c["core_ideas"]))
        if skipped or extras["captions_skipped"]:
            print("Nothing was overwritten. Re-run with --force to replace what is there.")
    return staged, skipped, merged, extras


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



# ==========================================================================
# WHAT A PACK CARRIES: independent components, not sizes
# ==========================================================================
#
# EH, 2026-09-04, rejecting a `bare` / `standard` / `full` ladder that an
# earlier draft of the brief proposed:
#
#   "I don't think it's as simple as bare standard and [full], because the
#    basic is just the lesson prose, but it can also be the lesson prose plus
#    the actual links to the actual lesson files, which include the videos,
#    the slides, and the transcripts. You can also add: links to the local
#    versions of the slides [and] the transcript; actual closed captioning
#    transcripts; different knowledge packs. I may be forgetting some things."
#
# 🔴 **A LADDER CANNOT EXPRESS "prose and captions but not the slides".** On
# three fixed sizes, somebody who wants the cues but not 400MB of slide PDFs
# has to take a tier carrying both or neither. His components are genuinely
# independent, so the data model is too.
#
# ⚠️ **"I may be forgetting some things" is a design instruction.** A new
# content kind must be a NEW KEY in `contents`, never a new tier. If adding
# one means editing a ladder, this model is wrong and should be fixed rather
# than worked around.

# The local-copy kinds, itemised because they are not alike in size and a
# recipient may legitimately want the transcripts and not the slides.
MATERIAL_KINDS = ("slides", "transcripts")

# 🔴 His own reading, and the ONLY component that is itemised for safety
# rather than for convenience. See `normalise_personal`.
PERSONAL_KINDS = ("marks", "notes", "chats", "cards", "bookmarks", "settings",
                  "visits")

CAPTION_PACK_SCHEMA = "1.0.0"
CAPTION_PACKS_DIR = "caption-packs"
# Where a course keeps its cues and where an import puts them:
# `<course>/captions/<DOC>/`, which is where the reader looks for them.
CAPTIONS_DIRNAME = "captions"

# 🔴 MIRRORED, NOT IMPORTED. The sidecar vocabulary belongs to `captions.py`
# (`ENGINE_CTC`, `ENGINE_WHISPER`) and `video_captions.py` (`TRANSCRIPT`,
# `HEARD`), and neither of those ships in the kit; this file does, and a
# recipient's install would fail on the import. `test_lesson_packs_sharing.py`
# pins each string to its owner, so the two ends cannot drift apart silently.
ENGINE_CTC = "ctc"
ENGINE_WHISPER = "whisper"
WORDS_TRANSCRIPT = "transcript"
WORDS_HEARD = "heard"

# A lecture id, matched BEFORE it becomes a folder name: the shape
# `study_server.DOC_ID_RE` accepts, and the same reasoning as CORE_IDEAS_ID
# above. It also keeps a caption rebuild's `<DOC>.<stamp>.bak` folders out of
# an inventory, which until 2026-09-17 shipped a course's backups to a
# recipient as extra lectures.
DOC_ID = CORE_IDEAS_ID
# A cue file's name inside a lecture folder: no separators, no dot-names.
CAPTION_FILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}\.(?:vtt|json)\Z")


def default_contents():
    """What a pack carries when nobody says otherwise.

    🔴 `lesson_prose` is `True` and is not a choice: it is the BASE, and a pack
    with nothing else is EH's "basic" rather than a degenerate one. It is
    recorded anyway so that a reader of `pack.json` never has to know which
    keys were optional in the version that wrote it.

    🔴 Every genuinely optional component defaults to the value that
    UNDER-shares, so that forgetting a flag is never the mistake that sends
    somebody something. `material_links` is the exception and it is deliberate:
    it is the existing default (`--no-links` turns it off), it costs bytes, and
    it resolves only for somebody who already holds KEATS access."""
    return {
        "lesson_prose": True,
        "material_links": True,
        "materials_local": {k: False for k in MATERIAL_KINDS},
        "captions": False,
        "knowledge_packs": [],
        "personal": False,
    }


def normalise_personal(value):
    """`personal` is `False`, or an ITEMISED dict. A bare `True` is refused.

    🔴 EH, 2026-09-04: "highlights, notes, chats, cards, and settings are not
    things that are shared with other people, though I guess in theory they
    could be, though by default they wouldn't be."

    ⚠️ An earlier draft of the brief hardened that into "there is no switch
    that includes them", which was the writer's addition and not his
    instruction: sharing your own notes with a study partner is a legitimate
    thing to want. **What he actually ruled is DEFAULT OFF, not never.**

    🔴 So the protection is not a ban, it is the SHAPE. A bare `True` is
    refused because it is the value that means "whatever this kind of data
    turns out to include", and the set grows: a pack must not be able to carry
    his chats because somebody asked for his highlights. 32 files of exactly
    this data reached git last week through nobody's decision, and the cost of
    that lands on him rather than on whoever built the switch."""
    if value is None or value is False:
        return False
    if value is True:
        raise Problem(
            "personal data cannot be a bare `true`: name the kinds you mean, "
            "as {\"marks\": true, \"notes\": true}. A blanket switch would grow "
            "to cover kinds nobody chose when the next one is added.\n"
            "  Kinds: %s" % ", ".join(PERSONAL_KINDS))
    if not isinstance(value, dict):
        raise Problem("personal must be false or a dict of kinds, not %s"
                      % type(value).__name__)
    on = {}
    for kind, want in value.items():
        if kind not in PERSONAL_KINDS:
            raise Problem("no such personal kind: %r\n  Kinds: %s"
                          % (kind, ", ".join(PERSONAL_KINDS)))
        if want:
            on[kind] = True
    return on or False


def normalise_contents(raw=None):
    """Validate a `contents` dict and fill in what it does not say.

    🔴 An unknown key is REFUSED rather than ignored. A typo that is silently
    dropped fails in the over-sharing direction on one key (`captions` misspelt
    stays off, which is safe) and in the under-sharing direction on another,
    and the builder has no way to tell which they got. Refusing costs one error
    message and removes the whole class."""
    out = default_contents()
    if not raw:
        return out
    if not isinstance(raw, dict):
        raise Problem("contents must be a dict of components")
    unknown = [k for k in raw if k not in out]
    if unknown:
        raise Problem("no such pack component: %s\n  Components: %s"
                      % (", ".join(sorted(unknown)), ", ".join(sorted(out))))

    if "lesson_prose" in raw and not raw["lesson_prose"]:
        raise Problem("lesson_prose is the base of a pack and cannot be turned "
                      "off; a pack without it is not a pack")
    out["material_links"] = bool(raw.get("material_links", out["material_links"]))
    out["captions"] = bool(raw.get("captions", out["captions"]))

    local = raw.get("materials_local")
    if local is not None:
        if local is True or local is False:
            # ⚠️ Refused for the same reason as a bare personal `true`: the
            # kinds differ enormously in size and consequence, and "all of
            # them" is not a thing anybody should be able to say by accident.
            raise Problem(
                "materials_local names its kinds: {\"slides\": true, "
                "\"transcripts\": false}. Kinds: %s" % ", ".join(MATERIAL_KINDS))
        if not isinstance(local, dict):
            raise Problem("materials_local must be a dict of kinds")
        bad = [k for k in local if k not in MATERIAL_KINDS]
        if bad:
            raise Problem("no such material kind: %s\n  Kinds: %s"
                          % (", ".join(sorted(bad)), ", ".join(MATERIAL_KINDS)))
        out["materials_local"] = {k: bool(local.get(k, False))
                                  for k in MATERIAL_KINDS}

    packs = raw.get("knowledge_packs")
    if packs is not None:
        # 🔴 A LIST, NOT A BOOLEAN. He said "different knowledge packs", so the
        # pack has to say WHICH: two people sharing "the knowledge packs" a
        # month apart would otherwise mean different sets and neither could
        # tell.
        if isinstance(packs, bool):
            raise Problem("knowledge_packs names which packs, as a list, not "
                          "true or false")
        if isinstance(packs, str):
            packs = [packs]
        if not isinstance(packs, (list, tuple)):
            raise Problem("knowledge_packs must be a list of pack names")
        out["knowledge_packs"] = sorted({str(p) for p in packs if str(p).strip()})

    out["personal"] = normalise_personal(raw.get("personal"))
    return out


# 🟢 Presets are a SHORTCUT, and this is the whole of their implementation.
#
# ⚠️ They preselect components and are then thrown away: what a pack records is
# the resulting component list, never the preset's name. A recorded preset name
# would become a second vocabulary that has to agree with the components
# forever, and it is the ladder coming back through the door it was shown out
# of. Anybody reading a pack should be able to answer "what is in this" without
# knowing what `standard` meant on the day it was built.
#
# 🔴 NO PRESET SETS `personal`, AND NONE EVER MAY. It is asked for explicitly,
# every time, or it is off.
PRESETS = {
    "prose": {"material_links": False},
    "linked": {"material_links": True},
    "captioned": {"material_links": True, "captions": True},
}


def contents_for_preset(name, base=None):
    """Expand a preset into components. The name does not survive this call."""
    if name not in PRESETS:
        raise Problem("no such preset: %s\n  Presets: %s\n"
                      "  (a preset only preselects components; you can set any "
                      "of them directly instead)"
                      % (name, ", ".join(sorted(PRESETS))))
    merged = dict(base or {})
    merged.update(PRESETS[name])
    return normalise_contents(merged)


# --------------------------------------------------------------------------
# Captions: reading what the pipeline wrote, and packaging it
# --------------------------------------------------------------------------
#
# 🔴 NOTHING HERE GENERATES A CUE, RESOLVES A URL OR OPENS A SOCKET. The
# recording pipeline is `server/video_captions.py` and the aligner is
# `server/captions.py`; this end only ever reads files those two have already
# written. Both sessions working this brief share one machine and therefore one
# IP, so EH's rate instruction ("slowly so that you don't get blocked") is a
# rule about the MACHINE: exactly one session may fetch, and it is not this one.

CUE_ARROW = " --> "


def cue_count(path):
    """Cues in a `.vtt`, counted from the FILE.

    ⚠️ The sidecar records this too, and reading it from there would be
    reading a number somebody else wrote about a file rather than the file.
    They should agree; a pack that disagreed with its own cues would be worth
    knowing about."""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    return sum(1 for line in text.splitlines() if CUE_ARROW in line)


def captions_for(folder, doc):
    """Every cue file a lecture owns, newest naming convention included.

    One folder per lecture named by SOURCE, which is the layout the brief
    settles in §3: `video.vtt` for a plain recording, `soundN.vtt` for each
    clip of the interactive slide player, `captions.json` beside them. This
    reads whatever is there rather than reconstructing the names, so a third
    source added later needs no change here."""
    d = Path(folder) / CAPTIONS_DIRNAME / doc
    if not d.is_dir():
        return []
    return sorted(p for p in d.iterdir()
                  if p.is_file() and CAPTION_FILE.match(p.name))


def caption_inventory(folder, docs=None):
    """{doc: [paths]} for every lecture that has cues. Empty dict if none."""
    root = Path(folder) / CAPTIONS_DIRNAME
    if not root.is_dir():
        return {}
    found = {}
    for d in sorted(p.name for p in root.iterdir() if p.is_dir()):
        if docs is not None and d not in docs:
            continue
        # 🔴 A rebuild leaves `<DOC>.<stamp>.bak` folders beside the real
        # lectures. Measured 2026-09-17 on a course with 64 folders under
        # `captions/`, 43 of them backups: an export carried all 64. The id
        # shape decides, as it does everywhere else a name becomes a path.
        if not DOC_ID.match(d):
            continue
        files = captions_for(folder, d)
        if any(p.suffix == ".vtt" for p in files):
            found[d] = files
    return found


CAPTION_README = """# %(title)s

The closed-caption cues for %(lectures)d lecture%(s)s of %(code)s%(name)s.

%(words)s
## What is here

    captions/<LECTURE>/video.vtt      cues for the plain lecture recording
    captions/<LECTURE>/soundN.vtt     cues for each clip of the interactive slide player
    captions/<LECTURE>/captions.json  what made them, and how well it matched

`.vtt` is WebVTT, which every browser and every video player reads.

## Where it goes

Each lecture's folder belongs at `courses/%(code)s/captions/<LECTURE>/` in the
recipient's own Study Hub, which is where the reader looks for cues. Two ways
to put it there, and both do the copying:

- drop this pack, zipped, onto the course's page in Study Hub, or
- `python3 server/lesson_packs.py --module %(code)s --import <this folder>`

Nothing already there is replaced unless `--force` says so, and what is
replaced is backed up first.

## Who may hold this

This pack may be handed to another person. It carries a named lecturer's
verbatim words, so it is not something to put anywhere public: no repository,
no website, no shared drive that is open by link.

## What is NOT here

No video, no audio, no slides, no transcripts, and nothing anybody wrote while
studying: no highlights, notes, chats, cards or bookmarks.
"""

# The README's second paragraph, chosen by what the sidecars say the WORDS are.
# ⚠️ Said per pack rather than as one fixed claim, because one course on this
# machine holds lectures whose cues are machine-heard (no transcript could be
# placed), and a README that called those "the lecturer's own words" would be
# the pack's own claim about itself being false.
README_WORDS_TRANSCRIPT = """**These are the lecturer's own words.** They were not transcribed by a machine:
they come from the transcript published with each lecture, and a speech model
supplied only the TIMINGS that say when each word is spoken. See
`PROVENANCE.md`.
"""
README_WORDS_SOME_HEARD = """**These are the lecturer's own words** for every lecture but %(n)d: they come from
the transcript published with each lecture, and a speech model supplied only
the TIMINGS that say when each word is spoken. ⚠️ For %(docs)s the cues are what
a speech recogniser HEARD, because no published transcript could be placed
against the recording; a wrong word there is the machine's. `PROVENANCE.md`
says which and how confident the recogniser was.
"""
README_WORDS_ALL_HEARD = """⚠️ **These cues are what a speech recogniser HEARD in each recording**, not the
lecturer's published transcript: none could be placed against the audio. A
wrong word is the machine's. `PROVENANCE.md` records the recogniser's
confidence per lecture.
"""

# PROVENANCE.md is composed from these by `caption_provenance`, from what the
# sidecars actually say. 🔴 It was one constant until 2026-09-17, and the
# constant described the speech-model matcher after plan 13 had replaced it
# with forced alignment: a pack's own account of how it was made is the one
# thing a recipient cannot check, so it has to be read off the files.
PROVENANCE_HEAD = """# Provenance

## Whose words these are

"""
PROVENANCE_WORDS_TRANSCRIPT = """The words are the lecturer's, taken from the transcript published alongside
each lecture. **No speech-recognition output reaches these files as text.**
"""
PROVENANCE_WORDS_SOME_HEARD = """The words are the lecturer's, taken from the transcript published alongside
each lecture, **except for %(n)d lecture%(s)s: %(docs)s.** For those no
published transcript could be placed against the recording, so the cues carry
what a speech recogniser HEARD, with the recogniser's own confidence in the
table below. A wrong word in them is the machine's, not the lecturer's: a
listening aid, not a quotation.
"""
PROVENANCE_WORDS_ALL_HEARD = """The words are what a speech recogniser HEARD in each recording. No published
transcript could be placed against the audio, so a wrong word is the machine's,
not the lecturer's: a listening aid, not a quotation. The recogniser's own
confidence is in the table below.
"""
PROVENANCE_TIMINGS_HEAD = """
## How the timings were made

"""
PROVENANCE_MIXED = """Two engines made the cues in this pack; the table at the end says which for
each lecture.

"""
PROVENANCE_ENGINE_CTC = """%(head)sThe transcript decides WHAT is said and an acoustic model decides WHEN. A
character-level speech model listens to the recording and gives, every 20 ms,
the probability of each letter being spoken. The transcript's letters are
placed on those frames by CTC forced alignment, so every word gets a start
time, an end time and a score for how well the audio there matches it.

Where a passage of the transcript cannot be placed confidently (the recording
skips it, the lecturer departs from the script, the audio is poor) that passage
gets NO cue rather than a guessed one. `transcript_covered` in each
`captions.json` is the fraction of the transcript's words that were placed.
"""
PROVENANCE_ENGINE_WHISPER = """%(head)sA speech model listens to the recording and produces a rough transcript with a
time against each word. That rough transcript is matched against the real one,
and each real word takes the time of the rough word it matched. So the model
decides WHEN, and the published transcript decides WHAT.

Where the match is poor the cues are refused rather than written, which is what
the coverage figure in each `captions.json` records.
"""
PROVENANCE_ENGINE_UNKNOWN = """%(n)d lecture%(s)s (%(docs)s) carr%(y)s cue files with no `captions.json`
beside them, or one that does not name its engine, so this pack cannot say how
their timings were made. Their cues are copied as found.
"""
PROVENANCE_TAIL = """
## What was checked

- Every `.vtt` in this pack was produced by the path above; none was hand-edited.
- `pack.json` counts the cues by reading the `.vtt` files in this pack, not by
  copying a number from the sidecars beside them.

## Coverage, per lecture

%(coverage)s

Coverage is the fraction of the published transcript that could be placed in
time confidently. A lecture below about 0.9 is worth spot-checking against the
recording before relying on its cues. A machine-heard lecture has no coverage
to report: its figure is the recogniser's confidence in what it heard.
"""


def sidecar_facts(side):
    """What a lecture's `captions.json` says about how its cues were made, in
    one shape whichever generation of the pipeline wrote it: `engine`, `words`
    (what the cue text IS), `covered` (the fraction of the transcript placed),
    `confidence` (a recogniser's, for machine-heard words) and `tool`. A
    missing or unreadable sidecar gives every field empty, and the caller says
    so rather than guessing.

    ⚠️ Two generations of sidecar are on disk. The forced-alignment engine
    writes `engine`, `words_are` and `transcript_covered`; the speech-model
    matcher before it wrote no `engine`, named its tool under `timings_from`,
    and recorded its match as `coverage` (older) or `transcript_covered`
    (later). Reading one key and printing "not recorded" for the rest is how a
    pack came to show an empty coverage column for a course whose every
    sidecar carried the number."""
    out = {"engine": "", "words": "", "covered": None, "confidence": None,
           "tool": ""}
    try:
        data = json.loads(Path(side).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return out
    if not isinstance(data, dict):
        return out
    tool = data.get("timings_from")
    tool = str(tool.get("tool") or "") if isinstance(tool, dict) else ""
    out["tool"] = tool
    engine = str(data.get("engine") or "")
    if not engine:
        low = tool.lower()
        if "whisper" in low:
            engine = ENGINE_WHISPER
        elif "forced_align" in low or "ctc" in low:
            engine = ENGINE_CTC
    out["engine"] = engine
    out["words"] = str(data.get("words_are") or WORDS_TRANSCRIPT)
    for key in ("transcript_covered", "coverage"):
        v = data.get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            out["covered"] = float(v)
            break
    v = data.get("heard_confidence")
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        out["confidence"] = float(v)
    return out


def _named(docs):
    return ", ".join("`%s`" % d for d in docs)


def caption_words_paragraph(facts, readme=False):
    """The paragraph saying what the cue TEXT is, for the README (`readme`)
    or PROVENANCE.md, from `{doc: sidecar_facts}`."""
    heard = sorted(d for d, f in facts.items() if f["words"] == WORDS_HEARD)
    if not heard:
        return README_WORDS_TRANSCRIPT if readme else PROVENANCE_WORDS_TRANSCRIPT
    fill = {"n": len(heard), "s": "" if len(heard) == 1 else "s",
            "docs": _named(heard)}
    if len(heard) == len(facts):
        return README_WORDS_ALL_HEARD if readme else PROVENANCE_WORDS_ALL_HEARD
    return (README_WORDS_SOME_HEARD if readme else PROVENANCE_WORDS_SOME_HEARD) % fill


def caption_provenance(facts, files_by_doc=None):
    """PROVENANCE.md for a caption pack, from `{doc: sidecar_facts}`."""
    files_by_doc = files_by_doc or {}
    engines = sorted({f["engine"] for f in facts.values() if f["engine"]})
    unknown = sorted(d for d, f in facts.items() if not f["engine"])
    out = [PROVENANCE_HEAD, caption_words_paragraph(facts), PROVENANCE_TIMINGS_HEAD]
    mixed = len(engines) > 1
    if mixed:
        out.append(PROVENANCE_MIXED)
    for eng in engines:
        if eng == ENGINE_CTC:
            head = "### By forced alignment (`%s`)\n\n" % eng if mixed else ""
            out.append(PROVENANCE_ENGINE_CTC % {"head": head})
        elif eng == ENGINE_WHISPER:
            head = "### By speech-model match (`%s`)\n\n" % eng if mixed else ""
            out.append(PROVENANCE_ENGINE_WHISPER % {"head": head})
        else:
            docs = sorted(d for d, f in facts.items() if f["engine"] == eng)
            out.append("%d lecture%s (%s) name%s an engine this file does not "
                       "know, `%s`; its cues are copied as found.\n"
                       % (len(docs), "" if len(docs) == 1 else "s", _named(docs),
                          "s" if len(docs) == 1 else "", eng))
        if mixed:
            out.append("\n")
    if unknown:
        out.append(PROVENANCE_ENGINE_UNKNOWN % {
            "n": len(unknown), "s": "" if len(unknown) == 1 else "s",
            "docs": _named(unknown), "y": "ies" if len(unknown) == 1 else "y"})
    rows = []
    for doc, f in sorted(facts.items()):
        if f["words"] == WORDS_HEARD:
            conf = ("recogniser confidence %.3f" % f["confidence"]
                    if f["confidence"] is not None else "confidence not recorded")
            rows.append("- `%s` ⚠️ machine-heard words, %s" % (doc, conf))
        elif f["covered"] is not None:
            how = {ENGINE_CTC: "by forced alignment (`%s`)" % ENGINE_CTC,
                   ENGINE_WHISPER: "by the speech-model match (`%s`)" % ENGINE_WHISPER,
                   }.get(f["engine"], "engine not recorded")
            rows.append("- `%s` %.3f of the transcript placed, %s"
                        % (doc, f["covered"], how))
        elif not f["engine"] and not f["tool"]:
            n = len(files_by_doc.get(doc) or [])
            rows.append("- `%s` no sidecar: %d cue file%s, engine not recorded"
                        % (doc, n, "" if n == 1 else "s"))
        else:
            rows.append("- `%s` coverage not recorded (`%s`)"
                        % (doc, f["engine"] or f["tool"] or "unknown"))
    out.append(PROVENANCE_TAIL % {"coverage": "\n".join(rows)})
    return "".join(out)


def caption_pack_manifest(code, lectures, cues, name="", module_code=""):
    """`pack.json` for a caption pack.

    🔴 `contains_lecturer_words` is ALWAYS true here and is never omitted. It is
    the field a future guard reads, and a guard that has to infer intent from a
    folder name is the kind this project keeps having to repair. It is written
    as data for the same reason the distribution posture is: a convention that
    lives only in a README cannot be checked by anything.

    🟢 `code` and `name` are the course's OWN, from its `settings.json` module
    block (EH, 2026-09-17: "relabel the captions pack so it's named correctly
    ... maybe it should be attached to the actual course ID"). No identifier is
    invented: `module` is the folder the course lives in, `code` is what the
    course calls itself, and on this machine the two are the same string."""
    return {
        "schema_version": CAPTION_PACK_SCHEMA,
        "kind": "captions",
        "module": code,
        "code": module_code or code,
        "name": name or "",
        # 🟢 EH, 2026-09-04: "the caption packs should be shareable. That's my
        # final answer." ⚠️ The VALUE changed on that ruling; the need to record
        # it did not, which is why the field survives an answer that made it
        # look unnecessary.
        "distribution": "shareable",
        "contains_lecturer_words": True,
        "contains_kcl_material": True,
        "counts": {"lectures": lectures, "cues": cues},
        "made": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def build_caption_pack(cfg, out_dir, docs=None, quiet=True):
    """Write `caption-packs/<MODULE>-captions/`. Returns (folder, report).

    ⚠️ A caption pack is NOT a knowledge pack, and lives in its own tree
    because of it: a knowledge pack is subject knowledge no course owns, and
    captions belong to exactly one module. It follows the same house shape
    (`pack.json`, `README.md`, `PROVENANCE.md`, one self-contained directory)
    so that anything which learns to read one can read the other."""
    folder = module_folder(cfg)
    code = cfg.get("module") or folder.name
    found = caption_inventory(folder, docs)
    if not found:
        raise Problem(
            "this course has no captions yet, so there is nothing to pack.\n"
            "  Cues are built by `python3 server/video_captions.py --all %s`\n"
            "  for plain recordings, and by `server/captions.py` for the clips\n"
            "  of the interactive slide player." % code)

    out_dir = Path(out_dir).expanduser() / CAPTION_PACKS_DIR / ("%s-captions" % code)
    if out_dir.exists():
        shutil.rmtree(out_dir)
    (out_dir / CAPTIONS_DIRNAME).mkdir(parents=True)

    cues, facts = 0, {}
    for doc, files in sorted(found.items()):
        dest = out_dir / CAPTIONS_DIRNAME / doc
        dest.mkdir()
        for src in files:
            shutil.copy2(src, dest / src.name)
            if src.suffix == ".vtt":
                cues += cue_count(src)
        facts[doc] = sidecar_facts(dest / "captions.json")

    name = cfg.get("module_name") or cfg.get("class_name") or ""
    manifest = caption_pack_manifest(code, len(found), cues, name,
                                     module_code=cfg.get("module_code") or "")
    (out_dir / "pack.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8")

    n = len(found)
    (out_dir / "README.md").write_text(
        CAPTION_README % {"code": code, "lectures": n,
                          "s": "" if n == 1 else "s",
                          "title": ("%s captions: %s" % (code, name)) if name
                          else ("%s captions" % code),
                          "name": (" (%s)" % name) if name else "",
                          "words": caption_words_paragraph(facts, readme=True)},
        encoding="utf-8")
    (out_dir / "PROVENANCE.md").write_text(caption_provenance(facts, found),
                                           encoding="utf-8")

    low = [d for d, f in sorted(facts.items())
           if f["covered"] is not None and f["covered"] < 0.9]
    report = {"lectures": n, "cues": cues, "folder": out_dir, "low": low,
              "heard": [d for d, f in sorted(facts.items())
                        if f["words"] == WORDS_HEARD],
              "engines": sorted({f["engine"] for f in facts.values()})}
    if not quiet:
        print("%d lecture%s, %d cues -> %s"
              % (n, "" if n == 1 else "s", cues, out_dir))
        if report["low"]:
            # 🔴 Said at build time, because a low-coverage lecture looks
            # exactly like a good one from outside the file.
            print("⚠️ %d below 0.9 coverage, worth checking against the "
                  "recording: %s" % (len(report["low"]), ", ".join(report["low"])))
        if report["heard"]:
            print("⚠️ %d with machine-heard words rather than the transcript: %s"
                  % (len(report["heard"]), ", ".join(report["heard"])))
    return out_dir, report


def course_cfg(raw, root, module_id):
    """The server's own view of one course: `notes_dir` at its folder and the
    course's name, code, class and vault link read from its `settings.json`,
    never from the machine config.

    🔴 Until 2026-09-17 `--module` set `notes_dir` and `module` and nothing
    else, so `dict(raw)` handed every course the machine's `class_name`: a
    pack exported for a second course said in its links, and a caption pack
    in its manifest, that it came from the first. The server had already
    fixed exactly this for the share button (`study_server.module_cfg`, the
    "identity is per COURSE" note); the command line simply never called
    it. One resolver now, so the two cannot drift again."""
    import study_server as S
    scfg = dict(raw)
    scfg["courses_dir"] = Path(root).expanduser()
    scfg["notes_dir"] = Path(str(raw.get("notes_dir")
                                 or Path(root).expanduser() / module_id)).expanduser()
    try:
        return S.module_cfg(scfg, module_id)
    except ValueError as exc:
        raise Problem(str(exc))


def print_course_report(zip_path, report):
    """What the share button's page says, for the terminal."""
    r = report
    print("  %d lesson%s" % (r["lessons"], "" if r["lessons"] == 1 else "s"))
    if r["glossary"] or r["readings"] or r["mistakes"] or r["core_ideas"]:
        print("  course pack: %d glossary terms, %d readings, %d mistakes, "
              "core ideas for %d" % (r["glossary"], r["readings"],
                                     r["mistakes"], r["core_ideas"]))
    if r["captions"]:
        print("  captions: %d lecture%s, %d cues"
              % (r["captions"], "" if r["captions"] == 1 else "s", r["cues"]))
    # `drive_withheld` is not repeated here: `export` has already printed the
    # full paragraph about it, once, when it withheld them.
    for kind, n in sorted(r["materials_local"].items()):
        print("🔴 %d %s: KCL's own files, leaving this machine. Not "
              "yours to publish." % (n, kind))
    for name, n in sorted(r["knowledge_packs"].items()):
        print("  knowledge pack %s: %d files" % (name, n))
    print("\n%s" % zip_path)


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
    ap.add_argument("--with-drive", action="store_true",
                    help="also include YOUR Google Drive addresses for the slides and "
                         "transcripts. Off by default (EH, 2026-08-28); for copying "
                         "between your own installs, not for sending to a coursemate")
    ap.add_argument("--force", action="store_true", help="replace lessons that are already there")

    # ---- §4: what the pack carries. Independent components, never a ladder.
    ap.add_argument("--with-captions", action="store_true",
                    help="include this course's caption cues (.vtt). OFF by "
                         "default: forgetting a flag should under-share")
    ap.add_argument("--with-materials-local", default="", metavar="KINDS",
                    help="include the LOCAL slide/transcript PDFs themselves, "
                         "comma separated (%s). These are KCL's files leaving "
                         "your machine" % ",".join(MATERIAL_KINDS))
    ap.add_argument("--with-knowledge-pack", action="append", default=[],
                    metavar="NAME", dest="knowledge_packs",
                    help="include a knowledge pack by name; repeatable")
    ap.add_argument("--with-personal", default="", metavar="KINDS",
                    help="include your own %s. Never set by a preset, never "
                         "remembered, and it must be asked for every time"
                         % "/".join(PERSONAL_KINDS))
    ap.add_argument("--preset", default="", metavar="NAME",
                    help="preselect components (%s). A shortcut only: the pack "
                         "records the components, never the preset name"
                         % ", ".join(sorted(PRESETS)))
    ap.add_argument("--caption-pack", action="store_true",
                    help="build a standalone caption pack for this module "
                         "instead of a lesson export")
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
            cfg = course_cfg(raw, root, args.module)

        if args.caption_pack:
            if not args.to:
                raise Problem("--to <folder> says where the pack goes")
            build_caption_pack(cfg, args.to, quiet=False)
        elif args.export_all:
            if not args.to:
                raise Problem("--to <folder> says where the zip goes")
            # 🔴 The same function the share button calls, so the zip carries
            # the course pack (glossary, readings, mistakes, core ideas) and
            # records its own contents. Until 2026-09-17 this wrote the bare
            # lesson packs, so a course shared from the terminal arrived
            # without its glossary and nothing said so.
            contents = contents_from_args(args)
            zip_path, report = export_course(
                cfg, args.to, with_links=contents["material_links"],
                with_drive=args.with_drive, quiet=False, contents=contents)
            print_course_report(zip_path, report)
        elif args.export:
            if not args.to:
                raise Problem("--to <folder> says where the packs go")
            docs = args.export
            contents = contents_from_args(args)
            refuse_personal(contents)
            export(cfg, docs, args.to, with_links=contents["material_links"],
                   with_drive=args.with_drive)
            # The same copiers `export_course` uses, so a component behaves
            # identically whether it was asked for here or by the reader's
            # own share button.
            extra = apply_components(cfg, module_folder(cfg), args.to,
                                     contents, docs)
            if extra["captions"]:
                print("  captions: %d lecture%s, %d cues"
                      % (extra["captions"], "" if extra["captions"] == 1 else "s",
                         extra["cues"]))
            for kind, n in sorted(extra["materials_local"].items()):
                print("🔴 %d %s: KCL's own files, leaving this machine. Not "
                      "yours to publish." % (n, kind))
            for name, n in sorted(extra["knowledge_packs"].items()):
                print("  knowledge pack %s: %d files" % (name, n))
        elif args.imp:
            import_packs(cfg, args.imp, with_links=not args.no_links, force=args.force)
        else:
            ap.print_help()
    except Problem as exc:
        sys.exit("\n%s\n" % exc)


if __name__ == "__main__":
    main()
