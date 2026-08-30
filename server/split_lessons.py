#!/usr/bin/env python3
"""Split a stamped lesson into CONTENT and READER, and prove the split lossless.

EH's decision, 2026-08-17: "the content lives in a file, and the actual
system that displays it is something that's like the server and the display".

Today every lesson file carries three things welded together: the note itself
(its <title>, its CSS, its `.wrap`), the highlight engine, and the local layer
stamped in by apply_layer.py. Twenty-nine copies of one reader, which is why
every reader change has to rewrite twenty-nine files and leave twenty-nine
backups.

After this, a lesson file is CONTENT ONLY and the server wraps it in one shell.

    python3 server/split_lessons.py --check              # prove it, write nothing
    python3 server/split_lessons.py --build-shell        # write server/reader/shell.html
    python3 server/split_lessons.py --split [--dir D]    # rewrite lessons as content

🔴 The safety argument is byte equality, not inspection, and `--check` proves
one of two things depending on when it runs.

BEFORE the migration, with stamped lessons present, it tears each one into
(content, per-lesson facts), composes it back through the shell, and demands the
whole page back BYTE FOR BYTE. That is what said the DOM was unchanged, so block
numbering was unchanged, so every mark still resolved.

AFTER, the reader is expected to move on, so a whole-page comparison would fail
the first time the layer legitimately changed, and a check that has to be
switched off is worse than no check. So it compares the NOTE instead: the title
and everything from the note's CSS to the end of `.wrap`, byte for byte, against
the stamped copy kept beside it. That stays true forever, and the composition
side is covered independently by `verify_notes.py`, which numbers blocks on the
page the server would send and on the file on disk and demands they agree.

Nothing is written unless every file passes.

The per-lesson facts (DOC_ID, the title, and the VAULT block the vault writer
needs) move out of the engine's source and into a JSON block in the content
file, which is also what R48's lesson packs will read.
"""

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
import pathlib

def default_module_dir(repo):
    """Where the lessons live, wherever they have been moved to.
    🔴 The folder was `notes/` until 2026-08-16 and is `courses/<MODULE>/` after
    it. Sixteen scripts had the old path written into them, which is exactly the
    kind of thing that turns a two-minute move into an afternoon. This asks the
    machine config first (the server's own answer), then looks for a courses
    root, then falls back to the old name so an untouched checkout still works.
    """
    import json, os
    cfg_path = pathlib.Path(os.environ.get("KCL_STUDY_CONFIG",
                                           "~/.kcl-study/config.json")).expanduser()
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        d = pathlib.Path(cfg["notes_dir"]).expanduser()
        if d.is_dir():
            return d
    except Exception:
        pass
    courses = repo / "courses"
    if courses.is_dir():
        mods = sorted(p for p in courses.iterdir()
                      if p.is_dir() and not p.name.startswith("."))
        if mods:
            return mods[0]
    return repo / "notes"

NOTES = default_module_dir(REPO)
LAYER = Path(__file__).resolve().parent / "local-layer.html"
SHELL = Path(__file__).resolve().parent / "reader" / "shell.html"

HEAD_START = "<!-- study-head:start -->"
HEAD_END = "<!-- study-head:end -->"
ENGINE_MARK = "<!-- ================= highlight layer ================= -->"
LAYER_START = "<!-- study-local-layer:start -->"
LAYER_END = "<!-- study-local-layer:end -->"
CONTENT_MARK = "<!-- study-lesson:v1 -->"
META_OPEN = '<script type="application/json" id="lesson-meta">'
META_CLOSE = "</script>"

# --- where dated backups go -----------------------------------------------------
# EH, 2026-08-22: the .bak files beside lessons, glossaries and sidecars were
# clogging the course folders (2,463 of them in one course). Every dated backup a
# course accumulates now lives in a backups/ folder beside the file it copies.
# One helper, used by the server and every script, so no writer can drift back.
BACKUP_DIRNAME = "backups"


