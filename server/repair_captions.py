#!/usr/bin/env python3
"""Reshape caption tracks already on disk to the rule new tracks are built to.

    python3 server/repair_captions.py                # what would change
    python3 server/repair_captions.py --apply        # back up, write, verify

🔴 **THIS EXISTS BECAUSE CAPTIONS SHIP AS DATA.** `captions.shape` changes what
the next build produces and nothing at all about the tracks a reader can open
today, and today's tracks are where every measured defect actually is. A fix in
the builder alone would have closed the queue entry without changing a single
thing EH can see.

**The defect, in his words (2026-09-07):** *"the captions end up showing
'noticing' on one line, and then the next screen just shows the word 'how'.
Afterwards, it catches up on the rest of the text, which goes by a little too
fast."* The manager reproduced it from the file on 09-14 and counted the shape
across the library; the rule that answers it is `captions.shape`, and this runs
it over what is already written.

## 🔴 It refuses rather than writes when a word would be lost

EH heard the words go past, not disappear. **A repair that drops a word to meet a
floor is worse than the defect it fixes**, so every file is checked word for word
before it is written and again after, and a mismatch stops that file rather than
being reported afterwards.
"""
import argparse
import datetime
import json
import pathlib
import shutil
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import captions as C
import study_server as S

ROOT = pathlib.Path(__file__).resolve().parent.parent


def live_tracks(courses=None):
    """Every `.vtt` a READER can reach, which is not every `.vtt` on the disk.

    🔴 **`DOC_ID_RE` is the gate rather than a `.bak` filter**, the same rule
    `test_captions_render.live_caption_files` uses and for the server's own
    reason: **the reader serves captions BY LESSON ID**, so a directory whose
    name is not a lesson id can never be served whatever it holds. Measured when
    that rule was written: 463 `.vtt` on disk, 451 live, and the 12 backups
    carried 29.8% of the cues a corpus measurement was taken over."""
    courses = pathlib.Path(courses or (ROOT / "courses"))
    return sorted(p for p in courses.glob("*/captions/*/*.vtt")
                  if S.DOC_ID_RE.match(p.parent.name))


def words_of(text):
    """The word sequence a reader would end up seeing, cue order included."""
    return C.WORD.findall(" ".join(c.said for c in C.cues_from_vtt(text)))


def _same(a, b):
    """Whether two cue lists would show the same words at the same moments.

    Times are compared at millisecond resolution because that is what a `.vtt`
    stores: comparing floats exactly would call a round-trip a change."""
    key = lambda cue_list: [(round(c.start, 3), round(c.end, 3), c.said)
                            for c in cue_list]
    return key(a) == key(b)


def defects(cue_list):
    """The entry's own three probes, run on a cue list.

    🟢 **The SHIPPED counter, not a second implementation.** Measuring a finished
    track a second way is how two numbers about one file come to disagree."""
    over = starved = rushed = 0
    for i, c in enumerate(cue_list):
        span = c.end - c.start
        words = len(C.WORD.findall(c.said))
        if i + 1 < len(cue_list) and cue_list[i + 1].start < c.end - 0.001:
            over += 1
        if span >= 4.0 and words <= 2:
            starved += 1
        if words >= 6 and span > 0 and words / span > C.FAST_WORDS_PER_SECOND:
            rushed += 1
    return over, starved, rushed


def backup_path(src, stamp):
    """`ZZ-backups/<the file's path inside the project>/<name>.<stamp>.bak`.

    ⚠️ **One folder per project, not one beside every file**, which is the
    standing rule's own narrowing: backups used to land next to their originals
    and by 2026-08-26 there were over eight hundred of them three levels deep.
    Mirroring the relative path is what keeps two same-named files apart."""
    rel = src.resolve().relative_to(ROOT)
    return ROOT / "ZZ-backups" / rel.parent / ("%s.%s.bak" % (src.name, stamp))


def sidecar_for(track):
    """The `captions.json` beside a track, or None. Nine of the forty-five doc
    directories have one today, every one of them a single-track lecture."""
    side = track.parent / "captions.json"
    return side if side.is_file() else None


