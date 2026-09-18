---
name: setup
description: >
  Set up Study Hub on this machine: write its config, start the server,
  open it in a browser, and offer to keep it running after a restart. Use this
  skill the first time someone opens this folder, and any time they say "set me
  up", "install this", "get this working", "start the server", "it isn't
  running", or "how do I open my lessons". It is also the right skill when the
  server was working and has stopped, when the browser says it cannot connect,
  or when someone wants to move where their courses are stored.
---

# Setting up Study Hub

You are setting this up for someone who may never have opened a terminal. They
double-clicked a folder and asked for help. **Do the work yourself and tell them
what happened in plain language.** Do not hand them commands to run, do not
explain what a port is, and do not apologise for how long anything takes.

## What this thing is, in one paragraph

A small server runs on their own machine and serves their lessons to their own
browser. Nothing is uploaded anywhere. Their highlights, notes, cards and
bookmarks are files on their disk beside the lessons. When they close the
browser nothing is lost, and when they close the server nothing is lost either.

## Do this in order

### 1. Check Python is there

```
python3 --version
```

Anything 3.9 or newer is fine. macOS has it. If the command is missing, say so
and point at <https://www.python.org/downloads/>; do not try to install it for
them.

### 2. Write the config

From the kit folder:

```
python3 server/study_server.py --init
```

This writes `~/.kcl-study/config.json`, readable only by them, and prints where
their courses folder is. It finds the `claude` command if it is installed, and
it turns vault publishing **off** unless it actually finds an Obsidian vault to
write into, because a reader that boots complaining about a missing vault looks
broken when it is merely unused.

🔴 **The config contains a token. Never print the file, never read it out, and
never paste it into the chat.** If you need to know a setting, read the file in
Python and print the key names or the one value you need. A token in a
transcript is a token that has to be replaced. The ONE exception is the phone
step below, where the owner has to type their own token into their own phone
once, and there is no other way for it to reach them.

If the file already exists, `--init` leaves it alone and says so. That is the
correct behaviour on a second run; do not delete it to force a fresh one.

### 3. Start it

```
python3 server/study_server.py
```

Run it in the background so the session stays usable. Then **wait for the port
to answer** before you claim anything works:

```
curl -sf http://127.0.0.1:8795/healthz
```

🔴 **A start can fail silently.** If something is already on the port, the new
process dies and the old one keeps answering `/healthz` perfectly. So when you
are restarting rather than starting, wait for the port to go quiet first, and
prove the new code is live by checking something only the new version has.

### 4. Open it

Open <http://127.0.0.1:8795/> in their browser. That is the home page: one card
per course, with where they left off, an **Add a course** box, and links to the
Notebook and to Settings.

**A brand new kit has no courses in it, and the page says so.** That is not an
error. It is the point at which they choose one of the two paths below.

### 5. Get some lessons into it

Ask which of these they have, and say it in their words, not ours:

- **Somebody sent them a course** (one `.zip` of the whole course, carrying its
  caption cues when the sender ticked that box) **or a lesson pack** (one `.html`
  file per lesson, or a folder of them). 🔴 **Tell them to do this themselves, in the browser**: on the course
  page there is a **Choose a lesson file** button, and the whole page is a drop
  target. That is the route the product is built around and the one they can
  repeat next week without you.

  Do it from here only when they ask you to, or when there are dozens of files:
  ```
  python3 server/lesson_packs.py --import <file, folder or .zip> --module <CODE>
  ```
  `<CODE>` names the course. **It does not have to exist yet**: the import
  creates it and says so. Take the code from the module if they know it,
  otherwise ask what to call the course, and check the spelling before running
  it, because a typo makes a second course rather than an error.

- **They have the course on KEATS and want their own copy.** Use the
  **download-keats** skill. They sign in themselves; nothing of theirs is stored.

- **They already have a folder of slides and transcripts.** Point the course at
  it and use the **write-lesson** skill to turn it into lessons.

