#!/usr/bin/env python3
"""Every capability a course pack claims either HAS its data or says it has none.

    python3 server/verify_course.py                 # every course
    python3 server/verify_course.py PSY101          # one course
    python3 server/verify_course.py --quiet         # rows only when something is partial

(The example code is invented. This file SHIPS, and `build_kit.py` refuses a
real module code in anything shipped, because a module code is somebody's
enrolment.)

Why this exists. Brain-region pictures were missing from an entire course for
weeks and nobody could tell, because nothing anywhere says a course lacks a
feature's DATA. The reader has no way to distinguish "this course has no anatomy
entries" from "the picture feature is broken", so it reads as the second, and the
bug report that eventually arrives is about the wrong half of the system.

🔴 **The rule this enforces**: a capability's data is either present for a course,
or somebody must SAY it is absent. Silence is what made the miss look like a
defect. So the script prints the rule whenever it finds a gap, rather than only
a number.

🔴 **Exit 1 on `partial` ONLY, and that restraint is the whole design.** A course
that does not use a capability is not an error. If this cried wolf about every
course without narrated packages it would be muted inside a week and the one
real signal would go with it. So:

  - `none`         the course has nothing that wants this capability. Fine.
  - `ok`           everything that wants it has it. Fine.
  - `partial n/m`  some of what wants it has it, INCLUDING none of it. Exit 1.

The difference between `none` and `partial (0 of m)` is the entire point: a
course with no brain regions in its lessons needs no anatomy entries, and a
course whose lessons are full of them and whose glossary has none is the exact
state that looks like a broken feature.

**Who acts on it**: the manager on data gaps (a harvest, a migration), the coder
on code gaps. This prints evidence, not a to-do list it owns.

**When it runs**: beside the checks that already gate a release
(`verify_notes.py`, `verify_packages.py`, `smoke_kit.py`), and after any course
import or rebuild, which is when a new pack's gaps are cheapest to fill.
"""

import argparse
import json
import sys
from pathlib import Path

SERVER = Path(__file__).resolve().parent
sys.path.insert(0, str(SERVER))

import study_server as S           # noqa: E402
import split_lessons               # noqa: E402

# 🔴 Both optional, because this script SHIPS in the kit and the kit is a subset.
# `readings.py` is in the manifest and `verify_packages.py` is not, so the
# packages probe has to be able to say "I cannot check that here" rather than
# taking the whole report down with an ImportError. A gate that refuses to run
# reports nothing about the five capabilities it could have checked.
try:
    import verify_packages         # noqa: E402
except ImportError:                # pragma: no cover - present in this repo
    verify_packages = None

try:
    import readings as _readings   # noqa: E402
except ImportError:                # pragma: no cover - readings.py ships with it
    _readings = None

# 🔴 OPTIONAL FOR A REASON THAT IS CHECKED, NOT ASSUMED: `build_kit.py`'s
# manifest ships `verify_course.py` and does NOT ship `regionpack.py` or the
# pack data. An unguarded import here would raise inside the kit and take the
# whole report down, which is the exact failure the block above exists to
# prevent. 🟢 **Absent is also the HONEST answer there**: no pack means no pack
# coverage, so the probe falls back to asking only about the glossary, which is
# what it asked before this and is right for a machine with no pack.
try:
    import regionpack               # noqa: E402
except ImportError:                # pragma: no cover - present in this repo
    regionpack = None


# --------------------------------------------------------------------------
# One row of the report
# --------------------------------------------------------------------------
#
# `wanted` is the number of things in this course that WANT the capability and
# `served` the number that have it. Every capability answers in those terms, so
# the verdict is one rule in one place rather than each probe deciding for
# itself what counts as broken.

class Row:
    def __init__(self, capability, wanted, served, note=""):
        self.capability = capability
        self.wanted = wanted
        self.served = served
        self.note = note

    @property
    def state(self):
        if not self.wanted:
            return "none"
        return "ok" if self.served >= self.wanted else "partial"

    @property
    def label(self):
        if self.state == "partial":
            return "partial (%d of %d)" % (self.served, self.wanted)
        return self.state

    @property
    def bad(self):
        return self.state == "partial"


# --------------------------------------------------------------------------
# The capabilities
# --------------------------------------------------------------------------

