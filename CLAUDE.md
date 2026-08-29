# CLAUDE.md: the study kit

You are working in someone's study kit. They are a student, probably not a
programmer, and they opened this folder because they want to read their lectures
properly. **Do the work for them.** Do not hand out commands to paste, do not
explain the architecture unless they ask, and do not make them choose between
options they have no way to judge.

## What this is

A local reader for lecture material. A small Python server runs on their own
machine and serves lessons to their own browser, with highlighting, notes,
flashcards, bookmarks, a definition lookup and a chat pane beside the text.

**Nothing leaves the machine** except the definition lookups (public reference
APIs) and, if they have it configured, the Claude CLI for the Explain feature.
Their highlights and notes are plain files on their disk, next to the lessons.

## The three skills

- **setup**: config, start the server, open it, keep it running. First thing on
  a new machine, and the thing to reach for when the page will not load.
- **download-keats**: collect a course's material from KEATS **in their own
  browser, signed in as themselves**, and write the table the reader runs on.
- **write-lesson**: turn a deck and a transcript into a lesson in the house
  standard, then prove it with the verifier.

## How it is laid out

```
courses/                  one folder per course, EMPTY in a fresh kit
  settings.json           preferences shared by every course
  <CODE>/                 a course
    settings.json         this course's identity, and any overrides
    <DOC>-<slug>.html     a lesson
    marks.<DOC>.json      their highlights and notes, per lesson
    materials.json        where each part's video, deck and transcript live
server/                   the reader and its tools
.claude/skills/           the three skills above
```

`~/.kcl-study/config.json` holds machine settings: which folder, which port,
whether vault publishing is on. It is theirs, mode 0600.

## Invariants. Break these and you lose their work

🔴 **Their marks are the irreplaceable thing here.** Lessons can be rewritten and
materials can be downloaded again. Six weeks of highlights cannot. Everything
below is in service of that.

- **Back up before any destructive change.** Copy to
  `<name>.YYYYMMDD-HHMMSS.bak` beside the original first. Leave the backup in
  place afterwards; it is free.
- **Highlights anchor to block position and character offsets.** Rewriting a
  paragraph moves every mark inside it. Edit the smallest region that does the
  job; never slice a lesson file by index; assert every anchor before writing.
- **`store_prefix` must never change for a course that already has marks.** It
  is the browser-storage key prefix. Change it and their highlights are still on
  disk but invisible in the reader.
- **Never print the config file.** It contains a token. Read it in Python and
  print key names, or the single value you need. A token in a transcript has to
  be replaced.
- **Never edit `server/reader/shell.html` or `server/local-layer.html` casually.
  Saving them is a deploy**, live on the next page load with no push step.
  `python3 server/verify_notes.py` is the only gate; run it after any edit
  there.
- **The server binds to this machine only.** `0.0.0.0` is refused outright, and
  for good reason: it writes files and runs a subprocess.

## Restarting the server without lying about it

🔴 **A restart can fail silently.** Kill it and start again immediately, and the
new process hits "Address already in use" and dies, while the old one keeps
answering `/healthz` perfectly. So: kill by pid, **wait for the port to go
quiet**, then start, and prove the new code is live by checking something only
the new version has. `/healthz` proves a server is up, never which one.

Never pattern-kill. `pkill -f` matching a path in this tree has killed the wrong
process before.

## Where the material may go

- **Lessons may be shared.** That is what `server/lesson_packs.py` is for: one
  file per lesson, carrying the text and the links, and **never anyone's marks**.
- 🔴 **Downloaded course materials may not be shared.** They belong to the
  institution, downloaded under one student's enrolment. Each person collects
  their own with their own login.
- A shared lesson's links keep working for anyone enrolled on that module.
  Someone who is not enrolled still gets the whole lesson; the reader tells them
  plainly which links they cannot open, rather than breaking.

## House style, for anything written for them

- **No em dashes.** Commas, parentheses, colons, or two sentences.
- British spelling in study material, following the source.
- Say what you actually ran. If a check was skipped, say it was skipped.
