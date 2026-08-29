#!/usr/bin/env python3
"""Extend notes/glossary.json with the W3 T3 definitions.

Same rules as the build of 2026-08-12: one key per <dt>, duplicate terms merged
rather than dropped, an alias key for anything whose key carries a parenthetical
abbreviation, and seeAlso computed by finding other glossary terms inside each
definition, longest first and word-bounded.

    python3 server/glossary_extend.py 'W3-T3-P*.html' --module PSY101
    python3 server/glossary_extend.py 'W3-T3-P*.html' --module PSY101 --write

🔴 Name the course. Without --module it uses the configured default, which on
a machine with more than one course is whichever is configured, not whichever
you are working on.

Idempotent: a term already carrying the same definition is left alone, so a
second run over the same notes reports nothing to do.
"""
import html as html_mod
import json
import pathlib
import re
import shutil
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))


def resolve_module(module, config):
    """The course folder to harvest into, named rather than guessed.

    🔴 This used to call `split_lessons.default_module_dir`, which returns the
    FIRST course folder. On a machine with two courses that silently harvested
    one course's terms into the other's glossary and reported "added 0", which
    reads like "nothing new" rather than like "wrong course". Every other script
    takes `--module`; this one now does too, and refuses an unknown code instead
    of falling back to a default that is right for one course and wrong for the
    next.
    """
    import study_server as S
    cfg = S.load_config(Path(config).expanduser())
    mods = S.resolve_modules(cfg)
    mid = module or S.default_module(cfg)
    if mid not in mods:
        raise SystemExit("no course called %r. There %s: %s"
                         % (mid, "is" if len(mods) == 1 else "are",
                            ", ".join(sorted(mods)) or "none yet"))
    return mid, mods[mid]
TAG = re.compile(r"<[^>]+>")
PAIR = re.compile(r"<dt>(.*?)</dt>\s*<dd>(.*?)</dd>", re.S)


def plain(frag):
    return re.sub(r"\s+", " ", html_mod.unescape(TAG.sub("", frag))).strip()


def words(x):
    return set(re.findall(r"[a-z0-9]+", x.lower()))


def covered(new, old):
    """True if the stored definition already says what the new one says.

    A plain substring test is not enough once an entry has been edited by hand:
    the merged text gets reworded, and the next run appends the same definition
    again. Content-word overlap survives that.
    """
    n = words(new)
    return bool(n) and len(n & words(old)) / len(n) >= 0.85


def see_also(term, text, terms, parent=None):
    """Other glossary terms named inside this definition, plus, for an alias key,
    the term it is an alias of. Singular and plural of the same word are one term
    to the lookup, so they never cross-reference each other."""
    def same(a, b):
        a, b = a.lower(), b.lower()
        return a == b or a.rstrip("s") == b.rstrip("s")

    out = [parent] if parent else []
    for other in terms:
        if same(other, term) or other in out:
            continue
        if re.search(r"\b%s\b" % re.escape(other), text, re.I):
            out.append(other)
    return sorted(out, key=str.lower)


