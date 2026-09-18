#!/usr/bin/env python3
"""Every transcript word on a MEASURED time: CTC forced alignment, no Whisper.

    python3 server/align_ctc.py --self-test                 # the stdlib half
    <venv python> server/align_ctc.py <request.json> <response.json>

**The problem this replaces, in one sentence:** the previous aligner asked a
speech recogniser to write down what it heard, then string-matched those words
against the transcript, and every word the recogniser misspelt, or the
transcript spelt differently, became a hole that had to be interpolated. On a
long recording the holes ran to hundreds of words (`captions.fabricated_time`
measured passages of 521 words squeezed into under a second) and the readable
answer was to throw the transcript away and ship the recogniser's own words.

**The shape here runs the other way round.** The transcript is treated as the
answer and the audio is asked WHEN each word of it was said:

1. A character-level acoustic model (`WAV2VEC2_ASR_BASE_960H`, through
   `torchaudio`) turns the audio into one probability distribution over letters
   every 20 ms. That runs once per recording at about twenty times real time on
   this machine's CPU, against the recogniser's 0.4x.
2. A greedy decode of those same letters gives a rough "heard" word list for free.
   It is used ONLY to find anchors: runs of `MIN_RUN` or more transcript words
   that were heard exactly, in order. Nothing the greedy decode gets wrong can
   move a word, because a wrong word is simply not an anchor.
3. Between anchors, `torchaudio.functional.forced_align` (the CTC Viterbi
   alignment) places every transcript character on the frames of that window.
   **A misheard word is not a hole any more**: the aligner is told the word is
   there and finds the frames that fit it best. Every word comes back with a
   time AND a score, the mean probability of its letters on the frames chosen.
4. **The score is what makes the result checkable.** A word the transcript has
   and the audio does not (a paragraph that was never read out) scores near
   zero; a word that was said scores near one. So the failure the old design
   could only detect by its symptom (cues running too fast) is now located word
   by word, and `captions` can drop or refill exactly that stretch.

🔴 **THIS MODULE IS TWO HALVES IN ONE FILE, on purpose, and the seam is `torch`.**
Everything above `# -- the torch half --` is stdlib and imported by `captions`
from the ordinary interpreter: tokenising, anchor finding, window planning,
splitting a timeline back into clips. Everything below imports `torch` lazily
and runs only as a subprocess in the interpreter `find_python` locates. **The
server never imports this file** (its import-closure test keeps caption building
out, exactly as it keeps Whisper out), and a reader's machine needs none of it:
captions ship as `.vtt` data.

⚠️ **The windowing is not a refinement, it is what makes the DP possible.** The
forced alignment keeps a backpointer per (frame, token) pair; a 30-minute
recording against its 3,500-word transcript is about 90,000 frames by 40,000
tokens, five gigabytes. Anchors cut that into windows a few seconds wide, and
`MAX_WINDOW_S` caps the rare long one.

**Measured before it was built** (2026-09-15, prototype against the same model):
on a clean 5-minute lecture the windowed alignment reproduced a whole-file
alignment to the frame (median 0.000 s, p99 0.10 s); against the recogniser's
timings on the 746 words both had, median 0.04 s, p99 0.51 s, none over a
second; with 80 words deleted from the transcript, 80 foreign words inserted,
half the text replaced or two paragraphs swapped, the words NOT touched moved a
median of 0.00 s and the words that were scored under 0.2. A 13-clip package
aligned as one timeline put 99.8% of its words inside the cue spans the previous
design had shipped for it.
"""
import bisect
import glob
import json
import os
import re
import subprocess
import sys
import time
import difflib

# The model's own alphabet, in its own order. `-` is the CTC blank (index 0) and
# `|` is the word boundary. 🔴 Pinned here as a constant so the stdlib half can
# tokenise without loading the model; the torch half asserts the bundle still
# agrees before it aligns anything, so a changed model cannot silently shift
# every letter by one.
LABELS = "-|ETAONIHSRDLUMWCFGYPBVK'XJQZ"
BLANK = 0
SEP = LABELS.index("|")
LETTER = {c: i for i, c in enumerate(LABELS)}
BUNDLE = "WAV2VEC2_ASR_BASE_960H"

SAMPLE_RATE = 16000
FRAME_SAMPLES = 320                    # the model emits one frame per 320 samples
FRAME = FRAME_SAMPLES / float(SAMPLE_RATE)   # 0.02 s

# 🔴 MEASURED, not chosen, and the two numbers pull against each other. Shorter
# runs give more anchors and more false ones: a run of two ("of the") matches
# somewhere in every minute of every lecture. Longer runs leave long windows.
# Four is where the previous design's own phrase-run measurement settled
# ("the honest recommendation is runs of about 4"), and at four the clean
# lecture anchors 80% of its words and the 13-clip package 72%.
MIN_RUN = 4
# Frames of slack either side of an anchor word when its own letters are
# aligned inside the frames the greedy decode gave it.
ANCHOR_SLACK = 5
# A window between two anchors longer than this is split in proportion to its
# tokens. The DP's memory is frames times tokens; at 480 s a window with no
# anchor at all still fits in a few hundred megabytes.
MAX_WINDOW_S = 480
# The model is run in chunks with context on both sides, then the context is
# trimmed, so a chunk boundary never falls inside a word's frames.
CHUNK_S = 20
CONTEXT_S = 2

# 🔴 THE FLOOR BETWEEN "SAID" AND "NOT SAID", per word. Measured on the clean
# lecture: real words score 0.85 to 1.00 with a tail of elided function words
# down to about 0.2; foreign words inserted into the transcript score 0.00 to
# 0.05. A single word under the floor is normal (the lecturer swallowed it); a
# RUN of them is a passage the audio does not contain, and `REGION_WORDS` is how
# long a run has to be before it is treated as one.
CONFIRM_FLOOR = 0.5
REGION_WORDS = 8

