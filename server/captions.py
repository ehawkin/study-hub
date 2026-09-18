"""Captions for a narrated lecture: real words, real timings, at GENERATION time.

    python3 server/captions.py --self-test
    python3 server/captions.py --survey                     # what a course could carry
    python3 server/captions.py --align <course> <part> --cache rough.json
    python3 server/captions.py --build <course> <part> --out <dir> [--cache rough.json]
    python3 server/captions.py --build <course> <part> --out <dir> --engine whisper
    python3 server/captions.py --sidecars <course> [--out <dir>]   # the lectures' records, from files

🔴 **`--rough` was advertised here until 2026-09-09 and was never an argument**,
so line 4 of this file, the first thing anybody copies, failed with *unrecognized
arguments*. **The DOCSTRING was wrong, not the parser**: there is no standalone
rough-words command and none is needed. `--align` and `--build` run whisper
themselves when `--cache` is absent or its file does not exist, and write the
rough words to that path when it is given, so `--cache` is both the way to
produce a rough file and the way to reuse it. Adding a flag would have been
inventing a feature to match a typo. `test_captions_docstring` now parses every
command advertised above and fails if one of them could not run.

🔴 **THIS NEVER RUNS ON A READER'S MACHINE**, and a test walks the server's import
closure to keep it that way. A recipient gets `.vtt` files as data: no model, no
torch, no Python stack. That is what keeps the server stdlib-only.

**The shape, and every part of it was measured rather than assumed:**

1. **The words are the lecturer's own**, from the course transcript. The audio is
   asked only WHEN each word is said, never what it says. 🟢 **Two engines answer
   that** (`ENGINES`): since 2026-09-15 the default is forced alignment
   (`align_ctc`, `time_by_ctc`), which places every transcript word on the audio
   directly and scores it; before it, and still wherever the aligner's
   interpreter is absent, Whisper writes down what it heard and `align` matches
   those words to the transcript. **Every rule below about clips, floors and cue
   shape holds for both**; the Whisper route's output is byte-identical to what
   it was.
2. **The clip a passage belongs to is known**, not derived: the package's own blob
   names each slide's narration clip (`presinfo.sound_for_slide`, off by one).
3. **The alignment is ONE global match over the whole lecture**, not a per-clip
   search. Measured on EH's own lecture: per-clip greedy windows scored 0.87 on the
   first eight clips and then collapsed to 0.0 as one bad span dragged the cursor
   past the rest; the global match holds every clip at 0.79 or better.
4. **A clip that does not match confidently gets NO cues**, which is what a caption
   track does anyway.
5. **The lecture has ONE record** (`captions.json`, `clip_sidecar`), written from
   the files at the end of a build and by `--sidecars` for lectures built before
   it existed: how much of the transcript reached a cue, by the same rule the
   recording route uses, and how many of the clips that play have a track.
"""

import argparse
import difflib
import glob
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unicodedata

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import align_ctc
import presinfo
import transcripts

# 🔴 MEASURED, not chosen. On EH's own lecture every real clip scores 0.787 or
# better and the one clip that genuinely is not in the transcript scores 0.410.
# The floor sits between them with room on both sides. **Raising it costs real
# captions; lowering it lets a clip caption itself with somebody else's words.**
MATCH_FLOOR = 0.6

# 🔴 A RATE NOBODY SPEAKS AT, so a span timed above it is arithmetic rather than
# evidence. **Measured 2026-09-09 over every cue on disk (6,683 across 388 files,
# both courses): median 2.30 words a second, p99 4.26, p99.9 8.87, and the
# fastest cue in the whole corpus is 11.3. NOT ONE clears 12.** On the six
# refused transcript-timed tracks, 206 cues do.
# ⚠️ **It is deliberately far above `FAST_WORDS_PER_SECOND`, because they answer
# different questions.** Six words a second is where a READER stops keeping up
# and a real lecturer can cross it. Twelve is where the claim stops being about
# a lecturer at all: `_timed` has spread words across a gap that cannot hold
# them, and the number is a property of our arithmetic, not of the speech.
SPEECH_CEILING = 12.0

# 🟢 How far the durations of a slide's clips may miss the slide's own length
# and still count as PIECES that all play in turn, rather than takes of which
# one is kept (`playing_clips`). Measured 2026-09-15 on nine such slides across
# two lectures: the widest miss was 0.30 s, over six files summing to 334 s;
# the two re-recorded slides the take rule was built on miss by 39.8 s and
# 109.4 s. One second sits well clear of both.
SEQUENTIAL_SLACK_S = 1.0

# Cue shaping, from ordinary subtitle practice.
CUE_CHARS = 42
CUE_LINES = 2
CUE_MIN = 1.0
CUE_MAX = 6.0

WORD = re.compile(r"[\w']+", re.UNICODE)
CLAUSE_END = re.compile(r"[.!?;:,]")


def plain(text):
    """Ligatures expanded, for MATCHING only.

    🔴 `pdftotext` preserves ligatures as single codepoints, and `ﬁgure` does not
    tokenise as `figure`: it tokenises as `gure`, and `brieﬂy` splits in two.
    **1,229 of them across 51 of the corpus's 88 transcripts**, every one a word
    that would silently fail to match. Found because an aligned caption came back
    reading "this gure from the papers".
    """
    return unicodedata.normalize("NFKD", text or "")


class Word(object):
    """One word of the lecturer's real text, and where it sits in the source."""

    __slots__ = ("text", "key", "start", "end", "at", "to")

    def __init__(self, text, at, to):
        self.text = text            # as written, ligatures and punctuation intact
        self.key = plain(text).lower()
        self.at, self.to = at, to   # character span in the transcript
        self.start = self.end = None

    def __repr__(self):
        return "Word(%r, %s..%s)" % (self.text, self.start, self.end)


def words_of(text):
    """The transcript's words, in order, keeping the lecturer's own spelling.

    🔴 **Tokenise the ORIGINAL, never the normalised copy.** `\\w` already matches a
    ligature codepoint, so `ﬁgure` is one token here and `plain()` turns it into the
    match key `figure`. Running the whole text through `plain()` first would work
    just as well for matching and would quietly rewrite what the reader sees.
    """
    return [Word(m.group(0), m.start(), m.end()) for m in WORD.finditer(text or "")]


def spoken_source(text):
    """The words the narrator actually says, as ONE string to align and slice.

    🔴 **A LABELLED transcript prints `Slide 13:` before each block and nobody says
    it.** Feeding those markers to the matcher costs a match at every block
    boundary, and it is not a rounding error: on a sample of two labelled lectures
    it left **9 of 19 clips below the floor and uncaptioned**, against none in an
    unlabelled one. **The labelled course looked like the easy case and matched
    worse.**

    ⚠️ This is the entry's own ruling arriving in code: *"a labelled transcript is
    a list of spans and an unlabelled one is a single span, same path."* The spans
    are the block BODIES; the labels are stripped, and become a cross-check
    somebody may want later rather than part of the mechanism.
    """
    # 🔴🔴 THE RUNNING FOOTER IS STRIPPED ON BOTH BRANCHES, AND IT USED TO BE ON
    # NEITHER. **The LABELLED branch never calls `spoken_text`**, so every fix
    # made in there reached 74 of the 88 transcripts and missed the 14 that
    # actually carried the reported defect. Both files the entry names
    # (the affective disorders course's `W4-T1-P1` and `W5-T1-P1`) are labelled, and both read
    # *"...personal experience, for Week 4 Transcripts by 3Playmedia © King's
    # College London 1. example..."* -- **furniture spliced into the middle of a
    # sentence and shown to the reader as speech.**
    # ⚠️ Before `shape_of`, safely: `strip_running_footer` never removes a line
    # `SLIDE_MARK` owns, so the labelling this branches on cannot change.
    text = transcripts.strip_running_footer(text)
    # 🔴 AN EMBEDDED VIDEO'S DIALOGUE IS PRINTED AND NEVER SPOKEN, and it is a
    # REGION between two markers rather than a line, so no rule in
    # `UNSPOKEN_LINES` can reach it. After the footer, deliberately: the
    # footer is found by its repeats across pages and a region can hold one.
    # Before the shape test, safely: a slide's own label beside the marker
    # (`Slide 4: <title> [Video]`) is kept. This is the denominator of record
    # for coverage, so up to 73% of one transcript stopped counting against a
    # figure it was never eligible for; `transcripts.strip_video_regions`
    # says what it removed, and `video_report` hands that to a caller.
    text, _regions, _unpaired = transcripts.strip_video_regions(text)
    if transcripts.shape_of(text) == transcripts.LABELLED:
        # 🔴 The unspoken lines too, for the same reason as the footer: the
        # labelled branch never reaches `_strip_furniture`. Twice, because a
        # marker can sit on a line of its own inside a block (caught before
        # the parse, where the rules' line anchors still mean something) or be
        # the whole of a block beside its label (`Slide 7: (no narration)`),
        # which `clean` flows into one line the rules then match entire.
        text = transcripts.strip_unspoken(text)
        bodies = (transcripts.strip_unspoken(b.text)
                  for b in transcripts.parse_blocks(text))
        return " ".join(b for b in bodies if b.strip())
    return transcripts.spoken_text(text)


def video_report(text):
    """What `spoken_source` set aside as embedded-video dialogue, as
    `(regions, unpaired)` from `transcripts.video_regions`, on the same text
    it read (the footer stripped first, exactly as there)."""
    return transcripts.video_regions(transcripts.strip_running_footer(text))


# --------------------------------------------------------------------------
# Which clips actually play
# --------------------------------------------------------------------------

def clip_name(sound):
    """The file a package's sound record plays: `sound0` in the index is
    `sound1.mp3` on disk."""
    return "sound%d.mp3" % (int(str(sound["i"]).replace("sound", "")) + 1)


def playing_seconds(pres):
    """How long the clips a listener hears run, in seconds, from the package's
    own durations, so a lecture's length is known with no audio opened.
    ⚠️ Playing clips only: an abandoned take on a slide is not lecture time."""
    playing = {name for _slide, name in playing_clips(pres)}
    total = 0.0
    for slide in pres.get("s", []):
        for s in slide.get("S", []) or []:
            if clip_name(s) in playing:
                total += float(s.get("d", 0) or 0)
    return total


def playing_clips(pres):
    """[(slide index, filename)] for the clips a listener actually hears.

    🔴 **A slide can carry an ABANDONED TAKE.** Two slides in this corpus do, and
    in both the discarded one is longer than the slide it sits on while the live
    one fits. **It is not a tidy detail**: the dead take's words are not in the
    transcript, so aligning it lets it swallow the real take's words. Measured on
    EH's own lecture, the live clip's score went from 0.240 to 0.787 once the dead
    take was left out.

    ⚠️ **Confirmed by a second witness rather than by the rule alone**: the
    transcript for that slide contains the fitting take's words and not the other's.

    ⚠️ **When a slide has ONE clip it is kept whatever its length.** One slide in
    the corpus holds a 77-second recording on a slide lasting a millisecond, and
    dropping it would lose real narration to a rule about a case it is not in.

    🔴 **AND A SLIDE CAN CARRY PIECES RATHER THAN TAKES, which look like several
    takes and are the opposite.** Found 2026-09-15 on a course whose lecturer
    recorded long slides in two to six files: the files' durations SUM to the
    slide's length (within `SEQUENTIAL_SLACK_S`), so every one of them plays,
    one after another. The take rule kept one file per slide and dropped 13 of
    21 clips in one lecture and 5 of 23 in another, and the transcript's words
    for the dropped files then landed in the neighbouring clip and sank its
    confirmed share under `MATCH_FLOOR`. **The sum is the witness**: a dead take
    beside a live one never adds up to the slide (measured on both re-recorded
    slides: 66.7 s against 26.9, and 215.8 against 106.4), while the pieces do
    to within a third of a second across six files.
    """
    out = []
    for n, slide in enumerate(pres.get("s", [])):
        sounds = slide.get("S", []) or []
        names = [clip_name(s) for s in sounds]
        if len(sounds) < 2:
            out.extend((n, name) for name in names)
            continue
        steps = slide.get("e") or [{}]
        want = steps[0].get("p")
        if want is None:
            out.extend((n, name) for name in names)
            continue
        if abs(sum(s.get("d", 0) for s in sounds) - want) <= SEQUENTIAL_SLACK_S:
            out.extend((n, name) for name in names)
            continue
        fits = [(abs(s.get("d", 0) - want), name) for s, name in zip(sounds, names)]
        fits.sort()
        out.append((n, fits[0][1]))
    return out


# --------------------------------------------------------------------------
# The alignment
# --------------------------------------------------------------------------

def fabricated_time(pairs, ceiling=SPEECH_CEILING):
    """How much of a clip's timing was invented, rather than measured.

    🔴 **THE QUESTION THIS ANSWERS, AND NOTHING ELSE DID.** `_timed` interpolates
    every unmatched word between its matched neighbours, which is a CLAIM about
    when that word was said. Across two anchors that are neighbours in TIME and
    hundreds of words apart in the TRANSCRIPT, the claim comes out at hundreds of
    words a second. **That is not a lecturer speaking quickly; it is division.**

    🔴🔴 **MEASURED 2026-09-09 ON THE SIX LECTURES EH COMPLAINED ABOUT**, and it
    overturned the reading everyone had of them. They were understood to be
    suffering from anchors going evenly sparse, so the fix on the table was to
    let a crowded cue borrow time from the gap beside it. **In fact each lecture
    has between 2 and 9 passages, up to 521 words long, that received NO anchor
    at all and were interpolated into under a second between them** -- 3.9% to
    26.6% of the whole transcript, accounting for 15% to 72% of every unreadable
    cue. **There is no gap beside those cues to borrow from**, which is why the
    proposed fix could not have worked and why this number had to exist first.

    ⚠️ **IT MUST BE COUNTED HERE AND NOT FROM THE CUES.** `_cue` floors a cue at
    `CUE_MIN`, so a cue holding 521 words reads as at most ~18 words a second
    however bad the span was. **The clamp hides the magnitude by an order of
    magnitude**, and the honest number is only visible at the span, before cue
    shaping.

    🟢 **IT COUNTS AND DOES NOT REFUSE**, the same call `legibility` and
    `heard_confidence` made, for the same reason: where the line goes is a
    ruling, and counting turns every future lecture into a data point.

    `pairs` is `[(true_word_index, rough_word)]`, strictly increasing, exactly as
    `_timed` takes it.
    """
    spans = words = 0
    seconds = 0.0
    for k in range(1, len(pairs)):
        (i0, a), (i1, b) = pairs[k - 1], pairs[k]
        between = i1 - i0 - 1
        if between <= 0:
            continue
        dt = b["s"] - a["e"]
        # ⚠️ `dt <= 0` is not an error and is the WORST case, not a missing one:
        # two anchors landing on the same instant with 500 words between them is
        # exactly the shape this exists to find. Guarding it away as a division
        # error would have hidden the finding.
        if dt <= 0 or between / dt > ceiling:
            spans += 1
            words += between
            seconds += max(0.0, dt)
    # 🔴 The denominator travels with the fraction. A share with no count beside
    # it cannot distinguish "none of a lot" from "none of nothing", which is the
    # shape `legibility` was already corrected for.
    total = (pairs[-1][0] - pairs[0][0] + 1) if pairs else 0
    return {"spans": spans, "words": words, "of_words": total,
            "seconds": round(seconds, 2),
            "share": round(words / total, 3) if total else 0.0}


