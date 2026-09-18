#!/usr/bin/env python3
"""Carry a highlight onto the sentence that replaces the one it was sitting on.

    python3 server/move_marks.py --course PSY101 --doc W2-T3-P4 \
        --old "the old sentence, exactly" --new "the sentence replacing it" --dry-run

🟢 **EH's instruction, 2026-09-12:** *"since I'm changing a piece of highlighted
text, the highlight should just now cover that new text. My highlight covered two
sentences. One of them was changed, so it should still cover two sentences: the
one before the change and the new sentence that replaced the old sentence."*

🔴 **WHY A REWRITE ORPHANS A HIGHLIGHT, and why it is silent.** A mark is stored
as a block index, a start and end offset into that block's TEXT, and the exact
text it covers (`b`, `s`, `e`, `t`). **Change a sentence and all four go wrong at
once**: the stored text no longer matches, and every offset after the edit shifts
by the length difference. Nothing errors. The reader simply opens the lesson one
day and the highlight is somewhere else, or gone.

⚠️ **THE DEFAULT BEHAVIOUR OF IMPROVING A LESSON IS TO DESTROY THE READER'S OWN
WORK**, which is the worst outcome this project has, and it is the reason this is
a mechanism rather than a careful edit.

🔴 **IT REFUSES RATHER THAN GUESSES ON A PARTIAL OVERLAP.** A mark that starts or
ends in the middle of the changed sentence has no correct answer: the reader
highlighted half of something that no longer exists. Those are reported by id and
nothing is written. **A tool that guesses here would move a highlight onto words
the reader never chose, silently, which is the same failure in a new coat.**

🟢 **THE BLOCK MODEL IS `import_marks.block_texts`, NOT A SECOND SPELLING OF IT**,
and it is validated against the reader's own output rather than against argument:
for all **148** marks EH has on disk today, `block_texts(lesson)[b][s:e]` equals
the mark's stored `t` exactly. That check is a test, so the day the two drift the
suite says so.

⚠️ **ON THE SHRINK GUARD.** Moving a mark changes its text, and `mark_key` is
(block, text), so this RETIRES one identity and MINTS another. That is the shape
the guard watches. This writes the file directly rather than through the API, so
the guard does not run here; what matters is the NEXT save from a page that was
already open. It declares the OLD key in its base, the merge finds a row it never
saw, and adopts it. **Adoption is the safe direction and it is tested.**
"""
import argparse
import json
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import import_marks as IM                                      # noqa: E402

ROOT = HERE.parent


class Refused(Exception):
    """Nothing was written. The message names every mark that has no answer."""


class PartlyWritten(Exception):
    """The marks moved and then the lesson write failed.

    🔴🔴 **THIS EXISTS BECAUSE THE CORRECT REFUSAL MESSAGE TEACHES A WRONG
    INFERENCE ABOUT EVERYTHING ELSE.** A `Refused` prints *"Nothing was
    written."*, so a user who has seen that reads any later traceback the same
    way: it crashed, so nothing happened. **The failed-write case is the one
    moment that reading is false**, and it was reaching them as a bare
    `PermissionError`.

    ⚠️ **`restored` is the field that matters and it is not decoration.** The
    marks are put back from the backup when the lesson cannot be written, which
    makes `apply`'s "or neither" promise true rather than aspirational. **If the
    restore ALSO fails, the file really is out of step with the lesson**, and
    the message has to say so and name the backup, because at that point only a
    person can finish it."""

    def __init__(self, error, marks_path, backup, restored):
        self.error, self.marks_path = error, marks_path
        self.backup, self.restored = backup, restored
        super().__init__(str(error))


def flexible(phrase):
    """A regex matching `phrase` however the file happens to wrap it.

    🔴 **WHITESPACE IS THE WHOLE REASON THIS EXISTS.** A sentence is quoted to
    us as prose, on one line. The lesson stores it wrapped across lines with
    eight spaces of indent, and `textContent` keeps every one of those
    characters, which is what the reader's offsets count. A literal match finds
    nothing and reports "not in this lesson", which is a true sentence about the
    wrong question."""
    return re.compile(r"\s+".join(re.escape(w) for w in phrase.split()))


def locate(blocks, phrase):
    """(block index, start, end) of `phrase` in the blocks, or Refused.

    🔴 Exactly once across the WHOLE lesson. A sentence that appears twice gives
    nothing to choose by, and picking the first is a guess that can land a
    reader's highlight on a passage they never chose.

    ⚠️ And it must sit inside ONE block. A mark carries a single block index, so
    a passage spanning a term and its definition, or two paragraphs, is not
    something a mark can cover and not something this can move."""
    pat = flexible(phrase)
    hits = [(i, m.start(), m.end())
            for i, b in enumerate(blocks) for m in pat.finditer(b)]
    if not hits:
        raise Refused(
            "that sentence is not inside any single block of this lesson. It "
            "may be the SOURCE's wording rather than the lesson's, or it may "
            "span two blocks (a term and its definition, say), which is not "
            "something one mark can cover")
    if len(hits) > 1:
        raise Refused("that sentence appears %d times (blocks %s), so nothing "
                      "says which one the rewrite means"
                      % (len(hits), ", ".join(str(i) for i, _s, _e in hits)))
    return hits[0]


