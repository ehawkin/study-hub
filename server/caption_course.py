#!/usr/bin/env python3
"""One control for a whole course's captions: narrated packages and plain
recordings alike.

**EH asked for this in one sentence** (2026-09-04): *"there should be a system or
option inside the web interface to download captions for more than one type of
video."* ⚠️ **The ask is not "a second button"** — it is that the person using the
reader should not have to know that a narrated package and a plain recording are
captioned by two different pipelines. **This file is the only place that knows**,
and the Settings page talks to nothing else.

🔴 **THE SERVER NEVER IMPORTS THIS FILE. IT SHELLS OUT TO IT.** Both generators
underneath carry the same line, and `test_caption_course` walks the server's
transitive import closure to keep all three outside it. **The reason is not
tidiness.** A recipient's reader is stdlib-only; importing this would quietly
make whisper, ffmpeg and pdftotext dependencies of *opening a lesson*.

⚠️ **AND IT NEVER RUNS BY SURPRISE**, which is a separate promise from the one
above and the one the brief actually asks for. Nothing here starts on a timer, on
a page load, or on a reader opening a lecture. There are exactly two entry
points: this file's own command line, and one authenticated POST that a person
clicks. 🟢 **`status()` reads the disk and starts no work at all**, which is what
makes it safe for the settings page to call on every render, and why the status
route and the build route are two different verbs rather than one.

**What this adds over running the two generators by hand:**

- **One list of a course's lectures with one vocabulary for their states**, so
  `done` means the same thing whichever pipeline would produce it.
- 🔴 **A record of WHY a lecture failed that outlives the run that failed it.**
  Without it a failed lecture is indistinguishable from one nobody has tried, and
  those want opposite things done about them.
- **A readiness answer that NAMES the missing tool**, checked before a byte is
  fetched rather than discovered thirty minutes into a run.

⚠️ **The rate rules are not re-implemented here and must not be.** Serial, one
lecture at a time, a pause between, and STOP DEAD on a 403 or 429 all live in
`video_captions.build_all`, which this calls. **A second copy of a politeness
rule is a second thing to get wrong.**
"""

import argparse
import glob
import json
import os
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import align_ctc                                                 # noqa: E402
import captions                                                  # noqa: E402
import install_captions                                          # noqa: E402
import presinfo                                                  # noqa: E402
import transcripts                                               # noqa: E402
import video_captions                                            # noqa: E402


PACKAGE = captions.PACKAGE       # the sidecar's `source` on that route, too

# What --build and --status say when the engine is the thing missing. One
# string, so the terminal and the tests agree on it.
INSTALL_SENTENCE = ("The caption engine is not installed. Run "
                    "`python3 server/caption_course.py --install` once: it says "
                    "what it will download (a few hundred MB) before it starts, "
                    "and nothing runs until you do.")
RECORDING = "recording"

# 🔴 The states a lecture can be in, and they are a CLOSED set on purpose: the
# settings page renders each one differently and a state it has never heard of
# would render as nothing at all. A test pins that every row status() returns is
# one of these.
DONE = "done"            # the caption files are on disk
MISSING = "missing"      # it could be built and has not been
FAILED = "failed"        # the last run tried and did not produce them
BLOCKED = "blocked"      # it cannot be built at all, and `reason` says why

STATES = (DONE, MISSING, FAILED, BLOCKED)

RUN_FILE = ".captions-run.json"

# 🔴 THE AUDIT'S TWO THRESHOLDS, and both are deliberately loose.
# Healthy lectures measured 2026-09-04 sat at 0.997 to 0.999 covered and 125 to
# 139 words a minute. The two TRUNCATED ones sat at 0.722/0.801 and 382/187.
# The gap is enormous, so a loose threshold separates them without ever calling
# an unusual-but-fine lecture broken. ⚠️ Nobody speaks at 220 words a minute;
# a lecture that appears to is a lecture whose audio is shorter than its words.
COVERED_FLOOR = 0.90
WPM_CEILING = 220

# 🔴 THE THIRD WITNESS, and the hole it plugs is the reason it is not redundant.
# `covered` and `wpm` BOTH need the transcript. When it is missing, or pdftotext
# cannot read it, this function computes neither and the row passes with NO
# witness at all: a lecture captioned for a third of its length is reported fine.
# 🟢 KCL's listed `minutes` needs no transcript and no download, so it answers on
# exactly the lecture the other two cannot see. (QA proposed the comparison;
# running the audit against the two truncated artifacts is what showed WHICH gap
# it fills, and also showed `covered` already catches those two at 0.722/0.801.)
# ⚠️ ONE-SIDED, on purpose. A recording LONGER than its listed minutes is
# ordinary rounding and one healthy lecture already measured 1.074 of its listed
# length, so a symmetric band would fire on good data. The failure direction is
# short. The allowance is about five times the worst shortfall measured on a
# healthy file (0.974 of listed, i.e. 3%), and the flat minute absorbs the
# rounding on a short lecture, where a percentage alone is too tight to trust.
DURATION_SHORTFALL = 0.15
DURATION_GRACE = 1.0


