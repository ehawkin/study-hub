#!/usr/bin/env python3
"""One listenable file per narrated lecture, assembled from the slides' own mp3s.

    python3 server/package_audio.py --course <CODE>            # every package
    python3 server/package_audio.py --course <CODE> --doc W2-T3-P1
    python3 server/package_audio.py --course <CODE> --dry-run  # say what it would do

🔴 **WHY THIS EXISTS.** `fetch_videos.py` can only reach a lecture that carries a
Kaltura entry id, and 42 of 122 do not: they are narrated slide packages with no
recording behind them. One course is 9 of its 38, so "let me listen to them on
the go" was three-quarters unmet there. **Their narration was on the disk the
whole time**, one mp3 per slide inside the package, needing no network at all.

🟢 **ONE FILE PER LECTURE, and that is the whole scope.** EH's words: *"downloading
the audios so I can listen to them on the go as audio files without using
Speechify."* The version with per-slide timings, so the audio can sit beside the
reader and follow the slides, is a much larger thing and nobody has asked for it.

⚠️ **A SLIDE WITH NO NARRATION CONTRIBUTES NOTHING**, rather than contributing
silence. For a continuous listen that is right: silence tells a listener nothing
and there are no per-slide timings here for it to keep aligned with. It would be
the wrong choice for the larger version above, which is a reason to decide it
again then rather than to inherit this.

🔴 **THE RISK IS ORDER, AND THE DURATION CHECK CANNOT SEE IT.** `sound1 … sound10`
sorts wrong as text, and a lecture assembled in the wrong order sounds almost
right: the voice is continuous and the sentences are whole. **Concatenating the
same clips in any order gives the same total duration**, so the duration proof
this module also runs is blind to it by construction. It catches a DROPPED or an
EXTRA part and nothing else, which is worth saying out loud because the entry
that commissioned this called it "the check that catches a dropped or mis-ordered
part".

🟢 **SO ORDER IS TAKEN FROM TWO INDEPENDENT WITNESSES AND THEY MUST AGREE**: the
package's own `index.html`, which names each clip where it is used, and the
numeric order of the files on disk. Measured 2026-09-12 across all 43 packages on
this machine: they agree everywhere, and the sets match exactly. **A disagreement
means one of the two is wrong, and this refuses rather than guessing**, because
guessing produces exactly the failure that sounds almost right.
"""
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fetch_videos as FV                                # noqa: E402

CLIP = re.compile(r"^(?P<stem>.*?)(?P<n>\d+)\.mp3$")
IN_HTML = re.compile(r"([A-Za-z_-]*?\d+\.mp3)")


# 🔴 WHAT THE ASSEMBLY IS CHECKED AGAINST, and the first answer was wrong.
#
# The entry that commissioned this said to compare the result with
# `materials.json`'s recorded `minutes`, "the check that catches a dropped or
# mis-ordered part". **Run on real data it refused 4 of 9 good lectures.**
# Measured 2026-09-12: the assembled audio matches the SUM OF ITS OWN CLIPS to
# within half a second in every case, including all four refusals, so nothing was
# dropped. `minutes` describes the RECORDED LECTURE, and a narrated slide package
# is a different artefact from the lecture somebody sat through: W5-T2-P2 is 7
# clips of narration against a 19-minute lecture, and both numbers are correct.
#
# 🟢 So the proof is the sum of the clips, which is what this module can actually
# be wrong about: a dropped clip, a truncated encode, a half-written file.
# ⚠️ `minutes` is still REPORTED, because the gap is real information for a
# listener, but it no longer refuses anything.
ASSEMBLY_SLACK = 5.0


class OrderUnclear(Exception):
    """The two witnesses disagree, or a clip name carries no number.

    🔴 Raised rather than resolved. Every resolution available here is a guess,
    and a guessed order is the one failure this module cannot detect afterwards."""


def clip_key(name):
    """`(stem, number)` for `sound10.mp3`, so 2 sorts before 10.

    🔴 Refuses a name with no trailing number instead of sorting it somewhere.
    A file this cannot place is a file whose position in the lecture is unknown."""
    m = CLIP.match(name)
    if not m:
        raise OrderUnclear(
            "%r carries no trailing number, so nothing here knows where in the "
            "lecture it belongs" % name)
    return (m.group("stem"), int(m.group("n")))


