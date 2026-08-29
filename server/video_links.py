#!/usr/bin/env python3
"""Take a file of lecture-video links and put them where the reader looks.

🔴 This replaces the crawler I started on 2026-08-22 and EH stopped the same
day, and his reasoning is worth keeping because it is right: **a scraper cannot
work, because KEATS courses are not all built the same way.** Collecting the
material is a job for a person's own Claude session driving their own browser,
where a human can see when a page is shaped unexpectedly. What is left for a
program is the boring, exact half: take what that session wrote down, check it,
and merge it into this course's materials.

**Why this exists separately from the slides and transcripts.** Someone can
already have a folder of decks and transcripts, from a coursemate or from their
own downloads, and that folder contains **no video links at all**. The lecture
recording lives on KEATS and is played in the reader's own side panel. So this is
needed even when nothing else is.

**The field that matters is `entry`.** The reader plays a lecture from its Kaltura
entry id (`1_` and eight characters), which is what `materials_for` turns into a
playable URL. A KEATS permalink alone gets you an "open it on KEATS" button and
no player. Both are worth having; only one of them is the feature.

The file it reads is JSON, and that is what the prompt asks Claude for:

    {
      "videos": [
        {"doc": "W1-T1-P1",
         "title":   "What affective disorders are",
         "video":   "https://keats.kcl.ac.uk/mod/kalvidres/view.php?id=1234567",
         "entry":   "1_xxxxxxxx",
         "minutes": 9}
      ]
    }

Everything except `doc` is optional. Three other shapes are accepted without
complaint, because a model asked for JSON sometimes gives you a slightly
different JSON, and refusing on a technicality wastes the person's afternoon:
a bare list, a `{"docs": {...}}` map shaped like materials.json itself, and
tab or pipe separated lines.
"""

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import study_server as S                                        # noqa: E402
import split_lessons                                            # noqa: E402

ENTRY_RE = re.compile(r"\b(1_[a-z0-9]{8})\b", re.I)
DOC_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,63}$")
URL_RE = re.compile(r"^https?://", re.I)

# Only these move from the file into materials.json. Anything else in it is
# ignored rather than written: a file from a helpful model can carry a dozen
# invented fields, and this is not the place to find out what they meant.
KEEP = ("video", "video_kind", "video_embed", "entry", "minutes", "title")


class Problem(Exception):
    pass


def _one(raw, where):
    """One record, cleaned and checked. Returns None for something unusable, with
    the reason, rather than raising: a file of forty is not thrown away because
    one line is wrong."""
    if not isinstance(raw, dict):
        return None, "%s is not a record" % where
    out = {}
    doc = str(raw.get("doc") or raw.get("id") or raw.get("part") or "").strip()
    if not doc:
        return None, "%s has no doc id, so nothing knows which lesson it is for" % where
    if not DOC_RE.match(doc):
        return None, "%s has a doc id this cannot use: %r" % (where, doc[:30])

    entry = str(raw.get("entry") or raw.get("entry_id") or raw.get("entryId") or "").strip()
    # 🔴 Also mined out of the link itself. A Kaltura entry id is usually sitting
    # in the embed URL, and asking a person to copy it separately when it is
    # already in what they pasted is how a field ends up empty.
    if not entry:
        for field in ("video_embed", "video", "url", "link"):
            m = ENTRY_RE.search(str(raw.get(field) or ""))
            if m:
                entry = m.group(1)
                break
    if entry:
        if not ENTRY_RE.fullmatch(entry):
            return None, "%s: %r is not a Kaltura entry id (1_ then eight characters)" % (where, entry[:20])
        out["entry"] = entry.lower()

    for src, dst in (("video", "video"), ("url", "video"), ("link", "video"),
                     ("video_embed", "video_embed")):
        val = str(raw.get(src) or "").strip()
        if val and dst not in out:
            if not URL_RE.match(val):
                return None, "%s: %r is not a link" % (where, val[:40])
            out[dst] = val

    mins = raw.get("minutes", raw.get("duration"))
    if mins not in (None, ""):
        try:
            m = int(float(str(mins).split(":")[0]))
            if 0 < m < 1000:
                out["minutes"] = m
        except (TypeError, ValueError):
            pass

    title = str(raw.get("title") or raw.get("name") or "").strip()
    if title:
        out["title"] = title[:200]

    if not (out.get("entry") or out.get("video") or out.get("video_embed")):
        return None, "%s has a doc id and no link of any kind" % where
    # 🔴 A supplied video_kind is kept, and the default is earned rather than
    # assumed. The old default called ANY keats link "Activity permalink
    # (Kaltura)", and 7PAYFMND delivers 29 of 38 lectures as mod/resource slide
    # packages with no video anywhere, so the reader told the student each of
    # them was a Kaltura activity. Kaltura is claimed only on evidence of it:
    # an entry id, or kalvidres in the link itself.
    kind = str(raw.get("video_kind") or "").strip()
    if kind:
        out["video_kind"] = kind[:100]
    if out.get("video") and "video_kind" not in out:
        v = out["video"].lower()
        if out.get("entry") or "kalvidres" in v:
            out["video_kind"] = "Activity permalink (Kaltura)"
        elif "mod/resource" in v:
            out["video_kind"] = "KEATS slide package, no video id"
        else:
            out["video_kind"] = "Link"
    return (doc, out), ""


