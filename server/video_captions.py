#!/usr/bin/env python3
"""Captions for a plain lecture RECORDING: words we already have, timings we do not.

    python3 server/video_captions.py --list PSY101
    python3 server/video_captions.py --build PSY101 W3-T1-P1
    python3 server/video_captions.py --all PSY101
    python3 server/video_captions.py --self-test

🔴 **THERE IS NO ALIGNER IN THIS FILE AND THERE MUST NEVER BE ONE.** Everything
that decides which words are said when lives in `captions.py`, is measured, and
is reused here unchanged:

    `rough_words(paths)`  takes audio PATHS and does not care what produced them,
                          so one extracted soundtrack is a list of one
    `align(text, clips)`  is ONE global match over the transcript, and a whole
                          recording is simply ONE clip
    `cues(words, source)` is untouched

**What is new here is the AUDIO SOURCE and the plumbing**: where a recording's
sound comes from, how it is fetched without getting anybody blocked, and where
the cues land. A rewrite of the matching would be a second, unmeasured aligner
sitting beside a tested one.

🔴 **THIS NEVER RUNS ON A READER'S MACHINE**, for the same reason `captions.py`
does not: it needs a model, ffmpeg and the KCL materials. A recipient gets `.vtt`
files as data or generates their own. `build_kit.py` refuses to ship either one.

**Why a plain recording needs a different front end at all.** `captions.py
build()` reads `courses/<C>/packages/<PART>/index.html` and aligns against the
narration MP3s inside it, so **a part with no package cannot produce a `.vtt` at
all**: there is no audio on this machine. A recording's audio is inside a video
that lives on KCL's video service, and getting it is the whole job.

## The rules that came from EH and are constraints rather than preferences

*"I want to make sure you do so slowly so that you don't get blocked."*

1. **SERIAL. One lecture at a time, never in parallel.**
2. **Rate limited** to roughly playback speed, `--rate-kbits`, 1000 by default.
3. **A pause between lectures**, `--pause`, 45 seconds by default.
4. **The address is resolved immediately before fetching**, which here means the
   fetch always starts from the unsigned `playManifest` URL and lets Kaltura mint
   the signed one. **Measured 2026-09-04: the signed CDN address expires about 24
   hours out**, so a batch that resolved every address up front would go stale
   part way through its own run. Starting from the stable address costs nothing
   and cannot go stale.
5. 🔴 **A 403 or a 429 STOPS THE RUN.** No retry, no backoff, no continuing to the
   next lecture. **A retry loop is what turns a polite fetch into a blocked
   account**, and the next lecture is the same server saying the same thing.
6. **Resumable**: a lecture whose `video.vtt` is already there is skipped, so an
   interrupted run continues rather than starting again.
7. 🔴 **KEEP NOTHING BUT THE CUES.** The video and the extracted audio live in a
   temporary directory that is removed however this exits. ⚠️ **Do not keep the
   audio "because it is smaller"**: 16 kHz mono WAV for these nine lectures is
   1.24 GB, which is LARGER than the video it came from.

🟢 **A LOCAL COPY IS USED AND KEPT.** If `courses/<C>/videos/<DOC>.mp4` exists,
that is the audio source and nothing is fetched. It is EH's own opt-in download
(`fetch_videos.py`), it is not ours to delete, and using it is both faster and
politer than asking KCL for bytes already on the disk.

## Where the cues land, and why that is settled rather than chosen here

    courses/<CODE>/captions/<DOC>/video.vtt        the recording's cues
    courses/<CODE>/captions/<DOC>/soundN.vtt       the package's, unchanged
    courses/<CODE>/captions/<DOC>/captions.json    what made them, and how well

**A folder per lecture named by SOURCE**, so a lecture that has both a package
and a recording keeps every cue it owns in one place with no collision, and the
route that already serves `/captions/<PART>/...` needs no new shape.

⚠️ **`captions.py --build` deliberately has no default `--out`, and this file
does have one.** That is not a disagreement: the question that made it
defaultless was *where do caption files live*, and **EH answered it on 2026-09-02
("captions should sit locally ... they can go to my private backup")**. The
destination is settled; `captions.py` was simply written before the answer.
"""

import argparse
import glob
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import captions
import fetch_videos
import transcripts

# ~1 Mbit/s. 🔴 Bytes per second internally, kbits on the command line, because
# the instruction that produced it was about a line speed and a reader checking
# this against EH's sentence should not have to divide by eight.
RATE_KBITS = 1000
PAUSE_SECONDS = 45

# 64 KiB. Small enough that the throttle sleeps often and the average rate is
# actually held over a short window, rather than arriving in bursts with long
# stalls between them.
CHUNK = 1 << 16

# 🔴 STOPPING CODES. 403 is "not for you" and 429 is "you are asking too often";
# both are answers, and the polite response to an answer is to stop.
STOP_CODES = (403, 429)

UA = "study-hub-captions"

# 🔴 HOW MANY TIMES A DROPPED TRANSFER MAY BE CONTINUED, and it is bounded on
# purpose. This is NOT a retry against a refusal: a 403 or a 429 still stops the
# whole run dead on the first one, per EH's instruction. This is resuming a
# transfer the service never declined, from the byte it stopped at, which moves
# FEWER bytes than starting the lecture again. Measured need: `W4-T1-P1` is
# 166,817,423 bytes and truncated twice at 53 MB and 82 MB, so without this it is
# simply unobtainable.
MAX_CONTINUATIONS = 8

# 🔴 A REFUSAL THAT LEAVES NOTHING ON DISK IS INDISTINGUISHABLE FROM A LECTURE
# NOBODY HAS TRIED. `cues()` refuses below `captions.MATCH_FLOOR` and writes no
# file, which is right: plausible-looking wrong timings are worse than none. But
# the settings page then reads "missing" and invites a reader to press build, and
# rediscovering the answer for one Mindfulness lecture costs a 167 MB download.
# 🟢 So the refusal is remembered, in the lecture's own caption folder, where it
# survives the run record, a different entry point and a different machine.
# ⚠️ `build_kit.py` refuses ANY file under a `captions/` folder, so this cannot
# reach a shared kit. That rule is why the marker can live here at all.
REFUSAL_FILE = "refused.json"
HEARD_FILE = "heard.txt"
HEARD_TIMINGS = "heard.json"
CONTINUE_PAUSE = 5

# 🔴 THE SCHEMA, AND ITS LOG, LIVE IN `captions` SINCE 1.8.0 (2026-09-18),
# because the clip route writes the same file now and `captions` is the module
# this one imports. The names are re-exported here so nothing that spelled
# `video_captions.SCHEMA_VERSION` has to move.
SCHEMA_VERSION = captions.SCHEMA_VERSION

# 🔴 THE FIELDS `residecar` CANNOT RECOVER, NAMED ONCE AND CALLED TWICE. Each is
# a fact about the MATCHING, and the rough words are gone by the time an old card
# is re-carded, so a card lacking any of them must keep its old version rather
# than claim one whose field it does not have.
# ⚠️ THIS LIST IS WHY IT IS A CONSTANT. The gate and the sentence a person reads
# were separate literals, and the sentence fell a field behind TWICE -- at 1.5.0
# and again at 1.6.0. A reader told a card is held back by a field they can SEE
# in the file has been sent to look in the wrong place, so the two now derive
# from one place and a new field joins both by being added here.
UNRECOVERABLE = ("cues_anchored_on", "fabricated_time", "audio_matched_transcript")

# The two routes a lecture's words can come from: named in `captions`
# (see there), re-exported here for the same reason as the schema.
TRANSCRIPT = captions.TRANSCRIPT
HEARD = captions.HEARD

# 🔴 THE SUCCESS STATES, NAMED ONCE AND CALLED, because spelling them out at
# each call site is how a new one gets missed. It already was: `written (heard)`
# was added to `caption_course.GENERATOR_STATES` and NOT to the two literals in
# `main()`, so the first lecture that succeeded through the fallback exited 1 and
# would have been counted under "could not be done" in the batch summary.
# ⚠️ A caption run is unattended and a script reads that exit code. A success
# reported as a failure is the direction that costs somebody a night.
WROTE = (u"written", u"written (heard)")
FINISHED = WROTE + (u"kept",)

# 🔴 A SEPARATE VERSION FOR THE VERDICT, and the conflation it fixes was mine.
# A refusal marker is only trustworthy while the code that DECIDED it is
# unchanged, and I first pinned that to `SCHEMA_VERSION`. Those are different
# things: adding a counting field to the sidecar changes the file's shape and
# nothing about whether a lecture should have been refused, so bumping the schema
# would have thrown away five verdicts for no reason.
# ⚠️ Bump this when `align`, `MATCH_FLOOR` or anything else that decides
# accept-or-refuse changes. NOT when a field is added.
# 3 -> 4 (2026-09-15): the forced aligner. Its `score` is a different quantity
# (the share of the transcript's words the audio CONFIRMS, not the share of the
# recogniser's words found in the transcript), gated by the same floor, so a
# lecture Whisper refused is a lecture this engine has not yet judged.
VERDICT_VERSION = "4"


class Blocked(RuntimeError):
    """The service refused us. The RUN stops, and a person decides what next.

    🔴 Deliberately not a per-lecture failure. The next lecture is the same
    service, asked the same way, seconds later.
    """