# --------------------------------------------------------------------------
# Is this machine equipped at all
# --------------------------------------------------------------------------

def find_pdftotext(named=None):
    """`pdftotext`, found rather than assumed: `transcripts.find_pdftotext`,
    which every reader of a transcript PDF now goes through, so the readiness
    answer here and the read it predicts cannot disagree."""
    return transcripts.find_pdftotext(named)


def tools():
    """What the three generators need, and whether this machine has it.

    🟢 **Checked BEFORE a fetch rather than during one.** The failure this
    prevents is real and expensive: half an hour of polite downloading, then a
    crash on a missing `ffmpeg` that was knowable in a millisecond.

    ⚠️ **`caption engine` is the slow one to answer**: the forced aligner's venv
    is a path test, but when it is absent `captions.find_python` may run a
    subprocess per candidate virtualenv, so callers who only want a file listing
    should not ask for this.

    🟢 **The engine is whichever of the two this machine has**: the forced
    aligner `install_captions` builds (the kit's way in, and the default engine),
    or a Whisper venv somebody made by hand. A missing engine is the one tool the
    kit can install for a person; `pdftotext` and `ffmpeg` are named separately
    so the settings page can say which action would help.
    """
    return {
        "ffmpeg": video_captions.find_ffmpeg(),
        "caption engine": align_ctc.find_python() or captions.find_python(),
        "pdftotext": find_pdftotext(),
    }


def missing_tools(have=None):
    have = tools() if have is None else have
    return sorted(name for name, path in have.items() if not path)


# --------------------------------------------------------------------------
# What a course has
# --------------------------------------------------------------------------

def packages(course, root="."):
    """Every narrated package in a course, by part id.

    Globs the same shape `captions.survey` does, so the two cannot disagree about
    what a package is.
    """
    out = []
    pattern = os.path.join(root, "courses", course, "packages", "*", "index.html")
    for idx in sorted(glob.glob(pattern)):
        out.append({"doc": os.path.basename(os.path.dirname(idx)), "index": idx})
    return out


def clip_plan(index_path):
    """How many clips a package actually plays, or None if it cannot be read.

    ⚠️ **Returns None rather than 0 on a broken package**, because 0 and "could
    not tell" want different words on the page and the same number would hide
    one inside the other.
    """
    try:
        return len(captions.playing_clips(presinfo.load(index_path)))
    except Exception:
        return None


def package_row(course, doc, index_path, root=".", out_dir=None):
    dest = video_captions.caption_dir(course, doc, root, out_dir)
    got = len(glob.glob(os.path.join(dest, "sound*.vtt")))
    want = clip_plan(index_path)
    row = {"doc": doc, "kind": PACKAGE, "cues_files": got, "clips": want}
    pdf = transcripts.transcript_for(os.path.join(root, "materials", course), doc)
    if not pdf and not got:
        # ⚠️ Same sentence as a recording with no transcript, and for the same
        # reason: whisper supplies timings and never words. Said UP FRONT rather
        # than discovered per clip thirty minutes into a run.
        row["state"] = BLOCKED
        row["reason"] = "no transcript, and captions are the transcript's words"
    elif want is None:
        row["state"] = BLOCKED
        row["reason"] = "the package's index.html could not be read"
    elif want == 0:
        row["state"] = BLOCKED
        row["reason"] = "the package plays no audio clips"
    elif got >= want:
        row["state"] = DONE
    elif got:
        # 🔴 A PARTIAL BUILD IS NOT `done`, and this is the case a file-existence
        # check gets wrong. `captions.build` is per clip and resumable, so a run
        # stopped halfway leaves real .vtt files for the clips it reached. Those
        # are worth keeping and worth finishing, and only a COUNT can tell the
        # difference between finished and interrupted.
        row["state"] = MISSING
        row["reason"] = "%d of %d clips captioned" % (got, want)
    else:
        row["state"] = MISSING
    return row


def recording_row(course, rec, root=".", out_dir=None):
    doc = rec["doc"]
    dest = video_captions.caption_dir(course, doc, root, out_dir)
    row = {"doc": doc, "kind": RECORDING, "entry": rec.get("entry") or "",
           "minutes": rec.get("minutes")}
    if os.path.isfile(os.path.join(dest, "video.vtt")):
        row["state"] = DONE
    elif not rec.get("transcript"):
        # The one thing no amount of fetching fixes: whisper supplies timings and
        # never words, so a lecture with no transcript has no words to time.
        row["state"] = BLOCKED
        row["reason"] = "no transcript, and captions are the transcript's words"
    else:
        # 🔴 A refusal left a marker precisely so this is not reported as
        # "nobody has tried yet". The disk carries it, so it is true whatever
        # ran the build and whether or not a run record survived.
        seen = video_captions.read_refusal(dest)
        # 🔴 A refusal from an OLDER aligner is not news, it is history. The
        # verdict was produced by code that has since changed, so the lecture
        # reads as MISSING (true: it has no captions and could be built) rather
        # than FAILED with a reason that may no longer hold.
        if seen and seen.get("verdict_version") != video_captions.VERDICT_VERSION:
            seen = None
        if seen:
            row["state"] = FAILED
            row["remembered"] = True
            row["reason"] = (seen.get("reason")
                             or "an earlier run could not time this lecture")
        else:
            row["state"] = MISSING
    return row


