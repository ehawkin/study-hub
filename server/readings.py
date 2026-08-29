#!/usr/bin/env python3
"""Core readings: the papers and chapters a course sets, and a way to skim them.

Asked for by EH on 2026-08-22. Most courses set core readings, usually
scientific papers or book chapters, and the reader had nowhere to put them. What
he asked for: a section per course, a folder or directory they live in, a skill
that works only on readings, and **summaries that can be skimmed and then opened
up**: "someone can skim and get the top ideas but also dive deeper into it".

🔴 **The summary's SHAPE is the design, and it is not one shape.** A paper and a
chapter answer different questions, so forcing one template on both produces a
summary that is vague about each. Two kinds, sharing a skeleton:

    the skim layer      claim + why it is set + three takeaways
    the deeper layer    sections, opened one at a time
    the apparatus       terms, and which lessons it connects to

**paper**    claim is the finding in one sentence; the sections are what they
             did, what they found, and what to be careful about, because that
             is the order in which a claim becomes trustworthy or does not.
**chapter**  claim is what the chapter is for; the sections are its key ideas,
             one per section, because a chapter has no single finding and
             pretending otherwise is where textbook summaries go wrong.

**Why data rather than prose.** A summary written as HTML would be one more thing
whose structure has to be parsed to be rendered, and the whole point is that the
reader draws the skim layer differently from the deep layer. As data, the page
decides what is open and what is folded, and the same summary can be re-rendered
when that decision changes.

**Nothing here reads a PDF.** Identifying and summarising a paper is the
`core-readings` skill's job, in a session that can actually read the document.
This is the boring, exact half: check what that session wrote, and file it.
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
URL_RE = re.compile(r"^https?://", re.I)
KINDS = ("paper", "chapter", "other")

# What a summary is allowed to carry. Anything else in the file is ignored rather
# than stored: a generous model invents fields, and this is not the place to find
# out what it meant by them.
TEXT_FIELDS = ("title", "authors", "venue", "claim", "why", "kind", "doi", "url",
               "file", "week", "topic")
LIST_FIELDS = ("takeaways", "sections", "terms", "links")


class Problem(Exception):
    pass


def _clean_sections(raw):
    """The deeper layer: a heading and a body, in the order they should open."""
    out = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        head = " ".join(str(item.get("h") or item.get("heading") or "").split())[:120]
        body = str(item.get("body") or item.get("text") or "").strip()
        if head and body:
            out.append({"h": head, "body": body[:20000]})
    return out[:20]


def _clean_terms(raw):
    out = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, dict):
            t = " ".join(str(item.get("t") or item.get("term") or "").split())[:80]
            d = str(item.get("d") or item.get("definition") or "").strip()[:2000]
            if t and d:
                out.append({"t": t, "d": d})
    return out[:40]


def _one(raw, where):
    """One reading, checked. Returns ((id, record), "") or (None, why)."""
    if not isinstance(raw, dict):
        return None, "%s is not a record" % where
    rid = str(raw.get("id") or raw.get("key") or "").strip()
    if not rid:
        # A readable id out of the first author and the year, which is what a
        # person would have typed anyway and what they will search for.
        who = re.split(r"[,&;]| and ", str(raw.get("authors") or ""))[0]
        who = re.sub(r"[^A-Za-z]", "", who.split()[-1] if who.split() else "")
        yr = re.search(r"(19|20)\d{2}", str(raw.get("year") or ""))
        if who and yr:
            rid = "%s-%s" % (who.lower(), yr.group(0))
    if not rid:
        return None, "%s has no id, and no author and year to build one from" % where
    if not ID_RE.match(rid):
        return None, "%s has an id this cannot use: %r" % (where, rid[:30])

    rec = {}
    for k in TEXT_FIELDS:
        v = raw.get(k)
        if v is None or v == "":
            continue
        rec[k] = " ".join(str(v).split())[:600] if k != "why" else str(v).strip()[:2000]
    if str(raw.get("claim") or "").strip():
        rec["claim"] = str(raw["claim"]).strip()[:1000]

    yr = re.search(r"(19|20)\d{2}", str(raw.get("year") or ""))
    if yr:
        rec["year"] = int(yr.group(0))

    kind = str(rec.get("kind") or "").lower()
    rec["kind"] = kind if kind in KINDS else ("paper" if rec.get("doi") else "other")

    # 🔴 A DOI is checked for SHAPE here and nowhere is it invented. The house
    # rule is that every identifier is verified against the record before it
    # ships, and that verification belongs to the skill, which can actually
    # resolve it. What this refuses is a DOI that is not even a DOI.
    doi = rec.get("doi", "")
    if doi:
        doi = doi.replace("https://doi.org/", "").replace("doi:", "").strip()
        if not DOI_RE.match(doi):
            return None, "%s: %r is not a DOI" % (where, doi[:40])
        rec["doi"] = doi
        rec.setdefault("url", "https://doi.org/" + doi)
    if rec.get("url") and not URL_RE.match(rec["url"]):
        return None, "%s: %r is not a link" % (where, rec["url"][:40])
    if rec.get("file"):
        # Kept relative and made safe: this becomes part of a URL under the
        # course, and a reading is not allowed to point outside it.
        f = rec["file"].replace("\\", "/").lstrip("/")
        if ".." in f.split("/"):
            return None, "%s: a reading cannot point outside its course" % where
        rec["file"] = f

    # A published correction or retraction, structured so the page can wear
    # the caution sign and link the notice (EH's design, 2026-08-23). Prose
    # about it in a section is welcome too; this field is what renders the
    # glyph and the standardised line.
    notice = raw.get("notice")
    if isinstance(notice, dict):
        nkind = " ".join(str(notice.get("kind") or "").lower().split())
        if nkind in ("correction", "corrigendum", "erratum", "retraction",
                     "expression of concern"):
            n = {"kind": nkind}
            ndoi = str(notice.get("doi") or "").replace(
                "https://doi.org/", "").replace("doi:", "").strip()
            if ndoi:
                if not DOI_RE.match(ndoi):
                    return None, "%s: notice %r is not a DOI" % (where, ndoi[:40])
                n["doi"] = ndoi
                n["url"] = "https://doi.org/" + ndoi
            elif str(notice.get("url") or "").strip():
                if not URL_RE.match(str(notice["url"]).strip()):
                    return None, "%s: notice %r is not a link" % (where, str(notice["url"])[:40])
                n["url"] = str(notice["url"]).strip()
            rec["notice"] = n
        elif nkind:
            return None, ("%s: notice kind %r is not one this understands"
                          % (where, nkind[:30]))

    rec["takeaways"] = [str(t).strip()[:400] for t in (raw.get("takeaways") or [])
                        if str(t).strip()][:8]
    rec["sections"] = _clean_sections(raw.get("sections"))
    rec["terms"] = _clean_terms(raw.get("terms"))
    rec["links"] = [str(x).strip() for x in (raw.get("links") or [])
                    if S.DOC_ID_RE.match(str(x).strip() or "")][:20]

    if not rec.get("title"):
        return None, "%s has no title" % where
    if not (rec.get("claim") or rec["takeaways"] or rec["sections"]):
        return None, ("%s is only a reference, with nothing to read: give it a "
                      "claim, some takeaways, or sections" % where)
    return (rid, rec), ""


def parse(text):
    """Readings out of whatever shape the file is. Returns (records, complaints)."""
    text = (text or "").strip()
    if not text:
        raise Problem("that file is empty")
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise Problem("this is not JSON: %s" % exc)

    if isinstance(data, list):
        rows = [(r, "item %d" % i) for i, r in enumerate(data, 1)]
    elif isinstance(data, dict) and isinstance(data.get("readings"), list):
        rows = [(r, "item %d" % i) for i, r in enumerate(data["readings"], 1)]
    elif isinstance(data, dict) and isinstance(data.get("readings"), dict):
        rows = [(dict(v, id=k), k) for k, v in data["readings"].items()
                if isinstance(v, dict)]
    else:
        raise Problem("this is JSON, but not a list of readings this recognises")

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
    """Fold readings into this course's readings.json.

    Unlike the video links, a re-summarised reading SHOULD replace the old one:
    the second attempt is a better summary of the same paper, not a competing
    fact about it. So a whole record is replaced when its id comes round again,
    and `overwrite=False` only protects readings the person has since edited by
    hand, which is what `edited` marks."""
    folder = Path(folder)
    path = folder / "readings.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError
    except (OSError, ValueError):
        data = {}
    docs = data.setdefault("readings", {})
    order = data.setdefault("order", [])
    if not isinstance(docs, dict) or not isinstance(order, list):
        raise Problem("%s is there but is not shaped like a readings file" % path)

    added = replaced = kept = 0
    for rid, rec in records:
        if rid in docs:
            if docs[rid].get("edited") and not overwrite:
                kept += 1
                continue
            rec["edited"] = docs[rid].get("edited", False)
            docs[rid] = rec
            replaced += 1
        else:
            docs[rid] = rec
            order.append(rid)
            added += 1

    data["count"] = len(docs)
    data.setdefault("built", "summarised by the core-readings skill")
    folder.mkdir(parents=True, exist_ok=True)
    if path.exists():
        from datetime import datetime
        import shutil
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.copy2(path, split_lessons.backup_target(
            path, "readings.json.%s.bak" % stamp))
        S.write_json_sidecar(path, data)
    else:
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return {"added": added, "replaced": replaced, "kept": kept, "count": len(docs)}


def read_all(folder):
    """Every reading for a course, in order. Missing file means no readings."""
    path = Path(folder) / "readings.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    docs = data.get("readings")
    if not isinstance(docs, dict):
        return []
    order = [r for r in (data.get("order") or []) if r in docs]
    order += [r for r in sorted(docs) if r not in order]
    out = []
    for rid in order:
        rec = dict(docs[rid])
        rec["id"] = rid
        out.append(rec)
    return out


def main():
    import argparse
    ap = argparse.ArgumentParser(description="merge core readings into a course")
    ap.add_argument("file", help="the readings file your Claude session wrote")
    ap.add_argument("--module", help="which course (default: the configured one)")
    ap.add_argument("--overwrite", action="store_true",
                    help="replace readings you have edited by hand too")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--config", default=str(S.CONFIG_PATH))
    args = ap.parse_args()

    cfg = S.load_config(Path(args.config).expanduser())
    mods = S.resolve_modules(cfg)
    mid = args.module or S.default_module(cfg)
    if mid not in mods:
        raise SystemExit("no course called %r. There %s: %s"
                         % (mid, "is" if len(mods) == 1 else "are",
                            ", ".join(sorted(mods)) or "none yet"))
    try:
        records, complaints = parse(Path(args.file).expanduser()
                                    .read_text(encoding="utf-8", errors="replace"))
    except (OSError, Problem) as exc:
        raise SystemExit("could not read it: %s" % exc)

    print("%d reading%s in %s" % (len(records), "" if len(records) == 1 else "s",
                                  Path(args.file).name))
    for why in complaints:
        print("  skipped: %s" % why)
    deep = sum(1 for _, r in records if r["sections"])
    print("  %d of them open into detail; the rest are a skim layer only." % deep)
    if args.dry_run:
        for rid, r in records:
            print("    %-22s %s" % (rid, r.get("title", "")[:52]))
        print("\nDry run. Nothing written.")
        return
    out = merge(mods[mid], records, overwrite=args.overwrite)
    print("\n%d added, %d re-summarised, %d left alone. %d reading%s on %s now."
          % (out["added"], out["replaced"], out["kept"], out["count"],
             "" if out["count"] == 1 else "s", mid))


if __name__ == "__main__":
    main()
