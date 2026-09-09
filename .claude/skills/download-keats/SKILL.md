---
name: download-keats
description: >
  Collect a KEATS course's lecture materials onto this machine, using the
  student's own browser session, and write the table that tells the reader where
  each video, deck and transcript lives. Use this skill when someone wants to
  download a module from KEATS, "get my lectures", "grab the slides and
  transcripts", "set up a new course from KEATS", or when they hand over a KEATS
  module URL. Also use it to refresh a course that has gained new material, or to
  rebuild the materials table when the links in the reader have gone stale.
---

# Collecting a course from KEATS

This drives the student's **own** browser, signed in as themselves, and saves
what their own enrolment entitles them to. That is the whole design. It is why
no materials ship in this kit and why nothing here needs an account of ours.

🔴 **Three hard rules.**

1. **They log in. You never do.** Never ask for a username or a password, never
   type one, never accept one offered in chat, and never store one. When a login
   page appears, say so and wait for them to sign in and tell you they are
   through.
2. **Never bypass a check.** Not a CAPTCHA, not a device prompt, not two-factor.
   Ask them to do it and wait.
3. **Nothing collected here is shareable.** These are KCL's files under their
   enrolment. Lessons written from them may be shared (that is what
   `lesson_packs.py` is for). The downloaded folder is not.

## The shape you are reading off the page

Most courses nest like this, and it is worth holding in mind before you look at
anything:

**course → week → topic → part.**

🔴 **The PART is the unit that matters.** It is what a student sits down and
watches, and it is what one lesson corresponds to. Weeks and topics are how the
course files its parts; the part is the thing with a video, a deck and a
transcript.

⚠️ **That is the COMMON shape and not the only one, and this frame must never be
used to force a course into it.** A flat lecture series has no weeks and does not
need any; a seminar course may have no parts. **Read the shape off the page.** The
section "When the course is not shaped like weeks" below is the better half of
this instruction, and it wins wherever they disagree.

## Before touching the browser

Ask for **the module's main KEATS page URL**. That is the only thing you need
from them.

Then ask **where the materials should live**. Default to `materials/<CODE>/`
beside the kit, where `<CODE>` is the module code out of the URL or the page
title. Confirm the folder before writing anything into it.

**This works for any KEATS module, not only the one it was written for.** Do not
assume a week/topic/part shape; read the shape off the page, and see "When the
course is not shaped like weeks" below.

## Driving the browser

Use the Claude in Chrome tools. Open a **new tab**; do not commandeer one they
are working in.

1. **Navigate to the module page.** If it redirects to a login, tell them, and
   wait until they say they are in. Do not poll silently for minutes.
2. 🔴 **ASK THE PACKAGE FOR ITS OWN CONTENTS BEFORE YOU SCRAPE THE SCREEN.**
   A published course package usually knows what it contains, and asking it
   returns structured data with no OCR anywhere in the chain. For a Rise
   package the call is:

   ```js
   await window.__fetchCourse()
   ```

   Read the answer, and scrape only what it does not tell you. **Screen-scraping
   a thing that has an index is how a wrong count gets in**, and every step you
   take through a picture of the page is a step that can be misread.
3. **Read the page structure before downloading anything.** Take the section
   headings and, under each, the activities. Build the inventory first and show
   it to them. A wrong inventory downloaded is a wrong inventory they have to
   sort out by hand.
   ⚠️ **Moodle's grid format renders ONE SECTION TILE AT A TIME**, so what is on
   screen is never the whole course. The section ids are in the course-index
   drawer; take them from there and visit each section, rather than believing the
   tile in front of you.