def clips_on_disk(package):
    """Every mp3 in the package's data folder, in numeric order."""
    data = package / "data"
    if not data.is_dir():
        return []
    return sorted((p for p in data.glob("*.mp3")), key=lambda p: clip_key(p.name))


def clips_in_manifest(package):
    """The clip names the package's own `index.html` mentions, first use first.

    The second witness. It is the package's own account of which audio belongs to
    it, so it also catches an orphan file in `data/` that no slide ever plays."""
    index = package / "index.html"
    if not index.is_file():
        return []
    seen, order = set(), []
    for name in IN_HTML.findall(index.read_text(encoding="utf-8", errors="replace")):
        if name not in seen:
            seen.add(name)
            order.append(name)
    return order


def clip_order(package):
    """The agreed order, or `OrderUnclear`. Returns a list of paths."""
    disk = clips_on_disk(package)
    if not disk:
        return []
    manifest = clips_in_manifest(package)
    if not manifest:
        # 🟡 One witness is not nothing: a package with no readable index still
        # has numbered files. It proceeds, and says so, because refusing here
        # would withhold a lecture over a missing cross-check rather than over a
        # disagreement.
        return disk
    if [p.name for p in disk] != manifest:
        raise OrderUnclear(
            "%s: the package's index.html and the files on disk disagree.\n"
            "  index.html: %s\n  on disk:    %s"
            % (package.name, ", ".join(manifest[:6]) or "none",
               ", ".join(p.name for p in disk[:6])))
    return disk


def concat_script(paths):
    """ffmpeg's concat demuxer list. Single quotes are escaped the way it wants."""
    out = []
    for p in paths:
        out.append("file '%s'" % str(p).replace("'", "'\\''"))
    return "\n".join(out) + "\n"


def clip_total(paths, probe):
    """The clips' own total duration, or None if any of them cannot be measured.

    🔴 None rather than a partial sum: a total missing one clip is a smaller
    number that still looks like an answer, and it would be compared against a
    file that legitimately contains that clip."""
    total = 0.0
    for p in paths:
        got = probe(p)
        if got is None:
            return None
        total += got
    return total


def assembly_verdict(seconds, expected):
    """Does the assembled file contain the clips it was built from?"""
    if seconds is None or expected is None:
        return True, "length unmeasured"
    drift = abs(seconds - expected)
    if drift <= ASSEMBLY_SLACK:
        return True, "%.0fs, all of it" % seconds
    return False, ("%.0fs but its clips total %.0fs, off by %.0fs - something "
                   "was dropped or the encode stopped early" % (seconds, expected, drift))


def recorded_minutes(folder, doc):
    try:
        docs = json.loads((folder / "materials.json").read_text(encoding="utf-8"))
        return (docs.get("docs", {}).get(doc) or {}).get("minutes")
    except (OSError, ValueError):
        return None


def assemble(package, dest, listfile, runner=subprocess.run):
    """Concatenate a package's clips into `dest`. Returns (paths, what happened).

    🔴 Writes to `<dest>.part` and renames only after the duration check passes,
    the same rule `fetch_videos` learned: a half-written `.m4a` looks finished in
    a file listing and plays as a truncated lecture."""
    paths = clip_order(package)
    if not paths:
        return [], "no narration in the package"
    listfile.write_text(concat_script(paths), encoding="utf-8")
    part = dest.with_suffix(dest.suffix + ".part")
    dest.parent.mkdir(parents=True, exist_ok=True)
    # 🔴 `-f ipod` NAMES THE CONTAINER, and it is not decoration: ffmpeg infers
    # the muxer from the extension, and the temp file ends in `.part`, which it
    # does not recognise ("Invalid argument", measured). The alternative was to
    # call the temp `<doc>.m4a.part.m4a` so the guess lands, which reintroduces
    # the thing the `.part` suffix exists to prevent: a half-written file that
    # reads as finished in a listing.
    got = runner(["ffmpeg", "-nostdin", "-y", "-f", "concat", "-safe", "0",
                  "-i", str(listfile), "-c:a", "aac", "-b:a", "64k",
                  "-f", "ipod", str(part)], capture_output=True, text=True)
    if getattr(got, "returncode", 1) != 0:
        if part.exists():
            part.unlink()
        return paths, "ffmpeg refused: %s" % (getattr(got, "stderr", "") or "")[-200:]
    return paths, part


