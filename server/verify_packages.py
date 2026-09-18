#!/usr/bin/env python3
"""Every asset a mirrored package references is a file that is actually there.

    python3 server/verify_packages.py                 # every course
    python3 server/verify_packages.py PSY101          # one course

Why this exists. A narrated package is mirrored out of the course site by
`mirror-package.js`, and until 2026-08-29 it could report success while leaving
files behind. The failure was completely silent: a broken `<img>` logs nothing,
the player does not complain, and the mirror counted only what it fetched, never
what it had been asked for. Seven of 29 packages were damaged for weeks and it
took EH opening a slide to notice.

So this is the gate that would have caught it: parse every text file in a package
for the assets it references, and diff that against what is on disk. It reads
files only. Nothing here fetches, and nothing here can repair a package: a
re-mirror needs a signed-in browser session and is a job for a person.
"""

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# 🔴 A reference is a QUOTED string, and that is not a detail. The first draft
# of this gate matched any `name.ext` shape, exactly as `mirror-package.js`'s
# own `refRe` does, and reported 323 missing assets across all 38 packages
# against QA's measured 19 across 7. Almost every extra was a property chain in
# minified JavaScript: `this.js`, `e.svg`, `f.js`, `b.Fn.svg`, `rq.prototype.js`.
# Those are code, not files. Requiring quotes drops them and keeps every real
# reference, because a real one is always written as a string or a css url().
# 🔴 Do NOT try to pair quotes. A slide file is one 6000-character line of HTML
# embedded in JavaScript, with double-quoted attributes and single-quoted ones
# side by side; any scanner that walks quote-to-quote drifts out of parity and
# swallows whole regions. Two earlier drafts of this gate found ZERO references
# in a file that plainly contains src="data/img22.jpg", and the second one
# reported every package clean, which is the dangerous direction for a gate.
#
# So match the asset token itself and require a DELIMITER either side. That is
# parity-free: it cannot be thrown off by what came before it on the line.
DELIM_L = r"""(?<=["'`(=\s])"""
DELIM_R = r"""(?=["'`)\s>])"""
ASSET_BODY = (r"[A-Za-z0-9_][A-Za-z0-9_\-./]*"
              r"\.(?:js|css|png|jpe?g|gif|svg|woff2?|ttf|otf|eot|mp3|m4a|wav|mp4|webm|ico)")
REF = re.compile(DELIM_L + "(" + ASSET_BODY + ")" + DELIM_R, re.I)
TEXT = re.compile(r"\.(html?|js|css|xml|json|svg)$", re.I)


def refs_in(text):
    """Every asset-looking path written as a delimited literal in this file."""
    return {m.group(1).strip() for m in REF.finditer(text)}


def referenced(pkg):
    """Every referenced asset that is NOT on disk, named relative to the package."""
    missing = set()
    pkg_r = pkg.resolve()
    for f in sorted(pkg.rglob("*")):
        if not f.is_file() or not TEXT.search(f.name):
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        here = f.parent
        for ref in refs_in(text):
            if ref.startswith("/") or "//" in ref or ".." in ref:
                continue
            # 🔴 A reference with no folder in it is assembled at RUNTIME from a
            # base this gate cannot know, and is not a claim that a file sits
            # beside the code. The worked example is `wj(this.Kh,
            # "btn_play_big.svg")` in the minified player: it is written in all
            # 38 mirrored packages and present in none of them, including the
            # one QA measured over HTTP as 53 of 53 serving. Something absent
            # from every mirror is a property of the format, not damage. Every
            # real reference in this format carries `data/`.
            if "/" not in ref:
                continue
            # These packages write `data/x.png` from index.html and plain
            # `x.png` from inside data/, so a reference resolves against
            # either its own folder or the package root. Present in either
            # place is present.
            cands = [here / ref, pkg / ref]
            if any(c.is_file() for c in cands):
                continue
            # Name it the way a person would look for it. A ref carrying a
            # folder is almost always package-root relative in this format.
            pick = (pkg / ref) if "/" in ref else (here / ref)
            try:
                missing.add(str(pick.resolve().relative_to(pkg_r)))
            except ValueError:
                missing.add(ref)
    return missing


# 🔴 The one thing a reference diff cannot see. This gate answers "is every
# referenced asset on disk", and a format that names none of its assets in its
# source references nothing, so it passes with a package that holds no course
# content whatsoever. Measured 2026-09-09 against a live Articulate Rise week:
# `mirror-package.js`'s own algorithm collected 6 files and 370KB of player
# shell, zero images, zero audio, and reported 4 missing code bundles. Both this
# gate and that report would have called it fine.
#
# The floor is ZERO, not a number off today's corpus: the 43 mirrored packages
# carry 20 media files at the fewest, but pinning 20 would fail a legitimately
# short package, while "a narrated slide package with no image, no audio and no
# video in it" is wrong at every size.
MEDIA = re.compile(r"\.(png|jpe?g|gif|svg|webp|mp3|m4a|wav|mp4|webm)$", re.I)


def media_count(pkg):
    """How many image, audio or video files this package actually holds."""
    return sum(1 for f in pkg.rglob("*") if f.is_file() and MEDIA.search(f.name))


def packages(course_dir):
    root = course_dir / "packages"
    if not root.is_dir():
        return []
    return [p for p in sorted(root.iterdir()) if p.is_dir()]


def main(argv):
    courses = REPO / "courses"
    wanted = argv[1:] or [d.name for d in sorted(courses.iterdir()) if d.is_dir()]
    total_pkgs = 0
    damaged = {}
    empty = []
    for code in wanted:
        cdir = courses / code
        if not cdir.is_dir():
            continue
        for pkg in packages(cdir):
            total_pkgs += 1
            name = "%s/%s" % (code, pkg.name)
            if not media_count(pkg):
                empty.append(name)
            missing = referenced(pkg)
            if missing:
                damaged[name] = sorted(missing)

    if not total_pkgs:
        print("no mirrored packages found")
        return 0

    if empty:
        print("  🔴 %d of %d packages hold no image, audio or video at all, so "
              "they are\n  not narrated slide packages whatever else is in them:\n"
              % (len(empty), total_pkgs))
        for name in sorted(empty):
            print("  %s" % name)
        print("\n  An Articulate Rise week mirrors to exactly this shape. It "
              "cannot be\n  mirrored: use await window.__fetchCourse() on the "
              "course page instead.")

    if not damaged:
        # 🟢 Say the count even when it is zero, so this gate's own blindness is
        # visible rather than inferred from silence.
        print("  clean: %d packages, every referenced asset is on disk, "
              "%d hold content." % (total_pkgs, total_pkgs - len(empty)))
        return 1 if empty else 0

    n = sum(len(v) for v in damaged.values())
    print("  🔴 %d of %d packages are incomplete, %d assets missing:\n"
          % (len(damaged), total_pkgs, n))
    for name in sorted(damaged):
        print("  %s" % name)
        for p in damaged[name]:
            print("      %s" % p)
    print("\n  A package cannot be repaired from here: re-mirroring needs a "
          "signed-in\n  browser session. This is a report, not a fix.")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