def lectures(course, root=".", out_dir=None):
    """Every lecture in a course that could carry captions, in part order.

    🔴 **Both halves are derived, never listed.** Packages come from the folders
    on disk and recordings from `materials.json`, which is the file the SERVER
    reads. A typed list is right the day it is written and silently wrong the day
    the course gains a lecture.
    """
    rows = [package_row(course, p["doc"], p["index"], root, out_dir)
            for p in packages(course, root)]
    rows += [recording_row(course, r, root, out_dir)
             for r in video_captions.recordings(course, root)]
    rows.sort(key=lambda r: r["doc"])
    return rows


# --------------------------------------------------------------------------
# The record of the last run, which outlives the run
# --------------------------------------------------------------------------

def run_path(course, root=".", out_dir=None):
    base = out_dir or os.path.join(root, "courses", course, "captions")
    return os.path.join(base, RUN_FILE)


def read_run(course, root=".", out_dir=None):
    try:
        with open(run_path(course, root, out_dir), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_run(record, course, root=".", out_dir=None):
    path = run_path(course, root, out_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(record, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)
    return path


def alive(pid):
    """Whether a pid is still running, without signalling it.

    ⚠️ **A stale pid file is the normal case, not the exception**: a run that is
    killed, or a machine that reboots, leaves one behind. Treating `running` as a
    fact recorded in a file rather than a property of the system is how a control
    ends up permanently refusing to start. One answer for both records that hold
    a pid, the run's and the install's: `install_captions.alive` is it.
    """
    return install_captions.alive(pid)


# --------------------------------------------------------------------------
# What a run reports, in this file's vocabulary rather than each generator's
# --------------------------------------------------------------------------

# 🔴 THE TRANSLATION TABLE, and it exists so the two generators do NOT have to
# agree with each other. `captions.py` and `video_captions.py` each grew their own
# words for what happened to a clip; the settings page needs ONE vocabulary, and
# the right place to reconcile them is here rather than in either of them or in
# the page. ⚠️ An unknown state maps to FAILED carrying its own text, so a word
# added to a generator later shows up as an honest failure rather than vanishing.
# 🔴 THE NOTE A SUCCESS CARRIES, where it has one. Only the words matter here:
# WHICH states are successes is not this table's business any more, and that is
# the whole of the change below.
DONE_NOTES = {
    "written (heard)": ("captions are the machine's own words, because the "
                        "audio did not match the transcript closely enough to "
                        "time the lecturer's"),
}


def done_states(finished=None):
    """The `DONE` rows, DERIVED from `video_captions.FINISHED` rather than restated.

    🔴🔴 **THE DEFECT THIS REMOVES HAS ALREADY SHIPPED ONCE.** `written (heard)`
    was added to this table and NOT to the literals deciding the exit code, so
    the first lecture that succeeded through the fallback **exited 1** and would
    have been counted under *"could not be done"*. ⚠️ **A caption run is
    unattended and a script reads that exit code**, so a success reported as a
    failure is the direction that costs somebody a night.

    🟢 **Two correct lists cannot disagree if there is only one of them.** An
    equality assertion would have been a second list to maintain; this removes
    the failure mode instead of watching for it.

    ⚠️ **THE DERIVATION RUNS THE OPPOSITE WAY FROM THE ONE THE QUEUE ENTRY
    PROPOSED, and it has to.** The entry said *"derive `FINISHED` from
    `GENERATOR_STATES`"*; **this module imports `video_captions`, so the reverse
    import would be a cycle.** The direction is forced, and the guarantee is the
    same either way: **the exit code's list is the only list.**

    🟢 Takes `finished` so a test can prove the derivation is LIVE rather than
    merely agreeing today, which a frozen copy would also do.
    """
    if finished is None:
        finished = video_captions.FINISHED
    return dict((state, (DONE, DONE_NOTES.get(state, ""))) for state in finished)


GENERATOR_STATES = dict(done_states(), **{
    "no transcript": (BLOCKED, "no transcript, and captions are the transcript's words"),
    "not a recording": (BLOCKED, "not a plain recording"),
    "not transcribed": (FAILED, "whisper returned no words for this recording's audio"),
    "refused": (FAILED, "no cue survived alignment against the transcript"),
    "refused before": (FAILED, "refused by an earlier run; build it again "
                       "with force to try anyway"),
    "too fast to read": (FAILED, "the captions would run faster than a reader "
                         "can follow"),
    "blocked": (FAILED, "the run stopped here: the video service refused it"),
})


def translate(state):
    """One generator's word for what happened, in this file's vocabulary."""
    if state in GENERATOR_STATES:
        return GENERATOR_STATES[state]
    text = str(state or "")
    if text.startswith("failed: "):
        return FAILED, text[len("failed: "):]
    return FAILED, text or "the generator said nothing"


def package_outcome(rows):
    """One package's clip rows folded into one lecture-level state."""
    if not rows:
        return BLOCKED, "the package plays no audio clips"
    good = [r for r in rows if r.get("state") in ("written", "kept")]
    if len(good) == len(rows):
        return DONE, ""
    bad = [r for r in rows if r.get("state") not in ("written", "kept")]
    if all(r.get("state") == "no transcript" for r in bad) and not good:
        # The whole package, not a bad clip in it. BLOCKED rather than FAILED,
        # because nothing about re-running would change the answer.
        return BLOCKED, "no transcript, and captions are the transcript's words"
    words = sorted({translate(r.get("state"))[1] or str(r.get("state")) for r in bad})
    return FAILED, "%d of %d clips captioned (%s)" % (len(good), len(rows),
                                                      "; ".join(words))


# --------------------------------------------------------------------------
# The status the settings page renders. IT STARTS NO WORK
# --------------------------------------------------------------------------

def status(course, root=".", out_dir=None, with_tools=True):
    """Every lecture, its state, and whether a run is in flight.

    🟢 **Reads the disk and nothing else.** No fetch, no whisper, no subprocess
    beyond the tool probe, so the settings page may call it on every render.

    🔴 **Disk truth wins over the run record**, always. A lecture whose files are
    present is `done` even if the last run recorded a failure for it, because
    somebody may have built it by hand since. The record is only consulted to put
    a REASON on a lecture the disk says is still missing, which is the one thing
    the disk cannot tell anybody.
    """
    rows = lectures(course, root, out_dir)
    run = read_run(course, root, out_dir)
    was = {r.get("doc"): r for r in (run.get("lectures") or [])
           if isinstance(r, dict) and r.get("doc")}
    for row in rows:
        if row["state"] != MISSING:
            continue
        seen = was.get(row["doc"]) or {}
        if seen.get("state") in (FAILED, BLOCKED):
            row["state"] = seen["state"]
            row["reason"] = seen.get("reason") or ""
    counts = {}
    for row in rows:
        counts[row["state"]] = counts.get(row["state"], 0) + 1
    out = {
        "ok": True,
        "course": course,
        "lectures": rows,
        "counts": counts,
        # 🔴 What a build would ATTEMPT, which is not what is merely not done.
        # A remembered refusal is skipped unless forced, so counting it
        # here would promise a reader work that will not happen.
        "buildable": sum(1 for r in rows if r["state"] == MISSING
                         or (r["state"] == FAILED and not r.get("remembered"))),
        "running": alive(run.get("pid")),
        "last_run": {k: run.get(k) for k in ("started", "finished", "stopped")
                     if run.get(k)},
    }
    if with_tools:
        have = tools()
        out["tools"] = {k: bool(v) for k, v in have.items()}
        out["missing_tools"] = missing_tools(have)
        out["ready"] = not out["missing_tools"]
        # 🟢 The one missing tool the kit can put right itself, and the state
        # of that put-right, so the settings page can offer the install (or
        # show it going) instead of a dead "not found".
        out["install_needed"] = not have.get("caption engine")
        out["install"] = install_captions.install_status()
        out["hints"] = hints(out["missing_tools"])
    return out


def hints(missing):
    """One sentence per missing tool the install cannot supply, for a person."""
    out = {}
    if "pdftotext" in missing:
        out["pdftotext"] = ("pdftotext comes with poppler: `brew install poppler` "
                            "on a Mac with Homebrew, or set STUDY_HUB_PDFTOTEXT "
                            "to one you have.")
    if "ffmpeg" in missing:
        out["ffmpeg"] = ("installing the caption engine places an ffmpeg; or "
                         "`brew install ffmpeg`, or set STUDY_HUB_FFMPEG.")
    return out


# --------------------------------------------------------------------------
# The audit: captions that EXIST but do not cover their lecture
# --------------------------------------------------------------------------

def audit(course, root=".", out_dir=None):
    """Every captioned recording, and whether its cues actually cover the lecture.

    🔴 **WHY THIS EXISTS, and it is worth reading before deleting it.** Until
    2026-09-04 a truncated download was accepted silently, and it produced a
    perfectly well-formed `.vtt` for the part that arrived. **Every guard passed**:
    the status was 200, the `ftyp` box is at the start of a file so a short MP4 is
    still an MP4, and the sidecar's own `cues_to` against `media.seconds` agreed,
    because both of those come from the same short file.

    ⚠️ **`video_captions.fetch_recording` now compares against `Content-Length`,
    so this cannot arise again.** This stays for two reasons: the captions written
    BEFORE that fix are still on disk, and a check that reads the finished artifact
    is a different witness from one that reads the wire.

    🟢 **It gives a POSITIVE result**, per this project's rule: a lecture that is
    fine says so, so the check's silence can never be mistaken for its absence.
    """
    rows = []
    mats = os.path.join(root, "materials", course)
    docs = {}
    try:
        with open(os.path.join(root, "courses", course, "materials.json"),
                  encoding="utf-8") as fh:
            docs = (json.load(fh) or {}).get("docs") or {}
    except (OSError, ValueError):
        pass
    for rec in video_captions.recordings(course, root):
        doc = rec["doc"]
        dest = video_captions.caption_dir(course, doc, root, out_dir)
        vtt = os.path.join(dest, "video.vtt")
        if not os.path.isfile(vtt):
            continue
        row = {"doc": doc, "ok": True, "why": ""}
        try:
            with open(os.path.join(dest, "captions.json"), encoding="utf-8") as fh:
                side = json.load(fh)
        except (OSError, ValueError):
            side = {}
        seconds = (side.get("media") or {}).get("seconds") or 0
        row["minutes_played"] = round(seconds / 60.0, 1) if seconds else None
        row["minutes_listed"] = (docs.get(doc) or {}).get("minutes")
        try:
            text = captions.spoken_source(
                transcripts.pdf_text(rec["transcript"])) if rec.get("transcript") else ""
        except Exception:
            text = ""
        if text:
            words = len(captions.words_of(text))
            row["words"] = words
            if seconds:
                row["wpm"] = int(round(words / (seconds / 60.0)))
            with open(vtt, encoding="utf-8") as fh:
                spans = video_captions.cue_spans_in(text, fh.read())
            row["covered"] = round(video_captions.covered_from_spans(text, spans), 3)
        bad = []
        if row.get("covered") is not None and row["covered"] < COVERED_FLOOR:
            bad.append("only %d%% of the transcript reached a cue"
                       % round(row["covered"] * 100))
        if row.get("wpm") and row["wpm"] > WPM_CEILING:
            bad.append("%d words a minute, which nobody speaks" % row["wpm"])
        # 🔴 This one deliberately sits OUTSIDE the `if text:` block above: it is
        # the only check here that still works when the transcript does not.
        listed, played = row.get("minutes_listed"), row.get("minutes_played")
        if listed and played is not None:
            allowed = max(DURATION_GRACE, DURATION_SHORTFALL * listed)
            if played < listed - allowed:
                bad.append("%.1f minutes of audio for a lecture listed at %d"
                           % (played, listed))
        if bad:
            row["ok"] = False
            # 🔴 Name the ACTION, not just the symptom. The fix is always the
            # same and it is not obvious from the numbers.
            row["why"] = ("%s. The recording probably downloaded short: delete "
                          "this lecture's caption folder and build it again."
                          % "; ".join(bad))
        rows.append(row)
    return rows


def is_doc_id(name):
    """Whether a directory under `captions/` names a LECTURE.

    🔴 **A DOC ID HAS NO DOT IN IT.** Every lecture id this project makes is
    `W1-T2-P3` shaped, and the standing backup rule names a backup
    `<name>.YYYYMMDD-HHMMSS.bak`. **The dot is the whole difference, and it is
    the difference for every backup shape rather than only the ones ending
    `.bak`** — `W4-T1-P1.PRIMED-hotwords-20260904-191948.bak` is one of the
    eleven this was written for, and a rule matching the suffix alone would have
    to be widened for each new shape somebody invents.

    ⚠️ **Verified against the disk rather than the sample, 2026-09-09: 62 caption
    directories, 17 with a dot, all 17 ending `.bak`, and not one dotless
    directory ending `.bak`.** Both rules agree 62 for 62 today; this one is the
    broader of the two, and the caller PRINTS what it discarded so a wrong call
    is visible rather than silent.

    🔴 It says nothing about whether the lecture EXISTS. A directory named for a
    lecture that was deleted is still a doc id; that is the audit's question.
    """
    return "." not in name


def legibility(course, root=".", out_dir=None):
    """Every captioned lecture, and how much of it a reader cannot keep up with.

    🔴 **THIS COUNTS AND DOES NOT REFUSE**, deliberately. QA and the manager both
    ruled that the gate belongs on the OUTPUT rather than on the anchor score,
    and both declined to fix the number, because where the line goes governs what
    EH is served. **What this produces is the distribution to put it on.**

    ⚠️ A package lecture is many `sound*.vtt`; they are folded into one lecture,
    because the reader meets one lecture rather than eleven clips.

    🟢 **It runs the SHIPPED counter on the finished tracks** via
    `captions.cues_from_vtt`, rather than measuring them a second way. Two
    implementations of "how fast is this cue" is how two numbers about one file
    come to disagree.

    🔴 **IT RETURNS WHAT IT SKIPPED, and the tuple is the point.** Measured
    2026-09-09: this reported **49 lectures for a 38-lecture course**, because it
    walked every directory under `captions/` and eleven of them are BACKUPS left
    by the standing backup rule. **Eleven rows were duplicates of other rows in
    the same table** and the overall share was diluted by counting good lectures
    twice. ⚠️ **A reporter that silently measures the wrong population fails in
    the reassuring direction**, and this one produced the distribution a
    threshold was spent on. Returning `(rows, skipped)` rather than `rows` is
    deliberate: it makes every caller name the discarded half instead of
    inheriting it.
    """
    base = out_dir or os.path.join(root, "courses", course, "captions")
    rows, skipped = [], []
    for doc in sorted(os.listdir(base)) if os.path.isdir(base) else []:
        here = os.path.join(base, doc)
        if not os.path.isdir(here):
            continue
        if not is_doc_id(doc):
            skipped.append(doc)
            continue
        tally = {"cues": 0, "unreadable": 0, "at_floor": 0}
        for name in sorted(os.listdir(here)):
            if not name.endswith(".vtt"):
                continue
            with open(os.path.join(here, name), encoding="utf-8") as fh:
                got = captions.legibility(captions.cues_from_vtt(fh.read()))
            for k in tally:
                tally[k] += got[k]
        if tally["cues"]:
            tally["doc"] = doc
            tally["share"] = round(tally["unreadable"] / tally["cues"], 3)
            rows.append(tally)
    rows.sort(key=lambda r: -r["share"])
    return rows, skipped


# --------------------------------------------------------------------------
# The run
# --------------------------------------------------------------------------

def build_course(course, root=".", out_dir=None, force=False, log=None,
                 only=None, rate_kbits=video_captions.RATE_KBITS,
                 pause=video_captions.PAUSE_SECONDS,
                 model=captions.PRIMARY_MODEL, engine=captions.ENGINE_AUTO):
    """Caption everything this course can caption, packages first.

    🔴 **PACKAGES FIRST, DELIBERATELY.** They need no network at all, so the work
    that cannot be refused by anybody else is finished before the work that can.
    A run stopped dead by a 429 has then still banked every package it did.

    ⚠️ **The rate rules are `video_captions.build_all`'s and are not repeated
    here.** This passes them through and otherwise stays out of the way.
    """
    log = log or (lambda *a: None)
    out_dir = out_dir or os.path.join(root, "courses", course, "captions")
    record = {"course": course, "started": time.time(), "pid": os.getpid(),
              "lectures": []}
    write_run(record, course, root, out_dir)

    pkgs = [p for p in packages(course, root) if not only or p["doc"] in only]
    log("%d package(s), then the recordings" % len(pkgs))
    for pkg in pkgs:
        doc = pkg["doc"]
        dest = video_captions.caption_dir(course, doc, root, out_dir)
        want = clip_plan(pkg["index"])
        got = len(glob.glob(os.path.join(dest, "sound*.vtt")))
        if not force and want and got >= want:
            log("%s: kept (%d clips)" % (doc, got))
            record["lectures"].append({"doc": doc, "kind": PACKAGE, "state": DONE,
                                       "reason": ""})
            write_run(record, course, root, out_dir)
            continue
        log("%s: %d of %s clips, building" % (doc, got, want))
        try:
            rows = captions.build(course, doc, out_dir, root, force, None, True,
                                  engine=engine)
            state, reason = package_outcome(rows)
        except Exception as e:                      # one lecture's failure is one fact
            state, reason = FAILED, str(e)
        log("  %s%s" % (state, (": " + reason) if reason else ""))
        record["lectures"].append({"doc": doc, "kind": PACKAGE, "state": state,
                                   "reason": reason})
        write_run(record, course, root, out_dir)

    log("recordings")
    rows = video_captions.build_all(course, root, out_dir, force, rate_kbits,
                                    pause, model, log=log, only=only, engine=engine)
    for row in rows:
        state, reason = translate(row.get("state"))
        record["lectures"].append({"doc": row.get("doc"), "kind": RECORDING,
                                   "state": state, "reason": reason})
        if row.get("state") == "blocked":
            record["stopped"] = "the video service refused a fetch"
    record["finished"] = time.time()
    write_run(record, course, root, out_dir)
    return record


def start(course, root=".", out_dir=None, force=False, python_bin=None,
          popen=subprocess.Popen):
    """Start a run in its OWN process and return at once.

    🔴 **This is what the server calls, and the detachment is the point.** A
    course takes an hour; an HTTP handler must not hold it. `start_new_session`
    puts the run in its own process group, so it survives the server being
    restarted underneath it and a stray Ctrl-C in the server's terminal does not
    kill a fetch halfway through somebody's lecture.
    """
    out_dir = out_dir or os.path.join(root, "courses", course, "captions")
    running = read_run(course, root, out_dir)
    if alive(running.get("pid")):
        return {"ok": False, "error": "a run is already going",
                "pid": running.get("pid")}
    os.makedirs(out_dir, exist_ok=True)
    argv = [python_bin or sys.executable, os.path.abspath(__file__),
            "--build", course, "--root", os.path.abspath(root),
            "--out", os.path.abspath(out_dir)]
    if force:
        argv.append("--force")
    logfile = os.path.join(out_dir, ".captions-run.log")
    # ⚠️ Opened here and CLOSED here. The child gets its own descriptor across the
    # fork, so holding this one open only leaks it into the SERVER, which is the
    # process that never exits.
    with open(logfile, "a", encoding="utf-8") as handle:
        handle.write("\n==== %s ====\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
        handle.flush()
        proc = popen(argv, stdout=handle, stderr=subprocess.STDOUT,
                     stdin=subprocess.DEVNULL, start_new_session=True,
                     cwd=os.path.abspath(root))
    return {"ok": True, "pid": getattr(proc, "pid", None), "log": logfile}


def say(*bits):
    """Print and FLUSH, for the same measured reason `video_captions.say` does:
    an unattended run whose output sits in a buffer cannot be watched at all."""
    print(*bits)
    sys.stdout.flush()


def self_test():
    """The checks that need no corpus, no network and no whisper."""
    for word, (state, _) in GENERATOR_STATES.items():
        assert state in STATES, word
    assert translate("written")[0] == DONE
    assert translate("failed: disk full") == (FAILED, "disk full")
    assert translate("something new")[0] == FAILED, "an unknown state must not vanish"
    assert translate("something new")[1] == "something new"

    assert package_outcome([])[0] == BLOCKED
    assert package_outcome([{"state": "written"}, {"state": "kept"}])[0] == DONE
    part = package_outcome([{"state": "written"}, {"state": "refused"}])
    assert part[0] == FAILED and "1 of 2" in part[1], part

    assert alive(os.getpid()) is True
    assert alive(None) is False
    assert alive(0) is False
    # 🔴 A pid that cannot exist. The control matters: `alive` returning True for
    # everything would make the "a run is already going" refusal permanent.
    assert alive(4000000) is False

    print("self-test: all checks pass")
    return 0


def _say_skipped(skipped):
    """🔴 SAY WHAT WAS NOT MEASURED, ALWAYS, INCLUDING WHEN IT IS NOTHING.

    This project's own rule about checks that are silent on success, applied to
    the half a reporter usually drops: **a passing filter and a missing filter
    look identical from outside.** The line costs one row and makes its own
    absence visible, which is exactly what was missing on the day this reported
    49 lectures for a 38-lecture course and nobody could tell from the output.
    """
    if skipped:
        print("%d director%s skipped as not a lecture id: %s"
              % (len(skipped), "y" if len(skipped) == 1 else "ies",
                 ", ".join(skipped)))
    else:
        print("every directory under captions/ is a lecture id; none skipped")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--status", metavar="COURSE")
    ap.add_argument("--build", metavar="COURSE")
    ap.add_argument("--audit", metavar="COURSE",
                    help="check captions that already exist actually cover their "
                         "lecture; needs no network")
    ap.add_argument("--legibility", metavar="COURSE",
                    help="how much of each captioned lecture a reader cannot keep "
                         "up with; counts, never refuses, and needs no network")
    ap.add_argument("--start", metavar="COURSE",
                    help="start a run in its own process and return at once")
    ap.add_argument("--install", action="store_true",
                    help="install the caption engine into ~/.kcl-study/captions-venv, "
                         "saying what it will download before it starts")
    ap.add_argument("--install-start", action="store_true",
                    help="the same install in its own process; answer at once")
    ap.add_argument("--install-status", action="store_true",
                    help="how the install stands; starts nothing")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--root", default=".")
    ap.add_argument("--out", help="where the caption folders go; defaults to "
                                  "courses/<COURSE>/captions")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--only", action="append",
                    help="one lecture; repeatable. Everything by default")
    ap.add_argument("--no-tools", action="store_true",
                    help="skip the tool probe, which is the slow part of --status")
    ap.add_argument("--engine", choices=captions.ENGINES, default=captions.ENGINE_AUTO,
                    help="which engine times the words: the forced aligner (ctc), "
                         "Whisper, or whichever this machine can run (auto)")
    a = ap.parse_args(argv)

    if a.self_test:
        return self_test()

    if a.install:
        # Foreground, in this process: the documented verb for a terminal. The
        # settings page uses --install-start, which detaches exactly as --start
        # does for a build.
        return install_captions.install(install_captions.say)

    if a.install_start:
        out = install_captions.start_install()
        json.dump(out, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
        return 0 if out.get("ok") else 1

    if a.install_status:
        data = install_captions.install_status()
        json.dump(data, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
        return 0

    if a.status:
        data = status(a.status, a.root, a.out, not a.no_tools)
        if a.json:
            json.dump(data, sys.stdout, indent=2, sort_keys=True)
            sys.stdout.write("\n")
            return 0
        miss = data.get("missing_tools") or []
        print("%s: %s" % (a.status, ", ".join(
            "%d %s" % (n, s) for s, n in sorted(data["counts"].items())) or "nothing"))
        if miss:
            print("🔴 not ready: %s" % ", ".join(miss))
            if data.get("install_needed"):
                print(INSTALL_SENTENCE)
            for name, hint in sorted((data.get("hints") or {}).items()):
                print("   %s: %s" % (name, hint))
        if data["running"]:
            print("a run is going now")
        for row in data["lectures"]:
            print("  %-10s %-9s %-8s %s" % (row["doc"], row["kind"], row["state"],
                                            row.get("reason") or ""))
        return 0

    if a.legibility:
        rows, skipped = legibility(a.legibility, a.root, a.out)
        if a.json:
            json.dump({"lectures": rows, "skipped": skipped},
                      sys.stdout, indent=2, sort_keys=True)
            sys.stdout.write("\n")
            return 0
        if not rows:
            print("%s: no captions to measure" % a.legibility)
            _say_skipped(skipped)
            return 0
        print("%-10s %6s %8s %8s %9s" % ("doc", "cues", "unread", "share", "at floor"))
        for r in rows:
            print("%-10s %6d %8d %7.1f%% %9d"
                  % (r["doc"], r["cues"], r["unreadable"], 100 * r["share"],
                     r["at_floor"]))
        cues = sum(r["cues"] for r in rows)
        bad = sum(r["unreadable"] for r in rows)
        # 🔴 A POSITIVE RESULT AND NO VERDICT. Where the line goes is EH's with
        # the manager ruling; this prints the distribution to put it on.
        print("\n%d lectures, %d cues, %d unreadable (%.2f%%). "
              "This COUNTS; it does not refuse."
              % (len(rows), cues, bad, 100.0 * bad / cues if cues else 0.0))
        _say_skipped(skipped)
        return 0

    if a.audit:
        rows = audit(a.audit, a.root, a.out)
        if a.json:
            json.dump(rows, sys.stdout, indent=2, sort_keys=True)
            sys.stdout.write("\n")
            return 0 if all(r["ok"] for r in rows) else 1
        if not rows:
            print("%s: no captioned recordings to check" % a.audit)
            return 0
        print("%-10s %6s %6s %6s %7s  %s"
              % ("doc", "played", "listed", "wpm", "covered", ""))
        for r in rows:
            print("%-10s %6s %6s %6s %7s  %s"
                  % (r["doc"], r.get("minutes_played"), r.get("minutes_listed"),
                     r.get("wpm"), r.get("covered"),
                     r["why"] if not r["ok"] else "ok"))
        bad = [r for r in rows if not r["ok"]]
        # 🟢 A positive result, so the check's silence is never mistaken for its
        # absence: it says how many it looked at even when all of them are fine.
        print("\n%d of %d captioned recordings cover their lecture."
              % (len(rows) - len(bad), len(rows)))
        return 1 if bad else 0

    if a.build:
        miss = missing_tools()
        if miss:
            # 🔴 BEFORE a byte is fetched, not thirty minutes into a run. And
            # when the missing tool is the one the kit can install, say so:
            # "not found" alone leaves a person on a fresh Mac with nowhere to go.
            print("🔴 cannot build: %s not found" % ", ".join(miss))
            if "caption engine" in miss:
                print(INSTALL_SENTENCE)
            for name, hint in sorted(hints(miss).items()):
                print("   %s: %s" % (name, hint))
            return 1
        record = build_course(a.build, a.root, a.out, a.force, say, a.only,
                              engine=a.engine)
        done = sum(1 for r in record["lectures"] if r["state"] == DONE)
        print("\n%d of %d lectures captioned" % (done, len(record["lectures"])))
        for row in record["lectures"]:
            if row["state"] != DONE:
                print("  %-10s %-8s %s" % (row["doc"], row["state"],
                                           row.get("reason") or ""))
        if record.get("stopped"):
            print("🔴 STOPPED: %s" % record["stopped"])
            return 1
        return 0

    if a.start:
        out = start(a.start, a.root, a.out, a.force)
        json.dump(out, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
        return 0 if out.get("ok") else 1

    ap.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