def backup_target(path, name):
    """The destination for a dated copy of *path*: backups/<name> beside it.

    Makes the folder. The caller still writes the file, because callers differ
    (some copy, some rename, one writes JSON it already holds)."""
    path = Path(path)
    d = path.parent / BACKUP_DIRNAME
    d.mkdir(exist_ok=True)
    return d / name


# The tokens the shell carries in place of what varies per lesson. Deliberately
# ugly so that a stray one in an output file is impossible to miss.
T_TITLE = "@@TITLE@@"
T_BODY = "@@BODY@@"
T_LAYER = "@@LAYER@@"
T_STORE_PREFIX = "@@STORE_PREFIX@@"
T_DOC_ID = "@@DOC_ID@@"
T_DOC_TITLE = "@@DOC_TITLE@@"
# 🔴 The origin the server is composing this page FOR, from the request's own
# Host. It is what tells the layer it is being served rather than looked at:
# see the guard in local-layer.html. Empty when nothing is serving (the
# verifier composes pages only to parse them).
T_SERVED_FROM = "@@SERVED_FROM@@"
# The course's own NAME, so the header can say "Mood and Neuroscience" where it
# used to print the enrolment code. A settings-level per-course fact, which the
# architecture rule allows, stamped rather than looked up by the page.
T_COURSE_NAME = "@@COURSE_NAME@@"
# 🔴 The neighbouring lessons, as JSON, so a reader with no token can still move
# through the module. Prev/next is otherwise served by `/api/materials`, and
# every `/api/` path is token-gated, so on an unpaired device both nav strips
# stayed hidden. `b868813` made the token prompt dismissable and labelled its
# own control "Dismiss, and read without it", at which point "read without it"
# meant "read this one page". Nav is not secret: it is two titles and two links
# to lessons this server already serves unauthenticated to anyone who asks.
T_LESSON_NAV = "@@LESSON_NAV@@"
# This lesson's own stars and bulbs, so the header controls can show what is
# already set without a round trip. Composed per request, so it cannot go stale.
T_LESSON_STATE = "@@LESSON_STATE@@"
T_VAULT = {
    "cls": "@@VAULT_CLS@@",
    "week": "@@VAULT_WEEK@@",
    "topicNo": "@@VAULT_TOPICNO@@",
    "weekTitle": "@@VAULT_WEEKTITLE@@",
    "topic": "@@VAULT_TOPIC@@",
    "part": "@@VAULT_PART@@",
    "keats": "@@VAULT_KEATS@@",
}

# 🔴 The prefix is per MODULE, not per lesson (plan 02 §10a): two modules on one
# origin would otherwise share marks for W1-T1-P1. This module keeps the prefix
# it has always had, so nothing in his browser has to be migrated.
DEFAULT_STORE_PREFIX = "kcl-affective-highlights:"

VAULT_KEYS = ("cls", "week", "topicNo", "weekTitle", "topic", "part", "keats")

DOC_ID_RE = re.compile(r"var DOC_ID\s*=\s*'((?:[^'\\]|\\.)*)';")
DOC_TITLE_RE = re.compile(r"var DOC_TITLE\s*=\s*'((?:[^'\\]|\\.)*)';")
STORE_KEY_RE = re.compile(r"var STORE_KEY\s*=\s*'((?:[^'\\]|\\.)*)'\s*\+\s*DOC_ID;")
VAULT_RE = re.compile(r"(var VAULT = \{\n)(.*?)(\n  \};)", re.S)
VAULT_LINE_RE = re.compile(r"^(\s*)(\w+):(\s*)'((?:[^'\\]|\\.)*)'(,?)$")


def js_unescape(raw):
    """The value as JavaScript sees it, from the source between the quotes."""
    return re.sub(r"\\(.)", lambda m: {"n": "\n", "t": "\t"}.get(m.group(1), m.group(1)), raw)


def js_escape(value):
    return value.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n")


class Problem(Exception):
    pass


# --------------------------------------------------------------------------
# tearing a stamped lesson apart
# --------------------------------------------------------------------------

