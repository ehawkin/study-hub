#!/usr/bin/env python3
"""Verification sweep over every note.

The block-count check is the one that matters most: the page numbers blocks with
a CSS selector and the server numbers them with an HTMLParser, and an edit is
refused whenever the two disagree. This re-implements the page's rule
independently rather than calling the server's, so agreement means something.
"""
import sys, pathlib, re, json, shutil, os, argparse, html as html_mod
from html.parser import HTMLParser

# 🔴 Derived, never hardcoded. This file ships inside the kit (plan 02 §9) as the
# gate the writing skill runs, so an absolute path into one person's synced
# folder made it unrunnable for everyone else. The script lives in
# <repo>/server/, so the repo is its parent's parent, whatever anybody called
# the folder.
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))
import study_server as S
import split_lessons as SPLIT
from doi_sidecar import DOI_SIDECAR, load_doi_exceptions
import doi_links as DOILINK
import shadow_records as SHADOW


def crossref_agent():
    """Crossref's polite pool wants a contact address, and it must belong to the
    person running the check rather than to whoever wrote the script. Read from
    the server's own config, or the environment, and sent without a mailto when
    neither says who to name."""
    who = str(os.environ.get("KCL_CONTACT_EMAIL") or "").strip()
    if not who:
        try:
            who = str(json.loads(S.CONFIG_PATH.read_text(encoding="utf-8"))
                      .get("contact_email") or "").strip()
        except (OSError, ValueError):
            who = ""
    return "kcl-study/1.0" + (" (mailto:%s)" % who if who else "")

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

# `--notes <dir>` points the sweep at a copy, which is how the content/reader
# split was verified before anything of his was touched. Default is his own.
#
# 🔴 argparse, and NOT a hand-rolled `sys.argv` scan, from a measured wrong
# answer rather than from tidiness. This read `if "--notes" in sys.argv` until
# 2026-09-09, so ANY OTHER FLAG WAS SILENTLY IGNORED: a sweep asked for one
# course, by a flag this script does not have, swept the CONFIGURED course
# instead. It printed 389 DOIs where the right scope holds 213 and raised a
# section-B3 hit naming a file that exists only in the other course, and it
# finished with ALL CHECKS PASS.
# ⚠️ Every number that run printed was TRUE. They were about the wrong subject,
# and a wrong-scope pass is indistinguishable from a right-scope pass. This is
# the one gate whose output everybody else stops checking, so a silent mis-scope
# here retires the suspicion that would otherwise have caught the thing.
# 🟢 argparse refuses an unknown flag for free, and its usage line names the
# flags that do exist, which is the half a refusal owes the person refused.
PARSER = argparse.ArgumentParser(
    prog="verify_notes.py",
    description="Verification sweep over one course's lessons.")
PARSER.add_argument("--notes", metavar="DIR",
                    help="the course directory to sweep; the default is the "
                         "configured course")
PARSER.add_argument("--dois", action="store_true",
                    help="also ask Crossref whether each DOI is the paper it "
                         "is labelled with (needs the network, about a minute)")
OPTS = PARSER.parse_args()

#
# ⚠️ `.resolve()` applies to BOTH routes, and that is about the printed line
# rather than about finding files. The flag used to resolve and the configured
# default did not, so one directory had two spellings depending on how you got
# there - and on this platform they differ by a symlinked prefix. A scope line
# whose spelling depends on the route is a scope line two runs cannot be
# compared with, which is the one job it has.
NOTES_DIR = (pathlib.Path(OPTS.notes).expanduser() if OPTS.notes
             else default_module_dir(ROOT)).resolve()

# Lessons are recognised by carrying a lesson-meta block rather than by their
# filename, so this sweep works on a course that does not number its parts the
# way this module does. See split_lessons.is_lesson_file.
NOTES = SPLIT.lessons_in(NOTES_DIR)

# 🔴 On EVERY run, not only when the directory was named on the command line.
# The old print sat INSIDE the `if`, so the DEFAULT run - the one whose scope
# comes from a config file the reader never opens - was the one run that said
# nothing at all about what it had read. That is the silent half of the same
# defect: the scope you did not choose is the scope you most need told.
print("swept: %s (%d lessons)" % (NOTES_DIR, len(NOTES)))

# 🔴 After the content/reader split a lesson file is content only, and the page
# the browser numbers is the composed one. So the page rule runs on what the
# SERVER WOULD SEND and the server rule runs on what is ON DISK, which is what
# an edit rewrites. Agreement across that boundary is the thing worth checking:
# it is exactly the property the split could have broken.
CFG = {"class_name": "Affective Disorders", "store_prefix": SPLIT.DEFAULT_STORE_PREFIX}