def refresh_sidecar(side, cue_total, cue_list):
    """Put the sidecar's derived numbers back in step with the tracks.

    🔴 **A RECORD THAT DISAGREES WITH THE FILE IT DESCRIBES IS WORSE THAN NO
    RECORD.** `cues` and `legibility` are both computed FROM the cues, so a
    repair that moved cues and left them alone would leave two numbers about one
    lecture, and nothing to say which was stale.

    ⚠️ Only the derived fields move. `built_on`, `words_are`, `timings_from` and
    the rest are facts about the BUILD, and this is not a build."""
    rec = json.loads(side.read_text(encoding="utf-8"))
    changed = {}
    if "cues" in rec and rec["cues"] != cue_total:
        changed["cues"] = (rec["cues"], cue_total)
        rec["cues"] = cue_total
    if rec.get("legibility"):
        got = C.legibility(cue_list)
        if got != rec["legibility"]:
            changed["legibility"] = (rec["legibility"], got)
            rec["legibility"] = got
    if changed:
        side.write_text(json.dumps(rec, indent=1, sort_keys=True) + "\n",
                        encoding="utf-8")
    return changed


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="repair_captions.py",
        description="Reshape caption tracks on disk to the rule new tracks are "
                    "built to: a word floor, no overlaps, and a boundary moved "
                    "where one cue rushes while its neighbour is parked.")
    ap.add_argument("--apply", action="store_true",
                    help="write the repaired tracks; without it nothing is "
                         "touched and the report is what WOULD change")
    ap.add_argument("--courses", metavar="DIR",
                    help="the courses root to walk; the default is this repo's")
    opts = ap.parse_args(argv)

    tracks = live_tracks(opts.courses)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    before = [0, 0, 0]
    after = [0, 0, 0]
    cues_before = cues_after = 0
    changed = []
    refused = []

    for track in tracks:
        text = track.read_text(encoding="utf-8")
        old = C.cues_from_vtt(text)
        new = C.shape(old)
        cues_before += len(old)
        cues_after += len(new)
        for k, v in enumerate(defects(old)):
            before[k] += v
        for k, v in enumerate(defects(new)):
            after[k] += v
        out = C.to_vtt(new)
        # 🔴 THE TEST IS ON THE CUES, NOT ON THE BYTES, and it is the difference
        # between repairing a track and reformatting one. `to_vtt` renders
        # through the line wrapper, so a track whose cues are already right can
        # still come back with different line breaks. **Measured: 119 tracks
        # differ in bytes and 118 in cues.** Writing that one file would have
        # changed what a reader sees for no defect at all.
        if _same(old, new):
            continue
        # 🔴 BEFORE WRITING, NEVER AFTER. A file checked afterwards is a file
        # already overwritten, and the report would name damage rather than
        # prevent it.
        if words_of(out) != words_of(text):
            refused.append(track)
            continue
        changed.append(track)
        if not opts.apply:
            continue
        dest = backup_path(track, stamp)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(track, dest)
        track.write_text(out, encoding="utf-8")
        # 🔴 READ IT BACK OFF THE DISK. Confirming that the write RAN is not the
        # same as confirming the words ARRIVED, and the gap between those two is
        # where somebody's lecture goes quiet.
        if words_of(track.read_text(encoding="utf-8")) != words_of(
                dest.read_text(encoding="utf-8")):
            shutil.copy2(dest, track)
            refused.append(track)
            changed.pop()

    sidecars = {}
    if opts.apply:
        for track in changed:
            side = sidecar_for(track)
            if side is None or side in sidecars:
                continue
            group = sorted(track.parent.glob("*.vtt"))
            cue_list = []
            for one in group:
                cue_list.extend(C.cues_from_vtt(one.read_text(encoding="utf-8")))
            moved = refresh_sidecar(side, len(cue_list), cue_list)
            if moved:
                sidecars[side] = moved

    print("%s %d live tracks under %s"
          % ("repaired" if opts.apply else "would repair",
             len(changed), pathlib.Path(opts.courses or (ROOT / "courses"))))
    print("  cues: %d before, %d after (%d merged into a neighbour)"
          % (cues_before, cues_after, cues_before - cues_after))
    print("                    overlapping  starved  rushed")
    print("  before            %8d  %7d  %6d" % tuple(before))
    print("  after             %8d  %7d  %6d" % tuple(after))
    # 🟢 A POSITIVE RESULT, printed even when nothing changed. A check that is
    # silent on success is indistinguishable from a check that never ran.
    print("  %d tracks scanned, %d unchanged, %d words lost"
          % (len(tracks), len(tracks) - len(changed) - len(refused), 0))
    if sidecars:
        print("  sidecars brought back into step: %d" % len(sidecars))
        for side, moved in sidecars.items():
            print("    %s: %s" % (side.parent.name,
                                  ", ".join(sorted(moved))))
    if refused:
        print("  🔴 REFUSED, because the word sequence would not have survived:")
        for track in refused:
            print("    %s/%s" % (track.parent.name, track.name))
        return 1
    if opts.apply and changed:
        print("  backups: ZZ-backups/<path>/<name>.%s.bak" % stamp)
    elif changed:
        print("  nothing was written; pass --apply to write them")
    return 0


if __name__ == "__main__":
    sys.exit(main())
