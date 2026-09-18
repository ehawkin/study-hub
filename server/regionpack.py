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

🟢 **THE PACK DOES NOT SHIP IN THE KIT; IT SHIPS BESIDE IT.** The second half of
this file packages the pack as its own release asset, named in the update feed,
and fetches, checks and installs it on a recipient's machine when they ask for
it (EH, 2026-09-17: *"give people the option to download the NeuroScience image
and glossary package"*).
"""
import argparse
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import zipfile
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

    🟢 Cached like `load()`, and with the same one exception: an ABSENT file is
    re-checked with a stat on each call, so a pack installed while the server
    runs is read on the next lookup. A REBUILT pack still needs `force=True` or a
    restart, because a file that is there is not re-read.
    """
    global _DEFS
    if _DEFS is not None and not force and (_DEFS or not DEFS_FILE.is_file()):
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

    ⚠️ Cached deliberately once it has been READ. **A rebuilt pack needs
    `force=True` or a restart**, which is true of every other change to this
    server's Python and is the same trade `vendor_version()` makes.

    🟢 **The one exception is ABSENCE, which is not cached**, since 2026-09-18:
    a pack that was not there is looked for again on the next call, one stat per
    lookup while it stays absent. That is what lets `install()` in another
    process (the `--install` verb a person or the Settings page runs) put the
    pack under a running server and have the next lookup show a plate, with no
    restart. Caching the failure was the earlier trade; a stat per lookup on a
    machine with no pack costs nothing a Wikipedia fetch does not dwarf.
    """
    global _LOADED
    if (_LOADED is not None and not force
            and (_LOADED["version"] or not (PACK_DIR / "pack.json").is_file())):
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


# --------------------------------------------------------------------------
# The pack as a RELEASE ASSET: built here, fetched here, verified here
# --------------------------------------------------------------------------
#
# EH, 2026-09-17: *"I wonder if, in our installer, we could give people the
# option to download the NeuroScience image and glossary package."*
#
# 🔴 THE KIT DOES NOT CARRY THE PACK, AND THAT DOES NOT CHANGE HERE. It is 44 MB
# against a kit of under one, and `formal_domain_review: required` is a fact
# about it that a recipient should meet before the plates are on their disk.
# So the pack travels as its OWN asset on the kit's release, named in the
# update feed under `packs`, and a person fetches it once, on purpose, from
# the wizard's tick, the Settings button or the verb below. Everything the
# reader needs is in the asset: the plates, the cross-reference AND the
# definitions file, because `pack_lookup` serves that file's prose today.
#
# 🟢 NOTHING HERE READS THE NETWORK UNLESS ASKED. `install()` takes its fetch
# as an argument so a test can hand it bytes; `write_asset()` only reads the
# tree it is given.

# What the authoring copy holds that a reader has no use for, each with the
# reason, so the list can be argued with rather than guessed at. 🔴 NOT
# `definitions/`: `REGION-PACK.md` is the glossary half of the pack, and the
# reader's lookup card serves it. Only its superseded drafts stay behind.
ASSET_EXCLUDED = {
    "authoring": "the guides and the verifier that MADE the pack, not what it holds",
    "reviews": "the review arguments; their outcomes are already in the pack",
    "definitions/zz-superseded": "drafts the reader never loads",
}
# A file of any other kind under the pack is a stray (a `.bak` beside the
# definitions, a `.DS_Store`) and never ships.
ASSET_SUFFIXES = (".json", ".md", ".png")
MANIFEST = "MANIFEST.json"
# Printed once by every install, because `pack.json` says `status: candidate`
# and `formal_domain_review: required`, and a person who just fetched 44 MB of
# anatomy is entitled to read that in words.
REVIEW_SENTENCE = ("This pack's plates and definitions have not had a formal "
                   "domain review (pack.json says so): study from them; do not "
                   "publish them as authoritative.")
# Where an install started from the Settings page leaves its record and its
# log, beside the caption engine's: the machine's own state folder, never the
# kit. Tests point this at a scratch folder.
STATE_DIR = Path("~/.kcl-study").expanduser()


class InstallProblem(Exception):
    """Anything that stops an install, said for a person."""


def pack_version(pack_dir=None):
    """The version `pack.json` states, read NOW and never cached. Empty when
    there is no readable pack. `version()` above is the reader's cached view;
    this is the installer's, which has to see a folder that just appeared."""
    root = Path(pack_dir or PACK_DIR)
    try:
        meta = json.loads((root / "pack.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    return str(meta.get("version") or "") if isinstance(meta, dict) else ""


def pack_counts(pack_dir=None):
    """`pack.json`'s own `counts`, uncached, or {} when there is no pack."""
    root = Path(pack_dir or PACK_DIR)
    try:
        meta = json.loads((root / "pack.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    counts = meta.get("counts") if isinstance(meta, dict) else None
    return counts if isinstance(counts, dict) else {}


def _excluded(rel):
    return any(rel == ex or rel.startswith(ex + "/") for ex in ASSET_EXCLUDED)


def asset_files(pack_dir=None):
    """The relative paths that make up the asset, sorted: everything under the
    pack except the excluded folders, hidden names, bytecode and files of a
    kind the pack does not consist of. The manifest itself is never listed,
    so listing an INSTALLED copy gives the same answer as listing the source."""
    root = Path(pack_dir or PACK_DIR)
    out = []
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        parts = p.relative_to(root).parts
        rel = "/".join(parts)
        if rel == MANIFEST or _excluded(rel):
            continue
        if any(part.startswith(".") or part == "__pycache__" for part in parts):
            continue
        if p.suffix.lower() not in ASSET_SUFFIXES:
            continue
        out.append(rel)
    return out


def manifest_for(pack_dir=None):
    """What the asset carries, by name, size and sha256, so an unpacked copy can
    be checked file by file rather than trusted because the zip opened."""
    root = Path(pack_dir or PACK_DIR)
    files = {}
    for rel in asset_files(root):
        data = (root / rel).read_bytes()
        files[rel] = {"bytes": len(data),
                      "sha256": hashlib.sha256(data).hexdigest()}
    return {"pack": PACK_ID, "version": pack_version(root), "files": files,
            "count": len(files),
            "bytes": sum(f["bytes"] for f in files.values())}


def asset_name(version):
    return "knowledge-pack-%s-%s.zip" % (PACK_ID, version)


def write_asset(out_dir, pack_dir=None):
    """The asset, written under `out_dir` and named for the pack's version.

    Every member sits under one top folder named `PACK_ID`, with the manifest
    beside `pack.json`. 🟢 Deterministic: fixed timestamps and a fixed order,
    so building the same pack twice gives the same bytes and the same sha256,
    which is what lets the feed's hash be checked against a rebuild."""
    root = Path(pack_dir or PACK_DIR)
    manifest = manifest_for(root)
    if not manifest["version"]:
        raise RuntimeError("no pack.json with a version under %s, so there is "
                           "nothing to package" % root)
    plates = [rel for rel in manifest["files"] if rel.endswith(".png")]
    if not plates:
        raise RuntimeError("the pack under %s has no plates, so it is not a "
                           "picture pack" % root)
    path = Path(out_dir) / asset_name(manifest["version"])
    if path.exists():
        path.unlink()
    stamp = (1980, 1, 1, 0, 0, 0)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for rel in manifest["files"]:
            info = zipfile.ZipInfo("%s/%s" % (PACK_ID, rel), date_time=stamp)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            z.writestr(info, (root / rel).read_bytes())
        info = zipfile.ZipInfo("%s/%s" % (PACK_ID, MANIFEST), date_time=stamp)
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o644 << 16
        z.writestr(info, json.dumps(manifest, indent=1, sort_keys=True) + "\n")
    return path


def asset_facts(path):
    """{name, bytes, sha256} of a written asset: what the feed states about it
    and what an install checks the download against."""
    data = Path(path).read_bytes()
    return {"name": Path(path).name, "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest()}


def unpack_problems(folder):
    """[what is wrong] with an unpacked copy, checked against the manifest it
    carries. Empty means every named file is present with its stated bytes and
    hash, nothing else is there, and `pack.json` agrees about the version."""
    folder = Path(folder)
    try:
        manifest = json.loads((folder / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return ["no readable %s: %s" % (MANIFEST, exc)]
    if not isinstance(manifest, dict):
        return ["%s is not an object" % MANIFEST]
    out = []
    if manifest.get("pack") != PACK_ID:
        out.append("the manifest is for %r, not %s" % (manifest.get("pack"), PACK_ID))
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        return out + ["the manifest names no files"]
    have = pack_version(folder)
    if not have or have != str(manifest.get("version") or ""):
        out.append("pack.json says version %r but the manifest says %r"
                   % (have, manifest.get("version")))
    for rel, meta in sorted(files.items()):
        meta = meta if isinstance(meta, dict) else {}
        try:
            data = (folder / rel).read_bytes()
        except OSError:
            out.append("missing: %s" % rel)
            continue
        if (len(data) != meta.get("bytes")
                or hashlib.sha256(data).hexdigest() != meta.get("sha256")):
            out.append("does not match its manifest entry: %s" % rel)
    extra = sorted(
        p.relative_to(folder).as_posix() for p in folder.rglob("*")
        if p.is_file() and p.name != MANIFEST
        and not any(part.startswith(".") for part in p.relative_to(folder).parts)
        and p.relative_to(folder).as_posix() not in files)
    if extra:
        out.append("%d file(s) the manifest does not name, e.g. %s"
                   % (len(extra), extra[0]))
    return out


def looks_authored(pack_dir=None):
    """True for the copy that is BEING WRITTEN (the repository's, with its
    authoring notes and reviews), which git keeps current and an install must
    never replace with a reader's copy of itself."""
    root = Path(pack_dir or PACK_DIR)
    return any((root / ex).exists() for ex in ASSET_EXCLUDED)


def default_feed():
    """The kit's update feed, from the same config the reader reads, so the
    verb and the reader can never point at two different feeds. The shipped
    default when this machine has no config yet."""
    try:
        import study_server as S
    except ImportError:
        return ""
    cfg = dict(S.DEFAULT_CONFIG)
    try:
        if Path(S.CONFIG_PATH).exists():
            cfg = S.load_config()
    except (OSError, ValueError, SystemExit):
        pass
    return str(cfg.get("update_url") or "").strip()


def _fetch(url):
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "study-hub-pack"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def feed_entry(feed):
    """This pack's entry out of a parsed feed, or None when the feed has none."""
    packs = feed.get("packs") if isinstance(feed, dict) else None
    entry = packs.get(PACK_ID) if isinstance(packs, dict) else None
    return entry if isinstance(entry, dict) and entry.get("url") else None


def install(feed_url=None, pack_dir=None, fetch=None, say=print, force=False):
    """Fetch the pack the feed names, check it, and put it where the reader looks.

    The order is the order of the ways it can go wrong: the feed first (is
    there a pack at all), then the download against the feed's size and hash,
    then the unpacked files against the manifest inside, and only then the
    swap, which is two renames. 🔴 Nothing is written under `pack_dir` until
    every check has passed, so a failed install leaves whatever was there.

    Returns {"ok", "version", "changed", "files"}; raises InstallProblem with
    the sentence for a person. `say` gets the progress lines and, once, the
    review sentence."""
    root = Path(pack_dir or PACK_DIR)
    fetch = fetch or _fetch
    url = feed_url or default_feed()
    if not url:
        raise InstallProblem("the update feed is off in this config, so there "
                             "is nowhere to fetch the pack from")
    try:
        feed = json.loads(fetch(url).decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        raise InstallProblem("the update feed at %s could not be read: %s" % (url, exc))
    entry = feed_entry(feed)
    if entry is None:
        raise InstallProblem("the update feed at %s names no %s pack, so there "
                             "is nothing to fetch" % (url, PACK_ID))
    want = str(entry.get("version") or "")
    have = pack_version(root)
    if have and have == want and not force:
        say("already installed: %s %s at %s" % (PACK_ID, have, root))
        return {"ok": True, "version": have, "changed": False,
                "files": len(asset_files(root))}
    if have and looks_authored(root):
        raise InstallProblem("%s is the authoring copy of the pack (it has %s), "
                             "which git keeps current; not replacing it with a "
                             "reader's copy" % (root, ", ".join(sorted(ASSET_EXCLUDED))))
    name = str(entry.get("name") or asset_name(want))
    size = entry.get("bytes") if isinstance(entry.get("bytes"), int) else 0
    say("downloading %s%s from %s"
        % (name, " (%d MB)" % round(size / 1048576) if size else "", entry["url"]))
    try:
        data = fetch(entry["url"])
    except OSError as exc:
        raise InstallProblem("the download failed: %s" % exc)
    if size and len(data) != size:
        raise InstallProblem("the download is %d bytes but the feed says %d, so "
                             "it was refused" % (len(data), size))
    want_sha = str(entry.get("sha256") or "")
    if want_sha and hashlib.sha256(data).hexdigest() != want_sha:
        raise InstallProblem("the download's sha256 does not match the feed's, "
                             "so it was refused")
    stage = root.parent / (".%s-incoming" % PACK_ID)
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for info in z.infolist():
                if info.is_dir():
                    continue
                top, _, rel = info.filename.partition("/")
                parts = rel.split("/") if rel else []
                if (top != PACK_ID or not parts or "" in parts or ".." in parts
                        or "\\" in rel):
                    raise InstallProblem("refusing a member outside %s/: %s"
                                         % (PACK_ID, info.filename))
                target = stage.joinpath(*parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(z.read(info))
    except zipfile.BadZipFile as exc:
        shutil.rmtree(stage)
        raise InstallProblem("the download is not a zip: %s" % exc)
    except InstallProblem:
        shutil.rmtree(stage)
        raise
    problems = unpack_problems(stage)
    if not problems and want and pack_version(stage) != want:
        problems.append("the feed says version %s but the pack says %s"
                        % (want, pack_version(stage)))
    if problems:
        shutil.rmtree(stage)
        raise InstallProblem("the unpacked pack does not match its manifest, so "
                             "nothing was installed:\n  " + "\n  ".join(problems))
    n = len(asset_files(stage))
    old = None
    if root.exists():
        old = root.parent / (".%s-replaced" % PACK_ID)
        if old.exists():
            shutil.rmtree(old)
        root.rename(old)
    stage.rename(root)
    if old is not None:
        shutil.rmtree(old)
    say("installed %s %s: %d files at %s" % (PACK_ID, pack_version(root), n, root))
    say(REVIEW_SENTENCE)
    # The reader in THIS process sees it now; another process (the server,
    # when this ran from the command line) sees it on its next lookup, because
    # `load()` and `definitions()` re-check an absent pack rather than caching
    # the absence.
    if root == Path(PACK_DIR):
        load(force=True)
        definitions(force=True)
    return {"ok": True, "version": pack_version(root), "changed": True, "files": n}


# ---- an install started from the Settings page, in its own process ----------

def record_path():
    return STATE_DIR / ("knowledge-pack-%s-install.json" % PACK_ID)


def log_path():
    return STATE_DIR / ("knowledge-pack-%s-install.log" % PACK_ID)


def read_record():
    try:
        data = json.loads(record_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_record(data):
    path = record_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _alive(pid):
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except (OSError, ValueError, TypeError):
        return False
    return True


def _log_tail(n=12):
    try:
        lines = log_path().read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return [ln for ln in lines if ln.strip()][-n:]


def install_status(pack_dir=None):
    """What the Settings page asks on every render. Reads the disk, starts
    nothing, and never trusts the cached `load()`: a pack that arrived a
    moment ago must read as installed on the next poll."""
    root = Path(pack_dir or PACK_DIR)
    ver = pack_version(root)
    counts = pack_counts(root)
    rec = read_record()
    state = rec.get("state") or ("done" if ver else "never")
    running = state == "running" and _alive(rec.get("pid"))
    error = rec.get("error") or ""
    if state == "running" and not running:
        state = "failed"
        error = error or "the install stopped without finishing"
    return {"installed": bool(ver), "version": ver,
            "plates": int(counts.get("plates") or 0),
            "regions": int(counts.get("regions") or 0),
            "running": running, "state": state,
            "started": rec.get("started"), "finished": rec.get("finished"),
            "error": error, "path": str(root),
            "log_tail": _log_tail() if state != "never" else []}


def start_install(feed_url=None, popen=None, python_bin=None):
    """Start `--install` in its OWN process and answer at once, the shape of
    the caption engine's install: an HTTP handler must not hold a 44 MB
    download, and a server restart must not kill one halfway."""
    now = install_status()
    if now["running"]:
        return {"ok": False, "error": "an install is already going",
                "pid": read_record().get("pid")}
    if now["installed"]:
        return {"ok": False, "error": "the pack is already installed (%s)" % now["version"]}
    url = feed_url or default_feed()
    if not url:
        return {"ok": False, "error": "the update feed is off in this config, so "
                                      "there is nowhere to fetch the pack from"}
    logfile = log_path()
    logfile.parent.mkdir(parents=True, exist_ok=True)
    argv = [python_bin or sys.executable, str(Path(__file__).resolve()),
            "--install", "--feed", url]
    with open(logfile, "a", encoding="utf-8") as handle:
        handle.write("\n==== %s ====\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
        handle.flush()
        proc = (popen or subprocess.Popen)(
            argv, stdout=handle, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, start_new_session=True,
            cwd=str(Path(__file__).resolve().parent))
    pid = getattr(proc, "pid", None)
    write_record({"pid": pid, "state": "running",
                  "started": time.strftime("%Y-%m-%d %H:%M:%S"), "log": str(logfile)})
    return {"ok": True, "pid": pid, "log": str(logfile)}


def _say(*bits):
    print(*bits)
    sys.stdout.flush()


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="the brain-region picture pack: fetch it, check it, package it")
    ap.add_argument("--install", action="store_true",
                    help="fetch the pack the update feed names and install it")
    ap.add_argument("--start", action="store_true",
                    help="the same install in its own process; answer at once")
    ap.add_argument("--status", action="store_true",
                    help="is it installed, and how does an install stand")
    ap.add_argument("--asset", metavar="OUT_DIR",
                    help="write the release asset for the pack in this tree")
    ap.add_argument("--verify", metavar="DIR", nargs="?", const="",
                    help="check an installed copy against its manifest")
    ap.add_argument("--feed", default="", help="the update feed URL (default: the config's)")
    ap.add_argument("--pack-dir", default="", help="where the pack is (default: the kit's)")
    ap.add_argument("--force", action="store_true",
                    help="install even though this version is already there")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    pack_dir = Path(a.pack_dir).expanduser() if a.pack_dir else None

    if a.asset:
        path = write_asset(a.asset, pack_dir)
        facts = asset_facts(path)
        _say("%s: %d files, %.1f MB, sha256 %s"
             % (path, len(asset_files(pack_dir)), facts["bytes"] / 1048576,
                facts["sha256"][:16]))
        return 0
    if a.verify is not None:
        where = Path(a.verify).expanduser() if a.verify else (pack_dir or PACK_DIR)
        problems = unpack_problems(where)
        if problems:
            _say("%s does not match its manifest:" % where)
            for line in problems:
                _say("  " + line)
            return 1
        _say("%s matches its manifest: %d files, version %s"
             % (where, len(asset_files(where)), pack_version(where)))
        return 0
    if a.status:
        data = install_status(pack_dir)
        if a.json:
            json.dump(data, sys.stdout, indent=2, sort_keys=True)
            sys.stdout.write("\n")
        elif data["installed"]:
            _say("installed: %s %s, %d plates for %d regions, at %s"
                 % (PACK_ID, data["version"], data["plates"], data["regions"], data["path"]))
        else:
            _say("not installed (%s)%s" % (data["state"],
                                           ": " + data["error"] if data["error"] else ""))
        return 0
    if a.start:
        out = start_install(a.feed or None)
        if a.json:
            json.dump(out, sys.stdout)
            sys.stdout.write("\n")
        else:
            _say(out.get("log") if out.get("ok") else out.get("error"))
        return 0 if out.get("ok") else 1
    if a.install:
        # The record is written on the way out, for the Settings page that
        # started this in its own process; harmless when a person ran it.
        try:
            out = install(a.feed or None, pack_dir, say=_say, force=a.force)
        except InstallProblem as exc:
            _say("not installed: %s" % exc)
            write_record(dict(read_record(), state="failed", error=str(exc),
                              finished=time.strftime("%Y-%m-%d %H:%M:%S")))
            return 1
        write_record(dict(read_record(), state="done", error="",
                          version=out["version"],
                          finished=time.strftime("%Y-%m-%d %H:%M:%S")))
        return 0
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