def parse(text):
    """Records out of whatever shape the file turned out to be.

    Returns (records, complaints). Never raises for content: a file that parses
    as nothing at all is the only hard failure, and that one is worth stopping
    for."""
    text = text.strip()
    if not text:
        raise Problem("that file is empty")

    rows = []
    try:
        data = json.loads(text)
    except ValueError:
        data = None

    if data is None:
        # Tab or pipe separated, one lesson per line, a markdown table pasted out
        # of a chat being the common case. The header row is skipped by being
        # unusable rather than by being detected.
        for n, line in enumerate(text.splitlines(), 1):
            line = line.strip().strip("|").strip()
            if not line or line.startswith("#") or set(line) <= set("-| :"):
                continue
            bits = [b.strip() for b in re.split(r"\t|\s*\|\s*", line) if b.strip()]
            if not bits:
                continue
            rec = {"doc": bits[0]}
            for b in bits[1:]:
                if ENTRY_RE.fullmatch(b):
                    rec["entry"] = b
                elif URL_RE.match(b):
                    rec["video"] = b
                elif re.fullmatch(r"\d{1,3}", b):
                    rec["minutes"] = b
                elif "title" not in rec:
                    rec["title"] = b
            rows.append((rec, "line %d" % n))
    elif isinstance(data, list):
        rows = [(r, "item %d" % i) for i, r in enumerate(data, 1)]
    elif isinstance(data, dict) and isinstance(data.get("videos"), list):
        rows = [(r, "item %d" % i) for i, r in enumerate(data["videos"], 1)]
    elif isinstance(data, dict) and isinstance(data.get("docs"), dict):
        rows = [(dict(v, doc=k), k) for k, v in data["docs"].items() if isinstance(v, dict)]
    elif isinstance(data, dict):
        # A bare map of doc id to link, which is the shortest thing anybody writes.
        rows = [({"doc": k, "video": v} if isinstance(v, str) else dict(v or {}, doc=k), k)
                for k, v in data.items() if k not in ("module", "built", "order", "parts")]
    else:
        raise Problem("this file is JSON, but not a shape this recognises")

    good, complaints = [], []
    for raw, where in rows:
        rec, why = _one(raw, where)
        if rec:
            good.append(rec)
        elif why:
            complaints.append(why)
    if not good:
        raise Problem("nothing usable in that file" +
                      (": " + complaints[0] if complaints else ""))
    return good, complaints