# 🔴🔴 THE SECOND SHAPE OF "NOT SAID", found LIVE 2026-09-16, and the run rule
# above is blind to it by construction. A wrong PASSAGE made of ordinary words
# never gives that rule its eight low words in a row: a lecture's page carried
# one slide's words a second time under the next slide, the aligner had to
# spread the 125-word copy over 107 s of different speech, about half of them
# scored under the floor and NEVER eight together, and the sidecar certified
# the lecture clean at 0 unconfirmed while the reader watched one slide's
# text over the next slide's speech.
# 🟢 So the second rule asks a different question: over any WINDOW_WORDS
# consecutive words, how many are under the floor, and how many did the audio
# anchor verbatim? A wrong copy answers "half, and none" in every window it
# fills. Genuine speech never does: see `suspect_regions` for the margins.
WINDOW_WORDS = 24
WINDOW_LOW = 14             # low words that condemn a window on their own
WINDOW_LOW_UNANCHORED = 10  # low words that condemn it when it also anchors
WINDOW_ANCHORS = 2          # ...no more than this many words verbatim


# --------------------------------------------------------------------------
# Words as the model hears them
# --------------------------------------------------------------------------

ONES = ("zero one two three four five six seven eight nine ten eleven twelve "
        "thirteen fourteen fifteen sixteen seventeen eighteen nineteen").split()
TENS = "zero ten twenty thirty forty fifty sixty seventy eighty ninety".split()


def num_words(n):
    """An integer as the words a British lecturer says for it."""
    n = int(n)
    if n < 20:
        return [ONES[n]]
    if n < 100:
        t, o = divmod(n, 10)
        return [TENS[t]] + ([ONES[o]] if o else [])
    if n < 1000:
        h, r = divmod(n, 100)
        return [ONES[h], "hundred"] + (["and"] + num_words(r) if r else [])
    if n < 1000000:
        k, r = divmod(n, 1000)
        return num_words(k) + ["thousand"] + (num_words(r) if r else [])
    m, r = divmod(n, 1000000)
    return num_words(m) + ["million"] + (num_words(r) if r else [])


PIECE = re.compile(r"\d+(?:\.\d+)?|[a-z']+|%")
THOUSANDS = re.compile(r"(?<=\d),(?=\d{3}\b)")      # 1,000 is one number


def spoken_tokens(word):
    """A transcript word as the lower-case tokens the aligner should listen for.

    🔴 **The DISPLAY word is never changed**; this is the aligner's copy. `75%`
    is heard as "seventy five percent", `1998` as "nineteen ninety eight",
    `0.5` as "zero point five". A digit string handed to a letter model as
    letters would match nothing, and every number in a lecture would become a
    hole exactly where the old design's holes were.
    """
    w = THOUSANDS.sub("", word.lower())
    out = []
    for piece in PIECE.findall(w):
        if piece == "%":
            out.append("percent")
        elif piece[0].isdigit():
            if "." in piece:
                a, b = piece.split(".", 1)
                out += num_words(a) + ["point"] + [ONES[int(d)] for d in b]
            elif len(piece) == 4 and (1900 <= int(piece) < 2000
                                      or 2010 <= int(piece) <= 2099):
                # "nineteen ninety eight", "twenty twenty"; 2000 to 2009 are
                # said as thousands ("two thousand and five") and fall through
                y = int(piece)
                out += num_words(y // 100) + (["hundred"] if y % 100 == 0
                                              else num_words(y % 100))
            else:
                out += num_words(piece)
        else:
            out.append(piece.strip("'") or piece)
    return [t for t in out if t]


def tokenise(words):
    """Transcript words to model tokens: `(ids, owner)`.

    `ids` is the character sequence the aligner is asked to place, with a `|`
    between words; `owner[k]` is the index of the transcript word token `k`
    belongs to, or -1 for a separator. A word with no alignable letters (a lone
    symbol) owns nothing and gets no time.
    """
    ids, owner = [], []
    for i, w in enumerate(words):
        chars = [LETTER[c] for t in spoken_tokens(w) for c in t.upper()
                 if c in LETTER]
        if not chars:
            continue
        if ids:
            ids.append(SEP)
            owner.append(-1)
        ids += chars
        owner += [i] * len(chars)
    return ids, owner


def token_ranges(owner):
    """`{word_index: (first_token, last_token)}` for every word that owns tokens."""
    out = {}
    for k, o in enumerate(owner):
        if o < 0:
            continue
        if o in out:
            out[o] = (out[o][0], k)
        else:
            out[o] = (k, k)
    return out


def _plain(w):
    return re.sub(r"[^a-z0-9']", "", w.lower())


def matching_runs(a, b, min_run=MIN_RUN):
    """Every maximal run of `min_run` or more equal tokens, ANYWHERE: `[(i, j, n)]`.

    🔴 **Not a diff, and that is the whole point.** A diff (`SequenceMatcher`,
    what the previous design used) keeps only matches that sit in one order in
    both texts. **This corpus's transcripts do not run in the audio's order**: on
    the mindfulness lecture measured 2026-09-15 the transcript's blocks of 100
    to 450 words are permuted against the recording (a slide-by-slide document
    read out in a different sequence), and a diff can keep only the longest
    chain, so 45% of the words landed in a run the aligner then squeezed into
    eight seconds. **A k-gram index finds a block wherever it was said.**
    """
    index = {}
    for j in range(len(b) - min_run + 1):
        index.setdefault(tuple(b[j:j + min_run]), []).append(j)
    runs, reach = [], {}
    for i in range(len(a) - min_run + 1):
        for j in index.get(tuple(a[i:i + min_run]), ()):
            d = j - i
            if reach.get(d, -1) > i:
                continue                  # inside a run already found on this diagonal
            n = min_run
            while i + n < len(a) and j + n < len(b) and a[i + n] == b[j + n]:
                n += 1
            runs.append((i, j, n))
            reach[d] = i + n
    return runs