4. 🔴🔴 **ASSERT THE COUNT, AND RETRY UNTIL IT AGREES.** Whenever you scrape
   anything, you have two independent statements of how many there should be:
   what the page or the package says, and what you actually collected. **Compare
   them and refuse to proceed while they differ.**
   **The worked case, and it is why this is a hard rule.** Rise leaves the
   previous lesson mounted while the next one loads, so asking for one part's
   slides straight after another returns BOTH: 21 images for an 11-slide part.
   **Every file was valid, every count was plausible, and nothing anywhere
   disagreed.** The assertion caught it on the first part scraped and all 34 then
   passed.
   🟢 **The general rule is the valuable part: when two independent sources
   describe the same quantity, make the tool compare them and refuse to
   proceed.**
   🔴🔴 **AND THE HALF A COUNT CANNOT REACH, measured on the same course.** The
   assertion above catches the previous lesson ADDING items: 21 for 11, a wrong
   count. **It is structurally blind to the previous lesson REPLACING one**, where
   the count is right and the words are wrong. **Four slides of that course were
   narrated with a different lecture's words, every one at the same slide index in
   both parts**, and one reached a written lesson before a person reading it
   noticed. **Every count agreed the whole time.**
   🟢 **So after collecting a course, run the narration check:**
   `python3 server/verify_course.py <CODE>` reports the row **"narration unique to
   its part"**. It compares every sentence of eight or more words across parts and
   names any that appear in two, with the slide index. ⚠️ **A course whose
   transcripts are one PDF per part reads `UNCHECKED, not clean`, which is the
   honest answer rather than a pass**: there is no per-slide form to compare, and
   the mechanism needs a per-slide fetch loop to exist at all.
   ⚠️ **The transferable shape: when a stale source can either ADD or REPLACE,
   a count answers only the first question. Compare the CONTENT for the second.**
5. 🔴 **READ THE `<img src>` FILENAME, NEVER THE `alt` TEXT.** Icons and labels
   drift apart: on one real course the `alt` attributes were offset from the
   icons they described, and a pass that read them got the right answer by the
   wrong method. **That is worse than a wrong answer**, because it passes and
   teaches you to trust the method.
6. **Classify each activity** by what it is: a lecture video (Kaltura, usually a
   `kalvidres` link), a slide deck (PDF or PPTX), a transcript, a reading, or
   something else. Keep the ones they want; ask when a whole class of thing is
   ambiguous rather than guessing 40 times.
7. **Download in small batches** and check as you go. A batch that silently
   returned HTML login pages instead of PDFs is the failure to catch early.
   🔴 Downloading a file is an explicit-permission action: say what is coming,
   how many and roughly how big, and get a yes before the first batch.

**Videos: link, never download.** A lecture video is watched through KEATS in
the reader's own pane. Record its permalink and its Kaltura entry id (the `1_`
followed by eight characters, in the embed) and move on. Downloading lecture
video is a large, slow copy of something the institution already streams them.

🔴 **Write the video links as their own file**, in the shape the reader ingests,
as well as putting them in the table below:

```json
{"videos": [{"doc": "W1-T1-P1", "title": "...", "video": "...",
             "entry": "1_xxxxxxxx", "minutes": 9}]}
```

Name it `<CODE>-video-links.json`. The **video-links** skill beside this one is
the detail, including where the entry id hides and why it is the field that
decides whether a lecture plays or merely opens out. A person who already has
slides and transcripts needs that file and nothing else from this skill, which is
why it is written down separately.

## Sorting what comes down

One folder per course. Inside it, one file per part, named so the reader can
pair them without a database, **with the file's original name kept in
parentheses** (EH's design, 2026-08-23: downloads arrive with names that match
nothing, so the standard form leads and the original stays visible):

```
materials/<CODE>/
  W2-T3-P1 - Slides (7XYZ_W2_T3_P1_Accessible_Slides).pdf
  W2-T3-P1 - Transcript (7XYZ-W2-T3-P1_Transcript).pdf
```

The form is `<DOC> - <Kind> (<original name without extension>).pdf`, where
Kind is `Slides` or `Transcript`.

🔴 **That form is not a tidiness convention, it is the wiring.** The reader finds
a downloaded file by looking in the materials folder for a name beginning
`<DOC> - Slides` or `<DOC> - Transcript`. Get the form wrong and the file is on
disk and invisible. Nothing else has to be written down for it to be found.

**When the download is finished, tell them the last step**, because it is a
choice only they can make: on the course's setup page, under *Where the slides
and transcripts come from*, pick **From a folder on this machine**. Until they
do, the pane keeps linking out to the course site, which is the other honest
answer and the default. The page then says how many parts it found a file for,
which is where a wrong folder or a mis-named download shows up. Drop the parenthetical only when the
original name IS already the standard form, so nothing reads twice. Write the
names you actually used into `materials.json`, which is what everything else
reads; the parenthetical is for the person browsing the folder, including any
later matching against the course site (and the future Zotero linking, which
joins on names).

