"""The DOI links a reader can actually click, read as RENDERED TEXT.

The single source for `verify_notes.py --dois` and `corrcheck.py`. They must see
the same set or "checked" and "swept" drift apart, and until 2026-09-08 they kept
that promise by carrying the same regex literal twice, with a test pinning the
literal in both files. That test could only fail on the edit it existed to
supervise, and the regex it pinned was wrong.

🔴 A CITATION LABEL IS THE RENDERED TEXT OF ITS LINK. Both tools used to read the
HTML SOURCE instead: `>([^<]{1,70})</a>` requires the anchor text to be plain and
under 70 characters, so an ordinary reference-list entry (an italicised journal
name, a full author list, or simply a long one) matched nothing and the link was
skipped entirely: not resolved, not author-matched, not year-matched, and NOT
CHECKED FOR A CORRECTION.

⚠️ It failed in the direction that reads as good news. The gate printed a smaller
`N distinct` and passed, which is why nobody found it from the output.

Measured across `courses/*/*.html` on 2026-09-08, 98 lessons:

    DOI links present                                     1779
    the old pattern could see                             1556
    invisible to it                                        223
      of those, carrying markup                            193
      of those, plain but over 70 characters                30
    🔴 still invisible after stripping tags alone          221
    distinct DOIs invisible EVERYWHERE, so never checked     35

🔴 THE LENGTH CAP WAS DOING ALMOST ALL OF THE HIDING. The report that found this
named the markup and asked for a tag-stripper; a tag-stripper alone would have
moved 223 to 221 and the gate would have gone on reading as good news. The cap
goes too, and the measurement above is here so the next person does not have to
re-derive why.

🔴 DECODE, DO NOT DISCARD. An entity fails the OTHER way, and loudly:
`Happ&eacute; 1995` is seen, resolved, and then reported as
`author Happ&eacute; 1995 vs ['Happe']`, because the checker folds the character
é onto e and cannot fold an entity. A stripper that swallowed entities would turn
that false alarm into a silent skip, which is strictly worse than a false alarm.
So tags are removed and entities are DECODED, in that order: unescaping first
would turn a written `&lt;i&gt;` into a tag and then delete it.
"""

import html as _html
import re

# The anchor's inner HTML, whatever it contains. Non-greedy, so it stops at the
# first `</a>`; `re.S` because a reference list wraps its lines.
LINK = re.compile(r'href="https://doi\.org/([^"]+)"[^>]*>(.*?)</a>', re.S)

# 🔴 The defect worth more than the pattern: a check that cannot say it missed
# anything. Every doi.org href is counted loosely as well, and the difference is
# reported EVEN WHEN IT IS ZERO, so this has a positive result and its own
# blindness is visible rather than inferred. A single-quoted href, an `http://`
# or `dx.doi.org` one, or an anchor nobody closed lands in the difference instead
# of vanishing.
ANY_HREF = re.compile(r'href\s*=\s*["\']?[^"\'>\s]*?doi\.org/', re.I)

TAG = re.compile(r"<[^>]+>")

# An anchor longer than this is not a citation label; it is an unclosed tag that
# has swallowed the rest of the paragraph. Refusing it keeps a runaway label out
# of the author check, and because it is refused rather than truncated it shows up
# in the unreadable count. The longest real one in the corpus is 348 characters.
LABEL_MAX = 600


def label_of(inner):
    """The rendered text of an anchor: tags removed, entities decoded, spaces
    squashed. In that order, and see the module docstring for why."""
    return re.sub(r"\s+", " ", _html.unescape(TAG.sub("", inner))).strip()


def links_in(src):
    """`(pairs, unreadable)` for one page.

    `pairs` is a list of `(doi, label)` in document order, one per clickable link.
    `unreadable` is how many doi.org hrefs this reader could NOT turn into a pair,
    which is the number that makes a silent miss loud.
    """
    pairs, readable = [], 0
    for doi, inner in LINK.findall(src):
        if len(inner) > LABEL_MAX:
            continue
        readable += 1
        pairs.append((doi, label_of(inner)))
    return pairs, len(ANY_HREF.findall(src)) - readable


def collect(pages, into=None):
    """`({doi: {labels}}, unreadable)` across an iterable of page sources."""
    seen = {} if into is None else into
    unreadable = 0
    for src in pages:
        pairs, missed = links_in(src)
        unreadable += missed
        for doi, label in pairs:
            seen.setdefault(doi, set()).add(label)
    return seen, unreadable


def why_not(exc):
    """`(headline, reason)` for a CrossRef lookup that did not come back.

    🔴 The gate used to catch every exception and print NOT IN CROSSREF, throwing
    the reason away: `except Exception as exc` bound a name it never used. So a
    slow response, a 503 and a genuinely missing record all printed the same
    sentence, and the sentence was the serious one.

    ⚠️ Measured 2026-09-08 while running the fixed gate over one course's 390 DOIs:
    EIGHT failures were reported and FOUR of them were the network. Every one of
    the four resolved at HTTP 200 on a re-ask with a longer timeout. A remediation
    list that is half noise cannot be handed to the person who has to act on it,
    and "this paper does not exist" is a serious thing to say about a real paper.

    🔴 Both kinds still FAIL the run. An unchecked citation is unchecked whichever
    way the ask went wrong; what changes is what the reader is told to do about it.
    """
    code = getattr(exc, "code", None)
    if code == 404:
        return ("NOT IN CROSSREF", "no record")
    if code is not None:
        return ("COULD NOT ASK", "HTTP %s" % code)
    return ("COULD NOT ASK", type(exc).__name__)