def main():
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("pattern", nargs="?", default="W3-T3-P*.html",
                    help="which lessons to fold in, as a glob")
    # 🔴 Required, not defaulted (EH approved retrospective item 8, 2026-08-23):
    # run bare, the old default silently harvested into the CONFIGURED course,
    # which on this machine meant one course quietly receiving another's terms.
    # A write into the wrong course looks exactly like success.
    ap.add_argument("--module", required=True,
                    help="which course the terms belong to (explicit, always)")
    ap.add_argument("--config", default="", help="path to the config (default: the configured one)")
    ap.add_argument("--write", action="store_true", help="write it; otherwise a dry run")
    ap.add_argument("--stamp-only", action="store_true",
                    help="record which lesson a term came from, without merging text")
    args = ap.parse_args()

    import study_server as S
    mid, module_dir = resolve_module(args.module, args.config or S.CONFIG_PATH)
    gloss_path = module_dir / "glossary.json"
    new_files = sorted(p for p in module_dir.glob(args.pattern) if ".bak" not in p.name)
    print("course %s, %d lesson%s matching %s"
          % (mid, len(new_files), "" if len(new_files) == 1 else "s", args.pattern))
    if not new_files:
        raise SystemExit("no lessons in %s match %r, so there is nothing to harvest"
                         % (module_dir, args.pattern))

    write = args.write
    # A course that has never been harvested has no glossary yet. Creating it is
    # the obvious thing; the old code raised FileNotFoundError, which reads like
    # a broken install rather than like a first run.
    if not gloss_path.exists():
        print("no glossary yet in %s; starting one" % mid)
        gloss = {}
    else:
        gloss = json.loads(gloss_path.read_text(encoding="utf-8"))
    before = len(gloss)

    stamp_only = args.stamp_only
    added, merged, aliased, parents = [], [], [], {}
    for p in new_files:
        code = re.match(r"(W\d+-T\d+-P\d+)", p.name).group(1)
        for dt, dd in PAIR.findall(p.read_text(encoding="utf-8")):
            term, text = plain(dt), plain(dd)
            if not term or not text:
                continue
            if term in gloss:
                # A note already folded in is never folded in again, however the
                # entry has been edited since. Similarity alone cannot do this:
                # a merged definition that is later tightened by hand stops
                # matching the note it came from.
                seen = gloss[term].setdefault("from", [])
                if code not in seen:
                    old = gloss[term].get("text", "")
                    if not (stamp_only or covered(text, old)):
                        gloss[term]["text"] = old.rstrip() + " " + text
                        merged.append(term)
                    seen.append(code)
                    seen.sort()
            else:
                gloss[term] = {"text": text, "from": [code]}
                added.append(term)

            m = re.match(r"^(.*?)\s*\(([A-Za-z0-9\-]{2,6})\)$", term)
            if m:
                for alias in (m.group(1).strip(), m.group(2).strip()):
                    # Two-letter aliases match ordinary English ("AN" inside "an"),
                    # so they are never worth a key.
                    if len(alias) >= 3 and alias not in gloss:
                        gloss[alias] = {"text": gloss[term]["text"], "from": [code]}
                        aliased.append(alias)
                        parents[alias] = term

    terms = list(gloss)
    touched = set(added) | set(merged) | set(aliased)
    for term in touched:
        rel = see_also(term, gloss[term]["text"], terms, parents.get(term))
        if rel:
            gloss[term]["seeAlso"] = rel
        else:
            gloss[term].pop("seeAlso", None)

    # An untouched entry keeps its stored seeAlso, and gains only links to terms
    # that did not exist before this run. Recomputing the rest would churn 132
    # entries to no purpose, and the stored links carry alias-to-parent pairs the
    # rule cannot derive.
    gained = []
    for term, entry in gloss.items():
        if term in touched:
            continue
        extra = [t for t in see_also(term, entry["text"], added) if t not in entry.get("seeAlso", [])]
        if extra:
            entry["seeAlso"] = sorted(entry.get("seeAlso", []) + extra, key=str.lower)
            gained.append("%s -> %s" % (term, ", ".join(extra)))

    # Sanity: the same rule, run over an untouched entry, must reproduce what is
    # already stored. A mismatch means the algorithm has drifted from the build.
    drift = []
    for term, entry in gloss.items():
        if term in touched:
            continue
        want = see_also(term, entry["text"], terms)
        have = entry.get("seeAlso", [])
        if want != have:
            drift.append((term, have, want))

    print("added %d, merged %d, aliases %d, total %d -> %d"
          % (len(added), len(merged), len(aliased), before, len(gloss)))
    print("added:  " + ", ".join(sorted(added)))
    print("merged: " + ", ".join(sorted(merged)))
    print("alias:  " + ", ".join(sorted(aliased)))
    print("gained: " + "; ".join(gained))
    print("\nseeAlso drift on untouched entries (diagnostic only, not written): %d" % len(drift))
    for term, have, want in drift[:12]:
        print("  %-34s have=%s want=%s" % (term, have, want))

    if write:
        from split_lessons import backup_target
        if gloss_path.exists():
            shutil.copy2(gloss_path, backup_target(
                gloss_path, gloss_path.name + "." + time.strftime("%Y%m%d-%H%M%S") + ".bak"))
        out = {k: gloss[k] for k in sorted(gloss, key=lambda s: s.lower())}
        gloss_path.write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n",
                              encoding="utf-8")
        print("\nwritten to %s" % gloss_path)


main()
