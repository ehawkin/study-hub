#!/usr/bin/env python3
"""Render every figure in a lesson, at the width and in the theme the reader gets.

    python3 server/figcheck.py courses/<COURSE>/W1-T1-P1-something.html
    python3 server/figcheck.py --course <COURSE>          # every lesson in one page
    python3 server/figcheck.py <lesson> --serve           # ... and keep serving

🔴 **NOTE-SPEC §E.7 has required this step since 2026-08-23 and named this file as
the way to do it. This file did not exist.** The step was carried by hand, by
whichever session remembered, and a manual step that each session reinvents is a
step some sessions skip.

🟢 **THE STEP EARNS ITSELF, which is why this is a tool rather than a deleted
sentence.** Roughly one figure in four has a defect that is invisible in the
markup and obvious in the picture: a legend clipped at the viewBox edge, an arrow
label sitting on a box, an arrow running through a caption. Measured on the first
five figures anybody checked, two were defective; Week 4's four produced four.

🔴 **WHY IT SERVES INSTEAD OF WRITING A FILE: `file://` is refused by the browser
tool.** That single fact is why a server is needed at all, and it is the detail
every future session would otherwise rediscover.

🔴🔴 **WHAT MAKES THIS FAITHFUL, and it is the lesson `rig_plate_render.py` paid
for: a rendering check is only worth the fidelity of the element it renders.**

- **The lesson's `<style>` and its `<div class="wrap">` go in VERBATIM**, straight
  from `split_lessons.read_content`. Nothing is extracted, rewritten or
  reassembled, so a rule that reaches a figure through `.wrap`, `section` or
  `.col` still reaches it here. **The recipe this replaces lifted the figures OUT
  of their ancestry**, which loses exactly the width constraints that produce the
  defects being looked for.
- **The only edit to the lesson's own markup is an `id` on each `<figure>` that
  does not already have one**, so the anchors work. An id cannot move a pixel.
- **The reader layer is NOT loaded and no lesson JS runs.** That is a deliberate
  trade, stated rather than hidden: the layer's panes change the lesson's width
  when they open, and a figure that survives here can still collide at a narrower
  column. ⚠️ **This tool answers "is the figure sound at the reader's full width",
  not "is it sound at every width".**

🟢 **BOTH THEMES, and stamped rather than inherited.** Each page carries
`data-theme` on `<html>`, so the answer does not depend on the OS setting of
whoever is looking. The lessons define their dark palette under both
`prefers-color-scheme` and `[data-theme="dark"]`, so stamping is the reliable half.

⚠️ **It prints a COUNT even when everything is fine.** A tool that is silent on
success is indistinguishable from a tool that did not run.

## 🟢 CLIPPING IS MEASURED (2026-09-09)

**Every page checks itself**: for each `<text>` in each figure's `<svg>`, is its
rectangle inside the `<svg>`'s? The answer is in the bar, in words, **even when it
is zero** (*"no text outside its box in 4 figure(s)"*), and on the console with a
`[figcheck]` prefix so a driven browser can read it without looking.
`data-fc-clip`, `data-fc-overlap` and `data-fc-figures` on `<html>` carry the
same numbers for a script.

🔴 **Screen rectangles, not viewBox arithmetic.** `getBoundingClientRect` needs no
coordinate system, no transform chain and no `viewBox` parsing, and it is **not**
affected by an ancestor's overflow clipping, so a label the browser has already
cut off still reports where it would have been. That is exactly the defect being
looked for.

## 🟢 AND SO IS A LABEL SITTING ON A BOX (2026-09-09, later the same day)

**Eight of those shipped in one course.** They were found by a script written for
that sitting and then thrown away, so the class had **exactly one witness in the
project's history and no way to get a second**. From outside, "nothing checks
this" and "this is checked and clean" were the same silence.

🔴 **THE RULE, and the distinction is the whole check: containment is FINE,
straddling is the finding.** A node label sits inside its own filled box in every
diagram ever drawn. What is wrong is a label lying half on a box and half off,
which is what an arrow label landing on a node actually looks like as geometry.
Without that distinction the check reports every correctly-labelled node in the
corpus and is never read again.

⚠️ **Two thresholds, both MEASURED over all 110 lessons rather than chosen.**

- **A contact under 2px is rounding**, not a defect. The two touches the queue
  entry called "1px" are really **1.20px and 1.23px**, and the true defects are
  **5px, 6px and 10px**: anything from 2 to 5 separates them, and 2.0 is the low
  end, so the check stays as sensitive as the evidence allows. It also holds
  without this corpus, which matters because two points are thin: a 1px stroke on
  an edge puts 0.5px each side and a text rect rounds outward.
- **A filled shape under 12px on either side is decoration**, because an
  **arrowhead** is a filled path a few pixels across and a label at the end of an
  arrow overlaps it every time.

🟢 **The false-positive rate is measured, not hoped for: 110 lessons, 194 figures,
ONE finding.**

🔴 **The viewBox trick that fixes CLIPPING cannot fix these**, and that is worth
carrying: widening moves the label and the box together, so an overlap inside the
picture is unmoved. Different defect, different fix.

⚠️ **This still does not replace looking.** An arrow running through a caption is
not a bounding box and remains the eye's.
"""
import argparse
import http.server
import pathlib
import re
import sys
import tempfile