def merge(folder, records, overwrite=False):
    """Fold the links into this course's materials.json.

    🔴 Additive by default and it does not overwrite a field that is already
    filled. Someone who scanned KEATS themselves, or imported a pack that carried
    links, has better data than a later paste, and losing it silently is worse
    than doing nothing. `--overwrite` is there for the case where the paste IS
    the correction."""
    folder = Path(folder)
    path = folder / "materials.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("not an object")
    except (OSError, ValueError):
        data = {}
    docs = data.setdefault("docs", {})
    order = data.setdefault("order", [])
    if not isinstance(docs, dict) or not isinstance(order, list):
        raise Problem("%s is there but is not shaped like a materials file" % path)

    added = kept = playable = 0
    touched = []
    for doc, fields in records:
        entry = docs.setdefault(doc, {})
        if doc not in order:
            order.append(doc)
        changed = False
        for k in KEEP:
            if k not in fields:
                continue
            if k in entry and not overwrite:
                kept += 1
                continue
            if entry.get(k) != fields[k]:
                entry[k] = fields[k]
                added += 1
                changed = True
        if entry.get("entry") or entry.get("video_embed"):
            playable += 1
        if changed:
            touched.append(doc)

    data["parts"] = len(docs)
    data.setdefault("built", "video links collected in a browser, merged by video_links.py")
    folder.mkdir(parents=True, exist_ok=True)
    if path.exists():
        # A dated copy beside the original before overwriting it, per the house
        # rule. materials.json is derived rather than precious, but it is also
        # the file somebody hand-corrected at 1am, and regenerating that is not
        # free.
        from datetime import datetime
        import shutil
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.copy2(path, split_lessons.backup_target(
            path, "materials.json.%s.bak" % stamp))
        S.write_json_sidecar(path, data)
    else:
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return {"added": added, "kept": kept, "playable": playable,
            "docs": sorted(touched), "parts": len(docs)}


def main():
    ap = argparse.ArgumentParser(
        description="merge a file of lecture-video links into a course")
    ap.add_argument("file", help="the links file your Claude session wrote")
    ap.add_argument("--module", help="which course (default: the configured one)")
    ap.add_argument("--overwrite", action="store_true",
                    help="replace links that are already there")
    ap.add_argument("--dry-run", action="store_true", help="say what would happen")
    ap.add_argument("--config", default=str(S.CONFIG_PATH))
    args = ap.parse_args()

    cfg = S.load_config(Path(args.config).expanduser())
    mods = S.resolve_modules(cfg)
    mid = args.module or S.default_module(cfg)
    if mid not in mods:
        raise SystemExit("no course called %r. There %s: %s"
                         % (mid, "is" if len(mods) == 1 else "are",
                            ", ".join(sorted(mods)) or "none yet"))
    folder = mods[mid]

    try:
        records, complaints = parse(Path(args.file).expanduser()
                                    .read_text(encoding="utf-8", errors="replace"))
    except (OSError, Problem) as exc:
        raise SystemExit("could not read it: %s" % exc)

    print("%d lecture%s in %s" % (len(records), "" if len(records) == 1 else "s",
                                  Path(args.file).name))
    withentry = sum(1 for _, f in records if f.get("entry") or f.get("video_embed"))
    print("  %d of them will PLAY in the reader; the rest open out to KEATS."
          % withentry)
    for why in complaints:
        print("  skipped: %s" % why)

    if args.dry_run:
        for doc, f in records:
            print("    %-12s %s" % (doc, f.get("entry") or f.get("video") or ""))
        print("\nDry run. Nothing written.")
        return

    out = merge(folder, records, overwrite=args.overwrite)
    print("\n%d field%s written into %s/materials.json across %d lesson%s."
          % (out["added"], "" if out["added"] == 1 else "s", folder.name,
             len(out["docs"]), "" if len(out["docs"]) == 1 else "s"))
    if out["kept"]:
        print("%d field%s already had a value and %s left alone. Use --overwrite "
              "to replace them." % (out["kept"], "" if out["kept"] == 1 else "s",
                                    "was" if out["kept"] == 1 else "were"))
    print("%d of %d part%s can now be played in the side panel."
          % (out["playable"], out["parts"], "" if out["parts"] == 1 else "s"))


if __name__ == "__main__":
    main()
