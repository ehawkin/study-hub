---
name: write-lesson
description: >
  Write a study lesson from a lecture's slides and transcript, in the house
  standard, and prove it before it ships. Use this skill when someone wants a
  lesson, note or summary made from lecture material, says "write this one up",
  "turn these slides into a lesson", "make a note from this transcript", or
  hands over a deck and a transcript and asks what to do with them. Also use it
  when editing an existing lesson, since the same standard and the same gate
  apply.
---

# Writing a lesson

**`NOTE-SPEC.md`, beside this file, is the standard.** Read it before writing
anything. It is long because it is the accumulated answer to real defects, and
skipping it produces work that has to be redone. This file is the procedure; the
spec is the substance.

## The four rules people get wrong

Everything else in the spec matters, but these four are the ones a fresh writer
breaks:

1. **Transmit, never point.** The lesson must be readable with nothing else
   open. Never gesture at something the reader cannot see: not "as shown in the
   diagram", not "the study discussed earlier", not a figure that was never
   drawn. If it matters, put it in.
2. **Stand alone.** No lecturer names, no "the slides said", no reference to
   decks, transcripts or lectures at all. Emphasis is kept; attribution is
   dropped. The lesson is a piece of writing, not a report on a lecture.
3. **Every citation is a checked, clickable DOI.** Never guess an identifier.
   Resolve it, confirm the title and authors match, and if it will not resolve,
   say so in the text rather than shipping a link that dies. This includes the
   citations printed small in the corner of a slide, which is exactly where the
   interesting papers hide.
4. 🔴 **Every clause about a study traces to a sentence you actually read.** If
   the source says *analysed*, write analysed, not *completed*. If the design is
   not stated in what you read, do not name it. **This is the one the gate cannot
   catch**: it checks identifiers, not whether the sentence wrapped around them is
   true, so a made-up study design ships with a correct citation underneath it and
   reads perfectly. The sentences most worth re-checking are the confident ones,
   because confidence is what stops anybody checking.

## Start the file with the scaffolder, not by hand

```
python3 server/new_lesson.py --module <CODE> --doc <DOC> --title "The title" \
    --week 3 --topic "The topic" --part 1
```

It writes a structurally correct empty lesson: the content marker, the title,
a valid `lesson-meta` block, the course's stylesheet, and an empty `.wrap`
waiting for your prose. It refuses a doc id that already exists, points
`materials.json` at the new file when the course has a row for it, and tells you
what to do next.

🔴 **The stylesheet comes from the course the lesson is joining**, so a course
that has evolved its own look stays consistent with itself. A course with no
lessons yet gets the house template. Add rules your lesson needs to its own
`<style>`; do not edit other lessons to match.

Only reach past it if you are editing a lesson that already exists.

## The file you are producing

Since the content/reader split, **a lesson file is the lesson and nothing
else**. The reader (highlighting, notes, cards, the panel, the chat) is served
around it by the server. Do not paste an engine into it; do not copy the
structure of some older stamped file you find in a backup.

The shape the scaffolder gives you, and the shape to keep:

```html
<!-- study-lesson:v1 -->
<title>Cognitive Bias, and the Case for Calling It a Cause: W4 T1 P1</title>
<script type="application/json" id="lesson-meta">
{
  "doc": "W4-T1-P1",
  "title": "W4 T1 P1: Cognitive Bias, and the Case for Calling It a Cause",
  "week": "04",
  "topicNo": "1",
  "weekTitle": "Cognition and affective disorders",
  "topic": "Cognitive bias in affective disorders",
  "part": "1"
}
</script>

<style>
  /* the lesson's own styles */
</style>

<div class="wrap">
  <!-- the lesson -->
</div>
```

- **`doc` is required** and must be unique in the course. Letters, digits and
  single dashes.
- `week`, `topicNo`, `weekTitle`, `topic`, `part` drive the Notebook's grouping
  by week and by topic, and the vault note's naming where publishing is on. A
  course that is not shaped like weeks can leave them out; the grouping falls
  back to the lesson.
- **Save it in the course folder**, `courses/<CODE>/<DOC>-<slug>.html`.

