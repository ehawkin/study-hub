#!/usr/bin/env python3
"""Recover a transcript whose words are DRAWN on the page instead of set as type.

    python3 server/recover_transcripts.py --survey                # what would be recovered, and why
    python3 server/recover_transcripts.py --control PSY101        # the accuracy control on its own
    python3 server/recover_transcripts.py --recover PSY101        # control, then recover, then re-measure

Four lectures in the mindfulness course had ZERO captions, and the cause was one PDF defect:
`W2-T1-P2`, `W2-T2-P1`, `W2-T2-P2`, `W2-T3-P2` carried a full transcript that
`pdftotext` could not read, because the body text is VECTOR OUTLINES rather than
type. Six pages, 17MB, zero images, and `pdftotext` returned 93 words.

🟢 **That is the BEST case for OCR, not the worst.** A vector page renders to a
perfect bitmap at any resolution; a scan is the hard case and this is a clean
digital render. EH proposed it himself: *"why don't we just re-OCR the PDFs?"*

🟢🟢 **RUN 2026-09-09, all four recovered**, each keeping its dated backup:

| lecture | words | coverage ratio |
| --- | --- | --- |
| `W2-T1-P2` | 0 to **3,204** | 0.00 to **0.95** |
| `W2-T2-P1` | 164 to **1,172** | 0.12 to **0.86** |
| `W2-T2-P2` | 178 to **1,146** | 0.13 to **0.85** |
| `W2-T3-P2` | 448 to **2,340** | 0.17 to **0.90** |

**The floor is 0.75, so all four clear it**, and the words are still the
lecturer's own. ⚠️ **Their captions are not built yet**: that is a `captions.py`
run and it is deliberately not part of this tool.

## 🔴🔴 THE THIRD ROUTE, 2026-09-15: A TRANSCRIPT CAN BE WHOLE AND STILL LOSE SENTENCES

**Everything above is about a transcript that is UNREADABLE.** There is a quieter
defect the same file can have: **sentences missing from the middle of pages that
extract perfectly**. The coverage ratio stays comfortable, the factor stays near
1, and the caption falls back to what the speech recogniser heard, so the reader
is shown a machine's guess where KCL's own words exist and were readable.

**`--deep` measures it, by MEMBERSHIP rather than alignment:** every page is read
by OCR, and any run of real words OCR sees that is **nowhere in the text layer**
is counted as lost. Two earlier instruments asked the wrong question and are
recorded in `lost_speech` so nobody builds them again.

    python3 server/recover_transcripts.py --survey --deep     # every page of every transcript

⚠️ **It costs about 5 seconds a page the first time and nothing afterwards**
(`ocr_cached`, keyed on the file's size and mtime so a rebuilt PDF is read
again). 🔴 **The plain `--survey` says out loud how many lectures it did NOT
deep-check**, because a check nobody ran must not read as a check that passed.

## 🔴 THIS TOOL BUILDS ALMOST NOTHING. It JOINS four things that already existed

Written after checking, because the queue entry asked for a detector, a control
and a stripper that all turned out to be shipped already:

| the entry asked for | what already does it |
| --- | --- |
| detect by structure, never a hardcoded list | `captions.coverage()`, which scores a transcript's words against its own audio and is a better test than the entry's proposed `0 images + 0 body fonts`, because it measures the thing that matters and cannot be fooled by a font NAME |
| strip the running header and footer | `transcripts.spoken_text()`, cover page and furniture both |
| OCR the pages | `pdf_fix.fix_one()`, which wraps `ocrmypdf --redo-ocr` and keeps the dated backup |
| compare like with like | `captions.plain()`, whose NFKD is what makes `ﬁgure` compare equal to `figure` |

**What was genuinely missing is the JOIN**: nothing took the survey's own verdict
and acted on it, so a person had to know which file to point `pdf_fix` at.

## 🔴🔴 THE SECOND TEST, AND IT IS THE WHOLE SAFETY OF THIS TOOL

**A low coverage ratio is not a reason to OCR.** Ten lectures score below the
floor and only four are unreadable; the other six are simply SHORT, and their
words extract perfectly. Running OCR on those would be worse than useless:
their pages carry slide images whose text a reader never speaks, so OCR would
pour non-narration words into the transcript, raise the coverage ratio, and make
the alignment worse while the number said it got better.

🟢 **So the deciding measurement is per file: OCR a probe page and compare it
with what `pdftotext` reads from the SAME page.** Text that is drawn rather than
set shows up as a large factor and nothing else does. Measured on this corpus:

| | factor (OCR words / pdftotext words on the probe page) |
| --- | --- |
| the four with drawn text | **3.98, 5.97, 7.32, 56.90** |
| the six that are merely short | 1.06, 1.16, 1.51, 1.55, 1.56, 1.93 |
| two healthy controls | 1.00, 1.01 |

⚠️ **The answer is THREE-VALUED and the middle band is not a decision.** Above
`RECOVER_ABOVE` the page is carrying text the extractor cannot see; below
`LEAVE_BELOW` it is not; **between them the tool reports and does nothing**,
because the gap it would be guessing across is exactly where a wrong guess
rewrites a good transcript. The same shape as `git_facts.inside_work_tree`.

## 🟢 THE CONTROL, WHICH RUNS BEFORE ANY FILE IS TOUCHED

OCR a transcript that already extracts CLEANLY, diff it against `pdftotext`, and
refuse the whole run if the agreement is below `ACCURACY_FLOOR`. The point is not
to prove OCR works in general; it is to notice the day tesseract, the render dpi
or the page furniture changes underneath us.

**Measured 2026-09-09 on the mindfulness course's `W3-T3-P1`, 6 pages: 99.84%, six differing
regions.** One is a real OCR error (`can` read as `gan`). Three are OCR being
MORE correct than `pdftotext`, which loses the space in `present moment`,
`self referencing` and `task related`. Two are page furniture the pipeline
strips anyway.

🔴 **The queue entry's own figure was 99.35%, and the difference is a lesson
rather than a discrepancy.** That number came from comparing raw tokens, where
`pdftotext`'s ligature codepoint `ﬁ` does not equal OCR's `fi`, so 34 correct
words counted as errors. **This module compares through `captions.plain()`, the
normalisation the caption pipeline itself applies**, which is the only
comparison that says anything about the captions a reader will see.

⚠️ **That mistake was made HERE first, and the control that caught it was
grepping the shipped `.vtt` files for the damage: zero hits in 400 files.** A
probe and the thing it probes are not independent witnesses.

## What this tool does NOT do

**It does not build captions.** Recovering the words is the blocker; turning them
into timed cues is a `captions.py` run that needs Whisper and real compute, and
it is deliberately a separate step so that the recovery can be checked on its own.
"""
import argparse
import difflib
import glob
import hashlib
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import captions as CAP
import pdf_fix as FIX
import transcripts as T

# The probe page's OCR words divided by its pdftotext words. Both constants are
# measured (see the table above), and the GAP between them is the point: a file
# that lands inside it is reported to a person, never acted on.
RECOVER_ABOVE = 3.0
LEAVE_BELOW = 2.0