def find_ffmpeg(named=None):
    """ffmpeg, without a hard-coded home directory: `fetch_videos.find_ffmpeg`,
    the one finder, which also knows the copy the caption-engine install
    places. The name stays here for the callers and tests that use it."""
    return fetch_videos.find_ffmpeg(named)


# --------------------------------------------------------------------------
# Which parts are plain recordings
# --------------------------------------------------------------------------

def recordings(course, root="."):
    """Every part of a course that is a recording rather than a package.

    🔴 **Structural, and from the file the SERVER actually reads.** Not a list of
    nine lectures typed into this file: `materials.json` is what
    `study_server.read_materials` derives `video_embed` from, and a part is a
    plain recording exactly when it has media and no package folder. A hard-coded
    list is right on the day it is written and silently wrong the day a course
    gains a lecture.

    ⚠️ **A part with no transcript is RETURNED, carrying `transcript: None`**,
    rather than filtered out. The caller can then say which lectures it cannot do
    and why, which is a different sentence from not mentioning them.
    """
    path = os.path.join(root, "courses", course, "materials.json")
    with open(path, encoding="utf-8") as fh:
        docs = (json.load(fh) or {}).get("docs") or {}
    mats = os.path.join(root, "materials", course)
    out = []
    for doc in sorted(docs):
        entry = docs[doc] if isinstance(docs[doc], dict) else {}
        if os.path.isdir(os.path.join(root, "courses", course, "packages", doc)):
            continue
        local = os.path.join(root, "courses", course, "videos", doc + ".mp4")
        stream = (entry.get("entry") or "").strip()
        if not os.path.isfile(local) and not stream:
            continue
        out.append({
            "doc": doc,
            "entry": stream,
            "local": local if os.path.isfile(local) else None,
            "minutes": entry.get("minutes"),
            "transcript": transcripts.transcript_for(mats, doc),
        })
    return out



# --------------------------------------------------------------------------
# Can a recording's transcript be its own narration?
# --------------------------------------------------------------------------

SECONDS_FROM_FILE = "file"
SECONDS_FROM_PAGE = "page"


def video_coverage(course, rec, root=".", probe=fetch_videos.probe_seconds):
    """`captions.coverage`, asked of a plain recording instead of a package.

    🔴 **THE QUESTION HAD NEVER BEEN ASKED OF THIS HALF OF THE LIBRARY.**
    `captions.survey` walks `packages/*/index.html`, so a course delivered as
    plain recordings is outside it entirely, and `captions.coverage` RAISES on a
    part with no package rather than answering. **79 lectures across three
    courses are video-route**, and "is this transcript long enough to be this
    lecture's speech" was answered for none of them.

    🟢 **The arithmetic is `captions.coverage_ratio` and nothing new**: same
    words, same words-a-minute constant, same ratio. **Only where the SECONDS
    come from differs**, which is the whole point of the split.

    ⚠️ **AND WHERE THEY COME FROM IS ITSELF A FINDING, so it is reported rather
    than blended away.** A local file is MEASURED with `ffprobe`. A stream-only
    lecture has no file to measure, and the only free figure is `minutes` from
    the course page, **which this project has already caught being wrong by a
    factor of three** (`W5-T2-P3` is listed at 20 minutes and runs 6). So a
    page-sourced row carries `seconds_from: "page"`, and callers say so.

    🔴 **The direction of that error matters and is stated rather than left to
    the reader: an OVERSTATED duration understates the ratio**, so a page-sourced
    row can cry wolf but cannot hide a short transcript. **False alarms, never
    false comfort.**
    """
    seconds = source = None
    if rec.get("local"):
        # 🔴 `or None` so a ZERO-length file falls through to the page rather
        # than standing as a measurement. `probe_seconds` returns None when
        # ffprobe cannot say and 0.0 for a file with no media in it, and neither
        # is a duration: without this, a truncated download reports a lecture as
        # needing no words at all, which is the direction that reads as healthy.
        seconds = probe(rec["local"]) or None
        source = SECONDS_FROM_FILE if seconds else None
    if seconds is None and rec.get("minutes"):
        try:
            seconds = float(rec["minutes"]) * 60.0
            source = SECONDS_FROM_PAGE
        except (TypeError, ValueError):
            seconds = source = None
    got = captions.coverage_ratio(rec["doc"], seconds, rec.get("transcript"))
    if got is not None:
        got["seconds_from"] = source
        got["course"] = course
    return got


def video_courses(root="."):
    """Every course with a `materials.json`, which is what `recordings` reads."""
    return sorted(os.path.basename(os.path.dirname(p)) for p in
                  glob.glob(os.path.join(root, "courses", "*", "materials.json")))


def video_survey(root=".", course=None, probe=fetch_videos.probe_seconds, out=print):
    """Every plain recording, worst first, and what could not be asked at all.

    🟢 **A lecture that cannot be measured is PRINTED rather than dropped**, with
    the reason. A survey that silently skips what it cannot read reports a clean
    library and a small denominator, which is this project's own most expensive
    shape: **a pass that fast has not looked.**
    """
    rows, cannot = [], []
    for code in ([course] if course else video_courses(root)):
        for rec in recordings(code, root):
            got = video_coverage(code, rec, root, probe)
            if got is None:
                cannot.append((code, rec["doc"],
                               "no transcript" if not rec.get("transcript")
                               else "no duration: not downloaded and the page gives no minutes"))
            else:
                rows.append(got)
    rows.sort(key=lambda r: r["ratio"])
    out("%-9s %-11s %8s %8s %8s %8s  %s"
        % ("course", "part", "audio", "words", "needs", "ratio", "from"))
    for r in rows:
        out("%-9s %-11s %7.0fs %8d %8.0f %8.2f  %-5s %s"
            % (r["course"], r["part"], r["seconds"], r["words"], r["expected"],
               r["ratio"], r["seconds_from"],
               "🔴 cannot be the narration" if r["ratio"] < captions.COVERAGE_FLOOR else ""))
    short = [r for r in rows if r["ratio"] < captions.COVERAGE_FLOOR]
    measured = [r for r in rows if r["seconds_from"] == SECONDS_FROM_FILE]
    # 🟢 A POSITIVE RESULT, printed when the answer is none. A check that is
    # silent on success cannot be told from one that never ran.
    out("\n%d of %d recordings have a transcript too short to be their own narration."
        % (len(short), len(rows)))
    out("%d of those %d durations were MEASURED from the file; %d rest on the "
        "course page's own figure, which has been wrong by a factor of three."
        % (len(measured), len(rows), len(rows) - len(measured)))
    # 🔴🔴 THE CONFOUND, DETECTED RATHER THAN LEFT TO THE READER. If every
    # measured row is in one course and every page-sourced row in others, then
    # "measured against page" and "this course against that one" are the SAME
    # split, and no row of this table can tell them apart. **Saying "the page
    # figures cause the flags" on that data would be evidence of existence read
    # as evidence of cause**, which is the mistake this project logs most often.
    if short:
        page_short = [r for r in short if r["seconds_from"] != SECONDS_FROM_FILE]
        out("⚠️ %d of the %d flagged rest on the page's figure rather than the file."
            % (len(page_short), len(short)))
        mc = {r["course"] for r in measured}
        pc = {r["course"] for r in rows if r["seconds_from"] != SECONDS_FROM_FILE}
        if mc and pc and not (mc & pc):
            out("🔴 CONFOUNDED, so do not read a cause here: every measured row is "
                "in %s and every page-sourced row is in %s, so 'measured against "
                "page' and 'one course against another' are the same split. "
                "Confirming a flag costs one download of that lecture."
                % (", ".join(sorted(mc)), ", ".join(sorted(pc))))
    if cannot:
        out("🔴 %d recording(s) could not be asked at all:" % len(cannot))
        for code, doc, why in cannot:
            out("    %s %s: %s" % (code, doc, why))
    out("⚠️ A ratio above the floor is not a promise: it rules lectures OUT only.")
    return 0


def caption_dir(course, doc, root=".", out_dir=None):
    return os.path.join(out_dir or os.path.join(root, "courses", course, "captions"), doc)


# --------------------------------------------------------------------------
# Getting the sound, politely
# --------------------------------------------------------------------------

def throttled_copy(src, dest, rate, chunk=CHUNK, now=time.monotonic,
                   sleep=time.sleep, on_chunk=None):
    """Copy `src` into `dest` at no more than `rate` bytes a second.

    An average over the whole transfer rather than a per-chunk delay: after
    writing `n` bytes the copy is entitled to have taken `n / rate` seconds, and
    it sleeps off whatever it has not yet spent. **A fast first megabyte is
    therefore paid back immediately**, and the rate cannot be beaten by a server
    that hands over a burst.

    🟢 `now` and `sleep` are arguments so a test can prove the arithmetic without
    spending the seconds. **A rate limiter nobody has watched hold a rate is a
    sleep call**, which is the class of unwitnessed guard this project keeps
    filing entries about.
    """
    started = now()
    total = 0
    while True:
        buf = src.read(chunk)
        if not buf:
            break
        dest.write(buf)
        total += len(buf)
        if on_chunk:
            on_chunk(total)
        if rate:
            owed = total / float(rate) - (now() - started)
            if owed > 0:
                sleep(owed)
    return total


def refusal_path(dest):
    return os.path.join(dest, REFUSAL_FILE)


