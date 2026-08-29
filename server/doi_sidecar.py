#!/usr/bin/env python3
"""Where a course keeps the DOI judgements somebody already made by hand.

Read by `verify_notes.py --dois`. Its own file rather than a function inside that script
because `verify_notes.py` is a SCRIPT: importing it runs every check and then exits, so a
loader living there could not be tested without running the gate.
"""
import json
import pathlib

DOI_SIDECAR = "verify-dois.json"


def load_doi_exceptions(course_dir):
    """({doi: why}, {doi: why}, [problem]) from `<course>/verify-dois.json`.

    🔴 These were two literals in this file until 2026-08-29, holding 30 DOIs out of the
    author's own two courses, in a file the kit SHIPS. A general verifier carrying one
    person's reading list gets one course worse every time a course is added, and a
    recipient had nowhere to put their own exceptions but a commit to the shared tool.
    They are course data, so they live beside the course.

    **An absent file means empty, and empty is the SAFE direction**: an exception list
    that does not load makes this check stricter, never quieter. A malformed one is
    reported and fails the run rather than being waved through, because a file somebody
    wrote and mistyped is a file they believe is working.

    🔴 The name is `verify-dois.json` and not `verify-notes.json`, which is what it wants
    to be called: `.gitignore` sends every `courses/**/*-notes.json` to the private-state
    pile (marks, notes, chats), so a sidecar named that way would never be committed and
    would look, to the next person, like it had simply vanished."""
    path = pathlib.Path(course_dir) / DOI_SIDECAR
    if not path.is_file():
        return {}, {}, []
    problems = []
    try:
        import json as _json
        data = _json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {}, {}, ["%s could not be read: %s" % (DOI_SIDECAR, exc)]
    if not isinstance(data, dict):
        return {}, {}, ["%s must hold an object with 'skip' and 'noted'" % DOI_SIDECAR]
    out = []
    for key in ("skip", "noted"):
        section = data.get(key) or {}
        if not isinstance(section, dict):
            problems.append("%s: '%s' must be an object of doi -> reason" % (DOI_SIDECAR, key))
            section = {}
        clean = {}
        for doi, why in section.items():
            if not str(doi).startswith("10."):
                problems.append("%s: %r in '%s' is not a DOI" % (DOI_SIDECAR, doi, key))
                continue
            clean[str(doi)] = str(why)
        out.append(clean)
    return out[0], out[1], problems