# 🔴 The entry's ruled number, kept as the floor even though this machine measures
# 0.9984 through `captions.plain()`. A floor set to today's measurement would fail
# on the next tesseract release for no reason; a floor set where the ruling put it
# fails when something real changes.
ACCURACY_FLOOR = 0.9935

# How many clean transcripts the control reads. Four, because the spread across
# clean files here is 0.9917 to 0.9984 and a median needs enough subjects to be
# one; every extra subject costs a whole lecture of OCR.
CONTROL_SAMPLE = 4

RENDER_DPI = 300


def page_count(pdf):
    """Pages, or 0 if pdfinfo cannot say. 0 means every caller declines."""
    out = FIX.run(["pdfinfo", str(pdf)])
    if out.returncode != 0:
        return 0
    m = re.search(r"Pages:\s+(\d+)", out.stdout or "")
    return int(m.group(1)) if m else 0


def probe_pages(pages):
    """Which pages to measure.

    🔴 **Never page 1.** A transcript's first page is often a cover: a title, a
    lecturer and a department, twenty words in a page of white space, which reads
    as "drawn text" under any ratio test and is nothing of the kind.

    Two pages rather than one wherever there are two to take, so a single odd
    page (a figure, a references list) cannot decide the file by itself.
    """
    if pages <= 0:
        return []
    if pages == 1:
        return [1]
    if pages == 2:
        return [2]
    mid = pages // 2 + 1
    # Prefer the page AFTER the middle as the second probe and fall back to the
    # one before, so a three-page transcript gets two probes rather than one:
    # its only non-cover pages are 2 and 3 and `mid - 1` is the cover.
    other = mid + 1 if mid + 1 <= pages else mid - 1
    return sorted({mid, other})


def text_words_on(pdf, page):
    """Words `pdftotext` reads from one page: what the pipeline gets today."""
    out = FIX.run([FIX.PDFTOTEXT or "pdftotext", "-f", str(page), "-l", str(page),
                   str(pdf), "-"])
    return len((out.stdout or "").split()) if out.returncode == 0 else 0


def ocr_text_on(pdf, page, dpi=RENDER_DPI):
    """The page rendered and read. Empty string when a tool is missing."""
    if not (FIX.PDFTOPPM and FIX.TESSERACT):
        return ""
    with tempfile.TemporaryDirectory() as td:
        base = os.path.join(td, "page")
        r = FIX.run([FIX.PDFTOPPM, "-r", str(dpi), "-f", str(page), "-l", str(page),
                     "-png", str(pdf), base])
        if r.returncode != 0:
            return ""
        pngs = sorted(glob.glob(base + "*.png"))
        if not pngs:
            return ""
        out = FIX.run([FIX.TESSERACT, pngs[0], "stdout", "--psm", "6"])
        return out.stdout or ""


def hidden_factor(pdf):
    """(factor, rows) for one PDF: how much text the extractor cannot see.

    The factor is the WORST probe page rather than the mean, because a file whose
    pages differ is a file worth looking at, and averaging is how it would stop
    being reported.
    """
    rows, worst = [], 0.0
    for page in probe_pages(page_count(pdf)):
        seen = text_words_on(pdf, page)
        drawn = len(ocr_text_on(pdf, page).split())
        # A page with no text at all and real OCR words is the extreme case, not a
        # division by zero: report it as the largest factor rather than skipping it.
        factor = (drawn / seen) if seen else (float(drawn) if drawn else 0.0)
        rows.append({"page": page, "pdftotext": seen, "ocr": drawn, "factor": factor})
        worst = max(worst, factor)
    return worst, rows


RECOVER, LEAVE, ASK = "recover", "leave", "ask"


def verdict(factor, lost_words=None):
    """🔴 Three-valued on purpose, and the middle value is the reason this is safe.

    `recover` (the page carries text nothing can extract), `leave` (its words are
    readable and it is short for some other reason, which OCR cannot fix), and
    `ask` for the band between, where acting either way is a guess. A tool that
    collapses `ask` into either neighbour is a tool that will one day pour a slide
    deck's words into somebody's transcript and report an improved ratio.

    🟢 `lost_words` is the SECOND ROUTE, and it exists because the factor cannot
    see a file that loses sentences from the middle of pages it otherwise reads
    perfectly. **`None` means the presence test was not run**, and then this
    behaves exactly as it did before it existed: a caller who does not pay for
    whole-file OCR gets the same answer as always, rather than a quiet `leave`
    standing in for a question nobody asked.

    ⚠️ Any lost speech below the bar is `ask`, never `leave`. The words really
    are gone from the text layer; what is uncertain is only whether rebuilding
    the layer is worth its own risk, and that is a person's call.
    """
    if lost_words is not None and lost_words >= LOST_RECOVER_AT:
        return RECOVER
    if factor >= RECOVER_ABOVE:
        return RECOVER
    if lost_words:
        return ASK
    if factor <= LEAVE_BELOW:
        return LEAVE
    return ASK


def keys(text):
    """The pipeline's OWN word keys, so a comparison here means something there.

    🔴 Not a second normaliser. `captions.words_of` keeps the lecturer's spelling
    and `Word.key` is `captions.plain()` lowered, which is NFKD: it is what makes
    `ﬁgure` compare equal to `figure`. Comparing raw tokens instead counts 34
    correct words as OCR errors on a single healthy transcript, which is how the
    entry's 99.35% was arrived at.
    """
    return [w.key for w in CAP.words_of(T.spoken_text(text or ""))]


def agreement(truth_text, ocr_text):
    """(accuracy, differing regions) between the extractor and OCR."""
    a, b = keys(truth_text), keys(ocr_text)
    if not a:
        return 0.0, []
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    same = sum(block.size for block in sm.get_matching_blocks())
    diffs = [(tag, " ".join(a[i1:i2]), " ".join(b[j1:j2]))
             for tag, i1, i2, j1, j2 in sm.get_opcodes() if tag != "equal"]
    return same / len(a), diffs


def ocr_whole(pdf, dpi=RENDER_DPI):
    """Every page rendered and read, joined by the form feed the parser expects.

    🔴 The form feed matters: `transcripts.spoken_text` looks for the PDF's own
    page boundary before it applies its blank-line heuristic, so joining with a
    newline would hide the cover page from the very function that strips it.
    """
    return "\f".join(ocr_text_on(pdf, page, dpi)
                     for page in range(1, page_count(pdf) + 1))