🔴 **Never edit a lesson by slicing the file on indexes**, and never
search-and-replace across it blind. Highlights anchor to a block's position and
to character offsets inside it, so **rewriting a paragraph moves every mark in
it**. Edit the smallest region that does the job, assert every anchor you are
matching before you write, and back the file up first
(`backups/<name>.YYYYMMDD-HHMMSS.bak` in a `backups/` folder beside it, which
is where every dated backup in a course lives).

## Working from a folder of files

Somebody hands you a folder and says "make a lesson from these". Before writing
anything:

1. **List what is actually there** and say what you found, by part. A folder is
   usually one part's deck and transcript, but it can be a whole week, and
   turning a week into one lesson is the wrong shape.
2. **Read the PDFs.** Your file-reading tool renders PDF pages, so read them
   rather than shelling out to a text extractor: the extractor loses the layout,
   and the layout is where the slide-corner citations live. Read every page,
   including the ones that look like title cards.
3. **Ask which part this is** if the filenames do not say, and **do not trust a
   deck's footers**: numbering in lecture material is frequently wrong. Confirm
   against the file name, the title slide and the transcript before choosing a
   doc id, because the doc id becomes part of every sidecar's filename and
   changing it later orphans the highlights.
4. **Say what you could not read.** A scanned deck with no text layer, a
   transcript that stops mid-sentence, a file that failed to open: name it now.
   The lesson is written from what you could read, and section A of the standard
   says which source to lean on when the other is damaged.

## Working from the material

1. **Read the transcript in full and the deck in full.** Both. The transcript
   carries the argument and the asides; the deck carries the structure, the
   figures and the references. Neither alone is the lecture.

   🔴 **OCR for the prose, the rendered page for the numbers.** Text extraction
   from this kind of material silently drops whole sentences, and OCR misreads
   digits (`1848` for `1348`, `46 minutes` for `45`, both real). Neither tool is
   trusted for the other's job: read the rendered page for anything numeric.

2. **Fetch the paper for any number or direction of effect you are about to
   write down.** The teaching material is a claim *about* the paper, not the
   paper. Decks routinely print a point estimate without its interval, take an
   effect size from the weakest subset, or keep the significant half of a result
   and drop the rest. In the worst case they state the **opposite** of the paper
   printed on the same slide. **A lesson that inverts a finding is more
   convincing than one that fumbles it**, so check the direction against the
   abstract before writing it in your own words.
3. **Plan the shape before writing.** What is the claim, what is the evidence,
   where does it stop being settled. A lesson that follows the slide order
   slavishly reproduces a deck's compromises rather than teaching anything.
4. **Write it as prose that teaches**, in British spelling, following the
   source's own terms. Keep emphasis the material gives something; drop the
   scaffolding it needed to be delivered live.
5. **Diagrams**: hand-authored inline SVG, `currentColor` so both themes work,
   labels that do not collide with the arrows, and a caption saying what it
   shows. 🔴 **Screenshot every figure and look at it. This is a required step,
   not a nicety** (EH made it official 2026-08-23). About one figure in four has
   a defect that is invisible in the markup and obvious in the picture: a legend
   clipped mid-word at the viewBox edge, a label sitting on top of a data row, a
   dashed box too narrow for its words. The same goes for any page a tool
   rewrote: **a tool's own report of success is not evidence that it
   succeeded.**
6. **No em dashes.** Anywhere. Commas, parentheses, colons or two sentences.

## Four things that happen when the source is wrong (EH's design, 2026-08-23)

The source being wrong is normal, not exceptional: one course produced twenty-one
recorded contradictions and twelve corrected or retracted papers. **None of these
four replaces the prose.** They sit alongside it.

1. **A contradiction gets a call-out quoting both sides.** Where the teaching
   material disagrees with the paper it cites, a small box says what the material
   says and what the paper says, in their own terms. **It does not adjudicate**;
   the reader gets to see the disagreement rather than being told who won. This
   is the shape for an inverted direction of effect, a construct renamed to its
   opposite, or a figure credited to a work that does not contain it.