def read_refusal(dest):
    """What an earlier run decided about this lecture, or `{}`."""
    try:
        with open(refusal_path(dest), encoding="utf-8") as fh:
            got = json.load(fh)
        return got if isinstance(got, dict) else {}
    except (OSError, ValueError):
        return {}


def heard_path(dest):
    return os.path.join(dest, HEARD_FILE)


def heard_timings_path(dest):
    return os.path.join(dest, HEARD_TIMINGS)


def write_refusal(dest, doc, entry, score, when=None, heard=None,
                  heard_key=None, heard_duration=None, reason=None,
                  engine=captions.ENGINE_WHISPER):
    """Why this lecture has no captions, in words a reader can act on.

    🔴 **`heard` IS THE WHOLE DIAGNOSIS AND IT USED TO BE THROWN AWAY.** A
    refusal means the audio did not match the transcript, and there are exactly
    two causes: the transcript is wrong for this recording, or the audio is too
    hard to make out. **Every number we have is consistent with both**, and one
    page of what the model actually heard separates them in a glance:

        coherent English that is simply not what the transcript says
            -> the TRANSCRIPT is the fault, and a bigger model buys nothing
        mush
            -> the AUDIO is hard, and a bigger model is exactly the purchase

    ⚠️ **The rough words were discarded on refusal**, so answering that question
    for a lecture already refused costs its download again. Keeping them makes
    every future refusal self-diagnosing for a few KB. The manager proposed the
    test; that it was not free is a fact about this code, now fixed.
    """
    os.makedirs(dest, exist_ok=True)
    if heard:
        # ⚠️ A rough word is `{"w": " word", "s":, "e":, "p":}` and the LEADING
        # SPACE is already in `w`, so this joins with nothing. Joining on a space
        # doubles every gap, which is exactly the sort of thing that makes a page
        # of transcription look like mush when it is not.
        with open(heard_path(dest), "w", encoding="utf-8") as fh:
            fh.write("".join(w.get("w", "") for w in heard).strip() + "\n")
        # 🔴 AND THE TIMINGS, which `heard.txt` throws away. The words alone
        # answer "is the audio mush or is the transcript wrong"; they cannot
        # answer anything about the CUES, because a cue is a word plus a time.
        # Banking these is what makes re-aligning a refused lecture free: it is
        # the same shape `--cache` reads, so a re-run never touches the network.
        # 🔴 KEYED BY THE CLIP NAME, because that is the shape `--cache` reads
        # and anything else is a new format wearing a cache's clothes. A test
        # round-trips it through `--cache` rather than trusting this comment.
        with open(heard_timings_path(dest), "w", encoding="utf-8") as fh:
            json.dump({(heard_key or doc): {"duration": heard_duration,
                                            "words": heard}}, fh)
            fh.write("\n")
    pct = int(round(score * 100))
    floor = int(round(captions.MATCH_FLOOR * 100))
    rec = {
        "doc": doc,
        # 🔴 WHICH ALIGNER SAID SO. A refusal is a VERDICT, and a verdict is only
        # worth keeping while the thing that produced it is unchanged. Learned by
        # creating the bug: the score/timing split at schema 1.2.0 admitted a
        # lecture these markers still described as permanently refused, telling a
        # reader "it will refuse again" about a lecture that now passes.
        "verdict_version": VERDICT_VERSION,
        "engine": engine,
        "kaltura_entry": entry or "",
        "heard_word_match": round(score, 3),
        "floor": captions.MATCH_FLOOR,
        "when": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                              time.gmtime(when if when is not None else time.time())),
        # 🔴 Name what a reader can DO about it, and be honest that a retry
        # costs the download again rather than pretending it is free.
        "reason": reason or (
            "the audio matched %d%% of this lecture's transcript, under "
            "the %d%% needed to place captions, so timing it would be "
            "guesswork. Building it again re-downloads the recording and "
            "will refuse again unless the transcript changes." % (pct, floor)),
    }
    with open(refusal_path(dest), "w", encoding="utf-8") as fh:
        json.dump(rec, fh, indent=2, sort_keys=True)
        fh.write("\n")
    return rec


def clear_refusal(dest):
    """A lecture that now HAS captions is not a refused one. Returns whether
    there was anything to clear."""
    gone = False
    for path in (refusal_path(dest), heard_path(dest),
                 heard_timings_path(dest)):
        try:
            os.remove(path)
            gone = True
        except OSError:
            pass
    return gone


def content_range_total(header):
    """The full size out of a `Content-Range: bytes 0-0/12345`, or None."""
    if not header or "/" not in header:
        return None
    tail = header.rsplit("/", 1)[-1].strip()
    return int(tail) if tail.isdigit() else None


def fetch_recording(entry, dest, rate, opener=urllib.request.urlopen,
                    now=time.monotonic, sleep=time.sleep, on_chunk=None,
                    attempts=MAX_CONTINUATIONS):
    """One recording onto disk, at a polite rate, stopping dead if refused.

    🔴 **The address is built here and used immediately**, never resolved in
    advance for a batch: `fetch_videos.stream_url` returns the unsigned
    `playManifest` address, and the redirect at fetch time mints a signed CDN URL
    good for about a day. **Resolving nine of those up front and then spending an
    hour on the first is how a batch dies half way through.** 🟢 A happy
    consequence for the resume below: each continuation re-resolves, so a
    signature cannot expire underneath a long transfer.

    🔴🔴 **A SHORT READ IS NOT AN END, AND UNTIL 2026-09-04 IT WAS.**
    `throttled_copy` stops on the first empty read, which is exactly what a
    dropped connection produces, so a truncated download and a complete one were
    the same event. Two of EH's lectures were captioned to a third of their length
    with nothing red anywhere. This now does two separate things about it:

    1. **Compares what arrived against what the server said it was sending**, and
       treats a mismatch as a failure. The declared size is the only witness that
       is not downstream of the fault: the status was 200, the `ftyp` box is at
       the START of a file so a short MP4 is still an MP4, and the sidecar's own
       health check read 614.2s of a 613.8s recording because both numbers came
       from the same short file.
    2. **RESUMES from the byte it stopped at**, because the service honours byte
       ranges. ⚠️ **This is not a retry against a refusal**: a 403 or 429 still
       raises `Blocked` on the first one and stops the whole run, which is EH's
       instruction and is untouched. A dropped transfer is not the service saying
       no, and continuing it moves fewer bytes than starting the lecture again.
       Bounded at `MAX_CONTINUATIONS` so a service that drops every connection at
       the same byte cannot spin here for ever.

    ⚠️ **A server that IGNORES the range restarts the file rather than appending
    to it.** A 200 in answer to a `Range` request carries the whole body from byte
    zero, and appending that to what is already on disk produces a file that is
    longer than the real one and correct nowhere. Detected by the absence of
    `Content-Range` rather than by the status code, because the status is the
    thing a stand-in in a test is least likely to model.
    """
    total = 0
    want = None
    tag = None
    with open(dest, "w+b") as fh:
        for attempt in range(max(1, attempts)):
            headers = {"User-Agent": UA}
            if total:
                headers["Range"] = "bytes=%d-" % total
                # 🔴 `If-Range` makes the SERVER refuse a stale resume: it answers
                # 206 only if the file is byte-for-byte the one we started, and
                # 200 with the whole body if it is not. That is the case a size
                # comparison cannot see, because a different rendition of the same
                # lecture can be exactly as long. Measured: KCL's CDN does send an
                # ETag (a 32-part multipart tag for one lecture), so
                # this is a real mechanism here and not a hopeful header.
                if tag:
                    headers["If-Range"] = tag
            req = urllib.request.Request(fetch_videos.stream_url(entry),
                                         headers=headers)
            try:
                response = opener(req, timeout=60)
            except urllib.error.HTTPError as e:
                # Nothing here ever reads an error body, and an HTTPError left
                # open warns at collection time, which puts noise in every run.
                e.close()
                if e.code in STOP_CODES:
                    raise Blocked("the video service answered %d for %s. Stopping "
                                  "the run rather than asking again: that is the "
                                  "answer, and a retry loop is what gets an "
                                  "account blocked." % (e.code, entry))
                if e.code == 416 and want is not None and total >= want:
                    break          # asked past the end of a file already complete
                raise
            with response as r:
                head = getattr(r, "headers", None) or {}
                ranged = content_range_total(head.get("Content-Range"))
                seen = head.get("ETag") or head.get("Last-Modified")

                # 🔴🔴 DID THE FILE CHANGE UNDERNEATH THE RESUME? Each attempt
                # re-resolves through `playManifest`, and nothing guarantees the
                # redirect lands on the same RENDITION twice. Splicing byte
                # 82,000,000 of one encoding onto byte 0 of another produces a
                # file that can be exactly the right LENGTH and a different video
                # in the middle, which the size check at the bottom would wave
                # through. Two witnesses, because one of them cannot see it: a
                # different ETag, and a different declared total. **A rendition of
                # the same lecture can be exactly as long**, which is why the tag
                # is checked at all. Measured: KCL's CDN does send an ETag.
                changed = bool(total) and (
                    (seen and tag and seen != tag)
                    or (ranged is not None and want is not None and ranged != want))
                if changed:
                    # 🔴 DISCARD THIS BODY. It was asked for from the OLD offset,
                    # so it starts in the middle of the new file; writing it at
                    # position zero produces something that is not even an MP4.
                    # Found by a test, having written exactly that bug first.
                    fh.seek(0)
                    fh.truncate()
                    total, want, tag = 0, None, None
                    continue

                if tag is None:
                    tag = seen
                if want is None:
                    declared = head.get("Content-Length")
                    if ranged is not None:
                        want = ranged
                    elif declared is not None and str(declared).isdigit():
                        want = int(declared)
                if total and ranged is None:
                    # The server ignored the range and is sending the whole file
                    # from byte zero, so starting the file again is correct here
                    # and appending would make it longer than the real one.
                    fh.seek(0)
                    fh.truncate()
                    total = 0
                got = throttled_copy(r, fh, rate, now=now, sleep=sleep,
                                     on_chunk=on_chunk)
            total += got
            if want is None or total >= want:
                break
            if not got:
                break              # no progress; continuing would spin
            if attempt + 1 < attempts:
                sleep(CONTINUE_PAUSE)

    # The ftyp box sits at offset 4 in every real MP4. An error page saved under
    # an .mp4 name is the failure this catches, and `fetch_videos` already knows
    # how: one implementation, not two that can disagree.
    if not fetch_videos.looks_like_mp4(pathlib.Path(dest)):
        raise RuntimeError("%s: %d bytes arrived and they are not an MP4" % (entry, total))

    if want is not None and total != want:
        raise RuntimeError(
            "%s: the server said %d bytes and %d arrived after %d attempt(s). A "
            "truncated recording makes captions for the part that downloaded and "
            "silently none for the rest, so this is a failure rather than a short "
            "lecture." % (entry, want, total, max(1, attempts)))
    return total