def parse_stamped(text, name="<lesson>"):
    """Split one stamped lesson into its five parts plus the facts about it."""
    for needle in (HEAD_START, HEAD_END, ENGINE_MARK, LAYER_START, LAYER_END):
        if text.count(needle) != 1:
            raise Problem("%s: expected exactly one %s, found %d"
                          % (name, needle, text.count(needle)))

    i_head = text.find(HEAD_END) + len(HEAD_END)
    i_title = text.find("<title>")
    i_title_end = text.find("</title>") + len("</title>")
    i_engine = text.find(ENGINE_MARK)
    i_layer = text.find(LAYER_START)
    i_layer_end = text.find(LAYER_END) + len(LAYER_END)

    if not (i_head < i_title < i_title_end < i_engine < i_layer < i_layer_end):
        raise Problem("%s: the file's parts are not in the expected order" % name)
    if '<div class="wrap">' not in text[i_title_end:i_engine]:
        raise Problem("%s: no .wrap between the title and the highlight layer" % name)

    parts = {
        "head": text[:i_head],
        "title": text[i_title:i_title_end],
        # Everything between the title and the engine: a blank line, the note's
        # own <style>, and the .wrap. Kept verbatim, whitespace included.
        "body": text[i_title_end:i_engine],
        "engine": text[i_engine:i_layer],
        "layer": text[i_layer:i_layer_end],
        "tail": text[i_layer_end:],
    }

    engine = parts["engine"]
    m_doc = DOC_ID_RE.search(engine)
    m_title = DOC_TITLE_RE.search(engine)
    m_store = STORE_KEY_RE.search(engine)
    m_vault = VAULT_RE.search(engine)
    if not (m_doc and m_title and m_store and m_vault):
        raise Problem("%s: the engine does not carry the constants this expects "
                      "(DOC_ID %s, DOC_TITLE %s, STORE_KEY %s, VAULT %s)"
                      % (name, bool(m_doc), bool(m_title), bool(m_store), bool(m_vault)))

    vault = {}
    for line in m_vault.group(2).split("\n"):
        lm = VAULT_LINE_RE.match(line)
        if not lm:
            raise Problem("%s: cannot read a VAULT line: %r" % (name, line))
        vault[lm.group(2)] = js_unescape(lm.group(4))
    missing = [k for k in VAULT_KEYS if k not in vault]
    if missing or len(vault) != len(VAULT_KEYS):
        raise Problem("%s: VAULT keys are %s, expected %s"
                      % (name, sorted(vault), sorted(VAULT_KEYS)))

    meta = {
        "doc": js_unescape(m_doc.group(1)),
        "title": js_unescape(m_title.group(1)),
    }
    meta.update({k: vault[k] for k in VAULT_KEYS if k != "cls"})
    parts["meta"] = meta
    parts["cls"] = vault["cls"]
    parts["store_prefix"] = js_unescape(m_store.group(1))
    return parts


def tokenise_engine(parts, name="<lesson>"):
    """The engine with every per-lesson fact replaced by its token."""
    engine = parts["engine"]
    engine = DOC_ID_RE.sub("var DOC_ID    = '%s';" % T_DOC_ID, engine, count=1)
    engine = DOC_TITLE_RE.sub("var DOC_TITLE = '%s';" % T_DOC_TITLE, engine, count=1)
    engine = STORE_KEY_RE.sub("var STORE_KEY = '%s' + DOC_ID;" % T_STORE_PREFIX,
                              engine, count=1)

    m_vault = VAULT_RE.search(engine)
    lines = []
    for line in m_vault.group(2).split("\n"):
        lm = VAULT_LINE_RE.match(line)
        lines.append("%s%s:%s'%s'%s"
                     % (lm.group(1), lm.group(2), lm.group(3), T_VAULT[lm.group(2)],
                        lm.group(5)))
    engine = engine[:m_vault.start(2)] + "\n".join(lines) + engine[m_vault.end(2):]

    for token in [T_DOC_ID, T_DOC_TITLE, T_STORE_PREFIX] + list(T_VAULT.values()):
        if engine.count(token) != 1:
            raise Problem("%s: token %s appears %d times in the engine, expected 1"
                          % (name, token, engine.count(token)))
    return engine