SERVER = pathlib.Path(__file__).resolve().parent
REPO = SERVER.parent
sys.path.insert(0, str(SERVER))
import split_lessons as SPLIT                                    # noqa: E402

PORT = 8803          # not 8795 (his), 8796 (TLS), 8797 (chat jump), 8798 (place),
                     # 8799 (speed) or 8802 (the plate rig)

FIGURE_OPEN = re.compile(r"<figure\b([^>]*)>", re.I)
LABEL = re.compile(r'aria-label="([^"]*)"', re.I)


def figures_in(body):
    """(body with an id on every figure, [(id, label)]), in document order.

    The id is added only where the author has not given one, and an attribute
    that changes nothing about layout is the only edit this tool makes to a
    lesson's markup.

    ⚠️ The label is read from INSIDE each figure's own span, not by collecting
    every `aria-label` on the page and pairing them off by position. The first
    version did the latter and lined up only by luck: any `aria-label` anywhere
    else in the lesson would have shifted every label onto the wrong figure, and
    an index that names the wrong picture is worse than one that names none.
    """
    out, found, pos = [], [], 0
    for m in FIGURE_OPEN.finditer(body):
        attrs = m.group(1)
        n = len(found) + 1
        existing = re.search(r'\bid="([^"]*)"', attrs, re.I)
        fid = existing.group(1) if existing else "fig-%d" % n
        out.append(body[pos:m.start()])
        out.append(m.group(0) if existing
                   else '<figure id="%s"%s>' % (fid, attrs))
        pos = m.end()
        end = body.find("</figure>", m.end())
        inside = body[m.end():end if end >= 0 else len(body)]
        lab = LABEL.search(inside)
        found.append((fid, lab.group(1) if lab else ""))
    out.append(body[pos:])
    return "".join(out), found


def lesson_page(path, theme):
    """One lesson, standalone, stamped with a theme. Returns (html, figures)."""
    text = path.read_text(encoding="utf-8")
    if not SPLIT.is_content_file(text):
        raise SystemExit("%s is not a lesson content file" % path.name)
    meta, body, title = SPLIT.read_content(text, path.name)
    body, figs = figures_in(body)
    return PAGE % {
        "theme": theme,
        "title": (meta.get("title") or path.name),
        "name": path.name,
        "count": len(figs),
        "body": body,
    }, figs