2. **Stripped statistics come back in a collapsed section.** The body keeps its
   readable sentence; a folded "the statistics and the detail" section beneath it
   carries the intervals, the sample sizes and the non-significant arms the source
   dropped. It folds because the reader revising does not want the interval every
   time and the reader checking a claim always does.

   The markup is `<details class="deets"><summary>The study, in detail</summary>
   …</details>`. 🔴 **Fold detail that SUPPORTS an argument, never detail that IS
   one.** Arms, sample sizes, instruments and durations fold. A table whose point
   is to show what the source left out does not: folding it re-hides the thing.
   The test is whether the reader loses the point by leaving it shut. Wrapping a
   table cannot move a highlight's anchor, because table cells and `<summary>` are
   not blocks, but check rather than trust it.
3. **A corrected or retracted paper keeps its place and gains a marker.** The
   paper stays where it belongs, prose and all. A small caution sign beside the
   citation links to that paper's entry in the reference section, and **that entry
   says it was corrected or retracted and links the notice itself.** Every one,
   not only the ones where the correction changes the claim: the noise is handled
   by keeping the marker small inline and the detail at the end.
4. **Every defect is also filed in the course's mistakes ledger.** Each course
   has a "Mistakes found in this course" page, linked from the end of its hub,
   collecting every source defect the build noticed: the wrong years, the
   misattributed authors, the inverted results, the slides missing from an
   export. File entries as you find them, in the same sitting as the lesson:

   ```
   python3 server/mistakes.py my-mistakes.json --module <CODE>
   ```

   The file is a JSON list of entries. `label` (required, one line), `kind`
   (short and lowercase: "wrong year", "inverted result", "misspelled author",
   "underspecified citation", "self-contradiction", "missing slide", or
   whatever names the defect), `doc` (the lesson it belongs to, omitted for a
   course-level defect), and the two sides as **`source_says` and
   `paper_says`**, separate fields so the page renders both sides the same way
   every time and nobody adjudicates. `text` for prose that fits neither side,
   `refs` for checked DOIs. `--module` is required, never defaulted: this file
   is exactly the kind of thing two courses would both have open.

## The gate, which is not optional

```
python3 server/verify_notes.py --notes courses/<CODE>
```

It checks block numbering agrees between the page's rule and the server's, that
every existing highlight still resolves, that links and DOIs are real, house
style, and that the JavaScript and CSS parse. **Run it after every lesson and
after every edit to one.**

🔴 **A run that finishes suspiciously fast has not done the work.** It is
checking DOIs over the network; a fast pass usually means it matched no files.
Read the counts it prints and make sure they are the number of lessons you
expect. A gate that silently checked nothing is worse than no gate, because
somebody believed it.

If it fails, fix the lesson. Do not adjust the verifier to agree with the
lesson.

🔴 **But a gate must fail for the RIGHT reason, and a wrong check is fixed, not
suppressed.** When it fires on something you are confident is correct, the first
hypothesis is that the check is wrong. Adding an identifier to the skip list is
allowed only once you can say what the check should have done instead. This is
not pedantry: a real defect meant that **any lesson citing a retraction notice
failed the gate for the wrong reason**, and the tempting fix would have silenced
the retraction at exactly the moment it mattered.

**Check for corrections against Crossref AND PubMed.** Neither is a superset of
the other, proven both ways in one course: one paper's corrigendum appears only
in Crossref's `updated-by`, another's only in PubMed's "Erratum in" line.

## The glossary, in the same sitting

Every lesson carries a "terms to define cold" section as `<dt>`/`<dd>` pairs,
and the reader's lookup popover answers from the course's `glossary.json`. A
lesson whose terms never reach the glossary looks up as nothing, so the harvest
is part of writing the lesson, not a chore for later:

```
python3 server/glossary_extend.py 'W4-T1-P*.html' --module <CODE>          # dry run: see what it would add
python3 server/glossary_extend.py 'W4-T1-P*.html' --module <CODE> --write
```

It merges rather than overwrites, aliases abbreviations so both halves of
"Short-chain fatty acids (SCFAs)" resolve, and a second run is a no-op. Run it
with the lesson files you just wrote, read the count it prints, and say it.

## Finishing

- Reload the page in the reader and look at it. Read the first screen, open a
  diagram, and check the panel opens.
- If the course has a `materials.json`, add this part's `href` so the Materials
  pane can pair the lesson with its deck and video.
- Say what you actually ran and what it said. If you skipped the verifier
  because the network was down, say that too, rather than reporting a pass that
  did not happen.