def region_vocabulary(courses):
    """Every phrase any course's glossary flags as anatomy.

    🔴 Derived, never hardcoded, and that is what makes the check honest. A fixed
    list of brain regions would be this project's opinion about neuroanatomy and
    would go stale; borrowing the vocabulary from whichever courses HAVE anatomy
    entries means the check measures one course against the standard another has
    already met. If no course has any, the vocabulary is empty and every course
    reports `none`, which is correct: nothing here knows what a region is yet.
    """
    vocab = set()
    for folder in courses.values():
        for term, entry in (read_glossary(folder) or {}).items():
            if isinstance(entry, dict) and entry.get("anatomy"):
                vocab.add(term.strip().lower())
    return {v for v in vocab if len(v) > 2}


def read_glossary(folder):
    try:
        data = json.loads((folder / "glossary.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def lesson_text(folder):
    """All of a course's lesson prose, lowercased, as one string."""
    parts = []
    for path in split_lessons.lessons_in(folder):
        try:
            parts.append(path.read_text(encoding="utf-8"))
        except OSError:
            continue
    return "\n".join(parts).lower()


def pack_covers(term):
    """Can the shared pack put a picture beside this term on its own?

    🔴 **THE READER'S OWN QUESTION, ASKED THE READER'S OWN WAY.** `study_server`
    resolves a term through `regionpack.plates()`, which returns `[]` for
    anything the pack does not cover, so this asks the same function rather than
    re-deriving coverage from `regions.json`. **A probe that computes coverage
    its own way answers a question the reader never asks.**

    ⚠️ **PLATES, NOT DEFINITIONS.** The pack can carry words for a region with no
    picture for it, and this capability is called `region pictures`.
    """
    if regionpack is None:
        return False
    try:
        return bool(regionpack.plates(term))
    except Exception:              # pragma: no cover - a malformed pack
        # 🔴 A broken pack must not take down the other five capabilities, for
        # the same reason the imports above are guarded. Uncovered is the safe
        # direction: it can only make this row look WORSE, never better.
        return False


def cap_region_pictures(ctx):
    """A brain region named in a lesson has a picture to show beside it.

    The one this script was written for. A course with no way to show one gives
    a plain definition where another course gives an illustration, and nothing
    says why.

    🔴🔴 **THE QUESTION CHANGED AT `d9cd336` AND THIS PROBE DID NOT, FOR A DAY.**
    It used to ask *"does this course's glossary carry an anatomy entry for each
    region named in its lessons?"* **Once the shared pack became a lookup source,
    the reader got pictures for all 22 regions of a course this check still
    reported as `partial (0 of 22)`.** 🟢 **It now asks what the reader actually
    experiences: CAN A PICTURE BE SHOWN, from the course's own glossary OR from
    the pack.**

    ⚠️ **THE ANATOMY DIMENSION IS KEPT DELIBERATELY, not left behind.** The flag
    is still what gates the Wikipedia fallback, so a course that tags its own
    terms still buys something the pack cannot give it, and a region in NEITHER
    place is still the gap this check exists to find.

    🔴 **WHY THIS MATTERED ENOUGH TO FIX:** a check firing on a non-problem gets
    muted, and `lesson outlines partial (0 of 38)` sits in the same output and is
    a REAL gap. The false row is what teaches a reader to skim past the true one.
    """
    vocab = ctx["vocab"]
    if not vocab:
        # Said in full, once, by main(): six identical `none` rows read like a
        # clean bill rather than like a check with nothing to check against.
        return Row("region pictures", 0, 0,
                   "no course defines any anatomy terms, so this is unassessed")
    text = ctx["lesson_text"]
    named = {t for t in vocab if t in text}
    mine = {t.strip().lower() for t, e in read_glossary(ctx["folder"]).items()
            if isinstance(e, dict) and e.get("anatomy")}
    # 🟢 Only the terms the glossary does not already answer for are put to the
    # pack, so a tagged course costs no pack lookups at all.
    from_pack = {t for t in named - mine if pack_covers(t)}
    missing = sorted(named - mine - from_pack)
    note = ""
    if missing:
        note = ("named in lessons, no picture from this course's glossary or the "
                "shared pack: " + ", ".join(missing[:6]))
        if len(missing) > 6:
            note += " (+%d more)" % (len(missing) - 6)
    elif from_pack:
        # 🔴 A POSITIVE RESULT, because silence on success is what let the stale
        # premise live for a day. This row says WHERE the pictures come from, so
        # a course carried entirely by the pack cannot look like a course that
        # tagged its own terms.
        note = ("%d of %d come from the shared pack rather than this course's "
                "own glossary" % (len(from_pack), len(named)))
    return Row("region pictures", len(named), len(named) - len(missing), note)


def cap_local_materials(ctx):
    """A lesson whose slides live on this machine can actually find them."""
    cfg = ctx["cfg"]
    if S.materials_source(cfg) != "local":
        return Row("local materials", 0, 0, "this course links out instead")
    rep = S.local_materials_report(cfg)
    if not rep["exists"]:
        return Row("local materials", rep["parts"], 0,
                   "the configured folder is not there: %s" % rep["dir"])
    served = max(rep["found"].values()) if rep["found"] else 0
    note = ""
    # 🔴 The GRADE reads the best kind and the NOTE reads every kind, and the two
    # answer different questions on purpose. Grading per kind would redden a
    # course whose source simply has no transcripts, which is a fact about the
    # course rather than a gap, and a permanently red row is a muted row. But a
    # bare `ok` then hides "slides 50, transcript 0", so the breakdown prints
    # whenever ANY kind is short, not only when the best one is.
    if rep["parts"] and any(v < rep["parts"] for v in rep["found"].values()):
        note = "found per kind: " + ", ".join(
            "%s %d" % (k, v) for k, v in sorted(rep["found"].items()))
    return Row("local materials", rep["parts"], served, note)


def cap_packages(ctx):
    """Narrated packages are complete. Delegates rather than re-implements."""
    if verify_packages is None:
        return Row("narrated packages", 0, 0,
                   "verify_packages.py is not installed here, so this is unchecked")
    pkgs = verify_packages.packages(ctx["folder"])
    if not pkgs:
        return Row("narrated packages", 0, 0, "this course has none")
    damaged = [p.name for p in pkgs if verify_packages.referenced(p)]
    note = ""
    if damaged:
        note = ("incomplete: " + ", ".join(sorted(damaged)[:4])
                + " (verify_packages.py names every missing asset)")
    return Row("narrated packages", len(pkgs), len(pkgs) - len(damaged), note)


def cap_outlines(ctx):
    """Each lesson has the working outline `<DOC>-outline.md` that the
    whole-course digest prefers over the lesson's opening prose."""
    docs = ctx["docs"]
    if not docs:
        return Row("lesson outlines", 0, 0, "no lessons")
    served = sum(1 for d in docs if (ctx["folder"] / (d + "-outline.md")).is_file())
    note = ""
    if served < len(docs):
        missing = sorted(d for d in docs
                         if not (ctx["folder"] / (d + "-outline.md")).is_file())
        note = "no outline: " + ", ".join(missing[:6])
        if len(missing) > 6:
            note += " (+%d more)" % (len(missing) - 6)
    return Row("lesson outlines", len(docs), served, note)


def cap_readings(ctx):
    """A recorded reading has a PDF filed against it, so the link opens
    something rather than naming a paper the reader has to go and find."""
    if _readings is None:
        return Row("readings", 0, 0, "readings.py is not importable")
    rows = _readings.read_all(ctx["folder"])
    if not rows:
        return Row("readings", 0, 0, "this course records none")
    filed = sum(1 for r in rows if r.get("file"))
    note = "" if filed >= len(rows) else "%d recorded with no PDF filed" % (len(rows) - filed)
    return Row("readings", len(rows), filed, note)


IDENTITY_FIELDS = ("class_name", "project_link", "store_prefix")


def cap_identity(ctx):
    """The three fields a course must declare rather than inherit.

    🔴 This is the capability with the most history behind it. `b058c03` and
    `6a01f3e` closed the leak where a machine-global `class_name` and
    `project_link` were handed to every course, which filed a mindfulness note
    under Affective Disorders. Closing it correctly EMPTIES those fields for a
    course that never set its own, which is honest and also means a note loses
    its `project:` line until somebody fills them in. That is a data gap with no
    other alarm on it, so it is one here.
    """
    facts = S.module_facts_of(ctx["folder"])
    served = sum(1 for f in IDENTITY_FIELDS if str(facts.get(f) or "").strip())
    note = ""
    if served < len(IDENTITY_FIELDS):
        missing = [f for f in IDENTITY_FIELDS if not str(facts.get(f) or "").strip()]
        note = "not declared: " + ", ".join(missing)
    return Row("per-course identity", len(IDENTITY_FIELDS), served, note)


CAPABILITIES = (cap_region_pictures, cap_local_materials, cap_packages,
                cap_outlines, cap_readings, cap_identity)


# --------------------------------------------------------------------------
# The run
# --------------------------------------------------------------------------

def check_course(base_cfg, code, folder, vocab):
    try:
        cfg = S.module_cfg(base_cfg, code)
    except ValueError:
        cfg = dict(base_cfg)
        cfg["notes_dir"] = folder
        cfg["module"] = code
    docs = sorted(S.lesson_meta_index(cfg).keys())
    ctx = {"cfg": cfg, "code": code, "folder": folder, "docs": docs,
           "vocab": vocab, "lesson_text": lesson_text(folder)}
    rows = []
    for probe in CAPABILITIES:
        try:
            rows.append(probe(ctx))
        except Exception as exc:                       # a probe must never stop the report
            rows.append(Row(probe.__name__, 1, 0, "the check itself failed: %s" % exc))
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Report which capabilities each course has the data for.")
    ap.add_argument("course", nargs="*", help="course codes (default: all)")
    ap.add_argument("--quiet", action="store_true",
                    help="print a course only when something is partial")
    ap.add_argument("--config", default=None, help="config path")
    args = ap.parse_args(argv)

    base_cfg = S.load_config(Path(args.config).expanduser() if args.config
                             else S.CONFIG_PATH)
    courses = S.resolve_modules(base_cfg)
    if args.course:
        unknown = [c for c in args.course if c not in courses]
        if unknown:
            print("no such course: %s" % ", ".join(unknown))
            return 2
        courses = {c: courses[c] for c in args.course}
    if not courses:
        print("no courses found")
        return 0

    # 🔴 The vocabulary comes from EVERY course, not only the ones being
    # reported, so `verify_course.py FMND` measures against the same standard as
    # a full run. A check whose verdict depends on its arguments is a check
    # nobody can quote.
    vocab = region_vocabulary(S.resolve_modules(base_cfg))

    # 🔴 The derived vocabulary is this check's own input, and a derived check
    # goes QUIET when its input regresses rather than red: strip every anatomy
    # flag from every glossary and each course reports `none`, which reads like
    # a clean bill while the regions are still named in the lessons with no
    # picture to show. So the dependency is stated once, here, whenever it is
    # empty. It does not fail the run, because a machine whose courses have no
    # anatomy terms is not broken; it just cannot be told anything about them.
    if not vocab:
        print("\n  note: no course defines any anatomy terms, so region coverage"
              "\n  cannot be assessed for any course in this report. The"
              "\n  vocabulary is derived from the glossaries rather than"
              "\n  hardcoded, so it is empty exactly when they are.")

    problems = 0
    for code in sorted(courses):
        rows = check_course(base_cfg, code, courses[code], vocab)
        bad = [r for r in rows if r.bad]
        problems += len(bad)
        if args.quiet and not bad:
            continue
        print("\n%s" % code)
        width = max(len(r.capability) for r in rows)
        for r in rows:
            mark = "🔴" if r.bad else "  "
            print("  %s %-*s  %-16s %s" % (mark, width, r.capability, r.label, r.note))

    if not problems:
        print("\n  clean: every capability is either populated or cleanly absent.")
        return 0

    print("\n  🔴 %d capability(ies) are half-populated." % problems)
    print("\n  A capability's data is either present for a course, or the reader"
          "\n  must SAY it is absent. A half-populated one is the state that looks"
          "\n  like a broken feature, which is why it is the only one that fails."
          "\n\n  Data gaps are the manager's (a harvest, a migration); code gaps are"
          "\n  the coder's. This is evidence for a queue entry, not a fix.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
