#!/usr/bin/env python3
"""The transcript PDFs, cut into per-slide blocks. The WORDS half of captions.

    python3 server/transcripts.py --self-test           # prove the parser can fire
    python3 server/transcripts.py --survey PSY101       # every package, blocks vs audio
    python3 server/transcripts.py --blocks PSY101 W1-T2-P1
    python3 server/transcripts.py --videos             # embedded-video markers, every course

🔴 **THIS RUNS AT GENERATION TIME AND IS NEVER IMPORTED BY THE SERVER.** The
captions ship as data; a recipient gets `.vtt` files, not a transcription
pipeline. Nothing here may be imported from `study_server.py`, which is how that
stays true rather than merely intended.

🔴 **WHY THE WORDS COME FROM HERE AND NOT FROM WHISPER.** Whisper mishears
exactly what this corpus is made of: `hypomania`, `subsyndromal`, `Merikangas`,
drug names. We already hold the correct words. Whisper is for TIMINGS only, and
the two are joined by `difflib` elsewhere.

## 🔴 THE PARSER TRAP, MEASURED, AND IT DROPS WHOLE SLIDES IN SILENCE

`pdftotext` emits a **form feed** (`\\x0c`) at each page break, and a `Slide N:`
marker can land immediately after one. It is then not at the start of a
`\\n`-delimited line. Measured on `W1-T2-P1`, whose transcript carries three form
feeds and has one marker directly behind one:

    text.split(chr(10)) + '^ *Slide \\d+:'      13   <-- WRONG, one slide lost
    text.splitlines()   + '^ *Slide \\d+:'      14
    text.split(chr(10)) + '^\\s*Slide \\d+:'     14
    text.splitlines()   + '^\\s*Slide \\d+:'     14
    unanchored finditer                        14

**8 of 50 transcripts in this course lose at least one marker to a `\\n`-only
split.** ⚠️ A dropped marker does not crash: the previous slide's block simply
swallows the next slide's words, **so that slide's captions are wrong and every
downstream check still passes.**

🟢 **So this file does not split into lines at all.** An unanchored scan over the
whole text agrees with the correct answer in every case above and cannot be
defeated by either half of the trap. `--self-test` fires the trap on purpose.

## 🔴 AND THE ENTRY'S ONE-TO-ONE PAIRING DOES NOT GENERALISE. MEASURED.

The queue entry says a package's transcript blocks and its narration files are
"an exact one-to-one pairing, derivable rather than guessed", from a sample of
one. **Across all nine packages of the LABELLED course here it holds for six
and fails for three**, and `--survey` prints the table rather than this comment asserting it:

    W5-T1-P2   19 audio   20 blocks
    W5-T2-P1   13 audio   12 blocks
    W5-T2-P2    7 audio   12 blocks

**Nor are the slide numbers contiguous**: `W5-T2-P1` runs 3, 5, 6, 7, 9, 12 ...

🔴 **And nothing on disk says which clip belongs to which slide.** The files are
`sound1.mp3 ... soundN.mp3` in flat order, `index.html` lists them as
`<audio id="sndK_...">` with no slide named, and no `slideN.js` mentions a sound
file at all. The link lives inside the minified player.

🟢 **Which is why the join is DERIVED rather than looked up**: Whisper's own
rough text for a clip is matched against these blocks, the best match wins, and a
weak best match means "no captions for this clip" instead of the wrong ones. That
also makes the three mismatched packages ordinary rather than special.

## 🔴🔴 AND THE OTHER COURSE HAS NO SLIDE LABELS AT ALL. ALL 29 PACKAGES.

**One course's transcripts are continuous prose.** A title block, a presenter, and
then paragraphs: no `Slide N:` anywhere, in any of them. `--survey` on it
reports `0 blocks` for every package and it is right to.

🔴 **So the queue entry's mechanism, "parse the transcript into blocks and pair
by slide number", covers ONE of the two courses** — and EH's own reported case
(Mindfulness W3 T3 P3) is in the one it does not cover.

🟢 **The derive-by-matching join survives this, which is the argument for it.**
An unlabelled transcript is simply a single span to align into rather than a list
of them: each clip's rough text is matched into the document, and because the
clips are in order the search is MONOTONIC — clip N's span begins at or after
clip N-1's end. Per-clip chunking still bounds a mis-alignment to one clip.

⚠️ **So slide labels become a CROSS-CHECK rather than the mechanism.** Where they
exist they confirm an alignment cheaply; where they do not, nothing is lost that
the alignment was relying on. `shape_of()` says which kind a transcript is, and
neither answer is an error.
"""

import argparse
import collections
import glob
import os
import re
import shutil
import subprocess
import unicodedata
import sys

# The one local import, and it is a leaf with none of its own: the sentence that
# says which of two transcript files for one part is CURRENT lives there, shared
# with the server's side panel and the pack copier, which cannot import this
# module (the server may never import it; see the docstring).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import material_names                                           # noqa: E402

# 🔴 UNANCHORED, DELIBERATELY. See the trap above: any pattern tied to the start
# of a line can be defeated by a form feed, and being tied to nothing cannot.
SLIDE_MARK = re.compile(r"\bSlide\s+(\d+)\s*:")

# 🔴🔴 THE SAME MARKER WITHOUT ITS COLON, AND IT IS A WHOLE COURSE'S WORTH.
# `SLIDE_MARK` requires the colon because a LABELLED transcript writes
# `Slide 13:` and the parser splits on it. The Mindfulness transcripts write
# `Slide 2` on its own line with no colon: **measured, 22 to 40 bare markers per
# lecture and ZERO with a colon**, so `shape_of` correctly calls them unlabelled
# and nothing ever removed the markers.
# ⚠️ **THIS DOES NOT MAKE THEM LABELLED.** Routing them down the block-parsing
# path would be a different and much larger change; they have no colon to split
# on and their blocks are not delimited. **This only strips the words**, which is
# what an unlabelled transcript needs, and it leaves `shape_of` alone.
# 🟢 Anchored to a LINE, so a sentence that happens to say "on Slide 4 you can
# see" is untouched: only a line that is nothing but the marker goes.
# ⚠️ `\f` IS IN THE LEADING CLASS because a PDF page break lands on the same line
# as the first marker of the page (`\x0cSlide 2`), which an anchor without it
# misses. Measured: it is the difference between 27 and 28 of 30 markers going.
# ⚠️ A marker may carry a LABEL in brackets, `Slide 1 (Title)` and `Slide 2
# (Topic List)`: measured, 14 builds of one course write both, and a marker with
# a label is still nothing but a marker. The label is bounded and bracket-free
# so a sentence in brackets on the same line can never be mistaken for one.
BARE_SLIDE_LINE = re.compile(
    r"^[ \t\f]*Slide\s+\d+(?:[ \t]*\([^()\n]{1,40}\))?[ \t\f]*$", re.M)