def select_runs(runs, n_a, n_b, min_run=MIN_RUN):
    """The runs to trust, longest first, none overlapping another in either text.

    A phrase said twice matches twice; the longer, more specific run claims its
    place first and a shorter one is trimmed to whatever is still free, or
    dropped when less than `min_run` of it is. Returned in text-`a` order."""
    taken_a = bytearray(n_a)
    taken_b = bytearray(n_b)
    out = []
    for i, j, n in sorted(runs, key=lambda r: (-r[2], r[0], r[1])):
        k = 0
        while k < n:
            while k < n and (taken_a[i + k] or taken_b[j + k]):
                k += 1
            s = k
            while k < n and not taken_a[i + k] and not taken_b[j + k]:
                k += 1
            if k - s >= min_run:
                out.append((i + s, j + s, k - s))
                for q in range(s, k):
                    taken_a[i + q] = taken_b[j + q] = 1
    return sorted(out)


def find_anchors(words, heard, min_run=MIN_RUN):
    """Transcript words the greedy decode heard exactly, in runs: `[(word, heard)]`.

    Spoken form on both sides, so the transcript's `75%` (three tokens) meets the
    decode's `SEVENTY FIVE PERCENT`. The result is in TRANSCRIPT order and is not
    monotone in `heard`: a block the lecturer read out of the transcript's order
    is anchored where it was actually said. `segments` sorts that out."""
    a_flat, a_owner = [], []
    for i, w in enumerate(words):
        for t in spoken_tokens(w):
            a_flat.append(t)
            a_owner.append(i)
    b = [_plain(h[0]) for h in heard]
    if not a_flat or not b:
        return []
    out, seen = [], set()
    for i, j, n in select_runs(matching_runs(a_flat, b, min_run), len(a_flat),
                               len(b), min_run):
        for k in range(n):
            wi = a_owner[i + k]
            if wi in seen:
                continue
            seen.add(wi)
            out.append((wi, j + k))
    return out


def pins(anchors, heard, ranges):
    """Anchors as `(word, start_frame, end_frame)`, one per anchored word, in
    transcript order. A word that owns no tokens (a bare symbol) cannot pin."""
    out = []
    for wi, hj in anchors:
        if wi not in ranges:
            continue
        _, fs, fe = heard[hj]
        if out and wi <= out[-1][0]:
            continue
        out.append((wi, fs, fe))
    return out


# ⚠️ The two numbers that decide where one block of the transcript ends and the
# next begins. Consecutive anchors stay in one segment while the audio between
# them is no longer than the words between them could take at a slow pace, plus
# a pause. **Breaking too eagerly is harmless** (the words between are aligned
# in the same gap either way, see `plan_windows`); failing to break is what
# squeezes a block into somebody else's seconds.
SECONDS_PER_WORD = 0.5
SEGMENT_PAUSE_S = 20
# A segment carrying fewer anchored words than this, on its own, is more likely
# a phrase the lecturer happens to say twice than a block read out of order.
MIN_SEGMENT_WORDS = 2 * MIN_RUN
# A stretch with no anchor at all may be moved to the gap it fits best, but
# only a stretch this long (about sixteen words): shorter ones fit anywhere.
# `move_to` has the measurements.
MIN_MOVE_TOKENS = 100
MOVE_MARGIN = 0.15         # how clearly one gap must beat the runner-up


def segments(pin_list, seconds_per_word=SECONDS_PER_WORD,
             pause_s=SEGMENT_PAUSE_S, min_words=MIN_SEGMENT_WORDS):
    """Runs of anchors that advance together in transcript AND in time.

    Returns `[[(word, fs, fe), ...], ...]` in transcript order. Anchors are
    split where the audio steps backwards, or forwards by more than the words
    between could fill; a segment left holding under `min_words` anchors is
    dropped as coincidence, and the survivors are chained once more so the two
    halves a false anchor had split are rejoined.

    ⚠️ **Then no segment may span another's seconds.** The forward allowance
    is generous enough (twice the words at half a second each, plus a pause)
    that a lecturer who breaks off for half a minute to read a different slide
    can leave the first block chained straight across the detour. Measured on
    the mindfulness introduction: a 560-word segment ran from 18 s to 341 s
    with a 60-word block said at 151 to 182 s inside it, and the gaps between
    time-neighbours, which assume the segments tile the timeline, came out
    negative on one side and 160 s of already-anchored speech on the other.
    So a segment is cut at any anchor gap another segment starts inside."""
    def chain(items):
        segs = []
        for wi, fs, fe in items:
            if segs:
                pw, _, pe = segs[-1][-1]
                allowed = (2 * (wi - pw - 1) * seconds_per_word + pause_s) / FRAME
                if pe <= fs <= pe + allowed:
                    segs[-1].append((wi, fs, fe))
                    continue
            segs.append([(wi, fs, fe)])
        return segs

    kept = [p for seg in chain(pin_list) if len(seg) >= min_words for p in seg]
    segs = [seg for seg in chain(kept) if len(seg) >= min_words]
    while True:
        starts = sorted(seg[0][1] for seg in segs)
        split = []
        for seg in segs:
            cut = 0
            for k in range(1, len(seg)):
                pe, fs = seg[k - 1][2], seg[k][1]
                if any(pe < st < fs for st in starts):
                    split.append(seg[cut:k])
                    cut = k
            split.append(seg[cut:])
        split = [seg for seg in split if len(seg) >= min_words]
        if len(split) == len(segs):
            return segs
        segs = split


def _split_long(toks, f_lo, f_hi, ids, max_frames):
    """A window over more than `max_frames`, cut at a word boundary near its
    middle with the frames shared in proportion to the tokens, recursively."""
    if f_hi - f_lo <= max_frames or len(toks) < 2:
        return [(toks, f_lo, f_hi)]
    mid = len(toks) // 2
    cut = None
    for step in range(len(toks)):
        for cand in (mid + step, mid - step):
            if 0 < cand < len(toks) and (toks[cand] is None or ids[toks[cand]] == SEP):
                cut = cand
                break
        if cut is not None:
            break
    if cut is None:
        cut = mid
    f_mid = f_lo + int((f_hi - f_lo) * cut / float(len(toks)))
    return (_split_long(toks[:cut], f_lo, f_mid, ids, max_frames)
            + _split_long(toks[cut:], f_mid, f_hi, ids, max_frames))


