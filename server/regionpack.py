#!/usr/bin/env python3
"""The brain-region picture pack: local plates instead of a Wikipedia lead image.

EH, 2026-09-03: *"we should only keep the unique images and have a
cross-referencing list ... we're going to try to implement this thing where you
can right- and left-scroll through different images of the same region ...
package it up and implement it."*

🔴 **WHY A PACK AND NOT A BETTER FETCH.** `region_image()` took the LEAD IMAGE of
a Wikipedia article, which is chosen by that article's editors and not for this
purpose: `Limbic system` led with the back cover of a book, and three others led
with 1918 engravings that label the structure in four-point italic. Five terms
already carried a hand-picked override. **A curated local plate is offline, was
chosen by somebody for this job, and there is more than one of them per region.**

🟢 **THE ORDER IN `regions.json` IS THE DISPLAY ORDER AND IS NEVER RE-SORTED
HERE.** It is six keys deep, specified after looking at the plates, and it is not
alphabetical. Two of its keys look redundant and are the two that matter: without
them **38 of 81 regions would open on a plate that never names them** (Medulla
opened on a plate whose entire legend read "Brainstem") and 8 would alternate
between two scenes. Re-sorting, filtering or de-duplicating this list undoes a
decision that was made by looking.

⚠️ **THE PACK IS A BUILD PRODUCT** (`knowledge-packs/tools/build-brain-regions-pack.py`).
A v2 is rebuilt, never hand-patched, and this module reads it rather than
knowing anything about how it was made.

🟢 **A MISSING OR UNREADABLE PACK IS NOT AN ERROR.** Every entry point answers
"nothing here" and the caller falls back to the network exactly as it did before
the pack existed, which is what makes wiring this in additive.
"""
import json
import re
import urllib.parse
from pathlib import Path

PACK_ID = "brain-regions"
PACK_DIR = Path(__file__).resolve().parent.parent / "knowledge-packs" / PACK_ID
IMAGE_DIR = PACK_DIR / "images"

# 🔴 THE VERSION IS IN THE URL, exactly as it is for the vendored reader assets
# and for the same reason: it buys `immutable` caching honestly, because a
# rebuilt pack serves its plates from URLs no browser has ever seen. The
# filenames carry a `--candidate-v4` of their own, but that is the SOURCE's
# version and it does not move when the pack is rebuilt around it.
ROUTE = "/packs/" + PACK_ID + "/"

# ⚠️ "unreviewed" IS IN THE CREDIT ON PURPOSE, and it is a judgement rather than
# a fact off the file. `pack.json` says `status: candidate` and
# `formal_domain_review: required`; these are GENERATED plates. A reader
# revising anatomy from a picture is entitled to know it has not been through a
# domain review, and the alternative -- a caption that reads exactly like a
# reviewed figure -- is the kind of quiet claim this project does not make. One
# constant, so it is one line to change when the review happens.
CREDIT = "Study Hub plate (unreviewed)"

_PARENTHETICAL = re.compile(r"\s*\([^)]*\)\s*$")
_NOT_SLUG = re.compile(r"[^a-z0-9]+")

_LOADED = None

# --------------------------------------------------------------------------
# The pack's DEFINITIONS, which are the half that makes it a lookup source
# --------------------------------------------------------------------------

# 🔴 ONE FILE, and the pack's own README says so: "This is the whole pack in one
# file." Everything it supersedes sits in `definitions/zz-superseded/` and is
# deliberately not read.
DEFS_FILE = PACK_DIR / "definitions" / "REGION-PACK.md"