# --------------------------------------------------------------------------
# the shell, and composing a page out of it
# --------------------------------------------------------------------------

def build_shell(lessons):
    """One shell, proven to be the engine every lesson already carries.

    Every lesson is tokenised and the results compared. They must all be the same
    string: if two lessons' engines differ by anything other than their own facts,
    that difference is drift, and silently adopting one of them would change the
    other's behaviour without saying so.
    """
    seen = {}
    for path in lessons:
        parts = parse_stamped(path.read_text(encoding="utf-8"), path.name)
        seen.setdefault(tokenise_engine(parts, path.name), []).append(path.name)
    if len(seen) != 1:
        report = "\n".join("  variant %d: %d files, first %s" % (i + 1, len(v), v[0])
                           for i, (_, v) in enumerate(sorted(seen.items(),
                                                             key=lambda kv: -len(kv[1]))))
        raise Problem("the lessons do not all carry the same engine:\n" + report)

    engine = next(iter(seen))
    head = HEAD_START + "\n" + parse_stamped(
        lessons[0].read_text(encoding="utf-8"), lessons[0].name)["head"].split(
            HEAD_START + "\n", 1)[1]
    return head + "\n" + T_TITLE + T_BODY + engine + T_LAYER + "\n"


def render(shell, layer, meta, body, title=None, cls="", store_prefix=DEFAULT_STORE_PREFIX,
           served_from="", course_name="", nav=None, state=None):
    """The page the browser gets: shell, with this lesson's facts in it.

    🔴 `served_from` is the origin this page is being composed FOR, and it is how
    the reader's layer knows it is on a server. It used to work that out by
    looking at the hostname, which cost a night on 2026-08-29: the machine moved
    to its MagicDNS name, the name matched neither loopback nor the Tailscale
    range, and every lesson silently rendered with the layer switched off. The
    server knows the answer at compose time and no longer makes the page guess."""
    title_tag = title if title is not None else "<title>%s</title>" % meta.get("title", "")
    out = shell.replace(T_TITLE, title_tag).replace(T_BODY, body)
    out = out.replace(T_LAYER, layer)
    out = out.replace(T_DOC_ID, js_escape(meta.get("doc", "")))
    out = out.replace(T_DOC_TITLE, js_escape(meta.get("title", "")))
    out = out.replace(T_STORE_PREFIX, js_escape(store_prefix))
    out = out.replace(T_SERVED_FROM, js_escape(served_from))
    out = out.replace(T_COURSE_NAME, js_escape(course_name))
    # 🔴 `<` becomes \u003c BEFORE js_escape, so a lesson title containing
    # "</script>" cannot close the tag it is sitting inside. js_escape covers
    # quotes, backslashes and newlines; it has no reason to know about HTML,
    # and this is the one token whose value is structured rather than a word.
    for tok, val in ((T_LESSON_NAV, nav), (T_LESSON_STATE, state)):
        out = out.replace(tok,
                          js_escape(json.dumps(val or {}).replace("<", "\\u003c")))
    out = out.replace(T_VAULT["cls"], js_escape(cls))
    for key in VAULT_KEYS:
        if key == "cls":
            continue
        out = out.replace(T_VAULT[key], js_escape(str(meta.get(key, ""))))
    # Only the tokens this file defines are looked for. A generic @@WORD@@ scan
    # would also fire on a lesson that happened to print one in its prose, and a
    # lesson must never be able to break its own page.
    left = [t for t in [T_TITLE, T_BODY, T_LAYER, T_DOC_ID, T_DOC_TITLE,
                        T_STORE_PREFIX, T_SERVED_FROM,
                        T_COURSE_NAME, T_LESSON_NAV,
                        T_LESSON_STATE] + list(T_VAULT.values())
            if t in out]
    if left:
        raise Problem("unfilled tokens in the composed page: %s" % sorted(left))
    return out


# --------------------------------------------------------------------------
# the content file
# --------------------------------------------------------------------------