def move_to(n_toks, cut_total, totals, adjacent, min_toks=MIN_MOVE_TOKENS,
            floor=CONFIRM_FLOOR, margin=MOVE_MARGIN):
    """The gap a stretch should move to, or None to leave it with its neighbours.

    🔴 **A short stretch scores well almost anywhere.** Measured on the
    mindfulness lecture: eight-word stretches averaged 0.7 to 0.9 per token in
    gaps they were never said in, because a few dozen letters can always find
    plausible spikes in half a minute of speech, and a bigger gap offers more
    of them. So a move needs all three of: a stretch long enough to mean
    something (`min_toks`, about sixteen words); a placement beside its
    neighbours that is unconfirmed on average (below `floor`); and one gap that
    stands out, at or above `floor` and `margin` clear of the runner-up.
    ⚠️ **The same lecture's one genuine case cleared every bar with room**: 267
    tokens, 0.00 beside its neighbours, 0.73 in the right gap against 0.43 in
    the next best. Every other stretch of 100 tokens or more scored 0.6 to 0.85
    where it sat and 0.33 to 0.58 anywhere else."""
    if n_toks < min_toks or cut_total / n_toks >= floor:
        return None
    means = sorted(((t / n_toks, g) for g, t in enumerate(totals)
                    if g not in adjacent), reverse=True)
    if not means or means[0][0] < floor:
        return None
    if len(means) > 1 and means[0][0] < means[1][0] + margin:
        return None
    return means[0][1]


def plan_windows(ids, n_frames, segs, ranges, align_fn=None,
                 slack=ANCHOR_SLACK, max_frames=int(MAX_WINDOW_S / FRAME)):
    """The DP calls to make: `[(toks, f_lo, f_hi)]`.

    `toks` lists token indices, with `None` for a separator inserted between two
    stretches that meet in one window. Three kinds of window:

    - **an anchor word's own letters**, inside its heard frames plus `slack`;
    - **the words between two anchors of one segment**, in the frames between;
    - **a gap between two segments that are neighbours in TIME**: everything
      that was said between them, in the order it was said.

    What goes into a gap is the open question, and it is settled by trial. A
    stretch of transcript between two segments may be the tail of the first
    block, the head of the next, or split between them at a word boundary; and
    where the greedy decode heard a block too badly to anchor a single phrase of
    it, the block may belong in a gap nowhere near either neighbour. So every
    stretch is aligned against every gap (`align_fn`): it is cut between its
    two neighbours' gaps where the letter scores add up best, unless `move_to`
    finds it belongs whole in some other gap. ⚠️ **The DP is the fuzzy
    matcher.** Measured on the mindfulness lecture: a 40-word block the decode
    spelt as "motivaramodo ... annalyst for h gender digonosic" found its own
    seconds this way, at 0.73 per token against 0.43 in the next best gap.

    Without `align_fn` a stretch stays with its neighbours, cut at the middle.
    🟢 Pure bookkeeping apart from the trials, so it is tested with a fake DP."""
    if not segs:
        toks = list(range(len(ids)))
        return _split_long(toks, 0, n_frames, ids, max_frames) if toks else []

    windows = []
    for seg in segs:
        for k, (wi, fs, fe) in enumerate(seg):
            lo, hi = ranges[wi]
            windows.append((list(range(lo, hi + 1)), max(0, fs - slack),
                            min(n_frames, fe + slack)))
            if k + 1 < len(seg):
                nw, nfs, _ = seg[k + 1]
                between = list(range(hi + 1, ranges[nw][0]))
                if between:
                    windows.append((between, fe, nfs))

    # Gap g is the audio before the g-th segment in time order; the last gap
    # is the audio after the final segment.
    by_time = sorted(range(len(segs)), key=lambda s: segs[s][0][1])
    time_pos = {s: pos for pos, s in enumerate(by_time)}
    gaps = []
    for pos in range(len(by_time) + 1):
        f_lo = segs[by_time[pos - 1]][-1][2] if pos else 0
        f_hi = segs[by_time[pos]][0][1] if pos < len(by_time) else n_frames
        gaps.append((f_lo, f_hi))
    pieces = [[] for _ in gaps]        # (order key, toks): tails, blocks, heads
    TAIL, HEAD = (0, 0), (2, 0)

    def trial(toks, f_lo, f_hi):
        """`(start_frame, score)` per token, zeros where nothing can be tried."""
        if align_fn is None or f_hi <= f_lo:
            return [(f_lo, 0.0)] * len(toks)
        out = []
        for part, a, b in _split_long(toks, f_lo, f_hi, ids, max_frames):
            res = align_fn(a, b, [ids[k] if k is not None else SEP for k in part])
            out += ([(s, sc) for s, _, sc in res] if res is not None
                    else [(a, 0.0)] * len(part))
        return out

    stretches = []                     # (segment before, segment after, toks)
    first_lo = ranges[segs[0][0][0]][0]
    if first_lo:
        stretches.append((None, 0, list(range(0, first_lo))))
    for s in range(len(segs) - 1):
        lo = ranges[segs[s][-1][0]][1] + 1
        hi = ranges[segs[s + 1][0][0]][0]
        if hi > lo:
            stretches.append((s, s + 1, list(range(lo, hi))))
    last_hi = ranges[segs[-1][-1][0]][1]
    if last_hi + 1 < len(ids):
        stretches.append((len(segs) - 1, None,
                          list(range(last_hi + 1, len(ids)))))

    for s_prev, s_next, toks in stretches:
        ga = time_pos[s_prev] + 1 if s_prev is not None else None
        gb = time_pos[s_next] if s_next is not None else None
        if ga is not None and ga == gb:
            pieces[ga].append(((1, 0), toks))       # neighbours in time too
            continue
        trials = [trial(toks, f_lo, f_hi) for f_lo, f_hi in gaps]
        totals = [sum(sc for _, sc in tr) for tr in trials]
        if ga is None or gb is None:
            cut = (len(toks), len(toks)) if gb is None else (0, 0)
            best = totals[ga if gb is None else gb]
        else:
            cut, best = None, None
            for c in ([0] + [c for c in range(len(toks)) if ids[toks[c]] == SEP]
                      + [len(toks)]):
                head_from = c + 1 if c < len(toks) and ids[toks[c]] == SEP else c
                total = (sum(sc for _, sc in trials[ga][:c])
                         + sum(sc for _, sc in trials[gb][head_from:]))
                if best is None or total > best:
                    cut, best = (c, head_from), total
        moved = move_to(len(toks), best, totals, (ga, gb))
        if moved is not None:
            pieces[moved].append(((1, trials[moved][0][0]), toks))
            continue
        c, head_from = cut
        if toks[:c]:
            pieces[ga].append((TAIL, toks[:c]))
        if toks[head_from:]:
            pieces[gb].append((HEAD, toks[head_from:]))

    for g, (f_lo, f_hi) in enumerate(gaps):
        parts = [toks for _, toks in sorted(pieces[g], key=lambda p: p[0])]
        if not parts:
            continue
        toks = list(parts[0])
        for part in parts[1:]:
            toks += [None] + part
        windows.append((toks, f_lo, f_hi))

    out = []
    for toks, f_lo, f_hi in windows:
        out.extend(_split_long(toks, max(0, f_lo), min(n_frames, f_hi), ids,
                               max_frames))
    return out