`<DOC>` is that part's id: whatever the course's own numbering gives you, made
safe (letters, digits and single dashes). `W2-T3-P1` for a weeks-and-topics
course, `L07` for a lecture series, `unit3-2` for units. **Use the course's real
numbering. Do not renumber it to look like somebody else's course**, because the
student will search for the number that is on the page in front of them.

## The table the reader reads

Write `courses/<CODE>/materials.json`. This is R35's table of every video, and it
is also what the reader's Materials pane runs on:

```json
{
  "built": "how this was made, in a sentence",
  "order": ["W2-T3-P1", "W2-T3-P2"],
  "parts": 2,
  "docs": {
    "W2-T3-P1": {
      "title":      "The title as the course gives it",
      "video":      "https://keats.kcl.ac.uk/mod/kalvidres/view.php?id=…",
      "video_kind": "Activity permalink (Kaltura)",
      "entry":      "the Kaltura entry id, of the form 1_ then eight characters",
      "minutes":    30,
      "slides":     "…",
      "transcript": "…",
      "href":       "W2-T3-P1-affective-disorders-and-mood.html"
    }
  }
}
```

- `order` is the order the student meets them in, and it drives the reader's
  back and forward buttons. Get it right; it is the field they feel.
- `slides` and `transcript` are the **course site's** addresses. Put the URL
  here even when you have just downloaded the file, and do NOT write a local
  path: the reader resolves local files by NAME out of the materials folder
  (see below), so a path here is a second answer that can disagree with the
  first.
- `href` is the lesson file, once one exists. Leave it out until then.
- Everything is optional except `title`. **A part with no video is normal.** Say
  nothing rather than inventing a link, because the reader tells the student
  plainly when a thing is not there and a wrong link is worse than a blank.

**Show them the table before you write it**, as a list of parts with what each
one got. That is the confirmation step, and it is where they catch the lecture
that came down under the wrong number.

## When the course publishes no lecture files at all

🔴 **Some courses hand you a website and nothing else.** No slide PDFs, no
transcript files: just a package you page through in a browser, with a single
combined handout somewhere. **When that happens, scraping stops being a tidy-up
at the end and becomes a PREREQUISITE for writing anything at all.** Find that
out before you promise anybody a download, because the job is a different job.

**What to build instead, and this is exactly what was done for a real course of
this shape:**

1. 🔴 **A file named `Transcript` holds the words that were SPOKEN and nothing
   else.** Extract the narration from the package and render THAT. **Do not save
   the combined handout under that name**, however convenient it is: a handout
   interleaves each slide with its narration, so a file labelled transcript then
   contains the slide text too. **Measured on a real course: 2,557 words in the
   file named `Transcript` against 1,486 words of actual narration, so about 42%
   of it was slide text**, and the whole-course consolidation inherited it, which
   is how a reader ends up with "transcripts" that are the slides again.

   ```
   W2-T1-P1 - Transcript (built from course narration).pdf
   ```

2. 🟢 **Keep the handout, under its own name.** It is a real document and the
   only one that shows a slide beside what was said about it, so it is worth
   having. It is simply not a transcript.

   ```
   W2-T1-P1 - Handout (<original filename>).pdf
   ```

   ⚠️ **And the handout is NOT "the only prose the course published"**, which is
   what this section used to say. The narration is prose, it is extracted in the
   same run, and it is the better source for writing a lesson: it is what was
   said, rather than what was said plus what was on the slide.

3. **Build a per-part `Slides` PDF from the package's slide images**, one slide
   per page, in order.
4. 🔴 **Put the fact that you built it in the brackets**, in the same
   parenthetical the standard naming already uses:

   ```
   W2-T1-P1 - Slides (built from course slide images).pdf
   ```

   ⚠️ **This matters more than it looks.** The parenthetical is normally the
   original filename, so a reader who sees one assumes the course published that
   file. **Saying where it came from is the difference between a document and a
   document somebody will later mistake for evidence.**

⚠️ The image-to-PDF step is not a tool in this kit yet, deliberately: it has been
needed once, and one instance is not a pattern. Build it for the course in front
of you and say that you did.

