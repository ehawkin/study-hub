#!/usr/bin/env python3
"""A DOI that resolves to a STAND-IN for the paper rather than to the paper.

Used by `verify_notes.py --dois`. Pure: it is handed a record and returns a
sentence or None, so it needs no network and every case below is a fixture.

🔴 **The case, found by the content role 2026-09-08 while writing W5-T1-P2.**
Resolving Treadway 2012 by bibliographic search returned
`10.1037/a0028813.supp`, whose title is *"Supplemental Material for
Effort-Based Decision-Making in Major Depressive Disorder…"*. That is the
article's supplement, not the article.

⚠️ **Why it needs a check of its own, and cannot be left to word overlap.** On
the shadow record the identifier resolves, the year matches (a supplement ships
with its article), the journal matches, and the title contains every content
word of the real title plus three. **Every instrument that compares words scores
it about 1.0**, and it is the case most likely to be believed, because the
gloss, the claim and the record genuinely are about the same study. The tell is
structural or it is nothing.

🔴 **CORRECTION TO THE PROPOSED TELLS, measured 2026-09-09 against the live
record before this was written.** The queue entry proposed three: a `.supp`
identifier, a title that is a prefix plus the real title, and *a Crossref `type`
of `component`*. **The third is wrong for the case it was written about**:
`10.1037/a0028813.supp` is typed `journal-article`, exactly like the article it
shadows. The first two are right and are what this uses. `component` is still
checked because it is a true positive when it appears, but nothing rests on it.

🟢 **This reports; it does not fail the gate.** A supplement is a real document
and somebody may one day mean to cite one. What it must never do is resolve
silently, which is what happened.
"""
import re

# A supplement's identifier is the article's with a suffix. `.supp`, `.supp1`,
# and the `-supp` some publishers use.
SUPP_ID = re.compile(r"[.\-]supp(?:l|lement(?:al|ary)?)?\d*$", re.I)

# The title of a stand-in names what it stands in for, and the tell is the
# CONNECTIVE, not the opening words. 🔴 Measured while writing this: requiring
# only the opening phrase flags "Supplementary materials and methods in modern
# chemistry", which is an ordinary article whose title happens to start that
# way. "Supplemental Material FOR x" and "Correction TO x" are announcements
# about another document; without the connective they are just titles.
SUPP_PHRASE = (r"supplement(?:al|ary)\s+"
               r"(?:material|materials|information|data|table|figure|appendix)s?")
NOTICE = (r"(?:author\s+correction|publisher\s+correction|correction|erratum"
          r"|corrigendum|retraction(?:\s+notice)?|expression\s+of\s+concern"
          r"|withdrawal)")

STANDIN_TITLE = re.compile(
    r"^\s*(?:"
    r"(?P<supp>" + SUPP_PHRASE + r")\s+(?:for|to)\b"
    r"|"
    r"(?P<notice>" + NOTICE + r")\b\s*(?:for|to|of|in)\b"
    r")\s*[:\-]?\s*(?P<rest>.*)$", re.I | re.S)

# A notice can also be the WHOLE title, with nothing after it: journals publish
# records titled simply "Erratum". Kept separate from the pattern above rather
# than made optional inside it, because making the connective optional is
# exactly the over-match that pattern was tightened to avoid.
BARE_NOTICE = re.compile(r"^\s*" + NOTICE + r"\s*[.:]?\s*$", re.I)

# Crossref types that are a PART of a work rather than the work. Kept because
# each is a true positive when it appears, but see the correction above: the
# record this check exists for is not typed as one of these, so a check built
# only on `type` would have missed the case it was written for.
PART_TYPES = frozenset(("component", "dataset", "peer-review", "grant"))


def first_title(rec):
    """Crossref's `title` is a list, and it can be empty or absent."""
    t = (rec or {}).get("title")
    if isinstance(t, list):
        t = next((x for x in t if isinstance(x, str) and x.strip()), None)
    return t.strip() if isinstance(t, str) else ""


def shadow_reason(doi, rec):
    """One sentence saying why this record stands in for a paper, or None.

    Ordered by how decisive each tell is, so the sentence a person reads names
    the strongest evidence rather than whichever matched first by accident.
    """
    title = first_title(rec)
    kind = STANDIN_TITLE.match(title)
    phrase = (kind.group("supp") or kind.group("notice")).strip().lower() if kind else None
    ident = bool(SUPP_ID.search((doi or "").strip()))

    if kind and ident:
        return ("the identifier ends in a supplement suffix AND the title begins "
                "%r, so this is the supplement rather than the article" % phrase)
    if ident:
        return ("the identifier ends in a supplement suffix, so it names the "
                "article's supplementary material rather than the article")
    if kind:
        rest = (kind.group("rest") or "").strip(" .:-")
        named = (", which names %r" % rest[:60]) if rest else ""
        return ("the title begins %r%s, so the record is about a paper rather "
                "than being it" % (phrase, named))
    if BARE_NOTICE.match(title):
        return ("the record's whole title is %r, so it is a notice about a paper "
                "rather than the paper" % title.strip())
    if (rec or {}).get("type") in PART_TYPES:
        return ("Crossref types this record %r, which is a part of a work rather "
                "than the work" % rec.get("type"))
    return None


def article_for(doi):
    """The identifier this one is probably a supplement OF, or None.

    Offered so the report can say what to cite instead. It is a suggestion from
    the identifier's shape and nothing has resolved it, so the wording that
    prints it must not claim it exists.
    """
    stripped = SUPP_ID.sub("", (doi or "").strip())
    return stripped if stripped and stripped != (doi or "").strip() else None

def norm_title(t):
    """A title reduced to what two spellings of it share."""
    return " ".join(re.findall(r"[a-z0-9]+", (t or "").lower()))


def stands_for(doi, rec):
    """The title of the work this record stands in for, or "" if it names none.

    A supplement's or correction's title is the announcement plus the article's
    own title, so this is the article's title as the shadow itself spells it.
    """
    kind = STANDIN_TITLE.match(first_title(rec))
    return (kind.group("rest") or "").strip(" .:-") if kind else ""


def companion_cited(doi, rec, titles):
    """Is the ARTICLE this record stands in for cited in the same run?

    🔴 The signal that separates a mistake from the documented pattern, and it
    costs nothing because the gate has already fetched every record.

    **A writer who deliberately links a correction cites the article too**, which
    is what `NOTE-SPEC`'s `corrnote` convention is: *"Carries a correction, …,
    which should be read with it."* **A writer who has the wrong record cites only
    the wrong record**, because they believe it is the article.

    Measured on the first real run: `10.1186/s13229-021-00433-x`, the correction
    to the Asperger paper, is cited deliberately in W4-T2-P1 alongside
    `10.1186/s13229-018-0208-6`, and without this it reads as a defect. The case
    the check exists for, the Treadway supplement, has no companion, because the
    writer thought the supplement WAS the article.

    `titles` is {doi: title} for everything this run resolved.
    """
    mine = norm_title(stands_for(doi, rec))
    if mine:
        for other, title in (titles or {}).items():
            if other != doi and norm_title(title) == mine:
                return other
    guess = article_for(doi)
    return guess if guess and guess in (titles or {}) else None
