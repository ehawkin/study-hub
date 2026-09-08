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
"""

import json
import sys
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


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    force = "--force" in sys.argv
    only = None
    if "--doc" in sys.argv:
        only = sys.argv[sys.argv.index("--doc") + 1]
    if not args:
        sys.exit(__doc__)
    folder = Path(args[0]).expanduser().resolve()
    mats = folder / "materials.json"
    if not mats.is_file():
        sys.exit("no materials.json in %s" % folder)
    docs = json.loads(mats.read_text(encoding="utf-8")).get("docs", {})
    vdir = folder / "videos"
    vdir.mkdir(exist_ok=True)
    got, skipped, failed = 0, 0, []
    for doc, e in sorted(docs.items()):
        if only and doc != only:
            continue
        entry = (e.get("entry") or "").strip()
        if not entry:
            skipped += 1
            continue
        try:
            what, size = fetch_one(doc, entry, vdir / (doc + ".mp4"), force)
            print("%s: %s, %.1f MB" % (doc, what, size / 1048576))
            got += 1
        except Exception as exc:  # report and continue; one failure is one fact
            print("%s: FAILED: %s" % (doc, exc))
            failed.append(doc)
    print("\n%d on disk, %d without an entry id (packages or plain links), %d failed%s"
          % (got, skipped, len(failed), (": " + ", ".join(failed)) if failed else ""))
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