class Clip(object):
    __slots__ = ("name", "slide", "rough", "words", "score", "anchored",
                 "fabricated", "unconfirmed")

    def __init__(self, name, slide, rough):
        self.name, self.slide, self.rough = name, slide, rough
        self.words, self.score = [], 0.0
        # 🔴 A THIRD number, and it answers the question the first two cannot:
        # WHY a track is unreadable. `score` and `anchored` both go DOWN as
        # anchoring thins, but neither says whether the words that missed out
        # were sprinkled through the lecture or sat in one 500-word passage that
        # got no anchor at all. Those need different fixes and looked identical
        # until this was counted.
        self.fabricated = fabricated_time([])
        # 🔴 A SECOND number, because the score and the timings are different
        # questions and this code used to answer both with one figure. `score`
        # is how much of the audio agrees with the transcript AT ALL; `anchored`
        # is how much of it the cues actually rest on.
        self.anchored = 0.0
        # 🟢 The forced-alignment engine's own number, and None under Whisper,
        # which never measures it: which stretches of the transcript the audio
        # does not contain (a slide read silently, a reference list), counted
        # in words and seconds. Those words get NO cue rather than an invented
        # time, so under that engine `fabricated` stays at zero and this is
        # the number that says why a track has a gap.
        self.unconfirmed = None


def lcs_length(a, b):
    """How many of `b` lie on a LONGEST common subsequence with `a`.

    🔴 **WHY THIS EXISTS AND `difflib` DOES NOT ANSWER IT.**
    `SequenceMatcher.get_matching_blocks()` takes the longest matching block and
    recurses either side. That is greedy and **not** a longest common
    subsequence: on `aaaa` against `abaa` it finds 2 where the answer is 3, which
    is the smallest case there is and was found by search rather than invented.

    ⚠️ **The gap is small on short input and grows with length**, measured
    2026-09-04 on one real lecture's own two word streams: 0.020 of heard at 400
    words, 0.099 at 1800, **0.123 at 2400**, while the true figure stayed flat at
    0.58 to 0.62. **That is why the short lectures passed the floor and the long
    ones did not**, and it is a fact about this function rather than about the
    recordings or the transcripts.

    🟢 Two rows, so the memory is O(len(b)) rather than the product. The time is
    the product: about 1.5s on a 30-minute lecture, beside a 6-minute download.
    """
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    for x in a:
        cur = [0] * (len(b) + 1)
        for j, y in enumerate(b):
            if x == y:
                cur[j + 1] = prev[j] + 1
            else:
                left, up = cur[j], prev[j + 1]
                cur[j + 1] = up if up >= left else left
        prev = cur
    return prev[-1]


def align(text, clips):
    """Put real timings on the lecturer's real words, clip by clip.

    ONE `SequenceMatcher` over every clip's rough words against the whole
    transcript, so the ordering is monotone by construction and a bad clip cannot
    drag a cursor past the good ones behind it.
    """
    true_words = words_of(text)
    keys = [w.key for w in true_words]

    rough, owner = [], []
    for c in clips:
        for w in c.rough:
            toks = WORD.findall(plain(w["w"]).lower())
            if toks:
                rough.append(toks[0])
                owner.append((c, w))

    if not rough or not keys:
        return true_words

    sm = difflib.SequenceMatcher(None, keys, rough, autojunk=False)
    hits = {}
    for b in sm.get_matching_blocks():
        for i in range(b.size):
            c, w = owner[b.b + i]
            hits.setdefault(c, []).append((b.a + i, w))

    for c in clips:
        pairs = hits.get(c, [])
        n = sum(1 for o, _ in owner if o is c)
        c.anchored = (len(pairs) / n) if n else 0.0
        # 🔴 THE SCORE AND THE TIMINGS ARE DIFFERENT QUESTIONS. `MATCH_FLOOR`
        # asks "do these words belong to this audio at all", and the greedy
        # blocks under-answer it on a long recording. The TIMINGS keep resting on
        # those same contiguous blocks, because a long run of agreeing words is
        # likelier to be genuine than a stray `the` matched across ten minutes,
        # and a spurious anchor drags real words to the wrong second.
        # 🟢 SO A LECTURE'S CUES ARE BYTE-IDENTICAL EITHER WAY. Only which
        # lectures clear the floor changes.
        # ⚠️ ONE CLIP ONLY, deliberately. A recording is one clip by
        # construction; a package's clips are scored under one monotone matcher
        # so a bad clip cannot drag a cursor past the good ones, and attributing
        # an optimal matching back to each clip needs the whole table. Packages
        # are short, show no deficit, and are left exactly as they were.
        c.score = (lcs_length(keys, rough) / n) if (len(clips) == 1 and n) else c.anchored
        # 🔴 BEFORE the refusal below, deliberately. A clip that falls under the
        # floor is exactly the one somebody will ask about, and computing this
        # after the `continue` would leave every refused clip reporting zero
        # fabricated time -- the flattering answer, on the only clips that matter.
        c.fabricated = fabricated_time(pairs)
        if not pairs or c.score < MATCH_FLOOR:
            c.words = []
            continue
        c.words = _timed(true_words, pairs)
    return true_words


# --------------------------------------------------------------------------
# The forced-alignment engine
# --------------------------------------------------------------------------

# Which engine times the words. `auto` is the forced aligner wherever an
# interpreter for it exists (`align_ctc.find_python`) and Whisper otherwise, so
# a machine without the venv still builds captions the way it always did.
ENGINE_CTC = "ctc"
ENGINE_WHISPER = "whisper"
ENGINE_AUTO = "auto"

# 🔴 THE SIDECAR (`captions.json`) IS WRITTEN BY BOTH ROUTES NOW, AND ITS NAMES
# LIVE HERE because this is the module the other one imports. `video_captions`
# re-exports every one of them, so its callers and tests spell nothing new.
#
# 1.6.0 -> 1.7.0 (2026-09-15): `engine`, `unconfirmed` and `alignment`, for the
# forced aligner (`ENGINE_CTC`). All three are RECOVERABLE on an old card: only
# one engine existed before this, so a card without `engine` was built by
# Whisper and the other two are honestly null for it.
# 1.7.0 -> 1.8.0 (2026-09-18): the CLIP route writes the file too (`source`
# `PACKAGE`), with `clips` (captioned / plays) and `cues_matched_to_transcript`.
# The recording route's shape is unchanged, so every 1.7.0 card is a 1.8.0 card,
# and `clips` is present exactly when `source` is `PACKAGE`.
SCHEMA_VERSION = "1.8.0"
SIDECAR_FILE = "captions.json"
PACKAGE = "package"                  # `source` on the clip route ("video" on the other)

# 🔴 THE TWO ROUTES A LECTURE'S WORDS CAN COME FROM, named once so the sidecar,
# the reader and the tests all spell them the same way. `TRANSCRIPT` is the
# lecturer's own text with timings matched onto it; `HEARD` is the machine's own
# words, which arrive already timed. ⚠️ The clip route has no heard fallback at
# all: a clip that cannot be matched gets no file, never machine words.
TRANSCRIPT = "transcript"
HEARD = "heard"

# What `timings_from.tool` says for each engine. `lesson_packs.sidecar_facts`
# recognises an engine by these words when a card predates the `engine` field.
TOOL_CTC = "torchaudio forced_align"
TOOL_WHISPER = "faster-whisper"
ENGINES = (ENGINE_AUTO, ENGINE_CTC, ENGINE_WHISPER)

# 🔴 The silence that ends a passage, for `cues(split_gap=...)`. Under the forced
# aligner every word's time is measured, so a gap between two consecutive
# transcript words is a real pause and not an interpolation artefact; four
# seconds is longer than a lecturer breathes and shorter than a slide change.
RUN_SPLIT_GAP = 4.0

# The stretch of confirmed words too short to stand on its own inside an
# unconfirmed region: a single `the` the aligner happens to find in a paragraph
# that was never read would otherwise show as a one-word cue in a blank.
ISLAND_WORDS = 3


def engine_for(engine, python_bin=None):
    """`auto` resolved to the engine this machine can actually run."""
    if engine not in ENGINES:
        raise ValueError("engine must be one of %s, not %r" % (", ".join(ENGINES), engine))
    if engine != ENGINE_AUTO:
        return engine
    return ENGINE_CTC if (python_bin or align_ctc.find_python()) else ENGINE_WHISPER


def blank_islands(unconfirmed, indices, min_words=ISLAND_WORDS):
    """The unconfirmed set widened over confirmed islands shorter than `min_words`.

    `indices` are the clip's transcript indices in order. An island is a run of
    confirmed words with an unconfirmed word (or the clip's edge is NOT an edge:
    only a region) on both sides."""
    out = set(unconfirmed)
    run, prev_blank = [], False
    for i in indices:
        if i in out:
            if run and prev_blank and len(run) < min_words:
                out.update(run)
            run, prev_blank = [], True
        else:
            run.append(i)
    return out


def time_by_ctc(text, clips, wavs, python_bin=None, log=None, emissions_dir=None,
                aligner=None):
    """Put MEASURED timings on the lecturer's real words: the forced-alignment
    counterpart of `align`, and the same contract on the way out.

    `clips` and `wavs` are in play order, one 16 kHz mono wav per clip. The
    whole lecture is aligned as ONE timeline (the wavs' emissions are joined end
    to end in the subprocess) and `align_ctc.split_by_clip` hands each word
    back to the clip its start falls in, with times local to that clip.

    **What each clip comes back with:** `words` are the transcript's `Word`s
    placed in that clip, each with a start and an end, except those inside an
    `unconfirmed` region (a stretch of `align_ctc.REGION_WORDS` or more words
    the audio does not contain, or a passage whose `align_ctc.WINDOW_WORDS`
    windows are mostly words the audio contradicts and never anchors, plus any
    island of under `ISLAND_WORDS` confirmed words between two such stretches),
    which keep `start=None` and so get no cue. `score` is the share of the
    clip's words the audio confirms at
    `align_ctc.CONFIRM_FLOOR`, and it is gated by `MATCH_FLOOR` exactly as the
    Whisper score is: a transcript that is not this lecture's confirms almost
    nothing, and the clip gets no words rather than a scatter of coincidences.
    `anchored` is the share of its words that were heard verbatim; `fabricated`
    stays at zero because nothing here is interpolated.

    Returns `(true_words, response)`; the response is what the subprocess
    returned, for the sidecar.
    """
    aligner = aligner or align_ctc.run
    true_words = words_of(text)
    keys = [w.key for w in true_words]
    for w in true_words:
        w.start = w.end = None
    resp = aligner(wavs, keys, python_bin=python_bin, emissions_dir=emissions_dir,
                   log=log)
    timed = resp["words"]
    anchored = set(resp.get("anchor_words", []))
    regions = align_ctc.unconfirmed_regions(timed, anchored)
    unconfirmed = set()
    for a, b in regions:
        unconfirmed.update(range(a, b + 1))
    per_clip = align_ctc.split_by_clip(timed, resp["clip_seconds"])
    for k, c in enumerate(clips):
        mine = [t for t in per_clip if t[4] == k]
        indices = [t[0] for t in mine]
        blank = blank_islands(unconfirmed, indices)
        n = len(mine)
        c.rough = []
        c.words = []
        confirmed = 0
        for i, a, b, score, _ in mine:
            w = true_words[i]
            if i not in blank:
                w.start, w.end = a, b
            if score >= align_ctc.CONFIRM_FLOOR:
                confirmed += 1
            c.words.append(w)
        c.score = (confirmed / float(n)) if n else 0.0
        c.anchored = (sum(1 for i in indices if i in anchored) / float(n)) if n else 0.0
        c.fabricated = fabricated_time([])
        # The denominator travels with the fraction, as `fabricated_time` has
        # it: "none of a lot" and "none of nothing" are different findings.
        by_index = {t[0]: t for t in mine}
        blanked = sum(1 for i in indices if i in blank)
        own, seconds = 0, 0.0
        for a, b in regions:
            inside = [i for i in range(a, b + 1) if i in by_index]
            if inside:
                own += 1
                seconds += max(0.0, by_index[inside[-1]][2] - by_index[inside[0]][1])
        c.unconfirmed = {"regions": own, "words": blanked, "of_words": n,
                         "seconds": round(seconds, 2),
                         "share": round(blanked / float(n), 3) if n else 0.0}
        if not n or c.score < MATCH_FLOOR:
            c.words = []
    return true_words, resp


def by_model_cache(cached):
    """True when a rough-words cache is keyed by MODEL rather than by clip.

    🔴 **TOLD APART BY SHAPE, NEVER BY COMPARING ITS KEYS AGAINST THE MODELS
    THIS RUN WANTS.** The first version of this did compare, and a two-model
    cache read during a single-model run was taken for one model's words: every
    clip then came back *"not transcribed"*, on a real cache, with no error.
    **A cache is a fact about what was transcribed, not about what is wanted.**
    """
    if not cached:
        return False
    for value in cached.values():
        if not isinstance(value, dict) or not value:
            return False
        # 🟢 THIS IS WHAT SEPARATES THE TWO SHAPES. A by-model cache's values are
        # {clip: {duration, words}}; a plain one's are {duration: 3.0, words:
        # [...]}, whose own values are a float and a list rather than dicts.
        # ⚠️ A sweep showed a `"words" in value` guard above this was redundant:
        # removing it changed no answer, because the inner test already rejects
        # every plain cache. Kept as one test rather than two that cannot both
        # be wrong at once.
        if not all(isinstance(w, dict) and "words" in w for w in value.values()):
            return False
    return True