# 🔴 A MARKER MID-LINE IS LEFT ALONE, DELIBERATELY, and it is 2 of 30 on the
# worst lecture: the extractor sometimes runs a marker into the sentence before
# it (`to the controls. Slide 13 Specifically, during`). **Stripping those needs
# a rule that cannot tell them from a lecturer SAYING "on Slide 4 you can see"**,
# and eating a real sentence is worse than leaving two stray numbers in a
# transcript that is already only used for matching.

# The stock sentence a transcript prints for a slide with no narration. It is
# not speech and it is not a marker, so neither rule above reaches it.
# ⚠️ Every rule below captures its leading whitespace, form feed included, and
# `strip_unspoken` puts that back: the page break is what `spoken_text`'s cover
# test reads, and a rule that ate it would hide a cover (see `spoken_text`).
NO_AUDIO_LINE = re.compile(
    r"^([ \t\f]*)No Audio on this Slide\.?[ \t]*$", re.M | re.I)

# 🔴🔴 THE SAME FACT IN A SECOND SPELLING, AND IT WAS A CAPTION IN 19 LESSONS.
# A transcript built from a course page's per-slide text writes a bracketed
# marker for a silent slide, and `NO_AUDIO_LINE` knew only the sentence above,
# so the marker went to the aligner as two words it then had to put SOMEWHERE
# in the audio. Measured 2026-09-16 across one course's 29 recordings: 54
# markers fed in, 32 cues showing them, two as a cue of their own and thirty
# spliced into the middle of speech; and the last real words of two lectures
# were dragged under the unconfirmed floor by the markers after them. Same rule
# as the sentence: a line that is nothing but the marker is furniture.
#
# 🔴 TWO SPELLINGS, ON PURPOSE. The builder wrote `(no narration)` until
# 2026-09-17 and writes `(silent slide)` since (the owner's word for the audio
# is "narration", so a marker using it read as a claim about the recording).
# The old arm stays because transcripts built BEFORE the rename are still on
# disk and a caption can be rebuilt from any of them: a matcher that knew only
# the new spelling would feed the old marker to the aligner again, which is the
# defect measured above. Remove the old arm only when no document carries it.
SILENT_SLIDE_LINE = re.compile(
    r"^([ \t\f]*)\((?:no narration|silent slide)\)[ \t]*$", re.M | re.I)

# The editorial note the same builder writes when one recording covers two
# slides and the seam between them could not be found. A reader needs it; the
# aligner does not, and it is 31 words that are in no audio anywhere. It wraps
# across lines, so the rule runs to the closing bracket rather than to the
# line's end, and the bracket-free class keeps it from crossing into a second
# note. Two openings for the same reason as `SILENT_SLIDE_LINE`: the note was
# reworded on 2026-09-17 and documents carrying the old wording still exist.
CONTINUED_NOTE = re.compile(
    r"^([ \t\f]*)\((?:Narration for this slide is included|"
    r"The words for this slide are) on the previous slide"
    r"[^()]*\)[ \t]*$", re.M)

# A link's label, copied from the course page along with the slide's text.
# Nobody reads a button aloud, and it reached a reader as a caption once.
LINK_LABEL_LINE = re.compile(r"^([ \t\f]*)View Paper[ \t]*$", re.M)

#: Every line a transcript prints that nobody speaks, in one place, so that
#: both shapes of transcript strip the same set (`captions.spoken_source` runs
#: it on the labelled branch, `_strip_furniture` on the unlabelled one).
UNSPOKEN_LINES = (NO_AUDIO_LINE, SILENT_SLIDE_LINE, CONTINUED_NOTE,
                  LINK_LABEL_LINE)

#: The last resort, not the answer: `find_pdftotext` looks first, because this
#: path is right on the machine that wrote it and wrong on an Intel Mac and on
#: every Mac the kit is installed on. Kept so a caller that passes nothing on a
#: machine where nothing is found still fails the old way (an OSError that
#: `pdf_text` turns into ""), not a new one.
PDFTOTEXT = "/opt/homebrew/bin/pdftotext"


def find_pdftotext(named=None):
    """`pdftotext`, found rather than assumed: a named one, then
    `STUDY_HUB_PDFTOTEXT`, then the PATH, then the two places Homebrew puts it.

    🔴 Returns None when there is none, and a caller that reports readiness must
    say so rather than counting zero passages: a check that could not run and a
    check that found nothing print the same number, and only one is good news.
    The install (`install_captions`) cannot supply this one; poppler does.
    """
    for cand in (named, os.environ.get("STUDY_HUB_PDFTOTEXT"),
                 shutil.which("pdftotext"),
                 "/opt/homebrew/bin/pdftotext", "/usr/local/bin/pdftotext"):
        if cand and os.path.exists(cand):
            return cand
    return None


class Block(object):
    """One slide's words, and the number the transcript gave it."""

    __slots__ = ("slide", "text")

    def __init__(self, slide, text):
        self.slide = slide
        self.text = text

    def __repr__(self):
        return "Block(slide=%d, %r)" % (self.slide, self.text[:40])

    def __eq__(self, other):
        return (isinstance(other, Block)
                and self.slide == other.slide and self.text == other.text)


def pdf_text(path, binary=None, layout=False, heal=True):
    """The transcript as text, or "" when it cannot be read.

    `binary` names the pdftotext to run; None means `find_pdftotext()`, falling
    back to the hard-coded `PDFTOTEXT` so the failure on a bare machine is the
    same "" it always was.

    ⚠️ Returns "" rather than raising, because a sweep over fifty files should
    report the one it could not read and keep going. The caller can tell the
    difference: no text means no blocks, and `--survey` prints that as a row.

    🔴 `layout=True` PASSES `-layout`. It used to decide whether
    `strip_running_footer` could work at all, **and that is no longer true**:
    the footer rule stopped requiring a page edge on 2026-09-15, and the
    comparison INVERTED with it. Over all 125 transcripts, through
    `captions.spoken_source`: the reflowed default now leaves **14** footer
    occurrences against **55** from `-layout`, where before the fix it was 151
    against 63. **Reflow is no longer the problem it was**, and the remaining
    question about the flag is which extraction is more faithful on a scanned
    page, which is a different judgement and not this function's to make.
    ⚠️ The default is UNCHANGED, because the callers that read `Slide N:` blocks
    were tuned against reflowed text. **That the flag cannot move a block
    boundary is now measured** (`rig_layout_blocks.py`), so the objection that
    kept it unused is gone; what remains is a per-file judgement.

    🔴 `heal=False` TURNS OFF THE DROPPED-`ti` REPAIR, and exists so the repair
    can be compared against what the extractor actually said. **See
    `drops_ti`**: one transcript's font carries a `ti` the PDF does not map, so
    `pdftotext` emits a space and `interventions` arrives as `interven ons`.
    The repair fires only on a document containing NO `ti` at all, and only on
    fragments it can prove: across this corpus that is **one file of 125**.
    """
    binary = binary or find_pdftotext() or PDFTOTEXT
    cmd = [binary] + (["-layout"] if layout else []) + [str(path), "-"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True)
    except (OSError, ValueError):
        return ""
    if r.returncode != 0:
        return ""
    text = r.stdout
    # 🟢 THE GUARD RUNS FIRST AND IS CHEAP: `re.search` for `ti` stops at the
    # first hit, which in ordinary prose is within the first sentence, so 124
    # of the 125 transcripts pay almost nothing and never load a word list.
    if heal and drops_ti(text):
        text, _ = heal_dropped_ti(text)
    return text


