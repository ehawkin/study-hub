#!/usr/bin/env python3
"""How close a lesson's prose sits to the words it was written from.

    python3 server/verbatim.py courses/PSY101          # one course, as a report

`verify_notes.py` runs this as a section, which is the point of it: NOTE-SPEC §B
forbade naming the lecturer and said NOTHING about reusing the lecturer's
sentences, and no gate here has ever measured similarity to a source. 164
passages across 62 lessons accumulated with every check green.

⚠️ **A person cannot see a borrowed sentence inside a four-thousand-word page.**
That is what makes this a job for a check rather than for care, and it is why the
absence of the check was invisible for as long as it was.

🔴 **IT REPORTS AND IT DOES NOT BLOCK, by instruction.** 62 of 110 lessons carried
a passage when this was specified, so a blocking gate would stop all lesson work
on the day it shipped. The flip to blocking is its own small unit, taken when the
rewrites are finished and the count is near zero. `verify_notes.py` says so on
every run rather than leaving a reader to infer it from a clean exit code.

**THE INSTRUMENT THIS WAS BUILT FROM** is `_admin/work/verbatim/scan_verbatim.py`,
which produced the pre-rewrite inventory in `baseline-20260911.json`. That one is
a measurement and says so in its own docstring; this is the gate. The method is
kept deliberately identical so the two can be compared against each other, and
they were, on the live corpus, the day this shipped.

## 🔴 The two controls, because a detector that has only run where the answer is unknown proves nothing

**A planted passage must make it fire**, or the test passes identically on a check
that was switched off. **And the same comparison run on MISMATCHED pairs must
return zero**, or the threshold is measuring nothing but the fact that two people
wrote about the same subject. Both live in `test_verbatim.py`; the second is the
one `scan_verbatim.py`'s own docstring insists on.
"""
import difflib
import json
import os
import pathlib
import re
import shutil
import sys

# 🔴 THE SIBLINGS ARE IMPORTED AT MODULE LEVEL, DELIBERATELY, and this is about
# the kit rather than about style. `test_every_shipped_module_can_actually_
# IMPORT_on_the_far_side` reads MODULE-LEVEL imports to decide what the kit must
# carry; an import hidden inside a function is invisible to it, so the module
# would ship without `transcripts.py` and die on a recipient's machine the first
# time somebody ran the gate. The hole is in the test and it is not mine to
# exploit.
# ⚠️ The path insert is explicit for the same reason `PROJECT-NOTES` records:
# `sys.path` is process-global mutable state, and an import that works only
# because some earlier call happened to extend the path is an import that fails
# the day that call moves.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import split_lessons as SPLIT
import study_server as S
import transcripts as T

# 🔴 THE THRESHOLD IS A NAMED CONSTANT AND ITS REASONING LIVES BESIDE IT, rather
# than a literal sitting in a condition where nobody can see what it was chosen
# against. 0.90 is the character-level SequenceMatcher ratio between a source
# sentence and a lesson sentence, both normalised to lowercase words.
#
# 🟢 WHAT IT WAS CHOSEN AGAINST, and the reason it is not arbitrary: the
# mismatched-pair control returns ZERO here. Run every lesson against a DIFFERENT
# part's transcript and no pair reaches 0.90, so a hit at this threshold is not
# two people writing about the same subject, it is one sentence carried across.
# 🔴 IF THIS NUMBER MOVES, THE CONTROL IS RE-RUN. A threshold changed without
# re-running the control is a threshold with no evidence behind it.
THRESHOLD = 0.90

# A sentence shorter than this is a phrase, and short phrases collide by accident:
# "This is an important point to remember" is not borrowed, it is English. Eight
# words is the instrument's own floor and the corpus was inventoried at it.
MIN_WORDS = 8

# The three exclusions, kept from the instrument and named here so a future glob
# cannot quietly widen them. Each one inflates the count, and this project has
# been caught by a glob that reached a backup tree before.
#
#   *.bak                    the timestamped backups this project writes everywhere
#   .snapshots/              the reader's own per-day lesson snapshots
#   *-transcript-repair-backup-*/   a whole second copy of one course's materials,
#                            which would double every hit in it
#
# 🟢 EXCLUDED BY RULE RATHER THAN BY ACCIDENT. The globs below are non-recursive,
# so today these directories are missed anyway; that is luck, and luck is what
# changes when somebody adds a `**`.
EXCLUDE_DIRS = (".snapshots",)
EXCLUDE_DIR_RE = re.compile(r"-transcript-repair-backup-\d")