# `### Amygdala **[C]**` -- the trailing tier marker is the AUTHORING state and
# is not part of the name. 🔴 NOR IS WHAT FOLLOWS IT: several headings carry a
# kind as well (`### Central sulcus **[C]** *(landmark)*`), and taking the whole
# line gave `central-sulcus-c-landmark` as the key, which nothing would ever ask
# for. Everything from the first `**` or `*(` to the end of the line is
# annotation, so the name is what comes before it.
_DEF_HEAD = re.compile(r"^### (.+?)(?:\s*(?:\*\*|\*\().*)?$", re.M)
_DEF_ALIASES = re.compile(r"^\*Aliases:\s*(.+?)\s*\*\s*$", re.M)
# A line that starts a FACT block (`**Core functions**: ...`), which is where the
# prose ends. `**Entity type**` is the one such line that comes BEFORE the prose.
_DEF_FACT = re.compile(r"^\*\*[^*]+\*\*:", re.M)

_DEFS = None


def definitions(force=False):
    """The pack's prose, keyed the way a lookup will ask for it.

    `{key: {"name", "text", "aliases"}}`, keyed by `region_key` of the heading
    AND of every alias, so `amygdaloid complex` finds `Amygdala`.

    🔴 **THE ALIASES ARE THE POINT, and they are why this cannot just read
    `regions.json`.** That file is the IMAGE cross-reference: 81 regions, keyed
    by name, no aliases anywhere. The definitions file carries 223 entries and
    their alternative names, and a reader who looks up `amygdaloid complex` is
    asking about the amygdala.

    ⚠️ **An alias never overwrites a heading.** Two entries can legitimately
    list the same alternative name, and a heading is a thing the pack asserts
    while an alias is a thing it merely allows. First writer wins among aliases,
    which is arbitrary and is why they lose to headings rather than racing them.

    🟢 Cached like `load()`, including the failure, and for the same reason: a
    pack that is not there will not appear while the server runs, and a rebuilt
    pack needs a restart.
    """
    global _DEFS
    if _DEFS is not None and not force:
        return _DEFS
    out = {}
    try:
        text = DEFS_FILE.read_text(encoding="utf-8")
    except OSError:
        _DEFS = out
        return out

    heads = list(_DEF_HEAD.finditer(text))
    aliased = []
    for n, m in enumerate(heads):
        name = m.group(1).strip()
        body = text[m.end(): heads[n + 1].start() if n + 1 < len(heads) else len(text)]
        alias_block, prose = _def_split(body)
        entry = {"name": name, "text": prose,
                 "aliases": _def_aliases(alias_block)}
        if not entry["text"]:
            # ⚠️ A guard that does NOT currently fire, and saying so is the
            # point: all 223 headings have prose today, because the pack's front
            # matter uses `##` and only entries use `###`. It stays because an
            # empty definition would read as "the pack knows this term and has
            # nothing to say", which is worse than silence, and because the day
            # somebody adds a `###` subheading it would start firing. 🔴 It was
            # also hiding a real bug for an hour: fourteen entries came back
            # empty from a truncation and were silently skipped, so a parser
            # failure looked exactly like a pack that did not cover them.
            continue
        out[region_key(name)] = entry
        aliased.append(entry)

    for entry in aliased:
        for alias in entry["aliases"]:
            key = region_key(alias)
            if key and key not in out:
                out[key] = entry
    _DEFS = out
    return out


