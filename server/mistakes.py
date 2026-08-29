#!/usr/bin/env python3
"""Mistakes found in a course's own material, and the page that collects them.

EH's design, 2026-08-23: "a per-course 'mistakes found in this course' page at
the end, collecting source defects the build noticed", given alongside the
contradiction call-out and collapsed-statistics rulings. The call-out lives in
the lesson where the reader meets the claim; THIS is the course-level ledger,
one entry per defect, so a person can see in one place what their own teaching
material gets wrong and check any of it against the sources.

**Why data rather than prose**, same reasoning as readings.py: as a record an
entry can say, structurally, what the material says AND what the paper says,
and the page renders the two sides the same way every time. NOTE-SPEC section C
makes quoting both sides without adjudicating the required shape; if this were
one text field, every writer would phrase it differently within a fortnight
(the ingest session's observation, 2026-08-24, and it is right).

**Nothing here finds a mistake.** Finding one is the course build's job, done
with the sources open (SPLIT-MAP and the citation sweeps are where the raw
material comes from). This is the boring, exact half: check what that session
wrote, and file it.
"""

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import study_server as S                                        # noqa: E402
import split_lessons                                            # noqa: E402

ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
DOI_RE = re.compile(r"^10\.\d{4,9}/\S+$")

# Short, lowercased, free text rather than a closed list: defect kinds are
# open-ended (an inverted result, a wrong year, a slide missing from the
# export, a damaged file...) and a closed vocabulary would fight reality.
# The write-lesson skill suggests the recurring ones so the wording converges.
TEXT_FIELDS = ("label", "kind", "doc", "week", "source_says", "paper_says")


class Problem(Exception):
    pass


def _one(raw, where):
    """One mistake, checked. Returns ((id, record), "") or (None, why)."""
    if not isinstance(raw, dict):
        return None, "%s is not a record" % where
    mid = str(raw.get("id") or "").strip()
    if not mid:
        # A readable id out of the doc and the first words of the label, which
        # is what a person would have typed anyway.
        doc = str(raw.get("doc") or "").strip().lower()
        stub = re.sub(r"[^a-z0-9]+", "-",
                      str(raw.get("label") or "").lower()).strip("-")[:40]
        if stub:
            mid = ("%s-%s" % (doc, stub)).strip("-")
    if not mid:
        return None, "%s has no id, and no label to build one from" % where
    if not ID_RE.match(mid):
        return None, "%s has an id this cannot use: %r" % (where, mid[:30])

    rec = {}
    for k in TEXT_FIELDS:
        v = raw.get(k)
        if v is None or v == "":
            continue
        limit = 2000 if k in ("source_says", "paper_says") else 200
        rec[k] = " ".join(str(v).split())[:limit]
    if str(raw.get("text") or "").strip():
        rec["text"] = str(raw["text"]).strip()[:8000]

    if not rec.get("label"):
        return None, "%s has no label" % where
    rec["kind"] = (rec.get("kind") or "source defect").lower()

    # `doc` names the lesson the defect belongs to, or is absent for a
    # course-level defect (a stale week introduction, a damaged file). The
    # page links it if the lesson exists and says so plainly if not.
    doc = str(rec.get("doc") or "").strip()
    if doc and not S.DOC_ID_RE.match(doc):
        return None, "%s: %r is not a lesson id" % (where, doc[:30])

    wk = re.search(r"\d+", str(rec.get("week") or ""))
    if wk:
        rec["week"] = wk.group(0).lstrip("0") or "0"
    elif doc:
        m = re.match(r"^W(\d+)", doc, re.I)
        if m:
            rec["week"] = m.group(1).lstrip("0") or "0"

    # 🔴 Same DOI stance as readings.py: shape is checked here, truth is the
    # writer's job, and nothing is ever invented to fill the field.
    refs = []
    for d in (raw.get("refs") or [])[:8]:
        d = str(d).replace("https://doi.org/", "").replace("doi:", "").strip()
        if not DOI_RE.match(d):
            return None, "%s: ref %r is not a DOI" % (where, d[:40])
        refs.append(d)
    if refs:
        rec["refs"] = refs

    if not (rec.get("source_says") or rec.get("paper_says") or rec.get("text")):
        return None, ("%s is only a label, with nothing to read: give it the "
                      "two sides, or a text" % where)
    return (mid, rec), ""