def best_by_clip(text, rough_by_model, order=None):
    """Which model to believe for each clip, chosen by how well it aligns.

    🔴 **THE RULING THIS IMPLEMENTS, 2026-09-09:** *"on any re-run of existing
    captions, keep the better transcription PER CLIP, never replace
    wholesale."* **A bigger model is better on average and NOT better on every
    clip**: in the run that decided this, one clip rose 0.583 to 0.620 and
    crossed `MATCH_FLOOR`, and another fell 0.620 to 0.617 in the same lecture.
    **Replacing wholesale takes both.**

    🟢 **The comparison is between two runs made TODAY**, never against a score
    recorded days ago: a `.vtt` on disk carries no score at all, and a remembered
    one would confound the model with every aligner change since.

    ⚠️ **Each model is scored on its OWN complete clip set**, because `align`
    runs one monotone matcher over every clip at once and a clip's score depends
    on its neighbours. Scoring a half-merged set would measure a lecture that
    never existed. **The chosen words are re-aligned by the caller.**

    Returns `{clip name: model}`, covering every clip any model transcribed.
    """
    order = list(order or sorted(rough_by_model))
    scores = {}
    for model in order:
        rough = rough_by_model.get(model) or {}
        names = [n for n in rough]
        clips = [Clip(n, i, rough[n]["words"]) for i, n in enumerate(names)]
        align(text, clips)
        for c in clips:
            scores.setdefault(c.name, {})[model] = c.score
    chosen = {}
    for name, by_model in scores.items():
        # 🔴 THE TIE GOES TO THE EARLIER MODEL IN `order`, which is the primary,
        # so a re-run cannot swap a clip's model with nothing having changed.
        # 🟢 `min` returns the FIRST minimal item, so iterating `order` IS the
        # tie rule. ⚠️ A sweep proved an explicit index tiebreaker here was dead
        # weight: deleting it changed nothing, which is a mutant that cannot
        # fail for the right reason. The guarantee is `min`'s, and it is named
        # here so the next reader does not re-add the redundant key.
        chosen[name] = min(order, key=lambda m: -by_model.get(m, -1.0))
    return chosen


def merge_rough(rough_by_model, chosen):
    """One rough-word set, taking each clip from the model `chosen` names."""
    out = {}
    for name, model in chosen.items():
        rough = rough_by_model.get(model) or {}
        if name in rough:
            out[name] = rough[name]
    return out


def _timed(true_words, pairs):
    """Every true word in the matched span, each with a start and an end.

    Matched words take the timing of the rough word they matched. **A word Whisper
    missed or misheard is interpolated between its matched neighbours** rather than
    dropped, because dropping it would delete a word the lecturer actually said.
    """
    lo, hi = pairs[0][0], pairs[-1][0]
    span = true_words[lo:hi + 1]
    for w in span:
        w.start = w.end = None
    for idx, rw in pairs:
        w = true_words[idx]
        w.start, w.end = rw["s"], rw["e"]

    known = [i for i, w in enumerate(span) if w.start is not None]
    if not known:
        return []
    for i, w in enumerate(span):
        if w.start is not None:
            continue
        before = [k for k in known if k < i]
        after = [k for k in known if k > i]
        if before and after:
            a, b = span[before[-1]], span[after[0]]
            gap = (b.start - a.end) / (after[0] - before[-1])
            w.start = a.end + gap * (i - before[-1] - 1)
            w.end = w.start + gap
        elif before:
            a = span[before[-1]]
            w.start = w.end = a.end
        else:
            b = span[after[0]]
            w.start = w.end = b.start
    return span


# --------------------------------------------------------------------------
# Cue shaping and WebVTT
# --------------------------------------------------------------------------

class Cue(object):
    """One caption: when it shows, and the lecturer's own sentence.

    🔴 **The text is SLICED from the transcript, never rebuilt from the word
    tokens.** The tokeniser drops every comma and full stop, so a cue joined back
    together from tokens reads as an unpunctuated stream. **Slicing between the
    first and last word's character offsets returns the sentence exactly as
    written**, punctuation, ligatures, capitals and all.
    """

    __slots__ = ("start", "end", "source", "at", "to")

    def __init__(self, start, end, source, at, to):
        self.start, self.end = start, end
        self.source, self.at, self.to = source, at, to

    @property
    def said(self):
        return self.source[self.at:self.to].strip()

    @property
    def lines(self):
        return _wrap(self.said)

    def text(self):
        return "\n".join(self.lines)


def _wrap(said):
    lines, line = [], ""
    for token in said.split(" "):
        nxt = (line + " " + token).strip()
        if line and len(nxt) > CUE_CHARS:
            lines.append(line)
            line = token
        else:
            line = nxt
    if line:
        lines.append(line)
    return lines


def cues(words, source="", chars=CUE_CHARS, lines=CUE_LINES, lo=CUE_MIN, hi=CUE_MAX,
         split_gap=None):
    """Group timed words into cues that can actually be read.

    At most `lines` lines of about `chars` characters, never longer than `hi`
    seconds, and broken at a clause boundary when one is available. ⚠️ **`lo` is a
    floor on DISPLAY time, not a reason to merge**: a short final cue is normal.

    🟢 **`split_gap` is the forced-alignment engine's parameter and the Whisper
    route never passes it**, so that route's tracks are byte-identical to
    before. With it, the words are first cut into `runs` wherever the timeline
    is not one forward line (a word with no time, a step backwards, a silence
    wider than `split_gap` seconds), each run is grouped and shaped on its own,
    and the cues are then put in time order and clamped against each other.
    🔴 **Shaping per run and not across them is load-bearing**: `_merge` widens a
    slice of the transcript between two cues, and two cues that are neighbours
    in TIME but pages apart in the TRANSCRIPT (a lecturer who came back to an
    earlier slide) would merge into a cue holding everything between them.
    """
    if split_gap is None:
        return shape(_group(words, source, chars, lines, lo, hi))
    out = []
    for run in runs(words, split_gap):
        out.extend(shape(_group(run, source, chars, lines, lo, hi)))
    out.sort(key=lambda c: (c.start, c.at))
    return clamp_ends(out)


# 🔴 A STEP BACKWARDS IS A REORDERING, NOT JITTER. Two words placed by one
# alignment window never overlap by more than a frame or two (0.02 s each), and
# a lecturer returning to an earlier slide moves the timeline back by minutes,
# so half a second separates the two cleanly.
BACKWARDS_S = 0.5


def runs(words, split_gap, backwards=BACKWARDS_S):
    """The timed words cut wherever the timeline is not one forward line.

    A run ends at a word with no time (it gets no cue and is not shown at all),
    where the next word starts earlier than the previous one ended by more than
    `backwards`, or where the silence between two words is wider than
    `split_gap`. Within a run the words are in transcript order AND time
    order, which is what lets the cue shaper treat them as one passage.
    """
    out, run = [], []
    for w in words:
        if w.start is None:
            if run:
                out.append(run)
            run = []
            continue
        if run and (w.start < run[-1].end - backwards
                    or w.start - run[-1].end > split_gap):
            out.append(run)
            run = []
        run.append(w)
    if run:
        out.append(run)
    return out


def _group(words, source, chars, lines, lo, hi):
    """The grouping half of `cues`: timed words into unshaped cues."""
    out, group = [], []
    budget = chars * lines
    for w in words:
        if w.start is None:
            continue
        trial = group + [w]
        text_len = sum(len(x.text) + 1 for x in trial) - 1
        too_long = text_len > budget
        too_slow = group and (w.end - group[0].start) > hi
        if group and (too_long or too_slow):
            out.append(_cue(group, lo, source))
            group = [w]
            continue
        group = trial
        if _ends_a_clause(source, w) and text_len > budget * 0.6:
            out.append(_cue(group, lo, source))
            group = []
    if group:
        out.append(_cue(group, lo, source))
    return out


# 🔴 A FLOOR ON WORDS, AND DURATION MUST NOT EXCUSE IT. Three is the smallest
# number that makes "noticing" / "how" impossible as two consecutive screens,
# which is the shape EH reported on 2026-09-07 and the manager reproduced from
# the file on 09-14 (`W5-T2-P2/sound4.vtt`, cues 21 to 24).
#
# ⚠️ THE OBVIOUS FORM OF THIS RULE IS THE WRONG ONE, and the entry said so after
# measuring: "never emit one or two words UNLESS A REAL PAUSE JUSTIFIES IT" reads
# well and excuses every case. **All 34 starved cues in the library run 4.0 to
# 14.6 seconds**, the worst holding the single word `found.` for 14.6. Length
# here is not a pause; it is the aligner parking a word between two distant
# anchors. So there is no duration escape hatch, deliberately.
CUE_MIN_WORDS = 3

# 🔴🔴 A CEILING ON THE MERGE, AND IT IS THE RENDERED LINE COUNT RATHER THAN A
# CHARACTER PROXY. **A merge may never make a cue wrap onto more lines than its
# parts already did.**
#
# ⚠️ **BOTH OF THIS RULE'S OBVIOUS FORMS ARE WRONG, and each was tried.** With no
# ceiling at all, merging took the corpus's longest cue from 93 characters to 110
# and put 34 over the line `test_captions_render` measured in a browser - trading
# one legibility defect for another, in EH's reader, at the `panelWidth: 379` he
# actually uses. With a flat 93-character ceiling, `cues()` produced a
# three-line cue from a two-line one and `test_captions.
# test_no_cue_is_more_than_two_lines` caught it, correctly: **`CUE_LINES` is the
# design's rule and a character count is only ever a proxy for it.**
#
# 🟢 **NOT A FLAT `<= CUE_LINES`, EITHER, and this is the part measured rather
# than assumed: 1,307 of the corpus's 7,373 cues ALREADY render on three lines.**
# A flat rule would refuse almost every repair on the tracks that need it most.
# **What a merge owes is not to make the wrapping worse than it found it.**
CUE_MAX_LINES = CUE_LINES

# 🔴 AND THE SECOND HALF OF THE CEILING, because the line rule alone is not
# enough and the corpus proved it. `_line_count` wraps at `CUE_CHARS`, which is a
# PROXY for what the pane does at the reader's own width: a cue already wrapping
# to three lines here can take a neighbour and stay at three, and reach 105
# characters doing it. **`test_captions_render` asserts 93 on the corpus**, from
# a browser measurement in a real lesson: 0 cues need three lines at 400px of
# pane or wider, and EH's `panelWidth` is 379.
#
# ⚠️ **Two bounds from two different measurements, and a merge must clear both.**
# 🔴 **93 IS A TRIGGER AND NOT A LAW, and the test carrying it says so: the answer
# is always a browser, never arithmetic on this constant.** It is used here as
# the ceiling precisely so that nothing about the browser's situation changes.
CUE_MAX_CHARS = 93


def _line_count(text):
    """How many lines this text renders to, through the module's own wrapper.

    🟢 `_wrap` rather than arithmetic on `CUE_CHARS`: short tokens give more
    break opportunities, so length does not predict wrapping. That is the same
    finding `test_captions_render` records from a browser - a 93-character cue
    fits two lines from 357px while an 89-character one needs 376px."""
    return len(_wrap(text)) if text else 0


def merge_runs(counts, min_words=CUE_MIN_WORDS, texts=None,
               max_lines=CUE_MAX_LINES, max_chars=CUE_MAX_CHARS):
    """Which neighbouring cues to join so none is left under the word floor.

    Takes word counts and returns index groups, so the RULE is written once and
    each caller applies it to its own representation: `cues()` merges `Cue`s
    sliced out of a transcript, and `repair_vtt` merges cues parsed back off
    disk. Two implementations of one rule is how two numbers about the same file
    come to disagree.

    🟢 **FORWARD, as the entry specifies**, because a starved cue and the rushed
    cue after it are usually two halves of one defect: the aligner parks a word
    in a long gap and then has no time left for the phrase. Joining them gives
    the phrase the gap's seconds back.
    ⚠️ **The LAST cue is the one exception and merges backward**, because there is
    nothing in front of it. A track ending on a one-word cue is the case that
    would otherwise sit under the floor for ever.
    """
    texts = list(texts or [""] * len(counts))

    def fits(group):
        """Whether joining these cues leaves the wrapping no worse than it was.

        Both bounds, because neither holds alone: the line count is the design's
        rule and the character count is what a browser was actually measured
        against."""
        joined = " ".join(texts[i] for i in group).strip()
        if len(joined) > max_chars:
            return False
        allowed = max([max_lines] + [_line_count(texts[i]) for i in group])
        return _line_count(joined) <= allowed

    out, pending, held = [], [], 0
    for i, n in enumerate(counts):
        # 🔴 THE CEILING WINS OVER THE FLOOR, deliberately. A cue that cannot be
        # merged without wrapping onto another line is left short and REPORTED,
        # rather than quietly becoming a different defect.
        if pending and not fits(pending + [i]):
            out.append(pending)
            pending, held = [], 0
        pending.append(i)
        held += n
        if held >= min_words:
            out.append(pending)
            pending, held = [], 0
    if pending:
        if out and fits(out[-1] + pending):
            out[-1].extend(pending)
        else:
            out.append(pending)
    # 🟢 BACKWARD, ONLY WHERE THE CEILING BLOCKED THE FORWARD MERGE. Forward is
    # the rule because a starved cue and the rushed cue after it are usually two
    # halves of one defect; but a group the ceiling stranded is better joined to
    # the cue BEFORE it than left holding two words for eight seconds.
    # ⚠️ Found by a property test rather than by design: `every starved cue left
    # is one the ceiling blocked` failed on `video.vtt` cue 2, `part 3.`, whose
    # next cue is 86 characters and whose previous is 53. Forward could not take
    # it and backward can.
    fixed = []
    for group in out:
        if (fixed and sum(counts[i] for i in group) < min_words
                and fits(fixed[-1] + group)):
            fixed[-1].extend(group)
        else:
            fixed.append(group)
    return fixed


def _merge(a, b):
    """Two cues as one, keeping the transcript's own punctuation where it can.

    🟢 Two shapes arrive here. Cues built in this module all slice ONE transcript
    string, so the merge is a wider slice of it and the commas and full stops
    between the two come with it. Cues parsed back off a `.vtt` each carry their
    own text, and there the join is the only thing available."""
    if a.source is b.source and a.to <= b.at:
        return Cue(a.start, b.end, a.source, a.at, b.to)
    joined = (a.said + " " + b.said).strip()
    return Cue(a.start, b.end, joined, 0, len(joined))


