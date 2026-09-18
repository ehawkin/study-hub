#!/usr/bin/env python3
"""Has this paper been corrected or retracted? Ask BOTH indexes, per NOTE-SPEC D14.

    python3 server/corrcheck.py --self-test          # prove the checker can fire
    python3 server/corrcheck.py --course PSY101      # sweep a course's DOIs
    echo 10.1016/j.brat.2010.04.006 | python3 server/corrcheck.py

🔴 **NEITHER INDEX IS A SUPERSET OF THE OTHER, and this project has both directions
on file.** Godfrin 2010 (`10.1016/j.brat.2010.04.006`) carries a corrigendum that
PubMed knows and Crossref does not; Stillman 2014 (`10.1016/j.concog.2014.07.002`)
carries one that Crossref knows and PubMed does not. A DOI is clean only when both
say so, which is why `verify_notes.py --dois`, which asks Crossref alone, is half
of rule D14 rather than all of it.

🔴 **THE TRAP THIS FILE EXISTS TO AVOID, and it is the reason the known-positive
test lives beside the code instead of in a plan.** Europe PMC spells the field
`"Erratum in"`, WITH A SPACE. A checker matching `"ErratumIn"` returns **clean for
every PubMed-only correction there is**, and reports it as a pass. Found by
Study-Hub-Injest on 2026-08-30, whose first probe did exactly that and was caught
only by running it against Godfrin. **A corrections check that has never been seen
to fire is not a check**, so `--self-test` is not decoration: it asserts a HIT on a
Crossref-blind case, a HIT on a PubMed-blind case, and silence on a clean paper.

**UNINDEXED is reported separately from clean**, deliberately. A DOI with no PubMed
record has not been checked by PubMed, and calling that "clean" is the exact failure
the rule exists to prevent.
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

# 🔴 The contact address is read from the environment and NEVER hardcoded. Crossref's
# polite pool asks for a mailto and rewards it with better rate limits, but a personal
# address written into this repo would ship to every kit recipient and is exactly what
# `build_kit.py`'s BLOCKING audit refuses. Unset simply means the public pool, which
# works and is only slower.
_CONTACT = os.environ.get("STUDYHUB_CONTACT_EMAIL", "").strip()
UA = {"User-Agent": "StudyHub/1.0" + (" (mailto:%s)" % _CONTACT if _CONTACT else "")}

# Europe PMC's `commentCorrectionList` type strings, normalised. Compare with `_norm`
# on BOTH sides or the space in "Erratum in" defeats the whole check.
NOTICE = {"erratumin", "retractionin", "expressionofconcernin",
          "correctedandrepublishedin", "republishedin", "updatein"}

# 🔴 Same extraction as `verify_notes.py --dois`, so a sweep covers the same set a
# reader can actually click. It used to be the same REGEX LITERAL, copied, with a
# test pinning the literal in both files; that test could only fail on the edit it
# existed to supervise, and the pattern it pinned was blind to 223 of the corpus's
# 1779 links. Shared code, and a test that compares what the two tools actually
# FIND, replaced both.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import doi_links as DOILINK


def _norm(text):
    """Squash whitespace and case, so "Erratum in" and "ErratumIn" are one thing."""
    return "".join((text or "").split()).lower()


def _get(url, timeout=40):
    req = urllib.request.Request(url, headers=UA)
    return json.load(urllib.request.urlopen(req, timeout=timeout))


def crossref(doi):
    """(verdict, detail). Reads `updated-by`, plus the title, because a retracted
    paper is often re-titled "RETRACTED: ..." without an `updated-by` entry."""
    try:
        msg = _get("https://api.crossref.org/works/" + urllib.parse.quote(doi))["message"]
    except Exception as exc:
        return ("ERROR", str(exc))
    hits = ["%s %s" % (u.get("type"), u.get("DOI")) for u in (msg.get("updated-by") or [])]
    title = (msg.get("title") or [""])[0]
    if title.upper().startswith(("RETRACTED", "WITHDRAWN")):
        hits.append("title flagged: " + title[:60])
    return ("HIT" if hits else "clean", "; ".join(hits))


def pubmed(doi):
    """(verdict, detail, pmid) from Europe PMC, which fronts PubMed and needs no key.

    🔴 **The PMID is RETURNED, not just tested.** It was read here from the first
    version and thrown away after the UNINDEXED check, which is the same shape of
    loss as `_notice` printing `PMID:None`: the value the next step needs was in
    hand and was discarded. The manager's ruling of 2026-08-30 turns on it. Six of
    the eight PubMed-only errata in the affective disorders course have no notice id, so the reference
    entry links the ORIGINAL paper's record instead, at
    `https://pubmed.ncbi.nlm.nih.gov/<pmid>/`, which is guaranteed to display the
    erratum because that record's "Erratum in" line is where the fact came from.
    """
    url = ("https://www.ebi.ac.uk/europepmc/webservices/rest/search?query="
           + urllib.parse.quote('DOI:"%s"' % doi)
           + "&resultType=core&format=json&pageSize=1")
    try:
        results = _get(url)["resultList"]["result"]
    except Exception as exc:
        return ("ERROR", str(exc), "")
    if not results:
        return ("UNINDEXED", "no record", "")
    rec = results[0]
    pmid = rec.get("pmid") or ""
    if not pmid:
        return ("UNINDEXED", "no pmid", "")
    corrections = (rec.get("commentCorrectionList") or {}).get("commentCorrection", [])
    hits = [_notice(c) for c in corrections if _norm(c.get("type")) in NOTICE]
    return ("HIT" if hits else "clean", "; ".join(hits), pmid)


def pubmed_url(pmid):
    """Where a reader is sent to see the notice, or "" when there is no record.

    The link the caution sign uses when the notice itself has no identifier.
    """
    return ("https://pubmed.ncbi.nlm.nih.gov/%s/" % pmid) if pmid else ""


def _notice(c):
    """One correction notice, in the most linkable form available.

    🔴 Europe PMC often returns NO `id` for a notice and a `reference` string
    instead, e.g. "Arch Gen Psychiatry. 2007 Sep;64(9):1039". Six of the eight
    PubMed-only corrections in the affective disorders course are that shape. Printing `PMID:None` and
    dropping the reference, which this did until 2026-08-30, throws away the only
    thing a writer can actually put in a reference entry, and makes a real notice
    look like a parse failure.
    """
    what = c.get("type")
    if c.get("id"):
        return "%s PMID:%s" % (what, c["id"])
    if c.get("reference"):
        return "%s %s" % (what, c["reference"])
    return "%s (no id and no reference)" % what


def check(doi):
    """Both indexes. `flagged` is true when EITHER carries a notice."""
    cs, cd = crossref(doi)
    ps, pd, pmid = pubmed(doi)
    return {"doi": doi, "crossref": cs, "crossref_detail": cd,
            "pubmed": ps, "pubmed_detail": pd,
            "pmid": pmid, "pubmed_url": pubmed_url(pmid),
            "flagged": "HIT" in (cs, ps), "errored": "ERROR" in (cs, ps)}


def course_dois(course_dir, report=None):
    """{doi: {labels}} for every clickable DOI in a course's lesson pages.

    `report`, when given, is called with the number of doi.org hrefs this reader
    could not turn into a link-and-label pair. A sweep that cannot say it missed
    anything is the failure this whole path was fixed for.
    """
    pages = [p.read_text(encoding="utf-8")
             for p in sorted(Path(course_dir).glob("*.html"))]
    seen, unreadable = DOILINK.collect(pages)
    if report is not None:
        report(unreadable)
    return seen


# 🔴 The known positives. Each is on file in this repo with its own write-up, and
# each one dies if the checker regresses in a different way:
#   Godfrin  PubMed knows, Crossref does not  -> catches the "Erratum in" trap
#   Stillman Crossref knows, PubMed does not  -> catches a PubMed-only rewrite
#   Gotink   both know (a retraction)         -> catches a broken parse either side
#   Sleep    neither knows                    -> catches a checker that flags anything
SELF_TEST = [
    ("10.1016/j.brat.2010.04.006", "Godfrin 2010", "clean", "HIT"),
    ("10.1016/j.concog.2014.07.002", "Stillman 2014", "HIT", "clean"),
    ("10.1371/journal.pone.0124344", "Gotink 2015, retracted", "HIT", "HIT"),
    ("10.1016/j.neubiorev.2014.03.016", "a clean paper", "clean", "clean"),
]


def self_test():
    """Needs the network. Exits 1 unless the checker is seen to FIRE and to STAY
    QUIET, which is the only evidence that a clean sweep means anything."""
    bad = 0
    print("=== corrcheck self-test: can it fire, and does it stay quiet? ===")
    for doi, what, want_cr, want_pm in SELF_TEST:
        r = check(doi)
        ok = (r["crossref"] == want_cr and r["pubmed"] == want_pm)
        # 🔴 An indexed paper MUST come back with a pmid. Returning "" for every
        # paper would leave the verdicts correct and silently break the link the
        # caution sign is built from, which is this tool's own failure mode
        # applied one field along: wrong in the direction that looks like working.
        if want_pm in ("HIT", "clean") and not r["pmid"]:
            ok = False
        bad += 0 if ok else 1
        print("%s %-32s crossref=%-9s (want %-5s) pubmed=%-9s (want %-5s) pmid=%s"
              % ("ok  " if ok else "FAIL", what, r["crossref"], want_cr,
                 r["pubmed"], want_pm, r["pmid"] or "MISSING"))
        time.sleep(1.1)
    if bad:
        print("\n%d of %d wrong. DO NOT TRUST A SWEEP FROM THIS BUILD." % (bad, len(SELF_TEST)))
        return 1
    print("\nAll %d as expected: it fires on both index-blind shapes and stays quiet on a "
          "clean paper." % len(SELF_TEST))
    return 0


def sweep(dois, labels=None):
    flagged, errored = [], []
    for doi in dois:
        r = check(doi)
        mark = "!!" if r["flagged"] else ("??" if r["errored"] else "  ")
        if r["flagged"]:
            flagged.append(r)
        if r["errored"]:
            errored.append(r)
        line = "%s %-44s crossref=%-9s pubmed=%-9s pmid=%-9s %s %s" % (
            mark, doi, r["crossref"], r["pubmed"], r["pmid"] or "-",
            r["crossref_detail"], r["pubmed_detail"])
        if labels and doi in labels:
            line += "  | " + sorted(labels[doi])[0][:48]
        print(line.rstrip())
        time.sleep(1.1)
    print("\n%d with a notice, %d that could not be checked, %d asked"
          % (len(flagged), len(errored), len(dois)))
    # 🔴 Errors are NOT clean. A sweep that could not reach an index has not
    # checked those DOIs, and reporting a total without them invites the reader
    # to treat "asked" as "cleared".
    return 1 if errored else 0


def main(argv):
    if "--self-test" in argv:
        return self_test()
    if "--course" in argv:
        code = argv[argv.index("--course") + 1]
        here = Path(__file__).resolve().parent
        course = here.parent / "courses" / code
        if not course.is_dir():
            print("no such course: %s" % course)
            return 2
        missed = []
        seen = course_dois(course, report=missed.append)
        print("=== %s: %d distinct DOIs across %d pages, %d links unreadable ==="
              % (code, len(seen), len(list(course.glob("*.html"))), sum(missed)))
        if sum(missed):
            print("🔴 %d doi.org links could not be read as a link+label pair, "
                  "so this sweep did NOT cover them" % sum(missed))
        return sweep(sorted(seen), seen)
    dois = [l.strip() for l in sys.stdin if l.strip()]
    return sweep(dois)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
