#!/usr/bin/env python3
"""Narration that belongs to a DIFFERENT part of the same course.

Promoted from the one-off `_admin/work/transcript-contamination/xpart.py`, which
found all four `7PAYCAMD` cases with zero misses and zero false positives against
an answer set established independently by OCR. **The logic is unchanged; what is
new is that it has tests and the ingest sweep runs it.**

🔴 **THERE ARE TWO CLASSES AND ONLY ONE HAS A PUNCTUATION TELL.** An APPENDED
splice joins with no space (`...address.So, we've got...`); a REPLACED one is
clean prose that is simply the WRONG prose. The first sweep of this searched for
the JOIN, so it found only the class that has a join and reported "one splice in
the course" an hour before three more turned up. **A punctuation scan answers
"was anything concatenated", which is a different question from "does this
narration belong to this slide"** — this project's own "searching for a check's
output is searching for its absence in disguise", in the wild.

🔴 **THE FALSE-POSITIVE CLASS IS BOILERPLATE, and it is reported rather than
hidden.** A transcript repeats its module header in every part, so a sentence in
MANY parts is furniture and a long sentence in exactly a FEW is the signal. Both
are returned, because a filter whose exclusions are invisible is a filter nobody
can audit.
"""
import collections
import os
import re

# A sentence must be this long to be worth comparing. Short ones collide by
# chance ("Thank you.") and would drown the signal.
MINW = 8
# In more parts than this, a repeated sentence is the document's own furniture
# rather than a splice.
MAXPARTS = 3

_SLIDE = re.compile(r"^##\s+Slide\s+(\d+)\s*$", re.M)

# 🔴 PAGE FURNITURE, STRIPPED INLINE AND NOT BY DROPPING THE SENTENCE, and that
# distinction matters more than the list. `pdftotext` interleaves a footer INTO
# the narration stream, mid-sentence and even mid-word: measured, "And while the
# PAGE | 1 Lecture Transcript DSM is the most popular diagnostic system..." and
# "moment-by" + footer + "moment". **Dropping any sentence containing a marker
# discarded 172 and 333 REAL narration sentences in two courses**, and a
# contaminated sentence straddling a page break would have been dropped and never
# compared. That is a sensitivity hole in the direction that reads as good news.
FURNITURE = (
    r"Transcripts?\s+by\s+3Playmedia(\s+Week\s+\d+)?"
    r"(\s*©\s*King's College London)?\s*\d*\s*\.?",
    r"©\s*King's College London",
    r"End of Media",
    r"PAGE\s*\|\s*\d+",
    r"\bLecture Transcript\b",
    r"No\s+[Aa]udio\s+(present|on this [Ss]lide)\s*\.?",
)
FURN_RE = re.compile("|".join(FURNITURE))


def norm(text):
    """One line, straight quotes, no soft hyphens, no hyphen-across-a-linebreak."""
    text = text.replace("­", "")
    text = re.sub(r"(\w)-\s*\n\s*(\w)", r"\1\2", text)
    text = re.sub(r"[‘’]", "'", text)
    text = re.sub(r"[“”]", '"', text)
    return re.sub(r"\s+", " ", text).strip()


def strip_furniture(text):
    return re.sub(r"\s+", " ", FURN_RE.sub(" ", text)).strip()


def sentences(text):
    for s in re.split(r"(?<=[.!?])\s+", text):
        s = s.strip()
        if len(s.split()) >= MINW:
            yield s


def key(sentence):
    """Compare on words alone: casing and punctuation vary between renderings."""
    return re.sub(r"[^a-z0-9 ]+", "", sentence.lower()).strip()


def slide_units(source_dir):
    """`(part, "slide N", text)` for every `## Slide N` in every `transcript.md`.

    🔴 **This is the per-slide markdown form only**, which is what a fetch against
    a web-shaped course produces. The caller is responsible for saying so when a
    course has none; see `cap_narration_unique`.
    """
    out = []
    if not os.path.isdir(source_dir):
        return out
    for part in sorted(os.listdir(source_dir)):
        path = os.path.join(source_dir, part, "transcript.md")
        if not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8", errors="replace") as fh:
            chunks = _SLIDE.split(fh.read())
        for i in range(1, len(chunks), 2):
            out.append((part, "slide %s" % chunks[i], chunks[i + 1]))
    return out


def scan(units):
    """{parts, units, sentences, duplicates, boilerplate}.

    `duplicates` is `[(parts, [(part, where, sentence), ...]), ...]`; each entry
    also carries `same_slide`, because **all four known cases sat at the SAME
    slide index in both parts, four for four**, which is what turns a duplicate
    into a diagnosis rather than a curiosity.
    """
    where = collections.defaultdict(list)
    counted = 0
    for part, loc, text in units:
        for sentence in sentences(strip_furniture(norm(text))):
            counted += 1
            where[key(sentence)].append((part, loc, sentence))

    dups, boiler = [], []
    for occurrences in where.values():
        parts = sorted({p for p, _l, _s in occurrences})
        if len(parts) < 2:
            continue
        places = {l for _p, l, _s in occurrences}
        row = {"parts": parts, "occurrences": occurrences,
               "same_slide": len(places) == 1 and occurrences[0][1] != "-"}
        (dups if len(parts) <= MAXPARTS else boiler).append(row)

    dups.sort(key=lambda r: (r["parts"], r["occurrences"][0][1]))
    boiler.sort(key=lambda r: -len(r["parts"]))
    return {"parts": sorted({u[0] for u in units}), "units": len(units),
            "sentences": counted, "duplicates": dups, "boilerplate": boiler}


def report(found):
    """The lines a person reads. A clean run SAYS so; it does not go quiet."""
    out = ["%d parts, %d slides, %d sentences of %d+ words compared"
           % (len(found["parts"]), found["units"], found["sentences"], MINW)]
    if found["boilerplate"]:
        out.append("%d repeated sentence(s) excluded as the document's own "
                   "furniture (in more than %d parts)"
                   % (len(found["boilerplate"]), MAXPARTS))
    if not found["parts"]:
        # 🔴 AN EMPTY POPULATION IS NOT A CLEAN BILL OF HEALTH, and the first
        # draft of this said "all 0 narrations are unique to their part", which
        # is the vacuity shape: a green sentence produced by comparing nothing.
        # A course with no per-slide transcripts must read as UNCHECKED.
        out.append("nothing to compare: this course has no per-slide "
                   "transcripts, so it is UNCHECKED rather than clean")
        return out
    if not found["duplicates"]:
        # 🟢 THE POSITIVE RESULT. This project's rule: a check silent on success
        # is indistinguishable from a check that was never built. The sentence
        # count is beside it deliberately - a fraction with no denominator is
        # how an empty run passes for a clean one.
        out.append("all %d narrations are unique to their part, across %d "
                   "compared sentences" % (len(found["parts"]), found["sentences"]))
        return out
    out.append("%d cross-part duplicate sentence(s):" % len(found["duplicates"]))
    for row in found["duplicates"]:
        out.append("  parts: %s" % ", ".join(row["parts"]))
        for part, loc, sentence in row["occurrences"]:
            out.append("    %-12s %-9s %s" % (part, loc, sentence[:96]))
        if row["same_slide"]:
            out.append("    SAME SLIDE INDEX IN BOTH PARTS (%s) - that is a "
                       "mechanism, not a coincidence" % row["occurrences"][0][1])
    return out