def clamp_ends(cue_list):
    """No cue may still be showing when the next one opens.

    🔴 **THE ONLY ONE OF THE THREE LEVERS WHOSE CORRECTNESS IS NOT A MATTER OF
    TASTE.** A starved cue and a rushed cue are judgements about reading comfort;
    two cues live at once is a malformed file. Measured across the 451 live
    tracks: 7 cues in 5 files.

    ⚠️ **`_cue` is one of the ways they arise and it is not a bug there**: a cue
    holding a word timed at a tenth of a second is stretched to `CUE_MIN` so it
    can be read, and the stretch can reach past its neighbour. The stretch is
    right; outliving the neighbour is not. **A clamp never moves a START**, so no
    cue is ever shown later than the words it captions."""
    out = list(cue_list)
    for i in range(len(out) - 1):
        nxt = out[i + 1].start
        if out[i].end > nxt:
            out[i] = Cue(out[i].start, max(out[i].start, nxt),
                         out[i].source, out[i].at, out[i].to)
    return out


def _floor_seconds(words):
    """The least time a cue of this many words may be given.

    Both halves matter. `CUE_MIN` is the display floor that already exists here;
    `FAST_WORDS_PER_SECOND` is the rate at which QA measured a reader stops
    keeping up. A cue must clear both."""
    return max(CUE_MIN, words / FAST_WORDS_PER_SECOND)


def rebalance(cue_list):
    """Move a shared boundary when one side is rushing and the other is parked.

    🔴 **THE ENTRY'S SECOND LEVER, kept narrow.** Between two anchors the aligner
    has to spread words across a gap, and where anchors are sparse it parks a
    word for seconds and then has nothing left for the phrase after it. **That is
    one defect at one boundary**, and the boundary is the thing to move.

    ⚠️ **WHY THE WORD FLOOR ALONE WAS NOT ENOUGH, measured on EH's own passage.**
    Merging turned "so noticing" / "how" into one cue, which is what he saw - and
    left `so noticing how` sitting for 8.02 seconds with 11 words crammed into
    the 0.58 after it. **The two screens became one screen and the complaint
    survived.** Only moving the boundary answers it: 1.93s and 7.09s, both at
    1.55 words a second.

    **THREE BOUNDS, and each one is there to stop this doing harm:**

    1. **Only where a side is above `FAST_WORDS_PER_SECOND`.** A pair that both
       read comfortably is left alone whatever its arithmetic looks like.
    2. **Never past the point where the two rates are equal**, so the giving side
       cannot be pushed below the taking side.
    3. **Never below either side's own `_floor_seconds`.** A cue that gives time
       away must still be readable itself, which is what stops a fast cue
       stealing its neighbour into a flash.

    🟢 **THE PAIR'S TOTAL SPAN NEVER CHANGES**, only where the cut falls inside
    it. Nothing is shown before its first word was spoken or after its last, and
    no word moves between cues. 🔴 **CONTIGUOUS BOUNDARIES ONLY**: where there is
    a real gap between two cues there is silence, and moving a boundary into
    silence captions something nobody said.
    """
    out = list(cue_list)
    for i in range(len(out) - 1):
        a, b = out[i], out[i + 1]
        if abs(b.start - a.end) > 0.001:        # a real gap: leave it alone
            continue
        wa = len(WORD.findall(a.said))
        wb = len(WORD.findall(b.said))
        span = b.end - a.start
        if not (wa and wb) or span <= 0:
            continue
        da, db = a.end - a.start, b.end - b.start
        fast = (da > 0 and wa / da > FAST_WORDS_PER_SECOND) or \
               (db > 0 and wb / db > FAST_WORDS_PER_SECOND)
        if not fast:
            continue
        even = a.start + span * (wa / float(wa + wb))
        lo = a.start + _floor_seconds(wa)
        hi = b.end - _floor_seconds(wb)
        if lo > hi:                             # the pair cannot satisfy both
            continue
        # Toward the even split from wherever the boundary is now, never past it.
        cut = min(max(even, lo), hi)
        cut = min(max(cut, a.start), b.end)
        out[i] = Cue(a.start, cut, a.source, a.at, a.to)
        out[i + 1] = Cue(cut, b.end, b.source, b.at, b.to)
    return out


def shape(cue_list, min_words=CUE_MIN_WORDS):
    """The word floor, the clamp and the rebalance, in that order.

    🔴 **THE ORDER IS LOAD-BEARING, all three times.**

    **Merge first**, because merging changes which cue is next, so a clamp
    applied before it would clamp against boundaries that are about to move. It
    can only ever remove an overlap, never create one: a merged cue ends where
    its last member ended.

    **Clamp second**, so every boundary the rebalance then looks at is a real
    boundary rather than two cues claiming the same instant.

    **Rebalance last, and it is what repairs the clamp's own cost.** Clamping an
    overlap shortens the earlier cue, and on EH's passage that earlier cue was
    the rushed one: 11 words went from 1.00s to 0.58s. The clamp is still right -
    two cues live at once is malformed - but it is only safe with the rebalance
    behind it."""
    if not cue_list:
        return list(cue_list)
    counts = [len(WORD.findall(c.said)) for c in cue_list]
    texts = [c.said for c in cue_list]
    merged = []
    for group in merge_runs(counts, min_words, texts):
        cue = cue_list[group[0]]
        for i in group[1:]:
            cue = _merge(cue, cue_list[i])
        merged.append(cue)
    return rebalance(clamp_ends(merged))


def repair_vtt(text, min_words=CUE_MIN_WORDS):
    """A track already on disk, reshaped by the same rule new tracks are built to.

    🔴 **THIS EXISTS BECAUSE CAPTIONS SHIP AS DATA.** Changing `cues()` changes
    what the next build produces and nothing at all about the 451 tracks a reader
    can open today, which is where every measured defect actually is.

    🟢 Round trip both ways: `cues_from_vtt` and `to_vtt` are already a documented
    pair with a test that says so, so nothing here re-parses or re-renders."""
    return to_vtt(shape(cues_from_vtt(text), min_words))


def heard(rough):
    """The MACHINE's own words as a timed transcript, ready for `cues()`.

    Returns `(source, words)`: the text exactly as the model emitted it, and
    `Word`s over it carrying the model's own start and end for each.

    🔴 **THIS IS THE PATH WITH NO MATCHING STEP, and that is the whole reason it
    exists.** Timing the LECTURER's words needs the model's words matched against
    the transcript, and on a long recording that match goes sparse; sparse
    anchors are what squeeze seventeen words into one second. **The model's own
    words already have their timings**, so there is nothing to match, nothing to
    interpolate, and the failure mode cannot occur. What it costs instead is
    transcription error: this model heard *"maximums"* for *mechanisms*.

    ⚠️ **EH priced that himself before asking for it**: KCL's own transcript
    renders the same word *"magnets"*. **Both sources are wrong sometimes and
    only one of them can always be timed.**

    🟢 **THE TEXT IS JOINED WITH NOTHING**, because a rough word is
    `{"w": " word", ...}` with its leading space already in `w`. Joining on a
    space would double every gap and, worse, shift every character offset away
    from the timings they index.

    🔴 **The tokens come from `words_of` over that joined text, NOT from the
    rough list**, so punctuation stays in the source where `_ends_a_clause` can
    see it and `Cue.said` can slice it. A cue rebuilt from tokens reads as an
    unpunctuated stream, which is the rule `Cue` already carries.
    """
    parts, spans, at = [], [], 0
    for w in rough:
        t = (w or {}).get("w") or ""
        if not t:
            continue
        parts.append(t)
        spans.append((at, at + len(t), w))
        at += len(t)
    source = "".join(parts)
    if not source:
        return "", []

    # char index -> which rough word owns it. A whole lecture is tens of KB, so
    # the flat table is cheaper to reason about than a bisect and cannot get the
    # boundary wrong.
    owner = [None] * len(source)
    for k, (a, b, _) in enumerate(spans):
        for c in range(a, b):
            owner[c] = k

    out = []
    for word in words_of(source):
        first = owner[word.at]
        last = owner[word.to - 1] if word.to > word.at else first
        if first is None or last is None:
            continue
        # ⚠️ A token can straddle two rough words when the model emits a piece
        # with no leading space, so this takes the FIRST one's start and the
        # LAST one's end rather than assuming they are the same word. Splitting
        # the difference would be inventing a timing the model never gave.
        word.start = spans[first][2].get("s")
        word.end = spans[last][2].get("e")
        if word.start is None or word.end is None:
            continue
        out.append(word)
    return source, out


def _ends_a_clause(source, word):
    """Whether the transcript puts a clause break right after this word.

    ⚠️ The word token itself never carries punctuation, so this looks at the
    character AFTER it in the source rather than at the token."""
    tail = source[word.to:word.to + 1]
    return bool(tail and CLAUSE_END.search(tail))


def _cue(group, lo, source):
    start = group[0].start
    end = max(group[-1].end, start + lo)
    at, to = group[0].at, group[-1].to
    # take the punctuation that closes the last word with it
    while to < len(source) and CLAUSE_END.search(source[to:to + 1]):
        to += 1
    return Cue(start, end, source, at, to)


def stamp(seconds):
    if seconds < 0:
        seconds = 0.0
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return "%02d:%02d:%02d.%03d" % (h, m, s, ms)


# 🔴 SIX WORDS A SECOND is where a caption stops being readable. It is not a
# threshold this project has to defend: it is QA's measure, and what makes it
# usable is that it needs no calibration curve. A lecture's own unreadable
# fraction is a fact about THAT lecture.
FAST_WORDS_PER_SECOND = 6.0

# 🔴 THE SECOND GATE, ruled by the manager 2026-09-04 at `be6a94a` on 38 lectures
# across both courses: **refuse a lecture more than 5% of whose cues are
# unreadable.** It is on the OUTPUT, and it is a gate on a DIFFERENT quantity from
# `MATCH_FLOOR`, which is untouched and stays untouched.
#
# 🟢 WHY 5%: 35 of 38 lectures sit under 3%, the worst CLEAN lecture is 2.8% and
# the best BAD one is 10.4%. 5% is in that gap.
# ⚠️ **AND WHY THE LOW END OF THE GAP RATHER THAN THE MIDDLE**, which is the part
# worth keeping: the harms are not symmetric. A false refuse costs captions on a
# fine lecture, which is visible, reversible and annoying. A false admit puts
# wrong captions in front of a reader who trusts them, **which is the harm this
# whole system exists to prevent.** A threshold between two unequal errors does
# not belong at the midpoint.
# ⚠️ **The 6 words/second cutoff is the manager's and has never been checked
# against a human reader.** It was measured to be conservative rather than
# validated: at 3 w/s even good lectures show 11 to 20%, at 6 w/s clean lectures
# show 0 to 1% and bad ones 22.6 to 43.4%. **So 43.4% is a FLOOR on how bad that
# lecture is, not a ceiling.** Not urgent, because the separation is not close.
LEGIBILITY_CEILING = 0.05


def too_fast_to_read(cue_list):
    """Whether a finished track fails the readability gate, and by how much.

    🔴 **ONE LECTURE, NOT ONE CLIP.** A package's clips are separate files and the
    reader meets one lecture, so a clip that is fine cannot rescue a lecture that
    is not, and a single bad clip cannot condemn ten good ones on its own. The
    caller folds every clip in before asking.
    """
    got = legibility(cue_list)
    return got["unreadable_share"] > LEGIBILITY_CEILING, got


VTT_TIME = re.compile(
    r"(\d\d):(\d\d):(\d\d)\.(\d\d\d) --> (\d\d):(\d\d):(\d\d)\.(\d\d\d)")


def cues_from_vtt(text):
    """A `.vtt` back into `Cue`s, so a track already on disk can be measured.

    🔴 **The point is that the SHIPPED counter runs on it.** Measuring finished
    tracks with a second, parallel implementation of "how fast is this cue" is
    how two numbers about the same file come to disagree. `to_vtt` and this are
    a round trip, and there is a test that says so.

    🔴🔴 **THE CUE IDENTIFIER IS STRIPPED BY POSITION, NEVER BY SPELLING, and
    that is a fix rather than a preference.** This read
    `not line.strip().isdigit()` until 2026-09-15, which deleted **any**
    digits-only line in the block. The identifier really is usually digits, but
    so is a line of CONTENT that happens to hold nothing but a number, and the
    filter could not tell them apart.

    ⚠️ **IT REACHED EH'S OWN FILES AND STAYED THERE ELEVEN DAYS.**
    `W4-T1-P2/video.vtt` lost a citation YEAR (`2019`, sitting alone after
    *"RCT from ... and colleagues in"*) and `W4-T1-P3/video.vtt` lost a PATIENT
    COUNT (`26`). **A sentence that ends in a bare number simply stopped, and
    nothing anywhere said so.**

    🟢 **WebVTT defines the identifier by WHERE IT IS: the optional line before
    the timing line.** So everything before the timing line is discarded and
    everything after it is the cue's text, which is what the format says and
    what a spelling test can never approximate.

    ⚠️ **Text runs to the NEXT timing line, not blindly to the end of the
    block.** Blocks come from splitting on blank lines, so a file written
    without them merges cues; stopping at the next timing line keeps that case
    from swallowing a timing line into somebody's sentence, and it is still a
    positional rule rather than a content one.
    """
    out = []
    for block in text.split("\n\n"):
        lines = block.splitlines()
        at_time = next((i for i, l in enumerate(lines) if VTT_TIME.search(l)), None)
        if at_time is None:
            continue
        m = VTT_TIME.search(lines[at_time])
        def at(off):
            h, mi, sec, ms = (int(m.group(off + i)) for i in range(4))
            return h * 3600 + mi * 60 + sec + ms / 1000.0
        body = lines[at_time + 1:]
        stop = next((i for i, l in enumerate(body) if VTT_TIME.search(l)), len(body))
        said = " ".join(body[:stop]).strip()
        if said:
            out.append(Cue(at(1), at(5), said, 0, len(said)))
    return out