def as_served(p, src=None):
    src = p.read_text() if src is None else src
    if not SPLIT.is_content_file(src):
        return src
    return S.compose_lesson(CFG, src, p.name)

# 🔴 THE FLOOR FOR "THIS LESSON WAS NEVER WRITTEN", and not a quality bar.
# `new_lesson.py` truncated a long title's slug mid-word, the write went to the
# untruncated path, nothing was created, and `verify_notes --dois` printed
# ALL CHECKS PASS over a 137-character scaffold. It was right to: every check in
# this file is about what a lesson CONTAINS, and none of them fires on a lesson
# that contains nothing. An empty body has no DOI to disagree with, no block to
# mis-number and no house-style slip.
# ⚠️ Deliberately far below any real lesson so it can never be read as a length
# opinion: measured 2026-09-08 across all 98 lessons in this repo, the smallest is
# 62 blocks and 996 words. This catches "never written", not "short".
BODY_MIN_BLOCKS, BODY_MIN_WORDS = 5, 50

PAGE_TAGS = {"p", "li", "h1", "h2", "h3", "h4", "dd", "dt", "figcaption",
             "blockquote", "td", "th"}
VOID = {"br", "img", "hr", "input", "meta", "link", "source", "path", "circle",
        "rect", "line", "polyline", "polygon", "ellipse", "use", "stop", "col"}


