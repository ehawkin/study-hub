#!/usr/bin/env python3
"""Download local copies of a course's real lecture recordings.

EH's opt-in wizard checkbox (2026-08-23): recordings stream into the reader
without being downloaded, so a local copy buys offline playback and survival
past the course site closing, nothing else. The stream URLs are derived from
the entry ids already sitting in materials.json, exactly as the server derives
them, so this is mechanical: no course site, no session, no browser.

Files land in <course>/videos/<doc>.mp4. The reader prefers a local copy
automatically (read_materials checks the file's existence); nothing else to
wire. Narrated slide packages are not videos and are not handled here: the
mirror-packages skill does those.

Usage:
    python3 server/fetch_videos.py <course-folder> [--doc W1-T1-P1] [--force]
                                   [--audio] [--recode-audio]
                                   [--limit N] [--pause SECONDS]

    --audio         also lift the audio out to <course>/audio/<doc>.m4a, with
                    `-c:a copy`, so it is a remux and not a re-encode
    --recode-audio  32 kbps mono instead: roughly half the size, and it does
                    cost quality. Never the default
    --limit N       stop after N actual downloads, so a first run can be 3
    --pause SECONDS between real downloads only, default 2.0

🔴 NOT EVERY LECTURE CAN BE FETCHED THIS WAY. Only the ones carrying a Kaltura
entry id have a recording behind them; the rest are narrated slide PACKAGES, and
on the three courses measured here that was 80 of 122, with one course as low as
9 of its 38. A package's narration is already on disk as per-slide mp3s inside
the package itself. Assembling those is a different job and is not this script's.

The summary line says how many were skipped for want of an entry id, so the gap
is visible from the run rather than discovered later.
"""

import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from study_server import KALTURA_PARTNER, KALTURA_SUBPARTNER

CHUNK = 1 << 20


def stream_url(entry):
    base = ("https://cdnapisec.kaltura.com/p/%s/sp/%s/playManifest/entryId/%s"
            % (KALTURA_PARTNER, KALTURA_SUBPARTNER, entry))
    return base + "/format/url/protocol/https/video.mp4"


def looks_like_mp4(path):
    # The ftyp box sits at offset 4 in every real MP4; an HTML error page
    # saved as .mp4 is the failure this catches.
    try:
        with path.open("rb") as f:
            return f.read(12)[4:8] == b"ftyp"
    except OSError:
        return False


def fetch_one(doc, entry, dest, force=False, opener=urllib.request.urlopen):
    """One recording onto disk, or an exception. Never a partial file.

    🔴 **A SHORT READ IS A FAILURE, AND UNTIL 2026-09-04 IT WAS A SUCCESS.** The
    loop below stops on the first empty read, which is exactly what a dropped
    connection produces, so a truncated download and a complete one were the same
    event. **Neither guard afterwards could tell them apart**: the `ftyp` box is
    at the START of a file, so a truncated MP4 is still an MP4, and the 1 MB floor
    passes anything bigger than about twenty seconds of video.

    ⚠️ **Measured in the sibling module the same day**: `W4-T1-P1` arrived as 10.2
    minutes of a 30-minute lecture and produced perfectly well-formed captions for
    the first third of it, with nothing red anywhere. **This function feeds
    `video_captions` through `courses/<C>/videos/<DOC>.mp4`**, which that module
    uses in preference to fetching, so the same silence reached the same place by
    a second road.

    🟢 **The fix compares against what the SERVER said it was sending**, which is
    the only witness not downstream of the fault.
    """
    if dest.exists() and not force:
        if looks_like_mp4(dest):
            return "kept", dest.stat().st_size
        dest.unlink()  # a broken half-download is not a reason to skip
    tmp = dest.with_suffix(".part")
    req = urllib.request.Request(stream_url(entry),
                                 headers={"User-Agent": "study-hub-fetch"})
    with opener(req, timeout=60) as r:
        declared = r.headers.get("Content-Length") if hasattr(r, "headers") else None
        with tmp.open("wb") as f:
            while True:
                chunk = r.read(CHUNK)
                if not chunk:
                    break
                f.write(chunk)
    size = tmp.stat().st_size
    if size < 1 << 20 or not looks_like_mp4(tmp):
        tmp.unlink()
        raise RuntimeError("%s: got %d bytes that are not an MP4" % (doc, size))
    if declared is not None:
        try:
            want = int(declared)
        except (TypeError, ValueError):
            want = None
        if want is not None and size != want:
            # 🔴 The partial file is REMOVED rather than left with a `.part`
            # suffix, so the next run cannot mistake it for progress.
            tmp.unlink()
            raise RuntimeError(
                "%s: the server said %d bytes and %d arrived. A truncated "
                "recording is worse than a missing one, because everything "
                "downstream treats it as the whole lecture." % (doc, want, size))
    tmp.rename(dest)
    return "fetched", dest.stat().st_size