def excluded(path):
    """Why this path is not part of the corpus, or None when it is.

    Returns the REASON rather than a boolean, so the caller can print which rule
    fired. An exclusion nobody can see the reason for is an exclusion nobody
    checks."""
    p = pathlib.Path(path)
    if ".bak" in p.name:
        return "a .bak backup"
    for part in p.parts[:-1]:
        if part in EXCLUDE_DIRS:
            return "under %s/" % part
        if EXCLUDE_DIR_RE.search(part):
            return "under the transcript-repair backup tree"
    return None


def find_pdftotext(named=None):
    """`pdftotext`, found rather than assumed: `transcripts.find_pdftotext`,
    the one finder every reader of a transcript PDF goes through.

    🔴 Returns None when there is none, and the caller must report that rather
    than counting zero passages. A check that could not run and a check that
    found nothing print the same number, and only one of them is good news."""
    return T.find_pdftotext(named)


def sentences(text):
    """The sentences long enough to be worth comparing, in order.

    Split on terminal punctuation, which is the instrument's rule. Anything
    shorter than MIN_WORDS is dropped here rather than filtered later, so the
    floor exists in one place."""
    text = re.sub(r"\s+", " ", text or "")
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text)
            if len(s.split()) >= MIN_WORDS]


def normalise(sentence):
    """Lowercase words, punctuation gone, as one space-joined string.

    Both sides go through this, so a difference in quoting, casing or an added
    comma is not what the ratio measures."""
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", (sentence or "").lower()).split())


def lesson_sentences(source):
    """Every sentence of a lesson's visible prose, with the block it sits in.

    🔴 BLOCK BY BLOCK, NEVER OVER A JOINED PAGE. Joining the blocks with a space
    before splitting into sentences merges the end of one paragraph with the
    start of the next and manufactures sentences nobody wrote. Two sessions here
    hit exactly that failure an hour apart on 2026-09-14, in two different tools,
    and both gave a confident wrong answer.

    The server's own `find_blocks` and `html_to_text` do the reading, so this
    measures the text the reader actually sees and the marks actually anchor to,
    rather than a second opinion about what the HTML means."""
    blocks = S.find_blocks(source)
    texts = [S.html_to_text(b["inner"]) for b in blocks]
    out = []
    for i, text in enumerate(texts):
        for sent in sentences(text):
            out.append((i, sent))
        # 🟢 A DEFINITION ROW IS ONE UNIT, and joining `dt` to its `dd` is
        # reading the structure rather than flattening it. The page numbers them
        # as two blocks because an edit has to address them separately; the
        # reader sees one line, and a glossary entry that copies the lecture's
        # own definition is exactly a section B4 case.
        # ⚠️ Measured: `Pregenual / In front of the knee of the corpus callosum.`
        # scores 0.851 as a bare definition and 0.946 once the term it defines is
        # in front of it, against `Pregenual means in front of the knee of the
        # corpus callosum.` Two real passages were invisible without this.
        # 🔴 The join is between a `dt` and the `dd` DIRECTLY after it, and
        # nothing else. Any wider rule is the document-wide flatten this file
        # exists to avoid.
        if (blocks[i]["tag"] == "dt" and i + 1 < len(blocks)
                and blocks[i + 1]["tag"] == "dd"):
            joined = (text + " " + texts[i + 1]).strip()
            for sent in sentences(joined):
                out.append((i + 1, sent))
    return out