def plan(blocks, items, old, new):
    """(moves, untouched) for one rewrite, or Refused naming every hard case.

    A move is `(mark, new_s, new_e, new_t)`. Nothing is mutated here, so
    `--dry-run` and the real run take one path rather than two that can
    disagree."""
    bi, at, end = locate(blocks, old)
    delta = len(new) - (end - at)
    moves, untouched, partial = [], [], []
    for m in items:
        if m.get("b") != bi:
            untouched.append(m)
            continue
        s, e = int(m.get("s", 0)), int(m.get("e", 0))
        if e <= at or s >= end:
            # 🔴 A mark AFTER the change must move even though it covers none of
            # it: every offset after an edit shifts by the length difference.
            # This is the half the entry's shape did not name, and leaving it
            # out would move one highlight correctly and break every later one.
            if s >= end and delta:
                moves.append((m, s + delta, e + delta, m.get("t")))
            else:
                untouched.append(m)
        elif s <= at and e >= end:
            t = m.get("t") or ""
            cut = at - s
            moves.append((m, s, e + delta, t[:cut] + new + t[cut + (end - at):]))
        else:
            partial.append(m)
    if partial:
        raise Refused(
            "%d mark(s) cover only PART of the changed sentence and have no "
            "correct answer: %s. The reader highlighted half of something that "
            "no longer exists, so it is a question for them rather than a guess "
            "for us." % (len(partial),
                         ", ".join("id %s" % m.get("id") for m in partial)))
    return moves, untouched


def plan_after(blocks, items, old, new):
    """`plan`, for a lesson whose prose has ALREADY been changed.

    🟢 The sentence begins at the same offset in both versions, because nothing
    before it moved. So the NEW text locates the edit, and the marks' own spans
    still carry the old length, which is what the arithmetic needs."""
    bi, at, end_new = locate(blocks, new)
    end_old = at + (end_new - at) - (len(new) - len(old))
    moves, untouched, partial = [], [], []
    delta = (end_new - at) - (end_old - at)
    for m in items:
        if m.get("b") != bi:
            untouched.append(m)
            continue
        s, e = int(m.get("s", 0)), int(m.get("e", 0))
        if e <= at or s >= end_old:
            if s >= end_old and delta:
                moves.append((m, s + delta, e + delta, m.get("t")))
            else:
                untouched.append(m)
        elif s <= at and e >= end_old:
            t = m.get("t") or ""
            cut = at - s
            moves.append((m, s, e + delta,
                          t[:cut] + blocks[bi][at:end_new] + t[cut + (end_old - at):]))
        else:
            partial.append(m)
    if partial:
        raise Refused(
            "%d mark(s) cover only PART of the changed sentence: %s"
            % (len(partial), ", ".join("id %s" % m.get("id") for m in partial)))
    return moves, untouched


def backup(path):
    """Timestamped, beside the original, per the standing rule on user data."""
    dest = path.with_name("%s.%s.bak"
                          % (path.name, datetime.now().strftime("%Y%m%d-%H%M%S")))
    shutil.copy2(path, dest)
    return dest