def cue_spans_in(text, vtt):
    """Where each cue of a written `.vtt` sits in the transcript, exactly.

    🟢 **This is possible at all because a cue's text is a verbatim SLICE of the
    transcript** (`Cue.said` is `source[at:to].strip()`), so the spans can be
    recovered from the file rather than kept in a second place that could drift.
    **A sidecar that can be recomputed from what is on disk is worth more than one
    that has to be believed.**

    🔴 **Matched on whitespace-NORMALISED text with an index map back to the
    original.** The `.vtt` re-wraps a cue into lines of about 42 characters and the
    transcript has its own newlines, so the two disagree about whitespace and
    about nothing else. Comparing the collapsed forms and mapping the answer back
    gives the true character span.

    ⚠️ **Forward-only, within ONE file.** Cues are monotone by construction, so a
    cue is searched for from the end of the previous one; that also stops a
    repeated sentence later in the lecture from claiming an earlier cue's span.
    🔴 **So on the clip route this is called once PER CLIP, with a fresh cursor
    each time, and the spans are unioned** (`clip_sidecar`): a package's clips
    do not play in transcript order, and one cursor run over the clips joined
    end to end reports a perfect lecture as 0.579 covered (measured 2026-09-18
    on the lecture whose sixth file opens its slide).

    🔴 **Parsed by `cues_from_vtt`, the same parser the builder round-trips
    through**, rather than by a second reading of the block. Until 2026-09-18
    this dropped every digits-only line as an identifier, the defect
    `cues_from_vtt` was corrected for three days earlier: a content line that
    holds nothing but a number (a citation year, a patient count) vanished from
    the cue, so the cue's text no longer matched the transcript and its whole
    span went uncounted.
    """
    keep = [(i, ch) for i, ch in enumerate(text) if not ch.isspace()]
    flat = "".join(ch for _i, ch in keep)
    at_index = [i for i, _ch in keep]
    spans = []
    cursor = 0
    for cue in cues_from_vtt(vtt):
        said = "".join(cue.said.split())
        if not said:
            continue
        found = flat.find(said, cursor)
        if found < 0:                     # a cue that is not a slice: skip it
            continue
        spans.append((at_index[found], at_index[found + len(said) - 1] + 1))
        cursor = found + len(said)
    return spans


def covered_from_spans(text, spans):
    """The fraction of the transcript's words that fall inside some span.

    🔴 **THE ONE RULE FOR `transcript_covered`, on both routes.** The recording
    route hands it the live cues' own spans (`video_captions.transcript_covered`),
    the clip route and `residecar` hand it spans recovered from the files, and
    the number is the same function either way, so the two cannot drift.

    🟢 **Overlapping spans are UNIONED first.** Spans from one file never
    overlap; spans unioned across a package's clips can, wherever a cue's text
    occurs twice in the transcript and the search picked the other occurrence.
    A word counts when it lies inside the union, so a misplaced span still
    covers the same words and never hides a neighbour.
    """
    words = words_of(text)
    if not words:
        return None
    merged = []
    for at, to in sorted(spans):
        if merged and at <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], to)
        else:
            merged.append([at, to])
    hit, i = 0, 0
    for w in words:
        while i < len(merged) and merged[i][1] <= w.at:
            i += 1
        if i < len(merged) and merged[i][0] <= w.at and w.to <= merged[i][1]:
            hit += 1
    return round(hit / float(len(words)), 3)


def voiced_seconds(cue_list):
    """How many seconds of a track actually carry words, overlaps counted ONCE.

    🔴 **Summing `end - start` double-counts**, and anchored cues do overlap: the
    W4-T2-P1 track has neighbouring cues sharing a boundary second. A metric that
    can be inflated by splitting one cue in two is not a recall metric.
    """
    spans = sorted((c.start, c.end) for c in cue_list if c.end > c.start)
    total, open_at, close_at = 0.0, None, None
    for a, b in spans:
        if open_at is None:
            open_at, close_at = a, b
        elif a <= close_at:
            close_at = max(close_at, b)
        else:
            total += close_at - open_at
            open_at, close_at = a, b
    if open_at is not None:
        total += close_at - open_at
    return round(total, 3)


#: How much of a track's voiced time a rebuild may lose before it is worth a
#: human looking. 🔴 NOT A REFUSAL AND NOT TUNED TO A CORPUS: removing a page
#: footer legitimately loses a little, and losing a tenth of a lecture is not
#: "a little" under any reading.
RECALL_FLOOR = 0.90


def recall_against(was, now):
    """What a rebuild REMOVED from the track it replaces. Counts; never refuses.

    🔴🔴 **THE HOLE THIS EXISTS TO FILL, and it is measured rather than feared.**
    The match score is `lcs_length(keys, rough) / len(rough)`: **precision over
    the words the model produced, with NO RECALL TERM ANYWHERE.** So a rebuild
    that DELETES a hard passage and gets the remainder right does not merely
    score better than one that keeps and mangles it -- **it scores 1.000,
    identical to a perfect run.** Verified against the shipped `lcs_length`:

        keeps the hard stretch and mishears it   heard 14, lcs 11 -> 0.786
        DELETES the hard stretch, rest correct   heard  9, lcs  9 -> 1.000
        perfect                                  heard 14, lcs 14 -> 1.000

    ⚠️ **Nothing else in this pipeline can see that.** It cost 819 seconds of
    voiced lecture, 13.6 minutes across six recordings, before a word count
    caught it by accident. **This is the number that would have caught it on the
    first file.**

    🟢 **IT COUNTS AND DOES NOT REFUSE**, exactly as `legibility` does, and for
    the same reason: where the line goes is a decision, and a build that stops
    on a threshold nobody has ruled on is a build that gets its guard removed.
    **`kept` is the fraction of voiced time surviving; `under_floor` says only
    that a human should look.**

    🔴🔴 **TWO RATIOS, AND VOICED SECONDS ALONE WOULD HAVE MISSED FOUR OF THE SIX.**
    Run against the preserved primed tracks, which is the failure this was built
    for rather than a fixture:

        part       voiced kept   words kept   caught by voiced?
        W4-T1-P1      0.921        0.866            no
        W4-T1-P2      0.965        0.873            no
        W4-T1-P3      0.856        0.785           yes
        W4-T2-P1      0.878        0.706           yes
        W4-T2-P2   🔴 1.000        0.791            no
        W4-T2-P3      0.936        0.905            no

    🔴 **`W4-T2-P2` LOST A FIFTH OF ITS WORDS WITH ITS VOICED TIME UNCHANGED**:
    the same spans, carrying less speech. **A duration metric cannot see that at
    all.** ⚠️ **So an earlier draft of this guard, checking voiced seconds only,
    flagged 2 of 6 and would have waved the rebuild through.**

    ⚠️ **AND THE TIDY STORY I FIRST WROTE HERE IS FALSE, so it is corrected
    rather than deleted**: I claimed footer-stripping drops words while leaving
    voiced time whole, and that dropped speech takes both. **`W4-T2-P2` is
    dropped speech that took only words.** The two ratios do not classify the
    cause; they only ensure something is visible. **Read both and look.**
    """
    was, now = list(was), list(now)
    before, after = voiced_seconds(was), voiced_seconds(now)
    words_was = sum(len(c.said.split()) for c in was)
    words_now = sum(len(c.said.split()) for c in now)
    kept = 1.0 if before <= 0 else round(after / before, 4)
    kept_words = 1.0 if words_was <= 0 else round(words_now / words_was, 4)
    return {
        "voiced_was": before, "voiced_now": after,
        "words_was": words_was, "words_now": words_now,
        "cues_was": len(was), "cues_now": len(now),
        "kept": kept, "kept_words": kept_words,
        # 🔴 EITHER ratio, never both: they catch different failures and the
        # primed run produced one of each.
        "under_floor": (before > 0 and kept < RECALL_FLOOR) or
                       (words_was > 0 and kept_words < RECALL_FLOOR),
    }


def recall_total(rows):
    """One verdict for the LECTURE, from the per-clip figures. `None` if new.

    🔴🔴 **PER CLIP IS THE WRONG UNIT AND REAL DATA SHOWED IT.** Rebuilding
    the affective disorders course's `W1-T2-P2` through the fixed stripper: seven of eight clips came
    back byte-identical, and the eighth -- **the only one that carried a page
    footer** -- dropped to 0.880 on words and tripped the floor. **The part as a
    whole lost 10 words of 1,110, which is 0.991 and is the footer itself.**

    ⚠️ **A guard that fires on the exact operation it was built to permit is a
    guard somebody switches off**, which is the same argument `legibility` makes
    for counting rather than refusing. **The clip figures are kept because they
    say WHERE; this says WHETHER.**
    """
    got = [r["recall"] for r in rows if r.get("recall")]
    if not got:
        return None
    voiced_was = sum(r["voiced_was"] for r in got)
    voiced_now = sum(r["voiced_now"] for r in got)
    words_was = sum(r["words_was"] for r in got)
    words_now = sum(r["words_now"] for r in got)
    kept = 1.0 if voiced_was <= 0 else round(voiced_now / voiced_was, 4)
    kept_words = 1.0 if words_was <= 0 else round(words_now / words_was, 4)
    return {
        "clips": len(got),
        "voiced_was": round(voiced_was, 3), "voiced_now": round(voiced_now, 3),
        "words_was": words_was, "words_now": words_now,
        "kept": kept, "kept_words": kept_words,
        "under_floor": (voiced_was > 0 and kept < RECALL_FLOOR) or
                       (words_was > 0 and kept_words < RECALL_FLOOR),
    }


def legibility(cue_list):
    """How much of a caption track a reader cannot keep up with.

    🔴 **THIS COUNTS. IT DOES NOT REFUSE.** Where the line goes is EH's, with the
    manager ruling, and neither of them has fixed a number yet. Counting from
    every build is what turns the next lecture into a data point instead of
    leaving the question resting on five, which was QA's proposal and is the
    whole reason this returns figures rather than a verdict.

    🔴 **TWO SIGNATURES, and the second one explains the first.**
    `unreadable` is cues running over six words a second. `at_floor` is cues
    pinned to exactly `CUE_MIN`, which is what happens when sparse anchoring
    compresses a span until the clamp catches it. **Measured 2026-09-04 across
    every caption file on disk: 34 package lectures average 2.15% unreadable, and
    the two outliers carry 20% and 44% of their cues at the floor.** The clamp is
    the mechanism; the reading speed is the harm.

    ⚠️ **A zero-length or single-word cue cannot be "too fast"**, so a track of
    them scores a clean 0%. That is why `cues` is returned beside the counts: a
    fraction with no denominator beside it is the shape that hides an empty run.
    """
    fast = floor = 0
    for cue in cue_list:
        span = cue.end - cue.start
        if span <= 0:
            continue
        if len(cue.said.split()) / span > FAST_WORDS_PER_SECOND:
            fast += 1
        if abs(span - CUE_MIN) < 1e-6:
            floor += 1
    n = len(cue_list)
    return {"cues": n, "unreadable": fast, "at_floor": floor,
            "unreadable_share": round(fast / n, 3) if n else 0.0}


def to_vtt(cue_list):
    """WebVTT, always.

    🔴 The manager's condition on the display probe: this is a `<track>` source if
    a track can load inside the package's sandbox, and a parseable interchange for
    the reader to render if it cannot. **Neither path gets a private cue format.**
    """
    parts = ["WEBVTT", ""]
    for i, c in enumerate(cue_list, 1):
        parts.append(str(i))
        parts.append("%s --> %s" % (stamp(c.start), stamp(c.end)))
        parts.append(c.text())
        parts.append("")
    return "\n".join(parts)


# --------------------------------------------------------------------------
# Priming: telling the recogniser which words to expect
# --------------------------------------------------------------------------

# 🔴🔴 THE CAP IS 223 TOKENS AND IT IS NOT NEGOTIABLE, and this number is why
# priming with the transcript itself does almost nothing. `faster_whisper`'s
# `WhisperModel.max_length` is 448 and the prompt is trimmed to
# `max_length // 2 - 1`, **keeping the LAST tokens** (`transcribe.py`, verified in
# 1.2.1 against the installed package rather than the docs).
#
# 🔴 MEASURED on the mindfulness course's `W4-T1-P1`: the transcript is **4,657 tokens**, so
# `initial_prompt=transcript` would prime with **4.8% of it, the closing
# paragraph** -- and the errors this exists to fix ("Wake 4" for *Week 4*,
# "magnum of change" for *mechanisms of change*) are in the FIRST MINUTE.
# ⚠️ **It would look implemented, cost a full re-transcription, and change nothing
# at the start of every lecture.**
#
# 🟢 SO PRIME WITH VOCABULARY, NOT PROSE. The words a recogniser gets wrong are
# the ones it rarely heard in training: proper names and technical terms. A list
# of those fits far more of them into 223 tokens than a paragraph of ordinary
# English does, and it applies to the WHOLE lecture rather than its ending.
# 🔴 70, AND THE NUMBER IS MEASURED RATHER THAN CHOSEN. Tokenised with the real
# `base.en` tokenizer across all nine transcripts of the mindfulness course, worst case:
#     60 terms -> 173 tokens    70 -> 196    75 -> 210    80 -> 220    90 -> 241
# The cap is 223, so 90 OVERFLOWS and 80 leaves three tokens of margin, which is
# not margin at all for a course whose names are longer. **70 leaves 27.**
#
# 🔴🔴 PRIMING IS OFF, AND 0 IS THE MEASURED SETTING, NOT A DISABLED FEATURE.
# The token arithmetic above is still correct and is kept for whoever re-enables
# this. What killed it is a A/B rebuild of six mindfulness lectures, primed against
# the SAME audio unprimed, which is the comparison this file's own docstring
# demands: **`hotwords` DROPPED 819 seconds of the lecturer's speech, 13.6
# minutes across the six**, in long silent stretches, one of 33s in `W4-T2-P1`
# where the unprimed track carries six cues of ordinary teaching.
#
# 🔴 AND THE GATE MOVED THE WRONG WAY, which is why nobody would have caught it.
# The match score ROSE in four of the six (`W4-T2-P1` 0.584 -> 0.607, crossing
# `MATCH_FLOOR`) BECAUSE it rewards agreement with the transcript on the words
# that are present and is blind to the ones that are gone. **A run that deletes
# a fifth of the speech and gets the rest more right scores BETTER.**
#
# 🟢 WHAT PRIMING GENUINELY DID, said plainly so this is not oversold: it won
# proper nouns and domain terms (`Holzel`, `Vago`, `Silbersweig`, `Teasdale`,
# `qigong`, `MBCT`, `FFMQ`) and lost ordinary English (`whereas`, `lastly`,
# `present`). **+10 terms of 420. That is a real gain and it is not worth
# 13.6 minutes of speech.** Set this back to 70 only alongside a recall-side
# guard; see `_admin/PROJECT-NOTES.md`, "the match score cannot see dropout".
PRIME_TERMS = 0