class PageBlocks(HTMLParser):
    """The page's own rule: outermost PAGE_TAGS inside .wrap, skipping the
    layer's own furniture, dropping any block whose textContent is empty."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []          # open element names
        self.wrap = 0            # depth at which .wrap opened, or None
        self.skip = 0            # inside .hl-panel / .sv-add
        self.block = None        # name of the block we are collecting
        self.buf = []
        self.out = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = a.get("class", "").split()
        if tag in VOID:
            return
        self.stack.append(tag)
        d = len(self.stack)
        if "wrap" in cls and self.wrap is None:
            self.wrap = d
        if self.wrap is None and "wrap" in cls:
            self.wrap = d
        if "hl-panel" in cls or "sv-add" in cls:
            self.skip = self.skip or d
        if (self.block is None and not self.skip and self.wrap
                and tag in PAGE_TAGS):
            self.block = d
            self.buf = []

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        d = len(self.stack)
        if self.block == d:
            text = "".join(self.buf)
            if text.strip():
                self.out.append(text)
            self.block = None
        if self.skip == d:
            self.skip = 0
        if self.wrap == d:
            self.wrap = None
        if self.stack:
            self.stack.pop()

    def handle_data(self, data):
        if self.block is not None:
            self.buf.append(data)


def page_blocks(src):
    p = PageBlocks()
    p.wrap = None
    p.feed(src)
    return p.out


def raw(frag):
    return html_mod.unescape(S.TAG_RE.sub("", frag))


B3 = [
    (r'^(This|That) is (the|what|where|why|how)\b', 'opens "This is …"'),
    (r'^Here\b|^Now\b|^At this point\b|^So far\b', 'positional deictic'),
    (r'^Notice\b|^Note that\b|^Observe\b', 'stage direction'),
    (r'^It is worth\b|^Worth\b', '"it is worth …"'),
    (r'\bhas been mentioned\b|\bmentioned in passing\b|\bas we have seen\b|\bas noted (above|earlier)\b', "the note's own history"),
    (r'^(This|The) (section|part|topic|note)\b|^Part \d\b', 'document is the subject'),
    (r'\bwhat follows\b|\bthe rest of this\b|\bcomes next\b', 'forward reference'),
]
# 🔴 This scan used to be headed "spec section B", and §B's Never list has FOUR
# bullets: naming a person, referring to the delivery format, narrating the
# source's behaviour, and reproducing interview scaffolding. This pattern
# implements the SECOND one. The other three are not phrase-matchable, so the
# header now names the bullet instead of the section: a check whose title claims
# more than its body does is how the hole below stayed open for weeks.
#
# 🔴 And it used to implement about HALF of even that bullet. `lecture`, `video`,
# `transcript` and `summary slide` were all absent while `lecturer` was present,
# which is exactly why nobody saw it: the word is in the regex, attached to a
# different rule, so the rule looked covered. A first draft of one lesson
# carried "the lecture" 17 times and this scan printed `total: 0`; two lessons
# had already shipped with one each, waved through on every run since they
# landed. Found 2026-09-09 by `study-hub-content` while writing W5-T3-P2.
#
# 🟢 So the alternatives below are a TRANSCRIPTION of NOTE-SPEC §B's list, not a
# summary of it, and `test_secb_scan.py` builds its fixture by reading that list
# out of the spec: a phrase added there turns the suite red until this pattern
# learns it. There is no count anywhere for anybody to bump.
#
# ⚠️ Two candidates were left out, both MEASURED over all 110 lessons on
# 2026-09-09 rather than argued:
#   `the video`     1 hit, and it is legitimate - "slowing the video down and
#                   speaking for the baby" is a clinical technique, not a
#                   delivery format. The spec's list says "this video", and that
#                   is what this implements. A scan whose total is never zero
#                   teaches its reader to stop reading the total.
#   bare `slide`    🔴 THIS LINE USED TO SAY "7 hits, every one legitimate" AND
#                   THAT WAS FALSE. `study-hub-content` checked the call instead
#                   of taking it (`5e27a54`) and found TWO of the seven were
#                   real violations: "stated on the module's own slide" and
#                   "five findings sit on the same slide", both inside a
#                   `note warn` box, which NOTE-SPEC:214 holds to a STRICTER
#                   version of §B rather than a looser one. They are fixed.
#                   ⚠️ I had printed the first four matches and generalised the
#                   word "every" from them. **The sample was the four I could
#                   see**, which is this project's own named failure.
#                   🟢 THE DECISION STILL STANDS AND THE REASON IS NOW TRUE:
#                   5 hits remain, 4 are provenance ("cited on no slide") and
#                   one is the ordinary English word meaning a slip ("the shared
#                   word invites exactly that slide"), so a bare `slide` would
#                   false-alarm for ever. It would also catch "the summary
#                   slide" free, which is why that phrase is spelled out instead.
#                   ⚠️ THE PHRASES THAT WOULD CATCH THE TWO ARE PREPOSITIONAL
#                   ("on a slide", "on the same slide"), their finding, and they
#                   are FILED rather than added: "cited on a slide" is one of the
#                   five and whether provenance may say it is a writing call.
# `presenter` and `interviewer` belong to bullet 1 and are not in scope here;
# `interviewer` measured 2 hits, both legitimate (blinded research interviewers).
#
# 🔴 EVERY alternative ends at a word boundary, and that is not tidiness. The
# first draft of this widening did not, and `the transcripts?` then matched
# "stimulates THE TRANSCRIPTion rate of a target gene" twice in a shipped
# neuroinflammation lesson. ⚠️ The measurement that cleared the widening had used
# `\bthe transcripts?\b`, so it was a witness for a pattern this file does not
# contain; the corpus test lifts the pattern from HERE for exactly that reason,
# and it went red inside a minute.
# ⚠️ `slides?\s+\d` is the one alternative with NO trailing boundary, deliberately:
# it ends in a digit, and `\b` after `\d` would refuse "slide 17" while accepting
# "slide 1", which is the worst of both.
SECB = re.compile(
    r'\b('
    r'lecturers?\b'                # §B bullet 1, the one word of it a scan can do
    r'|(the|this) lectures?\b'     # "the lecture"
    r'|this videos?\b'             # "this video"
    r'|(the|this) decks?\b'        # "the deck"
    r'|(the|this) slides?\b'       # "the slides", and "no audio on this slide"
    r'|the summary slide\b'        # "the summary slide"
    r'|the transcripts?\b'         # "the transcript", never "transcription"
    r'|slides?\s+\d'               # "slide 17"
    r')', re.I)

fails = []

print("=== block numbering, page rule vs server rule ===")
for p in NOTES:
    src = p.read_text()
    srv = [S.html_to_text(b["inner"]) for b in S.find_blocks(src)]
    pg = [re.sub(r"\s+", " ", t).strip() for t in page_blocks(as_served(p, src))]
    ok = (len(srv) == len(pg)) and all(a == b for a, b in zip(srv, pg))
    if not ok:
        fails.append("%s: page %d blocks, server %d" % (p.name, len(pg), len(srv)))
        for i, (a, b) in enumerate(zip(srv, pg)):
            if a != b:
                print("   first divergence at %d:\n     server %r\n     page   %r" % (i, a[:70], b[:70]))
                break
    words = sum(len(t.split()) for t in srv)
    empty = len(srv) < BODY_MIN_BLOCKS or words < BODY_MIN_WORDS
    if empty:
        fails.append("%s: no body, %d blocks and %d words against a floor of %d and %d"
                     % (p.name, len(srv), words, BODY_MIN_BLOCKS, BODY_MIN_WORDS))
    print("  %-58s %s  blocks %4d  words %5d%s"
          % (p.name[:58], "OK " if ok else "BAD", len(srv), words,
             "  🔴 NO BODY" if empty else ""))

print("\n=== marks ===")
total = good = 0
for mp in sorted(NOTES_DIR.glob("*-marks.json")):
    if ".bak" in mp.name:
        continue
    d = json.loads(mp.read_text())
    # 🔴 READINGS has no file on disk: it is composed from readings.json on
    # request. Its marks are resolved against the SAME generated content the
    # page is built from (readings_content is one implementation, shared), so
    # the numbers the browser sees and the numbers this checks are the same by
    # construction. Before this branch, the first highlight on a reading would
    # have crashed this whole sweep with an IndexError.
    if d["doc"] == "READINGS":
        gen = S.readings_content(dict(CFG, notes_dir=NOTES_DIR))
        if gen is None:
            fails.append("%s exists but the course has no readings.json" % mp.name)
            continue
        bl = S.find_blocks(gen)
    elif d["doc"] == "MISTAKES":
        # Same contract as READINGS: the page is generated, so marks on it are
        # checked against the same generator the browser was served from.
        gen = S.mistakes_content(dict(CFG, notes_dir=NOTES_DIR))
        if gen is None:
            fails.append("%s exists but the course has no mistakes.json" % mp.name)
            continue
        bl = S.find_blocks(gen)
    else:
        hits = [p for p in NOTES if p.name.startswith(d["doc"])]
        if not hits:
            fails.append("%s has no matching lesson file" % mp.name)
            continue
        bl = S.find_blocks(hits[0].read_text())
    for it in d["items"]:
        total += 1
        try:
            hit = raw(bl[it["b"]]["inner"])[it["s"]:it["e"]] == it["t"]
        except IndexError:
            hit = False
        if hit:
            good += 1
        else:
            fails.append("%s mark %d does not resolve" % (mp.name, it["id"]))
    print("  %-24s %d items, %d notes" % (d["doc"], len(d["items"]), len(d.get("notes", []))))
print("  %d of %d marks resolve" % (good, total))
if good != total:
    fails.append("marks")

print("\n=== cross-reference links ===")
broken = 0
for p in NOTES:
    src = p.read_text()
    for href in re.findall(r'<a class="xref" href="([^"]+)"', src):
        f, _, frag = href.partition("#")
        tgt = NOTES_DIR / f
        if not tgt.exists():
            print("  MISSING FILE %s -> %s" % (p.name, href)); broken += 1; continue
        if frag and ('id="%s"' % frag) not in tgt.read_text():
            print("  MISSING ANCHOR %s -> %s" % (p.name, href)); broken += 1
print("  %d broken" % broken)
if broken:
    fails.append("xrefs")

print("\n=== house style ===")
em = [p.name for p in NOTES if "\u2014" in S.html_to_text(p.read_text())]
print("  em dashes:", em or "none")
if em:
    fails.append("em dashes")

# 🔴 NO EXPECTED COUNT, and this header carried one until 2026-09-09: "expect
# exactly 1, the protected sentence". That number is only ever right for ONE
# course. It reads as a property and it was a pin on a situation: the second
# course here has 0, and a third has a number nothing in this file can know.
# ⚠️ Worse than stale, it left a SLOT. A reader who checks `total` against the
# header and never reads WHICH sentence sees the 1 and stops. Measured on this
# repo: the protected sentence had been edited away, a NEW hit took the vacant
# slot, and the run was read as clean because the number still said 1.
# 🟢 So the header sends the reader to the LINES. Each hit is printed above.
print("\n=== spec section B3 scan (no expected count; read the lines, not the "
      "total) ===")
n3 = 0
for p in NOTES:
    for b in S.find_blocks(p.read_text()):
        for sent in re.split(r'(?<=[.:;])\s+', S.html_to_text(b["inner"])):
            sent = sent.strip()
            if len(sent) < 12:
                continue
            for rx, why in B3:
                if re.search(rx, sent, re.I):
                    print("  %-22s %-26s %s" % (p.name[:22], why, sent[:66]))
                    n3 += 1
                    break
print("  total: %d" % n3)

print("\n=== spec section B, delivery format (source must not surface) ===")
nb = 0
for p in NOTES:
    for i, b in enumerate(S.find_blocks(p.read_text())):
        t = S.html_to_text(b["inner"])
        m = SECB.search(t)
        if m:
            print("  %-22s b%-4d %-12s %s" % (p.name[:22], i, m.group(0), t[:60]))
            nb += 1
print("  total: %d" % nb)


# One bad character in a JavaScript string literal takes the whole page down, and it
# fails silently: the browser renders the static HTML, runs nothing, and shows a search
# box that does not respond. On 2026-08-13 an unescaped apostrophe in "Alzheimer's",
# inside a single-quoted key-terms string, did exactly that to the hub for a day.
#
# 🔴 The hub was invisible to this script, because NOTES globs W*-T*-P*.html and the hub
# is notes/index.html. It carries the largest script in the project and nothing checked
# it. So this check walks its own file list, not NOTES.
print("\n=== javascript parses (node --check) ===")
# The reader's own JavaScript and CSS left the lessons in the content/reader
# split, so the two files it lives in now are checked by name. Miss them and
# this sweep would have gone quiet about the largest scripts in the project.
# 🔴 The reader's own HTML is DISCOVERED, not listed. This was a two-name list
# and `server/reader/player-controls.html` was added beside them on 2026-09-01
# with the gate silently going on checking two: a hardcoded list rots the day
# somebody adds a file, and the failure is that nothing is checked rather than
# that something is wrong. `*.html` also excludes the dated `.bak` copies that
# live in that folder, which are not code anybody runs.
JS_FILES = ([NOTES_DIR / "index.html"] + NOTES
            + [ROOT / "server" / "local-layer.html"]
            + sorted((ROOT / "server" / "reader").glob("*.html")))
import subprocess, tempfile, os

if not shutil.which("node"):
    print("  SKIPPED: node is not on PATH, so nothing was checked")
    fails.append("js unchecked")
else:
    njs = 0
    for p in JS_FILES:
        if not p.exists():
            continue
        src = p.read_text(encoding="utf-8")
        for m in re.finditer(r"<script([^>]*)>(.*?)</script>", src, re.S):
            attrs, frag = m.group(1), m.group(2)
            if not frag.strip():
                continue
            # A lesson's facts ride in a JSON script block since the content
            # split. It is data, not code: node would reject it and did, for all
            # 29, the first time this ran. Checked as JSON instead.
            kind = (re.search(r'type\s*=\s*"([^"]*)"', attrs) or [None, ""])[1].lower()
            if kind and kind not in ("text/javascript", "application/javascript", "module"):
                if "json" in kind:
                    try:
                        json.loads(frag)
                    except ValueError as exc:
                        print("  BAD JSON      %s  %s" % (p.name, exc))
                        fails.append("json %s" % p.name)
                        njs += 1
                continue
            # Pad with newlines so node reports the real line number in the HTML file.
            line0 = src[:m.start(2)].count("\n") + 1
            fd, tmp = tempfile.mkstemp(suffix=".js")
            os.write(fd, ("\n" * (line0 - 1) + frag).encode("utf-8"))
            os.close(fd)
            r = subprocess.run(["node", "--check", tmp], capture_output=True, text=True)
            os.unlink(tmp)
            if r.returncode:
                why = next((l.strip() for l in r.stderr.split("\n")
                            if "Error" in l), "parse error")
                where = next((l.strip() for l in r.stderr.split("\n")
                              if re.search(r"\.js:\d+", l)), "")
                ln = re.search(r"\.js:(\d+)", where)
                print("  SYNTAX ERROR  %s%s  %s"
                      % (p.name, (" line " + ln.group(1)) if ln else "", why))
                fails.append("js %s" % p.name)
                njs += 1
    print("  %d files parsed, %d with errors" % (len(JS_FILES), njs))


# --- the pages the SERVER builds, which no file on disk contains ----------------------
#
# 🔴 Added 2026-08-21, the same day it caught its first fault, which had already
# shipped past every other check here. The home page, the course hub, the Settings
# page and the token prompt are Python STRINGS inside study_server.py, so the file
# walk above cannot see them: it looks for <script> in .html files and these live
# in .py. The reader's own JavaScript has been checked since August; the server's
# has never been checked at all.
#
# What it caught: a quote inside a quote inside a Python string, escaped once where
# it needed escaping twice. Python collapsed the escape and the browser got a broken
# string literal, which takes down the WHOLE script block. The pages still render
# perfectly, and every button on them silently does nothing. That is the failure
# this project keeps meeting in different clothes: a healthy-looking answer from
# something that is not working.
print("\n=== the server's own pages parse ===")
if not shutil.which("node"):
    print("  SKIPPED: node is not on PATH")
else:
    class _Fill(dict):
        """Stand-ins for the template's holes. The three that are read back as
        JavaScript literals have to be valid JSON or the check fails on its own
        scaffolding rather than on the page."""
        def __missing__(self, key):
            if key in ("levels", "sizes", "models"):
                return "[]"
            if key == "state":
                return "{}"
            if key in ("module", "codejs", "backjs"):
                return '"CODE"'
            if key in ("prompts", "status", "frags"):
                return "{}"
            if key == "askname":
                return "false"
            if key == "tokenbar":
                return S.TOKEN_BAR
            return "x"

    PAGES = [("TOKEN_BAR", S.TOKEN_BAR),
             ("ADD_COURSE_CTA", S.ADD_COURSE_CTA),
             ("ADD_COURSE_PAGE", S.ADD_COURSE_PAGE),
             ("IMPORT_BLOCK", S.IMPORT_BLOCK),
             ("SETTINGS_PAGE", S.SETTINGS_PAGE),
             ("HELP_PAGE", S.HELP_PAGE),
             ("READINGS_PAGE", S.READINGS_PAGE),
             ("WIZARD_PAGE", S.WIZARD_PAGE),
             ("HOME_PAGE", S.HOME_PAGE)]
    nbad = 0
    for name, tpl in PAGES:
        try:
            rendered = tpl % _Fill()
        except (KeyError, ValueError, TypeError) as exc:
            print("  WILL NOT RENDER  %-18s %s" % (name, exc))
            fails.append("page %s" % name)
            nbad += 1
            continue
        for m in re.finditer(r"<script([^>]*)>(.*?)</script>", rendered, re.S):
            attrs, frag = m.group(1), m.group(2)
            if not frag.strip():
                continue
            kind = (re.search(r'type\s*=\s*"([^"]*)"', attrs) or [None, ""])[1].lower()
            if kind and kind not in ("text/javascript", "application/javascript", "module"):
                continue
            fd, tmp = tempfile.mkstemp(suffix=".js")
            os.write(fd, frag.encode("utf-8"))
            os.close(fd)
            r = subprocess.run(["node", "--check", tmp], capture_output=True, text=True)
            os.unlink(tmp)
            if r.returncode:
                why = next((l.strip() for l in r.stderr.split("\n") if "Error" in l),
                           "parse error")
                print("  SYNTAX ERROR     %-18s %s" % (name, why))
                fails.append("page %s" % name)
                nbad += 1
    print("  %d templates rendered and parsed, %d with errors" % (len(PAGES), nbad))


# --- the same idea one language across: is the CSS well formed? -----------------------
#
# 🔴 Added 2026-08-13 after a stray `*/` silently deleted a live rule. A comment was
# closed, six lines of prose followed, and a second `*/` closed nothing. CSS recovers
# from garbage by skipping to the next `}`, so it ate the rule that followed and the
# page rendered perfectly with one feature quietly missing. Nothing caught it: the JS
# check passes because the fault is in a <style> block, and every byte-level and
# HTTP-level check passes too. It was found only because a browser probe reported
# `mask-image: none` where a gradient was expected.
#
# This is the same lesson the JS check was added for, in a language nobody was looking
# at: the thing nothing checks is the thing that breaks.
#
# Deliberately not a full CSS parser, because there is no stdlib one and this project
# takes no dependencies. Comment balance and brace balance catch the failure above and
# cost nothing. Limitation worth knowing: a `/*` inside a quoted string or a url() would
# read as a comment here. There are none today.
print("\n=== css is well formed ===")
ncss = 0
for p in JS_FILES:
    if not p.exists():
        continue
    src = p.read_text(encoding="utf-8")
    for m in re.finditer(r"<style[^>]*>(.*?)</style>", src, re.S):
        css = m.group(1)
        base = src[:m.start(1)].count("\n") + 1
        i, depth, in_comment, comment_at = 0, 0, False, 0
        bad = []
        while i < len(css):
            two = css[i:i + 2]
            if in_comment:
                if two == "*/":
                    in_comment = False
                    i += 2
                    continue
            elif two == "/*":
                in_comment, comment_at = True, base + css[:i].count("\n")
                i += 2
                continue
            elif two == "*/":
                bad.append((base + css[:i].count("\n"),
                            "stray */ closes a comment that was never opened"))
                i += 2
                continue
            elif css[i] == "{":
                depth += 1
            elif css[i] == "}":
                depth -= 1
                if depth < 0:
                    bad.append((base + css[:i].count("\n"), "unbalanced } "))
                    depth = 0
            i += 1
        if in_comment:
            bad.append((comment_at, "/* is never closed, so the rest of the block is dead"))
        if depth:
            bad.append((base + css.count("\n"), "%d unclosed { at end of block" % depth))
        for ln, why in bad:
            print("  CSS ERROR  %s line %d  %s" % (p.name, ln, why))
            fails.append("css %s" % p.name)
            ncss += 1
print("  %d files checked, %d with errors" % (len(JS_FILES), ncss))


# --- optional, needs the network: does each DOI point at the paper it is labelled with?
# Off by default because it takes about a minute and the rest of this script is instant.
# A DOI that RESOLVES proves nothing: 8 of the 95 in these notes resolved to the wrong
# paper, each one a few digits off, which is what a constructed identifier looks like.
if OPTS.dois:
    import json, urllib.request, urllib.parse, time, unicodedata, collections

    # NFD strips accents, but two families of character survive it and produced three
    # false mismatches on Week 4 Topic 2: CrossRef stores U+2010 HYPHEN in some surnames
    # where a page types U+002D (Buist-Bouwman, Martinez-Aran), and letters whose
    # diacritic is part of the glyph do not decompose at all (Mork, Solheim).
    FOLD = {ord(c): "-" for c in "‐‑‒–−"}
    FOLD.update({ord(c): "'" for c in "’‘ʼ`"})   # O'Neil is stored with U+2019
    FOLD.update({ord("ø"): "o", ord("Ø"): "O", ord("æ"): "ae",
                 ord("Æ"): "AE", ord("œ"): "oe", ord("Œ"): "OE",
                 ord("ł"): "l", ord("Ł"): "L", ord("đ"): "d",
                 ord("Đ"): "D", ord("ß"): "ss", ord("ð"): "d",
                 ord("þ"): "th"})

    def deacc(x):
        x = "".join(c for c in unicodedata.normalize("NFD", x) if not unicodedata.combining(c))
        return x.translate(FOLD)

    def years(rec):
        """Both the online-first year and the issue year count as the paper's year.
        CrossRef's `issued` is the earliest, so a paper printed in a 2016 issue after
        appearing online in 2013 fails a naive check against the year a deck prints."""
        out = set()
        for key in ("issued", "published-print", "published-online"):
            dp = (rec.get(key) or {}).get("date-parts") or []
            if dp and dp[0] and dp[0][0]:
                out.add(dp[0][0])
        return out

    # The DOIs this course has already had judged by hand: `skip` for labels that carry
    # no author to compare against, `noted` for papers whose published correction is
    # already linked on the page. Both live beside the course, not in this file.
    SKIP, NOTED, _sidecar_problems = load_doi_exceptions(NOTES_DIR)
    for _problem in _sidecar_problems:
        print("  SIDECAR  %s" % _problem)
        fails.append("doi sidecar")

    # 🔴 The reader lives in `doi_links` because `corrcheck.py` has to see exactly
    # the same set: a DOI this gate checks and that sweep misses is a citation
    # nobody has ever looked at. It carries the measurement of what the old
    # source-reading pattern hid, and why stripping tags alone would not have been
    # enough.
    seen = collections.OrderedDict()
    unreadable = 0
    for p in NOTES:
        _, missed = DOILINK.collect([p.read_text()], into=seen)
        unreadable += missed

    print("\n=== DOI records (%d distinct, %d skipped, %d links unreadable) ==="
          % (len(seen), len(SKIP), unreadable))
    # 🔴 A count printed and not failed is the shape this whole check was fixed
    # for: a number in a wall of output that reads as good news. An unreadable
    # link is an UNCHECKED CITATION, which is the harm, so it fails. The corpus
    # is at zero today, so this costs nothing until
    # something genuinely breaks.
    if unreadable:
        print("  🔴 %d doi.org links could not be read as a link+label pair, so "
              "they were NOT checked" % unreadable)
        fails.append("doi %d unreadable links" % unreadable)
    shadows = []
    titles = {}
    for doi, labels in seen.items():
        if doi in SKIP:
            continue
        # 🔴 ASK TWICE, AND SAY WHICH WAY IT FAILED. One 25-second ask, treated as
        # "this paper does not exist", turned four slow responses into four
        # findings when this gate was first run over the corpus (see
        # doi_links.why_not). The second ask is patient rather than eager: a 404
        # is an answer and is not retried.
        rec = err = None
        for patience in (25, 60):
            try:
                rec = json.load(urllib.request.urlopen(urllib.request.Request(
                    "https://api.crossref.org/works/" + urllib.parse.quote(doi),
                    headers={"User-Agent": crossref_agent()}),
                    timeout=patience))["message"]
                break
            except Exception as exc:
                err = DOILINK.why_not(exc)
                if err[0] == "NOT IN CROSSREF":
                    break
        if rec is None:
            print("  %-16s %-38s %s (%s)"
                  % (err[0], doi, sorted(labels)[0][:60], err[1]))
            fails.append("doi %s %s" % (doi, err[1]))
            continue
        # Drop empty family names. An editorial notice (a retraction, a corrigendum
        # by "The Editors") carries an author entry with no family name, which used to
        # leave fam as [''] and fail the author check with nothing to compare against.
        fam = [f for f in (deacc(a.get("family", "")) for a in rec.get("author", [])) if f]
        yrs = years(rec)
        # One DOI often carries several link texts, because a table links the
        # instrument's name ("GAD-7") where the reference list links its authors.
        # Check the informative one: a label with a year in it names a paper.
        lab = min((l for l in labels if re.search(r"(19|20)\d{2}", l)),
                  default=None) or sorted(labels)[0]
        # A surname can be several words: "St John-Smith", "van der Berg", "de Vries".
        # Comparing only the label's first token made "St John-Smith et al. 2009" look
        # like a paper by somebody called St. Compare the whole author portion, meaning
        # everything before "et al" or the year, and accept containment either way.
        # Labels arrive from `doi_links` with entities already decoded, so the
        # ampersand between two names is "&" and no longer "&amp;".
        auth = deacc(re.split(r"\bet al\b|(?:19|20)\d{2}", lab)[0]
                     .replace("&", " ").strip(" ,.&"))
        laby = re.search(r"(19|20)\d{2}", lab)
        ok_a = (not fam) or any(f.lower() in auth.lower() or auth.lower() in f.lower()
                                for f in fam)
        ok_y = (not laby) or any(abs(int(laby.group(0)) - y) <= 1 for y in yrs)
        # 🔴 A record that STANDS IN for the paper: a supplement, a correction,
        # an erratum. It resolves, its year matches, its journal matches and its
        # title contains the real title, so every check above passes and every
        # word-overlap instrument scores it about 1.0. Structural or nothing.
        # Reported rather than failed: a supplement is a real document and
        # somebody may mean to cite one. What it must never do is pass silently.
        titles[doi] = SHADOW.first_title(rec)
        shadow = SHADOW.shadow_reason(doi, rec)
        if shadow:
            # Judged AFTER the loop: whether the article is cited too can only
            # be answered once every record on this run has been fetched.
            shadows.append((doi, shadow, lab, rec))
        upd = [u.get("type") for u in rec.get("updated-by", [])]
        why = ("author %s vs %s" % (lab, fam[:2]) if not ok_a
               else "year %s vs %s" % (lab, sorted(yrs)) if not ok_y
               else "updated-by %s" % upd if upd and doi not in NOTED
               else None)
        if why:
            print("  MISMATCH  %-38s %s" % (doi, why))
            fails.append("doi %s" % doi)
        time.sleep(0.04)
    print("  every DOI matches its record" if not any(f.startswith("doi ") for f in fails)
          else "  see mismatches above")

    # 🟢 A POSITIVE RESULT, printed even at zero. A check that is silent on
    # success is indistinguishable from a check that never ran, and this one
    # would be silent for months at a time.
    # 🔴 A deliberate correction link is NOT a finding, and telling the two
    # apart is the whole usefulness of this section. A writer who links a
    # correction on purpose cites the article beside it, which is what
    # NOTE-SPEC's `corrnote` convention says; a writer holding the wrong record
    # cites only the wrong record, because they believe it is the article.
    # Measured on the first real run over a whole course: without this the
    # Asperger correction in W4-T2-P1 reads as a defect and is not one.
    alone = [(d, w, l, SHADOW.companion_cited(d, r, titles)) for d, w, l, r in shadows]
    flagged = [x for x in alone if not x[3]]
    paired = [x for x in alone if x[3]]

    print("\n=== records that stand in for a paper (supplements, corrections) ===")
    if not flagged:
        # 🟢 A POSITIVE RESULT, printed even at zero. A check that is silent on
        # success is indistinguishable from a check that never ran, and this one
        # will be silent for months at a time.
        print("  none: %d of %d records are the paper they are cited as."
              % (len(seen) - len(flagged), len(seen)))
    for doi, why, lab, _ in flagged:
        print("  ⚠️ SHADOW RECORD  %s  (%s)" % (doi, lab[:48]))
        print("      %s" % why)
        instead = SHADOW.article_for(doi)
        if instead:
            print("      the article itself is probably %s, which this has NOT "
                  "resolved. Check it before swapping." % instead)
        print("      nothing else in this course cites the paper it stands for, "
              "which is what a wrong record looks like.")
    for doi, why, lab, mate in paired:
        print("  🟢 deliberate  %s is a stand-in, and %s (the paper itself) is "
              "cited too" % (doi, mate))

# 🔴 The scope goes on the line ABOVE the verdict, never appended to it. The
# verdict line is PARSED - `FAILURES: a; b` is split on the semicolons - so it
# has to stay last and stay exactly what it was. Beside the verdict is what was
# asked for, and the line above is beside.
print("\nswept: %s (%d lessons)" % (NOTES_DIR, len(NOTES)))
print(("FAILURES: " + "; ".join(fails)) if fails else "ALL CHECKS PASS")
sys.exit(1 if fails else 0)
