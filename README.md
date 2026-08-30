# Study Hub

**A better way to read your lectures.** This turns lecture slides and transcripts
into proper lessons you can read, highlight, question and revise from, in your
browser, on your own machine.

Highlight anything in five colours. Write notes on a highlight or on nothing in
particular. Turn a term into a flashcard and get a definition looked up for you.
Bookmark a paragraph, a figure or a chat. Ask questions about the passage you
just selected, with the lecture playing in the pane beside it.

**It all stays on your computer.** Your highlights and notes are files on your
own disk, next to your lessons. Nothing is uploaded, there is no account, and
there is nothing to pay for.

---

## Before you start

**You do not need to know what Python is**, and you do not need to install
anything by hand. Read this once and you can forget it.

**The one-double-click way.** In this folder there is a file called
**`Start Study Hub.command`**. Double-click it. A Terminal window opens, prints a
line or two, and your browser opens on the reader. You can close the Terminal
window afterwards; Study Hub keeps running. Double-click it again any time; if it
is already running it just opens the page.

🔴 **The first time only, macOS will not let you open it**, because the file came
from the internet. It says something like *"cannot be opened because it is from an
unidentified developer"*. That is normal and it is not a problem with the file.
**Right-click** (or Control-click) `Start Study Hub.command`, choose **Open**, and
then click **Open** in the dialog. macOS remembers, and every later double-click
works normally.

🔴 **You may also see one grey Apple dialog** saying *"The `python3` command
requires the command line developer tools"*. Click **Install** and wait; it is
Apple's own installer, it takes a few minutes, and it happens once ever. The
script tells you about this before it appears, and afterwards you just
double-click `Start Study Hub` again.

**Which computers this works on.** This kit is exercised on **macOS** only. It is
plain Python and plain HTML with nothing Mac-specific in the reader itself, so it
very likely works elsewhere, but nobody has tested it on Windows or Linux and the
`.command` file is a Mac thing. If you are on Windows, the commands under *"If you
would rather not use Claude for it"* at the bottom are your route, and please tell
whoever gave you this how it went.

---

## Getting started

You need two things:

1. **Claude Desktop**, from <https://claude.ai/download>, and an account.
2. **This folder**, somewhere you can find it again. Your Documents folder is
   fine.

Then:

1. Open Claude.
2. Start a **Code** session and point it at this folder, using the folder
   picker.
3. Say: **"set me up"**.

That is all of it. It will write its settings, start Study Hub, open it in your
browser, and ask what you want to read. It will offer to keep Study Hub running
after you restart your machine; say yes and you never think about it again.

**You will not need a terminal**, before or after.

---

## Where your lessons come from

Three routes, and you can mix them.

**Somebody sent you lessons.** A lesson arrives as a single file. **Open the
course page and drag the file onto it**, or use the button there to choose it.
You do not have to find a folder or rename anything, and there is nothing to log
into. The lesson keeps its links to the lecture video and the slides, so those
still work if you are enrolled on that module.

**You have the course on KEATS.** Say "download my course from KEATS" and give
it the module's address. **You sign in yourself**, in your own browser, and it
collects what your enrolment gives you. Your password is never asked for, seen or
stored.

**You already have the files.** A folder of slides and transcripts is enough.

**And the lecture videos, whichever route you took.** They are streamed by your
institution, so a folder of slides never contains them. Collecting their
addresses is a job of its own, and it is the commonest reason a course reads
fine but the video pane is empty.

🔴 **You do not have to work out how to ask for any of this.** Every course page
has **Show me how, step by step**: it walks through installing Claude, tells you
exactly which folder to point it at with the path ready to copy, and gives you
the words to paste, one button each. It is written for somebody who has not done
this before.

---

## Once it is running

Open the bookmark. You get a page listing your courses, each showing where you
left off. Pick one and read.

Everything is in reach from the page itself: highlight colours and what they
mean, the notes and cards panel, the lecture video, the slides, the transcript,
and settings. **Settings** is a link at the top of every course page, and it is
where you say where your courses are kept and whether your highlights are copied
into an Obsidian vault. **The Notebook** collects every highlight, note, card and bookmark
across a whole course, grouped by lesson, by week, by topic or by kind, with a
search across the lot.

If anything stops working, ask. "The reader won't open" is enough to go on.

---

## Things worth knowing

- **Your marks are the precious part.** They live beside your lessons as plain
  files and are copied into a dated snapshot the first time you highlight
  anything each day, kept for a fortnight.
- **Lessons can be shared. Downloaded course materials cannot.** The slides and
  transcripts belong to your institution and come down under your own enrolment.
  A lesson you pass to a coursemate carries the writing and the links, and never
  your highlights or notes.
- **It works on your phone**, over your own private network, if you set that up.
  Ask.
- **It is not only for one module.** Add as many courses as you like; they each
  get their own folder, their own settings and their own highlights.

---

## On your phone

The reader works well on a phone, and your highlights made there land in the
same files. It needs two things: **your computer stays on** (it is the server),
and both devices share a private network. The clean way is
[Tailscale](https://tailscale.com), whose free plan is plenty:

1. Install Tailscale on the computer and on the phone, signed into the same
   (free) account.
2. Ask your Claude session to "put Study Hub on my tailnet". It moves the
   server onto your private Tailscale address and tells you the address to
   open on the phone.
3. The phone's browser asks once for the server's token; your session will
   read it out of the config for you to type in. After that, it is a bookmark.

This is the one advanced setup in the whole kit. If you never do it, nothing
else changes.

## Upgrading

When a newer kit is out (the page tells you, quietly, if an update source is
configured), upgrading is a folder swap: **replace the kit's `server` and
`.claude` folders** with the new ones, and leave everything else. Your
lessons, highlights, notes and settings live in `courses/` and in
`~/.kcl-study/`, and an upgrade never touches either.

## If you would rather not use Claude for it

Everything here is plain Python and plain HTML, and you can run it yourself:

```
python3 server/study_server.py --init
python3 server/study_server.py
```

Then open <http://127.0.0.1:8795/>. The skills in `.claude/skills/` are written
as instructions, so they read perfectly well as documentation for doing it by
hand.