def apply(lesson_path, marks_path, old, new, write=True, marks_only=False):
    """Rewrite the lesson AND move the marks, or neither. Returns a report.

    ⚠️ **"OR NEITHER" WAS TRUE OF EVERY REFUSAL AND FALSE OF A FAILED WRITE**,
    which is `study-hub-qa`'s finding against this module's own promise. The
    marks are written first, so between the two writes the file pointed at a
    sentence the lesson did not contain. 🟢 **Now the marks are put back from
    the backup and `PartlyWritten` is raised**, so the sentence above is a
    guarantee rather than an intention. 🔴 **The one case it still cannot cover
    is the restore ALSO failing**, and then `PartlyWritten.restored` is False
    and the message says what a person has to finish.

    🔴 **`marks_only` IS FOR THE CASE THIS TOOL CANNOT EDIT SAFELY.** A sentence
    that spans inline markup (`<b>`, a link, an entity) exists in the block's
    TEXT and not in the source, so a source-level replace cannot find it, and
    one that guessed would delete the markup. Then the prose edit belongs to
    whoever owns the prose, and this moves the marks afterwards.

    🟢 **The arithmetic is the same either way, and that is not luck**: every
    character before the changed sentence is untouched, so the sentence begins
    at the SAME offset in the old text and the new one. The only thing that
    moves is what comes after it."""
    html = lesson_path.read_text(encoding="utf-8")
    blocks = IM.block_texts(html)
    src_span = None
    if not marks_only:
        # locate() first, so "not in this lesson" is answered against the TEXT
        # the reader sees rather than against the markup.
        locate(blocks, old)
        found = list(flexible(old).finditer(html))
        if len(found) > 1:
            raise Refused(
                "the sentence is in the lesson's TEXT exactly once but appears "
                "%d times in its SOURCE, so the edit itself is ambiguous"
                % len(found))
        # 🔴 ZERO IS THE COMMON CASE, NOT AN ODD ONE, and the first draft of this
        # could not reach the message below because of it. A `<b>` inside the
        # sentence breaks the source match entirely, so the count is 0 rather
        # than 1-with-a-tag-inside; the tag-inside case only happens when the
        # markup sits where whitespace is. Both mean the same thing and get the
        # same sentence.
        markup = (not found) or "<" in html[found[0].start():found[0].end()]
        if markup:
            raise Refused(
                "that sentence carries inline markup or an entity in the "
                "source, so replacing it here would delete the markup. Make the "
                "prose edit by hand, then re-run with --marks-only and the "
                "marks will follow it.")
        src_span = found[0].span()
    doc = json.loads(marks_path.read_text(encoding="utf-8")) if marks_path.exists() else {}
    items = doc.get("items") or []
    # 🔴 Two planners because the lesson is in two different states, not because
    # the arithmetic differs: `plan` measures against a sentence still on the
    # page, `plan_after` against one already replaced.
    moves, untouched = (plan_after(blocks, items, old, new) if marks_only
                        else plan(blocks, items, old, new))
    report = {"moved": len(moves), "untouched": len(untouched),
              "marks_file": marks_path.name, "backup": None}
    if not write:
        return report
    # 🔴 The marks first, and a backup before either. If the lesson were written
    # first and this raised, the file and the marks would disagree with nothing
    # recording that they do.
    if items:
        report["backup"] = str(backup(marks_path))
    for m, s, e, t in moves:
        m["s"], m["e"], m["t"] = s, e, t
    if items:
        marks_path.write_text(json.dumps(doc, ensure_ascii=False, indent=1),
                              encoding="utf-8")
    if not marks_only:
        # 🔴 THE WINDOW THIS GUARDS IS THE ONLY ONE "or neither" CAN BREAK IN.
        # Marks-first with a backup is still the right ORDER (writing the lesson
        # first and failing here would leave the two disagreeing with nothing
        # recording it), so the fix is not to swap them: it is to put the marks
        # back when the second write does not happen.
        try:
            lesson_path.write_text(
                html[:src_span[0]] + new + html[src_span[1]:], encoding="utf-8")
        except OSError as e:
            restored = True                    # nothing to undo if none moved
            if items and report["backup"]:
                try:
                    shutil.copy2(report["backup"], marks_path)
                except OSError:
                    restored = False
            raise PartlyWritten(e, marks_path, report["backup"], restored) from e
    return report


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--course", required=True)
    ap.add_argument("--doc", required=True)
    ap.add_argument("--old", required=True)
    ap.add_argument("--new", required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--marks-only", action="store_true",
                    help="the prose edit has already been made; move the marks "
                         "to follow it")
    a = ap.parse_args(argv)
    folder = ROOT / "courses" / a.course
    lesson = next((p for p in folder.glob(a.doc + "-*.html")), None)
    if not lesson:
        print("no lesson for %s in %s" % (a.doc, folder))
        return 2
    marks = folder / ("%s-marks.json" % a.doc)
    try:
        r = apply(lesson, marks, a.old, a.new, write=not a.dry_run,
                  marks_only=a.marks_only)
    except Refused as e:
        print("REFUSED: %s" % e)
        print("Nothing was written.")
        return 1
    except PartlyWritten as e:
        # 🔴 SAY WHICH OF THE TWO FILES MOVED. "It crashed" is the inference the
        # refusal message above teaches, and here it is false.
        print("FAILED: the lesson could not be written: %s" % e.error)
        if e.restored:
            print("The marks were put back, so nothing has changed.")
        else:
            print("🔴 THE MARKS WERE MOVED AND THE LESSON WAS NOT, and putting "
                  "them back failed too.")
            print("   They now point at a sentence the lesson does not contain.")
            print("   Restore %s from %s" % (e.marks_path.name,
                                             Path(e.backup).name if e.backup else "the backup"))
        return 1
    # 🟢 A positive result even when nothing moved: silence and a check that
    # never ran look identical from outside.
    print("%s %s: %d mark(s) moved, %d left alone"
          % ("would move in" if a.dry_run else "moved in", lesson.name,
             r["moved"], r["untouched"]))
    if r["backup"]:
        print("  marks backed up to %s" % Path(r["backup"]).name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
