---
name: video-links
description: >
  Collect the lecture-video links for a course into one file the reader can
  ingest, using the student's own browser. Use this skill when someone says the
  videos do not play, "get my lecture videos", "the video pane is empty", "I
  already have the slides but not the recordings", when they have imported
  lessons or been given a folder of material and the lectures are missing, or
  after a course has been written from slides and needs its recordings wired up.
---

# Collecting the lecture-video links

**What this is for.** The reader plays each lecture in a pane beside the lesson.
It can only do that if it knows the video's id. Slides and transcripts are files
and can be handed over in a folder; **the recordings are not, and a folder of
material contains no video links at all.** So this job exists on its own, and is
often the only thing missing.

**Nothing is downloaded.** A lecture recording is watched where it is published.
This collects addresses, writes them into one small file, and stops.

🔴 **Three hard rules, the same as `download-keats`.**

1. **They log in. You never do.** Never ask for a username or password, never
   type one, never accept one offered in chat. When a login page appears, say so
   and wait for them to sign in and tell you they are through.
2. **Never bypass a check.** Not a CAPTCHA, not a device prompt, not two-factor.
3. **What you collect is theirs.** These addresses work for people enrolled on
   that module. Lessons may be shared; this file is not worth sharing and should
   not be treated as if it were public.

## What you need from them

**The module's main page URL**, and **which course in the reader it is for** (the
course code, which is the folder name and the name in its web address). That is
all.

## Doing it

Use the Claude in Chrome tools. Open a **new tab**; do not take over one they are
working in.

1. **Go to the module page.** If it redirects to a login, tell them and wait.
2. **Find the lecture activities.** On KEATS these are usually `kalvidres`
   links, and the page's own section headings are the week structure. Do not
   assume a shape: read it off the page.

   🔴 **Never conclude a course has no videos from a summary view.** Enumerate
   the activities, every week, and count them. A media gallery, a "recordings"
   tab or a course-level count is a fact about that view and not about the
   course: one course in this project reports **zero media in its gallery while
   serving 38 lectures**, 9 of them real recordings. A session that trusted the
   gallery would have reported "this course has no videos" and been confidently
   wrong about all 38. **Expect a course to mix formats within a single week**,
   so finding a slide package says nothing about the next lecture along.
3. **For each lecture, collect four things**, and only the first is required:

   | field | what it is | how to get it |
   | --- | --- | --- |
   | `doc` | which lesson in the reader this is | the course's own numbering: `W2-T3-P1`, `L07`, `unit3-2` |
   | `entry` | 🔴 **the one that makes it play** | `1_` and eight characters, in the embed on the activity page |
   | `video` | the activity permalink | the address of the activity page itself |
   | `minutes` | runtime | usually printed beside the player |

   🔴 **`entry` is the field that matters.** Without it the reader can only offer
   a button out to KEATS; with it the lecture plays in the pane. Open the
   activity and look at the embedded player's source, or the iframe URL: the id
   is in it. If you genuinely cannot find one, record the permalink anyway and
   say which lectures have no id, rather than inventing one.

4. **Use the course's own numbering for `doc`, and do not renumber it.** The
   student searches for the number printed on the page in front of them. If the
   lessons already exist in the reader, match their ids exactly: get them with
   `python3 server/video_links.py --help` for the format and look at the course
   folder for the ids.

## The file

Write JSON, one object per lecture:

```json
{
  "videos": [
    {"doc": "W1-T1-P1",
     "title":   "What affective disorders are",
     "video":   "https://keats.kcl.ac.uk/mod/kalvidres/view.php?id=1234567",
     "entry":   "1_xxxxxxxx",
     "minutes": 9}
  ]
}
```

Name it something they will recognise a week later: `<CODE>-video-links.json`.

**Show them the list before writing it**, as parts with what each one got. That
is the confirmation step, and it is where the lecture collected under the wrong
number gets caught.

## Getting it into the reader

**Tell them to do this themselves, in the browser**, because it is the part they
will need to repeat:

> Open the course page in the reader and drag the file onto it.

That is it. The page takes a lessons file or a links file and works out which it
has. Do it from here only if they ask:

```
python3 server/video_links.py <CODE>-video-links.json --module <CODE>
```

It is **additive and does not overwrite** a link that is already there, so it is
safe to run twice. `--overwrite` is for when the new file IS the correction.

## Finishing

Say plainly:

1. **How many lectures will play, and how many only open out to KEATS.** The
   difference is the `entry` id, and a student who is told "12 of 14 will play"
   knows exactly what to ask you to look at again.
2. **Anything skipped, and why.** A silent skip is the thing discovered a
   fortnight later.
3. That the videos are streamed from their institution, so they need to be
   signed in and enrolled for them to play.

## When a "video" is not a video

Some lecture links resolve to a narrated HTML slide package (`mod/resource`
on Moodle) rather than a recording. Record the link anyway, with a
`video_kind` that says what it is (for example "KEATS slide package, no video
id"), and never label it Kaltura: the importer keeps a supplied kind and only
claims Kaltura on evidence of an entry id or a kalvidres link. Then offer the
**mirror-packages** skill: it copies the whole package into the course so the
part plays inside Study Hub with no course-site login. That is the difference
between "this one only opens on KEATS" and "this one plays here too".

## Downloading copies (the wizard's opt-in)

The setup wizard offers "Also download copies to this machine", two boxes:

- **Video files**: run `python3 server/fetch_videos.py <course-folder>` after
  the links are imported. It derives each stream URL from the entry ids in
  materials.json and files `videos/<doc>.mp4`; the reader prefers the local
  copy automatically. Recordings play in the reader WITHOUT this; it buys
  offline playback and survival past the course site closing. Say the honest
  size: lectures run to hundreds of megabytes each.
- **Presentations with audio**: the mirror-packages skill, as above. Unlike
  recordings, these cannot play in the reader at all until mirrored.

## Record the sizes while you are there

After the links file is imported, record how big each lecture's media is, so a
later "download everything" can state its total up front (EH's design,
2026-08-23; the setup page shows these totals on the download checkboxes):

```
python3 server/media_sizes.py --module <CODE>
```

One HEAD request per recording, nothing downloaded. A slide package's size is
knowable only after it is mirrored, and the tool says exactly that rather than
guessing; re-run it after any mirror run and it measures the mirrored copies
from disk. Report its two lines as printed, the honest split included.