def spread(n, f_lo, f_hi):
    """No room to align: the tokens spaced evenly across the frames, score 0."""
    step = max(0.0, (f_hi - f_lo) / float(max(1, n)))
    return [(f_lo + k * step, f_lo + (k + 1) * step, 0.0) for k in range(n)]


def place_tokens(ids, windows, align_fn):
    """Every token's `(start_frame, end_frame, score)` from the window results.

    `align_fn(f_lo, f_hi, sub_ids)` returns `[(start, end, score)]` relative to
    nothing (absolute frames), one per token, or `None` when the window has
    fewer frames than the CTC needs (a token per frame, plus one between
    repeated letters), in which case the tokens are `spread`. A token no window
    covers keeps `None`. 🟢 The DP itself is injected so the whole placement is
    testable with a fake."""
    spans = [None] * len(ids)
    for toks, f_lo, f_hi in windows:
        sub = [ids[k] if k is not None else SEP for k in toks]
        res = align_fn(f_lo, f_hi, sub) if sub else []
        if res is None:
            res = spread(len(sub), f_lo, f_hi)
        if len(res) != len(sub):
            raise RuntimeError("the aligner returned %d spans for %d tokens"
                               % (len(res), len(sub)))
        for k, span in zip(toks, res):
            if k is not None:
                spans[k] = span
    return spans


def word_times(spans, owner, n_words):
    """Per word: `[index, start_s, end_s, score]`, score the mean of its letters.

    A separator's frames belong to nobody. A word whose tokens were never
    placed (none, in practice: every window is aligned or spread) is left out
    rather than given a zero.
    """
    timed = {}
    for k, o in enumerate(owner):
        if o < 0 or spans[k] is None:
            continue
        a, b, s = spans[k]
        if o in timed:
            timed[o][1] = b
            timed[o][2].append(s)
        else:
            timed[o] = [a, b, [s]]
    out = []
    for i in sorted(timed):
        a, b, scores = timed[i]
        out.append([i, round(a * FRAME, 3), round(b * FRAME, 3),
                    round(sum(scores) / len(scores), 3)])
    return out


# --------------------------------------------------------------------------
# What `captions` does with the result
# --------------------------------------------------------------------------

def split_by_clip(timed, clip_seconds):
    """A concatenated timeline's words handed back to their clips.

    `clip_seconds` is each clip's length in the order the wavs were joined. A
    word belongs to the clip holding its START, and its end is clamped to that
    clip's end: measured on a 13-clip package, 2 words of 3,356 straddled a
    boundary, both by under a tenth of a second. Returns
    `[[index, start, end, score, clip]]` with times local to the clip.

    🔴 **Each word is looked up by its own start, never by a cursor that only
    moves forward.** The transcript's order and the timeline's order are not
    the same thing: a slide recorded in pieces can play its last-listed file
    FIRST (measured 2026-09-15 on a lecture where a six-file slide opened with
    its sixth file), so the words of one slide run 966 s, then 688 s onwards.
    The aligner places every one of them correctly, and a forward-only cursor
    then handed all 641 to the last clip with negative local starts, leaving
    twelve correctly aligned clips with no words at all.
    """
    bounds = [0.0]
    for s in clip_seconds:
        bounds.append(bounds[-1] + s)
    last = max(len(clip_seconds) - 1, 0)
    out = []
    for i, a, b, score in timed:
        k = min(max(bisect.bisect_right(bounds, a + 1e-6) - 1, 0), last)
        lo, hi = bounds[k], bounds[k + 1]
        out.append([i, round(a - lo, 3), round(min(b, hi) - lo, 3), score, k])
    return out