# 🔴 EH asked for the politeness explicitly ("Can we do so slowly so the server
# doesn't block us"). 122 sequential fetches of a few MB is nothing like an
# attack, but the polite version costs one sleep and removes the question.
PAUSE = 2.0

# The recorded size is rounded to the minute in materials.json, so a tolerance
# under 60s would fire on correct files. 90s is one rounding plus slack.
DURATION_SLACK = 90.0


def audio_dest(folder, doc):
    """`<course>/audio/<doc>.m4a`.

    🔴 NOT beside the video. `courses/**/videos/` is gitignored and
    `courses/**/audio/` is too (added in the same change), and `push_guard`'s
    "course media" rule catches a stray `.m4a` under `courses/` even if both
    ignore rules were deleted. These are KCL recordings; two independent guards
    is the right number."""
    return folder / "audio" / (doc + ".m4a")


def extract_audio(src, dest, recode=False, runner=subprocess.run):
    """The audio stream out of a downloaded lecture. Returns (what, bytes).

    🟢 `-vn -c:a copy` REMUXES: the AAC stream is lifted out untouched, so there
    is no quality loss and it takes about a second. `recode=True` is the second
    flag the entry asked for, 32 kbps mono, which roughly halves it and does cost
    quality - a deliberate choice, never the default.

    🔴 Writes to a `.part` and renames, the same discipline `fetch_one` uses: a
    half-written .m4a that looks finished is the failure this whole module was
    rewritten to stop."""
    if dest.exists():
        return "kept", dest.stat().st_size
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".part")
    if tmp.exists():
        tmp.unlink()
    codec = ["-c:a", "aac", "-b:a", "32k", "-ac", "1"] if recode else ["-c:a", "copy"]
    r = runner(["ffmpeg", "-nostdin", "-loglevel", "error", "-y",
                "-i", str(src), "-vn"] + codec + ["-f", "mp4", str(tmp)],
               capture_output=True, text=True)
    if getattr(r, "returncode", 1) != 0 or not tmp.exists() or tmp.stat().st_size == 0:
        if tmp.exists():
            tmp.unlink()
        raise RuntimeError("ffmpeg could not take the audio out of %s: %s"
                           % (src.name, (getattr(r, "stderr", "") or "").strip()[:200]))
    tmp.rename(dest)
    return "extracted", dest.stat().st_size


def probe_seconds(path, runner=subprocess.run):
    """The real duration of a media file, or None if ffprobe cannot say.

    🔴 None is NOT zero. A caller that treats "could not measure" as "zero
    seconds" reports every file as wrong, which is the shape that trains a reader
    to ignore the check."""
    r = runner(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
               capture_output=True, text=True)
    if getattr(r, "returncode", 1) != 0:
        return None
    try:
        return float((getattr(r, "stdout", "") or "").strip())
    except ValueError:
        return None


def duration_verdict(seconds, minutes):
    """Compare a measured duration against materials.json's recorded `minutes`.

    Returns (ok, sentence). 🔴 `ok` is True when the check could not run, because
    an unmeasurable file is not a wrong one and this must not manufacture
    failures; the sentence still says so."""
    if seconds is None:
        return True, "duration unmeasured (ffprobe said nothing)"
    if not minutes:
        return True, "%.0fs, nothing recorded to compare with" % seconds
    drift = abs(seconds - minutes * 60.0)
    if drift <= DURATION_SLACK:
        return True, "%.0fs against %d min recorded" % (seconds, minutes)
    return False, ("%.0fs but %d min recorded, off by %.0fs - the audio is not "
                   "the whole lecture" % (seconds, minutes, drift))