# --- the presence test: KCL's words that are nowhere in the text layer --------
#
# 🔴 THE DEFECT THE FACTOR CANNOT SEE. A transcript can lose sentences from the
# MIDDLE of pages that are otherwise read perfectly. `hidden_factor` compares
# word COUNTS on probe pages, so a file missing four sentences out of a thousand
# words scores 1.22 and is ruled `leave`; `candidates()` never even gets that
# far, because the coverage ratio is above the floor and the row is skipped. Both
# gates pass and the reader is shown the recogniser's guess where KCL's own words
# exist and were readable. Measured over all 125 transcripts 2026-09-15.
#
# 🔴 THE QUESTION IS MEMBERSHIP, NOT ALIGNMENT, and two instruments that asked
# the other question were built and thrown away first:
#
#   `agreement`      NOT DIRECTIONAL. It falls just as far when OCR fails as when
#                    the extractor does, and ranks six healthy Week 4 files above
#                    the one real case.
#   `insert` runs    NOT MEMBERSHIP. An insert opcode says words did not ALIGN
#                    there; text that merely MOVED makes one just as readily.
#                    60% of the words it reported are in the extractor's own
#                    text, verbatim.
#
# 🟢 So: take each run of speech OCR read, and look for it ANYWHERE in the text
# layer. A run counts as lost only when no 6-gram of it can be found, which
# survives OCR garbling a word here and there: a genuinely present passage of N
# words offers N-5 chances to match and needs only one.
LOST_MIN_RUN = 4        # a lost passage is several real words in a row
LOST_NGRAM = 6          # a 6-gram found anywhere means the passage is not lost

# 🔴 THE SECOND ROUTE TO `recover`, three-valued for the same reason the factor
# is: any lost speech at all is worth telling a person about, and only a
# substantial amount is worth rebuilding a whole text layer over.
#
# 🟢 WHERE 60 COMES FROM, AND IT IS NOT A GAP IN THE HISTOGRAM. The risk of
# rebuilding is that OCR loses text the extractor reads, and that risk was
# MEASURED over the worst-affected files: whole-file OCR ADDS 5,661 words and
# LOSES 17, eleven of the sixteen losing nothing at all. **That measurement
# covers the files above roughly this line and has not been taken below it**, so
# the bar sits where the evidence stops. Everything under it is `ask`, which
# means a person looks rather than nothing happening.
#
# ⚠️ A lower bar is not obviously wrong and may well be right: the trade above is
# 330 to 1. It is not taken here because the OTHER risk does not scale with the
# words lost - OCR-ing a file that needs nothing pours SLIDE text into a
# transcript, and that depends on what the pages carry, not on how much speech is
# missing.
LOST_RECOVER_AT = 60    # words of KCL speech absent from the text layer
WORDLIST = Path("/usr/share/dict/words")

# 🔴 OUTSIDE THE REPO ON PURPOSE. The cache is 125 files of transcript text,
# derived and re-derivable, and `_admin/work/` is TRACKED: a cache written
# beside the survey that produced it becomes 125 committable files carrying
# KCL's teaching in plain text. The state directory is where this project
# already keeps machine-local things it does not ship.
OCR_CACHE = Path.home() / ".kcl-study" / "ocr-cache"


def dictionary(path=WORDLIST):
    """The word list, read once. 🔴 It RAISES rather than returning an empty set.

    With no dictionary every candidate run scores zero real words, nothing is
    ever flagged, and the survey reports a clean corpus. **A check that cannot
    reach its subject must never report success**, which is the same rule
    `git_facts` exists for one directory over.
    """
    if not hasattr(dictionary, "_cache"):
        try:
            body = Path(path).read_text(encoding="utf-8", errors="ignore")
        except OSError as exc:
            raise AssertionError(
                "the presence test needs a word list and %s could not be read "
                "(%s). It refuses rather than reporting every transcript clean."
                % (path, exc))
        dictionary._cache = {w.strip().lower() for w in body.splitlines() if w.strip()}
    return dictionary._cache


def speech_run(toks, words=None):
    """The longest run of consecutive real words, which is what separates speech
    from OCR furniture. `ot`, `5` and `c s slide 1 5` are not sentences."""
    words = dictionary() if words is None else words
    best = run = 0
    for t in toks:
        run = run + 1 if (t in words and len(t) > 2) else 0
        best = max(best, run)
    return best


def found_in(seg, hay, ngram=LOST_NGRAM):
    """Is any `ngram`-word window of `seg` anywhere in `hay`?"""
    if len(seg) < ngram:
        return " ".join(seg) in hay
    return any(" ".join(seg[k:k + ngram]) in hay
               for k in range(len(seg) - ngram + 1))


def page_keys(text):
    """Every word of the text layer, cover page included, as pipeline keys.

    🔴🔴 THE HAYSTACK IS THE WHOLE TEXT LAYER AND NOT `keys()`, and the
    difference was 23 of the 42 files a first run flagged. `keys()` applies
    `transcripts.spoken_text`, which strips the cover block: the module title,
    the week, the lecturer's name. **That heuristic works on the born-digital
    layout and NOT on OCR's**, so the cover survived on one side and was stripped
    on the other, and every affected file was reported as losing a 37-word run
    that reads *"psychology and neuroscience of affective disorders week 2 …"*.

    ⚠️ **Those files lose nothing. The cover page IS in the text layer**, and
    "is this passage anywhere in the extractor's text" is the question this
    module asks. Asking it of the spoken text asks a different one.
    """
    return [w.key for w in CAP.words_of(text or "")]


def ocr_cached(pdf, cache_dir=None, dpi=RENDER_DPI):
    """`ocr_whole`, remembered. 25 minutes for the corpus is affordable once.

    🔴 The key carries the file's SIZE AND MTIME, not just its name. `recover()`
    rewrites the PDF in place, so a name-keyed cache would hand the repaired file
    its own pre-repair reading and report the holes still there.
    """
    pdf = Path(pdf)
    if not cache_dir:
        return ocr_whole(str(pdf), dpi)
    st = pdf.stat()
    tag = hashlib.sha1(("%s|%d|%d|%d" % (pdf.resolve(), st.st_size,
                                         st.st_mtime_ns, dpi)).encode()).hexdigest()[:12]
    cache = Path(cache_dir) / ("%s.%s.txt" % (pdf.stem[:40].replace("/", "_"), tag))
    if cache.is_file():
        return cache.read_text(encoding="utf-8")
    raw = ocr_whole(str(pdf), dpi)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(raw, encoding="utf-8")
    return raw


def lost_speech(pdf, cache_dir=None, dpi=RENDER_DPI, ocr_text=None,
                pdf_text=None):
    """What OCR reads as speech that the text layer does not hold at all.

    Returns `{lost, lost_words, moved, moved_words, worst}`. `moved` is the
    control that keeps this honest: a run that IS somewhere in the text layer is
    counted separately rather than silently dropped, so a run of zeros in the
    lost column beside a large moved column says the instrument looked and found
    the passage, not that it failed to look.
    """
    # 🟢 Both readings can be handed in, which is what lets the tests drive
    # this without a PDF or a tesseract on the machine running them.
    text = T.pdf_text(str(pdf)) if pdf_text is None else pdf_text
    a = page_keys(text)
    b = keys(ocr_text if ocr_text is not None else ocr_cached(pdf, cache_dir, dpi))
    hay = " ".join(a)
    words = dictionary()
    out = {"lost": 0, "lost_words": 0, "moved": 0, "moved_words": 0, "worst": ""}
    if not a or not b:
        return out
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    for tag, _i1, _i2, j1, j2 in sm.get_opcodes():
        if tag != "insert":
            continue
        seg = [t for t in b[j1:j2] if t]
        if speech_run(seg, words) < LOST_MIN_RUN:
            continue
        if found_in(seg, hay):
            out["moved"] += 1
            out["moved_words"] += len(seg)
            continue
        out["lost"] += 1
        out["lost_words"] += len(seg)
        if len(seg) > len(out["worst"].split()):
            out["worst"] = " ".join(seg)
    return out