def source_text(materials_dir, doc_id, binary=None):
    """The words the lecture spoke, for one part, or "" when there are none.

    Two shapes, because two kinds of course land here. A course exported from a
    Rise package carries per-slide transcripts as JSON; a course that hands over
    PDFs carries one transcript PDF per part, and `transcripts.transcript_for`
    is this repo's one answer to where that file is.

    ⚠️ "" is returned rather than raised, and the caller counts it: a part with no
    readable source is a part this check says nothing about, and saying nothing
    has to be visible."""
    materials_dir = pathlib.Path(materials_dir)
    sidecar = materials_dir / "source" / doc_id / "transcript.json"
    if sidecar.is_file() and not excluded(sidecar):
        try:
            data = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return ""
        return " ".join(
            s.get("p", "") for s in data.get("slides", [])
            if not str(s.get("p", "")).strip().lower().startswith("no audio"))

    pdf = T.transcript_for(materials_dir, doc_id)
    if not pdf or excluded(pdf):
        return ""
    # 🔴 `-layout`, AND IT IS NOT COSMETIC: it is what lets the footer stripping
    # below fire at all. See `transcripts.pdf_text`, where the measurement is.
    raw = T.pdf_text(pdf, binary=binary or find_pdftotext() or T.PDFTOTEXT,
                     layout=True)
    # 🔴 `spoken_text` IS NOT A TIDYING STEP EITHER, IT IS WHAT MAKES THE RATIO
    # MEAN ANYTHING. A running footer carries no full stop, so the sentence
    # splitter glues `PAGE | 5 (c) King's College London` onto the front of the
    # next sentence, and that sentence's best ratio drops below the threshold.
    # Measured on one course's `W4-T2-P2`: a passage the lesson reuses almost word
    # for word scores 0.911 against the clean sentence and 0.45 against the one
    # carrying the footer, so the hit disappears.
    # ⚠️ It is the same shape as the defect on the LESSON side below, arriving
    # from the other direction: unpunctuated furniture merging into real prose.
    # `spoken_text` also drops the cover page and the slide markers, which are
    # printed and never spoken, so what is compared is what the lecture said.
    return T.spoken_text(raw)


def matches(lesson_pairs, source, threshold=THRESHOLD, fast=True):
    """Every source sentence that survives almost intact inside this lesson.

    One row per source sentence, keyed on the source rather than on the lesson,
    because the question is "did this teaching sentence get carried across" and
    a lesson sentence can only answer it once.

    🔴 `fast=False` COMPARES EVERY PAIR EXACTLY and is what the cheap path is
    checked against. It is about forty times slower, which is the whole reason
    the cheap path exists.
    """
    lesson_pairs = list(lesson_pairs)
    keys = [normalise(s) for _, s in lesson_pairs]
    out = []
    # 🔴 `autojunk=False`, AND IT IS NOT A TUNING KNOB. With it on, difflib
    # treats any character appearing in more than 1% of `seq2` as junk once
    # `seq2` reaches 200 characters, which for an English sentence means the
    # common letters. Two things follow, and the second is the one that bit:
    # the score stops being about the whole sentence, and **`ratio()` stops
    # being symmetric**, because the heuristic reads `seq2` alone.
    #
    # ⚠️ MEASURED ON THE LIVE CORPUS 2026-09-15, one variable at a time, with
    # everything else held fixed - which is the only reason the second row can
    # be read as being about the argument order:
    #
    #     autojunk   source as seq2    passages found
    #     -------------------------------------------
    #     on         yes               162
    #     on         no                164   <- the SAME comparison, swapped
    #     off        yes               171
    #     off        no                171   <- order no longer matters
    #
    # 🔴 With the heuristic on, swapping the arguments moves the count in BOTH
    # directions, and it hides 7 to 9 real passages either way. Turning it off
    # is what makes the answer a property of the two sentences rather than of
    # which one was handed over second. `captions.py` and `rig_anchors.py` here
    # already pass `autojunk=False`, for the same reason written up differently.
    sm = difflib.SequenceMatcher(autojunk=False)
    for src in sentences(source):
        sn = normalise(src)
        if len(sn.split()) < MIN_WORDS:
            continue
        # 🔴 THE SOURCE SENTENCE IS `seq2`, AND THAT IS FOR SPEED RATHER THAN
        # SYMMETRY: `SequenceMatcher` indexes seq2 and caches its character
        # counts, and `set_seq1` leaves both alone. Setting it the other way
        # round rebuilds the index on every pair.
        sm.set_seq2(sn)
        best, at = 0.0, -1
        for i, key in enumerate(keys):
            sm.set_seq1(key)
            # 🟢 THE CHEAP PATH CANNOT LOSE A HIT, AND THAT IS BY CONSTRUCTION
            # RATHER THAN BY MEASUREMENT. `real_quick_ratio` (lengths only) and
            # `quick_ratio` (character counts) are difflib's own documented
            # UPPER BOUNDS on `ratio`, so a pair they put below the threshold
            # cannot reach it exactly.
            # 🔴 THE PREFILTER THAT SAT HERE FIRST WAS A WORD-SET OVERLAP AT
            # 0.72, AND IT LOST A REAL HIT: 44 rows with it against 45 without
            # on one course here, and 6 across the three. A cheap gate is only allowed if it is a
            # bound, and a word-set overlap is not one - characters match across
            # word boundaries, so a sentence whose every word carries one typo
            # can score high on characters and share no whole word at all.
            if fast and (sm.real_quick_ratio() < threshold
                         or sm.quick_ratio() < threshold):
                continue
            ratio = sm.ratio()
            if ratio > best:
                best, at = ratio, i
        if best >= threshold and at >= 0:
            block, sent = lesson_pairs[at]
            out.append({"ratio": round(best, 3), "block": block,
                        "source": src.strip(), "lesson": sent.strip()})
    return out