# --------------------------------------------------------------------------
# A font whose `ti` the extractor cannot read
# --------------------------------------------------------------------------

#: Where a system word list lives, most likely first. **This is the only
#: external data this module reads**, and it is optional: with no list the
#: repair below is SKIPPED and says so, rather than guessing.
DICT_PATHS = ("/usr/share/dict/words", "/usr/dict/words")

#: 🟢 Enough words for "this document contains no `ti`" to MEAN anything. The
#: shortest transcript in this corpus is 541 words and carries 42, and the
#: affected one carries 0 in 3,171. ⚠️ **MEASURED INERT: any floor from 1 to
#: 3,171 gives the same answer here**, which is the honest kind of floor. It is
#: not tuning the result; it is refusing a question a short document cannot
#: answer.
TI_FLOOR = 200

#: A run of letters, and the single space or tab that may once have been a
#: ligature. 🔴 **The glyph extracts as a SPACE, not as nothing** -
#: `interventions` arrives as `interven ons`, codepoints `...6e 20 6f...` - so
#: the word is SPLIT rather than shortened, and a rule looking for a MISSING
#: character would find nothing at all.
#:
#: ⚠️ **`[^\W\d_]` RATHER THAN `[A-Za-z]`, and it is not tidiness.** The same
#: PDFs carry `ﬁ`, `ﬂ` and `ﬃ` as single codepoints, which ARE mapped and do
#: extract. **`diﬃcul es` is `difficulties`**, and an ASCII-only class cannot
#: see the left fragment at all, so that word stays broken for ever.
TI_TOKEN = re.compile(r"[^\W\d_]+", re.UNICODE)

#: How many fragments one word may have been split into. 🔴 **Two is not
#: enough: `cul va ng` is `cultivating`, three fragments and two dropped
#: ligatures.** ⚠️ **MEASURED: 4 is inert on this corpus** (no word needs it),
#: so it is headroom rather than a tuned number, and 3 is the deepest any real
#: word here goes.
TI_MAX_PIECES = 4

#: Endings `/usr/share/dict/words` does not carry. web2 is a 1934 dictionary
#: with few inflections: it has `emotion` and not `emotions`, `investigate` and
#: not `investigated`. **Measured: teaching the test these six endings takes the
#: repair from 114 words to 130 and adds no false positive anywhere in the
#: corpus.**
TI_SUFFIXES = ("s", "es", "d", "ed", "ing", "ly")

#: A run of letters, for counting how long a document is. Deliberately its own
#: pattern rather than `TI_GAP`'s group: this one is asked "how much prose is
#: here", which is a different question from "where might a word have split".
TI_WORD = re.compile(r"[A-Za-z']+")

_WORDS = None


def word_list(paths=DICT_PATHS):
    """The system word list, or None when this machine has none.

    🟢 **None rather than an empty set, and the difference is the whole point**:
    an empty set makes every fragment look unknown and would repair wildly,
    while None is a state the caller can report. **A check that is silent when
    it could not run is indistinguishable from one that passed.**
    """
    for path in paths:
        try:
            with open(path, encoding="utf-8", errors="ignore") as fh:
                got = frozenset(w.strip().lower() for w in fh if w.strip())
            if got:
                return got
        except OSError:
            continue
    return None


def known_word(word, words):
    """Whether `word` is English, allowing the inflections web2 omits.

    🔴 **THE LOOKUP IS NORMALISED AND THE TEXT IS NOT.** These PDFs carry `ﬁ`,
    `ﬂ` and `ﬃ` as single codepoints, so `diﬃculties` is not `difficulties` to
    any word list. **NFKD here, nowhere else**: the repair must leave the
    reader's characters exactly as the extractor wrote them, and only the
    QUESTION being asked about them is normalised. `captions.plain` does the
    same thing for the same reason.
    """
    if not words:
        return False
    low = unicodedata.normalize("NFKD", word).lower()
    if low in words:
        return True
    # 🟢 `-ies` to `-y` is its own case because the stem CHANGES rather than
    # being trimmed: web2 has `difficulty` and this corpus writes
    # `diﬃcul es`, which is `difficulties`. Nothing else reaches it.
    if low.endswith("ies") and (low[:-3] + "y") in words:
        return True
    for suffix in TI_SUFFIXES:
        if not low.endswith(suffix):
            continue
        stem = low[:-len(suffix)]
        # 🟢 `+ "e"` covers `investigate` -> `investigated` and
        # `alleviate` -> `alleviating`, which is where most of the gain is.
        if stem in words or (stem + "e") in words:
            return True
    return False


def drops_ti(text, floor=TI_FLOOR):
    """Whether this extraction lost EVERY `ti`, which is a fact about the font.

    🔴🔴 **THE DEFECT: one transcript's font carries a `ti` ligature the PDF
    does not map to Unicode, so `pdftotext` emits a space for it.** The page
    RENDERS correctly, so anybody reading the PDF sees `interventions`; only
    extraction loses it, which is why nothing noticed. **No extraction flag
    recovers it**: `-layout` and `-raw` were both tried and both return 0.

    🟢 **THE SIGNAL IS ZERO AGAINST DOZENS, NOT A THRESHOLD.** Measured over all
    125 transcripts of this corpus: the affected file contains `ti` **0** times
    in 3,171 words; **the next lowest contains it 52 times**, and the median
    density is 71 per thousand words. **`ti` is unavoidable in English prose**
    (`time`, `until`, `action`, `continue`), so its total absence is a
    statement about the FONT rather than about the writing.
    """
    if len(TI_WORD.findall(text or "")) < floor:
        return False
    return not re.search(r"ti", text, re.I)