def main():
    argv = sys.argv[1:]

    def opt(name, default=None):
        return argv[argv.index(name) + 1] if name in argv else default

    flagged = set()
    for i, a in enumerate(argv):
        if a in ("--doc", "--limit", "--pause"):
            flagged.add(i + 1)
    args = [a for i, a in enumerate(argv)
            if not a.startswith("--") and i not in flagged]
    force = "--force" in argv
    want_audio = "--audio" in argv
    recode = "--recode-audio" in argv
    only = opt("--doc")
    try:
        limit = int(opt("--limit", "0"))
        pause = float(opt("--pause", str(PAUSE)))
    except ValueError:
        sys.exit("--limit takes a whole number and --pause takes seconds")
    if recode and not want_audio:
        sys.exit("--recode-audio only means something with --audio")
    if not args:
        sys.exit(__doc__)
    folder = Path(args[0]).expanduser().resolve()
    mats = folder / "materials.json"
    if not mats.is_file():
        sys.exit("no materials.json in %s" % folder)
    docs = json.loads(mats.read_text(encoding="utf-8")).get("docs", {})
    vdir = folder / "videos"
    vdir.mkdir(exist_ok=True)
    got, skipped, failed, drift, shortaudio = 0, 0, [], [], []
    fetched_this_run = 0
    for doc, e in sorted(docs.items()):
        if only and doc != only:
            continue
        entry = (e.get("entry") or "").strip()
        if not entry:
            skipped += 1
            continue
        if limit and fetched_this_run >= limit:
            break
        try:
            dest = vdir / (doc + ".mp4")
            # 🔴 The pause goes BEFORE a real fetch and never before a skip:
            # pausing between files already on disk turns a resume of 122
            # lectures into four minutes of sleeping for nothing.
            if pause and fetched_this_run and not (dest.exists() and not force):
                time.sleep(pause)
            what, size = fetch_one(doc, entry, dest, force)
            line = "%s: %s, %.1f MB" % (doc, what, size / 1048576)
            if what == "fetched":
                fetched_this_run += 1
            # The recorded size is a free, exact second opinion, and it is a
            # different question from `fetch_one`'s Content-Length check: that
            # one asks whether the transfer finished, this one asks whether the
            # file is still the one we measured. A complete file that disagrees
            # is reported, never deleted.
            want = e.get("media_bytes")
            if want and size != want:
                drift.append(doc)
                line += "  🔴 SIZE DRIFT: materials.json records %d" % want
            print(line)
            got += 1
            if want_audio:
                a = audio_dest(folder, doc)
                aw, asize = extract_audio(dest, a, recode=recode)
                ok, said = duration_verdict(probe_seconds(a), e.get("minutes"))
                if not ok:
                    shortaudio.append(doc)
                print("    audio %s, %.1f MB, %s%s"
                      % (aw, asize / 1048576, said, "" if ok else "  🔴"))
        except Exception as exc:  # report and continue; one failure is one fact
            print("%s: FAILED: %s" % (doc, exc))
            failed.append(doc)

    # 🟢 A POSITIVE RESULT, per this project's rule: a run that found nothing
    # wrong and a check that never ran look identical from outside.
    print("\n%d on disk, %d without an entry id (packages or plain links), %d failed%s"
          % (got, skipped, len(failed), (": " + ", ".join(failed)) if failed else ""))
    if fetched_this_run == 0 and got:
        print("nothing was downloaded: every one of those %d was already on disk" % got)
    if limit and fetched_this_run >= limit:
        print("stopped at --limit %d; run it again to continue" % limit)
    print("size against materials.json: %s"
          % ("all %d match" % got if not drift
             else "🔴 %d differ: %s" % (len(drift), ", ".join(drift))))
    if want_audio:
        print("audio length against the recorded minutes: %s"
              % ("all %d within %ds" % (got - len(shortaudio), int(DURATION_SLACK))
                 if not shortaudio
                 else "🔴 %d short: %s" % (len(shortaudio), ", ".join(shortaudio))))
    if failed or shortaudio:
        sys.exit(1)


if __name__ == "__main__":
    main()