def coverage_row(course, part, root="."):
    """`captions.coverage`, or None when the lecture has no package at all.

    🔴🔴 **THE DEEP PASS MADE THIS REACHABLE AND NOTHING CAUGHT IT.**
    `candidates` unions in every lecture that owns a transcript, package or not,
    so `recover` can now be handed a part `coverage` never had to answer for: it
    opens `packages/<part>/index.html` and raises `FileNotFoundError`.

    ⚠️ **The branch that handles a missing coverage row was already written and
    simply could not be reached.** Measured mid-run on the affective disorders course's `W5-T1-P3`,
    2026-09-15: the PDF was rebuilt, the process then died before it could say
    so, **and every row after it in that course would have been skipped with its
    file already changed.** A crash after the write is the worst place to have
    one, because the data has moved and the report has not.
    """
    try:
        return CAP.coverage(course, part, root)
    except (FileNotFoundError, NotADirectoryError):
        return None

def dropped_speech(before_text, after_text):
    """Speech the OLD text layer held that the REBUILT one does not.

    🔴🔴 **THE OTHER DIRECTION, AND IT IS THE ONE THE BACKUP RULE IS ABOUT.**
    `lost_speech` asks whether the words came BACK. Nothing asked whether the
    rebuild took any away, and `fix_one` replaces the whole text layer rather
    than patching the holes, so that is a real risk and not a theoretical one.
    **Confirming that a migration RAN is not the same as confirming the data
    ARRIVED.**

    🟢 **THE SAME COUNTER WITH THE ROLES SWAPPED, never a second
    implementation.** Measuring one file two ways is how two numbers about one
    lecture come to disagree, so this hands the old reading to `lost_speech` as
    the needle and the new one as the haystack, and everything the presence test
    already knows (the dictionary floor, the run length, the moved control)
    applies unchanged.

    ⚠️ **The asymmetry inside `lost_speech` points the safe way round here.**
    The needle goes through `keys`, which strips the cover block, and the
    haystack through `page_keys`, which keeps it. So a cover page the rebuild
    re-set in a different shape cannot be reported as dropped speech, which is
    the same false positive the haystack fix removed in the other direction.
    A genuinely dropped cover block would go unreported: it is not speech, and
    the alternative is a false alarm on every file.
    """
    return lost_speech(None, ocr_text=before_text, pdf_text=after_text)


SPLICE_MIN_RUN = 2      # a splice interrupts the word with at least this many words


def _shared_head(a, b):
    """The characters `a` and `b` both open with."""
    n = 0
    while n < len(a) and n < len(b) and a[n] == b[n]:
        n += 1
    return a[:n]


def _shared_tail(a, b):
    """The characters `a` and `b` both end with."""
    n = 0
    while n < len(a) and n < len(b) and a[-1 - n] == b[-1 - n]:
        n += 1
    return a[len(a) - n:] if n else ""


def spliced_runs(before_text, after_text):
    """Runs the rebuild put INSIDE a word, and whether each one belongs there.

    🔴🔴 **THE BLIND SPOT IS AN OPCODE, WHICH IS WHY A DAMAGED FILE READS CLEAN
    ON BOTH COUNTERS.** `lost_speech` iterates `if tag != "insert": continue`,
    and `dropped_speech` is that same function with its roles swapped, so
    **both skip every `replace` BY CONSTRUCTION rather than by oversight.** A
    run spliced into the middle of an existing word is a `replace`: measured on
    the affective disorders course's `W5-T2-P1`, which reports 0 lost and 0 dropped while reading
    *"it takes an eightmultiple, say three or more, episodes week group-based
    programme"*. **Neither counter is wrong; they answer a different question,
    and this is the one nobody was asking.**

    🟢 **THE SIGNATURE NEEDS NO AUDIO, and that is the whole point of choosing
    it.** A hole fuses the words either side of it into one token (`self` +
    `also` extracts as `selfalso`), so a rebuild that fills the hole SPLITS
    that token: the old key survives as a head on the first new key and a tail
    on the last. **Speech cannot be inserted inside a word**, so the shape is
    decidable from the two readings already in hand.

    🔴 **BUT THE SHAPE ALONE SAYS NOTHING ABOUT HARM, which is the mistake this
    function exists not to make.** Thirteen of the fourteen splices in the
    recovered corpus are the rebuild REPAIRING a fusion the hole itself caused.
    **The discriminator is whether the spliced run was already in the file**: a
    run absent from the before-text is recovered speech and the split is
    correct; a run already present has been MOVED off its own sentence and into
    the middle of a word, which is damage. Measured over the 15 recovered
    lectures: **14 splices, 13 repairs, 1 displacement**, and the one is the
    instance found by eye.

    ⚠️ **THE LIMIT, STATED BECAUSE IT IS NOT SMALL: a run relocated to a
    WHOLE-WORD boundary is invisible here**, and this function's own count of 1
    displacement was read as the corpus total for a day because of it.
    🟢 **`displaced_runs` below now counts that class, and it is 48 across the
    same 15 lectures.** Both of us wrote that only the audio could settle it;
    that is true of *were these words truly adjacent*, and the countable
    question turned out to be a different one. **What still needs the audio is
    `study-hub-qa`'s case unchanged: whether a genuinely NEW insert broke an
    adjacency that mattered.**
    """
    a, b = page_keys(before_text or ""), page_keys(after_text or "")
    out = {"splices": 0, "displaced": 0, "repairs": 0, "worst": "", "found": []}
    if not a or not b:
        return out
    hay = " ".join(a)
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
            None, a, b, autojunk=False).get_opcodes():
        if tag != "replace" or i2 - i1 != 1:
            continue
        word, new = a[i1], b[j1:j2]
        if len(new) < 2:
            continue
        # 🔴 THE LOAD-BEARING LINE, and the one that keeps this from being a
        # rewrite counter: the old key must survive as a HEAD on the first new
        # key and a TAIL on the last, which is what "the word was split around
        # something" means. **It blocks 45 of the 98 candidate opcodes in the
        # recovered corpus**, every one of them an ordinary rewrite.
        head = _shared_head(word, new[0])
        tail = _shared_tail(word, new[-1])
        if not head or not tail or head + tail != word:
            continue
        run = new[1:-1]
        if len(run) < SPLICE_MIN_RUN:
            continue
        # 🔴 ALREADY IN THE FILE means the rebuild MOVED it rather than found
        # it. `found_in` is the shipped presence test, not a second one.
        displaced = found_in(run, hay)
        out["splices"] += 1
        out["displaced" if displaced else "repairs"] += 1
        out["found"].append({"word": word, "head": head, "tail": tail,
                             "run": " ".join(run), "displaced": displaced})
        if displaced and len(run) > len(out["worst"].split()):
            out["worst"] = " ".join(run)
    return out