def heal_dropped_ti(text, words=None):
    """Rejoin words a dropped `ti` split in two or more. Returns (text, joins).

    🔴 **THE DISCRIMINATOR IS TWO-SIDED AND BOTH HALVES ARE LOAD-BEARING**: the
    joined form must be a word, AND the two fragments must not BOTH be words.
    **Either half alone is useless.** A rule that only asks whether the join is
    a word makes **117 false joins across the 124 clean transcripts** -- `as an`
    to `astian`, `the need` to `thetineed`, `can only` to `cantionly`, all of
    which a full dictionary accepts.

    ⚠️ **"THE LEFT FRAGMENT MUST NOT BE A WORD" WAS THE FIRST SHAPE OF THAT AND
    IT COST 29 REAL REPAIRS**, because `/usr/share/dict/words` is a 235,000-word
    list carrying plenty of obscure English: `par`, `pa`, `regula`, `media`,
    `sec` and `an` are all in it, so `par cipants`, `pa ents`, `regula ng` and
    `media ng` were all refused. 🟢 **Asking instead that they not BOTH be words
    recovers all 29 and costs ONE false join in the whole clean corpus**
    (`can co` to `cantico`), which cannot occur in practice because the healer
    only ever runs on a file `drops_ti` has already flagged.

    🔴🔴 **IT TAKES THE LONGEST JOIN, NOT THE FIRST, AND THAT IS THE WHOLE
    CORRECTNESS ARGUMENT.** `cul va ng` is `cultivating`. Joining the first
    pair it can gives `cul` + `vating` -- and **`vating` passes a dictionary
    test**, because `vat` is a word and `-ing` is an ending. ⚠️ **That is the
    one failure direction that matters: a fragment left split is VISIBLY wrong,
    while an invented word is not**, so a rule that stops at the first
    plausible join manufactures exactly the damage nobody will catch. Trying
    the longest span first means `cultivating` is found before `vating` is ever
    considered.

    🟢 **CONTROL: across all 125 transcripts this proposes a join in exactly ONE
    file**, the one `drops_ti` flags, and none anywhere else.
    """
    words = word_list() if words is None else words
    # ⚠️ A FAST PATH, NOT THE SAFETY. A 13-mutant sweep left exactly one
    # survivor here, and it is EQUIVALENT rather than a gap: `known_word`
    # returns False for an empty vocabulary, so an empty set repairs nothing
    # whichever way this line is written. **Recorded so the next sweep does not
    # spend an hour on it, and so nobody deletes `known_word`'s own guard on
    # the grounds that this one covers it.**
    if not words:
        return text, 0
    out, joins, pos = [], 0, 0
    while True:
        m = TI_TOKEN.search(text, pos)
        if not m:
            out.append(text[pos:])
            break
        out.append(text[pos:m.start()])
        pieces, ends = [m.group(0)], [m.end()]
        here = m.end()
        while len(pieces) < TI_MAX_PIECES:
            nxt = re.compile(r"[ \t](?=[^\W\d_])", re.UNICODE).match(text, here)
            if not nxt:
                break
            after = TI_TOKEN.match(text, nxt.end())
            if not after:
                break
            pieces.append(after.group(0))
            ends.append(after.end())
            here = after.end()
        # 🔴 LONGEST FIRST. See the docstring: stopping at the first join that
        # passes is how `cultivating` becomes `cul vating`.
        for n in range(len(pieces), 1, -1):
            candidate = "ti".join(pieces[:n])
            if not known_word(candidate, words):
                continue
            # 🔴🔴 ONE OF THE FIRST TWO FRAGMENTS MUST NOT BE A WORD, and this
            # is the whole of the safety. **A gap where BOTH sides are English
            # is an ordinary space**: `as an` joins to `astian`, `the need` to
            # `thetineed` and `can only` to `cantionly`, every one of which
            # `/usr/share/dict/words` accepts. **Measured on the 124 clean
            # transcripts, which are a perfect control because none of them
            # has a dropped ligature: without this, 117 false joins.**
            if known_word(pieces[0], words) and known_word(pieces[1], words):
                continue
            out.append(candidate)
            joins += n - 1
            pos = ends[n - 1]
            break
        else:
            out.append(m.group(0))
            pos = m.end()
    return "".join(out), joins


def parse_blocks(text):
    """`Slide N:` blocks, in the order the transcript states them.

    Each block runs from its own marker to the next one, or to the end. The
    marker itself is dropped; the words are stripped of surrounding whitespace
    and of the form feeds that produced the trap in the first place.
    """
    if not text:
        return []
    marks = list(SLIDE_MARK.finditer(text))
    out = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        body = text[m.end():end]
        out.append(Block(int(m.group(1)), clean(body)))
    return out


def clean(body):
    r"""One block's words as a single flowed paragraph.

    `pdftotext` wraps at the page width and pages break mid-sentence, so the
    line breaks inside a block are an artefact of the PDF rather than anything
    the speaker did. Collapsing them is what makes the text alignable against a
    transcriber's output, which has no line breaks at all.

    🔴 THE FORM FEED IS HANDLED HERE AND NEEDS NO LINE OF ITS OWN. This used to
    carry an explicit `body.replace("\x0c", " ")` above the collapse. A mutation
    deleting that line SURVIVED, and it was right to: `\s` already matches a form
    feed, so the line was inert. **It was deleted rather than given a test**,
    because a test for a distinction that does not exist teaches the next reader
    that one does. The form feed still never reaches a block, and
    `test_a_form_feed_never_survives_into_a_block` still says so.
    """
    return re.sub(r"\s+", " ", body).strip()


LABELLED, UNLABELLED, EMPTY = "labelled", "unlabelled", "empty"


def shape_of(text):
    """Which kind of transcript this is. **Neither kind is an error.**

    ⚠️ Do not read `UNLABELLED` as "malformed". Both courses' transcripts are
    what their department produced, and one of them simply never numbered the
    slides. A tool that treats the second as a failure is a tool that refuses
    EH's own course.
    """
    if not text or not text.strip():
        return EMPTY
    return LABELLED if SLIDE_MARK.search(text) else UNLABELLED