def _def_split(body):
    """An entry's body as `(aliases line, prose)`.

    🔴 **THE ALIASES LINE IS NOT ONE LINE.** It is an italic block that wraps,
    and on the widest entries it runs to four lines and carries a whole sentence
    of qualification. Skipping only the line that STARTS with `*Aliases:` leaked
    the rest of it into the prose, which is how `Prefrontal cortex` came back
    beginning mid-sentence with `cortex" is a common but imperfect synonym`.
    ⚠️ Found by reading the output rather than the parser: 90 of 210 entries had
    a stray italic marker in their text, and that was the tell.

    🟢 **`**Entity type**` ENDS IT, reliably**: every entry has one, it is the
    line immediately after the aliases, and it is the only fact line that comes
    BEFORE the prose. So the aliases are everything between the two, and the
    prose is everything after, up to the first fact line.
    """
    alias_lines, prose_lines = [], []
    where = "before"
    for line in body.split("\n"):
        stripped = line.strip()
        if where == "before":
            if stripped.startswith("*Aliases:"):
                where = "aliases"
                alias_lines.append(stripped)
            elif stripped.startswith("**Entity type**"):
                where = "prose"
            elif stripped:
                # No aliases and no entity type: prose starts here.
                where = "prose"
                prose_lines.append(line)
            continue
        if where == "aliases":
            if stripped.startswith("**Entity type**"):
                where = "prose"
            else:
                alias_lines.append(stripped)
            continue
        if stripped.startswith("#"):
            break
        if _DEF_FACT.match(stripped):
            # 🔴 A FACT LINE ENDS THE PROSE ONLY ONCE THE PROSE HAS STARTED.
            # The header block is a RUN of them, not one: `Anterior cingulate
            # cortex` carries `**Cytoarchitectonic correlates**` under its
            # entity type, and breaking on the first one returned an empty
            # definition for FOURTEEN entries, `Broca's area` and `Angular
            # gyrus` among them. ⚠️ Those are exactly the terms this pack is
            # being wired in to answer, so the bug would have shipped looking
            # like the pack simply not covering them.
            if not any(l.strip() for l in prose_lines):
                continue
            break
        prose_lines.append(line)
    return (" ".join(alias_lines), _plain(" ".join(prose_lines)))


# 🔴 THE MARKDOWN GOES, because this text lands in a lookup card as TEXT and a
# literal `**` there is not emphasis, it is a defect a reader can see. What does
# NOT go is the emoji: they carry the pack's own caveats (⚠️ before a
# qualification, 🔴 before a hazard) and they read correctly in a sentence.
# ⚠️ Emphasis is LOST, not translated, and that is a real if small cost: the
# card has no way to render it. Recorded rather than pretended away.
_EMPHASIS = re.compile(r"\*\*|`")


def _plain(text):
    return " ".join(_EMPHASIS.sub("", text or "").split()).strip()


def _def_aliases(block):
    """The alternative NAMES only, from a line that may also carry prose.

    🔴 **PRECISION OVER RECALL, because a wrong alias is a wrong ANSWER.** An
    alias becomes a lookup key, so a bad one makes an unrelated phrase resolve to
    a region and hand the reader a confident definition of something else. A
    missed one costs a lookup that finds nothing, which is what happens today.

    ⚠️ The qualifying sentences really are inside the alias block: one entry
    reads `PFC, frontal association cortex, prefrontal association cortex.
    **[R]** "Granular frontal cortex" is a common but imperfect synonym: ...`.
    So an item is kept only while it still looks like a NAME, and the first one
    that does not ends the list: everything after it is prose.
    """
    text = block.strip()
    if not text.startswith("*Aliases:"):
        return []
    text = text[len("*Aliases:"):].strip().rstrip("*").strip()
    out = []
    for item in text.split(","):
        name = item.strip()
        if not name or len(name) > 60:
            break
        if any(ch in name for ch in '":;*()['):
            break
        # A full stop inside means a sentence has begun. A trailing one is just
        # the end of the list.
        head = name.rstrip(".")
        if "." in head:
            break
        if not head:
            break
        out.append(head)
    return out


def definition(title, wiki=None):
    """The pack's entry for a term, by the term's own name then the `wiki` one.

    ⚠️ **The same order and the same reasoning as `region_for`**, deliberately:
    a term should not get its plates from one name and its words from another.
    """
    defs = definitions()
    for name in (title, wiki):
        if not name:
            continue
        hit = defs.get(region_key(name))
        if hit:
            return hit
    return None