def one(folder, doc, force=False, dry=False, runner=subprocess.run,
        probe=None, tmpdir=None):
    """Assemble one lecture. Returns a one-line report."""
    package = folder / "packages" / doc
    dest = FV.audio_dest(folder, doc)
    if dest.exists() and not force:
        # 🔴 A FETCHED RECORDING BEATS AN ASSEMBLY AND MUST NOT BE OVERWRITTEN.
        # `fetch_videos` writes to this same path, so on a lecture that has both
        # a Kaltura id and a package this would otherwise replace the real
        # lecture with a reconstruction of it. --force is deliberate and manual.
        return "%-10s skipped, %s already exists" % (doc, dest.name)
    try:
        paths = clip_order(package)
    except OrderUnclear as e:
        return "%-10s REFUSED: %s" % (doc, e)
    if not paths:
        return "%-10s no narration in the package" % doc
    if dry:
        return "%-10s would assemble %d clips -> %s" % (doc, len(paths), dest.name)
    tmp = (tmpdir or dest.parent) / (doc + ".concat.txt")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    paths, out = assemble(package, dest, tmp, runner=runner)
    tmp.unlink(missing_ok=True)
    if isinstance(out, str):
        return "%-10s %s" % (doc, out)
    probe = probe or FV.probe_seconds
    seconds = probe(out)
    ok, sentence = assembly_verdict(seconds, clip_total(paths, probe))
    if not ok:
        out.unlink(missing_ok=True)
        return "%-10s REFUSED: %d clips, %s" % (doc, len(paths), sentence)
    out.replace(dest)
    minutes = recorded_minutes(folder, doc)
    note = ""
    if seconds and minutes and (minutes * 60.0 - seconds) > FV.DURATION_SLACK:
        # 🟡 Information, not a fault. The slide narration being shorter than the
        # lecture it belongs to is the normal case, not a defect.
        note = ", %.0f min shorter than the %d min lecture" % (
            (minutes * 60.0 - seconds) / 60.0, minutes)
    return "%-10s %d clips, %.1f MB, %s%s" % (
        doc, len(paths), dest.stat().st_size / 1e6, sentence, note)


def packages(folder):
    root = folder / "packages"
    return sorted(p.name for p in root.glob("*") if p.is_dir()) if root.is_dir() else []


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)

    def opt(name, default=None):
        return argv[argv.index(name) + 1] if name in argv else default

    course = opt("--course")
    if not course:
        print(__doc__.strip().split("\n\n")[1])
        return 2
    repo = Path(__file__).resolve().parent.parent
    folder = repo / "courses" / course
    if not folder.is_dir():
        print("no course at %s" % folder)
        return 2
    docs = [opt("--doc")] if opt("--doc") else packages(folder)
    limit = int(opt("--limit", "0") or 0)
    if limit:
        docs = docs[:limit]
    dry, force = "--dry-run" in argv, "--force" in argv
    print("%s: %d package(s)%s" % (course, len(docs), ", dry run" if dry else ""))
    refused = skipped = done = 0
    for doc in docs:
        line = one(folder, doc, force=force, dry=dry)
        if "REFUSED" in line:
            refused += 1
        elif "skipped" in line or "no narration" in line:
            skipped += 1
        else:
            done += 1
        print("  " + line)
    # 🟢 A POSITIVE RESULT, per this project's own rule: silence on success and a
    # missing check look identical from outside.
    # 🔴 SKIPPED IS COUNTED SEPARATELY, because the first version of this line
    # said "9 assembled" on a run where 5 already existed and were left alone.
    # A summary that overstates its own work is the failure this project keeps
    # writing rules about, and it was in the line written to prevent it.
    print("%d %s, %d skipped, %d refused" % (
        done, "would be assembled" if dry else "assembled", skipped, refused))
    return 1 if refused else 0


if __name__ == "__main__":
    sys.exit(main())