PAGE = """<!doctype html>
<html lang="en" data-theme="%(theme)s">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>figcheck %(theme)s: %(title)s</title>
%(body)s
<style>
  /* figcheck's own chrome, last so it wins for its own elements, and namespaced
     so it cannot reach anything the lesson styles. */
  .fc-bar {
    position: fixed; top: 0; left: 0; right: 0; z-index: 99;
    font: 12px/1.4 ui-monospace, SFMono-Regular, Menlo, monospace;
    background: #1A2830; color: #fff; padding: 6px 12px;
  }
  .fc-bar a { color: #9ED9CC; margin-right: 10px; }
  .fc-wait { color: #9AA7AD; }
  .fc-ok { color: #7BD3A0; }
  /* Loud on purpose: this is the one state that wants interrupting for. */
  .fc-bad { color: #1A2830; background: #F2B705; padding: 1px 7px; border-radius: 3px; font-weight: 700; }
  body { padding-top: 34px !important; }
  figure:target { outline: 2px solid #C0392B; outline-offset: 8px; }
</style>
<div class="fc-bar">figcheck &middot; %(name)s &middot; theme <b>%(theme)s</b>
  &middot; %(count)d figure(s) &middot;
  <a href="./index.html">index</a>
  &middot; <span id="fc-clip" class="fc-wait">measuring...</span>
  &middot; <span id="fc-over" class="fc-wait">measuring...</span></div>
<script>
/* 🔴 THE HALF THE EYE KEEPS MISSING, and it is the half that is measurable.
   The reporter of this check missed a clipped label by eye on a screenshot, and
   BOTH times a label has escaped its viewBox in this project the variable was
   WIDTH, which is exactly what this page renders faithfully. Looking is still
   required for the defects that are not measurable (an arrow through a caption,
   a label sitting on a box); this closes the one that is. */

/* The decision, kept PURE and separate from the DOM so the suite can run it:
   which sides of `frame` does `box` escape? Rects in screen space, so no SVG
   coordinate system, no viewBox arithmetic and no transform chain to get wrong.
   🟢 getBoundingClientRect is not affected by an ancestor's overflow clipping,
   so a label the browser has already CLIPPED still reports where it would have
   been, which is the whole point. */
function fcOutside(box, frame, tol) {
  var sides = [];
  if (box.left   < frame.left   - tol) sides.push('left');
  if (box.right  > frame.right  + tol) sides.push('right');
  if (box.top    < frame.top    - tol) sides.push('top');
  if (box.bottom > frame.bottom + tol) sides.push('bottom');
  return sides;
}

/* 🔴 THE SECOND DEFECT CLASS: A LABEL SITTING ON A BOX (2026-09-09).
   Eight of these shipped in one course and were found by a script written for
   that sitting and then thrown away, so the class had exactly one witness in the
   project's history. This is that witness made permanent.

   ⚠️ THE HARD PART IS THAT MOST TEXT-ON-BOX IS CORRECT. A node label sits inside
   its own filled box in every diagram ever drawn; that is the design, not a
   defect. What is wrong is a label that STRADDLES a box - half on it, half off -
   which is what "an arrow label sitting on a node" actually looks like in
   geometry. So containment is FINE and partial overlap is the finding, and that
   distinction is the whole check.

   🔴 It also means the viewBox trick that fixed the seven CLIPPED labels cannot
   fix these, and the content session recorded the same thing from the other end:
   widening moves the label and the box together, so an overlap INSIDE the picture
   is unmoved. Different defect, different fix, and now a different check. */
var FC_OVERLAP_TOL = 2.0;

function fcStraddle(box, shape, tol) {
  var ix = Math.min(box.right, shape.right) - Math.max(box.left, shape.left);
  var iy = Math.min(box.bottom, shape.bottom) - Math.max(box.top, shape.top);
  /* No contact at all, or a contact thinner than the tolerance. 🔴 The tolerance
     is what keeps rounding out of the report, and a check that reports rounding
     teaches its reader to skim it, which costs more than the check is worth.

     🟢 THE NUMBER IS MEASURED, NOT CHOSEN. Swept over all 110 lessons: the two
     contacts the entry called "1px touches" are 1.20px and 1.23px, and the real
     defects are 5px, 6px and 10px. Anything from 2 to 5 separates them; 2.0 is
     the low end, so the check stays as sensitive as the evidence allows.
     ⚠️ It also has a reason independent of this corpus, which matters because two
     data points are thin: a 1px stroke centred on an edge already puts 0.5px each
     side, and a text rect rounds outward, so contacts under ~2px are what correct
     rendering produces. */
  if (ix <= tol || iy <= tol) return 0;
  /* 🟢 INSIDE ITS BOX IS THE NORMAL CASE, and the single most important line
     here. Without it every correctly-labelled node in every diagram is a
     finding, the check reports hundreds, and nobody ever reads it again. */
  if (box.left >= shape.left - tol && box.right <= shape.right + tol &&
      box.top >= shape.top - tol && box.bottom <= shape.bottom + tol) return 0;
  /* How far in it reaches. The thinner of the two overlaps is the intrusion:
     a label lying across the top edge of a wide box overlaps it for the label's
     whole width, and what matters is the few pixels of height.
     🔴 UNROUNDED, so the caller can classify on the real value: 1.20 and 1.48 sit
     on opposite sides of a judgement and both round to 1. */
  return Math.min(ix, iy);
}

/* ⚠️ `text` and not `text, tspan`: a `<text>`'s rect is the union of its tspan
   lines, so checking both reports the same overflow twice and reads as two
   defects. */
function fcScan(tol) {
  var rows = [], svgs = document.querySelectorAll('figure svg'), i, j;
  for (i = 0; i < svgs.length; i++) {
    var frame = svgs[i].getBoundingClientRect();
    var fig = svgs[i].closest('figure');
    var texts = svgs[i].querySelectorAll('text');
    for (j = 0; j < texts.length; j++) {
      var box = texts[j].getBoundingClientRect();
      if (!box.width && !box.height) continue;   /* an empty label is not a defect */
      var sides = fcOutside(box, frame, tol);
      if (sides.length) {
        rows.push({figure: (fig && fig.id) || ('svg-' + (i + 1)),
                   text: (texts[j].textContent || '').trim().slice(0, 60),
                   sides: sides.join('+'),
                   over: Math.round(Math.max(
                     frame.left - box.left, box.right - frame.right,
                     frame.top - box.top, box.bottom - frame.bottom))});
      }
    }
  }
  return {figures: svgs.length, bad: rows};
}

/* Which shapes count as a BOX. 🔴 `fill` decides, not the tag: a `<path>` with
   `fill:none` is an arrow or a connector, and a label crossing one is not this
   defect. An unfilled shape cannot be sat on.

   ⚠️ MIN SIDE, and it is a real judgement rather than a tidy-up. An ARROWHEAD is
   a filled path a few pixels across, and a label that legitimately sits at the
   end of an arrow overlaps its head every time. Reporting those would bury the
   eight real findings under a hundred, which is the failure mode this check was
   asked not to have. Anything smaller than `minSide` on either axis is treated
   as decoration. */
/* Pure, so the suite can run it: is this rect big enough to be a BOX somebody
   could sit a label on? 🔴 Separated from `fcBoxes` because a mutation sweep
   survived here - the guard was real, tested by nothing, and a browser is the
   only place the DOM half runs. */
function fcIsBox(r, minSide) {
  return r.width >= minSide && r.height >= minSide;
}

function fcBoxes(svg, minSide) {
  var out = [], els = svg.querySelectorAll('rect,circle,ellipse,polygon,path'), i;
  for (i = 0; i < els.length; i++) {
    var cs = window.getComputedStyle(els[i]);
    var fill = (cs.fill || '').trim().toLowerCase();
    if (!fill || fill === 'none' || fill === 'transparent') continue;
    if (cs.fillOpacity !== '' && parseFloat(cs.fillOpacity) === 0) continue;
    if (cs.visibility === 'hidden' || cs.display === 'none') continue;
    var r = els[i].getBoundingClientRect();
    if (!fcIsBox(r, minSide)) continue;
    out.push(r);
  }
  return out;
}

/* 🔴 ONE ROW PER LABEL, not one per box it touches. A label lying across the
   join between two adjacent boxes straddles both, and reporting it twice reads
   as two defects in a list whose whole value is that its length is the number of
   things to fix. The worst straddle wins. */
function fcScanOverlap(tol, minSide) {
  var rows = [], near = [], svgs = document.querySelectorAll('figure svg'), i, j, k;
  for (i = 0; i < svgs.length; i++) {
    var fig = svgs[i].closest('figure');
    var boxes = fcBoxes(svgs[i], minSide);
    var texts = svgs[i].querySelectorAll('text');
    for (j = 0; j < texts.length; j++) {
      var box = texts[j].getBoundingClientRect();
      if (!box.width && !box.height) continue;
      var worst = 0;
      for (k = 0; k < boxes.length; k++) {
        /* Measured at ZERO tolerance and classified afterwards, so a contact is
           never silently DROPPED - only quietened. */
        var d = fcStraddle(box, boxes[k], 0);
        if (d > worst) worst = d;
      }
      if (!worst) continue;
      var row = {figure: (fig && fig.id) || ('svg-' + (i + 1)),
                 text: (texts[j].textContent || '').trim().slice(0, 60),
                 by: Math.round(worst * 100) / 100};
      if (worst > tol) rows.push(row); else near.push(row);
    }
  }
  return {figures: svgs.length, bad: rows, near: near};
}

/* 🟢 IT SAYS SOMETHING WHEN IT IS FINE. A check that is silent on success cannot
   be told apart from a check that never ran, which is this project's own rule
   and the reason `verify_packages.py` grew a count on success the same week. */
function fcReport(res) {
  var el = document.getElementById('fc-clip');
  var msg;
  if (!res.figures) msg = 'no figure in this lesson';
  else if (!res.bad.length)
    msg = 'no text outside its box in ' + res.figures + ' figure(s)';
  else msg = res.bad.length + ' label(s) OUTSIDE the box';
  el.textContent = msg;
  el.className = res.bad.length ? 'fc-bad' : 'fc-ok';
  document.documentElement.setAttribute('data-fc-clip', String(res.bad.length));
  document.documentElement.setAttribute('data-fc-figures', String(res.figures));
  console.log('[figcheck] ' + msg);
  for (var k = 0; k < res.bad.length; k++) {
    var b = res.bad[k];
    console.log('[figcheck] ' + b.figure + ' ' + b.sides + ' by ' + b.over +
                'px: ' + JSON.stringify(b.text));
  }
  return res;
}

/* 🟢 IT SAYS SOMETHING WHEN IT IS FINE, same rule as the clipping half above and
   the reason this entry was filed at all: the sweep that found the eight was a
   one-off script, so from outside, "nothing runs this" and "this runs and finds
   nothing" were the same silence. `all N figure labels clear` is what makes the
   check's own absence visible. */
function fcReportOverlap(res) {
  var el = document.getElementById('fc-over');
  var msg;
  var also = res.near.length ? ' (+' + res.near.length + ' within ' +
             FC_OVERLAP_TOL + 'px, likely rounding)' : '';
  if (!res.figures) msg = 'no figure to check for overlap';
  else if (!res.bad.length)
    msg = 'all labels clear of their boxes in ' + res.figures + ' figure(s)' + also;
  else msg = res.bad.length + ' label(s) ON a box' + also;
  el.textContent = msg;
  el.className = res.bad.length ? 'fc-bad' : 'fc-ok';
  document.documentElement.setAttribute('data-fc-overlap', String(res.bad.length));
  document.documentElement.setAttribute('data-fc-near', String(res.near.length));
  console.log('[figcheck] ' + msg);
  for (var k = 0; k < res.bad.length; k++) {
    var b = res.bad[k];
    console.log('[figcheck] ' + b.figure + ' label on a box by ' + b.by +
                'px: ' + JSON.stringify(b.text));
  }
  /* 🔴 The quiet band is PRINTED, never hidden. Silently dropping a contact is
     how a real 1.48px defect would vanish beside a rounding 1.20px one. */
  for (var m = 0; m < res.near.length; m++) {
    var n = res.near[m];
    console.log('[figcheck] near-miss ' + n.figure + ' ' + n.by + 'px: ' +
                JSON.stringify(n.text));
  }
  return res;
}

/* ⚠️ AFTER THE FONTS, not on DOMContentLoaded. A label measured in the fallback
   face is a label measured at the wrong width, and the whole check is about
   width.

   🔴🔴 A TIMER AND NOT `requestAnimationFrame`, MEASURED THE HARD WAY. The first
   version waited for a frame, which never comes in a BACKGROUND tab: Chrome
   pauses rAF when the page is hidden, so the bar sat on "measuring..." for ever
   and `data-fc-clip` was never set. **A driven browser opens pages hidden**, so
   the check looked like it had not run at all, which is the same shape this
   project already recorded for pdf.js: a hidden tab looks exactly like a hang.
   ⚠️ Timers are throttled in a hidden tab but they still FIRE, and no frame is
   needed anyway: `getBoundingClientRect` forces layout itself. */
(function () {
  /* 🔴 SEPARATE try BLOCKS, deliberately. One check throwing must not leave the
     other reading "measuring..." for ever, which would look exactly like the tool
     not having run - the failure this whole file exists to stop. */
  function go() {
    try { fcReport(fcScan(0.5)); } catch (e) {
      var el = document.getElementById('fc-clip');
      if (el) { el.textContent = 'check FAILED: ' + e.message; el.className = 'fc-bad'; }
      console.log('[figcheck] clipping check failed: ' + e.message);
    }
    try { fcReportOverlap(fcScanOverlap(FC_OVERLAP_TOL, 12)); } catch (e2) {
      var el2 = document.getElementById('fc-over');
      if (el2) { el2.textContent = 'check FAILED: ' + e2.message; el2.className = 'fc-bad'; }
      console.log('[figcheck] overlap check failed: ' + e2.message);
    }
  }
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(function () {
    setTimeout(go, 0);
  });
  else window.addEventListener('load', function () { setTimeout(go, 0); });
}());
</script>
"""