def parse(text):
    """Mistakes out of whatever shape the file is. Returns (records, complaints)."""
    text = (text or "").strip()
    if not text:
        raise Problem("that file is empty")
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise Problem("this is not JSON: %s" % exc)

    if isinstance(data, list):
        rows = [(r, "item %d" % i) for i, r in enumerate(data, 1)]
    elif isinstance(data, dict) and isinstance(data.get("mistakes"), list):
        rows = [(r, "item %d" % i) for i, r in enumerate(data["mistakes"], 1)]
    elif isinstance(data, dict) and isinstance(data.get("mistakes"), dict):
        rows = [(dict(v, id=k), k) for k, v in data["mistakes"].items()
                if isinstance(v, dict)]
    else:
        raise Problem("this is JSON, but not a list of mistakes this recognises")

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
    """Fold mistakes into this course's mistakes.json.

    A re-filed entry replaces the old one under the same id (the second write
    is a better record of the same defect), except where the person has edited
    it by hand, which `edited` marks, same contract as readings."""
    folder = Path(folder)
    path = folder / "mistakes.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError
    except (OSError, ValueError):
        data = {}
    docs = data.setdefault("mistakes", {})
    order = data.setdefault("order", [])
    if not isinstance(docs, dict) or not isinstance(order, list):
        raise Problem("%s is there but is not shaped like a mistakes file" % path)

    added = replaced = kept = 0
    for mid, rec in records:
        if mid in docs:
            if docs[mid].get("edited") and not overwrite:
                kept += 1
                continue
            rec["edited"] = docs[mid].get("edited", False)
            docs[mid] = rec
            replaced += 1
        else:
            docs[mid] = rec
            order.append(mid)
            added += 1

    data["count"] = len(docs)
    data.setdefault("built", "filed by the course build")
    folder.mkdir(parents=True, exist_ok=True)
    if path.exists():
        from datetime import datetime
        import shutil
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.copy2(path, split_lessons.backup_target(
            path, "mistakes.json.%s.bak" % stamp))
        S.write_json_sidecar(path, data)
    else:
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return {"added": added, "replaced": replaced, "kept": kept, "count": len(docs)}


def read_all(folder):
    """Every recorded mistake for a course, in order. Missing file means none."""
    path = Path(folder) / "mistakes.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    docs = data.get("mistakes")
    if not isinstance(docs, dict):
        return []
    order = [r for r in (data.get("order") or []) if r in docs]
    order += [r for r in sorted(docs) if r not in order]
    out = []
    for mid in order:
        rec = dict(docs[mid])
        rec["id"] = mid
        out.append(rec)
    return out


def main():
    import argparse
    ap = argparse.ArgumentParser(description="file course-material mistakes "
                                             "into a course")
    ap.add_argument("file", help="the mistakes file your Claude session wrote")
    # 🔴 Required, not defaulted: same reasoning as glossary_extend. A default
    # module makes a silent wrong-course write possible, and this file is
    # exactly the kind of thing two courses would both plausibly have open.
    ap.add_argument("--module", required=True, help="which course")
    ap.add_argument("--overwrite", action="store_true",
                    help="replace entries you have edited by hand too")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--config", default=str(S.CONFIG_PATH))
    args = ap.parse_args()

    cfg = S.load_config(Path(args.config).expanduser())
    mods = S.resolve_modules(cfg)
    if args.module not in mods:
        raise SystemExit("no course called %r. There %s: %s"
                         % (args.module, "is" if len(mods) == 1 else "are",
                            ", ".join(sorted(mods)) or "none yet"))
    try:
        records, complaints = parse(Path(args.file).expanduser()
                                    .read_text(encoding="utf-8", errors="replace"))
    except (OSError, Problem) as exc:
        raise SystemExit("could not read it: %s" % exc)

    print("%d mistake%s in %s" % (len(records), "" if len(records) == 1 else "s",
                                  Path(args.file).name))
    for why in complaints:
        print("  skipped: %s" % why)
    if args.dry_run:
        print("dry run, nothing written")
        return
    try:
        out = merge(mods[args.module], records, overwrite=args.overwrite)
    except Problem as exc:
        raise SystemExit(str(exc))
    print("%(added)d added, %(replaced)d re-filed, %(kept)d left alone "
          "(edited by hand); the course now records %(count)d" % out)


if __name__ == "__main__":
    main()
