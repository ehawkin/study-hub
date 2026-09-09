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


# The naming convention the downloader writes, and the only thing that says
# which kind a file is. `<DOC> - Slides (...).pdf`, `<DOC> - Transcript (...).pdf`.
MATERIAL_MARKER = {"slides": " - Slides (", "transcripts": " - Transcript ("}


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
    counts = {}
    for kind in wanted:
        mark = MATERIAL_MARKER[kind]
        dest = Path(out_dir) / "materials" / kind
        n = 0
        for src in sorted(root.iterdir()):
            if not src.is_file() or mark not in src.name:
                continue
            if docs is not None and not any(src.name.startswith(d) for d in docs):
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
    settles in §3: `video.vtt` for a plain recording, `soundN.vtt` for the
    narrated package's clips, `captions.json` beside them. This reads whatever
    is there rather than reconstructing the names, so a third source added
    later needs no change here."""
    d = Path(folder) / "captions" / doc
    if not d.is_dir():
        return []
    return sorted(p for p in d.iterdir()
                  if p.is_file() and p.suffix in (".vtt", ".json"))


def caption_inventory(folder, docs=None):
    """{doc: [paths]} for every lecture that has cues. Empty dict if none."""
    root = Path(folder) / "captions"
    if not root.is_dir():
        return {}
    found = {}
    for d in sorted(p.name for p in root.iterdir() if p.is_dir()):
        if docs is not None and d not in docs:
            continue
        files = captions_for(folder, d)
        if any(p.suffix == ".vtt" for p in files):
            found[d] = files
    return found


CAPTION_README = """# %(code)s captions

The closed-caption cues for %(lectures)d lecture%(s)s of %(code)s%(name)s.

**These are the lecturer's own words.** They were not transcribed by a machine:
they come from the transcript published with each lecture, and a speech model
supplied only the TIMINGS that say when each word is spoken. See
`PROVENANCE.md`.

## What is here

    captions/<LECTURE>/video.vtt      cues for the plain lecture recording
    captions/<LECTURE>/soundN.vtt     cues for the narrated package's clips
    captions/<LECTURE>/captions.json  what made them, and how well it matched

`.vtt` is WebVTT, which every browser and every video player reads.

## Who may hold this

This pack may be handed to another person. It carries a named lecturer's
verbatim words, so it is not something to put anywhere public: no repository,
no website, no shared drive that is open by link.

## What is NOT here

No video, no audio, no slides, no transcripts, and nothing anybody wrote while
studying: no highlights, notes, chats, cards or bookmarks.
"""

CAPTION_PROVENANCE = """# Provenance

## Whose words these are

The words are the lecturer's, taken from the transcript published alongside
each lecture. **No speech-recognition output reaches these files as text.**

## How the timings were made

A speech model listens to the recording and produces a rough transcript with a
time against each word. That rough transcript is matched against the real one,
and each real word takes the time of the rough word it matched. So the model
decides WHEN, and the published transcript decides WHAT.

Where the match is poor the cues are refused rather than written, which is what
`coverage` records in each `captions.json`.

## What was checked

- Every `.vtt` in this pack was produced by that path; none was hand-edited.
- `pack.json` counts the cues by reading the `.vtt` files in this pack, not by
  copying a number from the sidecars beside them.

## Coverage, per lecture

%(coverage)s

Coverage is the fraction of the published transcript that could be placed in
time confidently. A lecture below about 0.9 is worth spot-checking against the
recording before relying on its cues.
"""


def caption_pack_manifest(code, lectures, cues, name=""):
    """`pack.json` for a caption pack.

    🔴 `contains_lecturer_words` is ALWAYS true here and is never omitted. It is
    the field a future guard reads, and a guard that has to infer intent from a
    folder name is the kind this project keeps having to repair. It is written
    as data for the same reason the distribution posture is: a convention that
    lives only in a README cannot be checked by anything."""
    return {
        "schema_version": CAPTION_PACK_SCHEMA,
        "kind": "captions",
        "module": code,
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
            "  for plain recordings, and by `server/captions.py` for narrated\n"
            "  packages." % code)

    out_dir = Path(out_dir).expanduser() / CAPTION_PACKS_DIR / ("%s-captions" % code)
    if out_dir.exists():
        shutil.rmtree(out_dir)
    (out_dir / "captions").mkdir(parents=True)

    cues, coverage = 0, []
    for doc, files in sorted(found.items()):
        dest = out_dir / "captions" / doc
        dest.mkdir()
        for src in files:
            shutil.copy2(src, dest / src.name)
            if src.suffix == ".vtt":
                cues += cue_count(src)
        side = dest / "captions.json"
        cov = None
        if side.is_file():
            try:
                cov = json.loads(side.read_text(encoding="utf-8")).get("coverage")
            except (OSError, ValueError):
                cov = None
        coverage.append((doc, cov))

    name = cfg.get("module_name") or cfg.get("class_name") or ""
    manifest = caption_pack_manifest(code, len(found), cues, name)
    (out_dir / "pack.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8")

    n = len(found)
    (out_dir / "README.md").write_text(
        CAPTION_README % {"code": code, "lectures": n,
                          "s": "" if n == 1 else "s",
                          "name": (" (%s)" % name) if name else ""},
        encoding="utf-8")
    rows = "\n".join(
        "- `%s` %s" % (doc, "not recorded" if cov is None else "%.3f" % cov)
        for doc, cov in coverage)
    (out_dir / "PROVENANCE.md").write_text(
        CAPTION_PROVENANCE % {"coverage": rows}, encoding="utf-8")

    report = {"lectures": n, "cues": cues, "folder": out_dir,
              "low": [d for d, c in coverage if c is not None and c < 0.9]}
    if not quiet:
        print("%d lecture%s, %d cues -> %s"
              % (n, "" if n == 1 else "s", cues, out_dir))
        if report["low"]:
            # 🔴 Said at build time, because a low-coverage lecture looks
            # exactly like a good one from outside the file.
            print("⚠️ %d below 0.9 coverage, worth checking against the "
                  "recording: %s" % (len(report["low"]), ", ".join(report["low"])))
    return out_dir, report


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

        if args.caption_pack:
            if not args.to:
                raise Problem("--to <folder> says where the pack goes")
            build_caption_pack(cfg, args.to, quiet=False)
        elif args.export or args.export_all:
            if not args.to:
                raise Problem("--to <folder> says where the packs go")
            docs = args.export or [p.name.split("-")[0] + "-" + p.name.split("-")[1]
                                   + "-" + p.name.split("-")[2]
                                   for p in lessons_in(module_folder(cfg))]
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