def content_file(parts):
    """What a lesson becomes: its title, its facts, its CSS and its .wrap.

    It still opens on its own in a browser and reads correctly; what it loses by
    itself is the reader (highlights, notes, the panel), which is the point.
    """
    # 🔴 The body is copied verbatim, its leading blank line included, because
    # what follows META_CLOSE is exactly what the composer puts back after the
    # title. Trimming it here is how the first attempt lost a newline in all 29.
    meta = json.dumps(parts["meta"], indent=2, ensure_ascii=False)
    return (CONTENT_MARK + "\n"
            + parts["title"] + "\n"
            + META_OPEN + "\n" + meta + "\n" + META_CLOSE
            + parts["body"])


def read_content(text, name="<lesson>"):
    """(meta, body, title tag) from a content file."""
    i = text.find(META_OPEN)
    if i < 0:
        raise Problem("%s: no lesson-meta block" % name)
    j = text.find(META_CLOSE, i)
    if j < 0:
        raise Problem("%s: the lesson-meta block is not closed" % name)
    try:
        meta = json.loads(text[i + len(META_OPEN):j])
    except ValueError as exc:
        raise Problem("%s: the lesson-meta block is not valid JSON: %s" % (name, exc))
    if not isinstance(meta, dict) or not meta.get("doc"):
        raise Problem("%s: the lesson-meta block has no doc id" % name)

    i_title = text.find("<title>")
    title = (text[i_title:text.find("</title>") + len("</title>")]
             if i_title >= 0 and i_title < i else None)
    body = text[j + len(META_CLOSE):]
    if '<div class="wrap">' not in body:
        raise Problem("%s: no .wrap after the lesson-meta block" % name)
    return meta, body, title


def is_content_file(text):
    return CONTENT_MARK in text[:200] or (
        META_OPEN in text and LAYER_START not in text)


# --------------------------------------------------------------------------
# what the command does
# --------------------------------------------------------------------------

def is_lesson_file(path):
    """Is this file a lesson? Answered by reading it, not by its name.

    🔴 Everything used to glob `W*-T*-P*.html`, which is THIS module's filename
    shape. `DOC_ID_RE` was widened on 2026-08-16 so another course could be read,
    but the globs were not, so a course whose lessons are `L01-…` or
    `unit3-2-…` had every lesson found by the reader and none of them found by
    the home page, the notebook or the packer: zero lessons on the card, an
    empty notebook, and nothing to export (plan 02 §9, R36).

    A lesson is recognised by carrying a `lesson-meta` block, which is the same
    thing `read_content` requires, so there is one answer to "what is a lesson"
    rather than a pattern in fourteen places disagreeing with it."""
    name = path.name
    if not name.endswith(".html"):
        return False
    # A backup, an exported pack, and the hub are all HTML in a module folder.
    if ".bak" in name or ".lesson." in name or name == "index.html":
        return False
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            head = fh.read(4096)
    except OSError:
        return False
    return META_OPEN in head


def lessons_in(folder):
    return sorted(p for p in Path(folder).glob("*.html") if is_lesson_file(p))


def newest_stamped_backup(path):
    """The most recent `<lesson>.html.<stamp>.bak` that still carries the layer.

    🔴 This is what keeps the check runnable after the migration. Before it, the
    stamped lessons were their own reference; after it, nothing in the folder is
    stamped, and a gate that cannot run is not a gate. The backups the split
    itself wrote are the reference, and they are the same files a revert would
    use, so if they ever stopped matching, the revert would be the thing at
    risk."""
    best = None
    for cand in sorted(path.parent.glob(path.stem + ".html.*.bak")):
        try:
            text = cand.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if LAYER_START in text and CONTENT_MARK not in text[:200]:
            best = cand
    return best