# 🔴 CAPITALISED-EVERY-TIME IS THE NAME SIGNAL, AND THESE ARE ITS FALSE
# POSITIVES. A discourse connective that always opens a sentence is capitalised
# in every appearance, exactly like a surname. **Measured on `W4-T1-P1`:
# "However" appears 10 times, capitalised every time, never lowercase** -- and so
# do "Overall" and "Similarly". They are indistinguishable from `Grabovac` by
# capitalisation alone, so they are excluded by name.
# ⚠️ **This list is the price of not maintaining a dictionary**, and it is bounded:
# it holds English connectives and transcript furniture, not domain words.
_NOT_A_NAME = frozenset("""
a an the and or but if then than that this these those of in on at to for with
by from as is are was were be been being it its he she they we you i his her
their our your there here when where which who whom whose what why how all any
both each few more most other some such no nor not only own same so too very
can will just should now slide lecture transcript part topic week figure table
page references reference university college london king kings
however therefore overall similarly subsequently including additionally
interestingly another many different setting finally moreover furthermore
consequently meanwhile nevertheless nonetheless firstly secondly thirdly
thus hence indeed likewise conversely importantly notably specifically
""".split())

# 🔴 A LONG WORD IS NOT AUTOMATICALLY A TECHNICAL ONE, so the ordinary long
# words of academic prose are excluded by name. Without this the prompt fills up
# with "particularly" and "understanding" and the names never fit.
_LONG_BUT_ORDINARY = frozenset("""
particularly understanding significant significantly participants participant
different differences difference following important information interested
experience experiences experienced individual individuals mindfulness practice
practise research researchers questions question everything something therefore
however although between because through während associated association
compared comparison including included increase increased decrease decreased
measured measures measurement measurements suggested suggests suggesting
proposed proposal reported reporting described describes conditions condition
intervention interventions treatment treatments training trained outcome
outcomes analysis analyses effects effect studies study results result
""".split())


def prime_terms(text, limit=None):
    """The distinctive vocabulary of a transcript, best first.

    🟢 **PROPER NAMES FIRST, and that ordering is the point rather than tidiness.**
    Measured on the six fallen-back lectures, the recogniser's ordinary-word
    errors stay readable in context (`disentangle` became "distangle") while its
    NAME errors do not: `Grabovac` became "guapurvik" and `Silbersweig` became
    "silver swag". 🔴 **A reader looks a researcher up by name**, so when the cap
    bites it must bite the ordinary words.

    ⚠️ **Capitalisation is the signal for a name and it is imperfect on purpose.**
    A sentence-initial word is skipped because every sentence starts capitalised;
    that loses a name at the start of a sentence, which is a miss rather than a
    wrong answer, and the alternative is a dictionary this project would have to
    maintain.

    🔴 **No fuzzy matching and no stemming.** Every term is a word that is
    literally in the transcript, so the worst case is a prompt that mentions a
    word the lecturer used.
    """
    words = words_of(text or "")

    # 🟢 A WORD IS A NAME IF THE TRANSCRIPT NEVER WRITES IT LOWERCASE, which uses
    # the whole document as evidence instead of guessing at sentence boundaries.
    # 🔴 The punctuation heuristic this replaced lost `Ruimin` to the full stop in
    # "Dr." and still leaked five connectives, both measured on `W4-T1-P1`.
    upper, lower = {}, set()
    for w in words:
        if not w.key:
            continue
        if w.text[:1].isupper():
            upper.setdefault(w.key, w.text)
        else:
            lower.add(w.key)

    names, terms, seen = [], [], set()
    for w in words:
        raw, low = w.text, w.key
        if not low or low in seen or len(raw) < 4:
            continue
        if low in _NOT_A_NAME:
            continue
        if low in upper and low not in lower:
            seen.add(low)
            names.append(upper[low])
        elif (len(raw) >= 8 and low not in _LONG_BUT_ORDINARY and raw.isalpha()):
            seen.add(low)
            terms.append(raw)
    # 🔴 RESOLVED HERE, NOT IN THE SIGNATURE. `limit=PRIME_TERMS` in the
    # signature binds the value at IMPORT, so the setting could not be
    # changed at call time and a test that patched it was silently ignored.
    # A setting that cannot be set is not a setting.
    limit = PRIME_TERMS if limit is None else limit
    return (names + terms)[:limit]


def prime_prompt(text, limit=None):
    """`prime_terms` as the string handed to the recogniser, or `None`.

    🟢 **A sentence rather than a bare list**: the prompt is fed to a model that
    predicts text, and a comma-separated run of nouns in a sentence frame is
    closer to what it was trained on than a heading would be.

    🔴 Returns `None` rather than an empty string when there is nothing to say,
    so the caller passes nothing at all and the recogniser behaves exactly as it
    did before priming existed. **The no-transcript path must not change.**
    """
    got = prime_terms(text, limit)
    if not got:
        return None
    # 🔴🔴 NOT REVERSED, AND IT WAS UNTIL THE MECHANISM CHANGED. While this fed
    # `initial_prompt`, the recogniser kept the LAST 223 tokens, so the names had
    # to go at the END to survive an overflow. **`hotwords` truncates from the
    # FRONT** (`hotwords_tokens[: max_length // 2 - 1]`), so the same reasoning
    # now points the other way and the names belong FIRST, which is also
    # `prime_terms`' own order.
    # 🟢 Both truncations now cut the same end, so there is one order to hold
    # instead of two. ⚠️ **Measured 70 terms against a 223-token cap with 27 to
    # spare, so this should never fire; it decides which half is lost if it ever
    # does.**
    return "This lecture mentions " + ", ".join(got) + "."


# --------------------------------------------------------------------------
# Whisper, at the edge and nowhere else
# --------------------------------------------------------------------------

# 🔴 THE PROMPT ARRIVES ON STDIN, NOT IN `sys.argv`. It is up to ~950 characters
# of the lecturer's own vocabulary, and an argv list is the wrong place for text
# that long: it counts against `ARG_MAX` alongside every clip path in the batch.
# ⚠️ An EMPTY stdin means no priming, and the call is then byte-identical to what
# it was before priming existed, which is what keeps the no-transcript path
# unchanged rather than merely equivalent.
#
# 🔴🔴 `hotwords`, NOT `initial_prompt`, AND THE DIFFERENCE IS THE WHOLE FEATURE
# WORKING OR NOT. QA measured it on the shipped code and I reproduced it against
# the installed `get_prompt` rather than by reading the slicing:
#
#     decoded tokens :    0    50   150   222   223  1000
#     initial_prompt : 181   173    73     1     0     0     <- evicted
#     hotwords       : 181   181   181   181   181   181     <- every window
#
# 🔴 `initial_prompt` is loaded into `all_tokens`, and each window is primed with
# `previous_tokens[-(max_length // 2 - 1):]`. **As the lecture decodes, its own
# speech fills that window and pushes the prompt out**, completely, after 223
# decoded tokens. On a 1,784-second lecture that is about the first minute.
# 🟢 **`hotwords` is re-added on EVERY window** (see `get_prompt`), so it reaches
# the whole recording.
# ⚠️ **The errors that motivated this work are in the first minute**, which is the
# one window where `initial_prompt` still worked, so a rebuild would have looked
# like a partial success and been a first-minute-only one.
RUNNER = r"""
import json, sys
from faster_whisper import WhisperModel
prompt = sys.stdin.read().strip() or None
model = WhisperModel(sys.argv[1], device="cpu", compute_type="int8")
out = {}
for path in sys.argv[2:]:
    segments, info = model.transcribe(path, word_timestamps=True, vad_filter=False,
                                      hotwords=prompt)
    out[path.split("/")[-1]] = {
        "duration": round(info.duration, 3),
        "words": [{"w": w.word, "s": round(w.start, 3), "e": round(w.end, 3),
                   "p": round(w.probability, 3)}
                  for s in segments for w in (s.words or [])],
    }
print(json.dumps(out))
"""


def find_python():
    """An interpreter that can import `faster_whisper`.

    🔴 **No hard-coded home directory.** `STUDY_HUB_WHISPER_PYTHON` wins; then this
    interpreter if it can already do it; then any virtualenv beside the projects
    folder this repo lives in. ⚠️ **Never set an HF cache variable and never pass
    `download_root`**: the models are shared with other projects on this machine
    and pointing this one somewhere else re-downloads gigabytes.
    """
    named = os.environ.get("STUDY_HUB_WHISPER_PYTHON")
    if named and os.path.exists(named):
        return named
    try:
        import faster_whisper  # noqa: F401
        return sys.executable
    except ImportError:
        pass
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    parent = os.path.dirname(here)
    for cand in sorted(glob.glob(os.path.join(parent, "*", "*", ".venv", "bin", "python"))
                       + glob.glob(os.path.join(parent, "*", ".venv", "bin", "python"))):
        probe = subprocess.run([cand, "-c", "import faster_whisper"],
                               capture_output=True)
        if probe.returncode == 0:
            return cand
    return None


# 🔴 **MEASURED AND RULED 2026-09-09, not chosen.** Every caption this project
# made before that date used `base.en`, and nobody picked it: it was this
# function's default and neither caller passed a model. The comparison
# (`rig_whisper_model.py`) found the headline number nearly unmoved (+0.008 and
# +0.010 on two lectures) and the thing it GATES nearly doubled: on a hard
# lecture one clip went 0.583 to 0.620, crossed `MATCH_FLOOR`, and went from 0
# words placed to 300. **The lecture went from 35 cues to 66.**
#
# 🟢 **A true accept, checked by reading both transcriptions rather than
# trusting the score**: `catecholaments` to catecholamines, `hippo thalamus` to
# hypothalamus, `code presser` to cold pressure, `trayer` to trier, plus a
# clause `base.en` never heard.
#
# 🔴 **IT MOVES CLIPS BOTH WAYS**, which is why `FALLBACK_MODEL` exists rather
# than being deleted: another clip fell 0.620 to 0.617 in the same run. **A
# wholesale re-run under one model takes the upside and the downside together.**
# `best_by_clip` is the ruling's answer and the reason both names are here.
#
# ⚠️ **PRICE: about 8x slower**, measured at 0.44x realtime against `base.en`'s
# ~0.05x, so a whole course is an overnight job rather than a coffee break.
# 🟢 **Neither model reaches a kit recipient by this default.** The caption
# modules ship since 2026-09-18, but the engine the kit installs is the forced
# aligner (`install_captions`, about 500 MB, on a person's click), and Whisper
# is only ever used on a machine where somebody built a Whisper venv by hand.
PRIMARY_MODEL = "medium.en"
FALLBACK_MODEL = "base.en"


