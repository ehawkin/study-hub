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
2. **Read the page structure before downloading anything.** Take the section
   headings and, under each, the activities. Build the inventory first and show
   it to them. A wrong inventory downloaded is a wrong inventory they have to
   sort out by hand.
3. **Classify each activity** by what it is: a lecture video (Kaltura, usually a
   `kalvidres` link), a slide deck (PDF or PPTX), a transcript, a reading, or
   something else. Keep the ones they want; ask when a whole class of thing is
   ambiguous rather than guessing 40 times.
4. **Download in small batches** and check as you go. A batch that silently
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

## Consolidated PDFs, if they asked for them

If the job includes the consolidated-PDFs option (the wizard's checkbox, or
they ask), build them AFTER pdf-fix, never before, so they inherit the clean
orientation and OCR:

```
python3 server/consolidate_pdfs.py <the folder of PDFs> --module <CODE>
```

It writes one PDF per week and one for the whole course, for the slide decks
and for the transcripts, into `courses/<CODE>/consolidated/`, and verifies
every output's page count against the sum of its parts. Files whose names
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

- **Consolidated PDFs** per week or per course.

If they ask, say it is a known want that is not built, and offer the manual
route rather than half-building it now. (Rotation and OCR used to be on this
list; since 2026-08-22 they are `server/pdf_fix.py`, the section above. It
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
4. Point at **write-lesson** as the next step, and say that a course with
   materials but no lessons still shows up on the home page, with nothing to
   read yet.