## When the course is not shaped like weeks

Some modules are a flat list of lectures, some are units, some are seminars. The
reader has not required `W#-T#-P#` since 2026-08-16, so use what the course uses.
The only constraints are: ids are unique inside the course, they are made of
letters, digits and single dashes, and `order` lists them in teaching order.

## Fix what came down, before anyone reads it

Slides and scanned handouts routinely arrive with a few pages rotated and
with OCR bad enough that search misses real words. Fixing that now is cheap;
finding it mid-revision is not. Run every downloaded PDF through:

```
python3 server/pdf_fix.py <the folder of PDFs>
```

It measures each page's readability in all four orientations and rotates only
on a decisive win, then re-runs OCR so the text layer matches the page
(existing OCR is REDONE, because badly-OCR'd slides are the common case;
born-digital text is left alone). Originals are kept in `backups/` beside the
files. `--check` first if you want to see what it would do. Report what it
rotated and what it re-read, from its own output.

## Consolidated PDFs, which are standard rather than an extra

🟢 **Build these by default.** They are turned OFF rather than on, and the
reason is the purpose of the whole onboarding step: a student who onboards a
course should end up with the readable, printable documents without having to
know that such a thing exists to ask for. A week's slides as one document
instead of nine tabs is not an advanced option.

Build them AFTER pdf-fix, never before, so they inherit the clean orientation
and OCR:

```
python3 server/consolidate_pdfs.py <the folder of PDFs> --module <CODE>
```

It writes one PDF per week and one for the whole course, for the slide decks,
the transcripts and the handouts, into `courses/<CODE>/consolidated/`, and
verifies every output's page count against the sum of its parts. 🟢 **Handouts
are a kind because a course of the shape above publishes one**, and it is a
document worth having whole; `--kind transcripts --kind handouts` limits a run
when only some of the inputs have changed. Files whose names
carry no `W<n>` week join only the whole-course PDF, and it says so. Report
its output as printed, failures included.

## The reading list, in the same visit

"Nothing yet, and my course is online" means everything: if the module carries
a reading list (KEATS courses usually keep one under "Module Reading Lists" or
"Core reading"), do not leave it behind for a second session. Follow the
**download-readings** skill while you are signed in: collect the list week by
week, download the PDFs the enrolment serves, run them through pdf-fix, and
offer the core-readings summarise step. If the module genuinely has no reading
list, say so in the finishing count rather than silently skipping the check.

## What this skill does not do

Not in the kit, deliberately, and worth saying when someone asks:

- **Building a PDF from a package's slide images**, for a course that publishes
  no slide files at all. The recipe is above; the tool is not written, because it
  has been needed once and one instance is not a pattern.

(Two things used to be on this list and are not any more, which is why the list
is worth re-reading rather than trusting. Rotation and OCR became
`server/pdf_fix.py` on 2026-08-22, and **consolidated PDFs became
`server/consolidate_pdfs.py`, are in the kit, and are now built by default**:
this list said the opposite of the section two above it until 2026-09-08. It
needs `ocrmypdf` and `tesseract`; on a Mac, `brew install ocrmypdf` brings
both, and the script says plainly when they are missing rather than failing
strangely.)

## Finishing

1. Show a count: parts found, decks saved, transcripts saved, videos linked,
   anything skipped and why. **A silent skip is the thing that gets discovered a
   fortnight later**, so name every one.
2. Tell them the folder is theirs alone and must not be passed on.
3. **Say how many lectures will play and how many only open out**, which is the
   `entry` id and nothing else. "38 of 41 will play" is a fact they can act on.
4. 🟢 **Report how many citations each PART carries**, counted from the text you
   downloaded, as a per-part line or a range with the outliers named.
   ⚠️ **Per part, not per course and not per week**, and that is the whole point:
   the writer of one lesson needs to know whether the part in front of them cites
   three papers or thirty. **Without it, whoever writes first generalises from
   their own week and every later writer inherits the guess.** A part with none is
   worth naming out loud, because it usually means the citations are in an image
   the text layer never saw.
4. Point at **write-lesson** as the next step, and say that a course with
   materials but no lessons still shows up on the home page, with nothing to
   read yet.