INDEX = """<!doctype html>
<html lang="en"><meta charset="utf-8">
<title>figcheck: %(what)s</title>
<style>
  body { font: 14px/1.6 system-ui, sans-serif; margin: 0; padding: 24px; background: #fff; color: #111; }
  h1 { font-size: 17px; margin: 0 0 4px; }
  p.sub { color: #555; margin: 0 0 18px; }
  table { border-collapse: collapse; width: 100%%; }
  td, th { border-bottom: 1px solid #ddd; padding: 7px 10px; text-align: left; vertical-align: top; }
  th { background: #f4f5f7; font-size: 12px; text-transform: uppercase; letter-spacing: .05em; }
  td.lab { color: #444; font-size: 13px; }
  a { color: #12695C; }
  .none { color: #A25E14; }
</style>
<h1>figcheck: %(what)s</h1>
<p class="sub">%(count)d figure(s) across %(lessons)d lesson(s). Open each one in
BOTH themes and look at it: a label sitting on a box, an arrow through a caption.
Neither shows up in the markup. <b>Text escaping the viewBox is now measured for
you</b> and the answer is in the bar at the top of every page, in words even when
it is zero; what is left for the eye is everything that is not a bounding box.</p>
<table><tr><th>lesson</th><th>figure</th><th>light</th><th>dark</th><th>what it says it is</th></tr>
%(rows)s
</table>
"""