DISPLACED_MIN_RUN = 2   # the sibling's floor: two words is the shortest "run"


def occurrences(keys, run):
    """How many times `run` appears in `keys`, as a word sequence.

    🔴 **NOT `found_in`, and the difference is the whole discriminator.** The
    presence test answers *is this anywhere*, which is exactly the question
    that reports a moved run as safe. **This asks HOW MANY**, because speech
    the rebuild RECOVERED raises its own count and speech it MOVED does not.
    """
    n = 0
    for i in range(len(keys) - len(run) + 1):
        if keys[i:i + len(run)] == run:
            n += 1
    return n


def displaced_runs(before_text, after_text):
    """Runs the rebuild moved to a WHOLE-WORD boundary rather than recovered.

    🔴🔴 **THIS IS THE GAP `spliced_runs` NAMES IN ITS OWN DOCSTRING** - *"a
    run relocated to a WHOLE-WORD boundary is invisible here"* - and the
    reason it was left open was a claim that turned out to be about a
    different question. **Both `study-hub-qa` and this coder wrote that only
    the audio could settle it.** That is true of *were these two words truly
    adjacent before*, which nobody can answer from text. **It is not true of
    the question that actually matters**, which is the one below.

    🟢 **THE DECIDABLE QUESTION: DID THIS RUN'S OCCURRENCE COUNT RISE?**
    Speech the rebuild RECOVERED was not in the old reading at all, so it
    arrives as a new occurrence. Speech it MOVED was already there, so it is
    inserted here and gone from where it was, and the count is unchanged.
    **No audio, because adjacency is never asked about.**

    ⚠️ **A RUN THAT OCCURS MORE THAN ONCE IS UNDECIDABLE AND IS NEVER COUNTED
    AS DAMAGE**, which is a well-definedness rule and not a tuned one: with
    two occurrences there is no fact about *which* one moved. **That single
    rule is what separates the classes.** Measured over the 15 recovered
    lectures: it removes 9 candidates, among them `and the` at eleven
    occurrences either side, which is difflib re-anchoring rather than damage.

    🟢 **AND THE LENGTH FLOOR IS INERT, WHICH IS THE POINT.** At a floor of
    one word this corpus gives the same count as at two, so nothing is being
    tuned: the uniqueness rule is doing all the work. It stays at
    `DISPLACED_MIN_RUN` to match the sibling and to keep single-word noise out
    of a corpus that has not been measured yet.

    ⚠️ **A SINGLE-OLD-WORD `replace` IS NOT READ HERE**: that is exactly
    `spliced_runs`' shape, and reading it in both would count one defect
    twice. Every other opcode shape that adds text is read, which is what the
    multi-word `replace` case is doing in the filter.

    🔴 **WHAT IS STILL INVISIBLE, because the limit moved rather than closed:
    a run that was moved AND rewritten** matches nothing exactly and is
    counted nowhere, and a genuinely new insert that breaks an old adjacency
    is still the case `study-hub-qa` named, which does need the audio. **This
    widens the countable class a second time. It does not close it.**
    """
    a, b = page_keys(before_text or ""), page_keys(after_text or "")
    out = {"moved": 0, "ambiguous": 0, "worst": "", "found": []}
    if not a or not b:
        return out
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
            None, a, b, autojunk=False).get_opcodes():
        if tag == "equal" or tag == "delete":
            continue
        if tag == "replace" and i2 - i1 == 1:
            continue
        run = b[j1:j2]
        if len(run) < DISPLACED_MIN_RUN:
            continue
        was, now = occurrences(a, run), occurrences(b, run)
        if not was:
            continue                      # speech the rebuild found: the point of it
        if was != 1 or now != 1:
            out["ambiguous"] += 1
            continue
        out["moved"] += 1
        out["found"].append({"run": " ".join(run), "before": was, "after": now})
        if len(run) > len(out["worst"].split()):
            out["worst"] = " ".join(run)
    return out


def rebuild_paid_off(dropped_words, gained_words):
    """Whether a rebuild that dropped some speech still came out ahead.

    🟢 **A PROPERTY, NOT A THRESHOLD.** The measured trade over the worst
    affected files is 5,661 words added against 17 lost, so any number pulled
    from that gap would be arbitrary and would need re-deriving the first time
    a different course was recovered. **"It must not lose more of the lecturer
    than it found" needs no calibration and cannot go stale.**

    🔴 **A drop with nothing measurably gained is a refusal**, because there is
    nothing to weigh it against. **Equal is not ahead**: a rebuild that traded
    evenly did the reader no good and still rewrote their transcript.
    """
    if not dropped_words:
        return True
    if not gained_words:
        return False
    return dropped_words < gained_words


def transcript_pdf(course, part, root="."):
    return T.transcript_for(os.path.join(root, "materials", course), part)


def transcript_parts(course, root="."):
    """Every part id with a transcript PDF, package or no package.

    🔴🔴 WHY THIS IS NOT `course_rows`, AND IT IS A BIGGER BLIND SPOT THAN THE
    COVERAGE FLOOR WAS. `course_rows` walks `courses/<CODE>/packages/*/index.html`,
    so a lecture with no mirrored slide package has no row and is invisible to
    this tool entirely. Measured 2026-09-15:

        child development   5 packages   37 transcripts
        affective disorders 9 packages   50 transcripts
        mindfulness        29 packages   38 transcripts

    **43 of 125 transcripts are reachable through packages; 82 are not.** A
    course delivered as Articulate Rise has almost none, so the whole of Child
    and Adolescent was outside the survey before this existed, and `--survey
    PSY101 --deep` returned "nothing to recover" in 0.6 seconds. ⚠️ **A check
    that answers that fast has not looked**, which is this project's own rule
    about a suspiciously quick pass.

    🟢 The coverage ratio genuinely needs a package (it compares transcript words
    against the audio's seconds). **The presence test needs only the PDF**, so
    the deep pass takes the wider set and says `-` where no ratio exists rather
    than inventing a zero.
    """
    seen = []
    folder = os.path.join(root, "materials", course)
    for path in sorted(glob.glob(os.path.join(folder, "*.pdf"))):
        name = os.path.basename(path)
        if " - Transcript" not in name:
            continue
        part = name.split(" - ")[0].strip()
        if part and part not in seen:
            seen.append(part)
    return seen


def words_per_page(pdf):
    """A free ranking of how cleanly a PDF's text extracts. No OCR, one call.

    Used only to CHOOSE the control subjects, never to judge one: a file whose
    words are drawn rather than set scores near zero here, which is exactly the
    file that must not become the yardstick for OCR.
    """
    pages = page_count(pdf)
    if not pages:
        return 0.0
    out = FIX.run([FIX.PDFTOTEXT or "pdftotext", str(pdf), "-"])
    return len((out.stdout or "").split()) / pages if out.returncode == 0 else 0.0