def unconfirmed_regions(timed, anchored=None, floor=CONFIRM_FLOOR,
                        run=REGION_WORDS, window=WINDOW_WORDS):
    """The stretches of transcript the audio does not confirm, by both rules.

    Returns `[(first_index, last_index)]` over TRANSCRIPT indices, merged and
    in order: the runs of `low_runs` (a paragraph the audio does not contain)
    and the windows of `suspect_regions` (a passage of ordinary words spread
    over speech that is not it). A single swallowed word is neither.
    `anchored` is the response's `anchor_words`; without it the window rule
    keeps only the path that needs no anchors.
    """
    marked = set()
    for a, b in low_runs(timed, floor, run):
        marked.update(range(a, b + 1))
    # The window rule sees only what the run rule left. A window that could
    # straddle a run would borrow the run's low words and reach the threshold
    # on genuine speech beside it (measured: up to 38 confirmed words blanked
    # around one run); filtered out, the runs break the index sequence and no
    # window crosses one.
    rest = [t for t in timed if t[0] not in marked]
    for a, b in suspect_regions(rest, anchored, floor, window):
        marked.update(range(a, b + 1))
    out = []
    for i in sorted(marked):
        if out and out[-1][1] == i - 1:
            out[-1][1] = i
        else:
            out.append([i, i])
    return [(a, b) for a, b in out]


def low_runs(timed, floor=CONFIRM_FLOOR, run=REGION_WORDS):
    """Runs of `run` or more consecutive words scoring under `floor`.

    Returns `[(first_index, last_index)]` over TRANSCRIPT indices. A single
    swallowed word is not a region; a paragraph the audio does not contain is.
    """
    out, start, prev = [], None, None
    for i, a, b, score in timed:
        low = score < floor
        contiguous = prev is not None and i == prev + 1
        if low and start is not None and contiguous:
            pass
        elif low:
            if start is not None and prev - start + 1 >= run:
                out.append((start, prev))
            start = i
        else:
            if start is not None and prev - start + 1 >= run:
                out.append((start, prev))
            start = None
        prev = i
    if start is not None and prev - start + 1 >= run:
        out.append((start, prev))
    return out


def suspect_regions(timed, anchored=None, floor=CONFIRM_FLOOR,
                    window=WINDOW_WORDS, low_words=WINDOW_LOW,
                    low_unanchored=WINDOW_LOW_UNANCHORED,
                    anchors=WINDOW_ANCHORS):
    """Stretches where some `window` consecutive words are mostly not what the
    audio says, as `[(first_index, last_index)]`, each trimmed to its own low
    words so a region never opens or closes on a word the audio confirms.

    A window is suspect when `low_words` or more of its words score under
    `floor`, or when `low_unanchored` or more do AND the audio anchored no
    more than `anchors` of them verbatim (`anchored` is the response's
    `anchor_words`; `None` switches that second path off). Every window of a
    wrong passage answers the second question with "none": its anchors are
    the chance "of the" that any passage shares with any speech.

    🔴 **The margins are measured, not reasoned** (2026-09-16, every child
    development recording, 29 lectures and 55,849 words, against the
    aligner's own per-word scores, plus two mindfulness alignments for the
    first path). In 24-word windows outside the run rule's regions and the
    one known wrong passage, genuine speech reached at most 11 low words
    (a diagnosis stretch with 10 anchors), and at most 6 low words in any
    window with 3 or fewer anchors; with 4 anchors it reached 10 (numbers
    read aloud: "a 140 over 90 ... a 130 over 80"). The wrong passage, 125
    words, gave 102 windows with 10 to 19 low words and 0 anchors, every one,
    and the rule marks all 125. So 14 sits three words above the worst
    genuine window, and 10-with-2 sits four low words and two anchors above
    it. A window straddling a wrong passage's edge can cover a few confirmed
    words beyond it; the trim gives those back unless a swallowed word of
    their own sits among them, which is the same edge the run rule has always
    had.
    """
    idx = [t[0] for t in timed]
    low = [t[3] < floor for t in timed]
    anchored = None if anchored is None else set(anchored)
    pinned = [anchored is not None and i in anchored for i in idx]
    covered = [False] * len(timed)
    for s in range(0, len(timed) - window + 1):
        # A hole in the timeline (a word never placed) is not one stretch.
        if idx[s + window - 1] != idx[s] + window - 1:
            continue
        n_low = sum(low[s:s + window])
        bad = n_low >= low_words or (
            anchored is not None and n_low >= low_unanchored
            and sum(pinned[s:s + window]) <= anchors)
        if bad:
            for k in range(s, s + window):
                covered[k] = True
    out, k = [], 0
    while k < len(timed):
        if not covered[k]:
            k += 1
            continue
        j = k
        while j + 1 < len(timed) and covered[j + 1] and idx[j + 1] == idx[j] + 1:
            j += 1
        lows = [m for m in range(k, j + 1) if low[m]]
        if lows:
            out.append((idx[lows[0]], idx[lows[-1]]))
        k = j + 1
    return out


def summarise(timed, n_words, anchors, regions):
    """The numbers a sidecar carries about one clip, from the timed words."""
    confirmed = sum(1 for _, _, _, s in timed if s >= CONFIRM_FLOOR)
    region_words = sum(b - a + 1 for a, b in regions)
    region_seconds = 0.0
    by_index = {t[0]: t for t in timed}
    for a, b in regions:
        region_seconds += max(0.0, by_index[b][2] - by_index[a][1])
    return {
        "words": n_words,
        "timed": len(timed),
        "confirmed": confirmed,
        "confirmed_share": round(confirmed / float(n_words), 3) if n_words else 0.0,
        "anchors": anchors,
        "anchored_share": round(anchors / float(n_words), 3) if n_words else 0.0,
        "unconfirmed": {"regions": len(regions), "words": region_words,
                        "of_words": n_words,
                        "seconds": round(region_seconds, 2),
                        "share": (round(region_words / float(n_words), 3)
                                  if n_words else 0.0)},
    }


# --------------------------------------------------------------------------
# Finding the interpreter and running the torch half
# --------------------------------------------------------------------------

VENV = os.path.join("~", ".kcl-study", "captions-venv", "bin", "python")