**Captions are optional, and the first build installs an engine.** A course
they download has no captions until they ask: the wizard's checkbox, or in
Settings **Install the caption engine** and then **Build the missing captions**.
The install fetches a few hundred megabytes once, into `~/.kcl-study/`, and says
the size before it starts; nothing runs unless they tick or click. From here it
is `python3 server/caption_course.py --install`, and the same command tells
you what it would need first. ⚠️ Reading a transcript also needs `pdftotext`,
which the install cannot supply; on a Mac with Homebrew it is
`brew install poppler`, and the readiness report says so when it is missing.

**Brain-region pictures are optional, and one download serves every course.**
Without them a region named in a lesson opens on whatever Wikipedia leads
with; with them, on a labelled plate chosen for the purpose, with a
definition. The wizard's checkbox, **Install the pictures** in Settings, or
`python3 server/regionpack.py --install` from the Study Hub folder all do the
same thing: fetch about 44 megabytes once, from the same release the kit came
from, check it, and put it in the folder's own `knowledge-packs/`, with no
restart. The verb prints a line saying the plates have not had a formal domain
review; that is a fact about the pack, not an error, and they should see it.

### 6. Offer to keep it running

Ask whether they want the reader to come back on its own after a restart. If
yes, on macOS write a LaunchAgent to
`~/Library/LaunchAgents/com.kcl-study.server.plist` running the same command,
with `RunAtLoad` and `KeepAlive` true, and load it with `launchctl load`. Tell
them the one command that turns it off again. If they say no, tell them the
reader is off whenever their machine restarts, and that asking you to "start my
lessons" is enough to get it back.

**After this step they never need a terminal again.** Say that out loud, and
tell them to bookmark the page.

## When it will not start

- **"Address already in use"**: it is probably already running. Open the page
  before you conclude anything is wrong.
- **"No config"**: step 2 has not run.
- **The page loads but has no courses**: that is step 5, not a fault.
- **A course is there but a lesson 404s**: the lesson file is missing from the
  course folder. Check the folder holds `.html` files, not a nested folder of
  them.

## The phone, for someone who asks for it

"Put Study Hub on my tailnet" or "I want this on my phone" means this, and it
is the ONE legitimate reason to leave loopback:

1. Check Tailscale is installed and connected on this machine (`ifconfig`
   shows an address in 100.64.0.0/10). If not, send them to
   <https://tailscale.com> for both devices, free plan, same account, and wait.
2. Set `bind_ip` in `~/.kcl-study/config.json` to this machine's tailnet
   address, by editing the file in Python. The config already carries a token
   from `--init`; a non-loopback bind requires it and the server enforces that.
3. Restart the server and health-check the new address.
4. Tell them the address to open on the phone
   (`http://<tailnet-ip>:8795/`), and that the phone will ask once for the
   token. Print the token's VALUE for them to type this once, and nothing
   else from the config; it is theirs, on their own machine, and the phone
   dialog is where it goes.
5. Say plainly: the phone works while this computer is on and both are on
   Tailscale. 🔴 If the machine is ever re-registered in Tailscale, its
   numeric address can CHANGE and the phone bookmark dies; prefer the
   MagicDNS machine name in the address if their tailnet has it on.

## Two things not to do

- **Do not bind beyond this machine except for the tailnet route above.** The
  config defaults to `127.0.0.1`. The tailnet bind needs the token and a
  connected Tailscale; the server refuses `0.0.0.0` outright because it
  writes files and runs a subprocess.
- **Do not put their courses inside the kit folder if they might replace the kit
  later.** The default is fine for now. If they want them elsewhere, **send them
  to Settings rather than editing the config**: the courses folder is a field
  there, it asks what should happen to the courses already in the old folder, and
  it moves them if that is the answer. Hand-editing `courses_dir` does none of
  that, and a config pointed at a folder that is not there stops the server
  starting at all.