def load(force=False):
    """`pack.json` and `regions.json`, read once per process.

    ⚠️ Cached deliberately, including the FAILURE. A pack that is not there will
    not appear while the server is running, and re-reading a missing file on
    every lookup would be a syscall per term for nothing. **A rebuilt pack needs
    a restart**, which is true of every other change to this server's Python and
    is the same trade `vendor_version()` makes.
    """
    global _LOADED
    if _LOADED is not None and not force:
        return _LOADED
    out = {"version": "", "regions": {}, "files": frozenset()}
    try:
        meta = json.loads((PACK_DIR / "pack.json").read_text(encoding="utf-8"))
        data = json.loads((PACK_DIR / "regions.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        _LOADED = out
        return out
    regions = data.get("regions")
    if isinstance(meta, dict) and isinstance(regions, dict):
        out["version"] = str(meta.get("version") or "")
        out["regions"] = regions
        # 🔴 THE ALLOW-LIST IS DERIVED FROM THE PACK'S OWN CROSS-REFERENCE, not
        # from a directory listing and not from a traversal check. A route that
        # can only serve names `regions.json` already points at cannot be walked
        # out of, whatever arrives in the path, and it also cannot serve a file
        # somebody dropped in the images directory by hand.
        names = set()
        for reg in regions.values():
            for im in (reg or {}).get("images") or []:
                name = (im or {}).get("file")
                if isinstance(name, str) and name and "/" not in name and "\\" not in name:
                    names.add(name)
        out["files"] = frozenset(names)
    _LOADED = out
    return out


def version():
    return load()["version"]


def files():
    return load()["files"]


def region_key(title):
    """A glossary term as the pack spells its region ids.

    🔴 THE PARENTHETICAL IS STRIPPED, and that is the whole difference between
    29 of 31 and 30 of 31 on the measured join: the glossary says
    `Suprachiasmatic nucleus (SCN)` and the pack says `suprachiasmatic-nucleus`.
    ⚠️ Only a TRAILING one, so a term whose name genuinely contains brackets in
    the middle is not silently truncated.

    🟢 No fuzzy matching anywhere. The one term that still misses
    (`Ventral striatum`) is a solved special case rather than a matching
    problem: it borrows the striatum's picture through the glossary's own
    `wiki` field, which is what this is handed.
    """
    s = _PARENTHETICAL.sub("", str(title or "")).strip().lower()
    return _NOT_SLUG.sub("-", s).strip("-")


def shows_for(im, region, title, pic_of=None):
    """What the caption says the picture is OF, which is the honest half.

    🔴 THREE ANSWERS AND THEY ARE DIFFERENT FACTS. `pic_of` is the glossary
    saying "this term has no picture of its own and is borrowing one", and it
    outranks everything: a reader who is not told takes the whole striatum for
    its ventral part. `names_region` is the PLATE saying whether the structure
    is labelled on it. When it is not, the honest caption is the SCENE, because
    a shared overview shows the parent territory alone: that is the case where
    Medulla used to be captioned "Medulla" over a plate whose legend read
    "Brainstem".
    """
    if pic_of:
        return pic_of
    name = (region or {}).get("name") or title
    if (im or {}).get("names_region"):
        return name
    return (im or {}).get("scene_title") or name


def region_for(title, wiki=None):
    """The pack entry for a term, by the TERM's own name first and the
    glossary's `wiki` name second.

    🔴 THE ORDER IS MEASURED AND IT IS NOT THE OBVIOUS ONE. The queue entry
    predicted 30 of 31 by slugging the glossary KEY; the code it was describing
    passes `wiki or title`, and slugging THAT gives a different pair of misses
    (`Insula`, whose wiki name is `Insular cortex`, and `Mediodorsal nucleus of
    the thalamus`, whose wiki name is `Medial dorsal nucleus`). 🟢 **Trying both
    takes the join to 31 of 31**, measured 2026-09-03 against
    one course's `glossary.json`, 31 terms.

    🟢 **The term's own name goes first because the pack was built around those
    names**, and the `wiki` name is what rescues the one term that genuinely has
    no plate of its own: `Ventral striatum` borrows `Striatum`, which is the
    behaviour `pic_of` exists to caption honestly.

    ⚠️ **THE ORDER IS UNOBSERVABLE TODAY AND THE TEST SAYS SO RATHER THAN
    PRETENDING OTHERWISE.** 28 of the 31 terms resolve under BOTH names and every
    one lands on the same region, so swapping these round changes nothing that
    can be measured -- a mutation proved exactly that by surviving.
    `test_the_two_names_never_disagree_which_is_WHY_the_order_is_safe` pins the
    invariant that makes it safe, so the day two names point at two different
    regions somebody has to choose on purpose.

    ⚠️ **This is exact lookup, not fuzzy matching.** Every name tried is written
    down somewhere in the pack or the glossary; nothing here guesses at one.

    🔴🔴 **THE ALIAS PASS IS SECOND, AND IT IS A SECOND LOOP RATHER THAN A LINE
    INSIDE THE FIRST.** `regions.json` is the IMAGE cross-reference and carries
    NO aliases; the pack's written definitions carry 223 headings and their
    alternative names. Until 2026-09-04 only the image index was consulted, so
    `Amygdaloid complex` found the amygdala's WORDS and lost its five PICTURES,
    with nothing on screen saying a picture existed. **That is the exact failure
    shape the pack was built to remove**, and it was found by TRYING the
    changelog's claim rather than by reading the diff.

    🔴 **Why two passes: a DIRECT hit must beat an ALIAS hit, for both names,
    before any alias is tried.** Folded into the first loop, `title`'s alias
    would outrank `wiki`'s own name. That is the same rule `definitions()`
    already applies internally ("an alias never overwrites a heading"): a
    heading is a thing the pack ASSERTS, an alias is a thing it merely ALLOWS.

    🟢 **ONE INDEX, TWO CONSUMERS.** The alias table is the one
    `definitions()` already builds. **A second alias table here would be a
    second thing to keep true**, and the two would disagree the first time
    somebody edited one file.

    ⚠️ **A DEFINITION IS NOT A PROMISE OF A PICTURE.** There are 655 definition
    keys and 81 regions with images, so most terms resolve to prose and no
    plate. `Broca's area` is exactly that: it is its own heading, it is not in
    the image index, and it correctly stays at zero plates. **This must never
    invent a picture for a term that has none.**
    """
    pack = load()
    for name in (title, wiki):
        if not name:
            continue
        hit = pack["regions"].get(region_key(name))
        if hit:
            return hit
    # The alias pass. `definitions()` maps every alias onto its heading's own
    # name, so this asks the image index the one further question worth asking:
    # "is the thing this name is ANOTHER NAME FOR in the picture index?"
    defs = definitions()
    for name in (title, wiki):
        if not name:
            continue
        entry = defs.get(region_key(name))
        if not entry:
            continue
        hit = pack["regions"].get(region_key(entry["name"]))
        if hit:
            return hit
    return None


def plates(title, pic_of=None, wiki=None):
    """Every plate for a structure, in the pack's own order, best first.

    Returns [] for anything the pack does not cover, which is the signal to fall
    back to the network. The shape of each item is the shape `region_image()`
    already returned, so the reader needs no new vocabulary to draw one.
    """
    pack = load()
    region = region_for(title, wiki)
    if not region:
        return []
    out = []
    for im in region.get("images") or []:
        name = (im or {}).get("file")
        if not isinstance(name, str) or name not in pack["files"]:
            continue
        out.append({
            "src": ROUTE + urllib.parse.quote(pack["version"], safe="")
                   + "/" + urllib.parse.quote(name, safe=""),
            "width": im.get("width"),
            "height": im.get("height"),
            # 🔴 EMPTY, not absent. The Wikipedia path fills this with the
            # article a reader can go and read; a generated plate has no such
            # page, and an empty string says so in the same key rather than
            # making the reader's code ask whether the key exists.
            "page": "",
            "credit": CREDIT,
            "shows": shows_for(im, region, title, pic_of),
        })
    return out


def image_bytes(name):
    """One plate, by a name the pack itself references. None for anything else."""
    if name not in files():
        return None
    try:
        return (IMAGE_DIR / name).read_bytes()
    except OSError:
        return None