def find_python():
    """An interpreter that can import `torchaudio`.

    🔴 **No hard-coded home directory in a path a recipient would hit**, the
    same rule as `captions.find_python`: `STUDY_HUB_ALIGN_PYTHON` wins; then the
    venv this project keeps OUTSIDE synced folders (`~/.kcl-study/captions-venv`, so a
    gigabyte of torch never syncs); then this interpreter; then any virtualenv
    beside the projects folder. ⚠️ **Never set a cache variable for the model
    and never pass a download root**: `torch.hub` keeps one copy per machine.
    """
    named = os.environ.get("STUDY_HUB_ALIGN_PYTHON")
    if named and os.path.exists(named):
        return named
    venv = os.path.expanduser(VENV)
    if os.path.exists(venv):
        return venv
    try:
        import torchaudio  # noqa: F401
        return sys.executable
    except ImportError:
        pass
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    parent = os.path.dirname(here)
    for cand in sorted(glob.glob(os.path.join(parent, "*", "*", ".venv", "bin", "python"))
                       + glob.glob(os.path.join(parent, "*", ".venv", "bin", "python"))):
        probe = subprocess.run([cand, "-c", "import torchaudio"], capture_output=True)
        if probe.returncode == 0:
            return cand
    return None


def run(wavs, words, python_bin=None, emissions_dir=None, log=None):
    """Align `words` to `wavs` (16 kHz mono, in play order) in a subprocess.

    Returns the response dict: `words` as `[[index, start, end, score]]` on the
    concatenated timeline, `clip_seconds` per wav, `heard`, `anchors`, and the
    seconds each stage took. The request and response go through files rather
    than pipes, because a response for a long lecture is a few hundred kilobytes
    and a pipe deadlock would look exactly like a slow model.
    """
    python_bin = python_bin or find_python()
    if not python_bin:
        raise RuntimeError("no interpreter here can import torchaudio; set "
                           "STUDY_HUB_ALIGN_PYTHON or build ~/.kcl-study/captions-venv")
    import tempfile
    with tempfile.TemporaryDirectory(prefix="study-hub-align-") as tmp:
        req = os.path.join(tmp, "request.json")
        resp = os.path.join(tmp, "response.json")
        with open(req, "w", encoding="utf-8") as fh:
            json.dump({"wavs": [os.path.abspath(w) for w in wavs],
                       "words": list(words),
                       "emissions_dir": emissions_dir}, fh)
        r = subprocess.run([python_bin, os.path.abspath(__file__), req, resp],
                           capture_output=True, text=True)
        if r.returncode != 0 or not os.path.exists(resp):
            raise RuntimeError("the aligner failed: " + (r.stderr or "")[-600:])
        if log:
            for line in (r.stderr or "").splitlines():
                log("  " + line)
        with open(resp, encoding="utf-8") as fh:
            return json.load(fh)


# --------------------------------------------------------------------------
# -- the torch half --
# Nothing below is imported by the ordinary interpreter; it runs as __main__ in
# the venv `find_python` names.
# --------------------------------------------------------------------------

_MODEL = None


def load_model():
    global _MODEL
    if _MODEL is None:
        from torchaudio.pipelines import WAV2VEC2_ASR_BASE_960H as bundle
        labels = "".join(bundle.get_labels())
        if labels != LABELS:
            raise RuntimeError("the model's alphabet is %r, this file expects %r"
                               % (labels, LABELS))
        _MODEL = bundle.get_model()
        _MODEL.eval()
    return _MODEL


def warm():
    """Fetch the model once, so the first build does not pay for it, and say
    where it landed and how big it is. The install's last step; run under the
    engine's own interpreter (`install_captions` does), never this one.

    Returns the dict it prints: `bundle`, `checkpoint` (the file torch.hub keeps,
    or None if the bundle did not say), `bytes` (its size on disk, or None).
    """
    import torch
    from torchaudio.pipelines import WAV2VEC2_ASR_BASE_960H as bundle
    load_model()
    path = getattr(bundle, "_path", None)
    checkpoint = (os.path.join(torch.hub.get_dir(), "checkpoints", path)
                  if path else None)
    size = None
    if checkpoint and os.path.exists(checkpoint):
        size = os.path.getsize(checkpoint)
    out = {"bundle": "WAV2VEC2_ASR_BASE_960H", "checkpoint": checkpoint, "bytes": size}
    print(json.dumps(out, sort_keys=True))
    return out


def read_wav(path):
    """A 16 kHz mono PCM wav as a float tensor. Anything else is refused: the
    frame arithmetic in this file assumes the rate, and resampling silently
    would make every time wrong by the ratio."""
    import wave
    import numpy as np
    import torch
    with wave.open(path) as w:
        if w.getframerate() != SAMPLE_RATE or w.getnchannels() != 1:
            raise RuntimeError("%s is %d Hz, %d channel(s); the aligner needs "
                               "16000 Hz mono" % (path, w.getframerate(),
                                                  w.getnchannels()))
        data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    return torch.from_numpy(data.astype(np.float32) / 32768.0)


def emissions(model, wav, chunk=CHUNK_S * SAMPLE_RATE, ctx=CONTEXT_S * SAMPLE_RATE):
    """Log-probabilities per frame for the whole file, in chunks with context
    on both sides, the context trimmed off so a chunk edge never lands inside
    a word."""
    import torch
    outs = []
    n = len(wav)
    with torch.inference_mode():
        for start in range(0, n, chunk):
            lo = max(0, start - ctx)
            hi = min(n, start + chunk + ctx)
            em, _ = model(wav[lo:hi].unsqueeze(0))
            em = torch.log_softmax(em[0], dim=-1)
            f_lo = (start - lo) // FRAME_SAMPLES
            f_hi = f_lo + (min(n, start + chunk) - start) // FRAME_SAMPLES
            outs.append(em[f_lo:f_hi])
    return torch.cat(outs)