def check(folder, verbose=True):
    """Compose every lesson back out of its parts and demand byte equality."""
    lessons = lessons_in(folder)
    if not lessons:
        raise Problem("no lesson files in %s" % folder)
    stamped = [p for p in lessons
               if LAYER_START in p.read_text(encoding="utf-8")]
    if not stamped:
        shell, checked = check_against_backups(lessons, verbose=verbose)
        return shell, checked, "note"

    shell = build_shell(stamped)
    # 🔴 Composed with the layer the FILE carries, not with whatever
    # `local-layer.html` says today. The two are the same at migration time and
    # diverge the moment the reader is next edited, and what this check is for is
    # the SPLIT: that a lesson's own three parts reassemble into a lesson. Using
    # the live layer would turn every later reader change into a false alarm.
    current_layer = LAYER.read_text(encoding="utf-8").rstrip("\n")
    drifted = 0
    bad = []
    for path in stamped:
        original = path.read_text(encoding="utf-8")
        parts = parse_stamped(original, path.name)
        layer = parts["layer"]
        if layer != current_layer:
            drifted += 1
        content = content_file(parts)
        meta, body, title = read_content(content, path.name)
        rebuilt = render(shell, layer, meta, body, title=title,
                         cls=parts["cls"], store_prefix=parts["store_prefix"])
        if rebuilt != original:
            where = next((i for i in range(min(len(rebuilt), len(original)))
                          if rebuilt[i] != original[i]), min(len(rebuilt), len(original)))
            bad.append((path.name, where, original[where:where + 90],
                        rebuilt[where:where + 90]))
        elif verbose:
            print("  ok   %-58s %d blocks of content" % (path.name[:58], body.count("<p")))
    if bad:
        for name, where, was, now in bad:
            print("  FAIL %s: differs at byte %d\n    was: %r\n    now: %r"
                  % (name, where, was, now))
        raise Problem("%d of %d lessons do not compose back to themselves"
                      % (len(bad), len(stamped)))
    if drifted and verbose:
        print("\n  note: %d of these carry a layer that is not the current "
              "local-layer.html.\n  That is expected for old backups; the page "
              "the server sends uses the current one." % drifted)
    return shell, stamped, "page"


def blocks_of(body):
    """The text of every block the reader numbers, which is what a mark anchors
    to. Imported from the server so there is one implementation of the rule."""
    import study_server as S
    return [S.html_to_text(b["inner"]) for b in S.find_blocks(body)]


def check_against_backups(lessons, verbose=True):
    """Post-migration: compare each content file with the stamped page it replaced.

    🔴 The invariant here is NARROWER than the one used before the migration, and
    it has to be. Before, nothing had changed yet, so the whole page could be
    demanded back byte for byte. After, the reader is expected to evolve: the
    first real change to `local-layer.html` would make a whole-page comparison
    fail forever, and a check that has to be switched off is worse than none.

    What must never change is **the note**: the title and everything from the
    note's own CSS to the end of `.wrap`, which is what mark offsets and block
    numbering are computed from. That is compared byte for byte, against the
    stamped copy, and it stays true no matter how much the reader moves.

    The composition side is covered elsewhere and independently:
    `verify_notes.py` numbers blocks on the page the SERVER WOULD SEND and on
    the file ON DISK and demands they agree.
    """
    checked, bad, orphans = [], [], []
    for path in lessons:
        ref = newest_stamped_backup(path)
        if ref is None:
            orphans.append(path.name)
            continue
        was = parse_stamped(ref.read_text(encoding="utf-8"), ref.name)
        meta, body, title = read_content(path.read_text(encoding="utf-8"), path.name)
        # 🔴 Block TEXT, not raw bytes. Corrected 2026-08-16 the first time a
        # legitimate presentational fix (an SVG label that overflowed its
        # viewBox) was about to make this file fail forever. What a mark anchors
        # to is the text of its block; a diagram's coordinates, an attribute or a
        # reflowed line are not that, and a gate that cannot tell the difference
        # gets switched off. A changed word in a paragraph still fails, which is
        # the case that can actually break a mark.
        now_blocks = blocks_of(body)
        was_blocks = blocks_of(was["body"])
        if title != was["title"] or now_blocks != was_blocks:
            which = "title" if title != was["title"] else "text"
            if which == "title":
                bad.append((path.name, which, 0, was["title"], title))
            else:
                i = next((k for k in range(min(len(now_blocks), len(was_blocks)))
                          if now_blocks[k] != was_blocks[k]), min(len(now_blocks), len(was_blocks)))
                bad.append((path.name, "block %d of %d" % (i, len(was_blocks)), i,
                            (was_blocks[i] if i < len(was_blocks) else "<missing>")[:90],
                            (now_blocks[i] if i < len(now_blocks) else "<missing>")[:90]))
        else:
            checked.append(path)
            if verbose:
                print("  ok   %-58s text unchanged since %s"
                      % (path.name[:58], ref.name[-18:-4]))
    for name in orphans:
        print("  ---- %-58s no stamped backup to check against" % name[:58])
    if bad:
        for name, which, where, a, b in bad:
            print("  FAIL %s: %s differs\n    was: %r\n    now: %r"
                  % (name, which, a, b))
        raise Problem("%d lesson(s) no longer carry the note they were split from.\n"
                      "That is either an edit (check `<DOC>-edits.json`, which records\n"
                      "every paragraph rewrite) or damage. Marks in a changed block\n"
                      "will not resolve." % len(bad))
    if not checked:
        raise Problem("nothing could be checked: no stamped backups found beside "
                      "the lessons in that folder")
    return SHELL.read_text(encoding="utf-8") if SHELL.exists() else "", checked