def build(dest, lessons):
    rows, total = [], 0
    for path in lessons:
        for theme in ("light", "dark"):
            html, figs = lesson_page(path, theme)
            (dest / ("%s.%s.html" % (path.stem, theme))).write_text(html, encoding="utf-8")
        total += len(figs)
        if not figs:
            rows.append('<tr><td>%s</td><td colspan="4" class="none">no figure in this lesson</td></tr>'
                        % path.name)
            continue
        for fid, label in figs:
            rows.append(
                '<tr><td>%s</td><td><code>%s</code></td>'
                '<td><a href="%s.light.html#%s">light</a></td>'
                '<td><a href="%s.dark.html#%s">dark</a></td>'
                '<td class="lab">%s</td></tr>'
                % (path.name, fid, path.stem, fid, path.stem, fid,
                   (label or "")[:160]))
    (dest / "index.html").write_text(INDEX % {
        "what": (lessons[0].name if len(lessons) == 1
                 else "%s, %d lessons" % (lessons[0].parent.name, len(lessons))),
        "count": total, "lessons": len(lessons), "rows": "\n".join(rows),
    }, encoding="utf-8")
    return total


def serve(dest, port):
    class Quiet(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=str(dest), **k)

        def log_message(self, *a):
            pass

    with http.server.ThreadingHTTPServer(("127.0.0.1", port), Quiet) as srv:
        print("serving on http://127.0.0.1:%d/  (ctrl-c to stop)" % port)
        srv.serve_forever()


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("lesson", nargs="?", help="a lesson content file")
    p.add_argument("--course", default="", help="every lesson in courses/<CODE>")
    p.add_argument("--out", default="", help="write here instead of a temp directory")
    p.add_argument("--serve", action="store_true", help="run the server too")
    p.add_argument("--port", type=int, default=PORT)
    a = p.parse_args(argv)

    if a.course:
        folder = REPO / "courses" / a.course
        if not folder.is_dir():
            raise SystemExit("no course at %s" % folder)
        lessons = SPLIT.lessons_in(folder)
        if not lessons:
            raise SystemExit("no lesson in %s" % folder)
    elif a.lesson:
        one = pathlib.Path(a.lesson)
        if not one.is_file():
            raise SystemExit("no lesson at %s" % one)
        lessons = [one]
    else:
        raise SystemExit("name a lesson, or --course <CODE>")

    dest = (pathlib.Path(a.out).expanduser().resolve() if a.out
            else pathlib.Path(tempfile.mkdtemp(prefix="figcheck-")))
    dest.mkdir(parents=True, exist_ok=True)
    total = build(dest, lessons)

    # 🔴 A POSITIVE RESULT, even when there is nothing to say. A lesson with no
    # figure and a tool that failed to find them look identical otherwise.
    print("%d figure(s) across %d lesson(s), written to %s"
          % (total, len(lessons), dest))
    if not total:
        print("  no figure in %s: nothing to look at, which is an answer"
              % ("this lesson" if len(lessons) == 1 else "any of these lessons"))
    print("  open http://127.0.0.1:%d/index.html  and LOOK at every one, in BOTH themes"
          % a.port)
    print("  🔴 file:// is refused by the browser tool, so it has to be served")
    if a.serve:
        serve(dest, a.port)
    else:
        print("  then: add --serve to the command you just ran, or serve %s yourself"
              % dest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