def spoken_text(text):
    """The words a narrator actually says, with the cover page dropped.

    🔴 EVERY TRANSCRIPT OPENS WITH MATTER NOBODY SPEAKS: the module name, the
    week, the topic, the presenter and their job title. Aligning a clip against
    that is aligning against words that are not in the audio, and it is worst at
    exactly the place it does most damage: the FIRST clip, whose span would be
    dragged up into the cover block.

    The cover ends at the first blank line following a run of short unpunctuated
    lines, which is what a title block is. ⚠️ Conservative on purpose: when the
    shape is not recognised the text is returned whole, because dropping real
    narration is worse than keeping a title.

    🔴🔴 THE PAGE BREAK IS TRIED FIRST, AND THE BLANK-LINE RULE ALONE WAS MISSING
    THE COVER ON A WHOLE COURSE. QA measured it: on the Mindfulness transcripts
    the first blank line falls after `Lecture Transcript / Module Name`, two
    lines in, **so those two were stripped and the real cover survived**, module,
    week, topic, `Lecturer`, `Dr. Ruimin Ma`, `Department of Psychological
    Medicine`. 🟢 A form feed is the PDF's own page boundary and is where a cover
    page actually ends.

    ⚠️ **THE TITLE-BLOCK TEST IS KEPT AND IS WHAT MAKES THIS SAFE.** Most first
    pages are NOT covers: measured across all 88 transcripts on this machine, a
    first page runs to 2,886 characters and 43 lines where the narration starts
    on page one. **Dropping page one unconditionally would delete real speech
    from most of the corpus.** 🟢 **Measured with the test in place: 20 of 88
    change, every one dropping 193 to 281 characters of genuine cover, and 68 are
    untouched.**

    🔴 The cost is not the match score. It is that the cover's words reach
    everything downstream that reads this: the caption text, and the vocabulary
    the recogniser is primed with, where `Lecturer`, `Ruimin`, `Department` and
    `Medicine` took **35 of 420 term slots across six lectures** for words nobody
    ever says.
    """
    if not text:
        return ""
    # 🔴🔴 THE COVER GOES FIRST AND THE FURNITURE SECOND, AND I HAD IT THE OTHER
    # WAY ROUND. Stripping the markers first removes `\f` from `\x0cSlide 2`,
    # which is the very page boundary the cover test depends on, **so the cover
    # came back and the prompt filled up with `Lecturer, Ruimin, Department`
    # again.** Caught by printing the resulting prompt, not by a test.
    # The PDF's own page boundary, tried before the blank-line heuristic.
    if "\f" in text:
        first, rest = text.split("\f", 1)
        if _is_title_block([l.strip() for l in first.splitlines() if l.strip()]):
            return clean(_strip_furniture(strip_running_footer(rest)))
    # 🔴 AFTER THE COVER DECISION, DELIBERATELY, AND BEFORE THE FURNITURE.
    # After: the cover test counts the lines on page one, and a footer is four of
    # them, so stripping first would newly call some 14-line pages a cover.
    # That may even be right, but it is a different change and it can only fail
    # by deleting narration. Before the furniture, because `BARE_SLIDE_LINE`
    # eats the `\f` in `\x0cSlide 2` and the page boundaries are what this reads.
    text = _strip_furniture(strip_running_footer(text))
    parts = re.split(r"\n\s*\n", text, maxsplit=1)
    if len(parts) != 2:
        return clean(text)
    head, rest = parts
    if not _is_title_block([l.strip() for l in head.splitlines() if l.strip()]):
        return clean(text)
    return clean(rest)


def _strip_furniture(text):
    """Slide markers and every unspoken line: printed, never spoken."""
    return strip_unspoken(BARE_SLIDE_LINE.sub("", text))


def strip_unspoken(text):
    """Remove every line in `UNSPOKEN_LINES`, keeping any page break on it.

    🟢 Safe to run before `shape_of` and before `parse_blocks`: none of the
    rules can match a `Slide N:` label, so the shape a transcript is read as
    cannot change, and the form feed each rule captures is written back."""
    for rule in UNSPOKEN_LINES:
        text = rule.sub(r"\1", text)
    return text


# --------------------------------------------------------------------------
# A video embedded in a lecture: its dialogue is printed, and never spoken
# --------------------------------------------------------------------------

# 🔴 A LECTURE CAN EMBED A VIDEO, AND THE TRANSCRIPT PRINTS ITS DIALOGUE BETWEEN
# TWO MARKERS: `<title> [Video]` ... `<title> [Video] End`. **None of it is in
# the slide audio and none of it ever can be**: the package holds the slides'
# own clips and no video file, so the aligner is right to place none of those
# words, and they were counting against a coverage figure they were never
# eligible for. Measured on the three transcripts of one course's week that
# carry the markers, 7%, 41% and 73% of the words sat inside a video, and the
# 41% lecture read 0.584 covered with every one of its clips captioned.
#
# ⚠️ THE MARKER ARRIVES MANGLED, exactly as this project's other OCR debris does:
# `C o nsultatio n 2 [V id e o ] E nd`, and once with `}` closing the bracket.
# Both are tolerated, because a marker that is only recognised when the
# extractor spelled it well is a marker that fails on the transcripts it is
# for. A marker may sit beside the slide's own label (`Slide 4: <title>
# [Video]`); the label stays, so the block keeps its number and a labelled
# transcript keeps its shape.
#
# 🔴 THE MARKERS ARE NOT RELIABLY PAIRED, AND THE FAILURE DIRECTION IS CHOSEN:
# keep words rather than drop speech. A start pairs only with the next end
# that carries the SAME title (whitespace and case aside, since the spacing is
# the extractor's); anything else is an unpaired marker, and an unpaired
# marker removes itself and nothing after it. **Every region and every
# unpaired marker is reported**, because a stripper that silently swallowed
# the rest of a transcript would be a worse defect than the one it fixes.
VIDEO_MARK = re.compile(
    r"\[\s*v\s*i\s*d\s*e\s*o\s*[\]}](?P<end>[ \t]*e\s*n\s*d\b)?", re.I)

#: A marker's title runs back along its own line to the line's start or to the
#: last colon before it (`Slide 4:`). Longer than this and it is not a title
#: but a sentence the extractor ran the marker into, which stays as words.
VIDEO_TITLE_MAX = 60

Video = collections.namedtuple("Video", "title start end words")
"""One stripped region: its title as the transcript prints it, the slice of
the text it occupied, and how many whitespace-separated words it held."""

Unpaired = collections.namedtuple("Unpaired", "kind title at")
"""A marker that found no partner: `kind` is `start` or `end`."""


def _video_title(text, at):
    """The title printed before a marker on its own line, and where it begins.
    Empty, and beginning at the marker, when the line is too long to be one."""
    line_start = text.rfind("\n", 0, at) + 1
    colon = text.rfind(":", line_start, at)
    begin = colon + 1 if colon >= 0 else line_start
    title = text[begin:at]
    if len(title) > VIDEO_TITLE_MAX or "[" in title or "]" in title:
        return "", at
    return " ".join(title.split()), begin


def _same_title(a, b):
    return re.sub(r"\s+", "", a).casefold() == re.sub(r"\s+", "", b).casefold()


def video_regions(text):
    """Every embedded video the transcript marks: `(regions, unpaired)`.

    A region runs from where its start marker's title begins to the end of its
    end marker. Pairing is by order AND by title: a start is closed by the next
    end that names the same video; an end with no open start, an end naming a
    different video, and a start still open at the end of the text or when
    another start arrives are all `Unpaired`, and strip nothing but themselves.
    """
    regions, unpaired = [], []
    open_ = None                                    # (title, begin, marker end)
    for m in VIDEO_MARK.finditer(text or ""):
        title, begin = _video_title(text, m.start())
        if not m.group("end"):
            if open_ is not None:
                unpaired.append(Unpaired("start", open_[0], open_[1]))
            open_ = (title, begin, m.end())
            continue
        if open_ is None or not _same_title(open_[0], title):
            unpaired.append(Unpaired("end", title, begin))
            continue
        regions.append(Video(open_[0], open_[1], m.end(),
                             len(text[open_[1]:m.end()].split())))
        open_ = None
    if open_ is not None:
        unpaired.append(Unpaired("start", open_[0], open_[1]))
    return regions, unpaired