def rough_words(paths, model=PRIMARY_MODEL, python_bin=None, prompt=None):
    """Whisper's rough text for some clips, as {filename: {duration, words}}.

    🟢 **`prompt` is the lecture's own vocabulary** (see `prime_prompt`), passed
    as `hotwords` so it reaches every window rather than only the first minute.
    It makes the recogniser likelier to write `mechanisms` than "magnum" and
    `Grabovac` than "guapurvik". **`None` means no priming and the call is then
    exactly what it was before priming existed.**

    ⚠️ **A prompt cannot make the recogniser hear a word that is not there, but it
    CAN make it write one it half-heard**, which is the known failure mode of
    priming and is not caught by any readability gate. 🔴 **So a primed run is
    only trustworthy against an UNPRIMED one**: compare, never assume.
    """
    python_bin = python_bin or find_python()
    if not python_bin:
        raise RuntimeError("no interpreter here can import faster_whisper; set "
                           "STUDY_HUB_WHISPER_PYTHON")
    r = subprocess.run([python_bin, "-c", RUNNER, model] + list(paths),
                       input=(prompt or ""), capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("whisper failed: " + (r.stderr or "")[-400:])
    return json.loads(r.stdout)


# --------------------------------------------------------------------------
# The checks
# --------------------------------------------------------------------------

# A lecture in miniature, carrying the three hazards that cost real time:
# a ligature, a word Whisper mishears, and a clip whose words are not in the
# transcript at all.
TRAP_TEXT = ("In this ﬁgure we brieﬂy set out the plan. "
             "The second clip says something else entirely. ")
TRAP_ROUGH = {
    "sound1.mp3": [
        {"w": " In", "s": 0.0, "e": 0.3, "p": 0.9},
        {"w": " this", "s": 0.3, "e": 0.5, "p": 0.9},
        {"w": " figure", "s": 0.5, "e": 0.9, "p": 0.9},
        {"w": " we", "s": 0.9, "e": 1.1, "p": 0.9},
        {"w": " briefly", "s": 1.1, "e": 1.6, "p": 0.9},
        {"w": " set", "s": 1.6, "e": 1.8, "p": 0.9},
        {"w": " out", "s": 1.8, "e": 2.0, "p": 0.9},
        {"w": " the", "s": 2.0, "e": 2.2, "p": 0.9},
        {"w": " plan.", "s": 2.2, "e": 2.6, "p": 0.9},
    ],
    "sound2.mp3": [
        {"w": " Completely", "s": 0.0, "e": 0.6, "p": 0.9},
        {"w": " unrelated", "s": 0.6, "e": 1.2, "p": 0.9},
        {"w": " material", "s": 1.2, "e": 1.8, "p": 0.9},
        {"w": " about", "s": 1.8, "e": 2.2, "p": 0.9},
        {"w": " hedgehogs", "s": 2.2, "e": 2.9, "p": 0.9},
    ],
}


def self_test():
    bad = []
    clips = [Clip("sound1.mp3", 0, TRAP_ROUGH["sound1.mp3"]),
             Clip("sound2.mp3", 1, TRAP_ROUGH["sound2.mp3"])]
    align(TRAP_TEXT, clips)

    if clips[0].score < 0.9:
        bad.append("the ligature clip scored %.2f, so NFKD is not being applied"
                   % clips[0].score)
    said = " ".join(w.text for w in clips[0].words)
    if "ﬁgure" not in said:
        bad.append("the caption lost the lecturer's own spelling: %r" % said)
    if clips[1].words:
        bad.append("a clip whose words are not in the transcript still got cues")

    c = cues(clips[0].words, TRAP_TEXT)
    if not c:
        bad.append("no cues came out of a clip that matched")
    elif c[0].start != 0.0:
        bad.append("the first cue does not start where the clip does: %r" % c[0].start)
    if any(len(x.lines) > CUE_LINES for x in c):
        bad.append("a cue is more than %d lines" % CUE_LINES)

    vtt = to_vtt(c)
    if not vtt.startswith("WEBVTT"):
        bad.append("that is not a WebVTT file")
    if "-->" not in vtt:
        bad.append("the VTT has no cue timings in it")

    if stamp(3661.5) != "01:01:01.500":
        bad.append("timestamps are wrong: %r" % stamp(3661.5))

    for line in bad:
        print("FAIL: " + line)
    print("self-test: %s" % ("FAILED" if bad else "all checks pass"))
    return 1 if bad else 0


def clips_for(course, part, root=".", rough_cache=None):
    """Every playing clip of one part, with Whisper's rough words attached."""
    pkg = os.path.join(root, "courses", course, "packages", part)
    pres = presinfo.load(os.path.join(pkg, "index.html"))
    plan = playing_clips(pres)
    if rough_cache and os.path.exists(rough_cache):
        with open(rough_cache, encoding="utf-8") as fh:
            rough = json.load(fh)
    else:
        rough = rough_words([os.path.join(pkg, "data", name) for _, name in plan])
        if rough_cache:
            with open(rough_cache, "w", encoding="utf-8") as fh:
                json.dump(rough, fh)
    return [Clip(name, slide, rough[name]["words"]) for slide, name in plan
            if name in rough]


# Speech runs at roughly this rate, which is all the precision this needs: it is
# separating 0.02 from 0.85, not measuring anybody's delivery.
WORDS_PER_MINUTE = 150.0

# 🔴 MEASURED across the corpus rather than chosen: eight lectures fall under
# 0.75 and the next one up is 0.75 exactly, so the gap is real. The worst is
# **62 words for 22 minutes of narration**.
#
# ⚠️ CORRECTED 2026-09-03 by the corpus run itself, and it is a correction to
# what this floor was said to MEAN rather than to the number. "Eight lectures
# cannot be captioned" was published twice and is wrong: only FOUR produced no
# cues at all, and they are exactly the four under 0.20. The other four scored
# 0.54 to 0.75 and captioned 3, 11, 9 and 16 clips.
#
#   ratio 0.02 0.14 0.15 0.19 -> 0 files each
#   ratio 0.54 0.63 0.74 0.75 -> 3, 11, 9, 16 files
#
# 🟢 So the floor still does its job, which was only ever to rule lectures OUT
# cheaply before spending Whisper time on them: below it a lecture is not worth
# assuming complete. **It does not predict silence.** A transcript can be far too
# short to be the WHOLE narration and still be the right words for part of it,
# and the aligner refuses per CLIP rather than per lecture.
COVERAGE_FLOOR = 0.75


def coverage(course, part, root="."):
    """How much of a lecture's narration its transcript could possibly account for.

    🟢 **A cheap answer to "can this lecture be captioned at all", with no
    transcription at all.** A transcript with a fifth of the words its own audio
    needs is not a transcript of that audio, and finding that out after a Whisper
    run over the whole corpus would be an expensive way to learn it.

    ⚠️ **It is a floor, not a verdict.** Over 1.0 does not mean good: two lectures
    in this corpus score above 1.1 because their transcripts also carry the
    dialogue of embedded consultation VIDEOS, which is words the narration never
    says. **A ratio only rules lectures OUT.**
    """
    idx = os.path.join(root, "courses", course, "packages", part, "index.html")
    pres = presinfo.load(idx)
    seconds = sum(d for _, _, d in presinfo.sound_for_slide(pres) if d)
    pdf = transcripts.transcript_for(os.path.join(root, "materials", course), part)
    return coverage_ratio(part, seconds, pdf)


def coverage_ratio(part, seconds, pdf):
    """The ratio itself, given seconds of audio and a transcript to read.

    🟢 **Split out so the VIDEO route can ask the same question of the same
    arithmetic.** `coverage` above gets its seconds from the package's own slide
    timings; a plain recording has no package and gets them from the file. **That
    is the only difference between the two, and everything that makes the number
    mean something (which words count, the words-a-minute constant, the ratio)
    lives here so there is one implementation rather than two that drift.**

    ⚠️ **`None` when there is nothing to measure**, never 0.0: a lecture whose
    transcript cannot be read and one whose transcript is empty are different
    facts, and a zero would report the first as the second.
    """
    if not pdf or not seconds:
        return None
    words = len(words_of(spoken_source(transcripts.pdf_text(pdf))))
    expected = seconds / 60.0 * WORDS_PER_MINUTE
    return {"part": part, "seconds": seconds, "words": words,
            "expected": expected, "ratio": words / expected if expected else 0.0}


def survey(root="."):
    """Every lecture, worst first. An artifact two runs can be compared with."""
    rows = []
    for idx in sorted(glob.glob(os.path.join(root, "courses", "*", "packages", "*", "index.html"))):
        parts = idx.split(os.sep)
        course, part = parts[-4], parts[-2]
        got = coverage(course, part, root)
        if got:
            got["course"] = course
            rows.append(got)
    rows.sort(key=lambda r: r["ratio"])
    print("%-9s %-10s %7s %8s %8s %8s" % ("course", "part", "audio", "words", "needs", "ratio"))
    for r in rows:
        print("%-9s %-10s %6.0fs %8d %8.0f %8.2f  %s"
              % (r["course"], r["part"], r["seconds"], r["words"], r["expected"], r["ratio"],
                 "🔴 cannot be the narration" if r["ratio"] < COVERAGE_FLOOR else ""))
    short = [r for r in rows if r["ratio"] < COVERAGE_FLOOR]
    print("\n%d of %d lectures have a transcript too short to be their own narration."
          % (len(short), len(rows)))
    print("⚠️ A ratio above the floor is not a promise: it rules lectures OUT only.")
    return 0


def is_current(vtt, mp3, transcript=None):
    """Whether a caption file is newer than every input it is made from.

    🔴🔴 **THE WORDS COME FROM THE TRANSCRIPT AND ONLY THE TIMINGS FROM THE
    AUDIO, so a `.vtt` is stale when EITHER moves.** This used to compare
    against the mp3 alone.

    ⚠️ **The failure was silent and read as success.** Measured 2026-09-15: the
    transcript recovery rebuilt 15 transcripts and touched not one mp3, so every
    clip of every repaired lecture reported `kept` and a rebuild printed *"0
    written, N already current"*. **A run that did nothing is indistinguishable
    from a run with nothing to do, if the only thing you read is the count.**

    🟢 **A missing transcript falls back to the audio rule rather than refusing
    everything**: `coverage` already treats an unreadable transcript as a fact
    about that lecture, and a freshness test is not the place to start failing.
    """
    if not os.path.exists(vtt):
        return False
    made = os.path.getmtime(vtt)
    if made < os.path.getmtime(mp3):
        return False
    if transcript and os.path.exists(transcript):
        return made >= os.path.getmtime(transcript)
    return True


def _to_wav(media, wav, to_wav=None):
    """One clip's sound as the 16 kHz mono wav the aligner reads.

    The extractor lives in `video_captions` (it is the recording route's) and is
    imported here at call time only: that module imports this one."""
    if to_wav is None:
        import video_captions
        to_wav = video_captions.extract_audio
    return to_wav(media, wav)


def build(course, part, out_dir, root=".", force=False, cache=None, quiet=False,
          models=None, engine=ENGINE_AUTO, to_wav=None):
    """Write one lecture's caption files, and say what happened for every clip.

    🟢 **`engine` chooses what times the words** (`ENGINES`): the forced aligner
    (`time_by_ctc`, transcript first, every word measured) wherever this machine
    can run it, or Whisper matched against the transcript (`align`). Under
    `auto` the choice is `engine_for`'s, and the row for every clip says which
    engine was used, so a track's provenance is never a guess.

    🔴 **`out_dir` has no default inside this repo, deliberately.** Where captions
    live is an open question in the decide lane, and it is a bigger one than a
    path: a caption file is a VERBATIM transcript of a KCL lecturer, where the
    repo today holds prose rewritten from the teaching. **A default would settle
    that by accident.**

    🟢 **Resumable and incremental**, because re-running a whole corpus to add one
    lecture is how a tool stops being used: a clip whose `.vtt` is newer than its
    own audio is skipped, and only then is Whisper asked for anything at all.
    ⚠️ **The forced aligner still LISTENS to every clip on a resume** (the
    timeline must be the whole lecture, see below) **and writes only the stale
    ones**: a resumed build there costs the lecture's emissions, not nothing.
    """
    pkg = os.path.join(root, "courses", course, "packages", part)
    dest = os.path.join(out_dir, part)
    pres = presinfo.load(os.path.join(pkg, "index.html"))
    plan = playing_clips(pres)
    # 🔴 Needed HERE, above the freshness test, and not only at the alignment
    # below: the words a caption carries come from this file, so it decides
    # staleness just as much as the audio does.
    src_pdf = transcripts.transcript_for(os.path.join(root, "materials", course), part)

    todo = []
    rows = []
    for slide, name in plan:
        mp3 = os.path.join(pkg, "data", name)
        vtt = os.path.join(dest, name.replace(".mp3", ".vtt"))
        if not force and is_current(vtt, mp3, src_pdf):
            rows.append({"clip": name, "slide": slide, "state": "kept", "cues": None,
                         "score": None})
            continue
        todo.append((slide, name, mp3, vtt))

    if not todo:
        # 🟢 A lecture that is complete but has no record gets one, from the
        # files; a run that changed nothing does not rewrite one it has.
        if not os.path.exists(os.path.join(dest, SIDECAR_FILE)):
            clip_sidecar(course, part, out_dir, root)
        return rows

    engine = engine_for(engine)
    if engine == ENGINE_CTC:
        # 🟢 TRANSCRIPT FIRST: with nothing to place there is nothing to listen
        # for, so the check comes before any audio work rather than after it.
        if not src_pdf:
            for slide, name, _, _ in todo:
                rows.append({"clip": name, "slide": slide, "state": "no transcript",
                             "cues": 0, "score": 0.0, "engine": engine})
            return rows
        text = spoken_source(transcripts.pdf_text(src_pdf))
        # ⚠️ Every clip's wav must exist at once: the lecture is aligned as ONE
        # timeline so that a passage a clip does not hold can be found in the
        # clip that does. The wavs are scratch and go with the directory.
        # 🔴 EVERY clip of the lecture, the kept ones included, and not only
        # `todo`: the timeline has to be the whole lecture or the transcript is
        # forced onto whatever audio is stale. Measured 2026-09-18 on a lecture
        # with 29 of 33 clips current: on the four stale clips alone the aligner
        # placed 1,585 words on 65 seconds, confirmed 130, and refused all four
        # at 0.000 to 0.059; on the whole lecture the same four confirm at 0.875
        # to 1.000. What is WRITTEN below is still only `todo`.
        heard = [(slide, name, os.path.join(pkg, "data", name)) for slide, name in plan]
        clips = [Clip(name, slide, []) for slide, name, _ in heard]
        with tempfile.TemporaryDirectory(prefix="study-hub-captions-") as tmp:
            wavs = [_to_wav(mp3, os.path.join(tmp, "%03d.wav" % k), to_wav)
                    for k, (_, _, mp3) in enumerate(heard)]
            time_by_ctc(text, clips, wavs, log=None if quiet else print)
        chosen = dict.fromkeys((c.name for c in clips), align_ctc.BUNDLE)
        split_gap = RUN_SPLIT_GAP
    else:
        text, clips, chosen, missing = _time_by_whisper(todo, src_pdf, cache, models)
        if missing is not None:
            rows.extend(missing)
            return rows
        split_gap = None
    by_name = {c.name: c for c in clips}

    # 🔴 EVERY CLIP IS MADE BEFORE ANY IS WRITTEN, because the readability gate is
    # about the LECTURE and a per-clip loop cannot see it. This is the route the
    # 43.4% lecture came by, and a gate on the aligner would never have looked.
    made_by, pending = {}, []
    for slide, name, _, vtt in todo:
        c = by_name.get(name)
        if c is None:
            rows.append({"clip": name, "slide": slide, "state": "not transcribed",
                         "cues": 0, "score": 0.0, "model": None, "engine": engine})
            continue
        made = cues(c.words, text, split_gap=split_gap)
        if not made:
            # 🔴 A refused clip leaves NO file. An empty .vtt would be indistinguishable
            # from a clip nobody has run yet, and the next incremental pass would skip it.
            rows.append({"clip": name, "slide": slide, "state": "refused",
                         "cues": 0, "score": c.score, "model": chosen.get(name),
                         "engine": engine})
            continue
        made_by[name] = made
        pending.append((slide, name, vtt, c))

    # ⚠️ The clips ALREADY on disk count too, or an incremental run measures a
    # fraction of the lecture and lets a bad one through one clip at a time.
    whole = [cue for _, name, _, _ in pending for cue in made_by[name]]
    for row in rows:
        if row["state"] == "kept":
            kept = os.path.join(dest, row["clip"].replace(".mp3", ".vtt"))
            try:
                with open(kept, encoding="utf-8") as fh:
                    whole.extend(cues_from_vtt(fh.read()))
            except OSError:
                pass

    bad, got = too_fast_to_read(whole)
    if bad:
        # 🔴 NOTHING IS WRITTEN. Captions a reader cannot follow are worse than
        # none, because a reader trusts them; that is the argument MATCH_FLOOR
        # rests on, applied to the output. Files already on disk are LEFT: taking
        # them away is a withdrawal, which is a ruling and not a build's business.
        for slide, name, _, c in pending:
            rows.append({"clip": name, "slide": slide, "state": "too fast to read",
                         "cues": 0, "score": c.score, "legibility": got,
                         "model": chosen.get(name), "engine": engine,
                         # the WHY beside the how-bad, same as the video route
                         "fabricated": c.fabricated, "unconfirmed": c.unconfirmed})
        return rows

    os.makedirs(dest, exist_ok=True)
    for slide, name, vtt, c in pending:
        # 🔴🔴 MEASURED AGAINST WHAT IS BEING OVERWRITTEN, BEFORE OVERWRITING IT.
        # `score` cannot see a rebuild that DELETES speech -- it is precision over
        # the words the model produced, so a run that drops a hard passage and
        # gets the rest right scores 1.000, the same as a perfect one. **This is
        # the only number in the build that can tell.** It counts and does not
        # refuse, exactly as `legibility` does.
        row = {"clip": name, "slide": slide, "state": "written",
               "cues": len(made_by[name]), "score": c.score,
               "model": chosen.get(name), "engine": engine}
        if c.unconfirmed is not None:
            row["unconfirmed"] = c.unconfirmed
        try:
            with open(vtt, encoding="utf-8") as fh:
                row["recall"] = recall_against(cues_from_vtt(fh.read()),
                                               made_by[name])
        except OSError:
            pass                          # a clip with nothing to compare against
        with open(vtt, "w", encoding="utf-8") as fh:
            fh.write(to_vtt(made_by[name]))
        rows.append(row)
    # 🔴 THE LECTURE'S RECORD, FROM THE FILES, at the end of every run that
    # leaves it with tracks. Not from `whole` above: that list mixes cues made
    # here with cues read back from disk, and a cue read back carries offsets
    # into its own text, not into the transcript (`cues_from_vtt`). Reading the
    # files is also the only computation that is right for a RESUMED build,
    # where the cues in memory are only the clips this run touched.
    written = [name for _, name, _, _ in pending]
    models_used = {chosen.get(name) for name in written}
    clip_sidecar(course, part, out_dir, root, engine=engine,
                 model=models_used.pop() if len(models_used) == 1 else None,
                 written=written)
    return rows


def timings_from(engine, model=None):
    """The sidecar's `timings_from` for an engine, or None when the engine is
    not known: a null says "not recorded", which is true of every track built
    before the record existed, and a guess would not be."""
    if engine == ENGINE_CTC:
        return {"tool": TOOL_CTC, "model": align_ctc.BUNDLE}
    if engine == ENGINE_WHISPER:
        return {"tool": TOOL_WHISPER, "model": model}
    return None


def clip_sidecar(course, part, out_dir, root=".", engine=None, model=None,
                 written=()):
    """`captions.json` for one lecture of the interactive slide player, from
    the `sound<N>.vtt` files on disk. Returns the card, or None when the lecture
    has no tracks: a record of nothing would be a claim about nothing.

    🔴 **THE CLAIM IS PER LECTURE, and it is the SAME number the recording route
    writes, by the same function.** The clip route aligns the whole lecture as
    ONE timeline and hands each word to the clip holding its start, so a clip's
    word set is an output of the alignment, not a division of the transcript:
    there is no per-clip coverage for a number to be about. `transcript_covered`
    here answers exactly what it answers for a recording, "of the words the
    aligner was given, how many reached a cue", and the pack prints the two
    kinds of lecture in one column because of it.

    🟢 **The clip route's OWN fact is `clips`: how many of the clips that play
    have a track.** The coverage alone cannot tell an unfinished build (0.205,
    11 of 27 clips) from a complete one whose audio lacks 40% of the transcript
    (0.584, 19 of 19); the pair can. Both halves are the status screen's
    (`caption_course.package_row`): every `sound*.vtt` in the folder, against
    `playing_clips`.

    🔴 **Spans are found PER CLIP, with a fresh cursor, and unioned.** See
    `cue_spans_in`; clips do not play in transcript order.

    ⚠️ **What is honestly null, and why.** `words_are` is always the transcript,
    because this route has no heard fallback, so `heard_because` and
    `heard_confidence` are null. `media.kaltura_entry`: a package is not a
    recording. `cues_from` / `cues_to`: each clip has its own clock starting at
    zero, so a lecture-wide number would be in no units at all. The matching
    facts (`audio_matched_transcript`, `cues_anchored_on`, `fabricated_time`,
    `unconfirmed`, `alignment`) describe ONE alignment run, and a lecture's
    tracks may come from several; the file-derived numbers are the lecture's.

    🟢 **`engine` is the run's only when every track on disk was written by
    this run** (`written`), else the existing card's when it agrees, else null:
    a track built before the record existed has no engine anybody can name.
    The backfill (`clip_sidecars`) passes nothing and records exactly that.

    ⚠️ `built_on` is the day this CARD was written; for a backfilled lecture
    that is not the day its tracks were.
    """
    pkg = os.path.join(root, "courses", course, "packages", part)
    dest = os.path.join(out_dir, part)
    tracks = sorted(glob.glob(os.path.join(dest, "sound*.vtt")))
    if not tracks:
        return None
    pres = presinfo.load(os.path.join(pkg, "index.html"))
    plan = playing_clips(pres)
    src_pdf = transcripts.transcript_for(os.path.join(root, "materials", course), part)
    text = spoken_source(transcripts.pdf_text(src_pdf)) if src_pdf else ""
    all_cues, spans = [], []
    for vtt in tracks:
        with open(vtt, encoding="utf-8") as fh:
            raw = fh.read()
        all_cues.extend(cues_from_vtt(raw))
        if text:
            spans.extend(cue_spans_in(text, raw))       # fresh cursor per clip

    path = os.path.join(dest, SIDECAR_FILE)
    prior = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                prior = json.load(fh)
        except (OSError, ValueError):
            prior = {}
        if not isinstance(prior, dict):
            prior = {}
    names = {os.path.basename(t)[:-4] + ".mp3" for t in tracks}
    if engine and names <= set(written):
        engine_of, timings = engine, timings_from(engine, model)
    elif engine is None or prior.get("engine") == engine:
        engine_of, timings = prior.get("engine"), prior.get("timings_from")
    else:
        engine_of, timings = None, None

    seconds = playing_seconds(pres)
    card = {
        "schema_version": SCHEMA_VERSION,
        "doc": part,
        "source": PACKAGE,
        "words_are": TRANSCRIPT,
        "words_from": os.path.relpath(src_pdf, root) if src_pdf else None,
        "heard_because": None,
        "heard_confidence": None,
        "engine": engine_of,
        "timings_from": timings,
        "unconfirmed": None,
        "alignment": None,
        "media": {"kaltura_entry": None,
                  "seconds": round(seconds, 1) or None,
                  "minutes": int(round(seconds / 60.0)) if seconds else None},
        "built_on": time.strftime("%Y-%m-%d"),
        "cues": len(all_cues),
        "cues_matched_to_transcript": len(spans) if text else None,
        "heard_word_match": None,
        "audio_matched_transcript": None,
        "cues_anchored_on": None,
        "fabricated_time": None,
        "legibility": legibility(all_cues),
        "transcript_covered": covered_from_spans(text, spans) if text else None,
        "cues_from": None,
        "cues_to": None,
        "clips": {"captioned": len(tracks), "plays": len(plan)},
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(card, fh, indent=2, sort_keys=True)
        fh.write("\n")
    return card


def clip_sidecars(course, root=".", out_dir=None, log=None, only=None):
    """Bring every clip-route lecture of a course forward: a `captions.json`
    beside its tracks, written from the files with no audio, no model and no
    torch, exactly as `video_captions.residecar` does for recordings.

    ⚠️ Not a caption build: no `.vtt` is read for anything but its text, and
    none is written. A lecture with no tracks gets no card.
    """
    log = log or (lambda *a: None)
    out_dir = out_dir or os.path.join(root, "courses", course, "captions")
    rows = []
    pattern = os.path.join(root, "courses", course, "packages", "*", "index.html")
    for idx in sorted(glob.glob(pattern)):
        part = os.path.basename(os.path.dirname(idx))
        if only and part not in only:
            continue
        try:
            card = clip_sidecar(course, part, out_dir, root)
        except Exception as e:                      # one lecture's failure is one fact
            rows.append({"doc": part, "error": str(e)})
            log("%-11s FAILED: %s" % (part, e))
            continue
        if card is None:
            continue
        rows.append({"doc": part, "covered": card["transcript_covered"],
                     "cues": card["cues"], "clips": card["clips"],
                     "engine": card["engine"]})
        log("%-11s covered %s  clips %d of %d  cues %d  engine %s"
            % (part, card["transcript_covered"], card["clips"]["captioned"],
               card["clips"]["plays"], card["cues"], card["engine"] or "not recorded"))
    return rows


def _time_by_whisper(todo, src_pdf, cache, models):
    """The Whisper half of `build`, exactly as it ran before the forced aligner.

    Returns `(text, clips, chosen, missing)`; `missing` is the rows to return
    instead when there is no transcript, else None.
    """
    # 🔴 A RE-RUN IS A CLIP BEING REBUILT OVER A `.vtt` THAT ALREADY EXISTS, and
    # the ruling treats it differently from new work: the bigger model is better
    # on average and NOT on every clip, so a re-run listens with BOTH and keeps
    # the better per clip. New work just uses the primary.
    # ⚠️ BOTH MODELS RUN OVER THE WHOLE `todo` SET, never over the re-run subset
    # alone: `align` scores one monotone matcher across every clip at once, so a
    # partial set would be scored as a lecture that never existed.
    if models is None:
        rerun = any(os.path.exists(vtt) for _, _, _, vtt in todo)
        models = (PRIMARY_MODEL, FALLBACK_MODEL) if rerun else (PRIMARY_MODEL,)
    models = tuple(models)

    if cache and os.path.exists(cache):
        with open(cache, encoding="utf-8") as fh:
            cached = json.load(fh)
        # ⚠️ An older cache holds one model's words directly rather than a map
        # keyed by model. Both shapes are read, so a cache written before this
        # change still works and is attributed to the model that made it.
        if by_model_cache(cached):
            rough_by_model = cached
            # 🟢 THE CACHE IS AUTHORITATIVE FOR WHAT IT HOLDS, and `models` is
            # the ORDER the chooser scores in, so it must name EVERY model in
            # the cache. 🔴 Filtering it down to the requested ones instead left
            # `best_by_clip` scoring one model while holding two, so every clip
            # was attributed to the primary and the fallback never won one.
            # Measured on the real cache: `sound11` should have gone to
            # `base.en` at 0.620 against 0.617 and did not.
            # ⚠️ The requested order still comes FIRST, so a tie goes to the
            # primary exactly as it does without a cache.
            models = tuple([m for m in models if m in cached]
                           + [m for m in cached if m not in models])
        else:
            rough_by_model = {models[0]: cached}
    else:
        rough_by_model = {m: rough_words([mp for _, _, mp, _ in todo], model=m)
                          for m in models}
        if cache:
            with open(cache, "w", encoding="utf-8") as fh:
                json.dump(rough_by_model, fh)

    pdf = src_pdf
    if not pdf:
        missing = [{"clip": name, "slide": slide, "state": "no transcript",
                    "cues": 0, "score": 0.0, "engine": ENGINE_WHISPER}
                   for slide, name, _, _ in todo]
        return None, [], {}, missing
    text = spoken_source(transcripts.pdf_text(pdf))

    # 🟢 THE PER-CLIP CHOICE, and it needs the transcript, which is why it
    # happens here rather than beside the transcription.
    if len(rough_by_model) > 1:
        chosen = best_by_clip(text, rough_by_model, models)
        rough = merge_rough(rough_by_model, chosen)
    else:
        only_model = list(rough_by_model)[0]
        rough = rough_by_model[only_model]
        chosen = dict.fromkeys(rough, only_model)

    clips = [Clip(name, slide, rough[name]["words"]) for slide, name, _, _ in todo
             if name in rough]
    align(text, clips)
    return text, clips, chosen, None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--align", nargs=2, metavar=("COURSE", "PART"))
    ap.add_argument("--survey", action="store_true")
    ap.add_argument("--build", nargs=2, metavar=("COURSE", "PART"))
    ap.add_argument("--sidecars", metavar="COURSE",
                    help="write captions.json for every clip-route lecture of the "
                         "course that has tracks, from the files alone; --out is "
                         "where the tracks are (default: the course's captions folder)")
    ap.add_argument("--out", help="where the .vtt files go; REQUIRED for --build, "
                                  "and deliberately has no default")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--cache", help="a rough-words json to use instead of running whisper")
    ap.add_argument("--engine", choices=ENGINES, default=ENGINE_AUTO,
                    help="what times the words: the forced aligner (ctc) where this "
                         "machine can run it, else whisper; `auto` decides")
    ap.add_argument("--root", default=".")
    a = ap.parse_args(argv)
    if a.self_test:
        return self_test()
    if a.survey:
        return survey(a.root)
    if a.sidecars:
        rows = clip_sidecars(a.sidecars, a.root, a.out, log=print)
        failed = [r for r in rows if "error" in r]
        print("%d lecture(s) carded, %d failed" % (len(rows) - len(failed), len(failed)))
        return 1 if failed else 0
    if a.build:
        if not a.out:
            print("--build needs --out: where captions live is an open question "
                  "in the decide lane, so there is no default")
            return 2
        course, part = a.build
        rows = build(course, part, a.out, a.root, a.force, a.cache, engine=a.engine)
        print("%-14s %5s %8s %6s  %s" % ("clip", "slide", "score", "cues", "state"))
        for r in rows:
            print("%-14s %5d %8s %6s  %s"
                  % (r["clip"], r["slide"],
                     "-" if r["score"] is None else "%.3f" % r["score"],
                     "-" if r["cues"] is None else r["cues"], r["state"]))
        wrote = sum(1 for r in rows if r["state"] == "written")
        refused = sum(1 for r in rows if r["state"] == "refused")
        print("\n%d written, %d refused, %d already current."
              % (wrote, refused, sum(1 for r in rows if r["state"] == "kept")))
        # 🔴 THE ONE NUMBER NOTHING ELSE IN THIS REPORT CAN GIVE. `score` is
        # precision, so a rebuild that DELETED a passage scores 1.000.
        whole = recall_total(rows)
        if whole:
            print("against what was overwritten: %.1f%% of the voiced seconds and "
                  "%.1f%% of the words, over %d clip(s)."
                  % (100 * whole["kept"], 100 * whole["kept_words"], whole["clips"]))
            if whole["under_floor"]:
                print("🔴 THIS REBUILD LOST MORE THAN %d%% OF THE LECTURE. The match "
                      "score CANNOT see this: a run that deletes a hard passage and "
                      "gets the rest right scores 1.000. Look before you keep it."
                      % round(100 * (1 - RECALL_FLOOR)))
            else:
                low = [r for r in rows if r.get("recall", {}).get("under_floor")]
                if low:
                    print("(%d clip(s) individually below the floor, which a footer "
                          "strip does on a short clip: %s)"
                          % (len(low), ", ".join(r["clip"] for r in low)))
        if refused:
            print("🔴 A refused clip leaves no file on purpose: an empty one would be "
                  "indistinguishable from a clip nobody has run.")
        return 0
    if a.align:
        course, part = a.align
        clips = clips_for(course, part, a.root, a.cache)
        pdf = transcripts.transcript_for(os.path.join(a.root, "materials", course), part)
        if not pdf:
            print("no transcript for %s %s" % (course, part))
            return 1
        text = spoken_source(transcripts.pdf_text(pdf))
        align(text, clips)
        print("%-14s %5s %8s %6s  %s" % ("clip", "slide", "score", "cues", "first cue"))
        for c in clips:
            cs = cues(c.words, text)
            first = cs[0].text().replace("\n", " / ") if cs else ""
            print("%-14s %5d %8.3f %6d  %s"
                  % (c.name, c.slide, c.score, len(cs), first[:60]))
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
