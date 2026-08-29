---
name: mirror-packages
description: >
  Mirror a narrated HTML slide package (an iSpring-style export a course site
  serves as a "video") into the course's packages/ folder, so it plays inside
  Study Hub with no course-site login. Use when video-links finds lecture
  links that are slide packages rather than recordings, or when someone says
  "these videos won't embed" or "save the narrated slides locally".
---

# Mirroring a narrated slide package

Some course "videos" are not videos: they are HTML slide packages (iSpring
and similar) that run on the course site itself, slides timed against
per-slide narration MP3s. The course site will not let another origin frame
its pages, so the reader cannot embed them remotely. But the package is
nothing more than a folder of static files, so a copy served by Study Hub
itself embeds fine, plays with no login, and works offline.

The same three rules as every download skill, absolute:

1. **They sign in themselves.** Never type, store or read out a password, and
   never read credentials or cookies out of the browser.
2. **Never bypass an access check.** Mirror only what their own enrolment
   already serves to their signed-in browser.
3. **A mirrored package is theirs to study, never to redistribute.** It lands
   in their course folder, is excluded from git and from lesson packs, and
   stays on their machine.

## What a package looks like

- The lecture link is `mod/resource/view.php?id=N` (Moodle). It either
  redirects straight to `pluginfile.php/.../index.html`, or stays on view.php
  with the real link in `.resourceworkaround a` / `.resourcecontent a`.
  🔴 Parse the page for that link; never regex the raw HTML for the first
  pluginfile URL, because the page carries other, stale ones.
- The export is `index.html` plus a flat `data/` folder of numbered files:
  `slideN.js/css`, `imgN.png`, `fntN.woff`, `soundN.mp3`, `player.js`.
  Format drift between exports is real (one course's packages carry files
  another's lack), so enumerate per package, never from a fixed list.

## Before the bulk run: one browser permission

The crawl hands each package to disk as one `.tar` download. Chrome allows
one automatic download per site and then silently blocks the rest until the
person allows "automatic multiple downloads" for the course site (the
blocked-download icon at the right of the address bar, or Site settings →
Automatic downloads). **Ask them to click Allow once before mirroring more
than one package**, and 🔴 verify every tar actually arrived on disk before
moving on: a blocked download looks exactly like a successful one from page
JavaScript.

## The crawl, in page context

Run this on any page of the course site (substitute DOC and VIEW), then poll
`window.__mirror` until `phase` is `done` or `failed`:

1. Resolve VIEW to the pluginfile base as above.
2. Breadth-first fetch from `index.html` with `credentials: 'same-origin'`:
   scan every text file (html/js/css/svg/xml/json) for relative asset
   references and queue each candidate both as written and relative to the
   referencing file's folder; skip anything absolute or containing `..`;
   404s on candidates are normal misses.
3. 🔴 `index.html` failing to fetch is the mirror failing, not a miss.
4. Probe numbered families the format builds at runtime (`data/soundN.mp3`,
   `imgN.png/jpg`, `slideN.js/css`, `fntN.woff/woff2`, `videoN.mp4`), upward
   until three consecutive misses, cancelling the body of each miss.
5. Pack everything into one ustar tar in memory, then download it as
   `<DOC>.tar`.

The reference implementation lives in the Study Hub repo at
`server/mirror-package.js`; use it rather than rewriting the crawl.

## Filing and verifying

- Untar into `courses/<CODE>/packages/<DOC>/` (so `index.html` sits directly
  in that folder). The reader needs no other wiring: the server notices the
  folder at read time and the part's Videos tab switches from "cannot be
  played here" to the local player. materials.json is not edited.
- Verify like you mean it: `tar -tf` count matches the crawl's file count;
  `index.html` exists; a narrated deck has at least one `soundN.mp3`; then
  load the lesson and confirm the Videos tab actually plays. Report the
  per-package file counts and any package that failed, never "all mirrored"
  without the counts.
- Delete the tars from Downloads after untarring.

## What this skill does not do

- It does not convert packages to video files. There is no video inside to
  extract; a screen-capture conversion loses text sharpness and per-slide
  seeking, and it is not what the reader needs.
- It does not touch real recordings (Kaltura and similar): those embed from
  their own links and need no mirror.

## Afterwards: record the sizes

A package's size is unknowable until it is mirrored; now it is knowable. Run

```
python3 server/media_sizes.py --module <CODE>
```

so materials.json records what each package weighs on disk and the setup
page's download checkbox can state the course's total.