def strip_video_regions(text):
    """The text with every paired video region removed and every unpaired
    marker removed, as `(text, regions, unpaired)`.

    🟢 A transcript with no marker comes back as the very same object, which is
    what lets a caller prove this changed nothing for the other lectures. Page
    breaks inside a region are kept, so the cover-page test downstream still
    sees the page boundaries it counts.
    """
    regions, unpaired = video_regions(text)
    if not regions and not unpaired:
        return text, regions, unpaired
    cuts = [(r.start, r.end) for r in regions]
    for u in unpaired:
        m = VIDEO_MARK.search(text, u.at)
        cuts.append((u.at, m.end()))
    out, last = [], 0
    for a, b in sorted(cuts):
        if a < last:                    # an unpaired end inside a region: gone already
            continue
        out.append(text[last:a])
        out.append("\f" * text.count("\f", a, b))
        last = b
    out.append(text[last:])
    return "".join(out), regions, unpaired


def video_sweep(paths, read=None, log=print):
    """Which transcripts carry a video marker, one row each, then the count.

    🟢 A POSITIVE RESULT ON PURPOSE: the last line says how many of how many,
    so a run that examined nothing cannot read the same as a run that found
    nothing. `read` turns a path into text and defaults to `pdf_text`.
    """
    read = read or pdf_text
    carrying, unpaired_total = 0, 0
    for path in paths:
        regions, unpaired = video_regions(read(path))
        if not regions and not unpaired:
            continue
        carrying += 1
        unpaired_total += len(unpaired)
        words = sum(r.words for r in regions)
        log("%s: %d region(s), %d words inside, %d unpaired marker(s)"
            % (os.path.join(os.path.basename(os.path.dirname(path)),
                            os.path.basename(path)),
               len(regions), words, len(unpaired)))
        for r in regions:
            log("    %-40s %5d words" % (r.title or "(untitled)", r.words))
        for u in unpaired:
            log("    UNPAIRED %s marker %r at %d, kept the words after it"
                % (u.kind, u.title, u.at))
    log("%d of %d transcripts carry a video marker; %d unpaired marker(s)"
        % (carrying, len(paths), unpaired_total))
    return carrying


def library_transcripts(root="."):
    """Every current transcript PDF under `materials/`, every course, sorted."""
    return material_names.current(
        glob.glob(os.path.join(root, "materials", "*", "* - Transcript*.pdf")))


#: How many non-blank lines at each end of a page can be page furniture. The
#: measured footer is 3 lines plus a page number; 5 leaves room without reaching
#: into a paragraph.
FOOTER_ZONE = 5
#: A footer is a LABEL, not a sentence. The measured one is four words.
FOOTER_WORDS = 8

#: 🔴🔴 WHAT MAKES A LINE FURNITURE WHEN IT IS NOWHERE NEAR A PAGE EDGE: it
#: carries the page number that makes a running footer RUNNING, or the rights
#: mark of a credit line. **Neither is anybody's wording**, which is the
#: constraint `strip_running_footer` is built under: a rule matching
#: `3Playmedia` works until a course changes captioner and then fails silently.
#: A numeral that varies page to page, and the © that says "this is a rights
#: notice", are conventions of the PAGE rather than of the course.
#:
#: ⚠️ **DELIBERATELY NARROWER THAN "it has two or more words".** That was the
#: first shape of this and it is the one to argue against: it gains exactly the
#: same nine keys on this corpus and it would also delete a repeated multi-word
#: line the lecturer SAYS, which the test below has pinned since this rule was
#: written. **Identical benefit, strictly more harm, so the narrow one ships.**
FOOTER_MARK = re.compile(r"[0-9\u00a9\u00ae]")


def _footer_key(line):
    """What two occurrences of the same running footer have in common.

    🔴 **THE PAGE NUMBER IS THE WHOLE DIFFICULTY**: the footer reads
    `© King's College London 1.` on page one and `... 5.` on page five, so the
    lines are never equal and a set would hold five distinct strings. Digits go,
    and so does punctuation and case, leaving the part that actually repeats.

    🟢 A line that is ONLY a number is a page number and keys as one, rather than
    keying as the empty string and colliding with every other digits-only line.
    """
    # 🔴🔴 A SLIDE MARKER IS NOT FOOTER, AND REMOVING ONE IS FATAL.
    # `Slide 13:` is what `parse_blocks` segments a labelled transcript ON, and
    # it recurs at page boundaries exactly like a footer does. An earlier version
    # of this deleted 34 of them across the corpus. **A line another rule owns is
    # that rule's to remove.**
    #
    # 🔴🔴 **AND THE BARE FORM COUNTS, which it did not until 2026-09-15.** The
    # exemption was keyed to `SLIDE_MARK` alone, and `SLIDE_MARK` requires the
    # colon that 77 of this corpus's transcripts do not write. **While the rule
    # still demanded a page edge that cost nothing measurable; the moment the
    # `FOOTER_MARK` route below was added it became load-bearing, because
    # `Slide 10` carries a numeral and the route reads a numeral as furniture.**
    # ⚠️ **MEASURED: without this line, `slide` becomes a confirmed footer key in
    # 76 of 125 transcripts** and the markers are deleted wholesale. **That is
    # this rule's oldest warning arriving by a new route.**
    if SLIDE_MARK.search(line) or BARE_SLIDE_LINE.search(line):
        return None
    bare = re.sub(r"[^a-z0-9]+", " ", line.lower()).strip()
    if not bare:
        return None
    if re.fullmatch(r"[0-9 ]+", bare):
        return "\x00page-number"
    key = re.sub(r"\s+", " ", re.sub(r"[0-9]+", "", bare)).strip()
    return key or None