def materials_for(notes_dir):
    """`materials/<CODE>/` beside `courses/`, which is where a course's downloads
    live by the same rule `study_server.local_materials_dir` uses.

    Returned whether or not it exists: a sweep pointed at a copy of a course has
    no materials beside it, and the caller has to be able to say so."""
    notes_dir = pathlib.Path(notes_dir).resolve()
    return notes_dir.parent.parent / "materials" / notes_dir.name


def scan(lessons, materials_dir, threshold=THRESHOLD, fast=True, binary=None):
    """Compare every lesson against its own part's transcript.

    Returns (rows, compared, unsourced): the passages at or above the threshold,
    how many lessons were actually compared, and the lessons that had no readable
    source. 🔴 The last one is not a detail. A run where nothing could be read
    prints the same zero as a run where nothing was found, and this is what lets
    the caller tell those two apart."""
    rows, compared, unsourced = [], 0, []
    for path in lessons:
        path = pathlib.Path(path)
        if excluded(path):
            continue
        text = path.read_text(encoding="utf-8")
        # 🔴 THE DOC ID IS READ OUT OF THE LESSON, NEVER OFF ITS FILENAME. The
        # instrument this came from matched `W\d+-T\d+-P\d+` against the
        # basename, which is THIS module's naming and silently skips a course
        # that numbers its parts any other way - the same defect
        # `is_lesson_file` was written to end. `read_content` is the project's
        # one answer to "which part is this", and it refuses rather than
        # guessing when the block is missing.
        try:
            meta, _, _ = SPLIT.read_content(text, path.name)
        except SPLIT.Problem as exc:
            unsourced.append((path.name, str(exc).split(": ", 1)[-1]))
            continue
        doc = str(meta["doc"])
        src = source_text(materials_dir, doc, binary=binary)
        if not src.strip():
            unsourced.append((path.name, "no transcript found"))
            continue
        compared += 1
        for row in matches(lesson_sentences(text), src, threshold, fast):
            row.update(doc=doc, file=path.name)
            rows.append(row)
    rows.sort(key=lambda r: (-r["ratio"], r["file"]))
    return rows, compared, unsourced


def report(rows, compared, unsourced, threshold=THRESHOLD, out=None):
    """Print the result, INCLUDING when there is nothing to report.

    🔴 Silence on success makes a passing check indistinguishable from a missing
    one. This check was itself missing for months and nothing anywhere said so,
    which is the whole reason the positive line is not optional."""
    say = (out or sys.stdout).write
    for r in rows:
        say("  %-22s b%-4d %.3f  %s\n" % (r["file"][:22], r["block"],
                                          r["ratio"], r["source"][:80]))
        say("  %-22s %6s %5s  %s\n" % ("", "", "", r["lesson"][:80]))
    if unsourced:
        say("  no source for %d lesson%s: %s\n"
            % (len(unsourced), "" if len(unsourced) == 1 else "s",
               ", ".join("%s (%s)" % (n, w) for n, w in unsourced[:6])
               + (", ..." if len(unsourced) > 6 else "")))
    say("  %d lesson%s compared, %d passage%s at or above %.2f\n"
        % (compared, "" if compared == 1 else "s",
           len(rows), "" if len(rows) == 1 else "s", threshold))
    return len(rows)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print("usage: python3 server/verbatim.py <course-dir> [materials-dir]")
        return 2
    notes = pathlib.Path(argv[0]).expanduser().resolve()
    mats = (pathlib.Path(argv[1]).expanduser().resolve() if len(argv) > 1
            else materials_for(notes))
    print("lessons:   %s" % notes)
    print("materials: %s%s" % (mats, "" if mats.is_dir() else "   (not a directory)"))
    if find_pdftotext() is None:
        print("  pdftotext was not found, so any PDF transcript is unreadable "
              "here; set STUDY_HUB_PDFTOTEXT or install poppler")
    rows, compared, unsourced = scan(SPLIT.lessons_in(notes), mats)
    report(rows, compared, unsourced)
    return 0


if __name__ == "__main__":
    sys.exit(main())