def control_sample(course, root=".", size=None):
    """The transcripts to check OCR against: the ones that extract most cleanly.

    🔴 **The obvious rule is wrong and this tool shipped with it for an hour.**
    "The highest coverage ratio" picked `W5-T2-P3`, whose ratio is 1.07 because
    its transcript carries the dialogue of an embedded consultation VIDEO, which
    `captions.coverage` warns about in its own docstring. A transcript with words
    nobody speaks is a strange yardstick for reading.

    🟢 Ranked instead by words per page, which is free, needs no OCR, and is
    directly the property a control subject must have: text that extracts.
    """
    healthy = [r for r in course_rows(course, root) if r["ratio"] >= CAP.COVERAGE_FLOOR]
    scored = []
    for row in healthy:
        pdf = transcript_pdf(course, row["part"], root)
        if pdf:
            scored.append((words_per_page(pdf), row["part"], row, pdf))
    # Descending density, then part name, so the sample is the same on every run.
    scored.sort(key=lambda s: (-s[0], s[1]))
    return [(row, pdf) for _, _, row, pdf in scored[:(size or CONTROL_SAMPLE)]]


def course_rows(course, root="."):
    """Every lecture in the course with a coverage row, worst ratio first."""
    rows = []
    pattern = os.path.join(root, "courses", course, "packages", "*", "index.html")
    for idx in sorted(glob.glob(pattern)):
        part = idx.split(os.sep)[-2]
        got = CAP.coverage(course, part, root)
        if got:
            got["course"] = course
            rows.append(got)
    rows.sort(key=lambda r: r["ratio"])
    return rows


def median(values):
    ordered = sorted(values)
    if not ordered:
        return 0.0
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def run_control(course, root=".", part=None, dpi=RENDER_DPI):
    """OCR transcripts that already read cleanly and diff them. Returns a dict.

    🔴 **The MEDIAN of several, not one file's number, and the reason is measured.**
    Agreement on cleanly extracting transcripts in this corpus ranges 0.9917 to
    0.9984, so a single subject against a 0.9935 floor passes or fails on which
    lecture happened to be picked. **Two of the four cleanest files here would
    have failed a floor that the other two clear comfortably**, and none of them
    has anything wrong with it. One subject is a coin flip; the median of several
    is a measurement of the READER, which is what this control is about.
    """
    if part:
        got = CAP.coverage(course, part, root)
        if not got:
            return {"ok": False, "why": "no coverage row for %s %s" % (course, part)}
        got["course"] = course
        pdf = transcript_pdf(course, part, root)
        if not pdf:
            return {"ok": False, "why": "no transcript pdf for %s %s" % (course, part)}
        sample = [(got, pdf)]
    else:
        sample = control_sample(course, root)
    if not sample:
        return {"ok": False, "why": "no lecture in %s clears the coverage floor, "
                                    "so there is nothing healthy to check against" % course}

    subjects = []
    for row, pdf in sample:
        got = ocr_whole(pdf, dpi)
        if not got.strip():
            return {"ok": False, "why": "OCR produced nothing for %s: is tesseract on "
                                        "PATH?" % row["part"]}
        acc, diffs = agreement(T.pdf_text(pdf), got)
        subjects.append({"part": row["part"], "pdf": pdf, "accuracy": acc, "diffs": diffs})

    mid = median([s["accuracy"] for s in subjects])
    return {"ok": mid >= ACCURACY_FLOOR, "accuracy": mid, "subjects": subjects,
            "floor": ACCURACY_FLOOR,
            "why": ("" if mid >= ACCURACY_FLOOR else
                    "OCR agreed with the extractor on a median of %.2f%% across %d "
                    "transcripts that read cleanly, against a floor of %.2f%%. Something "
                    "changed under this tool and nothing should be rewritten until it is "
                    "understood." % (mid * 100, len(subjects), ACCURACY_FLOOR * 100))}


def candidates(course, root=".", deep=False, cache=None):
    """Every lecture the survey rules out, with the second measurement and a verdict.

    🔴 The list is DERIVED, never written down. `captions.coverage` decides who is
    looked at and the probe decides who is touched, so a fifth broken lecture is
    found by running this and a repaired one drops out of it.

    🔴🔴 `deep` EXISTS BECAUSE THE COVERAGE RATIO IS THE FIRST GATE, AND IT WAS
    HIDING THE DEFECT MORE COMPLETELY THAN THE FACTOR WAS. Measured on the mindfulness course
    2026-09-15: 3 of its 29 lectures are below the floor, so 26 were skipped
    before any measurement was taken at all, and `W1-T2-P2` at 0.83 was one of
    them. **A lecture can be captioned end to end and still be shown the
    recogniser's guess for four sentences of it**, which is precisely the state
    a coverage ratio is blind to.

    ⚠️ `deep` is off by default and that is a cost decision, not a confidence
    one: it is whole-file OCR for every lecture in the course, about 5 seconds a
    page, against a few seconds for the shallow pass. `survey` says out loud how
    many lectures it did not deep-check, because a check nobody ran must not read
    as a check that passed.
    """
    out = []
    rows = list(course_rows(course, root))
    if deep:
        known = {r["part"] for r in rows}
        rows += [{"part": part, "course": course, "words": 0, "ratio": None,
                  "seconds": 0.0, "expected": 0.0}
                 for part in transcript_parts(course, root) if part not in known]
    for row in rows:
        # 🟢 `None` is "there is no coverage row", which is a different fact from
        # a low ratio and must not be rounded into one.
        shallow = row["ratio"] is not None and row["ratio"] < CAP.COVERAGE_FLOOR
        if not (shallow or deep):
            continue
        pdf = transcript_pdf(course, row["part"], root)
        if not pdf:
            row.update({"pdf": None, "factor": 0.0, "probes": [], "verdict": LEAVE,
                        "lost": None, "note": "no transcript pdf: OCR has nothing to read"})
            out.append(row)
            continue
        factor, probes = (hidden_factor(pdf) if shallow else (0.0, []))
        lost = lost_speech(pdf, cache) if deep else None
        row.update({"pdf": pdf, "factor": factor, "probes": probes, "lost": lost,
                    "verdict": verdict(factor, lost["lost_words"] if lost else None),
                    "note": ""})
        out.append(row)
    # 🟢 A lecture whose ratio is fine and whose text layer is whole is not a
    # candidate for anything; carrying it would bury the rows that are.
    return [r for r in out
            if (r["ratio"] is not None and r["ratio"] < CAP.COVERAGE_FLOOR)
            or r["verdict"] != LEAVE]


VERDICT_SAYS = {
    RECOVER: "🔴 drawn text: OCR would recover it",
    LEAVE: "🟢 words are readable: short for another reason, OCR cannot help",
    ASK: "⚠️ between the thresholds: a person decides, nothing done",
}