def strip_running_footer(text):
    """Lines repeating at a MAJORITY of page boundaries, which nobody speaks.

    🔴🔴 **THIS IS WHY IT EXISTS**: a running footer was being spliced into the
    lecturer's sentence and shown to the reader as speech. `sound3.vtt` of
    `W4-T1-P1`, at 00:01:29, read *"for Week 4 Transcripts by 3Playmedia
    © King's College London 1."* ⚠️ **On the anchored path the cue text comes
    from the TRANSCRIPT, so this is not a mishearing and no confidence score can
    see it: it is confidently wrong words at a real moment.**

    🔴🔴 **STRUCTURAL, AND DELIBERATELY NOT KEYED TO ANY WORDING.** A rule
    matching `3Playmedia`, or this course's copyright line, works until a course
    uses a different captioner and then **fails silently**. That is the same
    mistake as a reserved slot that matched the word "pack" and missed
    "Brain regions". **A running footer is identifiable by SHAPE: a short line
    that recurs at the end (or start) of most of a document's pages.**

    ⚠️ **ORDER VARIES BETWEEN PAGES and a block rule would miss it.** Measured on
    `W4-T1-P1`: four pages read `Week 4 / Transcripts by 3Playmedia /
    © King's College London / <n>.` and the fifth swaps the first two. **So this
    counts lines independently rather than matching a run of them.**

    🟢 **CONSERVATIVE IN THE SAME DIRECTION AS THE COVER TEST**: it needs at
    least three pages to see a pattern at all, only considers short lines, and
    needs a strict MAJORITY. A transcript whose shape it does not recognise is
    returned whole, because dropping real narration is worse than keeping a
    footer.

    🔴🔴 **A LINE NEED NOT SIT AT A PAGE EDGE, since 2026-09-15**, and the
    sentence that used to say "only looks at the ends of pages" was the defect.
    **Furniture qualifies by carrying a page numeral or a rights mark
    (`FOOTER_MARK`), OR by having been seen at an edge on two pages** -- see the
    two-route comment in the body for the measurement and the limit.

    ⚠️ **WHAT KEEPS THE EDGE ROUTE HONEST, measured rather than argued.** The
    obvious reading of "the recurrence signal is strong, drop the zone" deletes
    real speech. **In one lecture of the corpus the subject word itself stands
    alone on a line on a majority of pages**, three times as the first or last
    word of a sentence the extractor wrapped (*"...is a central characteristic
    of / mindfulness."*). **A lecture repeats its own subject about once a page
    by construction**, so recurrence alone cannot mean furniture. It carries no
    numeral and no rights mark, so `FOOTER_MARK` leaves it alone.
    """
    if not text or "\f" not in text:
        return text
    pages = text.split("\f")
    # 🔴 Two pages cannot establish a "majority" worth the name: one repetition
    # is a coincidence, and the rule would start eating short repeated lines.
    if len(pages) < 3:
        return text
    # 🔴🔴 TWO COUNTS, AND THE SPLIT IS THE WHOLE OF THE ACCURACY. Measured on
    # `W2-T1-P1`, the last file in the corpus still carrying a footer
    # after the first version of this: the credit line sits 9 and 10 lines from
    # the end on two of its five pages, because the extractor emits trailing
    # matter after it. **Counting only the zone saw it on 2 pages of 5, missed
    # the majority, and left the whole document untouched.**
    #   `zone`    -- pages carrying it AT A BOUNDARY. This is what makes it
    #                FURNITURE rather than a repeated phrase, and 2 is enough
    #                to establish that.
    #   `anywhere` -- pages carrying it as a short standalone line at all. This
    #                is what the MAJORITY is counted over.
    # 🟢 A line has to satisfy both, so a refrain the lecturer actually says
    # never qualifies (it is not at a page edge) and a footer the extractor
    # displaced still does.
    zone_hits, anywhere, marked = {}, {}, {}
    for page in pages:
        lines = [l for l in page.splitlines() if l.strip()]
        edge = set(lines[:FOOTER_ZONE] + lines[-FOOTER_ZONE:])
        seen_here, seen_edge = set(), set()
        for line in lines:
            if len(line.split()) > FOOTER_WORDS:
                continue
            key = _footer_key(line)
            if not key:
                continue
            seen_here.add(key)
            # 🟢 Marked ANYWHERE it occurs, not only where it recurs: the
            # extractor mangles the numeral on some pages (`PAGE | 1` reads as
            # `PAGE |` once in this corpus) and one clean occurrence is enough to
            # establish what the line IS.
            if FOOTER_MARK.search(line):
                marked[key] = True
            if line in edge:
                seen_edge.add(key)
        for key in seen_here:
            anywhere[key] = anywhere.get(key, 0) + 1
        for key in seen_edge:
            zone_hits[key] = zone_hits.get(key, 0) + 1
    # 🔴 A STRICT majority of the pages, not half: on a 4-page document "appears
    # twice" is exactly the coincidence this is meant not to fire on.
    need = len(pages) // 2 + 1
    # 🔴🔴 TWO ROUTES TO "THIS IS FURNITURE", NOT ONE GATE, and that is the whole
    # of the 2026-09-15 change. **The page edge is EVIDENCE of furniture, never a
    # requirement of it**: whether a line sits near an edge is a fact about the
    # EXTRACTOR, not about the document, and `pdftotext` without `-layout`
    # reflows this course's footer into the middle of the page. Measured there:
    # index 15 to 22 of ~40 non-blank lines, so `FOOTER_ZONE` never saw it and
    # `© King's College London` was spliced into the lecturer's sentence.
    #
    # ⚠️ **RAISING `FOOTER_ZONE` IS THE WRONG FIX** and is why this was an entry
    # rather than a one-liner: a zone big enough to reach index 22 covers most of
    # the page, and the rule starts eating speech.
    #
    # 🟢 **STRICTLY A WIDENING, and that is provable rather than hoped**: every
    # key the old rule confirmed still satisfies `zone_hits >= 2`, so this can
    # only ever remove MORE. Measured over 125 transcripts: it gains 9 keys
    # across 50 files (`king s college london` in 21, `page` in 15) and loses
    # NOTHING (including six OCR keys `ek`, `ah`, `td`, `dt`, `ma`, `ay`) that
    # only the edge route can justify, which is why that route stays.
    #
    # 🔴 **AND IT DOES NOT CLOSE THE CLASS.** A footer with neither a numeral nor
    # a rights mark, sitting away from every page edge, is still invisible here.
    # None exists in this corpus; the honest statement is that this widens the
    # rule and the next course may widen it again.
    footer = {k for k, n in anywhere.items()
              if n >= need and (marked.get(k) or zone_hits.get(k, 0) >= 2)}
    if not footer:
        return text
    # 🟢 Removal is NOT limited to the zone, because the zone's job was to prove
    # these lines are furniture and it has done it. A standalone short line that
    # keys to a confirmed footer is that footer wherever the extractor put it.
    out = []
    for page in pages:
        kept = [l for l in page.splitlines()
                if not (len(l.split()) <= FOOTER_WORDS
                        and _footer_key(l) in footer)]
        out.append("\n".join(kept))
    return "\f".join(out)


def _is_title_block(lines):
    """Short lines, not many of them, few ending in a full stop.

    🔴 The same test on both paths, so the page-break route and the blank-line
    route cannot disagree about what a cover looks like. **It is the whole of the
    conservatism**: without it, dropping the first page would delete narration
    from most of this corpus.
    """
    if not lines or len(lines) > 12:
        return False
    if [l for l in lines if len(l) > 120]:
        return False
    return len([l for l in lines if l.endswith(".")]) <= len(lines) // 2


def audio_files(package_dir):
    """`soundN.mp3` in NUMERIC order, which is not what sorting gives you.

    🔴 `sorted()` puts sound10 before sound2, and a caption attached to the
    wrong clip is worse than no caption.
    """
    paths = glob.glob(os.path.join(str(package_dir), "data", "sound*.mp3"))
    def n(p):
        m = re.search(r"sound(\d+)\.mp3$", p)
        return int(m.group(1)) if m else 0
    return sorted(paths, key=n)


