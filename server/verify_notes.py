#!/usr/bin/env python3
"""Verification sweep over every note.

The block-count check is the one that matters most: the page numbers blocks with
a CSS selector and the server numbers them with an HTMLParser, and an edit is
refused whenever the two disagree. This re-implements the page's rule
independently rather than calling the server's, so agreement means something.
"""
import sys, pathlib, re, json, shutil, os, html as html_mod
from html.parser import HTMLParser

# 🔴 Derived, never hardcoded. This file ships inside the kit (plan 02 §9) as the
# gate the writing skill runs, so an absolute path into one person's Dropbox made
# it unrunnable for everyone else. The script lives in <repo>/server/, so the repo
# is its parent's parent, whatever anybody called the folder.
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))
import study_server as S
import split_lessons as SPLIT
from doi_sidecar import DOI_SIDECAR, load_doi_exceptions


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
NOTES_DIR = default_module_dir(ROOT)
if "--notes" in sys.argv:
    NOTES_DIR = pathlib.Path(sys.argv[sys.argv.index("--notes") + 1]).expanduser().resolve()
    print("notes: %s" % NOTES_DIR)

# Lessons are recognised by carrying a lesson-meta block rather than by their
# filename, so this sweep works on a course that does not number its parts the
# way this module does. See split_lessons.is_lesson_file.
NOTES = SPLIT.lessons_in(NOTES_DIR)

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
SECB = re.compile(r'\b(lecturer|the deck|this deck|the slide|this slide|slides?\s+\d)', re.I)

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
    print("  %-58s %s  blocks %4d  words %5d" % (p.name[:58], "OK " if ok else "BAD", len(srv), words))

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

print("\n=== spec section B3 scan (expect exactly 1, the protected sentence) ===")
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

print("\n=== spec section B scan (source must not surface) ===")
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
JS_FILES = ([NOTES_DIR / "index.html"] + NOTES
            + [ROOT / "server" / "local-layer.html", ROOT / "server" / "reader" / "shell.html"])
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
             ("ADD_COURSE_BLOCK", S.ADD_COURSE_BLOCK),
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
if "--dois" in sys.argv:
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

    seen = collections.OrderedDict()
    for p in NOTES:
        for doi, lab in re.findall(r'href="https://doi\.org/([^"]+)"[^>]*>([^<]{1,70})</a>', p.read_text()):
            seen.setdefault(doi, set()).add(re.sub(r"\s+", " ", lab).strip())

    print("\n=== DOI records (%d distinct, %d skipped) ===" % (len(seen), len(SKIP)))
    for doi, labels in seen.items():
        if doi in SKIP:
            continue
        try:
            rec = json.load(urllib.request.urlopen(urllib.request.Request(
                "https://api.crossref.org/works/" + urllib.parse.quote(doi),
                headers={"User-Agent": crossref_agent()}),
                timeout=25))["message"]
        except Exception as exc:
            print("  NOT IN CROSSREF  %-38s %s" % (doi, sorted(labels)[0]))
            fails.append("doi %s" % doi)
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
        auth = deacc(re.split(r"\bet al\b|(?:19|20)\d{2}", lab)[0]
                     .replace("&amp;", " ").strip(" ,.&"))
        laby = re.search(r"(19|20)\d{2}", lab)
        ok_a = (not fam) or any(f.lower() in auth.lower() or auth.lower() in f.lower()
                                for f in fam)
        ok_y = (not laby) or any(abs(int(laby.group(0)) - y) <= 1 for y in yrs)
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

print("\n" + ("FAILURES: " + "; ".join(fails) if fails else "ALL CHECKS PASS"))
sys.exit(1 if fails else 0)