def cmd_check(args):
    shell, checked, mode = check(args.dir)
    # Say which of the two claims was actually proved. Before the migration the
    # whole page is reproduced; after it, the note is, and the reader is free to
    # have moved on since.
    if mode == "page":
        print("\n%d of %d lessons compose back BYTE FOR BYTE, whole page."
              % (len(checked), len(checked)))
    else:
        print("\n%d lessons still carry, word for word, the text they were split from."
              % len(checked))
        print("(Presentation may have moved on; what is checked is every block a "
              "mark can anchor to.)")
    print("shell is %d bytes, layer %d bytes." % (len(shell), LAYER.stat().st_size))


def cmd_build_shell(args):
    shell, stamped, _ = check(args.dir, verbose=False)
    SHELL.parent.mkdir(parents=True, exist_ok=True)
    if SHELL.exists():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        SHELL.with_suffix(".html.%s.bak" % stamp).write_text(
            SHELL.read_text(encoding="utf-8"), encoding="utf-8")
    SHELL.write_text(shell, encoding="utf-8")
    print("Verified against %d lessons, wrote %s (%d bytes)."
          % (len(stamped), SHELL, len(shell)))


def cmd_split(args):
    shell, stamped, _ = check(args.dir, verbose=False)
    if not SHELL.exists():
        raise Problem("no shell at %s; run --build-shell first" % SHELL)
    if SHELL.read_text(encoding="utf-8") != shell:
        raise Problem("the shell on disk is not the one these lessons build. "
                      "Re-run --build-shell, or split a folder that matches it.")

    staged = []
    for path in stamped:
        original = path.read_text(encoding="utf-8")
        parts = parse_stamped(original, path.name)
        staged.append((path, original, content_file(parts)))

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    for path, original, content in staged:
        if args.dry_run:
            print("would split %-58s %d -> %d bytes"
                  % (path.name[:58], len(original), len(content)))
            continue
        path.with_suffix(".html.%s.bak" % stamp).write_text(original, encoding="utf-8")
        path.write_text(content, encoding="utf-8")
        print("split %-58s %d -> %d bytes" % (path.name[:58], len(original), len(content)))
    if not args.dry_run:
        print("\n%d lessons are now content only. Backups: *.html.%s.bak"
              % (len(staged), stamp))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dir", default=str(NOTES), help="folder of lessons (default: notes/)")
    ap.add_argument("--check", action="store_true", help="prove the split lossless, write nothing")
    ap.add_argument("--build-shell", action="store_true", help="write server/reader/shell.html")
    ap.add_argument("--split", action="store_true", help="rewrite lessons as content only")
    ap.add_argument("--dry-run", action="store_true", help="with --split, report only")
    args = ap.parse_args()

    try:
        if args.build_shell:
            cmd_build_shell(args)
        elif args.split:
            cmd_split(args)
        else:
            cmd_check(args)
    except Problem as exc:
        sys.exit("\n%s\n\nNothing was written." % exc)


if __name__ == "__main__":
    main()