def transcript_for(materials_dir, doc_id):
    """The CURRENT transcript PDF for one part, or None.

    Named `<DOC> - Transcript (<whatever the course called it>).pdf`, and the
    parenthesised half varies per course and per week, so it is matched by
    prefix rather than reconstructed.

    🔴 A corrected transcript's superseded twin matches the same prefix and sits
    in the same folder (nothing is deleted). It is not a candidate, by name and
    not by sort order: until 2026-09-17 the corrected one won only because `(`
    sorts before `s`, and the captions would have been built from the OLD words
    the day a name sorted the other way. A folder holding only a superseded
    file answers None, so a corrected transcript that failed to write looks
    missing rather than fine.
    """
    hits = material_names.current(
        glob.glob(os.path.join(str(materials_dir), "%s - Transcript*.pdf" % doc_id)))
    return hits[0] if hits else None


# --------------------------------------------------------------------------
# The checks. A parser that has never been seen to drop a slide is not a parser
# anybody should trust, so the trap is fired here on purpose.
# --------------------------------------------------------------------------

# 🔴 THREE SEPARATE HAZARDS, and each one is here because a test that only
# covered the others went green over a real defect:
#   1. a form feed INSIDE a block's words (an ordinary mid-slide page break),
#   2. a form feed immediately BEFORE a marker (the one that loses a slide),
#   3. a marker arriving mid-line, because the extractor wraps paragraphs.
TRAP = (
    "Slide 1: The opening words.\n"
    "More of the opening\x0cwords, wrapped by the extractor.\n"
    "\x0cSlide 2: The words that a line split loses.\n"
    "Slide 3: And a marker that arrives mid-line, "
    "which is the other half of it: Slide 4: here.\n"
)


def self_test():
    """Prove the parser catches both halves of the measured trap."""
    bad = []

    naive = [l for l in TRAP.split("\n") if re.match(r"^ *Slide \d+:", l)]
    if len(naive) != 2:
        bad.append("the fixture no longer reproduces the trap: a naive split "
                   "found %d markers, expected 2" % len(naive))

    got = parse_blocks(TRAP)
    if [b.slide for b in got] != [1, 2, 3, 4]:
        bad.append("parse_blocks found %r, expected [1, 2, 3, 4]"
                   % [b.slide for b in got])

    if got and got[0].text != "The opening words. More of the opening words, wrapped by the extractor.":
        bad.append("block 1 did not flow into one paragraph: %r" % got[0].text)

    if got and "\x0c" in "".join(b.text for b in got):
        bad.append("a form feed survived into a block's text")

    if parse_blocks("no markers at all here") != []:
        bad.append("text with no markers should give no blocks")

    order = audio_files_order_check()
    if order:
        bad.append(order)

    for line in bad:
        print("FAIL: %s" % line)
    if not bad:
        print("self-test: the trap fires, all four markers are found, "
              "blocks flow, and sound10 sorts after sound2.")
    return 1 if bad else 0


def audio_files_order_check():
    """sound10 must not sort before sound2."""
    names = ["data/sound%d.mp3" % i for i in (1, 2, 10, 11)]
    def n(p):
        return int(re.search(r"sound(\d+)\.mp3$", p).group(1))
    got = sorted(reversed(names), key=n)
    return None if got == names else "numeric ordering is broken: %r" % got


def survey(course, root="."):
    """Every package in a course: blocks against narration files.

    🔴 An ARTIFACT rather than a count, on purpose. Two runs of this can be
    compared with each other; "it was six out of nine" can only be compared with
    somebody's memory.
    """
    materials = os.path.join(root, "materials", course)
    packages = os.path.join(root, "courses", course, "packages")
    if not os.path.isdir(packages):
        print("no packages for %s" % course)
        return 1
    docs = sorted(d for d in os.listdir(packages)
                  if os.path.isdir(os.path.join(packages, d)))
    print("%-12s %-10s %6s %7s %7s  %s"
          % ("part", "shape", "audio", "blocks", "match", "slide numbers"))
    agree = 0
    shapes = {}
    for doc in docs:
        pdf = transcript_for(materials, doc)
        n_audio = len(audio_files(os.path.join(packages, doc)))
        if not pdf:
            print("%-12s %-10s %6d %7s %7s  no transcript pdf"
                  % (doc, "-", n_audio, "-", "-"))
            shapes["no pdf"] = shapes.get("no pdf", 0) + 1
            continue
        text = pdf_text(pdf)
        shape = shape_of(text)
        shapes[shape] = shapes.get(shape, 0) + 1
        blocks = parse_blocks(text)
        if shape != LABELLED:
            print("%-12s %-10s %6d %7s %7s  %d chars of prose, no slide labels"
                  % (doc, shape, n_audio, "-", "n/a", len(spoken_text(text))))
            continue
        same = n_audio == len(blocks)
        agree += 1 if same else 0
        nums = [b.slide for b in blocks]
        run = "%d..%d" % (nums[0], nums[-1]) if nums else "-"
        gaps = sorted(set(range(nums[0], nums[-1] + 1)) - set(nums)) if nums else []
        print("%-12s %-10s %6d %7d %7s  %s%s"
              % (doc, shape, n_audio, len(blocks), "yes" if same else "NO", run,
                 ("  gaps at %s" % ",".join(str(g) for g in gaps)) if gaps else ""))
    labelled = shapes.get(LABELLED, 0)
    print("\nshapes: " + ", ".join("%s %d" % (k, v) for k, v in sorted(shapes.items())))
    if labelled:
        print("%d of %d LABELLED packages have as many blocks as narration files."
              % (agree, labelled))
    print("🔴 The pairing is NOT the join, and in an unlabelled course there is "
          "nothing to pair. The clip a span belongs to is DERIVED by matching.")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--self-test", action="store_true",
                    help="prove the parser catches the form-feed trap")
    ap.add_argument("--survey", metavar="COURSE",
                    help="blocks against narration files, every package")
    ap.add_argument("--blocks", nargs=2, metavar=("COURSE", "DOC"),
                    help="print one part's blocks")
    ap.add_argument("--videos", action="store_true",
                    help="which transcripts, every course, mark an embedded "
                         "video whose dialogue is never in the slide audio")
    ap.add_argument("--root", default=".", help="repo root (default: .)")
    a = ap.parse_args(argv)

    if a.self_test:
        return self_test()
    if a.survey:
        return survey(a.survey, a.root)
    if a.videos:
        video_sweep(library_transcripts(a.root))
        return 0
    if a.blocks:
        course, doc = a.blocks
        pdf = transcript_for(os.path.join(a.root, "materials", course), doc)
        if not pdf:
            print("no transcript pdf for %s/%s" % (course, doc))
            return 1
        for b in parse_blocks(pdf_text(pdf)):
            print("--- slide %d (%d chars) ---" % (b.slide, len(b.text)))
            print(b.text)
        return 0
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