def extract_audio(media, wav, ffmpeg=None):
    """The soundtrack, as 16 kHz mono PCM, which is what the model wants.

    ⚠️ **The output is a scratch file with a short life.** Sixteen-kilohertz mono
    WAV sounds small and is not: measured across these nine lectures it comes to
    1.24 GB, more than the 0.51 GB of video it was extracted from.
    """
    binary = find_ffmpeg(ffmpeg)
    if not binary:
        raise RuntimeError("no ffmpeg here; set STUDY_HUB_FFMPEG")
    r = subprocess.run([binary, "-nostdin", "-loglevel", "error", "-y",
                        "-i", str(media), "-vn", "-ac", "1", "-ar", "16000",
                        "-c:a", "pcm_s16le", str(wav)],
                       capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(wav):
        raise RuntimeError("ffmpeg could not take the sound out of %s: %s"
                           % (os.path.basename(str(media)), (r.stderr or "")[-300:]))
    return wav


# --------------------------------------------------------------------------
# One lecture, end to end
# --------------------------------------------------------------------------

def transcript_covered(text, made):
    """The fraction of the TRANSCRIPT'S words that reached a cue.

    🔴 **THE NUMBER A READER ACTUALLY WANTS, and the one the sidecar did not have
    until 2026-09-04.** It answers *"how much of this lecture has words on
    screen"*. The other number, `heard_word_match`, answers *"how much of what the
    model HEARD was found in the transcript"*, and the two are wildly different on
    a healthy run: measured on W3-T1-P1, **0.748 heard-word match against 0.978 of
    the transcript in cues**, because `base.en` mishears a good fraction of
    clinical vocabulary and those words are thrown away by design.

    ⚠️ **A field called `coverage` reporting 0.748 on an excellent run is worse
    than no field**, which is the manager's own finding about their own spec: nine
    lectures each saying 0.748 read as a broken feature.

    🟢 **Computed from the CUES' character spans, not from a word split**, so it
    needs no tokeniser of its own and cannot disagree with `cues()` about where a
    cue starts. A transcript word counts as covered when its span falls inside
    some cue's span.

    🔴🔴 **THE DENOMINATOR IS `spoken_source(text)`, NOT THE RAW PDF, and naming it
    is the whole point of this paragraph.** Two people measured this lecture on
    2026-09-04 and got 0.978 and 0.998, which looked like a disagreement and was
    not: the manager measured against the raw transcript, this measures against
    what the aligner is actually given. **`spoken_source` already removes the
    title-page furniture** (`Module: ... Week 3 ... Dr <name> ... Institute of`),
    and on W3-T1-P1 that is exactly 30 words, 1,734 down to 1,704. **Their "one
    gap of 30 words at position 0.0%" and these three uncovered words are the same
    measurement with and without the furniture in the denominator.**

    ⚠️ **So this number cannot see furniture and must not be read as if it could.**
    It answers *"of the words the aligner was given, how many reached a cue"*. On
    W3-T1-P1 the three it misses are `Week`, `3` and `6`.

    🔴 **ONLY FOR CUES THAT WERE CUT FROM `text`.** A cue's `at`/`to` are
    offsets into whatever it was cut from, and a cue read back from a `.vtt`
    (`captions.cues_from_vtt`) was cut from its own text, so its span is
    `(0, len(said))` whatever the transcript says. Fed such cues this would
    report that the first few words of the lecture are covered and nothing
    else; `captions.build`'s `whole` list mixes both kinds, and looks exactly
    like the right input. **So a cue whose span does not hold its own text is
    refused**, and the caller is pointed at `captions.cue_spans_in`, which
    finds the spans by searching. The clip route goes that way always
    (`captions.clip_sidecar`); the number itself is `captions.covered_from_spans`
    on either route, so the two cannot drift.
    """
    for c in made:
        if text[c.at:c.to].strip() != c.said:
            raise ValueError(
                "a cue whose span does not hold its own text: these cues were "
                "not cut from this transcript. Cues read back from a .vtt "
                "(captions.cues_from_vtt) carry offsets into their own text; "
                "find their spans with captions.cue_spans_in instead")
    return captions.covered_from_spans(text, [(c.at, c.to) for c in made])


def heard_confidence(entry):
    """The mean of the model's own per-word probabilities, or None.

    ⚠️ **None is a real answer and is not zero.** A model that reports no
    probabilities has told us nothing about its confidence, and a zero there
    would read as "it was sure of nothing", which is a different claim.
    """
    words = (entry or {}).get("words") or []
    ps = [w.get("p") for w in words if isinstance(w, dict) and w.get("p") is not None]
    if not ps:
        return None
    return round(sum(ps) / len(ps), 3)


def sidecar(doc, rough_name, pdf, entry, rough, clip, made, model, root=".",
            text="", heard_words=False, heard_because=None,
            engine=captions.ENGINE_WHISPER, alignment=None):
    """`captions.json`: what these cues are made of, and how well it went.

    🔴 **`coverage` is not decoration.** It is the fraction of what the recording
    says that was found in the transcript, and it is the one number that tells a
    reader looking at a thin caption track whether the track is thin or the
    lecture is quiet. **A run that went badly is visible without opening the
    `.vtt`**, which is the whole reason the file exists.
    """
    seconds = (rough.get(rough_name) or {}).get("duration") or 0.0
    return {
        "schema_version": SCHEMA_VERSION,
        "doc": doc,
        "source": "video",
        # 🔴 TWO FIELDS FROM ONE BRANCH, at schema 1.4.0, and they are derived
        # together here so they cannot come to disagree. `words_are` is what a
        # consumer BRANCHES on; `words_from` is where the words came from, which
        # is a path for the transcript route and a tool for the heard one.
        # ⚠️ A single field would have had to be a path sometimes and a sentence
        # other times, which is the "two facts in one field" shape this file has
        # already been corrected for twice (`heard_word_match`'s rename, and the
        # `cues_anchored_on` split).
        "words_are": HEARD if heard_words else TRANSCRIPT,
        "words_from": (("%s %s" % (captions.TOOL_WHISPER, model)) if heard_words
                       else (os.path.relpath(pdf, root) if pdf else None)),
        # 🔴 WHY, not just WHICH. `words_are` says a lecture fell back; this says
        # what it fell back FROM, and they are different questions. A reader
        # looking at a machine-worded lecture wants to know whether the
        # transcript was the wrong document or simply could not be timed, and
        # that answer exists only at the moment the decision is made.
        "heard_because": heard_because if heard_words else None,
        # 🔴 HOW SURE THE MODEL WAS, and on the heard route it is the number that
        # matters most, because it is the only signal separating the two things a
        # mismatch can mean. The refusal marker's own comment already names them:
        # coherent English that is simply not what the transcript says means the
        # TRANSCRIPT is wrong, and mush means the AUDIO is hard. On the transcript
        # route a low figure explains loose anchors; on the heard route it is the
        # difference between usable captions and nonsense with timings on it.
        #
        # 🟢 IT COUNTS AND DOES NOT REFUSE, which is the same call the legibility
        # work made first and for the same reason: where the line goes is EH's
        # with the manager ruling, and counting turns every future lecture into a
        # data point instead of leaving the question resting on five. A decide
        # entry carries the question.
        #
        # ⚠️ IT IS THE MODEL'S OWN CONFIDENCE, NOT AN ACCURACY. It says how sure
        # the model was, not how often it was right, and the two come apart
        # exactly where a model is confidently wrong. Named `heard_confidence`
        # rather than anything with "accuracy" in it for that reason.
        "heard_confidence": heard_confidence(rough.get(rough_name)),
        # 🔴 WHICH ENGINE TIMED THE WORDS, at schema 1.7.0. The forced aligner
        # (`captions.ENGINE_CTC`) puts the transcript's own words on the audio
        # and never interpolates; Whisper recognises first and matches after.
        # A heard track was timed by Whisper whichever engine was asked for,
        # because the heard words are Whisper's own.
        "engine": engine,
        "timings_from": (captions.timings_from(captions.ENGINE_WHISPER, model)
                         if heard_words or engine != captions.ENGINE_CTC else
                         captions.timings_from(captions.ENGINE_CTC)),
        # 🔴 THE STRETCHES THE AUDIO DOES NOT CONTAIN, counted by the forced
        # aligner and null under Whisper, which cannot see them. Those words got
        # NO cue rather than an interpolated one, so `fabricated_time` stays at
        # zero and this is the number to read instead when a track has holes.
        "unconfirmed": clip.unconfirmed,
        # How the alignment went: heard words, anchors, segments, windows. Null
        # under Whisper.
        "alignment": alignment,
        "media": {"kaltura_entry": entry or None,
                  "seconds": round(seconds, 1) or None,
                  "minutes": int(round(seconds / 60.0)) if seconds else None},
        "built_on": time.strftime("%Y-%m-%d"),
        "cues": len(made),
        # 🔴 TWO NUMBERS, BECAUSE ONE CANNOT ANSWER BOTH QUESTIONS, and the NAME is
        # what made the first reader ask the wrong one. `heard_word_match` was
        # called `coverage` until 2026-09-04; nothing consumed the file yet, so the
        # rename cost nothing and the misreading it was causing was immediate.
        # 🔴 NULL ON THE HEARD ROUTE, and that is not a missing value: there was
        # no match to score. Printing 1.0 here would say "the audio agreed
        # perfectly with the transcript", which is the opposite of what happened.
        "heard_word_match": None if heard_words else round(clip.score, 3),
        # 🔴 THE SAME QUANTITY, RECORDED WHICHEVER WORDS SHIPPED, at schema
        # 1.6.0. The line above nulls itself on the heard route and its reasoning
        # was sound for what it describes: the CUE ANCHORING really did not
        # happen, so a number there would claim a match that was never made.
        # 🔴 BUT THE SCORE ITSELF WAS ALWAYS TAKEN. `align()` runs against the
        # transcript before anything decides which door a lecture goes through,
        # and on the heard route it is the number that CHOSE the door. Nulling it
        # threw away the measurement along with the claim, and those are two
        # different things under one name.
        # ⚠️ WHAT IT COST, measured on EH's six heard lectures: four fell back on
        # READABILITY at 0.655, 0.617, 0.605 and 0.679 -- every one ABOVE
        # `MATCH_FLOOR`. That is the whole finding of the caption unit (the match
        # floor was never the story), and it survived only in a scratch directory.
        # 🟢 THE NAME PUTS THE DENOMINATOR FIRST, the same rule `transcript_covered`
        # follows: that one is "of the transcript, how much reached a cue", this
        # one is "of the audio, how much was found in the transcript". The two
        # were one field called `coverage` until 2026-09-04 and the name was the
        # defect, so the rule is written down rather than remembered.
        # ⚠️ IT DUPLICATES `heard_word_match` ON THE TRANSCRIPT ROUTE, deliberately.
        # One field that always means one thing is worth more than two fields a
        # reader has to pick between by route, which is exactly the shape that
        # produced this entry.
        "audio_matched_transcript": round(clip.score, 3),
        # 🔴 THE THIRD NUMBER, added at schema 1.2.0 with the aligner split, and
        # it is the one to read when a track looks loose. `heard_word_match` is
        # how much of the audio AGREES with the transcript; `cues_anchored_on` is
        # how much of it the timings actually REST on. They were one figure until
        # 2026-09-04, which hid the difference between a lecture that matched
        # badly and a lecture whose cues are mostly interpolated.
        # ⚠️ On EH's `W4-T1-P2` these are 0.620 and 0.505, and the four lectures
        # that passed before the split anchor 0.67 to 0.80. **A lecture that
        # clears the floor on agreement it cannot anchor is a real thing now**,
        # and this is the field that says so.
        # Same reason, and it is the stronger case: every heard cue rests on a
        # timing the model gave for that very word, so "anchored" has no
        # denominator here at all.
        "cues_anchored_on": None if heard_words else round(clip.anchored, 3),
        # 🔴 THE HARM, COUNTED, at schema 1.3.0. Every number above is a proxy for
        # whether a reader can follow the captions; this is the thing itself.
        # 🟢 IT COUNTS AND DOES NOT REFUSE. Where the line goes is EH's with the
        # manager ruling, and QA's argument for counting first is that it turns
        # every future lecture into a data point instead of leaving the question
        # resting on five. Measured across the corpus: 34 package lectures average
        # 2.15% unreadable and 32 of 34 sit under 5%.
        "legibility": captions.legibility(made),
        # 🔴 WHY THE LEGIBILITY FIGURE IS WHAT IT IS, at schema 1.5.0. Every
        # number above says how BAD a track is; this is the first one that says
        # what KIND of bad, and the two were being confused. A lecture whose
        # anchors thinned evenly and a lecture with one 521-word passage that got
        # no anchor at all produce the same `legibility`, the same
        # `cues_anchored_on` and completely different right answers.
        # 🔴 NULL ON THE HEARD ROUTE, and it is not a zero: the model's own words
        # arrive with the model's own timings, so nothing was interpolated and
        # there was no claim to fabricate. A 0.0 here would say "we interpolated
        # and got away with it", which is a different sentence.
        "fabricated_time": None if heard_words else clip.fabricated,
        "transcript_covered": transcript_covered(text, made) if text else None,
        # 🔴 WHERE THE CAPTIONS START AND STOP, in the recording's own clock, and
        # it answers a different question from `coverage`. Coverage says how much
        # of what was SAID was found in the transcript; these two say how much of
        # the LECTURE has words on screen. A track that matched well and stops
        # eight minutes in looks broken to a reader, and only these numbers make
        # that visible without playing the whole thing.
        "cues_from": round(made[0].start, 1) if made else None,
        "cues_to": round(made[-1].end, 1) if made else None,
    }


# 🔴 THE SPAN HELPERS LIVE IN `captions` SINCE 2026-09-18, because the clip
# route recovers spans from its files too and `captions` cannot import this
# module. Same names, same behaviour, one parser (`captions.cues_from_vtt`).
cue_spans_in = captions.cue_spans_in
covered_from_spans = captions.covered_from_spans


def residecar(course, root=".", out_dir=None, log=None):
    """Bring existing sidecars up to the current schema, with no network at all.

    🔴 **Written because a field's NAME was the defect.** `coverage` was
    `clip.score`, the fraction of what the model HEARD that matched the
    transcript; on a healthy lecture that is about 0.75 and it reads as a
    quarter-failed run. **The number a reader wants is how much of the transcript
    reached a cue**, which on the same lecture is about 0.98.

    🟢 **Re-fetching nine lectures to fix a field name would have been the wrong
    trade**, so this recovers what it can from disk: the old score is kept under
    its honest name, and the new number is computed from the `.vtt` itself.
    """
    log = log or (lambda *a: None)
    rows = []
    mats = os.path.join(root, "materials", course)
    for rec in recordings(course, root):
        dest = caption_dir(course, rec["doc"], root, out_dir)
        card_path = os.path.join(dest, "captions.json")
        vtt_path = os.path.join(dest, "video.vtt")
        if not (os.path.exists(card_path) and os.path.exists(vtt_path)):
            continue
        with open(card_path, encoding="utf-8") as fh:
            card = json.load(fh)
        if "coverage" in card and "heard_word_match" not in card:
            card["heard_word_match"] = card.pop("coverage")
        pdf = rec["transcript"] or transcripts.transcript_for(mats, rec["doc"])
        if pdf:
            text = captions.spoken_source(transcripts.pdf_text(pdf))
            with open(vtt_path, encoding="utf-8") as fh:
                vtt = fh.read()
            spans = cue_spans_in(text, vtt)
            card["transcript_covered"] = covered_from_spans(text, spans)
            card["cues_matched_to_transcript"] = len(spans)
        # ⚠️ `cues_anchored_on` cannot be recovered from disk: it is a fact about
        # the MATCHING, and the rough words are gone. An old sidecar re-carded
        # here keeps its version rather than claiming a field it does not have.
        # ⚠️ BOTH fields, not just the first. `fabricated_time` is a fact about
        # the MATCHING too and is equally unrecoverable from disk, so a card
        # re-carded here without it would claim schema 1.5.0 while missing the
        # field 1.5.0 is FOR. That is the same trap the line above was written
        # for, one schema version later.
        # ⚠️ EVERY field in `UNRECOVERABLE`, not a list retyped here. This is the
        # third schema running in which a card could have come forward claiming
        # the version whose field it lacks.
        # 🟢 The 1.7.0 fields ARE recoverable: before this schema only one
        # engine existed, so a card without `engine` was built by Whisper, and
        # `unconfirmed` and `alignment` are honestly null for that engine.
        if "engine" not in card:
            card["engine"] = captions.ENGINE_WHISPER
        card.setdefault("unconfirmed", None)
        card.setdefault("alignment", None)
        if all(f in card for f in UNRECOVERABLE):
            card["schema_version"] = SCHEMA_VERSION
        with open(card_path, "w", encoding="utf-8") as fh:
            json.dump(card, fh, indent=2, sort_keys=True)
            fh.write("\n")
        rows.append({"doc": rec["doc"], "heard": card.get("heard_word_match"),
                     "covered": card.get("transcript_covered"),
                     "schema_version": card.get("schema_version"),
                     "cues": card.get("cues")})
        log("%-11s heard %s  transcript covered %s"
            % (rec["doc"], card.get("heard_word_match"),
               card.get("transcript_covered")))
    return rows


def build_one(course, doc, root=".", out_dir=None, force=False,
              rate_kbits=RATE_KBITS, model=captions.PRIMARY_MODEL, cache=None,
              ffmpeg=None, log=None, opener=urllib.request.urlopen,
              now=time.monotonic, sleep=time.sleep, engine=captions.ENGINE_AUTO,
              aligner=None):
    """One recording's captions, and a row saying what happened.

    States, and each one is a different fact rather than a shade of failure:
    `kept` (already there), `no transcript`, `refused` (the words on the
    recording are not the words in the transcript), `written`.

    🔴 **A refused lecture leaves NO `.vtt`**, exactly as a refused clip does in
    `captions.py`. An empty `.vtt` is indistinguishable from a lecture nobody has
    run, and the next resumable pass would skip it for ever. **That rule is
    unchanged.**

    🟢 **It DOES leave a `refused.json`, and that reverses the retry half of the
    old decision on purpose (2026-09-04).** The reason the old rule wanted a
    retry was that a refusal could be caused by a SHORT DOWNLOAD, which is
    transient. **It cannot any more**: `fetch_recording` compares against
    `Content-Length` and raises, so a truncated fetch never reaches `cues()` and
    never produces a refusal. What is left is a complete recording whose audio
    does not match its transcript, **which no amount of retrying fixes and which
    costs 167 MB per rediscovery.** So it is remembered, `--force` retries it,
    and the marker is not a `.vtt` so nothing reads it as captions.
    """
    log = log or (lambda *a: None)
    dest = caption_dir(course, doc, root, out_dir)
    vtt = os.path.join(dest, "video.vtt")
    row = {"doc": doc, "state": None, "cues": 0, "score": None, "bytes": 0}

    if os.path.exists(vtt) and not force:
        row["state"] = "kept"
        return row

    found = [r for r in recordings(course, root) if r["doc"] == doc]
    if not found:
        row["state"] = "not a recording"
        return row
    rec = found[0]
    if not rec["transcript"]:
        row["state"] = "no transcript"
        return row

    # 🔴 AFTER the facts above, which are more specific, and BEFORE anything
    # touches the network. A remembered refusal is the whole point of the file.
    if not force and read_refusal(dest):
        row["state"] = "refused before"
        return row

    rate = int(rate_kbits) * 1000 // 8
    engine = captions.engine_for(engine)
    # 🔴 EVERYTHING BUT THE CUES LIVES IN HERE AND GOES WHEN THIS BLOCK ENDS,
    # however it ends. The alternative is a half-finished run leaving a gigabyte
    # of somebody else's lecture in the repo.
    # ⚠️ Since 2026-09-15 that includes the alignment, the gate and the heard
    # fallback, because the forced aligner needs the wav and so does a Whisper
    # run that is only made when the aligner's cues fail the gate.
    with tempfile.TemporaryDirectory(prefix="study-hub-captions-") as scratch:
        if rec["local"]:
            media = rec["local"]
            log("  using the local copy already on this machine, nothing fetched")
        else:
            media = os.path.join(scratch, doc + ".mp4")
            log("  fetching at about %d kbit/s" % rate_kbits)
            started = time.monotonic()
            row["bytes"] = fetch_recording(rec["entry"], media, rate, opener=opener,
                                          now=now, sleep=sleep)
            took = time.monotonic() - started
            # ⚠️ DECIMAL MB, and the exact byte count beside it. This line
            # divided by 1048576 and called the result "MB" until 2026-09-04,
            # which is MiB. Comparing a log line reading "21.0 MB" against a
            # server's `Content-Range` of 22,014,622 makes a COMPLETE download
            # look a megabyte short, and it briefly made every lecture in a run
            # look truncated. The exact figure is here so the comparison needs no
            # arithmetic at all.
            log("  %.1f MB (%d bytes) in %.0fs, %.0f kbit/s"
                % (row["bytes"] / 1e6, row["bytes"], took,
                   (row["bytes"] * 8 / 1000.0 / took) if took else 0))

        wav = os.path.join(scratch, doc + ".wav")
        extract_audio(media, wav, ffmpeg)
        name = os.path.basename(wav)

        # 🔴 THE TRANSCRIPT IS READ BEFORE LISTENING, NOT AFTER: the recogniser
        # is given the lecturer's own vocabulary up front so it is likelier to
        # write `mechanisms` than "magnum" and `Grabovac` than "guapurvik", and
        # the forced aligner is given nothing else at all.
        # 🟢 The same text is reused for every step below rather than read
        # twice, so the words a step is primed with and the words it is matched
        # against cannot drift apart.
        text = captions.spoken_source(transcripts.pdf_text(rec["transcript"]))
        listened = {}

        def listen():
            """Whisper's words for this recording, taken ONCE and only when a
            step needs them: on the Whisper route always, on the forced-aligner
            route only when its cues fail the gate and the heard fallback runs."""
            if listened:
                return listened["rough"]
            if cache and os.path.exists(cache):
                with open(cache, encoding="utf-8") as fh:
                    got = json.load(fh)
            else:
                primed = captions.prime_prompt(text)
                log("  listening for the timings%s"
                    % (" (primed with %d of the lecture's own words)"
                       % len(captions.prime_terms(text)) if primed else ""))
                got = captions.rough_words([wav], model=model, prompt=primed)
                if cache:
                    with open(cache, "w", encoding="utf-8") as fh:
                        json.dump(got, fh)
            listened["rough"] = got
            return got

        # 🔴 ONE CLIP, WHICH IS WHAT A RECORDING IS. Both engines were built to
        # hold many clips against one transcript; a single clip is the
        # degenerate case and the one they handle most naturally.
        alignment = None
        if engine == captions.ENGINE_CTC:
            log("  placing the transcript's words on the audio (forced alignment)")
            clip = captions.Clip(name, 0, [])
            _, resp = captions.time_by_ctc(text, [clip], [wav], log=log,
                                           aligner=aligner)
            alignment = {"heard": resp.get("heard"), "anchors": resp.get("anchors"),
                         "segments": resp.get("segments"),
                         "windows": resp.get("windows")}
            # ⚠️ A stand-in for the recogniser's record, so the sidecar and the
            # refusal marker have a duration to report; it is REPLACED by the
            # real thing the moment `listen()` runs. `heard_confidence` of an
            # entry with no words is None, which is the honest answer here.
            rough = {name: {"duration": (resp.get("clip_seconds") or [None])[0],
                            "words": []}}
        else:
            rough = listen()
            if name not in rough:
                row["state"] = "not transcribed"
                row["engine"] = engine
                return row
            clip = captions.Clip(name, 0, rough[name]["words"])
            captions.align(text, [clip])
        row["score"] = clip.score
        row["engine"] = engine

        # 🔴🔴 TWO ROUTES, AND THE SECOND ONE IS WHY EVERY LECTURE NOW ENDS WITH
        # CAPTIONS. EH, 2026-09-04: *"if we can use KCL's transcripts that would be
        # better even though they have errors in them as well to be honest with you
        # ... so let's go ahead and implement all those closed captions."*
        #
        # PREFER the lecturer's own words. Under Whisper, timing them needs the
        # model's words MATCHED against the transcript, and on a long recording
        # that match goes sparse; sparse anchors are what squeezed seventeen
        # words into one second and took five lectures down. Under the forced
        # aligner every word's time is measured and the words the audio does not
        # contain get no cue at all, so the gate below is expected to pass; it
        # still runs, because a gate that is skipped on the route that was built
        # to pass it is a gate nobody would notice failing.
        #
        # 🟢 FALL BACK to the model's own words, which arrive WITH their timings.
        # No matching, no anchors, no interpolation, so that failure mode cannot
        # occur. The cost is transcription error, and EH priced it himself: this
        # model heard "maximums" for *mechanisms*, and KCL's transcript renders the
        # same word "magnets".
        #
        # 🔴 THE GATE IS NOT RELAXED FOR THE FALLBACK. It is the reason the fallback
        # exists, so it runs on whichever route produced the cues and a heard track
        # that a reader could not follow is still refused.
        ctc = engine == captions.ENGINE_CTC
        made = captions.cues(clip.words, text,
                             split_gap=captions.RUN_SPLIT_GAP if ctc else None)
        heard_words = False
        why_fell_back = None
        if made:
            unreadable, got = captions.too_fast_to_read(made)
            if unreadable:
                # 🔴 THE SENTENCE SAYS THE CAUSE NOW, NOT ONLY THE SYMPTOM. It read
                # as the first clause alone until 2026-09-09, and a reader
                # reasonably took "ran too fast" to mean the anchors had thinned
                # evenly -- so the fix proposed for it was to let a crowded cue
                # borrow time from the gap beside it. **The measurement says
                # otherwise**: the words sit in a handful of passages that got no
                # anchor at all, where there is no gap to borrow from. The clause is
                # here because a refusal reason that names only the symptom sent
                # four days of work at the wrong mechanism.
                fab = clip.fabricated
                why_fell_back = ("%d%% of the transcript-timed captions would have run "
                                 "faster than six words a second, over the %d%% allowed"
                                 % (round(got["unreadable_share"] * 100),
                                    round(captions.LEGIBILITY_CEILING * 100)))
                if fab["words"]:
                    why_fell_back += (
                        "; %d of the transcript's %d words in this stretch (%d%%) got "
                        "their times by interpolating across %d passages that matched "
                        "nothing at all, at more than %g words a second, which is a "
                        "rate nobody speaks at"
                        % (fab["words"], fab["of_words"], round(fab["share"] * 100),
                           fab["spans"], captions.SPEECH_CEILING))
                row["legibility"] = got
                made = []
        elif ctc:
            why_fell_back = ("the audio confirmed %d%% of the transcript's words, "
                             "under the %d%% needed to place the lecturer's own words"
                             % (round((clip.score or 0) * 100),
                                round(captions.MATCH_FLOOR * 100)))
        else:
            why_fell_back = ("the audio matched %d%% of the transcript, under the %d%% "
                             "needed to place the lecturer's own words"
                             % (round((clip.score or 0) * 100),
                                round(captions.MATCH_FLOOR * 100)))

        # 🔴 A SEPARATE NAME, NOT A REBIND OF `text`. `Cue` slices its words out of
        # the source it was built from, and `transcript_covered` asks how much of the
        # TRANSCRIPT ended up on screen. Rebinding `text` here would have handed the
        # heard source to both, so the sidecar would have reported coverage of the
        # machine's own words against themselves, which is 100% and means nothing.
        cue_source = text
        if not made:
            rough = listen()
            cue_source, heard_list = captions.heard(
                (rough.get(name) or {}).get("words") or [])
            made = captions.cues(heard_list, cue_source)
            heard_words = True
        # ⚠️ What the recogniser said, or the stand-in: on the forced-aligner
        # route a lecture that passed the gate was never listened to, and the
        # refusal marker below is only reached after `listen()` has run.
        heard_entry = rough.get(name) or {"words": [], "duration": None}

    # 🔴 THE OUTPUT GATE, on the cues rather than the match. Ruled at `be6a94a`
    # after a shipped lecture was found with 43% of its cues running too fast to
    # read: captions a reader cannot follow are worse than none, because a reader
    # trusts them. `MATCH_FLOOR` is a different quantity and is untouched.
    # ⚠️ Reached here by BOTH routes, which is the point: a transcript-timed
    # track that fails it has already become a heard one above, and if the heard
    # one fails too the lecture is genuinely refused.
    # 🔴 TWO REFUSAL STATES AND THEY ARE DIFFERENT FACTS, which is this file's
    # own rule about states. `refused` means neither route produced a cue at all;
    # `too fast to read` means cues exist and a reader could not follow them. The
    # reason string carries which routes were tried either way.
    unreadable, got = (captions.too_fast_to_read(made) if made else (True, None))
    if unreadable:
        row["legibility"] = got
        write_refusal(dest, doc, rec.get("entry"), clip.score,
                      heard=heard_entry["words"], heard_key=name,
                      heard_duration=heard_entry.get("duration"),
                      engine=engine,
                      # 🔴 TWO DIFFERENT COSTS AND THE SENTENCE SAYS BOTH,
                      # because a test caught it saying only the flattering one.
                      # Pressing the button again DOES re-download; what costs
                      # nothing is re-running the alignment against the words
                      # banked beside this file, which is a developer's move and
                      # not the button's. Telling EH "re-testing costs no
                      # download" beside a button that downloads 167 MB is the
                      # kind of true-but-wrong sentence this project keeps
                      # finding.
                      reason=("neither route produced captions a reader could "
                              "follow: %s, and %s. Building it again "
                              "re-downloads the recording and will refuse again "
                              "unless the transcript or the recording changes. "
                              "The words and their timings are kept beside this "
                              "file, so re-running the alignment on them costs "
                              "no download."
                              % (why_fell_back or "the transcript route was not "
                                 "attempted",
                                 ("%d%% of the machine's own words would run too "
                                  "fast as well"
                                  % round(got["unreadable_share"] * 100)) if got
                                 else "the machine's own words produced no cues "
                                      "at all")))
        row["state"] = "too fast to read" if made else "refused"
        return row

    os.makedirs(dest, exist_ok=True)
    with open(vtt, "w", encoding="utf-8") as fh:
        fh.write(captions.to_vtt(made))
    with open(os.path.join(dest, "captions.json"), "w", encoding="utf-8") as fh:
        # ⚠️ `text` is EMPTY on the heard route, deliberately: `transcript_covered`
        # compares the cues against the transcript, and these cues do not index
        # into it. A null there is the honest answer; a number would be a lie
        # about a comparison nobody made.
        json.dump(sidecar(doc, name, rec["transcript"], rec["entry"], rough, clip,
                          made, model, root, "" if heard_words else text,
                          heard_words=heard_words, heard_because=why_fell_back,
                          engine=engine, alignment=alignment),
                  fh, indent=2, sort_keys=True)
        fh.write("\n")
    clear_refusal(dest)          # it has captions now, whatever it did before
    row["state"] = "written (heard)" if heard_words else "written"
    row["words_are"] = HEARD if heard_words else TRANSCRIPT
    row["cues"] = len(made)
    if clip.unconfirmed is not None:
        row["unconfirmed"] = clip.unconfirmed
    return row


def say(*bits):
    """Print a progress line and FLUSH it.

    🔴 **A run this long is unattended, and Python buffers stdout when it is not a
    terminal.** Measured 2026-09-04: the batch printed nothing at all for the
    first ten minutes when its output went to a file, so the only way to tell a
    working run from a hung one was to look for the `.vtt` files it had not
    written yet. **An unattended run that cannot be watched is one nobody leaves
    running.**
    """
    print(*bits)
    sys.stdout.flush()


def build_all(course, root=".", out_dir=None, force=False, rate_kbits=RATE_KBITS,
              pause=PAUSE_SECONDS, model=captions.PRIMARY_MODEL, ffmpeg=None, log=say,
              sleep=time.sleep, opener=urllib.request.urlopen, only=None,
              now=time.monotonic, engine=captions.ENGINE_AUTO):
    """Every recording in a course, one at a time, with a pause between.

    🔴 **`Blocked` ends the run and says so.** The rows already collected are
    returned rather than thrown away, so an interrupted batch still reports what
    it did before it stopped.
    """
    rows = []
    todo = [r for r in recordings(course, root) if not only or r["doc"] in only]
    for i, rec in enumerate(todo):
        log("%s (%s)" % (rec["doc"], rec["entry"] or "local copy"))
        try:
            row = build_one(course, rec["doc"], root, out_dir, force, rate_kbits,
                            model, None, ffmpeg, log, opener, now, sleep,
                            engine=engine)
        except Blocked as e:
            log("🔴 STOPPED: %s" % e)
            rows.append({"doc": rec["doc"], "state": "blocked", "cues": 0,
                         "score": None, "bytes": 0})
            return rows
        except Exception as e:                       # one failure is one fact
            log("  FAILED: %s" % e)
            row = {"doc": rec["doc"], "state": "failed: %s" % e, "cues": 0,
                   "score": None, "bytes": 0}
        rows.append(row)
        log("  %s%s" % (row["state"],
                        "" if row["cues"] == 0 else ", %d cues" % row["cues"]))
        # Only after a lecture that actually went to the network, and never after
        # the last one: a pause before finishing is a pause nobody benefits from.
        if row.get("bytes") and i + 1 < len(todo) and pause:
            log("  pausing %ds before the next one" % pause)
            sleep(pause)
    return rows


# --------------------------------------------------------------------------
# The checks
# --------------------------------------------------------------------------

# A recording in miniature: the transcript's words, and a "recording" that says
# them with two of them misheard, which is what the aligner exists to survive.
TRAP_TEXT = ("Today we look at the ﬁrst of the mood disorders. "
             "It is briefly described in the reading. ")
TRAP_ROUGH = {"W9-T9-P9.wav": {"duration": 6.0, "words": [
    {"w": " Today", "s": 0.0, "e": 0.4, "p": 0.9},
    {"w": " we", "s": 0.4, "e": 0.6, "p": 0.9},
    {"w": " look", "s": 0.6, "e": 0.9, "p": 0.9},
    {"w": " at", "s": 0.9, "e": 1.0, "p": 0.9},
    {"w": " the", "s": 1.0, "e": 1.2, "p": 0.9},
    {"w": " first", "s": 1.2, "e": 1.6, "p": 0.9},
    {"w": " of", "s": 1.6, "e": 1.7, "p": 0.9},
    {"w": " the", "s": 1.7, "e": 1.9, "p": 0.9},
    {"w": " mood", "s": 1.9, "e": 2.2, "p": 0.9},
    {"w": " disorders.", "s": 2.2, "e": 2.9, "p": 0.9},
    {"w": " It", "s": 3.1, "e": 3.3, "p": 0.9},
    {"w": " is", "s": 3.3, "e": 3.5, "p": 0.9},
    {"w": " breathy", "s": 3.5, "e": 4.0, "p": 0.4},
    {"w": " described", "s": 4.0, "e": 4.6, "p": 0.9},
    {"w": " in", "s": 4.6, "e": 4.7, "p": 0.9},
    {"w": " the", "s": 4.7, "e": 4.9, "p": 0.9},
    {"w": " reading.", "s": 4.9, "e": 5.6, "p": 0.9},
]}}


def self_test():
    """The whole-recording path, with no network, no ffmpeg and no model.

    ⚠️ **This proves the PLUMBING, not the alignment.** `captions.py` has its own
    self-test for the matching, and duplicating it here would be a second opinion
    from the same source.
    """
    bad = []
    name = "W9-T9-P9.wav"
    clip = captions.Clip(name, 0, TRAP_ROUGH[name]["words"])
    captions.align(TRAP_TEXT, [clip])

    if clip.score < captions.MATCH_FLOOR:
        bad.append("a recording that says the transcript's own words scored %.2f"
                   % clip.score)
    made = captions.cues(clip.words, TRAP_TEXT)
    if not made:
        bad.append("no cues came out of a recording that matched")
    said = " ".join(c.said for c in made)
    if "ﬁrst" not in said:
        bad.append("the caption lost the lecturer's own spelling: %r" % said)
    if "breathy" in said:
        bad.append("a word the model misheard reached the caption: %r" % said)

    card = sidecar("W9-T9-P9", name, "materials/X/W9-T9-P9 - Transcript.pdf", "1_x",
                   TRAP_ROUGH, clip, made, "base.en", ".", TRAP_TEXT)
    if card["heard_word_match"] != round(clip.score, 3):
        bad.append("heard_word_match is not the score: %r" % card["heard_word_match"])
    # 🔴 THE SAME NUMBER UNDER THE FIELD THAT KEEPS IT ON BOTH ROUTES. Here they
    # agree because this fixture takes the transcript route; the case that matters
    # is the heard one, and it has a test rather than a self-test because it needs
    # a fallback to be driven.
    if card["audio_matched_transcript"] != round(clip.score, 3):
        bad.append("audio_matched_transcript is not the score: %r"
                   % card["audio_matched_transcript"])
    # 🔴 THE TWO NUMBERS MUST NOT BE THE SAME NUMBER, which is the whole reason
    # the field was split. On this fixture every transcript word reaches a cue
    # while the model misheard one, so covered is 1.0 and the match is not.
    if card["transcript_covered"] != 1.0:
        bad.append("every word of the trap should have reached a cue: %r"
                   % card["transcript_covered"])
    if card["transcript_covered"] == card["heard_word_match"]:
        bad.append("the two coverage numbers cannot both be %r"
                   % card["transcript_covered"])

    # The migration recovers the same answer from the file alone.
    spans = cue_spans_in(TRAP_TEXT, captions.to_vtt(made))
    if len(spans) != len(made):
        bad.append("%d of %d cues were not found back in the transcript"
                   % (len(made) - len(spans), len(made)))
    if covered_from_spans(TRAP_TEXT, spans) != card["transcript_covered"]:
        bad.append("recomputing from the .vtt gave a different answer: %r vs %r"
                   % (covered_from_spans(TRAP_TEXT, spans), card["transcript_covered"]))
    if card["media"]["minutes"] != 0:
        bad.append("six seconds is not %r minutes" % card["media"]["minutes"])
    if card["source"] != "video":
        bad.append("the sidecar does not say what made these cues")

    # The rate limiter, on a clock that costs nothing to run.
    class FakeClock(object):
        def __init__(self):
            self.t = 0.0

        def now(self):
            return self.t

        def sleep(self, s):
            self.t += s

    import io
    clock = FakeClock()
    out = io.BytesIO()
    took = throttled_copy(io.BytesIO(b"x" * 100000), out, 10000,
                          now=clock.now, sleep=clock.sleep)
    if took != 100000:
        bad.append("the copy lost bytes: %d" % took)
    if abs(clock.t - 10.0) > 0.5:
        bad.append("100000 bytes at 10000 a second took %.1f seconds" % clock.t)

    for line in bad:
        print("FAIL: " + line)
    print("self-test: %s" % ("FAILED" if bad else "all checks pass"))
    return 1 if bad else 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Captions for plain lecture recordings.")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--list", metavar="COURSE")
    ap.add_argument("--build", nargs=2, metavar=("COURSE", "DOC"))
    ap.add_argument("--all", metavar="COURSE")
    ap.add_argument("--residecar", metavar="COURSE",
                    help="recompute every existing sidecar from disk; no network")
    ap.add_argument("--survey", nargs="?", const="", metavar="COURSE",
                    help="can each recording's transcript be its own narration? "
                         "reads the files already here and downloads nothing")
    ap.add_argument("--out", help="where the captions go; defaults to the course's "
                                  "own captions/ folder, which EH ruled on 2026-09-02")
    ap.add_argument("--root", default=".")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--rate-kbits", type=int, default=RATE_KBITS)
    ap.add_argument("--pause", type=int, default=PAUSE_SECONDS)
    ap.add_argument("--model", default=captions.PRIMARY_MODEL)
    ap.add_argument("--cache", help="a rough-words json to use instead of listening")
    ap.add_argument("--ffmpeg")
    ap.add_argument("--engine", choices=captions.ENGINES, default=captions.ENGINE_AUTO,
                    help="which engine times the words: the forced aligner (ctc), "
                         "Whisper, or whichever this machine can run (auto)")
    a = ap.parse_args(argv)

    if a.self_test:
        return self_test()

    if a.survey is not None:
        return video_survey(a.root, a.survey or None)

    if a.residecar:
        rows = residecar(a.residecar, a.root, a.out, log=say)
        # 🔴 SAY WHAT HAPPENED, not what the function is called. This printed
        # "brought up to schema 1.2.0" while leaving every card at 1.1.0, because
        # `cues_anchored_on` is a fact about the MATCHING and cannot be recovered
        # from disk. A summary that contradicts the files is worse than none.
        moved = sum(1 for r in rows if r.get("schema_version") == SCHEMA_VERSION)
        if moved == len(rows):
            say("\n%d sidecars brought up to schema %s." % (len(rows), SCHEMA_VERSION))
        else:
            # ⚠️ THE FIELDS ARE LISTED FROM `UNRECOVERABLE`, not typed out here.
            # This sentence named only `cues_anchored_on` until 1.5.0 added a
            # second under the same rule, and would have named only two at 1.6.0:
            # a reader told a card is held back by a field it can SEE in the file
            # has been sent to look in the wrong place.
            say("\n%d sidecars re-carded. %d still at an older schema: "
                "%s cannot be recovered without the rough words, so only a "
                "rebuild moves them."
                % (len(rows), len(rows) - moved,
                   ", ".join(UNRECOVERABLE[:-1]) + " and " + UNRECOVERABLE[-1]))
        return 0

    if a.list:
        rows = recordings(a.list, a.root)
        print("%-11s %-14s %-7s %-10s %s" % ("part", "entry", "local", "captions", "transcript"))
        for r in rows:
            vtt = os.path.join(caption_dir(a.list, r["doc"], a.root, a.out), "video.vtt")
            print("%-11s %-14s %-7s %-10s %s"
                  % (r["doc"], r["entry"] or "-", "yes" if r["local"] else "-",
                     "yes" if os.path.exists(vtt) else "-",
                     os.path.basename(r["transcript"]) if r["transcript"] else "🔴 NONE"))
        missing = [r for r in rows if not r["transcript"]]
        print("\n%d recordings, %d already captioned, %d with no transcript."
              % (len(rows),
                 sum(1 for r in rows
                     if os.path.exists(os.path.join(
                         caption_dir(a.list, r["doc"], a.root, a.out), "video.vtt"))),
                 len(missing)))
        return 0

    if a.build:
        course, doc = a.build
        say("%s" % doc)
        row = build_one(course, doc, a.root, a.out, a.force, a.rate_kbits,
                        a.model, a.cache, a.ffmpeg, log=say, engine=a.engine)
        print("  %s%s%s" % (row["state"],
                            "" if row["cues"] == 0 else ", %d cues" % row["cues"],
                            "" if row["score"] is None else
                            ", coverage %.3f" % row["score"]))
        return 0 if row["state"] in FINISHED else 1

    if a.all:
        rows = build_all(a.all, a.root, a.out, a.force, a.rate_kbits, a.pause,
                         a.model, a.ffmpeg, engine=a.engine)
        print("\n%-11s %8s %6s  %s" % ("part", "coverage", "cues", "state"))
        for r in rows:
            print("%-11s %8s %6s  %s"
                  % (r["doc"], "-" if r["score"] is None else "%.3f" % r["score"],
                     r["cues"] or "-", r["state"]))
        wrote = sum(1 for r in rows if r["state"] in WROTE)
        print("\n%d written (%d from the machine's own words), %d already "
              "current, %d could not be done."
              % (wrote,
                 sum(1 for r in rows if r["state"] == "written (heard)"),
                 sum(1 for r in rows if r["state"] == "kept"),
                 sum(1 for r in rows if r["state"] not in FINISHED)))
        return 1 if any(r["state"] == "blocked" for r in rows) else 0

    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