def emissions_for(path, emissions_dir=None):
    """Emissions for one wav, cached as `.npy` when a directory is given.

    The cache key is the file's name and size: the same clip re-extracted is
    the same audio, and a different lecture with the same name is not."""
    import numpy as np
    import torch
    npy = None
    if emissions_dir:
        os.makedirs(emissions_dir, exist_ok=True)
        npy = os.path.join(emissions_dir, "%s.%d.em.npy"
                           % (os.path.basename(path), os.path.getsize(path)))
        if os.path.exists(npy):
            return torch.from_numpy(np.load(npy))
    em = emissions(load_model(), read_wav(path))
    if npy:
        np.save(npy, em.numpy())
    return em


def greedy_words(em):
    """The letters the model would write on its own, as `[(word, f0, f1)]`.

    Argmax per frame, repeats collapsed, blanks dropped, split at `|`. It is a
    poor transcription and a good anchor source: where it agrees with the
    transcript for four words running it is not guessing."""
    ids = em.argmax(dim=-1).tolist()
    words, cur, start, end, prev = [], [], None, None, -1
    for f, i in enumerate(ids):
        if i != prev:
            if i not in (BLANK, SEP):
                if start is None:
                    start = f
                cur.append(LABELS[i])
                end = f
            elif i == SEP and cur:
                words.append(("".join(cur).lower(), start, end + 1))
                cur, start = [], None
        elif i not in (BLANK, SEP):
            end = f
        prev = i
    if cur:
        words.append(("".join(cur).lower(), start, end + 1))
    return words


def ctc_align(em):
    """The DP as `align_fn(f_lo, f_hi, sub_ids)` over these emissions."""
    import torch
    from torchaudio.functional import forced_align, merge_tokens

    def align_fn(f_lo, f_hi, sub):
        if not sub:
            return []
        window = em[f_lo:f_hi]
        need = len(sub) + sum(1 for k in range(1, len(sub)) if sub[k] == sub[k - 1])
        if window.shape[0] < need:
            return None
        path, scores = forced_align(window.unsqueeze(0),
                                    torch.tensor([sub], dtype=torch.int32),
                                    blank=BLANK)
        spans = merge_tokens(path[0], scores[0].exp())
        return [(f_lo + s.start, f_lo + s.end, float(s.score)) for s in spans]
    return align_fn


def align_request(request):
    """The whole job for one request dict; the response dict."""
    import torch
    t0 = time.time()
    ems, clip_seconds = [], []
    for wav in request["wavs"]:
        em = emissions_for(wav, request.get("emissions_dir"))
        ems.append(em)
        clip_seconds.append(round(em.shape[0] * FRAME, 3))
    em = torch.cat(ems) if len(ems) > 1 else ems[0]
    t1 = time.time()
    words = request["words"]
    heard = greedy_words(em)
    ids, owner = tokenise(words)
    ranges = token_ranges(owner)
    anchors = find_anchors(words, heard)
    segs = segments(pins(anchors, heard, ranges))
    align_fn = ctc_align(em)
    windows = plan_windows(ids, em.shape[0], segs, ranges, align_fn)
    spans = place_tokens(ids, windows, align_fn)
    timed = word_times(spans, owner, len(words))
    t2 = time.time()
    return {"bundle": BUNDLE, "frames": int(em.shape[0]),
            "clip_seconds": clip_seconds, "heard": len(heard),
            "anchors": sum(len(s) for s in segs), "segments": len(segs),
            "segment_words": [[s[0][0], s[-1][0]] for s in segs],
            # which transcript words were heard verbatim, so a clip can say
            # what share of its words the timing actually rests on
            "anchor_words": sorted({p[0] for s in segs for p in s}),
            "windows": len(windows),
            "words": timed,
            "seconds": {"emissions": round(t1 - t0, 1),
                        "alignment": round(t2 - t1, 1)}}


def self_test():
    """The stdlib half against a lecture in miniature, no model needed."""
    words = ["In", "1998", "we", "saw", "75%", "of", "the", "sample", "improve"]
    assert spoken_tokens("1998") == ["nineteen", "ninety", "eight"]
    assert spoken_tokens("75%") == ["seventy", "five", "percent"]
    ids, owner = tokenise(words)
    ranges = token_ranges(owner)
    assert len(ranges) == len(words)
    heard = [("in", 0, 5), ("nineteen", 6, 12), ("ninety", 13, 18),
             ("eight", 19, 24), ("we", 26, 30), ("saw", 31, 36)]
    anchors = find_anchors(words, heard)
    assert anchors == [(0, 0), (1, 1), (2, 4), (3, 5)], anchors
    segs = segments(pins(anchors, heard, ranges), min_words=4)
    assert len(segs) == 1 and len(segs[0]) == 4, segs
    windows = plan_windows(ids, 200, segs, ranges)
    spans = place_tokens(ids, windows, lambda lo, hi, sub: spread(len(sub), lo, hi))
    timed = word_times(spans, owner, len(words))
    assert [t[0] for t in timed] == list(range(len(words)))
    assert unconfirmed_regions(timed, run=3) == [(0, 8)]
    print("align_ctc self-test ok: %d words, %d anchors, %d windows"
          % (len(words), sum(len(s) for s in segs), len(windows)))
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv == ["--self-test"]:
        return self_test()
    if argv == ["--warm"]:
        warm()
        return 0
    if len(argv) != 2:
        print(__doc__.split("\n\n")[0])
        print("usage: align_ctc.py <request.json> <response.json> | --self-test | --warm")
        return 2
    with open(argv[0], encoding="utf-8") as fh:
        request = json.load(fh)
    response = align_request(request)
    with open(argv[1], "w", encoding="utf-8") as fh:
        json.dump(response, fh)
    sys.stderr.write("aligned %d words on %d frames: %d heard, %d anchors in "
                     "%d segments, %d windows; emissions %.1fs, alignment %.1fs\n"
                     % (len(response["words"]), response["frames"],
                        response["heard"], response["anchors"],
                        response["segments"], response["windows"],
                        response["seconds"]["emissions"],
                        response["seconds"]["alignment"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