def says(row):
    """The verdict line for one row, naming the ROUTE that produced it.

    🔴 `VERDICT_SAYS[RECOVER]` reads "drawn text", which is true of the factor's
    route and false of this one: a lecture selected by the presence test extracts
    perfectly and is missing sentences anyway. **A verdict that misdescribes why
    it fired sends the reader looking at the wrong thing in the file.**
    """
    lost = row.get("lost") or {}
    if row["verdict"] == RECOVER and lost.get("lost_words", 0) >= LOST_RECOVER_AT:
        return ("🔴 %d word(s) of speech are absent from the text layer: OCR "
                "would recover them" % lost["lost_words"])
    if row["verdict"] == ASK and lost.get("lost_words"):
        return ("⚠️ %d word(s) absent from the text layer, below the bar for "
                "rebuilding it: a person decides" % lost["lost_words"])
    return VERDICT_SAYS[row["verdict"]]


def print_rows(rows):
    # 🟢 The lost column appears only when something measured it. A column of
    # dashes would read as "nothing lost" for a check that never ran.
    deep = any(r.get("lost") for r in rows)
    print("%-9s %-10s %8s %8s %9s %8s  %s"
          % ("course", "part", "words", "ratio", "factor",
             "lost" if deep else "", "verdict"))
    for r in rows:
        lost = r.get("lost")
        print("%-9s %-10s %8d %8s %9s %8s  %s"
              % (r["course"], r["part"], r["words"],
                 "-" if r["ratio"] is None else "%.2f" % r["ratio"],
                 "%.2f" % r["factor"],
                 ("%dw" % lost["lost_words"]) if lost else "",
                 says(r)))
        if lost and lost["worst"]:
            print("%30s  %s" % ("", lost["worst"][:88]))


def survey(root=".", course=None, deep=False, cache=None):
    courses = [course] if course else sorted(
        os.path.basename(os.path.dirname(p))
        for p in glob.glob(os.path.join(root, "courses", "*", "packages")))
    rows, skipped = [], 0
    for code in courses:
        rows.extend(candidates(code, root, deep, cache))
        if not deep:
            rows = course_rows(code, root)
            skipped += (sum(1 for r in rows if r["ratio"] >= CAP.COVERAGE_FLOOR)
                        + len(set(transcript_parts(code, root))
                              - {r["part"] for r in rows}))
    if not deep:
        # 🔴 SAY WHAT WAS NOT CHECKED. A lecture above the coverage floor can
        # still be missing sentences from the middle of its transcript, and this
        # pass cannot see that. A silent skip is indistinguishable from a pass.
        print("⚠️ %d lecture(s) are above the coverage floor and were NOT "
              "deep-checked: a whole transcript can be long enough and still "
              "lose sentences. Run with --deep to read every page (about 5s a "
              "page, cached afterwards).\n" % skipped)
    if not rows:
        # 🟢 A POSITIVE RESULT. Silence on success is what makes a working check
        # indistinguishable from a missing one.
        #
        # ⚠️ AND IT HAS TO SAY WHICH QUESTION IT ANSWERED. The old sentence is
        # about the coverage ratio alone. After a deep run it would read as
        # though the text layers had been checked when, before this, they had
        # not been: `--survey --deep` on child development returned it in 0.6 seconds over a
        # course whose lectures were never in the set at all.
        if deep:
            read = sum(len(transcript_parts(code, root)) for code in courses)
            print("%d transcript(s) read end to end: every one of them holds the "
                  "words OCR can see, and every lecture's transcript is long "
                  "enough to be its own narration. Nothing to recover." % read)
        else:
            print("every lecture's transcript is long enough to be its own narration: "
                  "nothing to recover.")
        return 0
    print_rows(rows)
    counted = {v: sum(1 for r in rows if r["verdict"] == v) for v in (RECOVER, ASK, LEAVE)}
    if deep:
        lost = sum((r.get("lost") or {}).get("lost_words", 0) for r in rows)
        print("\n%d word(s) of KCL's own speech are absent from the text layers "
              "read, across %d lecture(s)."
              % (lost, sum(1 for r in rows if (r.get("lost") or {}).get("lost_words"))))
    print("\n%d would be recovered, %d need a person, %d are short for another reason."
          % (counted[RECOVER], counted[ASK], counted[LEAVE]))
    if counted[ASK]:
        # 🔴 NAME THE BAND THAT ACTUALLY FIRED. Every `ask` in a deep run can come
        # from the lost-words route, and a sentence about the factor's band then
        # sends the reader to look at a number that had nothing to do with it.
        by_lost = sum(1 for r in rows if r["verdict"] == ASK
                      and (r.get("lost") or {}).get("lost_words"))
        if by_lost:
            print("⚠️ %d of those lose speech but less than %d words of it: the "
                  "words really are gone, and what is uncertain is whether "
                  "rebuilding a whole text layer is worth its own risk."
                  % (by_lost, LOST_RECOVER_AT))
        if counted[ASK] - by_lost:
            print("⚠️ The middle band is deliberate: between a factor of %.1f and "
                  "%.1f acting either way is a guess." % (LEAVE_BELOW, RECOVER_ABOVE))
    return 0


def report_control(res):
    for s in res.get("subjects", []):
        print("   %-10s %.4f agreement, %d differing regions"
              % (s["part"], s["accuracy"], len(s["diffs"])))
    if not res.get("ok"):
        print("🔴 CONTROL FAILED: %s" % res.get("why", "unknown"))
        return 1
    # 🟢 THE POSITIVE RESULT. A check that is silent on success is one nobody can
    # tell apart from a check that was never written.
    print("🟢 control: OCR agrees with the extractor on a median of %.2f%% across %d "
          "clean transcripts, floor %.2f%%."
          % (res["accuracy"] * 100, len(res.get("subjects", [])), res["floor"] * 100))
    worst = max(res.get("subjects", []), key=lambda s: len(s["diffs"]), default=None)
    if worst:
        print("   the widest disagreement is %s, and its first regions are:" % worst["part"])
        for tag, truth, got in worst["diffs"][:6]:
            print("     %-8s extractor=%-30r ocr=%r" % (tag, truth[:28], got[:28]))
    return 0


