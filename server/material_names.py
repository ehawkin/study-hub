#!/usr/bin/env python3
"""What a downloaded material's NAME says about it, in one place.

The downloader writes `<DOC> - <Kind> (<whatever the course called it>).pdf`,
and everything that wants "the transcript for this part" matches that by
prefix, because the parenthesised tail is the course's own title for the file
and is not predictable. Four readers do that match: the reader's side panel
(`study_server.local_material_file`), the captions (`transcripts.transcript_for`),
a shared pack (`lesson_packs.copy_local_materials`) and the week books
(`consolidate_pdfs.kind_of`).

🔴 THE ACCIDENT THIS MODULE REPLACES. When a transcript is corrected, the
publisher writes `<DOC> - Transcript (..., with corrections).pdf` and RENAMES
the old one `<DOC> - Transcript superseded <date> (...).pdf`; nothing is
deleted, which is the owner's rule. Both match the prefix. Until 2026-09-17 the
corrected one won in three of the four readers only because `(` sorts before
`s`, and the fourth had this sentence written out by hand. The next round of
corrections could pick a name that sorts the other way, and the symptom would be
the pane and the captions quietly showing the OLD words, with nothing erroring.

So the rule is said once here and imported: a file whose name marks it as
superseded is not a candidate. This module has no local imports on purpose;
the server may not import `transcripts`, and `lesson_packs` may not either, so
a leaf is the only place all four can reach.

🔴 THE SECOND ACCIDENT, found by making the first one go away. Once all four
readers agreed which transcript is CURRENT, they still disagreed about what
counts as a TRANSCRIPT: the pane asked for the prefix `<DOC> - Transcript`,
the captions for the glob `<DOC> - Transcript*.pdf`, and a shared pack for the
substring `" - Transcript ("`, which needs the course's own title to follow
the word immediately. A file named `<DOC> - Transcript v2 (...)` was served
by the pane, used by the captions and silently left out of the pack, and a
pack is the one place a missing file reaches a person who cannot check. Since
2026-09-18 the kind question is asked here too, by `is_kind`, and it is the
pane's test.
"""

import os
import re

# The one word the publisher uses (`_admin/work/*/publish_corrections.py`), and
# the one word every reader looks for. Change both or neither.
SUPERSEDED = "superseded"

_TAIL = re.compile(r"\s*\([^()]*\)\s*$")


def standard_part(name):
    """`<DOC> - <Kind> ...` without the extension or the course's own title.

    The trailing parenthetical is the original download name and is only ever
    consulted for what KIND a file is, never for whether it is current, so a
    course whose own title happened to use the word would not be hidden."""
    stem = os.path.basename(str(name))
    stem = re.sub(r"\.pdf$", "", stem, flags=re.IGNORECASE)
    return _TAIL.sub("", stem)


def is_superseded(name):
    """True for a file kept beside its replacement as history.

    Case-insensitive, on the standard part of the name only."""
    return SUPERSEDED in standard_part(name).lower()


def is_kind(name, word, doc=None):
    """True when the standard part says `<DOC> - <Word>...`; with `doc`, that part.

    The KIND question, beside the currency one. The reader's pane has always
    asked it as a prefix, `startswith("<DOC> - <Word>")`, and this is that test
    with the part optional, for a reader that walks a whole folder rather than
    asking for one part. A prefix on the word rather than a whole word, because
    that is what the pane accepts; what follows the word (`v2`, `superseded
    <date>`) is versioning and history, which are `is_superseded`'s question
    and not this one. Case-insensitive, like the pane. A name with no part in
    front of the ` - ` is nobody's material and answers False.

    🔴 Until 2026-09-18 a shared pack decided kind by the substring
    `" - Transcript ("`, a naming convention smuggled into a type test: the
    `(` had to follow the word immediately, so a renamed transcript that every
    other reader served was left out of the pack, and the pack reported one
    transcript fewer with no error."""
    head, sep, rest = standard_part(name).partition(" - ")
    if not (head and sep and rest.lower().startswith(str(word).lower())):
        return False
    return doc is None or head.lower() == str(doc).lower()


def current(paths):
    """The candidates among `paths`, in sorted order: nothing superseded.

    Sorted so two current files for one part behave the same way on every run;
    the filter is what makes the FIRST one the right one rather than the one
    that happens to sort first."""
    return sorted(p for p in paths if not is_superseded(p))