def recover(course, root=".", parts=None, dry_run=False, deep=True, cache=None):
    """Control first, then recover, then re-measure. Returns a process exit code.

    ⚠️ `deep` defaults to TRUE here and to False in `survey`, and the asymmetry is
    deliberate: this function is about to spend minutes rebuilding text layers, so
    the minutes the presence test costs are not what anybody is weighing, and a
    recovery run that could not see half the defect is the worse trade.
    """
    res = run_control(course, root)
    if report_control(res):
        return 1

    rows = [r for r in candidates(course, root, deep, cache) if r["verdict"] == RECOVER]
    if parts:
        rows = [r for r in rows if r["part"] in parts]
        missing = sorted(set(parts) - {r["part"] for r in rows})
        if missing:
            print("🔴 refusing: %s is not a lecture this tool would recover. Run "
                  "--survey and read its verdict." % ", ".join(missing))
            return 1
    if not rows:
        print("nothing in %s carries drawn text: nothing to recover." % course)
        return 0

    print("\n%d to recover in %s:" % (len(rows), course))
    print_rows(rows)
    if dry_run:
        print("\n--dry-run: nothing was written.")
        return 0

    failed = 0
    for r in rows:
        print("\n[%s] %s" % (r["part"], os.path.basename(r["pdf"])))
        # 🔴🔴 READ THE OLD TEXT LAYER BEFORE THE REBUILD REPLACES IT. Taken
        # afterwards this reads the repaired file against itself, reports that
        # nothing was dropped, and does so on every file for ever: a check that
        # cannot fail is worse than no check, because it reads as a pass.
        before_text = T.pdf_text(r["pdf"])
        line = FIX.fix_one(Path(r["pdf"]))
        print("      %s" % line)
        if line.startswith(("FAIL", "SKIP")):
            failed += 1
            continue
        # 🔴 ONE ROW COUNTS ONCE, however many ways it disappointed. `failed`
        # used to be incremented per FINDING, so a file that both stayed below
        # the floor and recovered nothing subtracted two from a total of one,
        # and the closing line could report fewer recovered than a run had rows.
        bad = False
        gained = None
        after = coverage_row(course, r["part"], root)
        got = after["ratio"] if after else None
        if r["ratio"] is None or got is None:
            # 🟢 No coverage row means no audio to measure against, which is a
            # fact rather than a failure. The presence re-measure below is the
            # one that judges these.
            print("      no coverage row for this lecture: judged on the text "
                  "layer alone")
        else:
            print("      words %d -> %d, ratio %.2f -> %.2f  %s"
                  % (r["words"], after["words"], r["ratio"], got,
                     "🟢 clears the floor" if got >= CAP.COVERAGE_FLOOR
                     else "🔴 STILL below the floor: recovering the words was not enough"))
            if got < CAP.COVERAGE_FLOOR:
                bad = True
            # ⚠️ The weaker of the two answers to "what did this buy", and it is
            # only reached when the presence test did not run on this row. It
            # counts every word OCR added, slide furniture included, where the
            # fall in lost speech counts the lecturer's own.
            gained = after["words"] - r["words"]
        # 🔴🔴 RE-MEASURE WHAT SELECTED IT, NOT SOMETHING ELSE. A lecture picked
        # by the presence test was ALREADY above the coverage floor, so "clears
        # the floor" is true of it before anything is done and proves nothing at
        # all. The honest question is whether the words came back.
        if r.get("lost") and r["lost"]["lost_words"]:
            now = lost_speech(r["pdf"], cache)
            print("      lost speech %d -> %d words  %s"
                  % (r["lost"]["lost_words"], now["lost_words"],
                     "🟢 the text layer now holds them"
                     if now["lost_words"] < r["lost"]["lost_words"]
                     else "🔴 STILL missing: the rebuild did not recover them"))
            if now["lost_words"] >= r["lost"]["lost_words"]:
                bad = True
            gained = r["lost"]["lost_words"] - now["lost_words"]
        # 🔴🔴 THE READ-BACK, AND IT IS THE HALF THE BACKUP RULE EXISTS FOR.
        # `fix_one` replaces the WHOLE text layer rather than patching the holes,
        # so the question "did this take anything away" is a real one. Printed on
        # every file, including when the answer is none: a check that is silent
        # on success cannot be told from one that never ran.
        drop = dropped_speech(before_text, T.pdf_text(r["pdf"]))
        ok = rebuild_paid_off(drop["lost_words"], gained)
        print("      dropped %d word(s) the old text layer held  %s"
              % (drop["lost_words"],
                 "🟢 nothing the extractor could read was lost" if not drop["lost_words"]
                 else ("⚠️ against %s recovered" % ("%d" % gained if gained else "nothing")
                       if ok else
                       "🔴 MORE THAN IT RECOVERED: restore the backup and look")))
        if drop["lost_words"] and drop["worst"]:
            print("        worst: %s" % drop["worst"][:96])
        # 🔴 WHERE a run landed, which the two counters above cannot ask: both
        # read `insert` opcodes and a run spliced inside a word is a `replace`.
        # Printed on every file, zero included, for the same reason as the line
        # above it.
        spl = spliced_runs(before_text, T.pdf_text(r["pdf"]))
        print("      %d run(s) spliced inside a word; %d put back text the file "
              "already held  %s"
              % (spl["splices"], spl["displaced"],
                 "🟢 none" if not spl["splices"] else
                 ("🟢 every one of them filling the hole that fused the word"
                  if not spl["displaced"] else
                  "🔴 READ THESE: a run moved off its own sentence")))
        if spl["worst"]:
            print("        worst: %s" % spl["worst"][:96])
        # 🔴 AND THE SAME QUESTION AT A WHOLE-WORD BOUNDARY, which the line
        # above cannot ask: a run put between two words is an `insert`, and
        # the presence test calls it safe because the words are all still
        # there. **Measured over the 15 recovered lectures: 48 of these
        # against the 1 the splice counter sees**, so leaving it out reported
        # a corpus-wide defect as a single instance.
        dis = displaced_runs(before_text, T.pdf_text(r["pdf"]))
        print("      %d run(s) moved off their own sentence; %d undecidable  %s"
              % (dis["moved"], dis["ambiguous"],
                 "🟢 none" if not dis["moved"] else
                 "🔴 READ THESE: the words are all present and in the wrong order"))
        if dis["worst"]:
            print("        worst: %s" % dis["worst"][:96])
        # ⚠️ A displacement does NOT fail the run, and that is deliberate: it
        # loses no speech, so `rebuild_paid_off` has nothing to weigh. It is
        # loud here because a jumbled junction is something a PERSON decides
        # about, not something arithmetic can settle.
        failed += bad or not ok
    print("\n%d of %d rebuilt and read back clean; %d want a person."
          % (len(rows) - failed, len(rows), failed))
    print("🟢 The file each rebuild replaced is beside it in backups/, dated.")
    print("⚠️ Captions are NOT built by this: run captions.py for that.")
    return 1 if failed else 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--survey", nargs="?", const="", metavar="COURSE",
                    help="what would be recovered and why; writes nothing")
    ap.add_argument("--control", metavar="COURSE",
                    help="run the accuracy control on its own")
    ap.add_argument("--recover", metavar="COURSE",
                    help="control, then recover, then re-measure")
    ap.add_argument("parts", nargs="*", help="limit --recover to these parts")
    ap.add_argument("--dry-run", action="store_true",
                    help="with --recover: name the files and write nothing")
    ap.add_argument("--root", default=".")
    ap.add_argument("--deep", action="store_true",
                    help="read every page of every transcript and report KCL "
                         "speech that is absent from the text layer; about 5s a "
                         "page the first time, cached after that")
    ap.add_argument("--cache", default=str(OCR_CACHE), metavar="DIR",
                    help="where the page readings are remembered (default: %s)"
                         % OCR_CACHE)
    a = ap.parse_args(argv)

    if a.control:
        return report_control(run_control(a.control, a.root))
    if a.recover:
        return recover(a.recover, a.root, a.parts or None, a.dry_run,
                       deep=True, cache=a.cache)
    return survey(a.root, a.survey or None, a.deep, a.cache)


if __name__ == "__main__":
    sys.exit(main())
