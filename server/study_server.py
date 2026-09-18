#!/usr/bin/env python3
"""
KCL Affective Disorders: the local study server.

One stdlib-only process. It does four things the published artifacts cannot:

  1. Serves notes/ over http://127.0.0.1 so the pages run in a secure context
     with no CSP muzzle. Everything below follows from that.
  2. Writes EH's highlights and notes straight into the Obsidian vault,
     with no export step and no permission prompt. The SERVER holds the
     filesystem, not the page, so there is no File System Access dance.
  3. Looks terms up for free: the module's own glossary first, then MeSH,
     then Wikipedia. No API key, no cost, no account.
  4. Explains a selection on demand by shelling out to the Claude Code CLI,
     which uses EH's existing subscription. Still no API key.

Security posture, deliberately narrow because /api/explain runs a subprocess:
  - loopback bind only, 0.0.0.0 refused outright;
  - Host and Origin header validation on every request;
  - static serving confined to notes_dir, symlinks resolved and refused;
  - the vault filename is BUILT from validated fields, never taken from the
    request, and the result must resolve inside the configured Courses dir;
  - the CLI is invoked with an argv list, never a shell, with a fixed model
    from config and a hard timeout.

Config lives outside the repo and outside any synced folder (it is machine
state, and one day may hold a key): ~/.kcl-study/config.json, mode 0600.

    python3 server/study_server.py --init     # scaffold the config
    python3 server/study_server.py            # run it
"""

import argparse
import ast
import hashlib
import hmac
import html as html_mod
import json
import os
import re
import secrets
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# The content/reader split lives next door: one implementation of what a page is
# made of, shared by the server that serves them and the tool that made them.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import split_lessons                                            # noqa: E402
import icon                                                     # noqa: E402
# 🔴 `timeline`, NOT `presinfo`. The blob format and the scaler live in their own
# module precisely so this file can import them: `presinfo` globs the course tree,
# shells out to `ffprobe` and parses arguments, and a test pins it out of this
# server's import closure. See `timeline.py`'s own docstring for the split.
import timeline                                                 # noqa: E402
# The brain-region picture pack: a curated local plate instead of whatever
# Wikipedia's editors chose as an article's lead image. Its own module because it
# is data plumbing with no HTTP in it, and because it has to be testable without
# starting a server.
import regionpack                                               # noqa: E402
# What a downloaded material's name says about it: one sentence, shared with the
# captions and the pack copier, so "which of two transcript files is current" is
# answered the same way everywhere a transcript is looked up.
import material_names                                           # noqa: E402


# --- which code is actually answering ---------------------------------------------------
#
# 🔴 The deploy rule this project wrote for itself says to "confirm the new build id
# is the one actually serving, on a health endpoint that reports it. A stale build
# id is the single most common silent failure." There was no such endpoint, and we
# have paid for that four sessions running: four handovers in a row claimed NOT LIVE
# YET about code that was already live, each caught only by somebody comparing
# `ps -o lstart` against `git log`.
#
# 🔴 WHY IT IS COMPUTED HERE, AT IMPORT, AND NEVER AGAIN. Reading `git rev-parse HEAD`
# when the request arrives would defeat the whole purpose: it reports the working
# tree, so it would match whatever the reader compares it against and could never
# reveal a stale process. That is this project's "a probe and the thing it probes are
# not independent witnesses" in its most inviting form. Even re-reading the FILES per
# request would lie, and more subtly: after an edit it would report the new source
# while the old code went on running, which is exactly the state we want to detect.
#
# 🔴 It is a digest of the SOURCE, not the git revision, because the source is what is
# running. A revision is stale the moment a file is edited without being committed,
# which is a normal state during a deploy, and it needs a git checkout to exist at
# all, which the packaged kit does not have.
#
# 🟢 The file set is DISCOVERED rather than listed. `keep_the_losing_copy` was
# invented twice and inherited by four of six sidecars, because a protection you must
# remember at each new call site is one new call sites do not get. A hardcoded list
# here would rot the same way: the day somebody adds a third sibling module, a
# restart-needing change would stop moving the id. Anything loaded out of this
# directory counts, so the answer maintains itself.
#
# 🟢 The reader front end is deliberately NOT in it. `local-layer.html` and
# `reader/shell.html` both go through `read_reader_part()`, cached on mtime and size,
# so they live-reload with no restart and are always current. Stamping them here
# would make the id change when nothing stale could exist, which is worse than not
# having one. The staleness surface is the Python, and only the Python.

def _local_module_files(root=None):
    """Every module in this directory the server can reach by importing, with
    the bytes each one holds. Returns `{path: bytes}`.

    🔴 SCANNED FROM SOURCE, not read out of `sys.modules`, and that is the
    correction QA earned on 2026-09-01. `sys.modules` is a fact about a MOMENT,
    and the moment this runs is module import, when only the top-level siblings
    exist. `readings`, `mistakes` and `lesson_packs` are imported INSIDE
    functions -- eleven such sites in this file -- so they were invisible, and a
    restart deploying a change confined to them moved the id not at all. QA
    measured it with a control: the same harness saw `split_lessons.py` move and
    saw those three sit still.

    🟢 The transitive closure of LOCAL imports, at any indent depth, and
    `anchors.py` is correctly outside it: it is a test helper the server never
    imports.

    🔴🔴 THE COUNT BELOW IS THE ONLY ONE IN THIS FILE AND A TEST HOLDS IT TO
    THE TRUTH. It said "exactly six" from 2026-09-01, and it was **six, then
    seven, then nine**: three wrong numbers for one list, in a docstring whose
    own argument is that hand-maintained module lists go stale. ⚠️ The fix is
    not to delete the number, which would lose something useful; it is that
    `test_build_id` parses THIS sentence and asserts it against
    `len(_local_module_files())`, so it cannot drift again without going red.
    **Say it in exactly this shape, once, or the test will not find it.**
    🟢 Ten since 2026-09-17: `material_names.py`, the one place every reader of
    a downloaded material's name asks whether it is superseded, and the test
    went red on its own and was agreed to in the same unit.

    Today the closure holds exactly 10 files.

    🔴 THE TWO OBVIOUS ALTERNATIVES BOTH LOSE, and the reasons are worth
    keeping because both will be re-proposed.
    * *Digest every non-test `.py` in this directory.* That is 35 files, most of
      them one-off build scripts the server never imports, so editing a
      lesson-build script would move the server's build id. **A signal that
      fires when nothing relevant happened trains people to stop reading it**,
      and this signal's whole job is to be believed on the one day it matters.
    * *Import the deferred modules eagerly, then digest `sys.modules`.* That
      makes answering `/healthz` depend on importing three more modules, so a
      broken sibling would take out the endpoint that exists to tell you a
      sibling is broken. A probe and the thing it probes are not independent
      witnesses.

    🔴 AND THE THIRD, WHICH LOOKS FREE: compute the id lazily on the first
    `/healthz`, once real traffic has imported the deferred modules. It makes
    the id a function of what the process has been ASKED to do, so two calls to
    one process can disagree and the value drifts upward over its life. **A
    witness whose answer depends on when you ask it is not a witness.**

    ⚠️ WHAT A STATIC SCAN CANNOT SEE: a dynamic import (`importlib`,
    `__import__`, a module named by a string). There are none in this file
    today, and a test keeps it that way rather than the scan pretending to
    handle them.

    🔴 THERE IS DELIBERATELY NO `test_*.py` EXCLUSION, and it was here
    until a MUTATION survived without it. Under the old `sys.modules` reading
    the exclusion was load-bearing: test files really are in `sys.modules`
    whenever the suite is what imported the server, so the id would have
    depended on which test ran first. A source scan reaches a file only when
    something in the closure IMPORTS it, and no server module imports a test
    module -- `TheClosureIsScannedFromSourceAtAnyIndentDepth` pins that as the
    property it actually is. **And if one ever did, that file would genuinely be
    part of the running server**, so excluding it would rebuild the exact
    blindness this scan exists to remove. A guard that cannot fire is worth
    deleting; a guard that would fire wrongly is worth deleting twice.

    A file that will not PARSE still counts, by its bytes: it is running code
    that a restart would replace. Only the walk into its own imports is lost,
    which is the smallest honest degradation and keeps `/healthz` answering
    through exactly the breakage it is there to report.

    Cost, measured 2026-09-01: 42ms for the closure, once, at import.
    ⚠️ No count here on purpose. A timing note does not need one, and two
    sites carrying the same number is how this docstring went stale the
    first time: one was updated and the other was the site nobody read.
    """
    root = Path(root or __file__).resolve()
    here = root.parent
    found, queue = {}, [root]
    while queue:
        q = queue.pop()
        if q in found:
            continue
        # Deliberately NOT guarded: a file that is in the closure and cannot be
        # read means the digest would be computed over an incomplete set, and
        # the caller turns that into "unknown" rather than into a plausible
        # wrong answer. A module named in an import and absent from the disk is
        # a different thing entirely and never reaches here, because `is_file()`
        # gates what goes on the queue.
        found[q] = q.read_bytes()
        try:
            tree = ast.parse(found[q])
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    names = [node.module.split(".")[0]]
                elif node.level:                       # `from . import x`
                    names = [a.name.split(".")[0] for a in node.names]
            for n in names:
                cand = here / (n + ".py")
                if cand.is_file():
                    queue.append(cand.resolve())
    return found


def _compute_build_id(root=None):
    """A short digest of the Python this process actually runs.

    Never raises: a server that will not start because it could not identify
    itself would be a health check causing the outage it exists to report.
    """
    try:
        files = _local_module_files(root)
        h = hashlib.sha256()
        for q in sorted(files):
            h.update(q.name.encode("utf-8") + b"\0")
            h.update(files[q] + b"\0")
        return h.hexdigest()[:12]
    except Exception:
        return "unknown"


BUILD_ID = _compute_build_id()

# 🔴 Absolute paths, because these go on lesson pages served from
# /m/<CODE>/<file>.html as well as on the pages at the root, and a relative
# favicon there would be looked for inside the course folder.
#
# Three links rather than one, because the three are for different readers:
# the SVG is what every current browser uses and the only one that follows the
# tab bar's own light or dark; the .ico is what a browser asks for on its own
# when nothing tells it otherwise; the touch icon is what an iPhone uses when
# the page is kept on a home screen, which is how EH reads on a phone.
HEAD_ICONS = (
    '<link rel="icon" href="/favicon.svg" type="image/svg+xml">'
    '<link rel="alternate icon" href="/favicon.ico" sizes="32x32">'
    '<link rel="apple-touch-icon" href="/apple-touch-icon.png">'
)

# What the whole thing is called, chosen by EH on 2026-08-22. One constant,
# so the front page, the tab title and the guide cannot drift into three names.
PRODUCT = "Study Hub"

# Two paths, kept apart on purpose. MACHINE_CONFIG_PATH is the machine's own
# config: the environment variable, else the home-directory default. It is
# never reassigned, because load_config compares against it to decide that a
# config is a RIG's and so gets no vault by default. CONFIG_PATH is the file
# this process reads AND writes: main() points it at `--config` before
# anything loads or saves (bind_config_path), and read_raw_config /
# save_raw_config follow it at call time.
#
# 🔴 Until 2026-09-18 there was one name for both, and `--config` was a local
# variable in main() handed to load_config alone. So a rig started with
# `--config <scratch>` read the scratch file and `/api/machine` wrote the key
# it was given into the machine's own config, backup and all, answering `ok`.
# study-hub-qa found it by writing a sentinel into the owner's real file. The
# read succeeding is exactly what convinces you the flag worked.
MACHINE_CONFIG_PATH = Path(os.environ.get("KCL_STUDY_CONFIG",
                                          "~/.kcl-study/config.json")).expanduser()
CONFIG_PATH = MACHINE_CONFIG_PATH


def bind_config_path(flag):
    """Point CONFIG_PATH at `--config` when it was given, and return the path
    the process will use. With no flag the machine's own config is it, exactly
    as before. 🟢 The flag wins over KCL_STUDY_CONFIG when both are set: it is
    the more specific of the two and the one typed at the moment of use."""
    global CONFIG_PATH
    CONFIG_PATH = Path(flag).expanduser() if flag else MACHINE_CONFIG_PATH
    return CONFIG_PATH
# 🔴 Moved off 8792 on 2026-08-21, and RESERVED_PORTS below refuses it, so this
# had to move with it: leaving the default ON a reserved port refuses every fresh
# install with "Port 8792 belongs to another Agent Nexus service", which is a
# first-run blocker of exactly the kind the kit was built to avoid. Caught by
# running --init in a scratch HOME straight after reserving the port.
DEFAULT_PORT = 8795

# 8790 is the Social dashboard, 8791 is nexus-web. See Agent Nexus services.md.
#
# 🔴 8792 is CONTESTED and this server moved off it on 2026-08-21. Agent Nexus
# web also binds it, on 127.0.0.1, so the two coexisted only because this one
# binds the Tailscale address. Nothing broke, but a health check against
# 127.0.0.1:8792 SUCCEEDS against Nexus, which would report this server healthy
# while it is dead: worse than a check that fails. Refused here so a fresh
# install on this machine cannot wander back into it.
RESERVED_PORTS = {8790, 8791, 8792}

REPO = Path(__file__).resolve().parent.parent

def is_loopback(ip):
    return str(ip) in ("127.0.0.1", "::1", "localhost")


def bind_allowed(ip):
    """Loopback, or a Tailscale address. Nothing else, and never 0.0.0.0.

    Tailscale hands out 100.64.0.0/10, so the tailnet is the only network this
    is ever allowed to appear on. That network is EH's own devices, which is
    what makes a single shared token an adequate second gate rather than the
    only one."""
    if is_loopback(ip):
        return True
    parts = str(ip).split(".")
    if len(parts) != 4 or not all(p.isdigit() for p in parts):
        return False
    a, b = int(parts[0]), int(parts[1])
    return a == 100 and 64 <= b <= 127


VAULT_MARKER = ".obsidian"

# Tried by name before the marker scan below. A shortcut, never a requirement:
# every name here is also found by the scan, and this list only decides which
# vault wins on a machine that has more than one.
#
# 🔴 **EMPTY, AND DELIBERATELY SO SINCE 2026-09-11.** It used to name the
# author's own vault, which is a personal fact about one machine shipping inside
# software handed to other people (EH: *"Let's get rid of that"*). **Emptying it
# changes nothing that was measured**: the marker scan below finds the same vault
# on his machine, because a name here is a tie-break and never a requirement, and
# `find_vault_courses()` returned the identical path with the list full and with
# it empty. ⚠️ **The mechanism stays** so a machine with several vaults can
# express a preference; what is gone is a default nobody else should inherit.
PREFERRED_VAULT_NAMES = ()


def find_vault_courses():
    """Where to publish highlights, worked out from what is actually on the disk.

    Nothing is created here: if no vault is found the config carries a best
    guess and the server refuses to write until the path is real, rather than
    growing a convincing empty copy of somebody's notes beside it.

    🔴 This used to look ONLY for one hardcoded folder name, the author's own
    vault. It therefore found nothing on any other machine, and since `--init`
    turns publishing on only when a vault is found, a recipient with a perfectly
    good vault called something else got the feature switched off with no way to
    discover why (plan 02 §9).

    So: any named preference first (`PREFERRED_VAULT_NAMES`, empty by default
    since 2026-09-11), then any real Obsidian vault in the usual roots. `.obsidian` is
    the marker Obsidian itself writes, so this recognises a vault rather than
    guessing from a name."""
    home = Path.home()
    roots = [home / "Documents",
             home / "Library" / "Mobile Documents" / "com~apple~CloudDocs",
             home]
    roots += sorted(home.glob("*Dropbox*"))

    for root in roots:
        for name in PREFERRED_VAULT_NAMES:
            if (root / name).is_dir():
                return root / name / "Courses"

    # Any folder Obsidian has opened as a vault. Sorted so the answer does not
    # depend on directory order, and shallow so this never walks a whole disk.
    for root in roots:
        try:
            for child in sorted(root.iterdir()):
                if child.is_dir() and (child / VAULT_MARKER).is_dir():
                    return child / "Courses"
        except OSError:
            continue

    # 🔴 Not a real path, and deliberately so: `--init` tests whether the parent
    # exists to decide whether to switch publishing on, and this one does not,
    # so a machine with no vault starts with the feature off.
    return home / "Documents" / "Vault" / "Courses"


DEFAULT_CONFIG = {
    "bind_ip": "127.0.0.1",
    # Where the update check asks for the latest kit version: a URL serving
    # JSON {"version": "...", "url": "...", "note": "..."}. The default is the
    # public kit repository's feed (EH's yes, 2026-08-28); empty turns the
    # check off. The repo instance reads as version "dev" and never checks,
    # so this default only ever fires on a stamped kit install.
    "update_url": "https://raw.githubusercontent.com/ehawkin/study-hub/main/version.json",
    "port": DEFAULT_PORT,
    # Only used when bind_ip is not loopback. Generated by --init.
    "token": "",
    # Extra Host-header values to accept alongside the bound address, port
    # appended automatically. Added 2026-08-28 for the machine's stable
    # tailnet DNS name, after the raw tailnet IP changed under every
    # bookmark: the name survives a node re-registration, the IP does not.
    # Same posture as the bound address: pages readable, /api still wants
    # the bearer token on a non-loopback bind.
    "extra_hosts": [],
    # 🔴 TLS. Both empty means https is OFF and nothing about this
    # server changes; that is the shipped default and the kit's only state,
    # because a recipient reads on `127.0.0.1`, which is already a secure
    # context. Filled in, they are paths to a certificate and its key, kept
    # OUTSIDE the repo beside this config and mode 0600. On this machine they
    # come from `tailscale cert myrock.tail1e9444.ts.net`, which issues a real
    # Let's Encrypt certificate, so no device needs a CA installed.
    #
    # 🔴 A SECOND PORT, not a second scheme on the same one, ruled
    # 2026-08-30. `https://name:8795` is a fourth origin whichever port is
    # chosen, because an origin is scheme AND host AND port; what differs is
    # whether the THIRD origin keeps working. Serving https on its own port
    # leaves http listening on 8795 to redirect, path preserved. Serving it on
    # 8795 makes that redirect impossible, because nothing is left to answer.
    #
    # 🔴 EXPIRY TURNS ALL OF THIS OFF, and it has to, because these
    # certificates last about 90 days. A file that is present and readable but
    # out of date used to leave the listener running and the redirect firing at
    # a port every browser refuses: measured, a total outage rather than a stale
    # feature. `tls_paths` now asks the DATES as well, so the gate, the listener
    # and the redirect all go off together and http keeps serving. Renewal
    # itself is still not built; the start warns when expiry is close.
    "tls_cert": "",
    "tls_key": "",
    "tls_port": 8796,
    # The root that holds one folder per module. Empty means the single-module
    # world this project started in, where `notes_dir` IS the module and is
    # served from `/`. Both work; see resolve_modules().
    "courses_dir": "",
    "notes_dir": str(REPO / "notes"),
    # R8/R9. EH's choice of name and of a directory of its own, 2026-08-14.
    "resources_dir": str(REPO / "resources"),
    # Where the share button writes its zip. Empty means the Desktop (or the
    # home folder when there is no Desktop), which is the right answer for a
    # person and the only one the reader ever offered. 🔴 A HARNESS KEY, not a
    # preference: a scratch server whose every other path points into a
    # scratch folder still wrote its zip onto the machine owner's real Desktop
    # (measured 2026-09-17, twice), and `export_course` names the file after
    # the course CODE alone, so a rig serving a code he also has would
    # overwrite his own share file and say nothing. The rigs set this to their
    # own folder; nothing in Settings shows or sets it.
    "share_dest": "",
    # 🔴 Off is the right default for anyone who is not EH (plan §10d item 1):
    # a recipient has no Obsidian vault, and a reader that boots with a vault
    # badge and a "vault not found" warning looks broken on arrival. His config
    # has it on; --init turns it on only when a vault is actually found.
    "vault_enabled": True,
    "vault_courses": "",          # filled in by --init, per machine
    "class_name": "Affective Disorders",
    # 🔴 Per MODULE, not per machine, and it must never change for a module that
    # already has marks in a browser: it is the localStorage key prefix, and two
    # modules served from one origin would otherwise share marks for W1-T1-P1.
    # This module keeps the prefix it was born with, so nothing is migrated.
    "store_prefix": "kcl-affective-highlights:",
    "project_link": "KCL - Affective Disorders",
    "explain_enabled": True,
    # Measured on a term from this module: Sonnet and Haiku came back in the
    # same 8 seconds, and Sonnet named the model behind the concept where Haiku
    # only paraphrased it. Set this to claude-haiku-4-5-20251001 if that stops
    # being true.
    "explain_model": "claude-sonnet-5",
    "claude_bin": "",
    # How the reader reaches Claude for Explain, the chat and Rewrite. See
    # ask_backend(): auto is the CLI when it is there, else the API when a key
    # is set, so nothing changes on a machine that has Claude Code.
    "ask_backend": "auto",
    # 🔴 The machine owner's own key, in this file (0600) and nowhere else:
    # never in /api/status, the settings page, the log or an error string.
    "api_key": "",
    # Optional. Sent in the User-Agent of the public definition lookups, which
    # is the polite convention for them. Empty means the software identifies
    # itself and nobody else. See user_agent().
    "contact_email": "",
    "explain_timeout": 120,
    "lookup_timeout": 12,
    "cache_dir": "~/.kcl-study/cache",
    "log_path": "~/.kcl-study/server.log",
}

WRITE_LOCK = threading.Lock()
# One save of the config at a time. /api/machine takes no lock of its own,
# and save_raw_config writes through a shared `.tmp` name, so two saves in
# flight used to race each other as well as share a backup name.
CONFIG_LOCK = threading.Lock()

# Marker pair that fences one part's block inside a shared topic note. Anything
# outside a pair is EH's own writing and is never touched.
BLOCK_START = "<!-- study:{doc}:start -->"
BLOCK_END = "<!-- study:{doc}:end -->"
ANY_START = re.compile(r"<!--\s*study:([A-Za-z0-9\-]+):start\s*-->")

# A doc id is a lesson's filename stem up to the first dash-word, and it becomes
# part of every sidecar's filename, so this pattern is a path-safety boundary
# before it is anything else: letters, digits and single dashes, nothing that can
# traverse, quote or hide.
#
# 🔴 Widened 2026-08-16 (plan §10d item 5). It used to be `W#-T#-P#`, which is
# THIS module's shape. Another course may be weekly, or seminar-based, or number
# its parts differently, and a reader that refuses to open anything else cannot
# be given to anybody. What it must NOT do is stop being strict: the characters
# allowed are unchanged, only the shape is. `W3-T3-P4` still matches, and so does
# `L07`, `wk2-seminar-1` or `unit3-2`.
DOC_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,15}(?:-[A-Za-z0-9]{1,16}){0,5}\Z")


def captioned_docs(folder):
    """How many LECTURES in this course carry captions the reader is served.

    🟢 **THE QUESTION THIS NUMBER ANSWERS, said here because a bare count that
    does not name its own question is how this went wrong twice in one day, in
    opposite directions:** it is **how many lectures carry SOME captions**, not
    how many are finished. **A lecture with 12 of its 20 clips captioned is
    counted**, and the caption build still has work to do on it. The per-lecture
    answer (done, part-way, blocked) is what the Captions section in Settings
    prints, from `caption_course.lectures()`, which counts clips; this number is
    a decision aid on a page that must not shell out to anything.

    A lecture is captioned when a directory named after it holds **any `.vtt`
    the reader could ask for**. **All three halves are required** and each one
    of them has been the missing half of a live defect.

    🔴 **DEFECT ONE, THE NAME, found by `study-hub-qa` 2026-09-09.** The count
    used to be "every directory under `captions/` holding a `video.vtt`", never
    looking at the name, so it swept up **our own mandated timestamped
    backups**: `W4-T2-P1.20260904-163101.bak` and its siblings. **One course
    read 21 when 9 lectures were captioned**, because six were counted three
    times each.

    🔴 **DEFECT TWO, THE SHAPE, found by `study-hub-qa` the same evening, and it
    was four times bigger than the one above.** Fixing the name left
    `(d / "video.vtt").is_file()` in place, and **`video.vtt` is the MINORITY
    shape this system produces.** A narrated package is captioned **per clip**:
    `captions.py` names each file after the mp3 it was aligned from and the
    player rebuilds that name (`player-controls.html`,
    `name.replace(/\\.mp3$/i, ".vtt")`), so a lecture holding `sound1.vtt`
    through `sound20.vtt` counted as uncaptioned. **Measured on the disk: 37
    captioned lectures read as 9, and a course with 8 read as 0.** ⚠️ **QA
    walked one of them and found the tracks attached and 20 cues loaded, so the
    count was wrong about work that is live in front of the reader.**

    ⚠️ **It is not cosmetic, and the reason is what makes it worth a docstring.**
    The number exists to stop somebody ticking a box that quietly costs forty
    minutes, or skipping one they need. **Reading 21 of 38 when the truth is 9
    invites leaving it unticked and never getting the other 29; reading 9 when
    the truth is 37 invites re-running the job over lectures that already have
    captions.** **A wrong number in a decision aid is worse than no number,
    because it is acted on.**

    🟢 `DOC_ID_RE` is the right test rather than a `.bak` filter, and the
    argument is the one this project keeps having: **the reader serves captions
    BY LESSON ID**, so a directory whose name is not a lesson id can never be
    served whatever it holds. A list of extensions to exclude would be the same
    bug with a longer table. **The same call already appears at the stray-module
    sweep, so this is the file's own precedent rather than a new idea.**

    🟢 **And ANY `.vtt` rather than a list of the two filenames we see today, for
    the same reason.** The player asks for `video.vtt` for a plain recording and
    `<clip>.vtt` for a package, where the clip's name comes from the package
    (`name.replace(/\\.mp3$/i, ".vtt")`), **so the set of names is not ours to
    enumerate.** On the disk today every one of the 463 caption files is
    `video.vtt` or `soundN.vtt`, and **a table of those two would be defect two
    waiting for a third shape.**

    ⚠️ **`iterdir` rather than `glob`, and `is_dir()` in front of it, because a
    mutation sweep proved the check was doing nothing.** `Path.glob` on a FILE
    returns an empty list rather than raising (measured, 3.14), so with a glob
    the directory check could be deleted and every test stayed green: correct
    today, and correct only by an accident of the library. **With `iterdir` the
    guard is load-bearing**, and the test that a file named like a lesson is not
    a captioned lecture can fail for the right reason.
    """
    caps = folder / "captions"
    if not caps.is_dir():
        return 0
    return len([d for d in caps.iterdir()
                if d.is_dir() and DOC_ID_RE.match(d.name)
                and any(f.suffix == ".vtt" for f in d.iterdir())])


def caption_count_line(n):
    """The wizard's caption count as a SENTENCE, built here rather than in the page.

    🔴 **The sentence lives in Python because its wording is the fix, not
    decoration.** The manager's ruling on defect two made "say what the number
    means" a requirement rather than a nicety, and a requirement that lives in a
    JavaScript string inside a page template is one nothing can test without
    pinning its spelling. **Built here, the wording is a function anybody can
    run.** Its siblings on that page (lessons, videos, readings, PDFs) are still
    assembled in the page's own script; this one is not, and that asymmetry is
    the point rather than an oversight.

    🟢 **"whole or partly" is doing the work.** Without it the reader is told a
    number that answers "how many have some captions" while looking like an
    answer to "how many are finished", and those differ by every partly built
    lecture. **Empty string when nothing is captioned**, so the page says
    nothing rather than saying zero, which is what every sibling does.
    """
    if not n:
        return ""
    return "already on %d lecture%s, whole or partly" % (n, "" if n == 1 else "s")


# The shape this module happens to use, kept where a feature genuinely needs it
# (the vault's week and topic numbering) rather than as a gate on the whole
# reader.
WTP_RE = re.compile(r"^W\d{1,2}-T\d{1,2}-P\d{1,2}\Z")
# A colon is allowed because four of the module's topic titles contain one, and
# rejecting it silently blocked every vault write for those lessons: the badge
# said "not saved" and the highlights never reached the vault (found 2026-08-13).
# It is safe here because the two places this value lands both handle it:
# UNSAFE_FILENAME strips it out of the filename, and in the note it appears only
# in a markdown heading, never in YAML.
SAFE_FIELD_RE = re.compile(r"^[\w \-,:'()&./+]{1,120}\Z")
UNSAFE_FILENAME = re.compile(r'[/\\:*?"<>|\x00-\x1f]')

# 🔴 No personal address here. A contact address in a User-Agent is the polite
# convention for the public APIs this calls, but it must be the address of the
# person RUNNING the server, and this file is shipped to other students (plan 02
# §9). Whoever wants to be contactable sets `contact_email` in their own config;
# the default identifies the software and nobody else. Kept as a function rather
# than a constant so the config can reach it.
def user_agent(cfg=None):
    who = str((cfg or {}).get("contact_email") or "").strip()
    return ("kcl-study-server/1.0 (personal study tool%s)"
            % ("; " + who if who else ""))

# R26. KCL's Kaltura account, the one KEATS hands its lecture videos to. The
# subpartner is the partner id with "00" appended, which is Kaltura's own
# convention rather than a value anybody chose. Both are public: they appear in
# the KAF page's own markup. Kept here rather than in the layer so the identity
# of the account lives in one place.
KALTURA_PARTNER = "2368101"
KALTURA_SUBPARTNER = KALTURA_PARTNER + "00"


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

def load_config(path=None):
    path = Path(CONFIG_PATH if path is None else path)
    if not path.exists():
        sys.exit(
            "No config at %s\nRun:  python3 %s --init" % (path, Path(__file__).name)
        )
    cfg = dict(DEFAULT_CONFIG)
    raw = json.loads(path.read_text(encoding="utf-8"))
    cfg.update(raw)

    # 🔴 THE VAULT DEFAULTS ON FOR THE MACHINE'S OWN CONFIG AND FOR NOTHING
    # ELSE. On 2026-08-30 a scratch rig published `KCL RIG002 W02T1 - …` into
    # EH's real Obsidian vault. Nobody here noticed; the vault's own automation
    # reported it.
    #
    # The cause was a default rather than carelessness. `vault_enabled` is True
    # in `DEFAULT_CONFIG`, and a blank `vault_courses` falls back to
    # `find_vault_courses()` just below — which finds the REAL vault, because it
    # is looking at the real machine. **So any config a rig loaded inherited both
    # the switch and the path**, and every rig this project has run was one
    # publish away from writing into somebody's personal notes.
    #
    # 🔴 THE FIX THAT WAS REFUSED, and it matters because it is the obvious
    # one: adding `vault_courses` to the rig checklist. That checklist exists,
    # names five keys, says in its own words that such a list "is not a fact
    # anybody holds", and this was the THIRD breach and the second on a key it
    # does not name. Extending it is the fix that has already failed twice.
    #
    # A config that states `vault_enabled` gets what it asked for, either way,
    # and a rig that genuinely wants a vault has one line to write.
    #
    # 🔴 THE SENTENCE THAT USED TO FOLLOW WAS FALSE AND IT WAS THE SAFETY
    # ARGUMENT: "`--init` always writes the key, so every real install says so
    # explicitly." `--init` does write it — for configs IT creates. **EH's own
    # config predates that and does not contain `vault_enabled` at all**,
    # measured 2026-09-01 by `study-hub-qa` on the machine: the key is simply
    # not among its seventeen. So the ONE real install this project has is the
    # one relying on the default, and its vault is on because of the path
    # comparison below and nothing else.
    #
    # Nothing is broken by that today: his config sits at `MACHINE_CONFIG_PATH`,
    # so the comparison gives him `True`. The cost is that there is no second line of
    # defence: if his config were ever loaded from another path, or copied to
    # seed a rig, **publishing would silently stop** and the stated safety net
    # was never actually there. The remedy (backfill the key once, or persist
    # the resolved default on first computation) is a decision, not a coder's
    # to take: see the review-lane entry in `_admin/WORK-QUEUE.md`.
    #
    # ⚠️ THE RESIDUAL, named rather than left for somebody to find. "The
    # machine's own" means `MACHINE_CONFIG_PATH`, which honours `KCL_STUDY_CONFIG`.
    # A rig started with `--config <path>` is covered, which is how this project
    # starts them; a rig that instead EXPORTED `KCL_STUDY_CONFIG` would be its own
    # machine config by definition and would default on again. Closing that by
    # hardcoding `~/.kcl-study/config.json` would silently disable the vault for
    # anyone who legitimately relocates their config, which is a worse trade for
    # a hole no rig recipe here goes near. Pinned by a test that names it.
    #
    # 🔴 And it is MACHINE_CONFIG_PATH here, not CONFIG_PATH, on purpose: since
    # 2026-09-18 main() binds CONFIG_PATH to `--config`, so comparing against
    # that would make every rig equal to itself and hand it a vault.
    if "vault_enabled" not in raw and path != MACHINE_CONFIG_PATH:
        cfg["vault_enabled"] = False

    if not bind_allowed(cfg["bind_ip"]):
        sys.exit(
            "bind_ip must be loopback or this machine's Tailscale address (100.64.x.x "
            "to 100.127.x.x). 0.0.0.0 is refused: this server spawns a subprocess "
            "and writes files.")
    if not is_loopback(cfg["bind_ip"]) and not str(cfg.get("token") or "").strip():
        sys.exit(
            "A non-loopback bind needs a token. Add one to %s, or run --init on a "
            "fresh config to have one generated." % path)
    port = int(cfg["port"])
    if port in RESERVED_PORTS:
        sys.exit("Port %d belongs to another Agent Nexus service. Pick another." % port)
    cfg["port"] = port

    cfg["notes_dir"] = Path(cfg["notes_dir"]).expanduser().resolve()
    cfg["courses_dir"] = (Path(cfg["courses_dir"]).expanduser().resolve()
                          if str(cfg.get("courses_dir") or "").strip() else None)
    # An empty value would resolve to the current directory, which is the worst
    # possible place to write a vault note.
    cfg["vault_courses"] = (Path(cfg["vault_courses"]).expanduser()
                            if str(cfg.get("vault_courses") or "").strip()
                            else find_vault_courses())
    cfg["cache_dir"] = Path(cfg["cache_dir"]).expanduser()
    cfg["log_path"] = Path(cfg["log_path"]).expanduser()

    if cfg["courses_dir"] is not None and not cfg["courses_dir"].is_dir():
        sys.exit("courses_dir does not exist: %s" % cfg["courses_dir"])
    if cfg["courses_dir"] is None and not cfg["notes_dir"].is_dir():
        sys.exit("notes_dir does not exist: %s" % cfg["notes_dir"])
    # 🔴 An EMPTY courses root is a legitimate state, and it is the state every
    # fresh install of the kit starts in (plan 02 §9). Refusing to boot here made
    # the recipient's very first run exit with a sentence about "W…-T…-P….html",
    # which is meaningless to them and reads as a broken download. The home page
    # has had a real empty state since §10b; this let them reach it.
    #
    # The legacy single-module world still insists, because there `notes_dir` IS
    # the module: zero modules means the configured folder is wrong, and starting
    # anyway would serve a hub for notes that are not there.
    if not resolve_modules(cfg) and cfg["courses_dir"] is None:
        sys.exit("No modules found under %s. A module folder holds the lessons "
                 "(W…-T…-P….html) and their sidecars."
                 % cfg["notes_dir"])
    return cfg


def init_config(path=None):
    path = Path(CONFIG_PATH if path is None else path)
    if path.exists():
        print("Config already exists at %s, left alone." % path)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    cfg = dict(DEFAULT_CONFIG)
    from shutil import which
    cfg["claude_bin"] = which("claude") or ""
    cfg["vault_courses"] = str(find_vault_courses())

    # 🔴 A fresh install is born in the courses/ shape, and this is how the kit
    # gets it (plan 02 §9). The legacy single-module defaults below are THIS
    # module's: `class_name`, `store_prefix` and `project_link` describe
    # Affective Disorders, and a recipient inheriting them would see someone
    # else's class name in their reader and, worse, share a localStorage prefix
    # with it. A courses root supplies all three per module instead.
    #
    # The presence of the folder is the signal rather than a flag, because the
    # kit ships one and this repo did not have one until the folder move.
    if (REPO / "courses").is_dir():
        cfg["courses_dir"] = str(REPO / "courses")
        cfg["notes_dir"] = str(REPO / "courses")
        cfg["class_name"] = ""
        cfg["project_link"] = ""
        cfg["store_prefix"] = ""
    # 🔴 On only if there is actually a vault to write into. A fresh install on
    # a machine with no Obsidian otherwise boots showing "<vault name>: vault not
    # found", which is the first thing a new user would see and would read as a
    # broken install rather than as an unused feature.
    cfg["vault_enabled"] = Path(cfg["vault_courses"]).parent.is_dir()
    cfg["token"] = secrets.token_urlsafe(24)
    path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)
    print("Wrote %s (0600)." % path)
    print("  courses: %s" % (cfg["courses_dir"] or cfg["notes_dir"]))
    print("  vault:  %s" % (cfg["vault_courses"] if cfg["vault_enabled"] else
                             "off (no Obsidian vault found, and nothing needs one)"))
    print("  claude: %s" % (cfg["claude_bin"] or
                            "not found. Asking works through Claude Code, or "
                            "through an API key pasted in Settings."))


# --------------------------------------------------------------------------
# modules: one folder of lessons, and the reader pointed at one of them
# --------------------------------------------------------------------------
#
# Plan 02 §10a. The system was one module: one notes folder, doc ids unique only
# within it, one class name, one localStorage prefix. A second module needed a
# second server on a second port.
#
# A module is now a FOLDER, and the server can hold several:
#
#     courses/                      courses_dir
#       settings.json               reader preferences, shared by every module
#       PSY101/                     a module: exactly today's notes/ layout
#         settings.json             this module's facts, and any overrides
#         W3-T3-P4-….html           lessons and their sidecars
#
# 🔴 Everything downstream still reads `cfg["notes_dir"]`, so a request resolves
# its module ONCE and then works with a cfg whose notes_dir IS that module. That
# is why this landed without touching sixty call sites: the module is in the cfg,
# not in every signature.
#
# The single-module world is untouched. With no `courses_dir`, `notes_dir` is the
# one module, it is served from `/` as it always has been, and nothing about
# EH's setup changes until he moves the folder.

MODULE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_MODULE_LOCK = threading.Lock()
_MODULE_CACHE = {}          # (root, mtime_ns) -> {id: path}


def is_module_dir(path):
    """A folder with lessons in it, or a hub, is a module. Anything else is not,
    which is what keeps `_admin`, `resources` and a stray Downloads folder out of
    the list without needing a marker file."""
    if not path.is_dir() or path.name.startswith(".") or path.name.startswith("_"):
        return False
    if (path / "settings.json").is_file() or (path / "index.html").is_file():
        return True
    return any(True for _ in split_lessons.lessons_in(path))


def resolve_modules(cfg):
    """{module id: folder}, newest listing cached by the root's mtime."""
    root = cfg.get("courses_dir")
    if root is None:
        notes = cfg["notes_dir"]
        mid = module_facts_of(notes).get("id") or notes.name
        return {mid: notes} if notes.is_dir() else {}
    try:
        key = (str(root), root.stat().st_mtime_ns)
    except OSError:
        return {}
    with _MODULE_LOCK:
        hit = _MODULE_CACHE.get(key)
    if hit is not None:
        return hit
    found = {}
    for child in sorted(root.iterdir()):
        if not is_module_dir(child):
            continue
        mid = module_facts_of(child).get("id") or child.name
        if not MODULE_ID_RE.match(mid) or mid in found:
            mid = child.name
        if MODULE_ID_RE.match(mid):
            found[mid] = child
    with _MODULE_LOCK:
        _MODULE_CACHE.clear()
        _MODULE_CACHE[key] = found
    return found


def module_facts_of(folder):
    """The `module` block of a folder's settings.json, or {}. Read straight from
    disk rather than through read_settings, which needs a cfg and would recurse."""
    try:
        data = json.loads((folder / "settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    facts = data.get("module")
    return facts if isinstance(facts, dict) else {}


def default_module(cfg):
    mods = resolve_modules(cfg)
    if not mods:
        return None
    want = str(cfg.get("default_module") or "")
    if want in mods:
        return want
    return sorted(mods)[0]


def module_cfg(cfg, module_id):
    """A cfg pointed at one module: machine facts as they are, `notes_dir` at the
    module's folder, and the module's own name, class and store prefix on top.

    🔴 `store_prefix` per module is the one that must never be got wrong: it is
    the localStorage key prefix, and two modules sharing it would share marks for
    any doc id they both use. A module that does not set one gets a prefix
    derived from its id, which cannot collide; the original module keeps the
    literal historic value from machine config, so nothing of EH's moves."""
    mods = resolve_modules(cfg)
    if module_id not in mods:
        raise ValueError("no such module: %s" % module_id)
    folder = mods[module_id]
    facts = module_facts_of(folder)
    out = dict(cfg)
    out["notes_dir"] = folder
    out["module"] = module_id
    # 🔴 Attachments are keyed by DOC ID, and doc ids are not unique across
    # courses: EH's two share 21 of them (`W2-T3-P1`, `W3-T1-P1`, ...). Until
    # 2026-08-30 every course resolved `resources/W3-T2-P2/` to the SAME folder,
    # so the second course's Files tab listed, served and could displace the
    # first course's attachment. Every other per-doc store was already keyed to
    # `notes_dir`, which this sets per course; this one was chosen on 2026-08-14,
    # before courses were plural, and was the last machine-global left.
    #
    # It does not clash visibly when it bites, which is what makes it worth
    # fixing while `resources/` is still empty: re-attaching a name already used
    # renames the older file to `.bak` and `.bak` is filtered out of the
    # listing, so one course's `notes.pdf` simply stops being listed in the
    # other. Same shape as the vault block replacement b058c03 fixed.
    #
    # The legacy single-module install has no courses root and keeps the flat
    # layout, the same compatibility rule `store_prefix` follows above.
    if cfg.get("courses_dir") is not None:
        base = resources_base(cfg)
        out["resources_base"] = str(base)
        out["resources_dir"] = str(base / module_id)
    out["module_name"] = str(facts.get("name") or facts.get("title") or module_id)
    out["module_code"] = str(facts.get("code") or module_id)
    # 🔴 Identity is per COURSE, and a course that does not declare it gets
    # nothing rather than the machine's. The same rule `store_prefix` already
    # follows below, for the same reason, and `load_config` says so in words:
    # "a courses root supplies all three per module instead". It supplied two.
    #
    # The bug that fixed: a config written before courses were plural carries
    # machine-global `class_name` and `project_link` describing ONE course, and
    # `dict(cfg)` above hands them to every other course. That is how a
    # mindfulness note was filed under Affective Disorders, in its filename and
    # in `project: "[[...]]"`. b058c03 fixed the fresh-install half of this
    # story; this is the half that was live on an upgraded machine.
    #
    # The legacy single-module install keeps the configured globals, so nothing
    # of an existing user's moves.
    for field in ("class_name", "project_link"):
        if facts.get(field):
            out[field] = str(facts[field])
        elif cfg.get("courses_dir") is not None:
            out[field] = ""
    if facts.get("store_prefix"):
        out["store_prefix"] = str(facts["store_prefix"])
    elif cfg.get("courses_dir") is not None:
        # Only a courses-root module gets a derived prefix. The legacy single
        # module keeps the configured one, which is the one his browser holds.
        out["store_prefix"] = "kcl-study:%s:" % module_id
    return out


def module_url(cfg, module_id):
    """Where a module's hub lives. The single-module world keeps `/`."""
    if cfg.get("courses_dir") is None:
        return "/"
    return "/m/%s/" % module_id


def create_module(root, module_id, name="", class_name="",
                  project_link=None):
    """Make a course folder and its identity file. Returns the folder.

    🟢 `class_name` IS asked for now, since 2026-08-30, and `project_link`
    since 2026-09-09. The add-course page (`ADD_COURSE_PAGE`) asks for the full
    name, the short name and the code, all three required, plus the vault
    project name, which is optional and defaults to `KCL - <short name>`.
    `POST /api/modules` sends all four. Until that day
    this docstring said `class_name` was API-only and that no path a user could
    reach supplied it, which was true and is the reason the field existed
    unused: the home page's Add form had exactly two inputs.

    🔴 **`name` is still optional HERE and that is deliberate.**
    `lesson_packs.py` creates a course with neither name nor class when a pack
    is imported into one that does not exist, which is the kit's whole first-run
    path and has nobody to ask. The requirement lives in `POST /api/modules`,
    which is the route a person goes through. Do not "tidy" it down into this
    function: that breaks the kit, and the failure appears on somebody else's
    machine on their first run.

    🟢 So a class-less course stays a supported shape rather than a
    legacy one, and the vault namers still branch rather than interpolate an
    empty class: `KCL Biology Two W03T1 - Cell walls.md`, no double space, no
    empty `project:` line.

    🔴 One implementation, because there were about to be two. `lesson_packs.py`
    grew this on 2026-08-17 so that importing a pack into a course that does not
    exist creates it (the kit's whole first-run path), and the home page needs
    exactly the same thing when somebody adds a course by hand. Two copies of
    "what a new course is" would disagree about `store_prefix`, which is the one
    field that must never be got wrong.

    Refuses an existing folder rather than merging into it: adding a course that
    is already there is a mistake, and silently adopting somebody's lessons is
    the wrong way to find out."""
    if not MODULE_ID_RE.match(module_id or ""):
        raise ValueError("a course code is letters, digits, dots, dashes and "
                         "underscores, up to 64 characters")
    folder = Path(root).expanduser() / module_id
    if folder.exists():
        raise ValueError("there is already a course called %s" % module_id)
    folder.mkdir(parents=True)
    facts = {
        "id": module_id,
        "name": str(name or module_id).strip() or module_id,
        "code": module_id,
        # Per course and never changed afterwards: it is the browser-storage key
        # prefix, so two courses sharing one would share highlights for any doc
        # id they both use. Derived from the id, so it cannot collide.
        "store_prefix": "kcl-study:%s:" % module_id,
    }
    # Optional, and empty is correct rather than missing: the vault namers fall
    # back to `module_name`, and an absent class is how every course that is not
    # the original one looks. Written only when given, so settings.json does not
    # carry a field nobody set.
    if str(class_name or "").strip():
        facts["class_name"] = str(class_name).strip()
    # 🔴 THE THREE-STATE FIELD, and the distinction is the whole of EH's ask
    # (2026-09-08, from setting it by hand on the third course in a row):
    #
    #   None  nobody was asked. The kit's pack-import path, which has no one to
    #         ask, and every course made before 2026-09-09. The key is ABSENT,
    #         and `verify_course` reports the course's identity as partial.
    #   ""    asked and deliberately left empty: this course does not go to a
    #         vault. The key is PRESENT and empty, which is DECLARED, not
    #         missing, and the gate must stop nagging about it.
    #   text  the vault project this course's notes are filed under.
    #
    # ⚠️ `is None` rather than a truthiness test, because "" is a real answer
    # here and the two are the states that must not be collapsed.
    if project_link is not None:
        facts["project_link"] = str(project_link).strip()
    (folder / "settings.json").write_text(
        json.dumps({"module": facts}, indent=2) + "\n", encoding="utf-8")
    return folder


# --------------------------------------------------------------------------
# The two paths that are facts about this machine
# --------------------------------------------------------------------------
#
# 🔴 Reported 2026-08-21, twice in one message. The vault: "it has a vault
# publishing on or off but it doesn't let you select the location of the vault.
# Different people might have the vaults in different places. I may change where
# I have the vault." The courses folder: "it says this machine and the courses
# folder. I should also be able to change that, though we need to consider what
# happens if I have an existing course folder. We should be asked whether I want
# to move the items, or I should be told that I have to move any existing items.
# Otherwise things won't work."
#
# Both were printed as facts you could read and not change, because both live in
# `~/.kcl-study/config.json` rather than in the settings store, and nothing had
# ever written that file except `--init` and a person with a text editor.
#
# 🔴 Three things make writing it different from writing the settings store.
#
# 1. **The token is in there.** Every write reads the whole file, changes the one
#    key, and writes the whole file back at 0600. Nothing here ever constructs a
#    config from defaults, because that would silently drop the token and lock
#    every other device out.
# 2. **A bad value does not fail at write time, it fails at the NEXT START.**
#    `load_config` exits when `courses_dir` is not a directory, and it exits
#    before the server can serve the page that would let you fix it. So the value
#    is validated here, hard, and a folder that is not there is refused rather
#    than written and regretted.
# 3. **The answer to "what about what is already there" is his to give.** Moving
#    a course moves the marks inside it, and the honest options are move them,
#    leave them, or stop. So the endpoint ASKS: called without an explicit
#    answer, it writes nothing and returns what it found for the page to put to
#    him.

def read_raw_config(path=None):
    """The config file exactly as it is on disk, with no defaults merged in.
    `path` defaults to CONFIG_PATH at CALL time, so a test that points
    CONFIG_PATH at a scratch file (the way test_ask_backend does) is honoured,
    and so is `--config`, which main() binds there.

    🔴 Defaults must not be merged here. This dict is written straight back, and
    a merged default would silently become a written setting: the difference
    between "not set, so it follows the default" and "pinned to what the default
    happened to be the day you changed your vault path"."""
    path = CONFIG_PATH if path is None else path
    return json.loads(Path(path).read_text(encoding="utf-8"))


def claim_backup(name_at, mode=0o600):
    """Take a backup name that nobody else holds, and hand back the open file.

    `name_at(suffix)` names the file for a suffix: "" for the first backup of
    a second, then "-2", "-3" and so on. The name is claimed with O_EXCL, so
    two writers inside one second, or two threads at once, can never pick the
    same file: the loser counts up and tries again. The file exists with
    *mode* from the instant it exists, empty, and the caller fills it (or
    renames over it: see `set_aside`). Returns `(fd, path)`, the path being
    the file that was actually taken, which is what a route reports.

    🔴 The counter starts at -2 on purpose: the `-2` file IS the second
    backup of that second, and the first keeps the name it always had, so
    every reader of these names keeps working. Built for `save_raw_config`
    2026-09-18, after two cleanup writes in one second on the owner's own
    config shared a name; lifted here the same day because four more
    writers had the same shape, three of them on his own notes and the
    files he attaches, where the survivor of a collision is the INTERMEDIATE
    state and the copy a person restoring wants is the one that is gone."""
    n = 1
    while True:
        path = Path(name_at("" if n == 1 else "-%d" % n))
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        except FileExistsError:
            n += 1
            continue
        return fd, path


def set_aside(target):
    """Move an existing file into the `backups/` folder beside it under a dated
    name nobody else holds, and return where it went. A rename, never a copy
    and a delete: the attach and remove routes use this because he may have
    attached the only copy of something. The name is claimed first
    (`claim_backup`), so a second attachment of the same file inside one
    second, which used to rename OVER the first one's backup and lose it,
    now lands beside it as `-2`. The file is tightened to 0600 before the
    move, so the backup is 0600 from the instant it exists under that name."""
    target = Path(target)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    fd, bak = claim_backup(lambda n: split_lessons.backup_target(
        target, "%s.%s%s.bak" % (target.name, stamp, n)))
    os.close(fd)
    os.chmod(target, 0o600)
    target.replace(bak)
    return bak


def save_raw_config(data, path=None):
    """Write the config, keeping a dated copy of what it said before.

    The backup is beside the original and is left there: a person cleans those
    up, not the process that made them. 0600 on both, because the token is in
    both, and the key can be too.

    🔴 The stamp is to the second, and until 2026-09-18 a second save inside
    the same second copied over the first backup: the survivor held the
    intermediate state and the state BEFORE the first save, the one a person
    restoring actually wants, was gone. It cost something real the day it was
    filed: two cleanup writes in one second on the owner's own config, and the
    restore worked only because a forensic copy had been taken by hand first.
    🟢 So the name gets a counter when it is taken (`.bak`, then `-2.bak`,
    `-3.bak`), claimed with O_EXCL so two writers cannot pick it together, and
    the first backup of any second keeps the name it always had. Not a finer
    stamp: every reader of these names keeps working. The returned path is the
    file that was actually written, which is what the route reports. The
    claim itself is `claim_backup`, shared with every other backup writer in
    this file since the same day."""
    path = Path(CONFIG_PATH if path is None else path)
    with CONFIG_LOCK:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        fd, backup = claim_backup(
            lambda n: path.with_name(path.name + ".%s%s.bak" % (stamp, n)))
        with os.fdopen(fd, "wb") as out, open(path, "rb") as src:
            shutil.copyfileobj(src, out)
        shutil.copystat(path, backup)   # the mtime says when the config last changed
        os.chmod(backup, 0o600)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(path)          # atomic: no window where the config is half-written
        os.chmod(path, 0o600)
    return backup


def courses_in(folder):
    """The course folders directly inside a root, by name. Uses the same test the
    server uses to list them, so this cannot disagree with the home page."""
    try:
        return sorted(child.name for child in sorted(Path(folder).iterdir())
                      if is_module_dir(child))
    except OSError:
        return []


def set_vault_path(raw, wanted):
    """Point vault publishing at a folder. Returns (message, needs_restart)."""
    target = Path(str(wanted).strip()).expanduser()
    if not str(target).strip():
        raise ValueError("give a path, or switch vault publishing off instead")
    if not target.is_absolute():
        raise ValueError("that needs to be a full path, starting at the top of "
                         "the disk. %s is relative to wherever the server "
                         "happens to be running." % target)

    # A vault is the folder Obsidian opened, and what gets written is a `Courses`
    # folder inside it. Somebody pointing at the vault itself has given the right
    # answer to a slightly different question, so take it rather than refusing it.
    bits = []
    if (target / VAULT_MARKER).is_dir():
        target = target / "Courses"
        bits.append("that is the vault itself, so it will use the Courses folder in it")

    if not target.exists():
        if not target.parent.is_dir():
            raise ValueError("there is nothing at %s, and nothing at %s either, "
                             "so this was not written. Check the path."
                             % (target, target.parent))
        target.mkdir(parents=True)
        bits.append("made the folder, which was not there yet")
    elif not target.is_dir():
        raise ValueError("%s is a file, not a folder." % target)

    raw["vault_courses"] = str(target)
    said = "Notes will be published into %s" % target
    return (said + " (" + ", ".join(bits) + ")." if bits else said + "."), True


def set_api_key(raw, wanted):
    """Set, or with an empty value remove, the owner's API key. Returns the
    message. Nothing here echoes the key: not the message, not the log."""
    key = str(wanted or "").strip()
    if not key:
        raw["api_key"] = ""
        return "The API key is removed."
    if len(key) > 400 or any(ch.isspace() or not ch.isprintable() for ch in key):
        raise ValueError("that does not look like an API key: it has spaces or "
                         "characters a key cannot contain, so it was not saved")
    raw["api_key"] = key
    return "The API key is set."


def set_ask_backend(raw, wanted):
    """How the reader reaches Claude: auto, cli or api. Returns the message."""
    mode = str(wanted or "").strip().lower()
    if mode not in BACKEND_MODES:
        raise ValueError("the route must be one of auto, cli or api")
    raw["ask_backend"] = mode
    return {"auto": "Claude Code when the server has it, else the API key.",
            "cli": "Claude Code beside the server only.",
            "api": "The API, with the key set here, only."}[mode]


def set_courses_path(raw, wanted, answer, current_root):
    """Move the courses folder, or point at another one. Returns a dict.

    `answer` is what he said about the courses already in the old folder, and
    until he has said something this returns the question rather than acting:

      None      look, and describe what moving would mean
      "create"  make the folder, then come straight back with the move question
      "move"    move the course folders across, then repoint
      "leave"   repoint only; the old courses stay where they are

    🔴 "create" is its own answer and not a flavour of "leave". The first version
    of this had the Make-the-folder button send "leave", so making a folder
    quietly also answered the question about the courses already in the old one,
    and they were left behind without anybody being asked. Caught by clicking it
    in a browser rather than by reading the code, which had looked fine.

    🔴 Never both roots at once. A course that exists in both places is two sets
    of marks under one name, and there is no way to merge them that is not a
    guess about which highlight is the real one. So a name that exists on both
    sides stops the whole thing before a single file moves."""
    target = Path(str(wanted).strip()).expanduser()
    if not str(wanted).strip():
        raise ValueError("give a path")
    if not target.is_absolute():
        raise ValueError("that needs to be a full path, starting at the top of "
                         "the disk. %s is relative to wherever the server "
                         "happens to be running." % target)
    if target.exists() and not target.is_dir():
        raise ValueError("%s is a file, not a folder." % target)
    if not target.exists():
        if not target.parent.is_dir():
            raise ValueError("there is nothing at %s, and nothing at %s either, "
                             "so this was not written. Make the folder first."
                             % (target, target.parent))
        if answer is None:
            return {"ok": False, "ask": "create", "target": str(target),
                    "message": "There is no folder at %s. Make it?" % target}
        target.mkdir(parents=True)
        if answer == "create":
            answer = None          # made; now ask what happens to what is here

    target = target.resolve()
    here = Path(current_root).resolve() if current_root else None
    if here is not None and target == here:
        # Nothing to write, so nothing is written: a config backup made for a
        # save that changed nothing is a file somebody has to reason about later.
        return {"ok": True, "message": "That is already where they are.",
                "restart": False, "noop": True}

    moving = courses_in(here) if here is not None else []
    already = courses_in(target)
    clash = sorted(set(moving) & set(already))

    if answer is None:
        if not moving:
            answer = "leave"          # nothing to move, so nothing to ask about
        else:
            return {"ok": False, "ask": "move", "target": str(target),
                    "from": str(here), "courses": moving, "there": already,
                    "clash": clash,
                    "message": (
                        "There %s %d course%s in %s. Move %s to %s, or leave %s "
                        "there and start fresh in the new folder? Leaving %s "
                        "means the reader stops showing %s, and your highlights "
                        "stay behind in the old folder too."
                        % ("is" if len(moving) == 1 else "are", len(moving),
                           "" if len(moving) == 1 else "s", here,
                           "it" if len(moving) == 1 else "them", target,
                           "it" if len(moving) == 1 else "them",
                           "it" if len(moving) == 1 else "them",
                           "it" if len(moving) == 1 else "them"))}

    moved = []
    if answer == "move" and moving:
        if clash:
            raise ValueError(
                "%s already in %s, and moving would put two sets of highlights "
                "under one name. Rename one side first; nothing was moved."
                % (", ".join(clash) + (" is" if len(clash) == 1 else " are"),
                   target))
        for name in moving:
            src = here / name
            try:
                shutil.move(str(src), str(target / name))
            except (OSError, shutil.Error) as err:
                # 🔴 Stop on the first failure and say exactly how far it got.
                # A half-moved set of courses that reports success is the worst
                # outcome available here.
                raise ValueError(
                    "moved %d of %d and then stopped on %s: %s. The config was "
                    "NOT changed, so the reader is still pointed at %s."
                    % (len(moved), len(moving), name, err, here))
            moved.append(name)

    raw["courses_dir"] = str(target)
    # `notes_dir` is the default course, and on this machine it points INSIDE the
    # old root. Left alone it would point at a folder that just moved.
    notes = Path(str(raw.get("notes_dir") or "")).expanduser()
    if here is not None and str(notes).strip():
        try:
            rel = notes.resolve().relative_to(here)
        except (ValueError, OSError):
            rel = None
        if rel is not None:
            raw["notes_dir"] = str(target / rel) if str(rel) != "." else str(target)

    if answer == "move" and moved:
        msg = ("Moved %d course%s to %s. Restart to read them from there."
               % (len(moved), "" if len(moved) == 1 else "s", target))
    elif moving:
        msg = ("Now pointed at %s. The %d course%s in %s %s been left there and "
               "will not show until you move %s across yourself."
               % (target, len(moving), "" if len(moving) == 1 else "s", here,
                  "has" if len(moving) == 1 else "have",
                  "it" if len(moving) == 1 else "them"))
    else:
        msg = "Now pointed at %s. Restart to use it." % target
    return {"ok": True, "message": msg, "restart": True, "moved": moved}


def rename_module(root, module_id, name):
    """Change a course's display name. Returns the name that was written.

    🔴 The NAME and the CODE are different things and only one of them is safe to
    change. The code is the folder, the web address and the browser-storage
    prefix, so changing it would strand every highlight on that course; the name
    is what a person reads and nothing depends on it. Reported 2026-08-22: he
    made a course with a code and no name and there was nowhere to add one, which
    is a poor reason to live with the wrong word at the top of a page forever.

    Merged into the settings file rather than written over it, for the same
    reason `write_settings` merges: the same file carries the store prefix."""
    name = " ".join(str(name or "").split())[:120]
    if not name:
        raise ValueError("give the course a name, or leave the one it has")
    folder = Path(root).expanduser() / module_id
    if not is_module_dir(folder):
        raise ValueError("no course called %r" % module_id)
    path = folder / "settings.json"
    data = read_json_sidecar(path, {})
    facts = dict(data.get("module") or {})
    facts["name"] = name
    facts.setdefault("id", module_id)
    facts.setdefault("code", module_id)
    # 🔴 Never invented here. A course that somehow has no prefix keeps having
    # none rather than gaining one on a rename, because a prefix appearing for
    # the first time would move every mark on that course to a new key.
    data["module"] = facts
    write_json_sidecar(path, data)
    _MODULE_CACHE.clear()
    return name


def log(cfg, line):
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    try:
        cfg["log_path"].parent.mkdir(parents=True, exist_ok=True)
        with cfg["log_path"].open("a", encoding="utf-8") as fh:
            fh.write("%s  %s\n" % (stamp, line))
    except OSError:
        pass


# --------------------------------------------------------------------------
# vault writing
# --------------------------------------------------------------------------

def vault_filename(cls, week, topic_no, topic_title):
    """Courses/KCL <Class> W<nn>T<n> - <Topic title>.md, per the vault Conventions.

    Built from validated fields only. A request cannot name its own file.

    🔴 A course with no `class_name` still has to work, and the reason
    changed on 2026-08-30. It used to be the NORMAL case, because no path a user
    could reach supplied one; the add-course page now asks for it and every
    course made through the browser has one. What is left without one is a
    course created by `lesson_packs.py` on a pack import (the kit's first-run
    path, which has nobody to ask) and every course made before that date. So
    the empty branch below is not legacy and must not be removed. Interpolating an empty
    class used to leave "KCL  W03T1 - X.md" with a double space, because this
    function was written before courses were plural and never revisited when
    `vault_reading_filename` learned the answer on 2026-08-22. The two name
    files in the same vault by the same convention, so they follow the same
    three rules: branch rather than interpolate an empty class, collapse
    whitespace, cap the length.
    """
    cls = str(cls or "").strip()
    wt = "W%sT%s" % (str(week).zfill(2), topic_no)
    stem = ("KCL %s %s - %s" % (cls, wt, topic_title) if cls
            else "KCL %s - %s" % (wt, topic_title))
    stem = UNSAFE_FILENAME.sub("", stem).strip().rstrip(".")
    stem = re.sub(r"\s+", " ", stem)
    if not stem:
        raise ValueError("empty filename after sanitising")
    return stem[:120] + ".md"


def new_note_header(cfg, topic_title):
    """Minimal correct frontmatter. Nothing invented: categories, project and
    tags only, per the Vault Writing Contract. No week: or part: field, because
    the zero-padded filename already sorts and Conventions declined to add one."""
    lines = [
        "---",
        'categories: "[[Course Notes]]"',
    ]
    # 🔴 Guarded for the same reason `new_reading_header` guards it: a
    # courses-root install blanks `project_link`, and interpolating that
    # unconditionally wrote `project: "[[]]"` into the frontmatter, which
    # Obsidian shows as a broken link on every topic note a new user exports.
    if cfg.get("project_link"):
        lines.append('project: "[[%s]]"' % cfg["project_link"])
    lines += [
        "tags: [kcl, psychology]",
        "---",
        "",
        "## Topic - %s" % topic_title,
        "",
    ]
    return "\n".join(lines)


def vault_reading_filename(cls, title):
    """Courses/KCL <Class> Reading - <Title>.md, one note per reading.

    EH's decision, 2026-08-22: "Reading notes should publish into a single
    vault note for each reading, which is essentially a single vault note for
    each PDF, which might be a paper or a book chapter or an article or a whole
    book." The name deliberately departs from the W<nn>T<n> topic shape, because
    a reading has no week; "Reading" in the name is what keeps these sorting
    together and visibly distinct from the topic notes. Built from validated
    fields only, same as vault_filename."""
    cls = str(cls or "").strip()
    stem = ("KCL %s Reading - %s" % (cls, title)) if cls else ("KCL Reading - %s" % title)
    stem = UNSAFE_FILENAME.sub("", stem).strip().rstrip(".")
    stem = re.sub(r"\s+", " ", stem)
    if not stem:
        raise ValueError("the reading's title left nothing usable for a filename")
    return stem[:120] + ".md"


def new_reading_header(cfg, reading):
    """Frontmatter and header for a fresh reading note. Same contract as
    new_note_header: categories, project and tags only, nothing invented."""
    who = " ".join(str(reading.get(k) or "") for k in ("authors", "year")).strip()
    lines = [
        "---",
        'categories: "[[Course Notes]]"',
    ]
    if cfg.get("project_link"):
        lines.append('project: "[[%s]]"' % cfg["project_link"])
    lines += [
        "tags: [kcl, psychology, reading]",
        "---",
        "",
        "## Reading - %s" % reading["title"],
        "",
    ]
    if who:
        lines += [who, ""]
    link = reading.get("url") or ""
    if link:
        label = "DOI" if reading.get("doi") else "Source"
        lines += ["[%s](%s)" % (label, link), ""]
    return "\n".join(lines)


def group_marks(items):
    """One selection is one highlight, even when it crossed two blocks.

    EH, 2026-08-30: "A single selection shouldn't be the single note or single
    highlight. Even if it's two different sections, you can separate them in
    the note, let's say with a dash between the two items, but they should
    nevertheless be considered one note and one highlight."

    The store keeps one item per block, because painting and re-anchoring both
    work inside ONE block; members of one selection share a group id `g`. This
    folds them back for anything that PRESENTS them: text joined in document
    order with " - ", the head's note, the head's colour.

    🔴 An item with no `g` is a group of one, so every mark made before
    2026-08-30 folds to exactly itself and nothing migrates.

    🔴 This is the Python twin of `groupMarks` in `reader/shell.html`, and the
    two are pinned against each other by `test_mark_groups.py`. They exist
    separately only because one of them has to run in a browser.

    🔴 **ORPHANS ARE DECIDED HERE, INSIDE THE FOLD, and that is the whole of the
    2026-08-30 fix.** Publishing used to filter orphaned items out BEFORE folding
    them, so a group whose FIRST block lost its anchor lost its head, and with the
    head went the group's note. Not shortened, not flagged: absent, in the vault,
    days later, while the notebook still looked correct. QA proved it end to end
    (a two-cell highlight whose first cell was orphaned published as the second
    cell's words with an empty note).

    A group survives while ANY part still knows where it is:

      - dropped only when EVERY part is orphaned;
      - the head for anchoring is the first SURVIVING part, so `b`/`s` point at a
        block that still exists;
      - the note and the colour belong to the GROUP and survive whichever part
        was lost;
      - the text joins every part, orphaned or not, because the words EH
        highlighted are still the words he highlighted.

    That is also why the filter in front of this is gone: while it existed, the
    two implementations were structurally incapable of being compared on the one
    case where they disagreed."""
    ordered = sorted(items, key=lambda i: (i.get("b", 0), i.get("s", 0)))
    out, by_g = [], {}
    for it in ordered:
        g = it.get("g")
        if not g:
            out.append({"head": it, "parts": [it]})
            continue
        if g in by_g:
            by_g[g]["parts"].append(it)
            continue
        by_g[g] = {"head": it, "parts": [it]}
        out.append(by_g[g])
    folded = []
    for grp in out:
        parts = grp["parts"]
        alive = [p for p in parts if not p.get("orphan")]
        # The first part that still has an anchor: everything positional comes
        # from it, because the original head may be the one that was lost. With
        # nothing left, the first part still carries the words and the note, and
        # the group is flagged rather than dropped.
        head = alive[0] if alive else parts[0]
        texts = [" ".join(str(p.get("t") or "").split()) for p in parts]
        fold = dict(head)
        fold["t"] = " - ".join(t for t in texts if t)
        fold["ids"] = [p.get("id") for p in parts]
        # The note and the colour are the GROUP's, not the surviving part's.
        # `consolidateGroup` puts a group's note on its first member, which is
        # exactly the member most likely to be the one that orphaned.
        #
        # 🔴 The ANCHOR's own note wins, and only then any other part's. The
        # panel addresses a group by its anchor's id, so a note edited after one
        # part orphaned is written to the anchor; reading "the first part with a
        # note" in document order would find the orphan's OLD note instead and
        # quietly undo the edit. Falling back is what rescues the note when the
        # head is the part that was lost.
        note = str(head.get("n") or "").strip()
        if not note:
            note = next((str(p.get("n") or "") for p in parts
                         if str(p.get("n") or "").strip()), "")
        if note:
            fold["n"] = note
        colour = head.get("c") or next((p.get("c") for p in parts if p.get("c")), None)
        if colour:
            fold["c"] = colour
        # 🔴 Flagged, never dropped, and the two are not the same thing. The
        # BROWSER has to keep a fully-orphaned group so the panel can show it as
        # broken and EH can fix it; the PUBLISHER has to leave it out of the
        # vault. Folding identically and letting each side apply its own policy
        # afterwards is what makes the two implementations comparable at all,
        # and comparability is the entire job of `TheTwoGroupersAgree`.
        fold["orphan"] = not alive
        folded.append(fold)
    return folded


def reading_marks_md(items, palette):
    """The fenced body of a reading note, from its highlights.

    🔴 Bucketed by the course PALETTE, not by three colour literals. The
    client-side builder (buildVaultMd in shell.html) buckets only k, q and d
    and silently drops custom colours, which is a logged defect; this publisher
    must not inherit it. Bucket order is palette order, labels are the
    palette's own labels, and the define bucket keeps the vault's term:::
    syntax so the harvest finds it."""
    by_colour = {}
    # Folded into groups FIRST, so a selection that crossed two table cells
    # publishes one bullet rather than two. A group is one colour, so bucketing
    # the folds by colour is the same operation it always was.
    #
    # 🔴 And orphans are dropped AFTER the fold, never before it. Filtering
    # first is the 2026-08-30 defect: it could take the head off a group whose
    # other half was perfectly anchored, and the note went with the head,
    # silently, into the vault. A group is skipped here only when the fold says
    # every one of its parts lost its anchor.
    for it in group_marks(items):
        if it.get("orphan"):
            continue
        by_colour.setdefault(str(it.get("c") or "k"), []).append(it)

    lines = []
    seen = set()
    entries = [e for e in palette if not e.get("retired")]
    entries += [{"id": c, "label": "Marked (%s)" % c}
                for c in by_colour if c not in {e["id"] for e in entries}]
    for entry in entries:
        cid = entry["id"]
        rows = by_colour.get(cid)
        if not rows or cid in seen:
            continue
        seen.add(cid)
        if cid == "d":
            lines += ["**Cards to make**", ""]
            for it in rows:
                lines += [" ".join(str(it.get("t") or "").split()) + ":::", ""]
            continue
        lines += ["**%s**" % str(entry.get("label") or cid), ""]
        for it in rows:
            # The {kind} carries the NAME, so meaning survives colour changes
            # and the line being moved out of its section (EH, 2026-08-23).
            lines.append("- ==%s== {%s}"
                         % (" ".join(str(it.get("t") or "").split()),
                            str(entry.get("label") or cid).lower()))
            note = " ".join(str(it.get("n") or "").split())
            if note:
                lines.append("    - %s" % note)
        lines.append("")
    return "\n".join(lines).rstrip()


def publish_readings_vault(cfg):
    """One vault note per reading, from the marks on the READINGS document.

    Called after a READINGS marks save. Shares every safety with write_vault:
    the Courses parent must exist, containment and symlinks are checked, a
    daily .bak is kept, and the fenced block is the only region touched, so
    anything EH writes in a reading note outside the fence survives. A
    reading whose marks are all gone has its fence removed rather than left
    standing empty.

    Free page notes (the notes panel, not attached to a highlight) are NOT
    published: they carry no block, so nothing can say which reading they are
    about. Recorded in PROJECT-NOTES."""
    if not vault_on(cfg):
        return {"ok": False, "skipped": "vault publishing is off"}
    courses = cfg["vault_courses"]
    if not courses.parent.is_dir():
        return {"ok": False, "skipped": "vault not present at %s" % courses.parent}

    got = readings_content(cfg, with_map=True)
    text, blockmap = got if isinstance(got, tuple) else (None, [])
    if not blockmap:
        return {"ok": False, "skipped": "no readings"}
    marks = read_json_sidecar(sidecar_path(cfg, "READINGS", "marks"), {})
    # 🔴 The orphan filter that used to live HERE is gone, on purpose. It ran
    # before `group_marks` and so could take the head off a group that was still
    # perfectly anchored by its other half, silently dropping the note with it.
    # `group_marks` decides it now, with the whole group in view. Never put a
    # filter back in front of the fold: the two implementations cannot be
    # compared on a case only one of them is allowed to see.
    items = list(marks.get("items") or [])
    palette = read_settings(cfg).get("palette") or []

    written, removed = [], []
    for r in blockmap:
        mine = [i for i in items if r["start"] <= int(i.get("b", -1)) < r["end"]]
        name = vault_reading_filename(cfg.get("class_name") or
                                      cfg.get("module_name") or "", r["title"])
        target = courses / name
        resolved = target.resolve()
        if courses.resolve() not in resolved.parents:
            continue
        if target.is_symlink():
            continue
        if not mine:
            if target.exists():
                existing = target.read_text(encoding="utf-8")
                out = splice_block(existing, r["id"], None)
                if out != existing:
                    with WRITE_LOCK:
                        # Removing the fence is the destructive direction, so it
                        # takes the daily backup exactly as a rewrite does. The
                        # first version only backed up on the write path, which
                        # made deletion the one branch with no undo.
                        bak = target.with_suffix(".md.%s.bak"
                                                 % datetime.now().strftime("%Y%m%d"))
                        if not bak.exists():
                            bak.write_text(existing, encoding="utf-8")
                        tmp = target.with_suffix(".md.tmp")
                        tmp.write_text(out, encoding="utf-8")
                        os.replace(tmp, target)
                    removed.append(name)
            continue
        body = reading_marks_md(mine, palette)
        if not body:
            continue
        courses.mkdir(parents=True, exist_ok=True)
        with WRITE_LOCK:
            if target.exists():
                existing = target.read_text(encoding="utf-8")
                bak = target.with_suffix(".md.%s.bak" % datetime.now().strftime("%Y%m%d"))
                if not bak.exists():
                    bak.write_text(existing, encoding="utf-8")
            else:
                existing = new_reading_header(cfg, r)
            out = splice_block(existing, r["id"], body)
            if not out.endswith("\n"):
                out += "\n"
            tmp = target.with_suffix(".md.tmp")
            tmp.write_text(out, encoding="utf-8")
            os.replace(tmp, target)
        written.append(name)
    return {"ok": True, "written": written, "removed": removed}


def part_of(doc_id):
    m = re.search(r"-P(\d+)$", doc_id)
    return int(m.group(1)) if m else 999


def splice_block(existing, doc_id, body):
    """Replace this part's marker-to-marker region, or insert it in part order.

    Everything outside the markers survives untouched. That is the whole point:
    the file is shared with EH, and with four other parts.
    """
    start = BLOCK_START.format(doc=doc_id)
    end = BLOCK_END.format(doc=doc_id)

    # Nothing marked in that part: take its block out rather than leaving a
    # heading and a lecture link standing in for content that is not there.
    if body is None:
        i = existing.find(start)
        if i == -1:
            return existing
        j = existing.find(end, i)
        if j == -1:
            return existing[:i].rstrip("\n") + "\n"
        rest = existing[j + len(end):].lstrip("\n")
        head = existing[:i].rstrip("\n")
        return head + "\n" + ("\n" + rest if rest else "")

    block = "%s\n%s\n%s" % (start, body.rstrip(), end)

    i = existing.find(start)
    if i != -1:
        j = existing.find(end, i)
        if j != -1:
            return existing[:i] + block + existing[j + len(end):]
        # A start with no end means a half-written file. Treat the rest as lost
        # rather than duplicating the block; the .bak has the original.
        return existing[:i] + block + "\n"

    # Not present yet. Insert before the first block belonging to a later part,
    # so reading part 5 before part 2 still leaves the note in reading order.
    # The "### Part N" heading always lives INSIDE the markers, so moving a
    # block never orphans a heading.
    mine = part_of(doc_id)
    for m in ANY_START.finditer(existing):
        if part_of(m.group(1)) > mine:
            head = existing[:m.start()].rstrip("\n")
            return head + "\n\n" + block + "\n\n" + existing[m.start():]

    return existing.rstrip("\n") + "\n\n" + block + "\n"


def vault_on(cfg):
    """The preference if one has been set, otherwise the machine's default."""
    try:
        return bool(read_settings(cfg)["vaultEnabled"])
    except Exception:
        return bool(cfg.get("vault_enabled", True))


def vault_display_name(cfg):
    """What the reader calls the vault, taken from the path it writes into.

    🔴 The badge used to carry a HARDCODED vault name, which was the name of
    ONE person's Obsidian vault. On anybody else's machine that is a proper noun
    they have never seen, attached to a feature they may have just switched on,
    and the reader looked like it had been built for somebody else because it
    had.

    `vault_courses` points at `<vault>/Courses`, so the vault's own folder name
    is its parent, and deriving it means EH still reads his own vault's name
    while a recipient reads whatever they called theirs. "Vault" is the fallback
    for a path shaped in a way this cannot read."""
    try:
        name = Path(cfg.get("vault_courses") or "").parent.name.strip()
    except (TypeError, ValueError):
        name = ""
    return name or "Vault"


def write_vault(cfg, payload):
    if not vault_on(cfg):
        # Refused here as well as hidden in the reader, because a page loaded
        # before the setting changed would otherwise keep pushing.
        raise ValueError("vault publishing is off for this install")
    doc_id = payload["doc"]
    if not DOC_ID_RE.match(doc_id):
        raise ValueError("bad doc id")

    for key in ("topicTitle", "week", "topicNo"):
        if key not in payload:
            raise ValueError("missing %s" % key)
    week = str(payload["week"])
    topic_no = str(payload["topicNo"])
    if not week.isdigit() or not topic_no.isdigit():
        raise ValueError("week and topicNo must be numeric")
    topic_title = str(payload["topicTitle"]).strip()
    if not SAFE_FIELD_RE.match(topic_title):
        raise ValueError("topic title failed the charset check")

    body = str(payload.get("markdown", "")).strip()
    if len(body) > 400_000:
        raise ValueError("body too large")

    courses = cfg["vault_courses"]
    # Create Courses/ inside a vault that exists; never create the vault itself.
    # A wrong path would otherwise grow a convincing empty copy of the vault,
    # and every highlight would land in it unnoticed.
    if not courses.parent.is_dir():
        raise ValueError(
            "the vault is not at %s, so nothing was written. Fix vault_courses "
            "in ~/.kcl-study/config.json." % courses.parent)

    # The same fallback chain `vault_reading_filename` is called with above, so
    # a course names its topic notes and its reading notes alike.
    name = vault_filename(cfg.get("class_name") or cfg.get("module_name") or "",
                          week, topic_no, topic_title)
    target = (courses / name)

    courses.mkdir(parents=True, exist_ok=True)
    resolved = target.resolve()
    if courses.resolve() not in resolved.parents:
        raise ValueError("refusing to write outside the Courses directory")
    if target.is_symlink():
        raise ValueError("refusing to write through a symlink")

    # A part with no marks at all contributes nothing. If the note does not
    # exist yet, there is nothing to create either.
    empty = not payload.get("hasMarks", True)
    if empty and not target.exists():
        return {"ok": True, "file": str(target), "bytes": 0, "skipped": "nothing to write"}

    with WRITE_LOCK:
        if target.exists():
            existing = target.read_text(encoding="utf-8")
            # Back up before a substantial rewrite, per the house rule. One
            # per day is enough because of WHAT it guards: this code rewriting a
            # note badly. The first copy of the day holds the state before
            # today's writes, and a second would only preserve damage already
            # done. A synced vault keeps its own history on top of that, which
            # is a bonus and deliberately not the reason: a recipient's vault
            # may not be synced at all.
            bak = target.with_suffix(".md.%s.bak" % datetime.now().strftime("%Y%m%d"))
            if not bak.exists():
                bak.write_text(existing, encoding="utf-8")
        else:
            existing = new_note_header(cfg, topic_title)

        if empty:
            body = None
        else:
            # The heading is re-emitted every time because the block is replaced
            # wholesale. Testing "is it already in the file" loses it on the
            # second write, since the copy it finds is inside the block being cut.
            heading = "### Part %d" % part_of(doc_id)
            if not body.startswith(heading):
                body = heading + "\n\n" + body

        out = splice_block(existing, doc_id, body)
        if not out.endswith("\n"):
            out += "\n"
        tmp = target.with_suffix(".md.tmp")
        tmp.write_text(out, encoding="utf-8")
        os.replace(tmp, target)

    return {"ok": True, "file": str(target), "bytes": len(out)}


# --------------------------------------------------------------------------
# lookup: glossary, then MeSH, then Wikipedia. No key, no cost.
# --------------------------------------------------------------------------

# Sentence punctuation and quotes: never part of what was meant, either end.
NORM_TRIM = ".,;:!?\"'‘’“”"


def norm_term(q):
    """What a selection means, with the punctuation that came with it removed.

    🔴 This used to be `.strip(".,;:()[]\"'")`, and that was wrong in a way that
    took until 2026-08-15 to notice: `strip` takes characters off BOTH ends
    independently, so "Cognitive behavioural therapy (CBT)" lost its closing
    bracket and kept its opening one. The result matched no glossary key, so the
    term fell through to R28's resolver, which found CBT and Cognitive
    behavioural therapy and asked "Which one?" about a name and its own
    abbreviation. **14 of the 358 glossary keys were unreachable this way**, and
    they were the abbreviation keys: CBT, IPT, MBCT, SNP, FMT, tDCS, SCFAs, and
    the suprachiasmatic nucleus, which is how it was found.

    Brackets now come off as a PAIR when they wrap the whole selection, and a
    lone one comes off only when there is nothing in the string it could match.
    """
    q = re.sub(r"\s+", " ", (q or "")).strip().strip(NORM_TRIM).strip()
    # "(amygdala)" is the amygdala; "therapy (CBT)" is not "therapy (CBT".
    while len(q) >= 2 and ((q[0] == "(" and q[-1] == ")") or (q[0] == "[" and q[-1] == "]")):
        q = q[1:-1].strip().strip(NORM_TRIM).strip()
    # A closer with nothing to close is punctuation the selection swept up.
    if q.endswith(")") and q.count("(") < q.count(")"):
        q = q[:-1].rstrip()
    if q.endswith("]") and q.count("[") < q.count("]"):
        q = q[:-1].rstrip()
    if q.startswith("(") and q.count(")") < q.count("("):
        q = q[1:].lstrip()
    if q.startswith("[") and q.count("]") < q.count("["):
        q = q[1:].lstrip()
    return q


def glossary_path(cfg):
    return cfg["notes_dir"] / "glossary.json"


# 🔴 Keyed by the course's own glossary PATH, not by mtime alone.
#
# It used to be one slot for the whole process: `{"mtime": ..., "data": ...}`,
# invalidated by comparing the file's mtime against the cached number. The course
# the data came from was not part of the key, which has two consequences and the
# quieter one is the dangerous one.
#
# If two courses' `glossary.json` ever carry the SAME mtime, the second course's
# lookups are answered out of the first course's glossary, silently and with a
# picture beside them that makes the answer look authoritative. Equal mtimes are
# not exotic: a kit install writes its tree in one go, an unzip preserves stored
# timestamps, and a restore from `backups/` can hand two files the same second.
#
# The louder consequence is only waste: on a two-course install every alternation
# between courses re-read and re-parsed the file, so the cache did nothing for
# exactly the setup this project ships.
_glossary_cache = {}

# R28. What a person selects on the page is almost never the glossary's own key.
# Measured across the 29 lessons: of the anatomy phrases used four or more times,
# the old exact match found 11 and missed 36, and **"the amygdala" was one of the
# misses**, used 14 times. A determiner was enough to break it.
#
# These two strip the parts of a selection that carry no meaning of their own. A
# trailing "areas" or "regions" is the module saying "roughly here", which is
# still the same structure: "limbic areas" and "limbic" want the same answer.
GLOSS_LEAD = re.compile(
    r"^(?:the|a|an|this|that|these|those|its|their|his|her|both|all|each|every"
    r"|some|other|same|whole|entire)\s+", re.I)
GLOSS_TAIL = re.compile(r"\s+(?:areas?|regions?|structures?|parts?)$", re.I)

# 🔴 Words that name a *category* rather than a thing, and must never resolve on
# their own. Found by eye, scoring every match the rules made across the 29
# lessons: "the network" was resolving to "Network meta-analysis", a statistics
# term, purely because that key starts with the word. Selecting a category is not
# enough information to answer with, and a confident wrong answer is worse here
# than nothing, because the picture makes it look authoritative.
#
# "limbic" is deliberately NOT in this list, though "cortical" is: limbic names
# one system, while cortical describes any cortex there is.
GLOSS_GENERIC = {
    "network", "networks", "system", "systems", "cortex", "cortical", "cortices",
    "nucleus", "nuclei", "region", "regions", "area", "areas", "structure",
    "structures", "pathway", "pathways", "circuit", "circuits", "brain", "brains",
    "neural", "matter", "lobe", "lobes", "gyrus", "gyri", "tract", "tracts",
}


def gloss_forms(term):
    """Every spelling of a selection worth trying, longest intent first.

    Peeling loops because "all the other limbic areas" needs three passes, and
    the plural forms come last so "nuclei" never wins over an exact "nucleus".
    """
    # 🔴 The raw form comes FIRST, and leaving it out was a real defect found on
    # 2026-08-15. The strip below exists to clean punctuation off a selection
    # ("the amygdala." to "the amygdala"), which is right, but it took the
    # closing bracket off "Suprachiasmatic nucleus (SCN)" and left the opening
    # one, so the result matched no key and the term fell through to R28's
    # resolver, which found SCN and Suprachiasmatic nucleus and asked "Which
    # one?" about two names for the same structure. **14 of the 358 glossary
    # keys could not be matched by their own exact text**, and they are the
    # abbreviation keys: CBT, IPT, MBCT, SNP, FMT, tDCS, SCFAs and the rest. An
    # exact key must always win before anything is inferred.
    raw = re.sub(r"\s+", " ", (term or "").lower()).strip()
    t = raw.strip(" .,;:()[]\"'‘’“”")
    if not t:
        return []
    forms = [raw] if raw else []
    if t != raw:
        forms.append(t)
    prev = None
    while prev != t:
        prev = t
        t = GLOSS_TAIL.sub("", GLOSS_LEAD.sub("", t)).strip()
        if t and t != prev:
            forms.append(t)
    for f in list(forms):
        for alt in (f.rstrip("s"), f + "s"):
            if alt and alt not in forms:
                forms.append(alt)
    return forms


def resolve_region(anat_keys, forms):
    """A selection that is not a name, matched against the names we have.

    Three rules, tried in order, and **each one only fires when exactly one name
    matches**. "cingulate cortex" sits inside both the anterior and the posterior
    one, and guessing between them would put the wrong picture on the page, so an
    ambiguous match returns the candidates instead and lets him choose.

    Returns (key, how, alternatives).
    """
    for kind, test in (
            # "anterior cingulate" is the start of "anterior cingulate cortex"
            ("completed", lambda k, f: k.startswith(f + " ")),
            # "memory network" is the end of "autobiographical memory network"
            ("completed", lambda k, f: k.endswith(" " + f)),
            # a whole name sitting inside a longer selection
            ("contained", lambda k, f: re.search(r"\b" + re.escape(k) + r"\b", f) is not None)):
        for f in forms:
            if len(f) < 4:            # "the", "an", a stray letter: never a region
                continue
            if " " not in f and f in GLOSS_GENERIC:
                continue              # a category on its own says too little
            hits = sorted({anat_keys[k] for k in anat_keys if test(k, f)})
            if len(hits) == 1:
                return hits[0], kind, []
            if len(hits) > 1:
                return None, "ambiguous", hits
    return None, None, []


def glossary_lookup(cfg, term):
    path = glossary_path(cfg)
    if not path.exists():
        return None
    try:
        # resolve() so two spellings of one course (a symlink, a relative cfg)
        # share a slot rather than quietly keeping two copies of the same file.
        key = str(path.resolve())
        mtime = path.stat().st_mtime
        slot = _glossary_cache.get(key)
        if slot is None or slot["mtime"] != mtime:
            slot = {"mtime": mtime,
                    "data": json.loads(path.read_text(encoding="utf-8"))}
            _glossary_cache[key] = slot
    except (OSError, ValueError):
        return None

    data = slot["data"]
    if isinstance(data, dict) and "terms" in data:
        data = data["terms"]
    keys = {k.lower(): k for k in data}
    forms = gloss_forms(term)
    matched = None
    how = "exact"
    for cand in forms:
        if cand in keys:
            matched = keys[cand]
            break

    # R28. Nothing is named that; see whether it describes something that is.
    # Every glossary key is a candidate, not only the regions: "memory network"
    # should still reach the autobiographical memory network, which carries no
    # picture. The safety is not the anatomy flag, it is the rule that a match
    # only counts when exactly one key answers to the phrase.
    if matched is None:
        all_keys = {k.lower(): k for k in data}
        found, kind, others = resolve_region(all_keys, forms)
        if found:
            matched, how = found, kind
        elif others:
            return {"source": "This module", "title": "", "text": "", "extra": [],
                    "url": "", "ambiguous": True, "suggest": others}

    if matched is None:
        return None

    entry = data[matched]
    if isinstance(entry, str):
        entry = {"text": entry}
    hit = {
        "source": "This module",
        "title": matched,
        "text": entry.get("text") or entry.get("short") or "",
        "extra": entry.get("seeAlso") or [],
        "url": "",
    }
    # Say so when the answer is not what was selected, so a resolved match never
    # reads as though the page had used that exact wording.
    if how != "exact":
        hit["resolved"] = True
    # R14: a brain region carries two extra facts. `anatomy` is the gate, so a
    # looked-up eponym never gets a portrait of the person it is named after,
    # and `wiki` names the article explicitly rather than letting a search
    # guess, because "Insula" and "Striatum" both have readings that are not
    # brain structures.
    if entry.get("anatomy"):
        hit["anatomy"] = True
        hit["wiki"] = entry.get("wiki") or matched
        # 2026-08-15. `pic` names a file to use INSTEAD of the article's lead
        # image, and `picOf` says what that picture actually shows when it is not
        # the term itself. See region_image() for why both exist.
        if entry.get("pic"):
            hit["pic"] = entry["pic"]
        if entry.get("picOf"):
            hit["picOf"] = entry["picOf"]
    return hit


def fetch_json(url, timeout, retries=2, cfg=None):
    """NCBI allows three requests a second without a key, and two clicks in
    quick succession is four. A short backoff turns a dropped definition into
    a slightly slower one."""
    req = urllib.request.Request(url, headers={"User-Agent": user_agent(cfg)})
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as err:
            if err.code in (429, 503) and attempt < retries:
                time.sleep(0.5 * (attempt + 1))
                continue
            raise
        except (urllib.error.URLError, TimeoutError):
            if attempt < retries:
                time.sleep(0.3)
                continue
            raise


def mesh_lookup(term, timeout):
    """MeSH scope notes: authoritative biomedical definitions, free, no key.

    Relevance ranking alone is not good enough here. Searching "amygdala"
    returns the corticomedial nuclear complex first, which is a part of the
    amygdala and not an answer to the question asked. So take several hits and
    prefer the record that actually carries the term as one of its own names.
    """
    base = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
    wanted = term.lower().strip()

    def summaries(ids):
        if not ids:
            return {}, []
        data = fetch_json(
            base + "esummary.fcgi?db=mesh&retmode=json&id=" + ",".join(ids), timeout)
        return data.get("result", {}), ids

    def score_of(rec):
        names = [str(n).lower() for n in (rec.get("ds_meshterms") or [])]
        if wanted in names:
            return 3
        if any(n.startswith(wanted) or wanted.startswith(n) for n in names):
            return 2
        if any(wanted in n for n in names):
            return 1
        return 0

    def search(query, retmax):
        found = fetch_json(
            base + "esearch.fcgi?db=mesh&retmode=json&retmax=%d&term=%s"
            % (retmax, urllib.parse.quote(query)), timeout)
        return found.get("esearchresult", {}).get("idlist") or []

    # The [MeSH Terms] field resolves the concept itself, including synonyms:
    # cortisol lands on Hydrocortisone. A bare relevance search does not, and
    # answers "amygdala" with the corticomedial nuclear complex.
    best, best_score, best_id = None, -1, None
    result, ids = summaries(search(term + "[MeSH Terms]", 3))
    for uid in ids:
        rec = result.get(uid)
        if isinstance(rec, dict) and (rec.get("ds_scopenote") or "").strip():
            best, best_score, best_id = rec, 9, uid
            break

    if best is None:
        result, ids = summaries(search(term, 5))
        for uid in ids:
            rec = result.get(uid)
            if not isinstance(rec, dict) or not (rec.get("ds_scopenote") or "").strip():
                continue
            s = score_of(rec)
            # A zero means the record shares no name with the query. Returning
            # it is worse than returning nothing: Wikipedia will cover the term
            # and a confidently wrong definition would not be questioned.
            if s >= 1 and s > best_score:
                best, best_score, best_id = rec, s, uid

    if best is None:
        return None
    terms = best.get("ds_meshterms") or []
    return {
        "source": "MeSH",
        "title": terms[0] if terms else term,
        "text": (best.get("ds_scopenote") or "").strip(),
        "extra": terms[1:6],
        "url": "https://www.ncbi.nlm.nih.gov/mesh/%s" % best_id,
    }


def wikipedia_lookup(term, timeout):
    slug = urllib.parse.quote(term.replace(" ", "_"), safe="")
    try:
        data = fetch_json(
            "https://en.wikipedia.org/api/rest_v1/page/summary/" + slug, timeout)
    except Exception:
        search = fetch_json(
            "https://en.wikipedia.org/w/api.php?action=query&list=search&format=json"
            "&srlimit=1&srsearch=" + urllib.parse.quote(term), timeout)
        hits = search.get("query", {}).get("search") or []
        if not hits:
            return None
        slug = urllib.parse.quote(hits[0]["title"].replace(" ", "_"), safe="")
        data = fetch_json(
            "https://en.wikipedia.org/api/rest_v1/page/summary/" + slug, timeout)

    if data.get("type") == "disambiguation":
        return None
    text = (data.get("extract") or "").strip()
    if not text:
        return None
    return {
        "source": "Wikipedia",
        "title": data.get("title") or term,
        "text": text,
        "extra": [],
        "url": (data.get("content_urls", {}).get("desktop", {}) or {}).get("page", ""),
    }


def region_image(title, timeout, pic=None, pic_of=None):
    """The picture that goes with a brain structure.

    By default this is the LEAD IMAGE of the Wikipedia article, which for most
    brain structures is exactly the thing wanted: a brain with the structure
    picked out in colour.

    🔴 Not for all of them, and that is what `pic` is for. EH reported on
    2026-08-15 that he had opened one that "kind of didn't" show a region, and
    all 31 were then opened and looked at one at a time. Five were wrong, because
    a lead image is chosen by Wikipedia's editors for that article and not for
    this purpose: `Limbic system` led with the BACK COVER OF A BOOK, and Corpus
    callosum, Insular cortex and Raphe nuclei all led with 1918 Gray's engravings
    that label the structure in four-point italic and highlight nothing. Those
    entries now name a file explicitly in the glossary, and the lead image is
    used only where it was checked and found good.

    `pic_of` is the other half of being honest. Ventral striatum has no article
    and no picture of its own, so it borrows the striatum's; the caption says
    which structure the picture is of rather than letting the reader assume it is
    the term they selected.

    Returns None rather than raising: a missing picture should cost a definition
    nothing.

    🔴 SINCE 2026-09-03 THIS IS THE FALLBACK, not the first answer. `region_pictures`
    asks the local pack first and only reaches here for a structure the pack does
    not cover. Everything below is unchanged and still carries the whole corpus of
    special cases, because a pack that covers 31 of 31 terms in one course covers
    nothing at all in a course nobody has built plates for."""
    slug = urllib.parse.quote(title.replace(" ", "_"), safe="")
    try:
        data = fetch_json(
            "https://en.wikipedia.org/api/rest_v1/page/summary/" + slug, timeout)
    except Exception:
        return None
    if data.get("type") == "disambiguation":
        return None

    page = (data.get("content_urls", {}).get("desktop", {}) or {}).get("page", "")
    shows = pic_of or data.get("title") or title

    if pic:
        # A named file, asked for at a width rather than at its own size: several
        # of these are multi-megabyte originals, and one is a 9MB animation.
        try:
            info = fetch_json(
                "https://en.wikipedia.org/w/api.php?action=query&prop=imageinfo"
                "&iiprop=url&iiurlwidth=440&format=json&titles="
                + urllib.parse.quote(pic, safe=""), timeout)
        except Exception:
            info = {}
        for p in (info.get("query", {}).get("pages", {}) or {}).values():
            ii = (p.get("imageinfo") or [{}])[0]
            src = ii.get("thumburl") or ii.get("url")
            if src:
                return {"src": src, "width": ii.get("thumbwidth"),
                        "height": ii.get("thumbheight"), "page": page,
                        "credit": "Wikipedia", "shows": shows}
        # Falling through to the lead image would put back the picture this
        # override exists to replace, so a broken override shows nothing.
        return None

    # The thumbnail is a few hundred pixels wide, which is the right size for a
    # popover; originalimage can be several megabytes.
    thumb = data.get("thumbnail") or {}
    src = thumb.get("source")
    if not src:
        return None
    return {
        "src": src,
        "width": thumb.get("width"),
        "height": thumb.get("height"),
        "page": page,
        "credit": "Wikipedia",
        "shows": shows,
    }


def region_pictures(title, timeout, pic=None, pic_of=None, wiki=None):
    """The MODULE's own pictures for a brain structure. **Not the pack's.**

    🔴🔴 THE PACK IS NO LONGER CONSULTED HERE, and that is EH's ruling rather
    than a refactor: *"Pack pictures should display in the pack section and
    module pictures in the module section. It should not be one or the other."*
    **The pack has its own card now** (`pack_lookup`), so a module card that
    also sourced from the pack showed the reader the same plates twice under two
    headings.

    ⚠️ **THE TWO HALVES SHIPPED TOGETHER AND HAD TO.** Until this line changed,
    30 of the 31 anatomy terms in the affective-disorders course were covered by
    the pack, and its module card was serving **the pack's own files** (verified
    identical by URL), so "both cards show their own" could not be true while
    this function reached for the pack. **Removing the duplication guard without
    this would have shown every one of them twice.**

    🟢 What remains genuinely module-owned is the Wikipedia fallback and the
    glossary's own `pic` / `picOf` overrides. 🔴 **EH has authorised deleting
    those** (*"our current module associated pictures are crap and can be
    deleted"*), which is a data change and not this function's business.

    🔴 A LIST, ALWAYS, so the caller has one shape to hold. The network path
    yields at most one.
    """
    one = region_image(wiki or title, timeout, pic=pic, pic_of=pic_of)
    return [one] if one else []


def cache_get(cfg, key):
    path = cfg["cache_dir"] / "lookup" / (hashlib.sha1(key.encode()).hexdigest() + ".json")
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            return None
    return None


def cache_put(cfg, key, value):
    path = cfg["cache_dir"] / "lookup" / (hashlib.sha1(key.encode()).hexdigest() + ".json")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")
    except OSError:
        pass


PACK_SOURCE = "Brain regions"
# 🔴 WHAT A SOURCE **IS**, sent beside what it is CALLED, because the client had
# only the display name to sort on and a name is a guess about the future. The
# rank reserved a pack's slot by testing for the literal word "pack"; the pack
# that shipped calls itself "Brain regions" and fell to the catch-all, which
# sorts LAST -- EH saw that on his own screen. ⚠️ THE REPAIR WAS TO THIS PACK'S
# NAME, so the same defect returns the day a second pack is named after its own
# subject (`Neurotransmitters`, `Receptor families`), which is exactly what
# `plans/10-knowledge-packs.md` proposes. **One structural field lets the client
# ask what a source IS, and a renamed or second pack then sorts right with no
# client change at all.**
PACK_KIND = "pack"


def pack_lookup(term, wiki=None, with_plates=True):
    """The region pack answering for itself, with no course glossary involved.

    🔴🔴 **THIS IS THE WHOLE POINT OF THE ENTRY, and the manager's own
    correction is why.** The obvious change was to move the pack lookup outside
    the `anatomy` gate, and that would have fixed nothing for the term EH
    actually tried: `do_lookup` only reaches the picture block inside `if
    local:`, and **the course he was reading has no `Amygdala` entry at all** --
    its nearest key is `Basolateral amygdala` -- so `local` is falsy and the
    gate is never reached. Measured against both courses' glossaries, not
    reasoned: one has the key and one does not.

    ⚠️ **The two course codes are deliberately not written here.** The kit's
    personal-data audit refuses a module code in a shipped file, calling it
    somebody's enrolment, and it caught this docstring quoting both. It was
    right to: this file ships.

    🟢 **So the pack is a SOURCE, not a better-gated branch of somebody else's
    lookup.** A term gets its plates and its definition with no glossary entry,
    no `anatomy` flag and no tagging, ever.

    🟢 **NO CACHE, and that is a departure from the entry's step 5 rather than an
    oversight.** It says to cache this the way the picture path is cached. That
    cache exists because `region_image` goes to Wikipedia; **nothing here leaves
    the process.** `regionpack.load()` and `definitions()` are read once per
    process (an ABSENT pack is looked for again, so one installed under a
    running server is seen on the next lookup), so a disk cache would add a
    syscall, a staleness class and a second thing to invalidate when the pack
    is rebuilt, in exchange for nothing. The
    property step 5 was protecting -- that a rebuilt pack cannot serve stale
    plates -- is kept by construction here, because there is nothing to go stale.

    ⚠️ **`with_plates` is FALSE when something else on the page is already
    showing them.** The course glossary's own picture path already prefers pack
    plates for an `anatomy` term, so a covered region in a tagged course would
    otherwise show the same four plates twice, in two cards, under two headings.
    """
    entry = regionpack.definition(term, wiki)
    if not entry:
        return None
    hit = {"source": PACK_SOURCE, "kind": PACK_KIND, "title": entry["name"],
           "text": entry["text"], "extra": entry["aliases"], "url": ""}
    if with_plates:
        shots = regionpack.plates(entry["name"])
        if shots:
            # Both, for the reason the glossary path gives: a reader running an
            # older layer draws `image` and knows nothing about paging.
            hit["image"] = shots[0]
            hit["images"] = shots
    return hit


def do_lookup(cfg, term):
    term = norm_term(term)
    if not term or len(term) > 120:
        return {"term": term, "sources": []}

    sources = []
    suggest = []
    local = glossary_lookup(cfg, term)
    # R28. Several regions answer to this phrase and picking one would be a
    # guess. It travels beside the sources rather than as one of them, because
    # it is a question rather than an answer, and the other lookups still run:
    # MeSH may well have something to say about "cingulate cortex" itself.
    if local and local.get("ambiguous"):
        suggest = local.get("suggest") or []
        local = None
    if local:
        # R14. Cached separately from the definition lookup and under its own key,
        # so that adding pictures does not invalidate every definition already
        # cached, and so a region whose text is edited keeps its picture.
        if local.get("anatomy"):
            # 🔴 The pinned file and the caption are part of the cache key. Without
            # them, correcting a wrong picture in the glossary would leave every
            # machine that had already looked that term up showing the old one
            # until the cache was cleared by hand.
            # 🔴 THE PACK VERSION IS PART OF THE KEY, and it is what makes
            # wiring the pack in safe on a machine that has already cached
            # pictures. Without it, every term looked up before today would go
            # on showing its Wikipedia lead image from cache until somebody
            # cleared it by hand, and a rebuilt pack would serve plates from
            # URLs that no longer exist.
            #
            # 🔴🔴 AND SO IS THE SOURCING RULE (`owns=`), WHICH THE PACK VERSION DOES
            # NOT COVER. Caught live rather than by a test: after EH's ruling made
            # `region_pictures` stop consulting the pack, the affective-disorders
            # course still served **the same five plates in both cards**, because
            # the cached answer was written while this path DID consult the pack
            # and nothing in the key had changed. ⚠️ **Every unit test builds a
            # fresh cache directory, so the suite could not see it and EH's warm
            # cache is exactly where it bites.**
            # 🟢 The rule of thumb this earns: **a cache key must name every input
            # to the answer, and "which source produces it" is an input.** Bump
            # `owns` whenever the ownership of a picture moves.
            key = "img:" + "|".join([
                local["title"].lower(),
                (local.get("wiki") or "").lower(),
                local.get("pic", ""), local.get("picOf", ""),
                "pack=" + (regionpack.version() or "none"),
                "owns=module-only"])
            shots = cache_get(cfg, key)
            # ⚠️ A cache written before this was a list. The key above means one
            # cannot be read any more, so this is a guard against a hand-edited
            # file rather than a migration, and it costs one line.
            if isinstance(shots, dict):
                shots = [shots] if shots else []
            if shots is None:
                shots = region_pictures(local["title"],
                                        cfg["lookup_timeout"],
                                        pic=local.get("pic"),
                                        pic_of=local.get("picOf"),
                                        wiki=local.get("wiki"))
                cache_put(cfg, key, shots)
            if shots:
                # 🔴 BOTH, and `image` stays FIRST-CLASS rather than becoming a
                # legacy alias. A reader running an older layer draws `image`
                # and knows nothing about paging; it must keep working, which is
                # what makes this change additive on a machine where the two
                # halves deploy at different moments.
                local["image"] = shots[0]
                local["images"] = shots

    # 🔴 THE PACK ANSWERS INDEPENDENTLY OF THE COURSE GLOSSARY, which is EH's
    # ask: *"We search across all things... We show everything to the user."*
    # It is not gated on `local`, not gated on `anatomy`, and it does NOT
    # short-circuit: it joins the union and MeSH and Wikipedia still run.
    # ⚠️ It is asked by the TERM the reader typed, and by the glossary's `wiki`
    # name when there is one, which is the same pair and the same order
    # `region_for` uses. A term should not take its plates from one name and its
    # words from another.
    #
    # 🔴 READ OFF `local` RATHER THAN OFF `sources`, AND THAT IS FORCED BY THE
    # ORDER BELOW rather than being a tidy-up. It used to be
    # `any(src.get("images") for src in sources)`, which was exact while `local`
    # was the only thing in the list by now. It is no longer in the list at all,
    # so the old line would read an EMPTY list, come back False, and hand the
    # pack plates on every lookup: the duplication guard would be gone and
    # nothing about the order would look wrong.
    # 🔴🔴 NO DUPLICATION GUARD, AND ITS ABSENCE IS A RULING RATHER THAN AN
    # OVERSIGHT. EH, asked directly: *"Pack pictures should display in the pack
    # section and module pictures in the module section. It should not be one or
    # the other."*
    #
    # 🟢 There WAS a guard, and it was correct for the question it answered:
    # while `region_pictures` also served pack plates, a covered term in a tagged
    # course would have shown the same five pictures twice under two headings.
    # **`region_pictures` no longer consults the pack**, so the two cards cannot
    # hold the same files and the condition the guard existed for is dissolved
    # rather than fixed. ⚠️ **The two changes are one change and must not be
    # separated**: either alone shows every covered term twice.
    #
    # 🟢 It also retires the binary-yield problem QA raised on the guard (which
    # side yields when the two overlap only partly): nobody yields now.
    from_pack = pack_lookup(term, (local or {}).get("wiki"), with_plates=True)

    if from_pack:
        sources.append(from_pack)
    if local:
        sources.append(local)

    cached = cache_get(cfg, term.lower())
    if cached is not None:
        return {"term": term, "sources": sources + cached, "cached": True,
                "suggest": suggest, "failed": []}

    remote = []
    failed = []
    timeout = cfg["lookup_timeout"]
    for fn, name in ((mesh_lookup, "MeSH"), (wikipedia_lookup, "Wikipedia")):
        try:
            hit = fn(term, timeout)
            if hit:
                remote.append(hit)
        except Exception:
            # 🔴 EH, 2026-08-30: "a source that FAILED must look like neither" a
            # source with an answer nor one with no entry. Until now this
            # `continue` erased the difference: a timeout and an absent term
            # produced byte-identical output, and a reader told nothing would
            # conclude the term does not exist in MeSH when MeSH was simply
            # unreachable.
            failed.append(name)
            continue

    # 🔴 AND THE ERASURE WAS PERMANENT, which is the worse half. `cache_put` ran
    # unconditionally, so one network blip wrote "MeSH has nothing for this term"
    # into the cache and every later lookup of that term was served the failure
    # as though it were an answer. Nothing expired it and nothing recorded that
    # it had happened. A failed lookup is now simply not cached, so the next
    # lookup asks again.
    if not failed:
        cache_put(cfg, term.lower(), remote)
    return {"term": term, "sources": sources + remote, "suggest": suggest,
            "failed": failed}


# --------------------------------------------------------------------------
# explain: the Claude Code CLI, so his subscription pays, not an API key
# --------------------------------------------------------------------------

# The pitch of an answer, chosen once and remembered by the page. "explained" is
# the one EH asked for by name: MSc level, but nothing left as jargon.
LEVELS = {
    "plain": (
        "- Pitch it at an intelligent reader with no background in the field. Ordinary words.\n"
        "- Where a technical term is unavoidable, give it in plain language first and name it second.\n"
        "- Analogies are welcome if they are accurate."),
    "explained": (
        "- Pitch it at an MSc student in psychology and neuroscience, but explain EVERY technical\n"
        "  term as it arrives, in a clause or a short parenthesis, without breaking the flow.\n"
        "- Never assume a term is known just because it is standard in the field.\n"
        "- Do not pad: the explanations are inline, not a glossary at the end."),
    "technical": (
        "- Pitch it at an MSc student with solid undergraduate neuroscience and psychology.\n"
        "- Assume the standard vocabulary and do not define terms they already know.\n"
        "- Be precise about mechanisms, and name the evidence where it matters."),
}
DEFAULT_LEVEL = "explained"


def level_rules(name):
    return LEVELS.get(str(name or "").lower(), LEVELS[DEFAULT_LEVEL])


# Only these may reach an argv. A model name is passed to a subprocess, so it is
# an allowlist rather than a validated string.
MODELS = [
    {"id": "claude-opus-5", "label": "Opus 5", "note": "the most capable, and the slowest"},
    {"id": "claude-sonnet-5", "label": "Sonnet 5", "note": "the default: as fast as Haiku here, and better"},
    {"id": "claude-haiku-4-5-20251001", "label": "Haiku 4.5", "note": "cheapest on quota"},
]
MODEL_IDS = {m["id"] for m in MODELS}


# The highlight palette. EH asked on 2026-08-13 for "a set of colors you can
# choose from and add to" with "a key as to what each color denotes", so the
# label is not decoration: it IS the key, and it is why this is stored rather
# than hard-coded. The three built-ins keep the ids k, q and d because every
# mark ever made carries one of them in its `c` field.
#
# `d` is Card. It is deliberately NOT a swatch in the picker and never becomes
# the sticky default, on his instruction, because it writes flashcard syntax
# into the vault and a sticky Card would keep doing that silently.
DEFAULT_PALETTE = [
    {"id": "k", "label": "Highlight", "color": "#C9E4DC", "dark": "#22504A"},
    {"id": "q", "label": "Flag", "color": "#F3DDBE", "dark": "#4B3721"},
    {"id": "d", "label": "Card", "color": "#CBDCEA", "dark": "#22384E", "fixed": True},
]
PALETTE_ID_RE = re.compile(r"^[a-z][a-z0-9]{0,7}\Z")
HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def clean_palette(raw, fallback):
    """Validate a palette from the client. Anything malformed loses the whole
    submission rather than being silently repaired, because a half-applied
    palette would recolour marks he did not touch."""
    if raw is None:
        return fallback
    if not isinstance(raw, list) or not 1 <= len(raw) <= 24:
        raise ValueError("palette must be a list of 1 to 24 colours")
    out, seen = [], set()
    for e in raw:
        if not isinstance(e, dict):
            raise ValueError("palette entries must be objects")
        pid = str(e.get("id", ""))
        label = str(e.get("label", "")).strip()
        color = str(e.get("color", ""))
        if not PALETTE_ID_RE.match(pid):
            raise ValueError("bad palette id %r" % pid[:12])
        if pid in seen:
            raise ValueError("duplicate palette id %r" % pid)
        seen.add(pid)
        if not 1 <= len(label) <= 40 or not SAFE_FIELD_RE.match(label):
            raise ValueError("bad label for %r" % pid)
        if not HEX_RE.match(color):
            raise ValueError("bad colour for %r" % pid)
        entry = {"id": pid, "label": label, "color": color}
        dark = str(e.get("dark", "") or "")
        if dark:
            if not HEX_RE.match(dark):
                raise ValueError("bad dark colour for %r" % pid)
            entry["dark"] = dark
        if e.get("fixed"):
            entry["fixed"] = True
        # R45. A deleted colour is RETIRED, never removed: marks that keep it
        # still need its hex to render, so the entry stays here and the picker
        # hides it. This is what makes "delete the colour, keep the highlights"
        # possible at all.
        if e.get("retired"):
            entry["retired"] = True
        out.append(entry)
    # The built-ins cannot be deleted: marks already carry their ids, and a mark
    # whose colour no longer exists would render as nothing at all.
    have = {e["id"] for e in out}
    for base in DEFAULT_PALETTE:
        if base["id"] not in have:
            raise ValueError("the built-in colour %r cannot be removed" % base["id"])
    # 🔴 Card is a MARK TYPE, not a colour (EH, 2026-08-23: "completely
    # separate from all other highlight colors... a separate functionality").
    # Its row exists here only so marks already carrying c="d" keep rendering;
    # nothing about it is editable, whatever the client sent. Pinned rather
    # than refused so an old client echoing a drifted copy cannot brick the
    # palette save.
    card = next(e for e in DEFAULT_PALETTE if e["id"] == "d")
    out = [dict(card) if e["id"] == "d" else e for e in out]
    return out


def settings_path(cfg):
    """In the working directory, not the machine config, because these are
    preferences rather than machine state: they belong beside the courses, where
    a synced folder gives every machine the same ones, and nothing in here is a
    secret.

    🔴 Two levels since 2026-08-16 (plan §10a/§10c): with a courses root the
    preferences live at the ROOT, so his palette and his model follow him into
    the next module instead of being rebuilt per course. A module's own file
    still overrides, key by key, which is how a shared module can arrive with a
    sensible palette without silently rewriting his. Writes go to the root file
    when there is one, so there is never a question of which file a change
    landed in. Without a courses root this is exactly what it always was."""
    root = cfg.get("courses_dir")
    return (root / "settings.json") if root is not None else (cfg["notes_dir"] / "settings.json")


def module_settings_path(cfg):
    return cfg["notes_dir"] / "settings.json"


def read_settings_data(cfg):
    """Root preferences with the module's own on top of them."""
    data = dict(read_json_sidecar(settings_path(cfg), {}))
    if cfg.get("courses_dir") is not None:
        for k, v in read_json_sidecar(module_settings_path(cfg), {}).items():
            if k != "module":
                data[k] = v
    return data


# R3, asked 2026-08-13: "we need a way to adjust text size in the right-hand panel."
# A multiplier rather than a pixel size, because the two right-hand panels set type in
# a dozen places at half a dozen sizes and a single number has to scale all of them
# while keeping their relative hierarchy. Stored here rather than per browser so both
# Macs agree, which is how every other preference in this file already behaves.
PANEL_SIZES = [
    {"id": "s",  "label": "Smaller",   "scale": 0.88},
    {"id": "m",  "label": "Default",   "scale": 1.0},
    {"id": "l",  "label": "Larger",    "scale": 1.14},
    {"id": "xl", "label": "Largest",   "scale": 1.3},
]
PANEL_SIZE_IDS = {e["id"] for e in PANEL_SIZES}
DEFAULT_PANEL_SIZE = "m"

# EH, 2026-09-03: "it would be nice to have something that lets you change the
# font size, especially in the main left pane with the lessons."
#
# 🟢 THE SAME FOUR STEPS AND THE SAME FOUR WORDS as the panel above, deliberately.
# It is one idea on a second surface, and giving the reader two vocabularies for
# one idea is how they drift apart. So `PANEL_SIZES` is the list for both.
#
# 🔴 BUT A SEPARATE KEY, for the reason `playbackRate` and `lectureRate` are
# separate keys: two controls over two surfaces. Wanting a big lesson beside a
# compact panel is the ordinary case, not a corner one, and reading one from the
# other would make the second control a lie.
DEFAULT_LESSON_SIZE = "m"

# R19: how wide the right-hand pane is, in CSS pixels, dragged by its left edge.
# Stored here for the same reason panelSize is: both Macs should agree.
#
# The bounds are the point of keeping this server side rather than trusting the
# page. MIN is a little under the 410px the notes panel was born at, so a deck is
# still readable; MAX stops a drag from leaving no lesson behind the pane, which
# would be unrecoverable without editing the file by hand, since the handle would
# be off screen. The page clamps too, against the live window, which this cannot
# see: a width that fits the Mini can swallow the MacBook.
PANEL_W_MIN = 320
PANEL_W_MAX = 900
DEFAULT_PANEL_W = 440


def clean_panel_width(value, fallback):
    """A width from the page, or from a hand-edited settings file, made safe."""
    try:
        n = int(round(float(value)))
    except (TypeError, ValueError):
        return fallback
    return max(PANEL_W_MIN, min(PANEL_W_MAX, n))


# 🔴 THE RATES ARE A LIST, NOT A RANGE, and that is a security decision as
# much as a design one. This value is chosen inside a SANDBOXED package and
# arrives at the lesson page by `postMessage` from an opaque origin, so the
# sender cannot be identified by origin at all. An allow-list means the worst a
# hostile package can do with the channel is set a speed the reader could have
# set themselves; a range would let it write an arbitrary number into his
# settings file. It is also the list the strip cycles through, and a test joins
# the two so they cannot drift.
# 🟢 **ELEVEN VALUES 2026-09-03, EH in chat**: *"What I'd love for you to add is
# 0.5 speed and then 2.25, 2.5, 2.75, and 3."* The top of the list only became
# worth having hours earlier: until `c36ef0c` a faster voice bought silence
# rather than time, because the slide ended on its own timeline.
#
# 🔴 **THE SERVER LEADS THE CLIENTS HERE, DELIBERATELY, AND THE ORDER IS THE
# WHOLE POINT.** This file needs a RESTART; `local-layer.html` and
# `reader/player-controls.html` deploy ON SAVE. So widening the two client lists
# first would put five speeds in front of a reader that this running server still
# refuses: `clean_rate` would fall them back to 1, the lecture would play at 2.5
# and revert on reload, **and a control that forgets what you told it reads as a
# bug rather than as a deploy in progress.**
#
# 🟢 Widening the server FIRST is inert: it accepts values nothing sends yet.
# The clients follow once this is deployed, and `test_player_controls.py`'s join
# pins the direction that can actually hurt (a client offering what the server
# refuses) rather than plain equality, which would have forbidden the only safe
# order.
PLAYBACK_RATES = [0.5, 0.75, 1, 1.25, 1.5, 1.75, 2, 2.25, 2.5, 2.75, 3]
DEFAULT_PLAYBACK_RATE = 1


def clean_rate(v, fallback=DEFAULT_PLAYBACK_RATE):
    """One of `PLAYBACK_RATES`, or the fallback. Never raises: a rate is a
    convenience, and refusing a whole settings save because of one would take
    the palette down with it.

    🔴 A BOOL IS AN INT IN PYTHON, so `float(True)` is `1.0`, which is ON the
    list: without the guard below, `{"playbackRate": true}` did not fall back,
    it quietly SET the rate to 1 and overwrote whatever the reader had chosen.
    ⚠️ Found 2026-09-03 by a test written for `lectureRate`, and it was already
    true of `playbackRate`; `timeline.positive_rate` has carried the same guard
    since it was written, with the same reasoning, which is what made it worth
    checking here rather than assuming.
    """
    if isinstance(v, bool):
        return fallback
    try:
        n = float(v)
    except (TypeError, ValueError):
        return fallback
    for r in PLAYBACK_RATES:
        if abs(n - r) < 1e-9:
            return r
    return fallback


def read_settings(cfg):
    data = read_settings_data(cfg)
    model = data.get("model")
    if model not in MODEL_IDS:
        model = cfg["explain_model"] if cfg["explain_model"] in MODEL_IDS else "claude-sonnet-5"
    lvl = str(data.get("level") or "").lower()
    try:
        palette = clean_palette(data.get("palette"), DEFAULT_PALETTE)
    except ValueError:
        # A settings file edited by hand into something invalid should not take
        # the reader down with it: fall back rather than refuse to load.
        palette = DEFAULT_PALETTE
    ids = {e["id"] for e in palette}
    last = data.get("lastColour")
    retired_ids = {e["id"] for e in palette if e.get("retired")}
    if last not in ids or last == "d" or last in retired_ids:
        last = "k"       # Card is never the sticky default, retired never sticks
    size = data.get("panelSize")
    if size not in PANEL_SIZE_IDS:
        size = DEFAULT_PANEL_SIZE
    # Same list, separate key; see DEFAULT_LESSON_SIZE. Fallen back rather than
    # rejected here for the reason every other read is: a settings file edited
    # by hand into something invalid should not take the reader down with it.
    lesson_size = data.get("lessonSize")
    if lesson_size not in PANEL_SIZE_IDS:
        lesson_size = DEFAULT_LESSON_SIZE
    width = clean_panel_width(data.get("panelWidth"), DEFAULT_PANEL_W)
    rate = clean_rate(data.get("playbackRate"))
    # 🔴 A SECOND RATE, AND IT IS A DIFFERENT THING FROM THE ONE ABOVE.
    # `playbackRate` is the VOICE: it is applied to the `<audio>` elements
    # and can be moved while the lecture plays. `lectureRate` is the whole
    # LECTURE, slides and voice together, and it is applied by rebuilding
    # the timing blob as the package is served (`?lecture_rate=`).
    #
    # ⚠️ THEY MUST NOT BE MERGED INTO ONE KEY, however similar they look.
    # EH asked for two controls (2026-09-02) precisely because they behave
    # differently: one locks when you press play, the other does not.
    # Reading one from the other would make the lock meaningless.
    lecture_rate = clean_rate(data.get("lectureRate"))
    # 🔴 CAPTIONS DEFAULT OFF, and that is a CONSTRUCTION state rather than a
    # product decision. Ruled 2026-09-03: the visible on/off control ships before
    # the rendering behind it, so every intermediate save leaves the reader
    # looking exactly as it does today and the drawing half can land in as many
    # saves as it takes. **Flipping this default belongs in the same change as
    # the last piece of the rendering**, and whoever ships that should say so.
    captions_on = data.get("captionsOn")
    if not isinstance(captions_on, bool):
        captions_on = False
    # R50, 2026-08-16: "let's move the vault thing into a setting you can enable
    # or disable." It arrived earlier the same day as a machine-config key, so
    # turning it off meant hand-editing a JSON file and relaunching.
    #
    # 🔴 The machine config still answers WHERE the vault is, because that is a
    # path and paths do not get typed into a browser (plan §10c). This is only
    # whether to publish, which is a preference, so it lives with the other
    # preferences and the config value becomes its default.
    vault_on = data.get("vaultEnabled")
    if not isinstance(vault_on, bool):
        vault_on = bool(cfg.get("vault_enabled", True))
    return {
        "model": model,
        "vaultEnabled": vault_on,
        "vaultPath": str(cfg.get("vault_courses") or ""),
        "vaultFound": bool(cfg.get("vault_courses")) and Path(cfg["vault_courses"]).parent.is_dir(),
        "level": lvl if lvl in LEVELS else DEFAULT_LEVEL,
        "models": MODELS,
        "palette": palette,
        "lastColour": last,
        "panelSize": size,
        "panelSizes": PANEL_SIZES,
        "lessonSize": lesson_size,
        "panelWidth": width,
        "panelWidthMin": PANEL_W_MIN,
        "panelWidthMax": PANEL_W_MAX,
        "playbackRate": rate,
        "lectureRate": lecture_rate,
        "playbackRates": PLAYBACK_RATES,
        "captionsOn": captions_on,
        "path": str(settings_path(cfg)),
    }


def write_settings(cfg, payload):
    current = read_settings(cfg)
    model = payload.get("model", current["model"])
    if model not in MODEL_IDS:
        raise ValueError("unknown model")
    lvl = str(payload.get("level", current["level"])).lower()
    if lvl not in LEVELS:
        raise ValueError("unknown answer level")
    palette = clean_palette(payload.get("palette"), current["palette"])
    ids = {e["id"] for e in palette}
    last = payload.get("lastColour", current["lastColour"])
    retired_ids = {e["id"] for e in palette if e.get("retired")}
    if last not in ids or last == "d" or last in retired_ids:
        last = "k"
    size = payload.get("panelSize", current["panelSize"])
    if size not in PANEL_SIZE_IDS:
        raise ValueError("unknown panel text size")
    # Rejected rather than clamped, exactly like the panel size above it: the
    # four ids have no nearest legal value, and quietly reading a typo as
    # "Default" would look identical to the reader choosing Default.
    lesson_size = payload.get("lessonSize", current["lessonSize"])
    if lesson_size not in PANEL_SIZE_IDS:
        raise ValueError("unknown lesson text size")
    # Clamped rather than rejected: a drag that ends outside the bounds should
    # settle at the bound, not throw away the whole save (which carries the
    # palette with it).
    width = clean_panel_width(payload.get("panelWidth"), current["panelWidth"])
    # Clamped to the list rather than rejected, for the reason the width is
    # clamped rather than rejected: this save carries the palette with it.
    rate = clean_rate(payload.get("playbackRate"), current["playbackRate"])
    # Same list, same clamp, same reason. A separate key because it is a
    # separate control; see the note in `read_settings`.
    lecture_rate = clean_rate(payload.get("lectureRate"), current["lectureRate"])
    vault_on = payload.get("vaultEnabled", current["vaultEnabled"])
    if not isinstance(vault_on, bool):
        raise ValueError("vaultEnabled must be true or false")
    # Rejected rather than clamped, like `vaultEnabled` beside it: a preference
    # with two values has no nearest legal value to fall back to, and silently
    # reading a typo as "off" would look exactly like the reader turning it off.
    captions_on = payload.get("captionsOn", current["captionsOn"])
    if not isinstance(captions_on, bool):
        raise ValueError("captionsOn must be true or false")
    # 🔴 Merged into what is already in the file, never written over it. The same
    # file can carry a `module` block (its id, name, class and store prefix), and
    # replacing the whole document would delete the module's identity every time
    # he dragged the panel wider.
    keep = dict(read_json_sidecar(settings_path(cfg), {}))
    keep.update({
        "model": model, "level": lvl, "palette": palette, "lastColour": last,
        "panelSize": size, "panelWidth": width, "vaultEnabled": vault_on,
        "lessonSize": lesson_size,
        "playbackRate": rate, "lectureRate": lecture_rate,
        "captionsOn": captions_on,
    })
    write_json_sidecar(settings_path(cfg), keep)
    return {"ok": True, "model": model, "level": lvl,
            "palette": palette, "lastColour": last, "panelSize": size,
            "lessonSize": lesson_size,
            "panelWidth": width, "vaultEnabled": vault_on,
            "playbackRate": rate, "lectureRate": lecture_rate,
            "captionsOn": captions_on}


def write_materials_source(cfg, payload):
    """Store this COURSE's choice: links out of materials.json, or its own folder.

    🔴 Written to the MODULE's settings.json, not the root one. `write_settings`
    writes the root file, because a palette and a model should follow him from
    course to course; where a course's slides come from is a fact about that one
    course, and two courses on one machine can honestly disagree about it."""
    source = str(payload.get("source") or "").strip().lower()
    if source not in MATERIALS_SOURCES:
        raise ValueError("source must be one of: %s" % ", ".join(MATERIALS_SOURCES))
    folder = str(payload.get("dir") or "").strip()
    if len(folder) > 1000:
        raise ValueError("that path is too long")
    # 🔴 Merged, never written over: the same file carries the `module` block
    # (id, name, class, store prefix), and replacing the document would delete
    # the course's identity every time somebody changed this.
    keep = dict(read_json_sidecar(module_settings_path(cfg), {}))
    keep["materials_source"] = source
    if folder:
        keep["materials_dir"] = folder
    else:
        keep.pop("materials_dir", None)
    write_json_sidecar(module_settings_path(cfg), keep)
    return local_materials_report(cfg)


def chosen_model(cfg):
    return read_settings(cfg)["model"]


def note_text(cfg, doc_id):
    """The whole note as plain text. Measured before deciding to send it: a note
    is 3,200 to 5,900 tokens, so the round trip an on-demand fetch would add
    costs more (in the only currency that matters here, which is the eight
    seconds EH is waiting) than the tokens it would save."""
    try:
        path = note_path(cfg, doc_id)
    except ValueError:
        return ""
    try:
        blocks = find_blocks(path.read_text(encoding="utf-8"))
    except OSError:
        return ""
    return "\n".join(html_to_text(b["inner"]) for b in blocks)[:60000]


ASK_PROMPT = """A student is reading one of their own lessons for their course, {course}. They selected a passage and asked a question about it.

THE WHOLE LESSON THEY ARE READING, for context. Use it where it helps and ignore it where it does not:
<lesson>
{lesson}
</lesson>

WHAT THEY SELECTED:
{term}

THE PASSAGE IT SITS IN:
{context}

THEIR QUESTION:
{question}

Answer it. Rules:
{level}
- Answer the question actually asked, in as few sentences as it takes.
- If the lesson itself answers it somewhere else, say where, by section name.
- Where the evidence is contested or unknown, say so plainly rather than smoothing it over.
- British spelling. No em dashes. No preamble, no sign-off, no headings.
"""

EXPLAIN_QUESTION = "What does this mean? Lead with what it is, then the mechanism or why it matters here. If the passage uses it in a particular sense, explain that sense rather than the general one."

FOLLOWUP_PROMPT = """A student is reading one of their own lessons for their course, {course}. They selected a passage and have been asking about it.

THE WHOLE LESSON THEY ARE READING, for context. Use it where it helps and ignore it where it does not:
<lesson>
{lesson}
</lesson>

WHAT THEY SELECTED:
{term}

THE PASSAGE IT SITS IN:
{context}

THE CONVERSATION SO FAR:
{history}

THEIR NEXT QUESTION:
{question}

Answer it. Rules:
{level}
- Answer the question actually asked, in as few sentences as it takes. Two or three is usually right; one is fine.
- Assume they have read everything above. Do not restate what you already told them.
- If the lesson itself answers it somewhere else, say where, by section name.
- Where the evidence is contested or unknown, say so plainly rather than smoothing it over.
- British spelling. No em dashes. No preamble, no sign-off, no headings.
"""


# --------------------------------------------------------------------------
# reaching Claude: the CLI on this machine, or the API with the owner's key
# --------------------------------------------------------------------------
#
# EH, in chat 2026-09-17: "We should query Claude directly." Until then every
# question the reader asked went through the `claude` binary, which uses the
# subscription that CLI is signed in with; a machine without Claude Code had
# the feature switched off and no way to turn it on. Now there are two routes
# behind one seam. `ask_claude` is the only thing `do_ask` and `do_rewrite`
# call, and which route answers is `ask_backend` in the config:
#
#   auto   the CLI when the binary is there, else the API when a key is set
#          (the default, so a machine that has the CLI behaves as it did)
#   cli    the CLI only
#   api    the API only
#
# 🔴 The key is the machine owner's, lives in their config file (0600) and
# nowhere else: never in /api/status, never in the settings page, never in
# the log, never in an error string. No key ships in the kit.
#
# The API call is one POST with urllib, not an SDK: a new dependency in the
# kit would have to install itself on a recipient's machine on first use,
# which is exactly the difficulty the caption engine has and this need not.

ANTHROPIC_MESSAGES_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
API_MAX_TOKENS = 8192
BACKEND_MODES = ("auto", "cli", "api")


def claude_binary(cfg):
    """The Claude Code CLI on this machine, or "" when there is none."""
    binary = str(cfg.get("claude_bin") or "")
    if binary and Path(binary).exists():
        return binary
    return shutil.which("claude") or ""


def api_key(cfg):
    return str(cfg.get("api_key") or "").strip()


HOST_HERE = "this machine"


def host_word(client_ip):
    """The layer's hostWord(), server-side, for sentences the SERVER composes
    about where Claude Code would have to be. A request from loopback is on
    the machine holding the files, always; from anywhere else the reader is
    somewhere else (EH on the MacBook, the server on the Mini), and "this
    machine" would be false, so the machine's own name is used, or the
    generic term when there is none. See the note above machine_name()."""
    if is_loopback(client_ip):
        return HOST_HERE
    return machine_name() or "the study server"


def ask_backend(cfg, host=HOST_HERE):
    """Which route answers, as (kind, detail): ("cli", binary), ("api", key),
    or ("", why) when neither can, with a sentence a person can act on.
    `host` is the word for the machine the server runs on, from host_word():
    the sentences name it, and they must not claim "this machine" to a reader
    who is somewhere else."""
    if not cfg.get("explain_enabled"):
        return "", "Explain is switched off in the config."
    mode = str(cfg.get("ask_backend") or "auto").strip().lower()
    if mode not in BACKEND_MODES:
        mode = "auto"
    binary = claude_binary(cfg)
    key = api_key(cfg)
    if mode == "cli":
        if binary:
            return "cli", binary
        return "", ("Claude Code was not found on %s, and the config "
                    "says to use it (ask_backend: cli). Install it and sign in, "
                    "or choose another route in Settings." % host)
    if mode == "api":
        if key:
            return "api", key
        return "", ("No API key is set, and the config says to use the API "
                    "(ask_backend: api). Paste a key in Settings, or choose "
                    "another route there.")
    if binary:
        return "cli", binary
    if key:
        return "api", key
    return "", ("Neither route to Claude is set up on %s: install "
                "Claude Code and sign in, or paste an API key in Settings."
                % host)


def explain_status(cfg, host=HOST_HERE):
    """What /api/status and the Settings page both say about asking: on or
    off, which route, and why when off. One computation, so the two pages
    cannot disagree, and the key is not in it."""
    kind, detail = ask_backend(cfg, host)
    return {"explain": bool(kind), "backend": kind,
            "explainWhy": "" if kind else detail}


def ask_claude(cfg, prompt, waiting_for="an answer", host=HOST_HERE):
    """One prompt, one answer, by whichever route is set up. Returns
    {"ok": True, "text": ...} or {"ok": False, "error": ...}, and the error
    names the route, because "it did not answer" has two different fixes."""
    kind, detail = ask_backend(cfg, host)
    if kind == "cli":
        out = ask_via_cli(cfg, detail, prompt, waiting_for)
    elif kind == "api":
        out = ask_via_api(cfg, detail, prompt, waiting_for)
    else:
        return {"ok": False, "error": detail}
    if out.get("ok"):
        out["text"] = out["text"].replace("\u2014", ", ")
    return out


def ask_via_cli(cfg, binary, prompt, waiting_for="an answer"):
    """The Claude Code CLI. No API key: it uses the subscription the CLI is
    already signed in with."""
    cwd = cfg["cache_dir"] / "explain-cwd"
    cwd.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.run(
            [binary, "-p", prompt, "--model", chosen_model(cfg)],
            cwd=str(cwd), capture_output=True, text=True,
            timeout=int(cfg["explain_timeout"]),
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "Timed out waiting for %s from Claude Code."
                % waiting_for}
    except OSError as err:
        return {"ok": False, "error": "Could not run Claude Code: %s" % err}

    if proc.returncode != 0:
        detail = (proc.stderr or "").strip().splitlines()
        return {"ok": False, "error": "Claude Code: %s"
                % (detail[-1] if detail else "the CLI exited with an error.")}

    text = (proc.stdout or "").strip()
    if not text:
        return {"ok": False, "error": "Claude Code gave an empty answer."}
    return {"ok": True, "text": text}


def _https_post(url, headers, body, timeout):
    """One HTTPS POST, as (status, text). Kept apart so the tests can stand
    in for the network and the suite never makes a real call."""
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as err:
        with err:
            return err.code, err.read().decode("utf-8", "replace")


def without_key(text, key):
    """A sentence about to be shown or logged, with the key taken out of it
    should anything upstream have echoed it back."""
    return text.replace(key, "[the key]") if key else text


def ask_via_api(cfg, key, prompt, waiting_for="an answer"):
    """The Messages API with the owner's key. Same model ids as the CLI, same
    prompt text, the reply's text blocks joined."""
    body = json.dumps({
        "model": chosen_model(cfg),
        "max_tokens": API_MAX_TOKENS,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")
    headers = {
        "x-api-key": key,
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
        "user-agent": user_agent(cfg),
    }
    timed_out = {"ok": False, "error": "Timed out waiting for %s from the API." % waiting_for}
    try:
        status, text = _https_post(ANTHROPIC_MESSAGES_URL, headers, body,
                                   int(cfg["explain_timeout"]))
    except TimeoutError:
        return timed_out
    except urllib.error.URLError as err:
        if isinstance(err.reason, TimeoutError):
            return timed_out
        return {"ok": False, "error": without_key(
            "Could not reach api.anthropic.com: %s" % err.reason, key)}
    except OSError as err:
        return {"ok": False, "error": without_key(
            "Could not reach api.anthropic.com: %s" % err, key)}

    try:
        data = json.loads(text)
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    if status == 401:
        return {"ok": False, "error": "The API key was refused (401). Check it in Settings."}
    if status != 200:
        err = data.get("error")
        msg = str((err.get("message") if isinstance(err, dict) else err)
                  or text or "").strip()[:300]
        return {"ok": False, "error": without_key(
            "The API answered %d: %s" % (status, msg or "no detail"), key)}
    parts = [str(c.get("text") or "") for c in (data.get("content") or [])
             if isinstance(c, dict) and c.get("type") == "text"]
    text = "".join(parts).strip()
    if not text:
        return {"ok": False, "error": "The API gave an empty answer."}
    return {"ok": True, "text": text}


GENERAL_PROMPT = """A student is studying their course, {course}. Their question may be about this material:

{title}

THE LESSON IN FULL:
<lesson>
{lesson}
</lesson>

THE CONVERSATION SO FAR:
{history}

THEIR QUESTION:
{question}

Answer it. Rules:
{level}
- Answer the question actually asked, in as few sentences as it takes.
- Where the lesson bears on the answer, say so, by section name; where the evidence is contested or unknown, say that plainly rather than smoothing it over.
- British spelling. No em dashes. No preamble, no sign-off, no headings.
"""


def topic_text(cfg, doc_id):
    """Every part of this doc's topic, in full, labelled per part. Measured
    before choosing to send it whole: a three-part topic is ~45KB of text."""
    m = re.match(r"^(W\d+-T\d+)", doc_id or "")
    if not m:
        return note_text(cfg, doc_id)
    prefix = m.group(1)
    metas = lesson_meta_index(cfg)
    docs = sorted(d for d in metas if d == prefix or d.startswith(prefix + "-"))
    parts = []
    for d in docs:
        title = str(metas[d].get("title") or d)
        parts.append("=== %s: %s ===\n%s" % (d, title, note_text(cfg, d)))
    return "\n\n".join(parts)[:150000]


def course_text(cfg):
    """The whole course as a digest, one block per lesson: the working outline
    where one exists, else the lesson's opening, because every lesson leads
    with its thesis (NOTE-SPEC). Full text of a course is ~500KB and would
    drown the question; this stays ~30-50KB and names every lesson."""
    metas = lesson_meta_index(cfg)
    out = []
    for d in sorted(metas):
        meta = metas[d]
        head = "%s: %s" % (d, str(meta.get("title") or d))
        wk = str(meta.get("week") or "")
        if wk:
            tp = str(meta.get("topic") or "")
            head += "  (week %s%s)" % (wk, (", " + tp) if tp else "")
        digest = ""
        o = cfg["notes_dir"] / (d + "-outline.md")
        try:
            if o.is_file():
                digest = o.read_text(encoding="utf-8")[:1500]
        except OSError:
            pass
        if not digest:
            digest = note_text(cfg, d)[:700]
        out.append(head + "\n" + digest.strip())
    return "\n\n".join(out)[:150000]


def do_ask(cfg, payload, host=HOST_HERE):
    """One path for every question about a selection, including "explain this",
    which is now just a preset question rather than a separate mode. The
    conversation is stateless on the server: the page holds the turns and sends
    them back, so there is nothing to expire and nothing to clean up."""
    term = norm_term(str(payload.get("term") or ""))[:400]
    context = re.sub(r"\s+", " ", str(payload.get("context") or ""))[:2500]
    question = re.sub(r"\s+", " ", str(payload.get("question") or ""))[:2000]
    title = re.sub(r"\s+", " ", str(payload.get("title") or ""))[:200]
    level = level_rules(payload.get("level"))
    doc = str(payload.get("doc") or "")
    # EH's design (asked 2026-08-22, built 2026-08-23): the chat answers from
    # this lesson, this whole topic, or the whole course. The scope changes
    # WHAT is in front of the answerer, not how it answers.
    scope = str(payload.get("scope") or "lesson").lower()
    if scope == "course":
        note = ("THE WHOLE COURSE the student is asking across, one lesson per "
                "block (its title, then its outline or opening):\n\n"
                + course_text(cfg))
    elif scope == "topic" and doc:
        note = ("THE WHOLE TOPIC the student is asking across, every part in "
                "full:\n\n" + topic_text(cfg, doc))
    else:
        note = note_text(cfg, doc) if doc else ""

    if not question:
        question = EXPLAIN_QUESTION if term else ""
    if not question:
        return {"ok": False, "error": "Ask something first."}

    turns = payload.get("history")
    if not isinstance(turns, list):
        turns = []
    lines = []
    for turn in turns[-12:]:
        if not isinstance(turn, dict):
            continue
        who = "Student" if turn.get("role") == "user" else "You"
        said = re.sub(r"\s+", " ", str(turn.get("text") or ""))[:3000]
        if said:
            lines.append("%s: %s" % (who, said))
    history = "\n\n".join(lines)[:12000]

    blob = note or "(the lesson text was not available)"
    if term and history:
        course = str(cfg.get("module_name") or cfg.get("module") or "their course")
        prompt = FOLLOWUP_PROMPT.format(
            course=course, lesson=blob, term=term, context=context or "(none)",
            history=history, question=question, level=level)
    elif term:
        course = str(cfg.get("module_name") or cfg.get("module") or "their course")
        prompt = ASK_PROMPT.format(
            course=course, lesson=blob, term=term,
            context=context or "(no surrounding text)",
            question=question, level=level)
    else:
        # A panel chat opened on its own has no selection behind it.
        course = str(cfg.get("module_name") or cfg.get("module") or "their course")
        prompt = GENERAL_PROMPT.format(
            course=course, title=title or "this course", lesson=blob,
            history=history or "(nothing yet)", question=question, level=level)

    out = ask_claude(cfg, prompt, host=host)
    if out.get("ok"):
        out["model"] = chosen_model(cfg)
        out["term"] = term
    return out


# --------------------------------------------------------------------------
# annotations: kept beside the note, never inside it
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# marks: the source of truth, on disk beside the notes
# --------------------------------------------------------------------------

def sidecar_path(cfg, doc_id, suffix):
    if not DOC_ID_RE.match(doc_id or ""):
        raise ValueError("bad doc id")
    return cfg["notes_dir"] / ("%s-%s.json" % (doc_id, suffix))


def keep_the_unreadable_bytes(path, raw, why, cfg=None):
    """Preserve a sidecar that would not parse, and say so out loud.

    🔴 An ABSENT file and a CORRUPT file used to be indistinguishable
    here, and only one of them is harmless. Reproduced 2026-09-01 (data-loss
    audit §6b): write `{` into a `-marks.json`, save a highlight on that lesson,
    and the guard beside the writer keeps **no copy and logs nothing** while the
    write replaces the damaged file. The case where the losing copy is most
    worth having was the one case it was never taken.

    ⚠️ **Once per distinct content, not once per read.** A corrupt
    sidecar is read on every request that touches the lesson, and a backup per
    read would bury the notes folder in copies of one broken file. Identical
    bytes already kept means there is nothing to do.
    """
    try:
        if raw is None:                     # unreadable, not unparseable
            return None
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        folder = split_lessons.backup_target(path, "x").parent
        for old in folder.glob(path.name + ".unreadable-*.bak"):
            try:
                if old.read_text(encoding="utf-8") == raw:
                    return old
            except OSError:
                continue
        # 🔴 The stamp is second-resolution, so two DIFFERENT corrupt
        # states within one second collide and the second write destroys the
        # copy taken for the first. Caught by a probe on this function's first
        # run: the log said "kept 1 bytes" then "kept 2 bytes", both naming the
        # same file. A backup that overwrites a backup is the defect this whole
        # entry is about, arriving inside its own fix.
        # 🟢 Since 2026-09-18 the name is CLAIMED (`claim_backup`), not merely
        # checked: an exists-then-write can still lose the race between two
        # threads, and the counter now reads `-2` like every other backup here.
        fd, bak = claim_backup(lambda n: split_lessons.backup_target(
            path, "%s.unreadable-%s%s.bak" % (path.name, stamp, n)))
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write(raw)
        line = ("UNREADABLE sidecar %s (%s), kept %d bytes as %s -- it is being "
                "treated as EMPTY, so anything it held is not in what the "
                "reader sees" % (path.name, why, len(raw), bak.name))
        if cfg is not None:
            log(cfg, line)
        print("study-server: " + line, file=sys.stderr, flush=True)
        return bak
    except Exception:
        # A failed rescue must never fail the read it was trying to protect.
        return None


def read_json_sidecar(path, fallback, cfg=None):
    """The sidecar as a dict, or *fallback* when there is nothing usable.

    🔴 `fallback=None` means "tell me there is nothing" and returns
    `None`. It used to raise `TypeError` from `dict(None)` — on an absent file
    as well as a corrupt one — which three call sites were quietly relying on
    or quietly broken by. `colour_uses` and `colour_purge` glob every
    `*-marks.json` and pass `None`, so **one corrupt sidecar anywhere in a
    module raised an uncaught `TypeError` out of both**, and the delete-a-colour
    confirm and purge failed for the whole module. Reproduced before fixing.

    A file that will not parse is preserved and announced rather than being
    silently reported as empty: see `keep_the_unreadable_bytes`.
    """
    def nothing():
        return dict(fallback) if fallback is not None else None

    if not path.exists():
        return nothing()
    raw = None
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
        why = "it holds a %s rather than an object" % type(data).__name__
    except ValueError as exc:
        why = "%s: %s" % (type(exc).__name__, exc)
    except OSError as exc:
        # Nothing was read, so there are no bytes to keep; still not silent.
        raw, why = None, "%s: %s" % (type(exc).__name__, exc)
    keep_the_unreadable_bytes(path, raw, why, cfg)
    return nothing()


def write_json_sidecar(path, doc):
    with WRITE_LOCK:
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, path)


# --- rolling snapshots: what the shrink guard cannot protect against -------------------
#
# Plan 02 §10d item 2. The shrink guard beside `write_marks` keeps a copy of one
# bad write. It does not protect against a bad WEEK: a slow corruption, a script
# that rewrites everything, a sync conflict that resolves the wrong way. EH
# has had that protection only when a session happened to be running and made an
# `_admin/userdata-backups/` copy by hand; a recipient of this system would have
# nothing at all.
#
# So: on the first sidecar write of each day, copy every sidecar in the module to
# `.snapshots/<YYYY-MM-DD>/`, and keep the last SNAPSHOT_KEEP days. The sidecars
# are kilobytes, the copy happens once a day, and the folder starts with a dot so
# nothing that globs the module can see it.

SNAPSHOT_KEEP = 14
SNAPSHOT_DIR = ".snapshots"
_SNAPSHOT_DONE = set()          # (module folder, date) already taken this run


def tidy_stray_baks(cfg):
    """File stray dated backups into backups/ folders, once per start.

    Until 2026-08-22 every writer left its .bak beside the original, and one
    course had accumulated 2,463 of them in its top folder (EH: "they're
    really clogging up the main folders"). The writers now use
    split_lessons.backup_target; this moves what the old writers left, so an
    upgraded install converges without anyone running anything. Idempotent,
    never raises: an unmovable stray stays a stray."""
    moved = 0
    try:
        roots = []
        root = cfg.get("courses_dir")
        if root:
            roots.append(Path(root))
        for mid, folder in resolve_modules(cfg).items():
            roots.append(folder)
        # 🔴 Attachments live in a root of their own, never inside the course
        # folder. This looked for `<course>/resources/`, which nothing has ever
        # created, so the sweep covered no attachment folder at all. Corrected
        # 2026-08-30 with the move to `resources/<MODULE>/<DOC>/`; both levels
        # are swept so the flat pre-2026-08-30 layout is covered too.
        base = resources_base(cfg)
        if base.is_dir():
            for d in base.iterdir():
                if not d.is_dir() or d.name == split_lessons.BACKUP_DIRNAME:
                    continue
                roots.append(d)
                roots.extend(q for q in d.iterdir() if q.is_dir()
                             and q.name != split_lessons.BACKUP_DIRNAME)
        for d in roots:
            if not d.is_dir() or d.name == split_lessons.BACKUP_DIRNAME:
                continue
            for p in d.iterdir():
                if not p.is_file() or not p.name.endswith(".bak"):
                    continue
                dest = split_lessons.backup_target(p, p.name)
                if dest.exists():
                    continue           # never overwrite; a stray beats a loss
                try:
                    p.replace(dest)
                    moved += 1
                except OSError:
                    pass
        if moved:
            log(cfg, "tidied %d stray .bak files into backups/" % moved)
    except Exception as exc:
        log(cfg, "bak tidy FAILED, carrying on: %s" % exc)
    return moved


def place_legacy_resources(cfg):
    """Move a flat `resources/<DOC>/` into the course it belongs to, once.

    Attachments became `resources/<MODULE>/<DOC>/` on 2026-08-30, because doc
    ids repeat across courses and the flat folder made two lessons one folder.
    A flat folder that already exists on an upgraded install would simply stop
    being listed, which is a file going quiet rather than a file being lost, and
    is still the failure this project least wants to ship.

    🔴 It only moves what it can place WITHOUT GUESSING, and the restraint is
    the point. A flat folder can only have been written while the install had
    one course, so with exactly one course today the owner is known. With
    several, the owner is a question for a person: dealing the folder to
    whichever course sorts first would recreate the exact bug this replaces,
    quietly, and with the server's authority behind it. So it is left where it
    is and named in the log.

    Never raises, never overwrites, never deletes: an unmovable folder stays
    where it is and stays readable by hand.
    """
    moved = 0
    try:
        if cfg.get("courses_dir") is None:
            return 0            # the single-module world keeps the flat layout
        base = resources_base(cfg)
        if not base.is_dir():
            return 0
        mods = resolve_modules(cfg)
        strays = [d for d in sorted(base.iterdir())
                  if d.is_dir() and d.name not in mods
                  and DOC_ID_RE.match(d.name)]
        if not strays:
            return 0
        if len(mods) != 1:
            log(cfg, "resources: %d flat doc folder(s) predate per-course "
                     "attachments and %d courses could own them, so they are "
                     "left alone: %s" % (len(strays), len(mods),
                                         ", ".join(d.name for d in strays)))
            return 0
        mid = next(iter(mods))
        for d in strays:
            dest = base / mid / d.name
            if dest.exists():
                log(cfg, "resources: %s is already placed in %s, leaving the "
                         "flat copy alone" % (d.name, mid))
                continue
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                d.replace(dest)
                moved += 1
            except OSError as exc:
                log(cfg, "resources: could not place %s: %s" % (d.name, exc))
        if moved:
            log(cfg, "resources: placed %d doc folder(s) under %s" % (moved, mid))
    except Exception as exc:
        log(cfg, "resource placement FAILED, carrying on: %s" % exc)
    return moved


def snapshot_sidecars(cfg):
    """Once a day, per module. Never raises: a failed snapshot must not stop a
    highlight being saved, which is the thing that actually matters."""
    try:
        folder = cfg["notes_dir"]
        day = datetime.now().strftime("%Y-%m-%d")
        key = (str(folder), day)
        if key in _SNAPSHOT_DONE:
            return None
        root = folder / SNAPSHOT_DIR
        target = root / day
        if target.is_dir():
            _SNAPSHOT_DONE.add(key)
            return None
        target.mkdir(parents=True, exist_ok=True)
        n = 0
        for src in sorted(folder.glob("*.json")):
            if ".bak" in src.name or src.name.endswith(".tmp"):
                continue
            shutil.copy2(src, target / src.name)
            n += 1
        _SNAPSHOT_DONE.add(key)
        # Prune oldest first, and only ever inside our own folder.
        days = sorted(p for p in root.iterdir() if p.is_dir())
        for old in days[:-SNAPSHOT_KEEP] if len(days) > SNAPSHOT_KEEP else []:
            shutil.rmtree(old, ignore_errors=True)
        log(cfg, "snapshot %s: %d sidecars, keeping %d days"
            % (day, n, min(len(days), SNAPSHOT_KEEP)))
        return target
    except Exception as exc:                       # deliberately broad
        try:
            log(cfg, "snapshot FAILED, carrying on: %s" % exc)
        except Exception:
            pass
        return None


# --- the losing copy, for every sidecar rather than for two of them --------------------
#
# 🔴 Found by the data-loss audit (`_admin/AUDIT-data-loss-routes-2026-09-01.md`
# §3), and the shape of the finding matters more than the fix. `marks` and `cards` each
# grew a shrink guard after losing something; `bookmarks`, `chatmarks`, `chats` and
# `additions` were written LATER and inherited neither the guard nor the snapshot call.
# A protection that has to be remembered at each new call site is one that new call sites
# do not get, and four of six is what that looks like after a fortnight.
#
# So the guard is one function now, and adding a seventh sidecar is a question somebody
# has to answer rather than a step they can silently skip.
#
# 🔴 It does NOT choose a winner and does not change what the client may do, which
# is the property both original comments went out of their way to state: it only means the
# copy that is about to be replaced still exists afterwards.
#
# ⚠️ `additions` is the one that would have hurt most. It is the only sidecar
# holding content the reader chose to keep and cannot regenerate, and its own docstring
# says it exists so that a rewrite "never destroys his additions".

def keep_the_losing_copy(cfg, path, kind, doc_id, count, after, keys=None):
    """Take the day's snapshot, and if this write would DROP anything the file
    holds, keep the copy that is about to be replaced beside the lesson.

    `count` is a callable over a loaded sidecar rather than a number, because
    the six sidecars count different things: marks are items plus notes, chat
    marks are marks plus chats, cards are the keys of an object. Passing the
    rule in is what lets one function serve all of them without knowing any of
    their shapes.

    🔴 It now runs over BOTH sides. It used to be a callable for the stored
    document and a separately written expression for the new one: the same rule
    spelled twice, in two places, free to drift apart without anything failing.

    `keys` is that idea applied to IDENTITY, and it is what makes this a
    comparison rather than a tally. Given a callable returning one key per
    item, a write loses something when a key the file holds is missing from
    what replaces it, however the totals move. Without it the guard can only
    see a shrink, which QA measured on 2026-09-01 as one losing shape in
    three: swapping a mark at equal count, and dropping one while adding two,
    are both silent.

    🔴 `marks` passed one first, and `additions` joined it on 2026-09-04 when
    the kept notes were given an identity: the block plus the instant the
    reader pressed Keep. **That is still a ruling rather than an unfinished
    job for the rest.** A key must be chosen against what CHANGES it, never
    against whether it looks unique in today's data, so the remaining sidecars
    are separate questions with separate answers. `cards` is the one that
    looks easy and is not: its keys are mark ids, issued per device from a
    local counter, so two devices both call a card `3`. Keying on that would
    report a card kept when it had in fact been replaced, which is worse than
    counting.

    Never raises. A failed backup must not fail the write: the reader's
    highlight matters more than the copy, and the log carries the failure.
    """
    # Before the write, so the copy is of what was there rather than of what is
    # about to replace it. `snapshot_sidecars` self-limits to once per module
    # per day, so calling it from every writer costs nothing after the first.
    snapshot_sidecars(cfg)
    try:
        before = read_json_sidecar(path, None, cfg)
    except Exception:
        before = None
    if not before:
        # Absent, empty, or unreadable. 🟢 The unreadable case is no
        # longer a silent loss: the READER preserved the bytes and logged it
        # before returning, which is what this guard would have wanted to do and
        # could not, having nothing to measure a shrink against.
        return None
    try:
        was = int(count(before))
        now = int(count(after))
    except Exception:
        # An unreadable or unexpected shape on either side is not a change
        # anybody can measure, and guessing would produce a backup on every
        # write.
        return None

    # What the file holds against what is about to replace it, by identity.
    # A multiset, not a set: one phrase can carry two marks in one block, and
    # subtracting sets would call the second one a duplicate and lose it.
    #
    # 🔴 An item the key function cannot name is counted rather than dropped
    # from the comparison. Skipping them reads as the modest choice and is not:
    # skipped, they leave the comparison entirely, so the one thing that IS
    # knowable about them -- how MANY there were -- goes unsaid. Bucketed, a
    # FALL in their number is a loss like any other.
    #
    # ⚠️ What this does NOT do, because the wording is easy to over-read and a
    # first draft of it did (caught by QA, 2026-09-01): swapping one unnameable
    # item for another stays invisible, and no rule can fix that. Two items with
    # no identity are indistinguishable, so two in and two out cancel whichever
    # way they are counted. What becomes visible is two unnameable becoming one
    # unnameable and one named.
    #
    # The cost is one spare backup if a mark ever gains an anchor it did not
    # have, and the reader's marks all carry one, so that is a trade of a file
    # against a highlight.
    lost, unknown = [], False
    if keys is not None:
        try:
            held = Counter(k or NO_IDENTITY for k in keys(before))
            kept = Counter(k or NO_IDENTITY for k in keys(after))
            lost = list((held - kept).elements())
        except Exception as exc:
            # 🔴 A broken key function must not fall back to counting in
            # silence: an identity check that quietly stops checking is the
            # exact defect this parameter was added to fix. Keep the copy and
            # say so, so the cost of the bug is one spare backup rather than
            # the reader's highlight.
            log(cfg, "%s %s key check FAILED, keeping a copy anyway: %s"
                % (kind, doc_id, exc))
            unknown = True

    if not lost and not unknown and was <= now:
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    # ⚠️ The name stays `shrank-` although the trigger is now wider. Every
    # recovery instruction this project has written names that file, in the
    # audit, the QA log, the changelog and PROJECT-NOTES, and ONE place to look
    # is worth more to somebody recovering data than a tidier word. The log
    # line is what says which trigger fired.
    def name_at(n):
        return split_lessons.backup_target(
            path, "%s.shrank-%s%s.bak" % (path.name, stamp, n))
    if unknown:
        why = "key check FAILED at %d" % was
    elif was > now:
        # The classic shape, and the verb is unchanged on purpose: the audit,
        # the QA log, the changelog and PROJECT-NOTES all quote `SHRANK n -> m`,
        # and every one of those sentences stays true. `DROPPED` therefore means
        # something precise and new -- the count did NOT fall and the file lost
        # something anyway, which is the half that used to be silent.
        why = "SHRANK %d -> %d" % (was, now)
    else:
        # Reaching here means `lost` is non-empty: the early return above covers
        # every other case, so there is no third branch to write.
        #
        # 🔴 The COUNT of what was dropped, never the keys themselves. A mark
        # key carries the reader's own highlighted sentence, and the log is read
        # by agents and pasted into reports. The backup beside it holds every
        # word, which is where that content belongs.
        why = "DROPPED %d of %d" % (len(lost), was)
    try:
        # 🔴 Claimed, not just named: this guard runs OUTSIDE `WRITE_LOCK`
        # (its callers take the lock inside `write_json_sidecar`, after it),
        # so two saves of one sidecar in one second could pick the same name
        # and the second would destroy the copy taken for the first.
        fd, bak = claim_backup(name_at)
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write(json.dumps(before, indent=2, ensure_ascii=False))
        log(cfg, "%s %s %s, kept %s" % (kind, doc_id, why, bak.name))
        return bak
    except OSError as exc:
        log(cfg, "%s %s %s and the backup FAILED: %s"
            % (kind, doc_id, why, exc))
        return None


# --- R17: what has been opened, and where he left off ----------------------------------
#
# Asked 2026-08-13: "we also need some kind of [indicator] which we just have and have not
# been opened already which shows what's new and not new, and maybe something that shows
# the last page that was read. This should go on the index page."
#
# 🔴 Half of this already existed and was not doing its job. The hub has had a Read
# toggle, an isNew() helper and an Unread filter all along, so "new versus not new" was
# already modelled. What was missing is that ALL of it is manual: nothing marked a lesson
# read when he actually read it, so the state was only ever as true as his discipline.
# His word "opened" is the fix, and it is a different fact from "I have finished with
# this", which the manual toggle stays for.
#
# 🔴 It lives here rather than in localStorage because the hub's existing state is
# per browser, so the Mini and the MacBook have always disagreed about what he has read.
# That is the same split-brain that cost 13 highlights on 12 August. A visit is the
# cheapest possible thing to move off the browser first: losing one costs nothing, which
# makes it a safe place to establish the pattern.

VISIT_LOCK = threading.Lock()


def visits_path(cfg):
    return Path(cfg["notes_dir"]) / "visits.json"


def read_visits(cfg):
    data = read_json_sidecar(visits_path(cfg), {})
    docs = data.get("docs")
    if not isinstance(docs, dict):
        return {}
    # A hand-edited file should not be able to put junk in front of the hub.
    return {k: v for k, v in docs.items()
            if DOC_ID_RE.match(k or "") and isinstance(v, dict)}


def unit_ids(doc_id):
    """The week and topic a part belongs to, as ids in the same store.

    `W2-T3-P1` is a part of topic `W2-T3` in week `W2`. Derived from the DOC ID
    rather than from the metadata's `week`/`topicNo`, which are display values
    and disagree in shape: a doc is `W2` while its meta week is `"02"`, and
    building `W%s` from the latter would key a week nothing else can find.

    Returns (week_id, topic_id), either of which may be None for a doc id that
    does not have that shape. Nothing is invented: a course whose ids are not
    W/T/P simply gets no week or topic ratings, rather than ratings filed under
    a guess."""
    parts = str(doc_id or "").split("-")
    if len(parts) < 2:
        return None, None
    week = parts[0]
    topic = "-".join(parts[:2])
    if not DOC_ID_RE.match(week) or not DOC_ID_RE.match(topic):
        return None, None
    return week, topic


# 🔴 Two scales, deliberately the same shape. EH, 2026-08-29: stars for how
# good it was, bulbs for how interesting, "one bulb or two bulbs, all the way
# to five bulbs". Tapping the current value clears it, exactly as stars do.
RATINGS = (("stars", "&#9733;", "star"),
           ("bulbs", "&#128161;", "bulb"))


def rating_html(kind, glyph, word, value, label):
    """One row of five buttons. The same markup at part, topic and week level,
    so one click handler and one paint function serve all three."""
    btns = "".join(
        '<button type="button" class="hrate h%s%s" data-k="%s" data-n="%d" '
        'aria-label="%d %s%s">%s</button>'
        % (word, " on" if n <= value else "", kind, n, n, word,
           "" if n == 1 else "s", glyph)
        for n in range(1, 6))
    return ('<span class="hrates" role="group" aria-label="%s">%s</span>'
            % (label, btns))


def lecture_time(order, mats_docs, lstate):
    """(total, watched, without a duration) in minutes, over a course's LESSONS.

    🔴 SUMMED OVER `order`, WHICH IS THE LESSON LIST, NEVER OVER `docs`.
    `materials.json` carries `docs` for everything a course has material for,
    lecture or not: 50 entries in one course whose page lists far fewer lessons.
    **Summing `docs` would overstate the total by a plausible-looking amount**,
    which is the worst kind of wrong number, and it would disagree with the
    "0 of 38" bar rendered three lines away from it. One list feeds both.

    🔴 A lesson with no usable duration is COUNTED AS A GAP, never as zero, and
    the page says so. A tally with holes in it is worse than no tally, because a
    reader trusts a number. Junk (a string, a negative, a zero) is a gap too: a
    course onboarded badly must not throw on the page that lists it.

    Module-level and named, rather than four lines inside the handler, because
    it is the arithmetic the whole feature rests on and a test has to be able to
    drive THIS, not a copy of it. (A copy is what the first version of
    `test_video_tally.py` tested, and four hand-written breakages of the real
    code sailed through it.)
    """
    def minutes(doc):
        try:
            v = int((mats_docs.get(doc) or {}).get("minutes"))
        except (TypeError, ValueError):
            return None
        return v if v > 0 else None

    mins = dict((d, minutes(d)) for d in order)
    total = sum(v for v in mins.values() if v)
    watched = sum(v for d, v in mins.items()
                  if v and (lstate.get(d) or {}).get("watched"))
    return total, watched, sum(1 for d in order if mins[d] is None)


def unlisted_time(order, mats_docs):
    """(minutes, lectures) a course has material for and no lesson page for.

    🔴 THE SILENCE THIS EXISTS TO END. `lecture_time` sums over `order`, and
    that is right: a total disagreeing with the "0 of 38" bar three lines away
    would be the worst kind of wrong number. **But the page then never mentions
    the rest.** One course carries 12 lectures and 2 h 24 min of material with
    no lesson written, and a reader is told 9 h 53 min and never told the other
    two and a half hours exist. ⚠️ **That is the same under-reporting the gap
    clause beside it already refuses to do silently** for a lesson with no
    duration, one level up.

    🟢 So the page shows BOTH numbers, each labelled with what it counts. It
    chooses between them for nobody, which is why it needed no ruling from the
    reader: no reading of "the total video time for the whole course" is misled
    by being shown the whole course.

    ⚠️ Junk is SKIPPED here rather than counted as a gap, which is the opposite
    of `lecture_time`'s rule and deliberate: this is a secondary clause about
    material nobody has written up, and a course onboarded badly should not
    grow a second complaint on the strength of it.
    """
    listed = set(order or ())
    minutes = 0
    lectures = 0
    for doc, row in (mats_docs or {}).items():
        if doc in listed:
            continue
        try:
            v = int((row or {}).get("minutes"))
        except (TypeError, ValueError, AttributeError):
            continue
        if v > 0:
            minutes += v
            lectures += 1
    return minutes, lectures


def nb(text):
    """The same words, with every space made non-breaking.

    🔴 A quantity and its label are one thing to read and must wrap as one. QA
    measured a 382px viewport putting "left" alone on the next line, which
    separates "9 h 53 min" from the only word saying what it is, **and the two
    figures on that line are often identical**, so the reader is briefly
    looking at what appears to be a bare repetition.

    ⚠️ The obvious fix is one non-breaking space between the figure and its
    label, and it moves the problem rather than solving it: "9 h 53 min" has
    two breakable spaces of its own, so the line can still split as "9 h 53 /
    min left". The unit that must not break is the whole phrase.
    """
    return text.replace(" ", "\u00a0")


def ratings_html(state, label_for):
    """Both scales for one unit, in EH's order: bulbs first, then stars."""
    return "".join(
        rating_html(kind, glyph, word, int((state or {}).get(kind) or 0),
                    label_for(word))
        for kind, glyph, word in reversed(RATINGS))


# 🔴 The same guard `visits.json` has had all along, and lesson-state never got.
# `write_lesson_state` is read-modify-write on one file, the server is a
# ThreadingHTTPServer, and two ratings clicked in quick succession are two
# concurrent requests: both read the same state and the second write erases the
# first. Found on 2026-08-29 by clicking four ratings in a row and finding one
# in the file. It was always reachable; the bulbs made it easy, because marking
# a lesson good AND interesting is two clicks on the same box, which is the
# thing EH asked for.
LESSON_STATE_LOCK = threading.Lock()


def lesson_state_path(cfg):
    return Path(cfg["notes_dir"]) / "lesson-state.json"


def read_lesson_state(cfg):
    """Read/watched/stars per lesson: the state EH declares, as opposed to
    visits.json, which records what merely happened. Server-side because he
    reads from two Macs, and browser storage marooned the old hub's state on
    whichever machine clicked it. One file per course; the daily sidecar
    snapshot picks it up by its .json suffix like everything else."""
    data = read_json_sidecar(lesson_state_path(cfg), {})
    docs = data.get("docs")
    if not isinstance(docs, dict):
        return {}
    out = {}
    for k, v in docs.items():
        if not (DOC_ID_RE.match(k or "") and isinstance(v, dict)):
            continue
        row = {}
        if v.get("read") is True:
            row["read"] = True
        if v.get("watched") is True:
            row["watched"] = True
        # 🔴 `stars` and `bulbs` are the same shape and are read the same way.
        # EH, 2026-08-29: "one bulb or two bulbs, all the way to five bulbs",
        # and the two mean different things: how good it was, and how
        # interesting. Keys here are UNIT ids, not only lesson ids: `W3` is a
        # week, `W3-T1` a topic, `W3-T1-P2` a part. One store, one merge path,
        # one write path for all three, which is why DOC_ID_RE already fits.
        for key in ("stars", "bulbs"):
            n = v.get(key)
            if isinstance(n, int) and 1 <= n <= 5:
                row[key] = n
        if row:
            out[k] = row
    return out


def write_lesson_state(cfg, doc, payload):
    """One lesson's declared state, patched. Absent keys are untouched; a
    False or zero removes the key, so the file only ever holds positives."""
    if not DOC_ID_RE.match(doc or ""):
        raise ValueError("bad doc id")
    with LESSON_STATE_LOCK:
        docs = read_lesson_state(cfg)
        row = dict(docs.get(doc) or {})
        for key in ("read", "watched"):
            if key in payload:
                if not isinstance(payload[key], bool):
                    raise ValueError("%s must be true or false" % key)
                if payload[key]:
                    row[key] = True
                else:
                    row.pop(key, None)
        for key in ("stars", "bulbs"):
            if key not in payload:
                continue
            n = payload[key]
            # 🔴 `True` is an int in Python and would sail through as 1. The read
            # and watched flags above are booleans on the same object, so this is a
            # realistic mistake for a caller to make, not a theoretical one.
            if isinstance(n, bool) or not (isinstance(n, int) and 0 <= n <= 5):
                raise ValueError("%s must be 0 to 5" % key)
            if n:
                row[key] = n
            else:
                row.pop(key, None)
        if row:
            docs[doc] = row
        else:
            docs.pop(doc, None)
        snapshot_sidecars(cfg)
        write_json_sidecar(lesson_state_path(cfg), {"docs": docs})
    return {"ok": True, "doc": doc, "state": row}


def visits_summary(cfg):
    docs = read_visits(cfg)
    last, last_at = None, ""
    for doc, row in docs.items():
        at = str(row.get("last") or "")
        if at > last_at:
            last, last_at = doc, at
    return {"ok": True, "docs": docs, "last": last, "lastAt": last_at}


def record_visit(cfg, doc_id):
    if not DOC_ID_RE.match(doc_id or ""):
        raise ValueError("bad doc id")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    # Read, modify and write under one lock: several lessons can be open at once
    # and they all report on load, so an unguarded read-modify-write would drop
    # visits more or less at random.
    with VISIT_LOCK:
        docs = read_visits(cfg)
        row = dict(docs.get(doc_id) or {})
        try:
            count = int(row.get("count") or 0)
        except (TypeError, ValueError):
            count = 0
        row["count"] = count + 1
        row["first"] = row.get("first") or now
        row["last"] = now
        docs[doc_id] = row
        write_json_sidecar(visits_path(cfg), {"docs": docs})
    return {"ok": True, "doc": doc_id, "count": row["count"], "last": now}


# --- R15: a card keeps its definition here, resolved when it is made -------------------
#
# Asked 2026-08-13: "We should also be able to, in the right-hand panel, get everything
# we've asked for as a card. Frankly, we might as well just get definitions listed right
# there. We might as well be doing it on the spot and recording them instead of waiting
# for the Obsidian Vault to do it."
#
# 🔴 Its own sidecar, and NOT a new field on the mark, which is the important decision
# here. Marks are the file that matters and the one already lost once, on 12 August. A
# definition is derived data: it can always be looked up again, so it has no business
# sharing a file with the only copy of his highlights. Keeping them apart means nothing
# in this feature can write to, reorder or truncate a mark. Keyed by mark id, so a card
# whose mark is gone simply stops being joined to anything.
#
# 🔴 What this does NOT do: it does not change what the `d` colour writes to the vault.
# Whether the `term:::` request should stop now that definitions are captured here is
# EH's call and is outside this role's authority over Terms/. Recorded as open.

MAX_CARDS = 400
MAX_CARD_TEXT = 4000


def read_cards(cfg, doc_id):
    doc = read_json_sidecar(sidecar_path(cfg, doc_id, "cards"), {"doc": doc_id, "cards": {}})
    cards = doc.get("cards")
    return {"ok": True, "doc": doc_id, "cards": cards if isinstance(cards, dict) else {}}


# Two kinds of key, because a card can come from two places (R5). A card made on
# the page is keyed by its mark id, an integer the page issues. A card made
# inside a chat answer, or typed by hand since R44, has no mark to key on, so it
# carries its own "c" + base36 stamp. Keeping them in one file means one Cards
# list rather than two, which is what he asked for.
CARD_KEY_RE = re.compile(r"^(\d{1,9}|c[a-z0-9]{1,24})$")
# 🔴 The stamp half, on its own, because it is the half that is an IDENTITY.
CARD_STAMP_RE = re.compile(r"^c[a-z0-9]{1,24}$")


def card_key(key):
    """What identifies a CARD across two devices, or None when nothing does.

    🔴🔴 **THE ANSWER IS NOT ONE ANSWER, AND THAT IS THIS SIDECAR'S FINDING.**
    The five-sidecars entry called cards "the TRAP this entry's five-questions
    ruling exists for, not the free one", and said to do it LAST. The reason it
    gave is right and is only half of it: the file is **two collections sharing
    one document**, with opposite identities and opposite losses.

    | kind | its key | device-independent? | recoverable if lost? |
    | --- | --- | --- | --- |
    | `from: "mark"` | the mark's `id` | 🔴 **no**, a local ordinal | 🟢 **yes** |
    | `from: "chat"` | `c` + a base36 stamp | 🟢 **yes** | 🔴 **no** |
    | `from: "typed"` | `c` + a base36 stamp | 🟢 **yes** | 🔴 **no** |

    🟢 **SO THE KEY ITSELF IS THE IDENTITY, for exactly the two kinds whose key
    is a creation stamp.** `markless()` mints it once, in one place, and nothing
    in the client ever writes it again -- the same property that made `cid` the
    answer for a saved conversation, arrived at independently here because the
    stamp was already being minted for a different reason.

    🔴 **AND THERE IS NO IDENTITY FOR THE THIRD KIND. It is refused rather than
    invented**, which is the entry's own ruling applied: a key must be chosen
    against what CHANGES it. A mark card's key is `mark.id`, and `merge_marks`
    RENUMBERS every mark it adopts from another device (`_renumber`, so an
    adopted mark cannot land on an id the writer is already using). **So a mark
    card sitting on disk under a key this writer does not hold names a mark that
    has, by construction, already been renumbered to something else.** Adopting
    it would file one highlight's definition under another highlight's term:
    a card reported kept when it had in fact been replaced, which is the entry's
    own sentence and is a WRONG ANSWER rather than a loss.

    ⚠️ **WHAT THAT COSTS, said plainly rather than left as a silence:** a mark
    card the other device made is still dropped by this device's next save,
    exactly as today. **The cost is a network lookup and not a row**: the MARK
    survives its own merge, the Cards list is built by walking the marks
    (`cardItems()` in the layer, not the sidecar), and `fillCards()` re-resolves
    any card-coloured mark that has no definition on load. That is the whole
    reason `write_cards` was left out of the first shrink-guard sweep and the
    reason the comment there calls cards "DERIVED and recreatable". **The two
    kinds that are NOT recreatable are the two this merge rescues.**

    🔴 **WHAT WOULD INVALIDATE IT:** a client that rewrites a card's key after
    creation; a second place minting `c` stamps with a different generator; or
    two devices minting a stamp in the same millisecond, since the stamp is
    `Date.now().toString(36)` with no random suffix. ⚠️ **The last one is real
    and it is narrower than it looks in only one direction** -- it needs two
    devices to press Card inside one millisecond -- **but it fails as a
    deletion, not a duplicate.** It is filed at the bottom of the build lane
    rather than fixed here, because changing the generator is a client change
    whose old data cannot be migrated and it is not what this entry asked for.

    ⚠️ `from` is deliberately NOT consulted. The two are in agreement today
    (`markless()` is the only minter of a stamp and it is the only source of
    `chat` and `typed`), and the server CLAMPS an unknown `from` to `"mark"`,
    so reading `from` would let a malformed row change its own identity. The
    key is what the file is keyed by; it is what decides.
    """
    k = str(key)
    return k if CARD_STAMP_RE.match(k) else None


def merge_cards(disk, sent, base):
    """Everything the writer sent, plus the STAMPED cards on disk it never saw.

    🔴 THE SAME RULE AS THE OTHER FOUR: **a write may delete only what the
    writer knows about.** A browser's knowledge is what it is sending plus
    `base`, the keys it last agreed the file held.

    🔴 **AND IT IS THE ONE SIDECAR THAT DOES NOT USE `_unseen`, for a reason
    that is structural rather than stylistic.** `_unseen` COUNTS rather than
    testing set membership, because in a list one key can legitimately name two
    rows (a phrase repeated in one block, highlighted twice). **A card is stored
    in an OBJECT**, so a key names exactly one row by construction and the
    multiplicity `_unseen` exists for cannot arise. Reusing it would mean
    flattening a dict to a list and back to make a counter do nothing.
    ⚠️ `_agreed_from` IS shared, because the reading of `base` -- absent means
    agreed to nothing, malformed is an error and never a fallback -- must be
    identical across all six or the drift this project keeps paying for gets in
    through the one that spelled it itself.

    🔴 A base-less writer adopts everything it can NAME and deletes nothing it
    can name, exactly as for the other four; `card_key` argues which rows those
    are and what the unnameable ones cost.
    """
    agreed = _agreed_from(base)
    held = disk.get("cards")
    if not isinstance(held, dict):
        return sent, 0
    adopted = 0
    for key, row in held.items():
        k = card_key(key)
        if not k:
            # A mark card. Neither adopted nor rescued, which is today's
            # behaviour for every card and is argued in full at `card_key`.
            continue
        if k in sent:
            # The writer holds this one. The writer's own row wins, as it does
            # in all five: the reader is looking at that card right now.
            continue
        if agreed[k]:
            # The writer knew it and did not send it. That is a deletion, and
            # deleting still works by the same rule that makes adopting work.
            continue
        # 🔴 Cleaned again on the way back in. It was cleaned when it was
        # written, so this is defence rather than repair, and it costs nothing:
        # the alternative is that the one path which does not validate is the
        # one carrying rows this writer has never seen.
        sent[k] = clean_card(row)
        adopted += 1
    return sent, adopted


def clean_card(row):
    """One card, clamped. Shared by the payload and by anything the merge
    adopts, so a row cannot enter the file by a route that validates less."""
    if not isinstance(row, dict):
        return None
    src = str(row.get("from") or "")
    return {
        "term": str(row.get("term") or "")[:MAX_CARD_TEXT],
        "def": str(row.get("def") or "")[:MAX_CARD_TEXT],
        "source": str(row.get("source") or "")[:120],
        "url": str(row.get("url") or "")[:500],
        # R44 added "typed": a card entered by hand in the panel. Clamping
        # an unknown value to "mark" was the safe default until it silently
        # orphaned every typed card on reload, because a "mark" card whose
        # key matches no mark is dropped from the list by design.
        "from": src if src in ("mark", "chat", "typed") else "mark",
        "at": str(row.get("at") or "")[:40],
    }


def write_cards(cfg, doc_id, payload):
    cards = payload.get("cards")
    if not isinstance(cards, dict):
        raise ValueError("cards must be an object")
    if len(cards) > MAX_CARDS:
        raise ValueError("too many cards")
    clean = {}
    for key, row in cards.items():
        got = clean_card(row)
        if got is None or not CARD_KEY_RE.match(str(key)):
            continue
        clean[str(key)] = got
    path = sidecar_path(cfg, doc_id, "cards")

    if payload.get("base") is None:
        # 🔴 THE SAME LOG LINE AS THE OTHER FOUR, and for the same reason: every
        # internal caller says what it knew, so a base-less write is by
        # construction a page older than this merge, still open somewhere and
        # still saving. It is how "are there stale clients out there" gets an
        # answer off a log rather than an argument about how long a tab lives.
        log(cfg, "cards %s: a write with NO BASE, from a page older than the "
                 "merge. It adopts what it never saw and can NAME, and deletes "
                 "nothing it can name." % doc_id)

    # 🔴 READ, MERGE, WRITE, under one lock, for the fifth and last time and for
    # the reason the marks path records: this is read-modify-write on one file
    # inside a threading server, so two devices saving together would both read
    # the old file and the second would erase what the first adopted.
    #
    # ⚠️ `MAX_CARDS` above is a limit on what a client may SEND, not on what the
    # file may hold afterwards, exactly as `write_additions` argues for its 500.
    # Truncating after the merge would drop the adopted rows, which are the
    # other device's typed and chat cards and the one thing here that cannot be
    # looked up again. It is bounded: a merge only ever adopts rows already on
    # the disk, so the file settles at the union of the devices rather than
    # growing on every write.
    with CARDS_LOCK:
        clean, adopted = merge_cards(read_json_sidecar(path, {}, cfg),
                                     clean, payload.get("base"))
        doc = {"doc": doc_id, "cards": clean,
               "saved": datetime.now(timezone.utc).isoformat(timespec="seconds")}

        # 🔴 The same guard, and the reasoning that put it here is in the
        # helper: cards are DERIVED and recreatable, which is why they were left
        # out originally, and that argument assumes the network answers and the
        # source has not moved. `Clear all` is what settled it.
        #
        # 🟢 IT NOW PASSES AN IDENTITY, and it is the THIRD call site to manage
        # one. ⚠️ **It covers two thirds of the file and the guard says so**: a
        # stamped card carries its key as its identity, a mark card buckets
        # under `NO_IDENTITY`, so a write that SWAPS one typed card for another
        # at equal count is no longer silent and the same swap between two mark
        # cards still is. **That asymmetry is the honest report of what an
        # identity exists for**, not an unfinished job, and both halves are
        # measured in `test_guard_compares.py`.
        #
        # 🔴 Against the MERGED document, because that is what is about to be
        # written. Comparing the payload would measure the disk against
        # something that never reaches it.
        keep_the_losing_copy(cfg, path, "cards", doc_id,
                             lambda d: len(d.get("cards") or {}),
                             doc,
                             keys=lambda d: [card_key(k)
                                             for k in (d.get("cards") or {})])
        write_json_sidecar(path, doc)
    # `adopted` is reported on every save, zero included, so its absence is
    # visible: a silent success and a merge that never ran look identical
    # otherwise, which is how a check stops being one.
    return {"ok": True, "doc": doc_id, "cards": len(clean), "adopted": adopted}


# --- R7: bookmarks, which point at a whole block rather than a range -------------------
#
# Asked 2026-08-13: "I wonder if we should also add bookmarks where I can bookmark a whole
# paragraph or image or a section or a chat."
#
# 🔴 A bookmark is not a highlight with different styling, and that is why it gets its own
# store. A highlight is a RANGE, {block, start, end}, and it exists to say something about
# the words inside it. A bookmark is a POINTER, {block}, and it exists to be come back to.
# Putting a range-shaped record in the marks file with the range left blank would make
# every consumer of that file, including the vault export and the block-numbering check,
# handle a shape it was never written for.
#
# Its own sidecar also keeps it out of the file already lost once. Unlike the cards in
# read_cards, though, a bookmark is NOT derived data and cannot be recreated by looking
# something up, so it is his content and belongs in the backup routine beside the marks.

MAX_BOOKMARKS = 500


def read_bookmarks(cfg, doc_id):
    doc = read_json_sidecar(sidecar_path(cfg, doc_id, "bookmarks"),
                            {"doc": doc_id, "marks": []})
    rows = doc.get("marks")
    return {"ok": True, "doc": doc_id, "marks": rows if isinstance(rows, list) else []}


def write_bookmarks(cfg, doc_id, payload):
    rows = payload.get("marks")
    if not isinstance(rows, list):
        raise ValueError("marks must be a list")
    if len(rows) > MAX_BOOKMARKS:
        raise ValueError("too many bookmarks")
    clean = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            b = int(row.get("b"))
        except (TypeError, ValueError):
            continue
        if b < 0 or b > 100000:
            continue
        clean.append({
            "b": b,
            "t": str(row.get("t") or "")[:400],
            "at": str(row.get("at") or "")[:40],
        })
    # 🔴 The four newest sidecars inherited no guard: audit §3, 2026-09-01.
    # The load swallows its own failure and leaves the array at `[]`, so one
    # failed GET plus one addition replaces the file with a single item.
    bpath = sidecar_path(cfg, doc_id, "bookmarks")

    # 🔴 READ, MERGE, WRITE, under the same lock as the marks path and for the
    # same reason: this is read-modify-write on one file inside a threading
    # server, so two devices saving together would both read the old file and
    # the second would erase what the first adopted.
    if payload.get("base") is None:
        # 🔴 THE SAME LOG LINE AS THE MARKS PATH, and for the same reason: every
        # internal caller says what it knew, so a base-less write is by
        # construction a page older than this merge, still open somewhere and
        # still saving. It is how "are there stale clients out there" gets an
        # answer off a log rather than an argument about how long a tab lives.
        log(cfg, "bookmarks %s: a write with NO BASE, from a page older than "
                 "the merge. It adopts what it never saw and can NAME, and "
                 "deletes nothing it can name."
            % doc_id)
    with BOOKMARKS_LOCK:
        disk = read_json_sidecar(bpath, {})
        clean, adopted = merge_bookmarks(disk, clean, payload.get("base"))
        keep_the_losing_copy(cfg, bpath, "bookmarks", doc_id,
                             lambda d: len(d.get("marks") or []),
                             {"marks": clean},
                             # 🔴 WITHOUT FILTERING `None`. An unnameable row is
                             # bucketed under NO_IDENTITY so losing one is still
                             # visible; a filtered list can never produce the
                             # falsy key that bucketing watches for.
                             keys=lambda d: [
                                 bookmark_key(r) if isinstance(r, dict) else None
                                 for r in (d.get("marks") or [])])
        write_json_sidecar(bpath,
                           {"doc": doc_id, "marks": clean,
                            "saved": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    return {"ok": True, "doc": doc_id, "marks": len(clean), "adopted": adopted}


def merge_bookmarks(disk, sent, base):
    """Everything the writer sent, plus the bookmarks on disk it has never seen.

    🔴 THE SAME RULE AS THE MARKS MERGE -- **a write may delete only what the
    writer knows about** -- reached through the same counting helper, with
    `bookmark_key` as the identity. **The mechanism generalises; the identity
    does not**, and `bookmark_key` argues its own case.

    ⚠️ THE COUNTING IN `_unseen` IS INHERITED HERE AND IS **NOT** LOAD-BEARING,
    which is written down because the first version of this docstring claimed it
    was and was wrong. **A mutation replacing the counting with set membership
    killed no test**, and rather than invent a test to justify the code, the
    claim was measured.

    🟢 **Two bookmark rows can only share a key if they share BOTH block and
    text**, and that state turns out to be UNREACHABLE: a device never CREATES a
    second bookmark on a block (pressing Bookmark on one that already carries a
    row removes instead of adding), and when a second device saves the same
    block with the same text, the merge sees that the writer HOLDS that key and
    does not adopt the disk's copy back. **Driven through the real write path
    with two base-less devices, the file stays at one row.**

    ⚠️ **THE REASON IS CREATION, NOT REMOVAL, AND THAT DISTINCTION IS NEW.** This
    argument used to be written as "the toggle filters by `b`", which was true of
    the client at the time and is no longer: since 2026-09-04 the page removes
    the row it drew rather than every row on the block, because removing by block
    was deleting the duplicate this merge exists to create. **The unreachability
    survives that change** -- it never depended on the removal rule -- but a
    reader checking the old sentence against the page would not find it.

    🔴 **So the invariant is what gets pinned, not the preference**: no two rows
    on the disk ever share a key. **What would make the counting matter is a
    client that allows two bookmarks on one block** -- the same change that
    `bookmark_key` names as invalidating the key itself. Until then the shared
    helper is used for having ONE copy of the merge rule, not because bookmarks
    need its allowance.

    🔴 A base-less writer adopts everything IT CAN NAME, and deletes nothing it
    can name, exactly as for marks. That is not a compromise: it is the only
    correct reading of the only thing an absent base can now mean, which is
    "this client is too old to say". ⚠️ The qualifier was missing until
    2026-09-08 and it is load-bearing: `_unseen` skips a disk row
    `bookmark_key` cannot name, so such a write does drop it. Why that is the
    right trade is at `_unseen`.
    """
    agreed = _agreed_from(base)
    rows = disk.get("marks")
    keep = _unseen(rows if isinstance(rows, list) else [], sent, agreed, bookmark_key)
    if not keep:
        return sent, 0
    return sent + keep, len(keep)


def merge_chatmarks(disk, sent, base):
    """Everything the writer sent, plus the chat marks on disk it never saw.

    🔴 THE SAME RULE, THE SAME HELPER, A DIFFERENT IDENTITY. `chatmark_key`
    argues its own case; the mechanism is `_unseen`, which is shared so there is
    ONE copy of "a write may delete only what the writer knows about".

    🟢 **THE COUNTING IS NOT LOAD-BEARING HERE EITHER, and it is said rather
    than assumed** -- the bookmark docstring claimed it was and had to be
    retracted after a mutation killed nothing. **Two chat marks can share a key
    only by sharing chat, turn, BOTH offsets and text, which is the same mark.**
    So the invariant is that no two rows on disk share a key, and that is what
    the tests pin.

    ⚠️ **AND THERE IS A REASON THIS SIDECAR IS SAFER THAN THE OTHERS**: measured
    at source, **the client has no way to DELETE a chat mark at all** (a push at
    creation, a whole-array assignment at load, and no filter anywhere). So the
    deletion this merge exists to prevent cannot be reached by a reader pressing
    anything. 🟢 **`test_chatmark_merge.py` pins that by counting the ASSIGNMENTS
    to `chatMarks` rather than by matching one spelling of a filter**, so a delete
    added later fails it however it is written. 🔴 **The merge is still needed**: a device that loaded before the
    other device's mark existed still POSTs an array without it, and today that
    write wins.
    """
    agreed = _agreed_from(base)
    rows = disk.get("marks")
    keep = _unseen(rows if isinstance(rows, list) else [], sent, agreed, chatmark_key)
    if not keep:
        return sent, 0
    return sent + keep, len(keep)


def chat_key(row):
    """What identifies a SAVED CONVERSATION across two devices.

    🔴 **`ts` IS NOT IT, AND THAT IS THE WHOLE FINDING OF THIS SIDECAR.** The
    obvious move is to copy the kept-notes answer, `a:<b>:<ts>`, which is the
    block plus the instant the reader pressed Keep. It does not transfer, and it
    fails in the direction that deletes. **A kept note never changes; a
    conversation grows**, and the client rewrites `c.ts = Date.now()` on every
    user turn and every answer that lands (three sites in `local-layer.html`).
    `ts` is LAST ACTIVITY, not creation: it is what the History list sorts on.
    So a key built on it names a different row after every reply.

    🔴 **`id` IS NOT IT EITHER, and this is the fourth sidecar to carry that same
    trap.** `newChat` mints `var id = 1; chats.forEach(c => if (c.id >= id) id =
    c.id + 1)` from THIS device's list, so two devices each starting their first
    conversation both call it `1`.

    🟢 **SO: `cid`, minted once at creation and never written again.** It is the
    only field of a conversation that is both device-independent and immutable,
    because it is the only one created to be. New conversations carry it.

    🟢 **AND A DERIVED KEY FOR THE ONES THAT ALREADY EXIST**, which cannot be
    given a `cid` retrospectively without one device inventing an identity the
    other cannot guess. It is built from what a conversation cannot change: the
    block it was about, the scope it was asked at, and the reader's FIRST
    question. Appending turns does not touch any of them.

    🔴 **WHAT WOULD INVALIDATE IT**: a client that rewrites `cid`, or that edits
    a conversation's first question; and, for the derived half only, two
    genuinely different conversations begun on the same block at the same scope
    with a byte-identical first question, which the merge would then treat as
    one. **That collision loses a conversation, so the derived half is a
    fallback for existing data and not the answer**: every conversation made
    from now on has a `cid` and cannot collide.
    """
    if not isinstance(row, dict):
        return None
    cid = row.get("cid")
    if isinstance(cid, str) and CHAT_CID_RE.match(cid):
        return "k:" + cid
    turns = row.get("turns")
    first = ""
    if isinstance(turns, list):
        for t in turns:
            if isinstance(t, dict) and t.get("role") == "user":
                first = str(t.get("text") or "")[:300]
                break
    if not first:
        # 🔴 Unnameable, so `_unseen` skips it: neither adopted nor rescued.
        # The trade is argued in `_unseen`, and adopting a row nothing can match
        # would duplicate it on EVERY write until the cap refused the save.
        return None
    b = row.get("b")
    scope = str(row.get("scope") or "")
    return "d:%s:%s:%s" % ("" if b is None else b, scope, first)


def merge_chats(disk, sent, base):
    """A conversation the writer never knew about survives its write.

    🔴 THE ONE SIDECAR WHERE A LOST ROW IS UNRECOVERABLE, which is why it is the
    last of the five to be built and was not rushed. Marks, bookmarks and cards
    can be made again by reading the lesson again. **The answers in a
    conversation were written once, against a page that may since have been
    rewritten**, and `write_chats` already keeps the losing copy for exactly
    that reason. This stops the loss instead of archiving it.
    """
    agreed = _agreed_from(base)
    rows = disk.get("chats")
    keep = _unseen(rows if isinstance(rows, list) else [], sent, agreed, chat_key)
    if not keep:
        return sent, 0
    return sent + keep, len(keep)


def merge_chatbooks(disk, sent, base):
    """The other half of the same sidecar, and it needs its own base.

    🔴 TWO COLLECTIONS, TWO BASES, and that is not tidiness. A single base
    covering both would let a writer that knew about the marks be treated as
    knowing about the conversations, so a page that had loaded one and not the
    other could delete the half it never saw. **The two are agreed to
    separately because they are known separately.**
    """
    agreed = _agreed_from(base)
    rows = disk.get("chats")
    keep = _unseen(rows if isinstance(rows, list) else [], sent, agreed, chatbook_key)
    if not keep:
        return sent, 0
    return sent + keep, len(keep)


# --- the chat anchor: marks that live in a conversation, not on the page ---------------
#
# This is the remaining half of BOTH R5 ("highlight things ... from inside the chat") and
# R7 ("bookmark a whole paragraph or image or a section or a chat"). They stopped at the
# same wall and it is one job: everything the reader can mark so far anchors to a block
# index inside .wrap, and a chat answer is not in .wrap and has no stable block. It is
# re-rendered from the chats store every time the thread is drawn.
#
# So a chat mark names {chat id, turn index, start, end} against that turn's own text, and
# a chat bookmark names a conversation and nothing else, because "bookmark a chat" means
# the whole thing rather than a place inside it.
#
# 🔴 Its own sidecar again, for the reason the bookmarks have one: this is his content,
# it is not derived, and it must not share a file with either the marks or the chats. In
# particular NOT inside -chats.json, whose entries are rewritten wholesale every time a
# conversation gains a turn.

# A conversation's own id, minted by the client at creation and never rewritten.
# Deliberately opaque and deliberately not a number: the two id traps this
# project has already paid for were both per-device ORDINALS, and a value that
# cannot be counted up to cannot be minted the same way twice.
CHAT_CID_RE = re.compile(r"^[a-z0-9]{4,32}$")

MAX_CHAT_MARKS = 1000


def read_chatmarks(cfg, doc_id):
    doc = read_json_sidecar(sidecar_path(cfg, doc_id, "chatmarks"),
                            {"doc": doc_id, "marks": [], "chats": []})
    marks = doc.get("marks")
    chats = doc.get("chats")
    return {"ok": True, "doc": doc_id,
            "marks": marks if isinstance(marks, list) else [],
            "chats": chats if isinstance(chats, list) else []}


def write_chatmarks(cfg, doc_id, payload):
    marks = payload.get("marks")
    chats = payload.get("chats")
    if not isinstance(marks, list) or not isinstance(chats, list):
        raise ValueError("marks and chats must both be lists")
    if len(marks) > MAX_CHAT_MARKS or len(chats) > MAX_CHAT_MARKS:
        raise ValueError("too many chat marks")

    clean = []
    for row in marks:
        if not isinstance(row, dict):
            continue
        try:
            c, i, s, e = int(row.get("c")), int(row.get("i")), int(row.get("s")), int(row.get("e"))
        except (TypeError, ValueError):
            continue
        # An empty or reversed range would paint nothing and confuse the repaint.
        if i < 0 or s < 0 or e <= s or e - s > 20000:
            continue
        clean.append({"c": c, "i": i, "s": s, "e": e,
                      "t": str(row.get("t") or "")[:400],
                      "at": str(row.get("at") or "")[:40]})

    seen, cleanchats = set(), []
    for row in chats:
        try:
            c = int(row.get("c")) if isinstance(row, dict) else int(row)
        except (TypeError, ValueError):
            continue
        if c in seen:
            continue
        seen.add(c)
        # 🔴 `k` CARRIED THROUGH, or the fix dies on the first save. It is the
        # conversation's device-independent identity, and it is what lets a
        # bookmark adopted from the other Mac open the conversation it names
        # rather than whatever this Mac holds at the same ordinal. A field the
        # writer strips is a field that does not exist.
        k = (row or {}).get("k") if isinstance(row, dict) else None
        cleanchats.append({"c": c,
                           "k": str(k)[:400] if isinstance(k, str) and k else None,
                           "t": str((row or {}).get("t") or "")[:300] if isinstance(row, dict) else "",
                           "at": str((row or {}).get("at") or "")[:40] if isinstance(row, dict) else ""})

    # Audit §3: same inherited gap as bookmarks. Both halves count, because a
    # write that keeps the marks and drops the chats is still a loss.
    cmpath = sidecar_path(cfg, doc_id, "chatmarks")

    # 🔴 TWO BASES, because this sidecar carries two independent collections and
    # a writer can know about one and not the other. `base` is
    # `{"marks": [...], "chats": [...]}`; either half may be absent, and an
    # absent half means the same thing an absent base has always meant here --
    # "too old to say" -- so it adopts everything IT CAN NAME and deletes
    # nothing it can name. The qualifier is load-bearing and `_unseen` says why.
    base = payload.get("base")
    if not isinstance(base, dict):
        base = {}
        base_marks = base_chats = None
    else:
        base_marks, base_chats = base.get("marks"), base.get("chats")

    if payload.get("base") is None:
        # 🔴 The same log line as the marks and bookmarks paths, for the same
        # reason: a base-less write is by construction a page older than this
        # merge, still open somewhere and still saving, and this is how "are
        # there stale clients out there" gets an answer off a log rather than an
        # argument about how long a tab lives.
        log(cfg, "chatmarks %s: a write with NO BASE, from a page older than "
                 "the merge. It adopts what it never saw and can NAME, and "
                 "deletes nothing it can name."
            % doc_id)

    # 🔴 READ, MERGE, WRITE under one lock: read-modify-write on one file inside
    # a threading server, so two devices saving together would otherwise both
    # read the old file and the second would erase what the first adopted.
    with CHATMARKS_LOCK:
        disk = read_json_sidecar(cmpath, {})
        clean, adopted_marks = merge_chatmarks(disk, clean, base_marks)
        cleanchats, adopted_chats = merge_chatbooks(disk, cleanchats, base_chats)
        keep_the_losing_copy(cfg, cmpath, "chatmarks", doc_id,
                             lambda d: len(d.get("marks") or []) + len(d.get("chats") or []),
                             {"marks": clean, "chats": cleanchats},
                             keys=chatmarks_identity)
        write_json_sidecar(cmpath,
                           {"doc": doc_id, "marks": clean, "chats": cleanchats,
                            "saved": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    return {"ok": True, "doc": doc_id, "marks": len(clean), "chats": len(cleanchats),
            "adopted": adopted_marks + adopted_chats}


def read_chats(cfg, doc_id):
    """Saved Explain conversations for one note. Same sidecar pattern as marks
    and additions: a plain JSON file next to the note, so a conversation is
    still there next week, is readable without the page, and travels with the
    courses folder however that folder is synced."""
    return read_json_sidecar(sidecar_path(cfg, doc_id, "chats"),
                             {"doc": doc_id, "chats": []})


def write_chats(cfg, doc_id, payload):
    chats = payload.get("chats")
    if not isinstance(chats, list):
        raise ValueError("chats must be a list")

    clean = []
    for c in chats[:200]:
        if not isinstance(c, dict):
            continue
        turns = c.get("turns")
        if not isinstance(turns, list):
            continue
        # 🔴 `cid` is carried through verbatim. A field the writer strips is a
        # field that does not exist: the identity would survive exactly until
        # the first save.
        cid = c.get("cid")
        clean.append({
            "id": c.get("id"),
            "cid": cid if (isinstance(cid, str) and CHAT_CID_RE.match(cid)) else None,
            "ts": c.get("ts"),
            "title": str(c.get("title") or "")[:160],
            "term": str(c.get("term") or "")[:400],
            "context": str(c.get("context") or "")[:3000],
            "scope": (str(c.get("scope") or "")[:10]
                      if str(c.get("scope") or "") in ("topic", "course") else ""),
            "b": c.get("b"),
            # Where on the page it was about, so reopening it next week can mark
            # the passage again.
            "segments": [{"b": s.get("b"), "s": s.get("s"), "e": s.get("e")}
                         for s in (c.get("segments") or [])[:40]
                         if isinstance(s, dict)],
            "turns": [{"role": ("user" if t.get("role") == "user" else "claude"),
                       "text": str(t.get("text") or "")[:20000]}
                      for t in turns[:200] if isinstance(t, dict)],
        })
    # Audit §3. A conversation is not regenerable: the answers were written
    # once, against a page that may since have been rewritten.
    cpath = sidecar_path(cfg, doc_id, "chats")

    if payload.get("base") is None:
        # 🔴 THE SAME LOG LINE AS THE OTHER FOUR, and for the same reason: a
        # base-less write is by construction a page older than this merge, still
        # open somewhere and still saving. It is how "are there stale clients out
        # there" gets an answer off a log rather than an argument.
        log(cfg, "chats %s: a write with NO BASE, from a page older than the "
                 "merge. It adopts what it never saw and can NAME, and deletes "
                 "nothing it can name." % doc_id)

    # 🔴 READ, MERGE, WRITE UNDER ONE LOCK. This is read-modify-write on one
    # file inside a threading server: outside the lock, two devices saving
    # together both read the old file and the second erases what the first
    # adopted, which is the very loss this merge exists to stop.
    with CHATS_LOCK:
        disk = read_json_sidecar(cpath, {})
        clean, adopted = merge_chats(disk, clean, payload.get("base"))
        # 🔴 THE CAP IS APPLIED AFTER THE MERGE, not before it. Refusing the
        # whole save because rescuing rows crossed 200 would turn a two-device
        # collision into a lesson that cannot be saved at all, which `_unseen`
        # already argues is the worse outcome.
        #
        # 🔴 AND IT DROPS THE LEAST RECENTLY ACTIVE, NOT THE FIRST OR THE LAST.
        # Both of those are wrong here and a slice was the first thing written:
        # after the merge the list is the writer's own rows followed by the rows
        # rescued from disk, so `clean[-200:]` throws away the conversation the
        # reader just had in order to keep the other Mac's, and `clean[:200]`
        # throws away everything rescued. A test caught it.
        #
        # 🟢 `ts` IS THE RIGHT FIELD FOR THIS, and it is the same field that is
        # WRONG as an identity: it is last activity, which is exactly what
        # "least recently used" needs and exactly what an identity must not be.
        # The History list already sorts on it for the same reason.
        if len(clean) > 200:
            def _recency(c):
                v = c.get("ts")
                return v if isinstance(v, (int, float)) else 0
            survivors = set(id(c) for c in sorted(clean, key=_recency,
                                                  reverse=True)[:200])
            clean = [c for c in clean if id(c) in survivors]
        doc = {
            "doc": doc_id,
            "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "chats": clean,
        }
        # 🟢 IT PASSES AN IDENTITY, added 2026-09-09 with the CARDS merge and
        # not with this one, which is the finding: `chat_key` shipped hours
        # earlier and the guard beside it was left counting. **The sidecar where
        # a lost row is unrecoverable was the one still measured by a tally**,
        # so a write that swapped one conversation for another at equal count
        # was silent here and caught in `marks`. The gap was invisible because
        # both halves were individually correct.
        #
        # ⚠️ ONE ENTRY PER ROW, `None` INCLUDED. The guard buckets an unnameable
        # conversation under `NO_IDENTITY` so that losing one is still visible;
        # filtering here would make a conversation with no question yet free to
        # disappear. It is the asymmetry `_unseen` documents, and it must not be
        # tidied into agreement.
        keep_the_losing_copy(cfg, cpath, "chats", doc_id,
                             lambda d: len(d.get("chats") or []),
                             doc,
                             keys=lambda d: [chat_key(c) if isinstance(c, dict)
                                             else None
                                             for c in (d.get("chats") or [])])
        write_json_sidecar(cpath, doc)
    return {"ok": True, "chats": len(clean), "adopted": adopted}


# --- R8 and R9: files attached to a lesson ---------------------------------------------
#
# Asked 2026-08-13: "The ability to open associated slides or any other associated file or
# set of files that's associated with that lesson in a side panel or a new tab", and
# "Ability to load and attach files to each lesson."
#
# 🔴 Where they live is EH'S decision, 2026-08-14: "a new files directory, which keeps
# the data clean. Maybe we call it resources." So `resources/`, not `notes/`, and not
# beside the marks. Keying them by lesson underneath it is mine, and it is in the review
# queue: a file's whole purpose here is to belong to a lesson, so `resources/<DOC>/<name>`
# needs no manifest to say which lesson a file is for. The directory IS the record.
#
# 🔴 There is deliberately no index file. materials.json is derived and can be rebuilt;
# a manifest of attachments could not be, so it would be a second source of truth that
# silently drifts the first time a file is moved in Finder. Listing the directory cannot
# drift, and it means he can drop a PDF into the folder from Finder and have it appear.
# The cost is that a file has no title beyond its filename and no recorded "added by",
# which is the right trade for something whose whole job is to be a file.
#
# The lecture materials themselves stay where they are: Google Drive, read only, never
# written. Nothing here copies them.

# A filename arrives over HTTP and is used to build a path, so it is checked properly.
#
# 🔴 An ASCII whitelist was written first and was wrong: it rejected `café.pdf`, and half
# the reading list on this module has an accented author name in it. The rule that
# replaced it names the characters that are dangerous HERE rather than guessing at a safe
# set, which it can afford to do because a resource filename never reaches a shell: it is
# used to build a path and a URL, and the URL is quoted at both ends.
#
# What is left after that: separators (which would climb out of the directory), the two
# characters that end a path on other filesystems, and control characters including NUL
# (which truncate a path in C and are how a check like this gets walked past).
RESOURCE_BAD_CHARS = re.compile(r"[/\\:\x00-\x1f\x7f]")
RESOURCE_MAX_NAME = 120
RESOURCE_MAX_BYTES = 64 * 1024 * 1024

# A lesson is HTML and its figures are links, so it is tens of kilobytes.
# The limit is here to refuse a 2GB body before reading it into memory,
# not to be a budget anybody has to think about.
LESSON_MAX_BYTES = 16 * 1024 * 1024

# A list of links for a whole course is a few kilobytes.
LINKS_MAX_BYTES = 2 * 1024 * 1024


def resources_base(cfg):
    """The root that holds attachments, before any course scoping.

    `module_cfg` rewrites `resources_dir` to a folder inside this one and keeps
    the unscoped value here, so scoping an already-scoped cfg cannot nest. A cfg
    that has never been through it has only the one key and both readings agree.
    """
    return Path(cfg.get("resources_base") or cfg.get("resources_dir")
                or (REPO / "resources")).expanduser()


def resources_root(cfg):
    """Where THIS request's attachments live: one folder per course wherever
    courses are plural. See module_cfg for why that is not optional."""
    return Path(cfg.get("resources_dir") or (REPO / "resources")).expanduser()


def share_destination(cfg):
    """The folder the share button's zip lands in.

    `share_dest` when the config sets it, else the Desktop, else the home
    folder: the last two are what `/api/share` always did, and a config that
    says nothing gets exactly that. The configured path need not exist yet;
    `lesson_packs.export` creates what it writes into. See `DEFAULT_CONFIG`
    for why the key exists at all.
    """
    want = str(cfg.get("share_dest") or "").strip()
    if want:
        return Path(want).expanduser()
    dest = Path.home() / "Desktop"
    if not dest.is_dir():
        dest = Path.home()
    return dest


def resource_url(cfg, doc_id, name):
    """The address a browser asks for one attachment at.

    🔴 It names the course wherever courses are plural, and that is not
    decoration. The FOLDER is per course now, but `/resources/<DOC>/<name>`
    names no course, so the server would have to work out which one from the
    Referer. That is fine for a fetch the reader makes and wrong for a link
    opened in a new tab, pasted to somebody, or bookmarked, which is exactly
    what the Files tab's arrow is. The address carries the course instead, and
    the bare form still resolves for the single-course world it was built for.
    """
    quoted = urllib.parse.quote(name)
    mid = str(cfg.get("module") or "")
    if cfg.get("courses_dir") is not None and mid:
        return "/m/%s/resources/%s/%s" % (urllib.parse.quote(mid), doc_id, quoted)
    return "/resources/%s/%s" % (doc_id, quoted)


def safe_resource_name(name, normalise=False):
    """The filename to use, or None if it is not one we are willing to touch.

    Only the basename survives, so a path arriving in the name is discarded rather
    than rejected: browsers send bare names, and a name with a separator in it is
    far more likely to be a badly-behaved client than an attack.

    🔴 `normalise` is only set when WRITING, and the asymmetry is deliberate. macOS
    stores a filename in NFD and hands it back in NFD, while browsers send NFC, so
    normalising on the way in keeps two attachments of the same file from becoming
    two files. Doing it on the way OUT as well would mean looking up a name this
    server never wrote: it happens to work here because APFS compares
    normalisation-insensitively, but that is the filesystem being forgiving rather
    than the code being right. On a lookup the name from the listing is used as it
    came, and resolve_resource() below tries the other form if it has to.
    """
    name = (name or "").replace("\\", "/").split("/")[-1].strip()
    if normalise:
        name = unicodedata.normalize("NFC", name)
    if not name or name in (".", "..") or name.startswith("."):
        return None
    if ".." in name or len(name) > RESOURCE_MAX_NAME:
        return None
    if RESOURCE_BAD_CHARS.search(name):
        return None
    return name


def resolve_resource(cfg, doc_id, name):
    """The file on disk for a listed name, or None. Containment is checked here so
    that every caller gets it, rather than each remembering to."""
    safe = safe_resource_name(name)
    if not safe or not DOC_ID_RE.match(doc_id or ""):
        return None
    root = resources_root(cfg).resolve()
    here = (root / doc_id)
    for form in (safe, unicodedata.normalize("NFC", safe), unicodedata.normalize("NFD", safe)):
        try:
            target = (here / form).resolve()
        except OSError:
            continue
        if here.resolve() in target.parents and target.is_file():
            return target
    return None


def resource_dir(cfg, doc_id, make=False):
    if not DOC_ID_RE.match(doc_id or ""):
        raise ValueError("bad doc id")
    d = resources_root(cfg) / doc_id
    if make:
        d.mkdir(parents=True, exist_ok=True)
    return d


def resource_folder_label(cfg, doc_id):
    """What to call this lesson's attachment folder in the reader.

    🔴 Derived from the folder actually used, never spelled out again. The Files
    pane printed `resources/<DOC>` as a literal, which was true until courses
    got a folder each on 2026-08-30 and then quietly was not: the sentence was
    still there, still correctly spelled, and naming a directory that no longer
    existed. Same shape as the "in Drive" wording 2072436 deleted, and the
    reason this returns a string instead of the pane building one.
    """
    d = resource_dir(cfg, doc_id)
    base = resources_base(cfg)
    try:
        return "/".join((base.name,) + d.relative_to(base).parts)
    except ValueError:
        return str(d)


def resource_kind(suffix):
    """How the reader should try to show it. The client decides the markup; this
    only says which of the four shapes it is, so the two stay in step."""
    s = suffix.lower()
    if s == ".pdf":
        return "pdf"
    if s in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".avif"):
        return "image"
    if s in (".txt", ".md", ".csv", ".tsv", ".json", ".log"):
        return "text"
    if s in (".htm", ".html"):
        return "page"
    if s in (".mp4", ".webm", ".m4v"):
        return "video"
    if s in (".mp3", ".m4a", ".wav", ".aac"):
        return "audio"
    return "other"


# --- the manifest, added 2026-08-15 on EH's answer ------------------------------
#
# R8 and R9 shipped with no index at all, on the argument that a manifest is authored
# and therefore drifts, while a directory listing cannot. He asked for one anyway, and
# he is right that a file which is only ever its filename is a poor way to keep a paper.
#
# 🔴 So it is built as a SIDECAR, not as a replacement, and that is what keeps the
# original argument satisfied. The directory is still the list of what exists. The
# manifest only decorates: a title, a note, and an order. A file in the folder with no
# manifest row still appears, so dropping a PDF in from Finder goes on working. A
# manifest row whose file has gone is dropped on read and never shown. Neither half can
# make the other lie.
def manifest_path(cfg, doc_id):
    return resource_dir(cfg, doc_id) / "_about.json"


def read_manifest(cfg, doc_id):
    p = manifest_path(cfg, doc_id)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    files = data.get("files") if isinstance(data, dict) else None
    return files if isinstance(files, dict) else {}


def write_manifest(cfg, doc_id, name, title=None, note=None, order=None):
    """Set or clear one file's decoration. Returns the row as stored."""
    if resolve_resource(cfg, doc_id, name) is None:
        raise ValueError("no such file")
    safe = safe_resource_name(name)
    p = manifest_path(cfg, doc_id)
    with WRITE_LOCK:
        files = read_manifest(cfg, doc_id)
        row = files.get(safe) or {}
        for field, value in (("title", title), ("note", note)):
            if value is None:
                continue
            value = re.sub(r"\s+", " ", str(value)).strip()[:200]
            if value:
                row[field] = value
            else:
                row.pop(field, None)   # an emptied box removes the field
        if order is not None:
            try:
                row["order"] = int(order)
            except (TypeError, ValueError):
                pass
        if row:
            files[safe] = row
        else:
            files.pop(safe, None)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"doc": doc_id, "files": files}, indent=2,
                                ensure_ascii=False) + "\n", encoding="utf-8")
    return {"ok": True, "doc": doc_id, "name": safe, "row": row}


def stray_resources(cfg, doc_id):
    """Files left in the FLAT `resources/<DOC>/` when courses went plural, or
    None when there is nothing honest to report.

    🔴 Why this exists at all, and it is a mechanism rather than a preference.
    `place_legacy_resources` moves a flat folder into its course only when
    exactly one course exists; with two it cannot know which course owns the
    files, so it correctly moves nothing and writes one line to the log. That
    restraint is right and is not what this changes. But it runs ONCE, at
    startup, and the pane is rendered per request, so the one moment the server
    knows about the stray is a moment the reader is not present for. By the time
    somebody is looking for their file, nothing is carrying the fact. The pane is
    the only place the two ever meet.

    The failure it removes is the one `452658f` was written to remove. That unit
    fixed "the pane shows a file and serves the wrong one"; for this population
    it introduced "the pane shows nothing and says nothing", and the person
    looking for the file is looking at the pane, never at the log.

    🔴 The three conditions are all necessary. Courses must be plural, or the
    flat folder IS the folder being served and there is nothing to say. The
    folder must exist. And it must hold at least one file: an empty stray is
    somebody who has already moved them, and telling them to move files that are
    not there is its own small lie.

    Both folder names are DERIVED, never spelled again, for the reason
    `resource_folder_label` exists: the pane used to print `resources/<DOC>` as a
    literal and quietly stopped being true the day courses got a folder each.
    """
    base = resources_base(cfg)
    if base == resources_root(cfg):
        return None
    flat = base / doc_id
    if not flat.is_dir():
        return None
    try:
        files = [q for q in flat.iterdir()
                 if q.is_file() and not q.name.startswith(".")
                 and q.suffix.lower() != ".bak" and q.name != "_about.json"]
    except OSError:
        return None
    if not files:
        return None
    return {"count": len(files),
            "from": "/".join((base.name, doc_id)),
            "to": resource_folder_label(cfg, doc_id)}


def read_resources(cfg, doc_id):
    d = resource_dir(cfg, doc_id)
    about = read_manifest(cfg, doc_id)
    out = []
    if d.is_dir():
        for p in sorted(d.iterdir(), key=lambda q: q.name.lower()):
            if not p.is_file() or p.name.startswith("."):
                continue
            # 🔴 The backups this module makes are not attachments, and listing
            # them was a real bug: removing a file put its own backup straight
            # back into the list under a uglier name, so "Remove" looked like it
            # had renamed the thing rather than removed it.
            if p.suffix.lower() == ".bak":
                continue
            # The manifest is not one of the things it describes.
            if p.name == "_about.json":
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            # The manifest is keyed by the name as written, which is NFC; macOS
            # hands the name back in NFD. Both are tried, for the same reason
            # resolve_resource() tries both.
            row = (about.get(p.name)
                   or about.get(unicodedata.normalize("NFC", p.name))
                   or about.get(unicodedata.normalize("NFD", p.name))
                   or {})
            out.append({
                "name": p.name,
                "title": row.get("title") or "",
                "note": row.get("note") or "",
                "order": row.get("order"),
                "size": st.st_size,
                "added": datetime.fromtimestamp(st.st_mtime, timezone.utc)
                         .isoformat(timespec="seconds"),
                "kind": resource_kind(p.suffix),
                "url": resource_url(cfg, doc_id, p.name),
            })
    # Anything given an order comes first, in that order; everything else keeps
    # its alphabetical place underneath. So ordering a few files by hand does not
    # oblige him to order all of them.
    out.sort(key=lambda f: (f["order"] is None, f["order"] if f["order"] is not None else 0))
    res = {"ok": True, "doc": doc_id, "files": out, "count": len(out),
           "folder": resource_folder_label(cfg, doc_id)}
    # Absent rather than null on the overwhelmingly common path, so the layer's
    # test is `res.stray` and a course with nothing stray carries nothing.
    stray = stray_resources(cfg, doc_id)
    if stray:
        res["stray"] = stray
    return res


def write_resource(cfg, doc_id, name, data):
    safe = safe_resource_name(name, normalise=True)
    if not safe:
        raise ValueError("that filename cannot be used: no slashes or colons, "
                         "nothing starting with a dot, and 120 characters at most")
    if not data:
        raise ValueError("empty file")
    if len(data) > RESOURCE_MAX_BYTES:
        raise ValueError("too big: %.1f MB, and the limit is %d MB"
                         % (len(data) / 1048576.0, RESOURCE_MAX_BYTES // 1048576))

    d = resource_dir(cfg, doc_id, make=True)
    target = (d / safe).resolve()
    # Belt to the regex's braces: whatever the name did, the path it produced has
    # to still be inside this lesson's own directory.
    if d.resolve() not in target.parents:
        raise ValueError("bad filename")

    # A second attachment of the same name is a new version, not an error, but the
    # old one is not thrown away silently: same .bak convention as everything else
    # in this project. 🔴 Until 2026-09-18 that sentence was false inside one
    # second: the rename went OVER the backup the first attachment had just
    # made. `set_aside` claims the name first, so both versions stay.
    bak = None
    with WRITE_LOCK:
        if target.exists():
            bak = set_aside(target)
        target.write_bytes(data)
    out = {"ok": True, "doc": doc_id, "name": safe, "size": len(data),
           "kind": resource_kind(target.suffix),
           "url": resource_url(cfg, doc_id, safe)}
    if bak is not None:
        out["backup"] = bak.name        # the file actually written, never a guess
    return out


def delete_resource(cfg, doc_id, name):
    target = resolve_resource(cfg, doc_id, name)
    if target is None:
        raise ValueError("no such file")
    # 🔴 Renamed, never unlinked. He may have attached the only copy of something,
    # and this button is one tap away from a list on a phone.
    with WRITE_LOCK:
        bak = set_aside(target)
    return {"ok": True, "doc": doc_id, "name": target.name, "removed": True,
            "backup": bak.name}


def materials_path(cfg):
    return cfg["notes_dir"] / "materials.json"


def access_for(link):
    """What a person needs in order to open this link. Returns (kind, note).

    Nothing here checks whether a link resolves, because nothing here can: the
    server has no session anywhere, and a probe from the server would answer a
    different question from the one the reader's browser is asking. What it can
    do is read the address and say honestly what it will want, which is what
    turns "this is broken" into "this one needs a login"."""
    text = str(link or "").strip()
    if not text:
        return "none", ""
    low = text.lower()
    if not low.startswith(("http://", "https://")):
        return "local", "A file on this machine."
    try:
        host = (urllib.parse.urlparse(text).hostname or "").lower()
    except ValueError:
        host = ""
    if host.endswith("drive.google.com") or host.endswith("docs.google.com"):
        return "drive", "Needs the shared Google Drive folder for this course."
    if "keats" in host:
        return "keats", "Needs a KEATS login and enrolment on this module."
    if not host:
        return "none", ""
    # Everything else is NAMED rather than guessed at. A student told "needs a
    # login at moodle.otheruni.ac.uk" knows exactly what to do, and a public link
    # they can already open costs them one glance.
    return "site:" + host, ("Hosted at %s, so you may need to be signed in there."
                            % host)


def lesson_order(mcfg, metas=None):
    """This module's lesson doc ids, in the order a reader meets them.

    🔴 ONE answer, used by the hub and by the reader's prev/next both. They were
    two answers until 2026-08-29: the hub derived order from the lessons while
    navigation read a `prev`/`next` chain baked into `materials.json`, so a course
    built by a different path had a hub and no navigation at all. Two halves of
    one question, edited separately, disagreeing in a way only a reader could see.

    `materials.json`'s `order` first, filtered to lessons that actually exist,
    then anything it does not mention, sorted: a lesson missing from the order is
    still a lesson, and one nobody can reach is worse than one out of place."""
    if metas is None:
        metas = lesson_meta_index(mcfg)
    order = []
    try:
        mats = json.loads((mcfg["notes_dir"] / "materials.json")
                          .read_text(encoding="utf-8"))
        order = [d for d in mats.get("order", []) if d in metas]
    except (OSError, ValueError):
        pass
    order += [d for d in sorted(metas) if d not in order]
    return order


def derived_neighbours(mcfg, doc_id):
    """{"prev": {...}, "next": {...}} for this lesson, from the module's order.

    🔴 EH's architecture rule, 2026-08-29: "The courses themselves should just be
    packs of information, and all of the navigation, the actual site, everything
    should be a wrapper that those are fed into." So navigation is the reader's
    job for every course, imported packs included, and a chain baked into a
    course's own data is ignored ENTIRELY rather than used as a fallback. Measured
    before that was ruled: of this repo's two courses, one has a partial chain
    with zero conflicts against natural order and the other has none at all, so
    nothing observable changes and no course can disagree.

    A course needing a genuinely non-natural order becomes a settings-level fact,
    designed when such a course appears, rather than data smuggled into a pack."""
    metas = lesson_meta_index(mcfg)
    order = lesson_order(mcfg, metas)
    try:
        at = order.index(doc_id)
    except ValueError:
        return {}
    out = {}
    for rel, idx in (("prev", at - 1), ("next", at + 1)):
        if idx < 0 or idx >= len(order):
            continue
        doc = order[idx]
        meta = metas.get(doc) or {}
        href = str(meta.get("file") or "")
        if not href:
            continue
        out[rel] = {"doc": doc,
                    "title": str(meta.get("title") or doc),
                    "href": href}
    return out


# --------------------------------------------------------------------------
# Where a course's slides and transcripts come from
# --------------------------------------------------------------------------
#
# EH's design, 2026-08-28: "they're going to have an option to either download
# the materials or point you to a folder with the materials and then choose that
# folder." And his sharpening of it, the same day: the user CHOOSES between two
# explicit options; it is not a preference order that silently picks one.
#
# 🔴 That is why this is a stored choice and not "use the local file if there is
# one". A silent fallback means a course that is set to local materials quietly
# reads somebody's Google Drive on the parts whose files did not download, and
# nobody can tell which parts those are by looking. With an explicit choice, a
# part with no local file has NO slides tab, which is a visible fact somebody can
# act on, and the setup page counts them so it is actionable in one place.
MATERIALS_SOURCES = ("keats", "local")
DEFAULT_MATERIALS_SOURCE = "keats"

# The two fields a downloaded folder can satisfy, and the word the download-keats
# skill puts in the filename for each: `<DOC> - Slides (original name).pdf`.
MATERIAL_KINDS = (("slides", "Slides"), ("transcript", "Transcript"))

# Everything that gives the pane something to SHOW. Used to decide whether a
# part has any materials at all, which is a different question from whether the
# index mentions it: a downloaded deck and a mirrored package are both derived
# from the disk at read time and neither needs an entry.
MATERIAL_FIELDS = ("video", "video_embed", "video_hls", "slides", "slides_embed",
                   "transcript", "transcript_embed", "package_embed")


def install_root(cfg):
    """The folder that holds `courses/`, which is where `materials/` sits beside it."""
    root = cfg.get("courses_dir")
    if root is not None:
        return Path(root).parent
    return Path(cfg["notes_dir"]).parent


def materials_source(cfg):
    """"keats" (links out of materials.json) or "local" (files on this disk)."""
    want = str(read_settings_data(cfg).get("materials_source") or "").strip().lower()
    return want if want in MATERIALS_SOURCES else DEFAULT_MATERIALS_SOURCE


def local_materials_dir(cfg):
    """The folder this course's downloaded materials live in.

    A stored path wins; otherwise `materials/<CODE>/` beside `courses/`, which is
    where the download-keats skill puts them by default. Returned whether or not
    it exists, because the setup page has to be able to say that it does not."""
    raw = str(read_settings_data(cfg).get("materials_dir") or "").strip()
    if raw:
        d = Path(raw).expanduser()
        return d if d.is_absolute() else (install_root(cfg) / d)
    return install_root(cfg) / "materials" / str(cfg.get("module") or "")


def local_material_file(folder, doc_id, word):
    """The CURRENT `<DOC> - <Word> (whatever).pdf` in that folder, or None.

    Matched by PREFIX because the skill keeps the download's original name in
    parentheses after the standard part, so the tail is not predictable. The
    match is case-insensitive. Since 2026-09-18 the prefix test is
    `material_names.is_kind`, shared with the pack copier, so the pane and a
    shared pack cannot disagree about what KIND a file is: they did, and a
    renamed transcript was served here and silently left out of a pack.

    🔴 A file whose name marks it superseded is not a candidate. When a transcript
    is corrected its old copy stays beside it renamed `... superseded <date> ...`
    (nothing is deleted), and both match the prefix. Until 2026-09-17 the
    corrected one won only because `(` sorts before `s`, which is determinism
    and not correctness: the pane would have shown the OLD words the day a name
    sorted the other way, with nothing erroring. `material_names.current` is the
    one sentence, shared with the captions and the pack copier. Among the
    candidates the first in sorted order wins, so two current files for one part
    behave the same way on every run."""
    if folder is None:
        return None
    try:
        hits = material_names.current(
            q for q in folder.iterdir()
            if q.is_file() and material_names.is_kind(q.name, word, doc_id))
    except OSError:
        return None
    return hits[0] if hits else None


def local_materials_report(cfg, docs=None):
    """{source, dir, exists, parts, found} for the setup page.

    `found` is per kind, so the page can say "38 of 50 have slides, 12 have a
    transcript" rather than a single number that hides which half is missing."""
    folder = local_materials_dir(cfg)
    out = {"source": materials_source(cfg), "dir": str(folder),
           "exists": folder.is_dir(), "parts": 0,
           "found": {k: 0 for k, _w in MATERIAL_KINDS}}
    if docs is None:
        try:
            docs = list(json.loads(materials_path(cfg).read_text(encoding="utf-8"))
                        .get("docs", {}))
        except (OSError, ValueError):
            docs = []
        # 🔴 With no index, the LESSONS are the list of parts. Added 2026-08-30
        # alongside the change that lets a course with no `materials.json` serve
        # local files: without it this page reported "slides for 0 of 0 parts"
        # about the very folder the pane was serving a deck out of, and the two
        # halves of one screen disagreed about the same directory.
        if not docs:
            docs = sorted(lesson_meta_index(cfg))
    out["parts"] = len(docs)
    if not out["exists"]:
        return out
    for doc in docs:
        for key, word in MATERIAL_KINDS:
            if local_material_file(folder, doc, word):
                out["found"][key] += 1
    return out


def read_materials(cfg, doc_id):
    """Where this part's lecture video and slide deck live.

    Built by build_materials.py from the two tracking documents that already
    held these facts, so adding the feature needed no edit to any note. Rebuild
    with `python3 server/build_materials.py` whenever either document changes.
    """
    if not DOC_ID_RE.match(doc_id or ""):
        raise ValueError("bad doc id")
    path = materials_path(cfg)
    # 🔴 A missing index is an EMPTY ENTRY, not a hard stop, and the difference
    # is the whole of this function's usefulness to anybody who is not EH.
    #
    # This returned here until 2026-08-30, above every derivation below, so a
    # course with no `materials.json` could not reach LOCAL materials EITHER: a
    # pack-built course pointed at a folder of downloaded slides, with a
    # correctly named deck in it and a downloaded recording beside it, still
    # answered "no index". Reproduced over HTTP before it was changed. The
    # recipient could take the one action available to him and the server
    # ignored it, while the setup page cheerfully reported the folder.
    #
    # So the sentence is HELD instead of returned, every derived path runs, and
    # it is used only if nothing turned up by any route. Where it was true
    # before it is still true, and the reader gets the same words.
    problem, index_error, docs = "", "", {}
    if not path.exists():
        # 🔴 Written for whoever actually READS it, which is not the person who
        # could fix it. The message before this one said "Import lessons with
        # their links, or scan the module on KEATS", which is two actions a kit
        # recipient cannot take: no enrolment, no links. A course built from
        # shared lesson packs has no materials file and never will, so for that
        # reader this is a permanent sentence about a permanent state, and it
        # should describe the state and point at the one thing on this screen
        # that does work rather than asking for the impossible. (Its own
        # predecessor said "run build_materials.py", advice only this machine
        # could take: the same mistake, one audience further out.)
        # 🔴 The clause "the lessons travel, the recordings stay on the site
        # they came from" was here for a day and is gone on QA's argument: it is
        # an ASSERTION, and it is false in a state a person can reach (materials
        # set to a local folder, files on the disk, no index). The shipped fix
        # for that state means the sentence no longer appears there at all, but
        # a sentence that is only true because of a fix elsewhere is one bug
        # away from lying again. Trim the claim rather than hedge it.
        problem = ("This course has no index of slides or recordings. That is "
                   "the normal state for a course built from shared lesson "
                   "packs. Anything you attach yourself appears under Files.")
    else:
        try:
            docs = json.loads(path.read_text(encoding="utf-8")).get("docs", {})
        except (OSError, ValueError) as exc:
            # 🔴 The parser's own words are for whoever can act on them, and that
            # is not the reader: "Expecting property name enclosed in double
            # quotes: line 1 column 3 (char 2)" in front of somebody who has
            # never opened a JSON file explains nothing and reads as a crash. It
            # goes to the log, with the path, where whoever can repair it looks.
            if cfg.get("log_path") is not None:
                log(cfg, "materials: %s is unreadable: %s" % (path, exc))
            # 🔴 This one does NOT go into `problem`, and the reason is a type
            # confusion rather than a message in the wrong place. `problem`
            # carries three facts of two different KINDS, and the gate at the
            # bottom asks "did this PART turn anything up", which is the right
            # question for the other two and the wrong one for this:
            #
            #   index absent      course-wide, benign, permanent   gate correct
            #   index UNREADABLE  course-wide, abnormal, REPAIRABLE  gate wrong
            #   nothing for <doc> genuinely per-part                gate correct
            #
            # So a corrupt index was announced or hidden depending on which
            # lesson you opened: a part with a package on disk answered ok:true
            # with a working Videos tab and no hint the course was damaged,
            # while a part with nothing on disk said the index cannot be read.
            # QA measured both on one truncated file, 2026-08-30, verifying
            # `a967f45`. A course-level fault needs a course-level channel.
            #
            # ⚠️ Not `notice` (taken at the readings retraction notices, a
            # different thing with its own class) and not `access_notes` (keyed
            # by access KIND and rendered per view, so a course-level fact would
            # appear under one tab and not another, which is this bug again).
            index_error = ("This course's index of slides and recordings cannot "
                           "be read, so nothing here can point at them. Anything "
                           "you attach yourself still appears under Files.")
    entry = docs.get(doc_id) or {}
    if docs and not entry and not problem:
        problem = "no materials recorded for %s" % doc_id
    out = {"ok": True, "doc": doc_id}
    out.update(entry)
    # 🔴 Navigation is DERIVED, and the course's own chain is dropped rather than
    # used as a fallback. EH's ruling, 2026-08-29. Popped BEFORE the derivation is
    # merged so a course carrying a stale or partial chain cannot leak half of it
    # through: what the reader gets is the module's order or nothing.
    out.pop("prev", None)
    out.pop("next", None)
    out.update(derived_neighbours(cfg, doc_id))
    # R20: the transcript can be shown in the pane rather than only linked out of
    # it, and Drive previews the same way it previews the deck. Derived here at
    # read time rather than in build_materials.py so no rebuild is needed and
    # every doc gains it at once; all 50 transcripts are Drive /file/d/<id>/
    # links, checked before writing this.
    if entry.get("transcript") and not entry.get("transcript_embed"):
        m = re.search(r"/file/d/([A-Za-z0-9_-]+)/", entry["transcript"])
        if m:
            out["transcript_embed"] = "https://drive.google.com/file/d/%s/preview" % m.group(1)
    # R26: the lecture itself, played in the pane rather than only linked out of
    # it. KEATS refuses to be framed, but the header that refuses governs
    # *framing* and says nothing about loading a media file, and the video is
    # served by Kaltura's CDN with no session token of any kind: verified from a
    # shell with no cookies at all, as well as from this reader's own origin.
    # So nothing is downloaded and nothing is stored but the entry id, harvested
    # once from KEATS into materials.json. 41 of the 50 parts have one; the other
    # nine are narrated slide packages rather than video, and have no id to find.
    # R49, 2026-08-16. EH: "someone who's not in the program at all might
    # still end up using this software, and if they do, they should still have
    # all the content and just show that those links are not valid."
    #
    # 🔴 So say what each link NEEDS, rather than presenting them all as if they
    # will open. Nothing here checks whether a link resolves, because nothing
    # here can: the server has no KEATS session and no Drive access, and a probe
    # from the server would answer a different question from the one the reader's
    # browser is asking. What it can do is state the requirement honestly, which
    # is what turns "this is broken" into "this one needs a KCL login".
    #
    #   none   the Kaltura media, which carries no session token at all
    #   keats  a KEATS link, which needs enrolment on the module
    #   drive  a Google Drive file, which needs that folder shared with you
    # 🔴 Derived from each link, not hardcoded. It used to say "Needs a KEATS
    # login" for every video and "Needs the shared Drive folder" for every deck,
    # whatever the link pointed at. On this module that happened to be true. On
    # anybody else's it was a sentence about an institution they have never heard
    # of, attached to a file sitting on their own disk. EH asked on
    # 2026-08-22 whether any of this works for a course that is not on KEATS;
    # this was one of the places where it did not.
    out["access"] = {}
    out["access_notes"] = {"none": ""}
    for field in ("video", "video_embed", "video_hls", "slides", "slides_embed",
                  "transcript", "transcript_embed"):
        kind, note = access_for(entry.get(field))
        out["access"][field] = kind
        if note:
            out["access_notes"][kind] = note
    # The media itself carries no session of any kind, so once its address is
    # known it opens for anybody, however it was found.
    for field in ("video_embed", "video_hls"):
        out["access"][field] = "none"
    if entry.get("entry") and not entry.get("video_embed"):
        base = ("https://cdnapisec.kaltura.com/p/%s/sp/%s/playManifest/entryId/%s"
                % (KALTURA_PARTNER, KALTURA_SUBPARTNER, entry["entry"]))
        # format/url redirects to a single progressive MP4, moov at the front and
        # byte ranges honoured, so it plays and seeks in a plain <video>. It picks
        # the smaller flavour, so the layer reads the HLS manifest to find the
        # better one and falls back to this when anything about that fails.
        out["video_embed"] = base + "/format/url/protocol/https/video.mp4"
        out["video_hls"] = base + "/format/applehttp/protocol/https/a.m3u8"
    # A downloaded copy of the recording wins over the stream: same file, no
    # internet needed, and it keeps playing if the course site ever closes.
    # Derived from the file's existence at read time, like everything local.
    # video_hls goes away with it, so the reader's flavour upgrade cannot swap
    # the local copy back out for the CDN.
    vid = cfg["notes_dir"] / "videos" / (doc_id + ".mp4")
    if vid.is_file():
        out["video_embed"] = "/m/%s/videos/%s.mp4" % (
            cfg.get("module") or "", urllib.parse.quote(doc_id))
        out.pop("video_hls", None)
        out["access"]["video_embed"] = "none"
    # A mirrored narrated slide package: the whole iSpring export fetched into
    # packages/<doc>/, slides, narration MP3s, player and all, so the part plays
    # from this server with no KEATS session of any kind. Derived from the
    # folder's existence at read time, like the Kaltura embed above, so
    # mirroring a package needs no rebuild of materials.json.
    pkg = cfg["notes_dir"] / "packages" / doc_id / "index.html"
    if pkg.is_file():
        out["package_embed"] = "/m/%s/packages/%s/index.html" % (
            cfg.get("module") or "", urllib.parse.quote(doc_id))
        out["access"]["package_embed"] = "none"
    # 🔴 THE RECORDING'S OWN CAPTIONS, and the answer comes from disk rather than
    # from the reader trying a URL and reading the 404. Derived at read time like
    # every other local thing above, which is what lets a caption file appear
    # without a rebuild of materials.json or a restart.
    #
    # ⚠️ WHY THE SERVER ANSWERS THIS AND NOT THE PAGE. The layer would otherwise
    # have to construct `/m/<CODE>/captions/<DOC>/video.vtt` from pieces it holds
    # separately, and then tell a missing file apart from a broken URL by their
    # status code. **They are the same status code.** Deciding it here makes "this
    # lecture has no captions" a fact about the disk, which is the sentence the
    # strip already promises to say honestly.
    #
    # 🟢 `video.vtt` is the recording's whole track; `soundN.vtt` beside it belong
    # to a narrated package and are fetched by the package's own injected script.
    # One folder per lecture, named by source: see `video_captions.py`.
    cap = cfg["notes_dir"] / "captions" / doc_id / "video.vtt"
    if cap.is_file():
        out["video_captions"] = "/m/%s/captions/%s/video.vtt" % (
            cfg.get("module") or "", urllib.parse.quote(doc_id))
        out["access"]["video_captions"] = "none"
        # 🔴 WHOSE WORDS THESE ARE, and the reader is told because a caption
        # track carries the lecturer's authority whether or not it earned it.
        # Since 2026-09-04 a lecture whose audio cannot be matched to its
        # transcript is captioned from the MACHINE's own words instead, which
        # is the difference between having captions and not; a reader who does
        # not know which kind they are reading cannot judge a word that looks
        # wrong, and this model heard "maximums" for *mechanisms*.
        #
        # ⚠️ READ FROM THE SIDECAR, NOT INFERRED. The `.vtt` itself says nothing
        # about where its words came from, and guessing from a filename is how
        # this would go quietly wrong the day a third route exists. Absent or
        # unreadable means the older schema, which only ever had one route, so
        # the honest default is the transcript.
        out["video_captions_words"] = "transcript"
        try:
            with open(cap.parent / "captions.json", encoding="utf-8") as fh:
                said = json.load(fh).get("words_are")
            if said:
                out["video_captions_words"] = str(said)
        except (OSError, ValueError, AttributeError):
            pass
    # 🔴 The choice, applied last so it wins over everything derived above. When
    # a course is set to its own materials folder, the slides and the transcript
    # come from that folder and from nowhere else: a part with no file there
    # loses the tab rather than quietly falling back to the link it was told not
    # to use. See MATERIALS_SOURCES for why that is a feature.
    out["materials_source"] = materials_source(cfg)
    if out["materials_source"] == "local":
        folder = local_materials_dir(cfg)
        code = cfg.get("module") or ""
        for key, word in MATERIAL_KINDS:
            found = local_material_file(folder, doc_id, word)
            for field in (key, key + "_embed"):
                out.pop(field, None)
                out["access"].pop(field, None)
            if found:
                url = "/m/%s/materials/%s" % (code, urllib.parse.quote(found.name))
                out[key] = url
                out[key + "_embed"] = url
                # 🔴 What the file actually IS, decided exactly the way the route
                # that serves it decides: suffix first, first bytes second. The
                # pane needs it to choose the PDF viewer over an iframe, and
                # deciding it HERE rather than in the browser saves a round trip
                # and means one answer rather than two that can disagree. It also
                # makes the naming fix of the same morning pay twice: a deck with
                # a broken name now both serves correctly AND gets the viewer.
                try:
                    with found.open("rb") as fh:
                        head = fh.read(MAGIC_PEEK)
                except OSError:
                    head = b""
                out.setdefault("types", {})[key + "_embed"] = \
                    content_type_for(found.name, head)
                # access_for() already answers "local" for anything that is not
                # an http address, so the note the pane shows needs no new case.
                for field in (key, key + "_embed"):
                    kind, note = access_for(url)
                    out["access"][field] = kind
                    if note:
                        out["access_notes"][kind] = note
    # 🔴 The held sentence, used only now that every route has been tried. A part
    # with nothing to point at gets the same words it always got; a part whose
    # index is missing but whose FILES are on this disk gets the files, which is
    # the whole change. `prev`/`next` and `materials_source` are on `out`
    # whatever happens, so they cannot count as having found anything.
    if problem and not any(out.get(k) for k in MATERIAL_FIELDS):
        return {"ok": False, "error": problem}
    # 🔴 Set on EVERY part of a course whose index will not parse, including the
    # ones that found their files anyway, so the course tells one story rather
    # than a different one per lesson. Never `ok: false`: the local files are
    # still servable and serving them is the right call.
    if index_error:
        out["index_error"] = index_error
    return out


def read_marks(cfg, doc_id):
    """Highlights and free notes, anchored. This is the copy that can rebuild the
    page; the vault note is a readable publication with the offsets thrown away,
    so it cannot.

    🔴 `cfg` is passed through so a corrupt file is announced IN THE LOG and not
    only on stderr. It was not, until `write_marks` started reading the file
    before writing it: this read then reached the damaged sidecar first, kept the
    bytes with no cfg to log with, and `keep_the_losing_copy` a moment later
    found its rescue already made and correctly said nothing. The bytes were
    still saved, so nothing was lost, but **the only witness a person can find
    afterwards moved from the log to a stream nobody keeps**, and the test that
    caught it is the one asserting the log line rather than the file."""
    path = sidecar_path(cfg, doc_id, "marks")
    return read_json_sidecar(path, {"doc": doc_id, "updated": 0, "items": [], "notes": []},
                             cfg)


# 🔴 THE IDENTITY A MARK DID NOT HAVE, and the reason it is DERIVED rather than
# stored. Adding a uid field would have needed a migration for every mark on
# every device, and two devices back-filling a uid for the same existing mark
# would mint two different ones, which is the fusion this is trying to avoid
# arriving by the front door.
# The bucket every unnameable item counts under. It cannot collide with a real
# key: `mark_key` produces "h:<int>:<text>" and `note_key` "n:<int>".
NO_IDENTITY = "\x00 no identity"

MARKS_LOCK = threading.Lock()
# 🔴 ITS OWN LOCK, not the marks one. A different file, so sharing would
# serialise two unrelated saves; and not `LESSON_STATE_LOCK` either, which is
# about the read/watched flags and was my first, wrong, reach for "a lock that
# exists nearby". The rule is that a lock names the thing it protects.
BOOKMARKS_LOCK = threading.Lock()

# 🔴 ITS OWN LOCK, not the bookmarks one. The chatmarks sidecar is a different
# FILE, so sharing a lock would serialise two independent writers for nothing;
# and sharing the wrong one is a mistake this codebase has already made once
# (the bookmark merge was first written against LESSON_STATE_LOCK, which guards
# a different file entirely and would have left the real race open).
CHATMARKS_LOCK = threading.Lock()

# 🔴 ITS OWN LOCK AGAIN, and the rule has now been stated three times because
# each new sidecar is a fresh chance to reach for a lock that exists nearby.
# `additions` is a fourth file; sharing would serialise saves that cannot
# collide, and sharing the WRONG one leaves the real race open.
ADDITIONS_LOCK = threading.Lock()

# 🔴 A FIFTH FILE AND A FIFTH LOCK. Stated again because the comment above was
# right that every new sidecar is a fresh chance to reach for a lock that
# happens to be nearby: `chats` is not `chatmarks`, and sharing CHATMARKS_LOCK
# would serialise saves that cannot collide while leaving this file's real race
# wide open.
CHATS_LOCK = threading.Lock()

# 🔴 A SIXTH FILE AND A SIXTH LOCK, and this is the last of them: `cards` is the
# fifth and final sidecar to gain a merge. Stated once more because the rule has
# earned it -- every new sidecar is a fresh chance to reach for a lock that
# happens to be nearby, and `cards` sits beside `marks` in every other sense
# (its keys ARE mark ids) which makes MARKS_LOCK the tempting wrong answer. It
# is a different file, so sharing would serialise saves that cannot collide
# while leaving this file's own race wide open.
CARDS_LOCK = threading.Lock()
MAX_BASE_KEYS = 2600      # the 2000 items and 500 notes a payload may carry, and room


def _int(v):
    """A real integer, and `True` is not one. JSON has no separate bool type on
    the way in, so a bool passes `isinstance(v, int)` unless it is refused by
    name, and `"h:1:True:3"` would be a key that matches nothing for ever."""
    return isinstance(v, int) and not isinstance(v, bool)


def mark_key(item):
    """What identifies a highlight across two devices, or None when nothing does.

    🔴 `id` is NOT it, and that was measured rather than argued. On the rig, disk
    item `id=1` was one device's highlight at 19:15:21 and the other device's at
    19:15:45, on the same lesson, because both browsers assign ids from zero. A
    union by id fuses two different marks into one.

    🔴 NEITHER ARE THE OFFSETS, and this key was `(b, s, e)` until `study-hub-qa`
    said why not. They measured every marks sidecar in both courses, 72
    highlights in 11 lessons: `(b, s, e)` and `(b, t)` both collide zero times,
    **and choosing on that measurement would have been the mistake.**
    `shell.html`'s `reanchor()` rewrites `it.b`, `it.s` AND `it.e` from
    `indexOf(it.t)` whenever the lesson prose shifts, so an identity built on the
    offsets breaks in exactly the case the reader's own anchoring exists to
    survive, and this project edits lesson prose (the corrections sweep changed
    sixteen sites in one day). **`t` is the field everything else is recomputed
    FROM, and the only one nothing rewrites**, which is why it is the identity.

    ⚠️ `b` moves too, when a block is inserted or removed, so a device that has
    not reloaded since the lesson changed can still key a mark differently from
    one that has. That fails as a DUPLICATE rather than as a deletion, which is
    the direction to fail in, and `t` alone is worse: a phrase repeated in two
    blocks would then be one key, and one device's copy would suppress the
    other's.

    🔴 None means "cannot be reasoned about", and a mark with no key gets exactly
    today's behaviour: written when the writer holds it, and not rescued from
    disk when the writer does not. Adopting an unkeyable mark would duplicate it
    on EVERY write, because nothing could match it next time, and a collection
    that grows on every save reaches the 2000 cap and then refuses to save at
    all. Losing a mark is bad; a lesson that can no longer be saved is worse.
    """
    b, t = item.get("b"), item.get("t")
    if not _int(b) or not isinstance(t, str) or not t:
        return None
    return "h:%d:%s" % (b, t)


def bookmark_key(row):
    """What identifies a BOOKMARK across two devices, or None when nothing does.

    🔴 CHOSEN AGAINST WHAT CHANGES IT, not against whether it looks unique in
    today's data. That is the entry's own ruling and it is QA's precedent:
    `(b, s, e)` collided zero times across all 72 of EH's real highlights and was
    still the wrong key for marks, because `reanchor()` rewrites those.

    🟢 **`b` IS ALREADY THE CLIENT'S OWN IDENTITY FOR CREATION**, measured at
    source rather than assumed (`local-layer.html`, the bookmark toggle):
    pressing Bookmark on a block that already carries one removes rather than
    adds. **So a device can never MAKE two bookmarks on one block**, which is
    the half this key rests on.

    🔴 **CORRECTED 2026-09-04: IT CANNOT MAKE TWO AND IT CAN NOW HOLD TWO.** The
    first version of this sentence said a device could never hold two, which
    stopped being true the moment `merge_bookmarks` shipped: the duplicate this
    key deliberately creates is delivered straight into the page's array. **QA
    found what that met** -- a client removing by `b` alone, so one click took
    both rows and the save behind it deleted the other device's bookmark from
    the file. The page now removes the ROW it drew (`dropRow`, `pickRow`) and
    still refuses to create a second one, so the creation half above holds and
    the key is unaffected.

    🔴 **AND `b` ALONE IS STILL THE WRONG KEY, for the reason that decides every
    one of these: which way it fails.** Block indices move when lesson prose
    gains or loses a block, and this project edits lesson prose. Two devices that
    disagree about which paragraph is block 5 would then key two DIFFERENT
    bookmarks the same, and the merge would treat one device's as an account of
    the other's -- **a deletion**. Adding `t` makes that same disagreement
    produce two keys and therefore a DUPLICATE, which is the direction to fail
    in.

    ⚠️ **`t` IS A FROZEN SNAPSHOT HERE, WHICH IS NOT WHAT IT IS FOR A MARK, and
    the difference is worth stating because the key looks identical.** A mark's
    `t` is the field `reanchor()` recomputes `b`, `s` and `e` FROM. **Nothing
    reanchors a bookmark**: `t` is the block's text as it read when the reader
    pressed the button, and it is never rewritten. So the two keys have the same
    SHAPE for different reasons, and only one of them would survive its file
    gaining a reanchor.

    🔴 **WHAT WOULD INVALIDATE THIS KEY**, in one line as the entry asks: **a
    bookmark file that gains reanchoring** (then `b` becomes derived and the key
    should drop to `t` alone), **or a client that allows two bookmarks on one
    block** (then `b` stops being unique and `at` would have to join the key).

    🔴 `at` IS DELIBERATELY EXCLUDED. It is issued per creation, so two devices
    bookmarking the same block would never share a key and the pair would
    duplicate on every write -- the unkeyable-item failure `mark_key` documents,
    reached by a key that looks more precise.
    """
    b, t = row.get("b"), row.get("t")
    if not _int(b) or not isinstance(t, str) or not t:
        return None
    return "b:%d:%s" % (b, t)


def note_key(note):
    """A free note's identity: the instant it was created.

    `addNote` stamps `ts` once with `Date.now()` and never touches it again,
    while `text` changes with every keystroke and `id` is reassigned by
    `replaceMarks`. The timestamp is the only field that both survives editing
    and is not renumbered.
    """
    ts = note.get("ts")
    return "n:%d" % ts if _int(ts) else None


def addition_key(row):
    """What identifies a KEPT NOTE across two devices, or None when nothing does.

    🔴 THE THIRD OF FIVE, and chosen against what CHANGES it rather than
    against what looks unique in today's file. That is the entry's ruling and
    QA's precedent: `(b, s, e)` collided zero times across all 72 of EH's real
    highlights and was still the wrong key for a mark.

    🔴 **`id` IS NOT IT, and this sidecar is the third place the same trap has
    turned up.** The Keep button mints `var id = 1; adds.items.forEach(x => if
    (x.id >= id) id = x.id + 1)`, computed from THIS device's list, so two
    devices that each keep their first note both call it `1`. It is the chat
    id's shape and the card mark id's shape, read out of the shipped client
    rather than assumed.

    🟢 **`ts` IS THE CREATION INSTANT AND NOTHING REWRITES IT.** The Keep
    handler stamps `Date.now()` once; there is no edit path for a kept note at
    all (the card renders `it.text` and offers Remove, never an input), and the
    server stores the row as it arrives. **It is the one field that is neither
    renumbered nor recomputed**, which is the same argument `note_key` makes for
    a free note.

    🟢 **`b` JOINS IT SO THAT A COLLISION NEEDS TWO ACCIDENTS AT ONCE**, not
    one: two devices would have to keep a note in the same millisecond AND on
    the same block. `b` is safe to include here for the reason it is not for a
    mark: **nothing reanchors an addition.** `renderAdds` reads `it.b` to find
    the block and skips the card when the block is gone; no code path writes it
    back. A device that has not reloaded since the prose moved therefore keys
    its own rows exactly as it always did.

    ⚠️ **AND `b`'s CONTRIBUTION IS NOT LOAD-BEARING, which is said rather than
    implied**, because the bookmark docstring claimed the opposite about its own
    counting and had to retract it. The mutation that drops the block from this
    key DIES, but on the two-sides agreement test rather than on anything about
    merging: the browser still spells the block into its base. **The collision
    the block narrows is one nothing in this system can create**, since the Keep
    button disables itself and needs a fresh selection, so no honest test
    measures it. It is kept because a same-instant collision would be a
    DELETION, and one integer is a cheap guard against the worst direction.

    🔴 **THE TEXT IS DELIBERATELY EXCLUDED, and the reason is measured rather
    than aesthetic.** Every text-bearing key in this file truncates (400 for a
    bookmark, 300 for a bookmarked conversation) and the browser computes the
    same key for its `base`. **A JavaScript `slice` counts UTF-16 code units and
    a Python slice counts code points**, so one astral character before the cut
    makes the two sides truncate at different places and produce different
    keys: verified 2026-09-04, `"a😀b".slice(0, 2)` is a lone surrogate in node
    and `'a😀b'[:2]` is the whole emoji in Python. A base that names nothing the
    server can match does not duplicate anything, it makes a REMOVAL silently
    fail to land. **Two integers cannot reach that class at all**, and the
    reader's kept text can carry anything a lesson or a chat answer holds.

    🔴 **WHAT WOULD INVALIDATE THIS KEY**, in one line as the entry demands: **a
    kept note that becomes editable in place and re-stamps `ts`**, or **a client
    that reanchors an addition** (then `b` is derived and the key should drop to
    `ts` alone), or **two notes created in the same millisecond on one block**,
    which the Keep button cannot do today because it disables itself and needs a
    fresh selection.

    None means "cannot be reasoned about", and an unnameable row gets exactly
    today's behaviour: written when the writer holds it, never rescued from disk
    when it does not. Adopting it would duplicate it on EVERY write, because
    nothing could match it next time.
    """
    b, ts = row.get("b"), row.get("ts")
    if not _int(b) or not _int(ts):
        return None
    return "a:%d:%d" % (b, ts)


def marks_keys(doc):
    """Every key a marks document names: the marks first, then the notes.

    🔴 ONE FUNCTION, because two of its callers have to agree exactly.
    What `write_marks` returns as `keys` is what a browser stores and sends back
    as its next `base`; what `colour_purge` sends as ITS base is the same
    reading of the same file. A second spelling of "the keys of this document"
    is a place for those two to drift, and drift here deletes marks rather than
    merely disagreeing about them.

    Unnameable entries are dropped rather than represented: `base` is a list of
    strings and a key nothing can match belongs in neither list. An entry that
    is not a dict is unnameable for the same reason and is dropped in the same
    breath, so one corrupt row cannot fail a sweep across every lesson the way
    `read_json_sidecar(path, None)` once did.

    ⚠️ NOT the shrink guard's key function, which buckets the unnameable under
    `NO_IDENTITY` instead of dropping them. Dropping them there would let a swap
    of two unnameable items read as no change at all, which is the defect that
    guard was rewritten to catch.

    🔴 THE THIRD MEMBER, written down 2026-09-08 so the triangle is stated once
    instead of being rediscovered: `_unseen` drops them as well, and THERE the
    drop causes the loss the guard exists to catch. Three functions, two
    treatments, each correct for its own purpose. The argument is at `_unseen`.
    """
    return [k for k in
            [mark_key(x) for x in (doc.get("items") or []) if isinstance(x, dict)]
            + [note_key(x) for x in (doc.get("notes") or []) if isinstance(x, dict)]
            if k]


def _renumber(kept, mine, groups=False):
    """Fresh ids, and fresh group labels, for marks arriving from another device.

    🔴 The ids of the marks the WRITER sent are never touched. The reader may be
    typing into a note keyed by its id at this moment, and renumbering under them
    would move the row being edited. Only the adopted marks are given new ids,
    above everything the writer holds.

    A group must arrive whole or its halves stop being one highlight. `g` is
    minted as `g<seq>` on both devices, so a disk group can carry the same label
    as one the writer sent; adopted groups are relabelled `m<n>`, a form the
    shell never mints, and checked against the labels already present so two
    merges in a row cannot collide either.
    """
    nxt = max([x["id"] for x in mine if _int(x.get("id"))] + [-1]) + 1
    taken = {x.get("g") for x in mine if x.get("g")}
    seen, out, n = {}, [], 0
    for x in kept:
        y = dict(x)
        y["id"] = nxt
        nxt += 1
        g = x.get("g")
        if groups and g:
            if g not in seen:
                while ("m%d" % n) in taken:
                    n += 1
                seen[g] = "m%d" % n
                taken.add(seen[g])
            y["g"] = seen[g]
        out.append(y)
    return out


def chatmark_key(row):
    """What identifies a HIGHLIGHT INSIDE A CHAT across two devices.

    🔴 CHOSEN AGAINST WHAT CHANGES IT, per the entry's ruling, and the answer is
    NOT the bookmark answer. Measured by `server/probe_chatmark_identity.py`
    before this was written.

    🔴 **`c` IS A LOCAL ORDINAL, NOT AN IDENTITY, and this is the trap the entry
    warned about arriving early.** `newChat` mints `var id = 1; chats.forEach(c
    => if (c.id >= id) id = c.id + 1)`, computed from THIS device's chats. **Two
    devices that each start their first conversation both get `1`.** It is the
    same shape as the card mark id the entry calls the trap, and it means `c`
    can never carry the key alone.

    🟢 **`i` LOCATES THE TURN and `s`,`e` LOCATE THE RANGE INSIDE IT**, and both
    are needed: the painter selects a LIST for one `(c, i)` and nothing dedupes
    on add, **so one device may legitimately hold two marks on one turn**. A key
    without the offsets would key those two the same, and a collision here is a
    deletion.

    🟢 **NOTHING REANCHORS A CHAT MARK, which is why the offsets may be in the
    key at all.** This is the exact difference from a lesson mark, where
    `(b, s, e)` looked unique across all 72 of EH's highlights and was still
    wrong because `reanchor()` rewrites them.

    🔴 **THE ARGUMENT FOR THAT IS STRUCTURAL, AND IT REPLACES A WEAKER ONE
    (QA, 2026-09-04, on this docstring's own invited attack).** The first version
    argued from an INVENTORY of call sites -- every use of `chatMarks` is a push
    at creation, a whole-array assignment at load, or a read. **True, and it
    needs re-deriving the day somebody adds a call site**, which is the kind of
    claim this project keeps paying for.

    🟢 **The reason that cannot rot: a turn is never RENDERED.** `renderThread`
    does `txt.textContent = turn.text`, a direct assignment, **so the DOM text IS
    the stored text by construction** rather than by the absence of a rewrite.
    There is no markdown step and no escaping step that could render one turn to
    different characters. **And painting wraps ranges in `<mark>`, which does not
    change `textContent`**, so the offsets survive their own painter. The
    inventory was right; this is why.

    🔴 **`t` IS WHAT MAKES THE COLLISION FAIL SAFELY.** Two devices whose chat
    `1` is a different conversation would otherwise key two unrelated marks the
    same, and the merge would read one as an account of the other: **a
    deletion**. With the marked text in the key, that same disagreement produces
    two keys and therefore a DUPLICATE, which is the direction to fail in.

    🔴 **`at` IS EXCLUDED**, for the reason it is excluded from `bookmark_key`:
    issued per creation, so two devices could never share a key and every pair
    would duplicate on every write. **The unkeyable item, reached by a key that
    looks more precise.**

    🔴 **WHAT WOULD INVALIDATE IT**, in one line as the entry demands: **a turn
    whose text can be edited or re-rendered differently** (the offsets stop
    meaning anything), **or a chats merge that INTERLEAVES two devices' turns**
    (`i` moves). ⚠️ The second is a real upcoming change, since saved
    conversations is another of the five sidecars. 🟢 **It fails toward keeping**:
    a moved `i` changes a mark's key, so the pair duplicates rather than deletes.
    """
    try:
        c = int(row.get("c"))
        i = int(row.get("i"))
        st = int(row.get("s"))
        en = int(row.get("e"))
    except (TypeError, ValueError, AttributeError):
        return None
    t = str(row.get("t") or "")[:400]
    if not t:
        return None
    return "cm:%d:%d:%d:%d:%s" % (c, i, st, en, t)


def chatbook_key(row):
    """What identifies a BOOKMARKED CONVERSATION across two devices.

    🟢 **This half really is the bookmark answer**, and it is the same shape for
    the same reasons: `c` is unique within one device by construction (adding
    asks `chatBooks.some(x => x.c === id)`, removing filters `x.c !== id`), and
    `c` alone still fails the wrong way across devices because the id is a local
    ordinal. **`t` turns that collision into a duplicate.**

    🔴 **IT SHIPPED KNOWING QA'S FINDING AGAINST `dbe5c2b`, AND THE FINDING IS
    NOW FIXED (2026-09-04).** Their finding: a merge that can put two rows on one
    `c` meets a client that removes by `c`, so one click deletes both.
    **`chatBooks` had exactly that shape** -- deduped by `c`, removed by `c`, in
    two places -- which is why it was written down here rather than discovered
    later. 🟢 **Both surfaces were fixed in one change** (`local-layer.html`,
    `dropRow` and `pickRow`), because it was one defect with two collections and
    fixing the bookmarks alone would have made the pair harder to see.

    🔴 **WHAT WOULD INVALIDATE IT**: a client that allows two bookmarks on one
    conversation, or a chat title that is rewritten after the fact.
    """
    try:
        c = int(row.get("c"))
    except (TypeError, ValueError, AttributeError):
        return None
    t = str(row.get("t") or "")[:300]
    if not t:
        return None
    return "cb:%d:%s" % (c, t)


def chatmarks_identity(doc):
    """One key per row across BOTH of this sidecar's collections.

    🔴 **THE DECISION THE ENTRY LEFT OPEN: together, not apart.** `chatmarks`
    holds marks inside conversations AND bookmarked conversations, and the guard
    already COUNTS them together (`len(marks) + len(chats)`). Naming only half
    would make the two halves disagree about what "this file" means: a
    conversation swapped at equal count would be invisible while a mark swapped
    at equal count was caught, in one file, with nothing saying why.

    🟢 **NAMESPACED, so one collection cannot mask a loss in the other.** The two
    key functions already spell their own prefixes (`cm:` and `cb:`), so a
    collision is unlikely rather than impossible; the prefix here makes it
    impossible, which is cheaper than arguing about it.

    🔴 **A ROW THAT CANNOT BE NAMED STAYS `None` AND IS NOT FILTERED OUT.** The
    guard buckets falsy keys under `NO_IDENTITY` so that a FALL in the number of
    unnameable rows is still a loss. ⚠️ Namespacing must therefore preserve
    falsiness: `("cm", None)` is a TRUTHY tuple, which would quietly remove those
    rows from the one comparison that can still see them."""
    out = []
    for row in (doc.get("marks") or []):
        k = chatmark_key(row) if isinstance(row, dict) else None
        out.append("m/" + k if k else None)
    for row in (doc.get("chats") or []):
        k = chatbook_key(row) if isinstance(row, dict) else None
        out.append("c/" + k if k else None)
    return out


def _unseen(theirs, mine, agreed, key_of):
    """The disk's rows this writer can account for neither way.

    🔴 COUNTED, not set membership, because one key can legitimately name two
    rows: a phrase repeated inside one block can be highlighted twice, and after
    `reanchor()` both copies carry the same block and the same text. With a set,
    a writer holding one of the pair would suppress BOTH the disk's copies and
    the second would be deleted. The allowance is `max(held, agreed)` rather
    than their sum: the base and the payload are two descriptions of the same
    rows, not two separate stocks.

    🟢 LIFTED OUT OF `merge_marks` UNCHANGED, 2026-09-03, so a second sidecar
    could use the COUNTING without a second copy of it. ⚠️ **The mechanism is
    what generalises; the IDENTITY is not**, which is why `key_of` is an
    argument and why each sidecar argues its own key in its own docstring. The
    entry that asked for this says so in terms, and the first shrink guard was
    got wrong by widening it with a hole still in it.

    🔴 A ROW THIS KEY CANNOT NAME IS SKIPPED, AND THAT IS WHY A BASE-LESS WRITE
    DELETES NOTHING IT CAN **NAME** rather than nothing at all. `held` counts
    only truthy keys and the loop below does `if not k: continue`, so an
    unnameable disk row is neither adopted nor rescued. **It is a deliberate
    trade, argued in each `*_key` docstring**: adopting a row nothing can match
    would duplicate it on EVERY write, and a collection that grows on every save
    reaches its cap and then refuses to save at all. Losing a row is bad; a
    lesson that can no longer be saved is worse.

    🔴🔴 THE ASYMMETRY WITH THE SHRINK GUARD IS DELIBERATE AND MUST NOT BE
    HARMONISED. The guard BUCKETS the unnameable under `NO_IDENTITY`; this
    function DROPS them. **One keeps them to CATCH a loss, the other drops them,
    CAUSING one**, and both are right for their own purpose: the guard must see
    everything, or a swap of two unnameable rows reads as no change at all; the
    merge must not adopt what it cannot match, or the file grows without bound.
    ⚠️ A session tidying the two into agreement would break one of them, and
    which one depends only on the direction it tidies. Pinned by
    `test_deletes_nothing_it_can_name.py`, which drives all three on one
    document.

    🔴 WHICH FUNCTION BUCKETS, said exactly, because the queue entry that asked
    for this paragraph named the wrong one and a corrected sentence is worth
    nothing if it points at the wrong code. It is NOT `marks_keys()`, which
    drops them like this function does, and correctly: its output is the
    browser's `base`, a list of strings, and a key nothing can match belongs in
    neither list. The bucketing is `keep_the_losing_copy`'s, and it is made of
    two halves that must stay together: the `keys=` lambda `write_marks` passes
    does NOT filter `None`, and the guard then counts `k or NO_IDENTITY`.
    ⚠️ Hand it `marks_keys` instead and the bucketing stops happening, because a
    filtered list can never yield the falsy key the `or` is watching for.
    🟢 **MEASURED rather than asserted, 2026-09-08, by running the whole suite
    with exactly that substitution: TWO tests object, and only two.**
    `test_guard_compares`'s replaced-mark test, which predates this paragraph,
    and `test_deletes_nothing_it_can_name`'s equal-count swap. **The first draft
    of this sentence said nothing at all would object; it was wrong, and the
    divergence is better guarded than I gave it credit for.**
    **Three functions, two treatments.**

    ⚠️ Live exposure re-measured 2026-09-08 on the reader's own machine, because
    it is exactly the number that goes stale: 179 marks sidecars, 1057 rows, 11
    bookmark rows, **zero unnameable**. The limit is real and nobody is standing
    in it.
    """
    held = Counter(k for k in (key_of(x) for x in mine) if k)
    seen, out = Counter(), []
    for x in theirs:
        k = key_of(x)
        if not k:
            continue
        if seen[k] < max(held[k], agreed[k]):
            seen[k] += 1
            continue
        out.append(x)
    return out


def _agreed_from(base):
    """`base` validated the one way it may be read, as a Counter.

    🔴 A MALFORMED `base` IS AN ERROR, NOT A FALLBACK, and `None` is not
    malformed: a writer too old to say has agreed to nothing, which is what `[]`
    means. Both rules are `merge_marks`'s and are kept identical here on purpose,
    because two sidecars disagreeing about what an absent base means is exactly
    the drift this project keeps paying for.
    """
    if base is None:
        base = []
    if (not isinstance(base, list) or len(base) > MAX_BASE_KEYS
            or not all(isinstance(k, str) for k in base)):
        raise ValueError("base must be a list of at most %d strings" % MAX_BASE_KEYS)
    return Counter(base)


def merge_marks(disk, payload):
    """Everything the writer sent, plus what the disk holds that the writer has
    never seen. Returns `(items, notes, kept)`.

    🔴 THE RULE, and it is the whole unit: **a write may delete only what the
    writer knows about.** A browser's knowledge is the collection it is sending
    plus `base`, the keys it last agreed the file held. A mark on disk that is in
    neither was made somewhere else since this browser last looked, so this write
    is not entitled to an opinion about it, and it is kept.

    Deleting still works, and by the same rule: a mark the reader removed is in
    `base` and not in the payload, so it is not adopted back.

    🔴 A PAYLOAD WITH NO `base` MAY NOT DELETE, and that is a RULING that
    reversed this function's first answer. It used to get whole-collection
    replacement, on the reasoning that "absent" means "this writer cannot say
    what it knew" and the honest answer to that is the behaviour it was written
    for. ⚠️ **That reasoning was sound while the server had two kinds of
    base-less writer** -- an old page, and `colour_purge`, which sent nothing
    while holding the file in its hand. Refusing to delete would have broken the
    purge, which exists to delete, and it would have gone on reporting the
    deletions it did not make.

    🟢 The purge now sends what it read, so there is exactly ONE class of
    base-less writer left: **a client too old to say.** Refusing to let that
    client delete is then not a compromise between two goods; it is the only
    correct reading of the only thing the value can now mean. Such a write
    adopts everything on the disk it did not send AND CAN NAME, and deletes
    nothing it can name.

    🔴 THOSE LAST FOUR WORDS WERE MISSING UNTIL 2026-09-08, in this docstring
    and in seven other places, and the short sentence is the argument
    `base = []` rests on. It was false: `_unseen` skips a disk row `mark_key`
    cannot name, so a base-less write sending only nameable marks DOES drop an
    unnameable one, reproduced on the rig at `faff12e` with `kept: 0`.
    ⚠️ THE MERGE IS RIGHT AND THE SENTENCE WAS WRONG, which is the whole
    finding: adopting a row nothing can match would duplicate it on every
    write (`mark_key` argues it in full), and an unnameable row is the shrink
    guard's to notice rather than this function's. The asymmetry that follows
    from that is written out once, at `_unseen`.

    `[]` is a different SENTENCE with the same effect, made by a browser that
    has this code and has agreed to nothing: "I know of nothing on disk." The
    two are kept apart because only one of them is worth logging.

    🔴 A MALFORMED `base` IS AN ERROR, NOT A FALLBACK. Ignoring it would drop
    silently back to the destructive path, which is the failure this exists to
    remove, and it would do it in the case where something is already wrong.

    ⚠️ THE BOUND, so nobody reads this as more than it is. The writer's own
    items are always kept, so a mark DELETED on another device and still held
    here is written back. That is today's behaviour too (the last writer's array
    wins outright), so nothing regresses, and closing it needs the reader's copy
    to be pruned at load, which is the load path and not this one.
    """
    items = payload.get("items") or []
    notes = payload.get("notes") or []
    # 🔴 A Counter and not a set, for the same reason `_unseen` counts: a base
    # naming one copy of a repeated phrase must not account for two. `None` and
    # `[]` are two different SENTENCES with the same effect, and `_agreed_from`
    # is where both are read, once, for every sidecar that has a base.
    agreed = _agreed_from(payload.get("base"))

    def unseen(theirs, mine, key_of):
        return _unseen(theirs, mine, agreed, key_of)

    keep_i = unseen(disk.get("items") or [], items, mark_key)
    keep_n = unseen(disk.get("notes") or [], notes, note_key)
    if not keep_i and not keep_n:
        return items, notes, 0
    return (items + _renumber(keep_i, items, groups=True),
            notes + _renumber(keep_n, notes),
            len(keep_i) + len(keep_n))


def write_marks(cfg, doc_id, payload):
    items = payload.get("items")
    notes = payload.get("notes")
    if not isinstance(items, list) or not isinstance(notes, list):
        raise ValueError("items and notes must both be lists")
    if len(items) > 2000 or len(notes) > 500:
        raise ValueError("too many marks")
    path = sidecar_path(cfg, doc_id, "marks")

    # 🔴 READ, MERGE, WRITE, under one lock, and the lock is the lesson
    # `LESSON_STATE_LOCK` above already records: this is read-modify-write on one
    # file inside a ThreadingHTTPServer, so two saves arriving together would
    # both read the old file and the second would erase what the first adopted.
    # `write_json_sidecar` takes WRITE_LOCK inside itself; that stays a different
    # lock, because taking it out here as well would deadlock on a plain
    # non-reentrant Lock.
    #
    # ⚠️ The 2000/500 caps above are a limit on what a client may SEND, not on
    # what the file may hold afterwards. A merge can push the file past them, and
    # refusing that would mean a reader cannot save because another device has
    # marks, which is a worse failure than a long file. It is bounded: a merge
    # only ever adopts marks that are already on the disk, so the file settles at
    # the union of the devices rather than growing on every write.
    if payload.get("base") is None:
        # 🔴 MEASURED, NOT ESTIMATED. Every internal caller says what it
        # knew, so a payload with no base is by construction a lesson page
        # older than the merge, still open somewhere and still saving. This
        # line is how the question "how many stale clients are actually out
        # there" gets an answer off a log instead of an argument about how long
        # a tab could persist. It is a log line, not a feature: the reader is
        # told by the page itself, which asks `/healthz` when it comes back.
        log(cfg, "marks %s: a write with NO BASE, from a page older than the "
                 "merge. It adopts what it never saw and can NAME, and deletes "
                 "nothing it can name."
            % doc_id)
    with MARKS_LOCK:
        items, notes, kept = merge_marks(read_marks(cfg, doc_id), payload)

        # 🔴 The snapshot and the losing copy, both in one place now: this
        # is where the guard was invented and it is no longer the only sidecar
        # that has it. `keep_the_losing_copy` carries the 2026-08-13 story.
        #
        # 🔴 Compared against the MERGED collection, because that is what is
        # about to be written. Comparing the payload would measure the disk
        # against something that never reaches it, which is a guard measuring
        # the wrong thing rather than a guard that is merely coarse.
        doc = {
            "doc": doc_id,
            "updated": int(payload.get("updated") or 0),
            "saved": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "items": items,
            "notes": notes,
        }
        # The document is built BEFORE the guard because the guard compares the
        # two collections rather than their sizes, so it needs the one about to
        # be written and not a number derived from it.
        #
        # 🟢 Marks is the one sidecar that can pass an identity today, and it
        # is the same `mark_key` the merge uses: the block and the anchor text.
        # That is what `reanchor()` preserves when the prose moves, and what QA
        # proved on 2026-09-01 the offsets do not.
        keep_the_losing_copy(cfg, path, "marks", doc_id,
                             lambda d: len(d.get("items") or []) + len(d.get("notes") or []),
                             doc,
                             keys=lambda d: (
                                 [mark_key(x) for x in (d.get("items") or [])]
                                 + [note_key(x) for x in (d.get("notes") or [])]))
        write_json_sidecar(path, doc)
    # `kept` is reported on every save, zero included, so its absence is
    # visible: a silent success and a merge that never ran look identical
    # otherwise, which is how a check stops being one.
    #
    # 🔴 `keys` is the writer's next `base`, and it is the FILE's keys rather
    # than the payload's. After a merge the file holds marks the writer never
    # sent, and a browser that recorded only what it sent would treat those as
    # unseen for ever: every later write would adopt them again, and a mark the
    # reader deleted could never be deleted at all. The client stores this
    # verbatim.
    return {"ok": True, "items": len(items), "notes": len(notes), "kept": kept,
            "keys": marks_keys(doc)}


def colour_count(doc, colour):
    """How many highlights in one marks document carry this colour.

    🔴 ONE FUNCTION, for the same reason `marks_keys` is one: the purge
    subtracts an AFTER from a BEFORE, and two spellings of "how many are this
    colour" would make that subtraction a comparison between two questions
    rather than one answer at two times.
    """
    return sum(1 for it in (doc.get("items") or [])
               if isinstance(it, dict) and it.get("c") == colour)


def colour_uses(cfg, colour):
    """R45. How many highlights carry this colour, across every lesson. The
    delete-a-colour confirm shows this so 'also delete its highlights' is a
    decision about a known number, not a guess."""
    count, lessons = 0, 0
    for path in sorted(cfg["notes_dir"].glob("*-marks.json")):
        data = read_json_sidecar(path, None, cfg) or {}
        n = colour_count(data, colour)
        if n:
            count += n
            lessons += 1
    return {"ok": True, "colour": colour, "count": count, "lessons": lessons}


def colour_purge(cfg, payload):
    """R45. Remove every highlight of one colour, in every lesson, going through
    write_marks per document so the shrink guard keeps a dated backup of each
    file it shrinks. The palette entry itself is retired by the client in the
    same breath; this only touches marks.

    🔴 THE ONE INTERNAL WRITER OF MARKS, which is why it carries the
    comments it does. Every other write arrives from a browser. That makes it
    the only caller that can say what it knew WITHOUT guessing, and after it
    started saying so, `base is None` means exactly one thing: a client too old
    to have the word.
    """
    colour = str(payload.get("c") or "")
    if not PALETTE_ID_RE.match(colour):
        raise ValueError("bad colour id")
    if colour == "k":
        raise ValueError("the default colour cannot be purged")
    removed, touched = 0, []
    for path in sorted(cfg["notes_dir"].glob("*-marks.json")):
        data = read_json_sidecar(path, None, cfg) or {}
        before_n = colour_count(data, colour)
        if not before_n:
            continue
        items = data.get("items") or []
        keep = [it for it in items
                if not (isinstance(it, dict) and it.get("c") == colour)]
        doc_id = str(data.get("doc") or path.name.rsplit("-marks.json", 1)[0])
        # 🔴 THE BASE IS NOT A GUESS HERE, and that is the whole reason
        # this caller sends one. It is the keys of the file this loop read four
        # lines earlier, so the merge deletes exactly the purged colour, adopts
        # anything it has never seen, and a mark another device made between the
        # read and the write SURVIVES. Sending nothing meant a plain replace,
        # which destroyed that mark.
        #
        # ⚠️ This makes the read-then-write SAFE, not ATOMIC. The race
        # window is unchanged; what changes is that landing in it no longer
        # costs a mark.
        write_marks(cfg, doc_id, {"items": keep,
                                  "notes": data.get("notes", []),
                                  "updated": data.get("updated") or 0,
                                  "base": marks_keys(data)})
        # 🔴 THE COUNT IS READ BACK FROM THE FILE, not from the
        # subtraction that decided what to send. `len(items) - len(keep)` is
        # this loop's INTENTION; it is a report about a decision, and the write
        # it describes happens afterwards, through a merge, under a lock, into a
        # file another device may be writing too. The number reaches the reader
        # as a sentence ("and 4 highlights with it"), so a number that cannot be
        # wrong is worth a second read of a file that is already in the page
        # cache.
        #
        # ⚠️ NOT A LIVE DEFECT the day this was written, and it should not be
        # re-filed as one: the write was a plain replace, so the intention and
        # the outcome agreed. It is what stops the next change to the merge
        # turning a wrong answer into a LYING one, which is the shape this
        # project has now paid for three times (the shrink guard, the restore
        # toast, and this).
        #
        # 🟢 The re-read beats anything `write_marks` could return, and
        # that is the argument for paying for it: a value handed back by the
        # writer is still the writer describing itself. The file is a separate
        # witness, and it catches a write that never landed at all.
        #
        # Read back by `doc_id` and not by `path`, because `doc_id` is where the
        # write went. The two can only differ on a hand-edited file whose `doc`
        # field disagrees with its own name, and there the honest question is
        # what happened to the file that was WRITTEN.
        after_n = colour_count(read_marks(cfg, doc_id), colour)
        gone = before_n - after_n
        if gone > 0:
            removed += gone
            touched.append(doc_id)
            log(cfg, "palette purge %s: %s lost %d mark(s)" % (colour, doc_id, gone))
        else:
            # Zero when the write did not take; negative when another device
            # added more of this colour while the sweep ran. Neither is a
            # removal, and neither is silent.
            log(cfg, "palette purge %s: %s WROTE and removed nothing: %d intended, "
                     "%d still on the file" % (colour, doc_id, before_n, after_n))
    return {"ok": True, "colour": colour, "removed": removed, "lessons": touched}


# --------------------------------------------------------------------------
# editing a paragraph of a note, in the note's own HTML
# --------------------------------------------------------------------------

BLOCK_TAGS = {"p", "li", "h1", "h2", "h3", "h4", "dd", "dt", "figcaption",
              "blockquote", "td", "th"}
SKIP_CLASSES = ("hl-panel", "sv-add", "sv-pop", "sv-edit")


class BlockFinder(HTMLParser):
    """Locate the same blocks the page's own highlight layer numbers.

    The page picks outermost `.wrap p|li|h*|dd|dt|figcaption|blockquote` with
    non-empty text, skipping its own furniture. This walks the file and produces
    the byte range of each such element's INNER html, in the same order.

    Two implementations of one rule is a standing hazard, so nothing here is
    trusted on its own: an edit must also send the text it believes is there,
    and a mismatch is refused rather than written.
    """

    def __init__(self, source):
        super().__init__(convert_charrefs=False)
        self.source = source
        self.line_start = [0]
        for line in source.splitlines(keepends=True):
            self.line_start.append(self.line_start[-1] + len(line))
        self.stack = []          # (tag, classes, is_block_candidate)
        self.blocks = []         # dicts with inner_start / inner_end
        self.open_block = None   # index into self.blocks
        self.block_depth = 0
        self.in_wrap = 0
        self.in_skip = 0

    def _pos(self):
        line, col = self.getpos()
        return self.line_start[line - 1] + col

    def handle_starttag(self, tag, attrs):
        classes = ""
        for k, v in attrs:
            if k == "class":
                classes = v or ""
        is_wrap = "wrap" in classes.split()
        is_skip = any(c in classes.split() for c in SKIP_CLASSES)

        candidate = (
            tag in BLOCK_TAGS
            and self.in_wrap > 0
            and self.in_skip == 0
            and self.open_block is None
        )
        if candidate:
            start_text = self.get_starttag_text() or ""
            self.blocks.append({
                "tag": tag,
                "outer_start": self._pos(),
                "inner_start": self._pos() + len(start_text),
                "inner_end": None,
            })
            self.open_block = len(self.blocks) - 1
            self.block_depth = 0
        elif self.open_block is not None and tag == self.blocks[self.open_block]["tag"]:
            self.block_depth += 1

        self.stack.append((tag, is_wrap, is_skip))
        if is_wrap:
            self.in_wrap += 1
        if is_skip:
            self.in_skip += 1

    def handle_startendtag(self, tag, attrs):
        # <br/> and friends open nothing.
        pass

    def handle_endtag(self, tag):
        if self.open_block is not None and tag == self.blocks[self.open_block]["tag"]:
            if self.block_depth == 0:
                self.blocks[self.open_block]["inner_end"] = self._pos()
                self.open_block = None
            else:
                self.block_depth -= 1

        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                _, was_wrap, was_skip = self.stack[i]
                if was_wrap:
                    self.in_wrap -= 1
                if was_skip:
                    self.in_skip -= 1
                del self.stack[i:]
                break


TAG_RE = re.compile(r"<[^>]+>")


def html_to_text(fragment):
    """Must match the browser's textContent, because that is what an edit sends
    as `expect` and a mismatch refuses the write. Tags contribute NOTHING, not a
    space: `<code>…138</code>.` is "138." on the page, and substituting a space
    made it "138 ." and refused every edit to a paragraph containing markup."""
    text = TAG_RE.sub("", fragment)
    text = html_mod.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def find_blocks(source):
    finder = BlockFinder(source)
    finder.feed(source)
    out = []
    for b in finder.blocks:
        if b["inner_end"] is None:
            continue
        inner = source[b["inner_start"]:b["inner_end"]]
        if not html_to_text(inner):
            continue
        # The outer span is what a RANGE edit removes: from the opening tag
        # to just past the closing one. inner_end is where "</tag" begins, so
        # the outer end is the next ">" after it.
        close = source.find(">", b["inner_end"])
        out.append({"tag": b["tag"], "start": b["inner_start"], "end": b["inner_end"],
                    "outer_start": b["outer_start"],
                    "outer_end": (close + 1) if close != -1 else b["inner_end"],
                    "inner": inner})
    return out


def note_path(cfg, doc_id):
    if not DOC_ID_RE.match(doc_id or ""):
        raise ValueError("bad doc id")
    matches = sorted(cfg["notes_dir"].glob("%s-*.html" % doc_id))
    matches = [m for m in matches if ".bak" not in m.name]
    if not matches:
        raise ValueError("no note file for %s" % doc_id)
    return matches[0]


INLINE_OK = re.compile(
    r"</?(?:strong|em|b|i|sub|sup|br|abbr)\s*/?>"
    r"|<code(?:\s+style=\"[\w\-:;.()% ]*\")?>|</code>"
    r"|<span(?:\s+(?:class|style)=\"[\w\-:;.()% ]*\")?>|</span>"
    r"|<a\s+href=\"https?://[^\"<>\s]+\"(?:\s+target=\"_blank\")?"
    r"(?:\s+rel=\"noopener\")?\s*>|</a>",
    re.I)


def sanitise_inner(fragment):
    """Only the inline markup the notes already use survives, and a link may only
    point at http or https. An edit is text, not a licence to inject markup into
    a page that also holds his highlights."""
    if len(fragment) > 20000:
        raise ValueError("replacement too long")
    holes = []

    def stash(m):
        holes.append(m.group(0))
        return "\x00%d\x00" % (len(holes) - 1)

    kept = INLINE_OK.sub(stash, fragment)
    if "<" in kept or ">" in kept:
        raise ValueError("only emphasis and plain links are allowed in an edit")
    kept = html_mod.escape(kept, quote=False)
    for i, tag in enumerate(holes):
        kept = kept.replace("\x00%d\x00" % i, tag)
    return kept


LINK_MD = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)")


def md_rules(out):
    """The markdown the edit surface takes, applied to text that has ALREADY
    been escaped. Split out of md_to_inline on 2026-08-15 so a table cell can
    run the same rules over a fragment whose allowed inline HTML was stashed
    away first."""
    out = LINK_MD.sub(
        lambda m: '<a href="%s" target="_blank" rel="noopener">%s</a>'
                  % (html_mod.escape(m.group(2), quote=True), m.group(1)),
        out)
    out = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", out, flags=re.S)
    out = re.sub(r"(?<![\w*])\*([^*\n]+?)\*(?![\w*])", r"<em>\1</em>", out)
    # Backticks carry the notes' monospace runs (DOIs, identifiers) through an
    # edit. The exact inline style is not preserved, the intent is.
    out = re.sub(r"`([^`\n]+)`",
                 r'<code style="font-family:var(--mono);font-size:.9em">\1</code>', out)
    return out


def md_to_inline(text):
    """The edit surface is plain text with **bold**, *italic* and [text](url).
    Everything else is escaped, so nothing can smuggle markup into a note."""
    return md_rules(html_mod.escape(text, quote=False)).strip()


def md_to_cell(text):
    """A table cell takes the same markdown as a paragraph, PLUS the literal
    inline HTML the notes already use inside cells.

    🔴 Measured across the 29 lessons on 2026-08-15, before writing this: of
    1,482 cells in 124 tables, 730 carry a <b>, 52 a link, 37 a <br> and 28 a
    classed <span>. Markdown expresses the first two and neither of the last
    two, so running a cell through md_to_inline, which escapes everything it
    does not know, would silently flatten a table on every rewrite. That is the
    same failure htmlToMd exists to prevent for paragraphs.
    """
    if len(text) > 20000:
        raise ValueError("that cell is too long")
    holes = []

    def stash(m):
        holes.append(m.group(0))
        return "\x00%d\x00" % (len(holes) - 1)

    kept = INLINE_OK.sub(stash, text)
    # 🔴 Anything left that looks like a tag is ESCAPED, not refused, because
    # this is a text surface and cells legitimately hold `p<0.001` and
    # `s/s > l/s`. Refusing them, which is what sanitise_inner does with raw
    # HTML, made three real tables in the corpus unrewritable. Escaping is both
    # safe (a <div> becomes visible text, never markup) and consistent with the
    # paragraph surface, which escapes everything it does not recognise.
    out = md_rules(html_mod.escape(kept, quote=False))
    for i, tag in enumerate(holes):
        out = out.replace("\x00%d\x00" % i, tag)
    return out.strip()


# --------------------------------------------------------------------------
# editing a whole table
#
# 🔴 A table is not a block. The page numbers `td` and `th` individually, so a
# table of four rows is nine blocks and the table itself has no index at all.
# Rewrite took the FIRST block of a selection, so selecting a table and asking
# for the table to be fixed rewrote its first header cell, which is what EH
# reported on 2026-08-15 with an essay sitting inside a <th>. What follows gives
# a table an address of its own, and replaces the whole element rather than the
# inside of one cell.
# --------------------------------------------------------------------------

class TableFinder(HTMLParser):
    """The byte range of every <table> inside .wrap, in document order.

    Unlike BlockFinder this records the OUTER range, tags included, because a
    table rewrite can change the number of rows and cells and therefore has to
    replace the element rather than its contents.
    """

    def __init__(self, source):
        super().__init__(convert_charrefs=False)
        self.line_start = [0]
        for line in source.splitlines(keepends=True):
            self.line_start.append(self.line_start[-1] + len(line))
        self.tables = []
        self.open_table = None
        self.depth = 0
        self.in_wrap = 0
        self.in_skip = 0
        self.stack = []

    def _pos(self):
        line, col = self.getpos()
        return self.line_start[line - 1] + col

    def handle_startendtag(self, tag, attrs):
        pass

    def handle_starttag(self, tag, attrs):
        classes = ""
        for k, v in attrs:
            if k == "class":
                classes = v or ""
        is_wrap = "wrap" in classes.split()
        is_skip = any(c in classes.split() for c in SKIP_CLASSES)

        if tag == "table" and self.in_wrap > 0 and self.in_skip == 0:
            if self.open_table is None:
                self.tables.append({
                    "start": self._pos(),
                    "open_tag": self.get_starttag_text() or "<table>",
                    "end": None,
                })
                self.open_table = len(self.tables) - 1
                self.depth = 0
            else:
                self.depth += 1

        self.stack.append((tag, is_wrap, is_skip))
        if is_wrap:
            self.in_wrap += 1
        if is_skip:
            self.in_skip += 1

    def handle_endtag(self, tag):
        if tag == "table" and self.open_table is not None:
            if self.depth == 0:
                self.tables[self.open_table]["end"] = self._pos() + len("</table>")
                self.open_table = None
            else:
                self.depth -= 1

        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                _, was_wrap, was_skip = self.stack[i]
                if was_wrap:
                    self.in_wrap -= 1
                if was_skip:
                    self.in_skip -= 1
                del self.stack[i:]
                break


def find_tables(source):
    finder = TableFinder(source)
    finder.feed(source)
    return [t for t in finder.tables if t["end"] is not None]


# The two continuation markers. A pipe table cannot express a cell that spans
# rows or columns, and the notes use both: 2 rowspans and 5 colspans across the
# corpus, all of them in the lesson EH was reading when he reported this.
# Refusing to edit those tables would have put the hole exactly where he is.
TABLE_ROWSPAN = "^"
TABLE_COLSPAN = "<"
RULE_ROW = re.compile(r"^:?-{2,}:?$")


def parse_md_table(text):
    """A pipe table into a rectangular grid of cell sources."""
    rows = []
    for raw in str(text).splitlines():
        line = raw.strip()
        if not line:
            continue
        if "|" not in line:
            raise ValueError("every row of a table is written with | between the cells")
        if line.startswith("|"):
            line = line[1:]
        if line.endswith("|") and not line.endswith("\\|"):
            line = line[:-1]
        cells = [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", line)]
        if cells and all(RULE_ROW.match(c or "") for c in cells):
            continue          # the ---|--- rule under the header, if one is written
        rows.append(cells)

    if len(rows) < 2:
        raise ValueError("a table needs a header row and at least one row under it")
    width = len(rows[0])
    if width < 1:
        raise ValueError("a table needs at least one column")
    for i, row in enumerate(rows):
        if len(row) != width:
            raise ValueError(
                "row %d has %d cells and the header has %d, so the table is not rectangular"
                % (i + 1, len(row), width))
    if any(c in (TABLE_ROWSPAN, TABLE_COLSPAN) for c in rows[0]):
        raise ValueError("the header row cannot continue another cell")
    return rows


def table_to_html(rows, open_tag, indent):
    """The grid back into the note's own markup, resolving the continuation
    markers into rowspan and colspan. Every marker is checked against what it
    claims to continue, because a marker pointing at nothing would silently
    drop a column."""
    height, width = len(rows), len(rows[0])

    def span_right(r, c):
        n = 1
        while c + n < width and rows[r][c + n] == TABLE_COLSPAN:
            n += 1
        return n

    def span_down(r, c):
        n = 1
        while r + n < height and rows[r + n][c] == TABLE_ROWSPAN:
            n += 1
        return n

    for r in range(height):
        for c in range(width):
            value = rows[r][c]
            if value == TABLE_ROWSPAN:
                up = r - 1
                while up >= 1 and rows[up][c] == TABLE_ROWSPAN:
                    up -= 1
                # 🔴 up must land on a BODY row. A ^ in the first row under the
                # header would otherwise give a header cell a rowspan reaching
                # out of thead and into tbody, which is not a table any browser
                # will lay out the way it reads in the editor.
                if up < 1 or rows[up][c] in (TABLE_COLSPAN, TABLE_ROWSPAN):
                    raise ValueError(
                        "the ^ in row %d column %d has no cell above it to continue: the row "
                        "above it is the header" % (r + 1, c + 1))
            elif value == TABLE_COLSPAN:
                left = c - 1
                while left >= 0 and rows[r][left] == TABLE_COLSPAN:
                    left -= 1
                if left < 0 or rows[r][left] == TABLE_ROWSPAN:
                    raise ValueError(
                        "the < in row %d column %d has no cell to its left to continue" % (r + 1, c + 1))

    def cell(tag, r, c):
        # The header is one row, so it can span columns but never rows.
        down = 1 if r == 0 else span_down(r, c)
        right = span_right(r, c)
        if down > 1 and right > 1:
            raise ValueError("a cell cannot span rows and columns at the same time")
        attr = ""
        if down > 1:
            attr = ' rowspan="%d"' % down
        elif right > 1:
            attr = ' colspan="%d"' % right
        return "<%s%s>%s</%s>" % (tag, attr, md_to_cell(rows[r][c]), tag)

    # The house style, taken from the notes rather than invented: the header
    # row on one line, body cells one per line. The layout is not cosmetic.
    # html_to_text drops tags without substituting anything, so `</td><td>` with
    # nothing between them reads as "QuetiapinePrevents", and that text is what
    # an edit compares against and what the vault export carries.
    pad = " " * indent
    head = "".join(cell("th", 0, c) for c in range(width)
                   if rows[0][c] != TABLE_COLSPAN)
    lines = [open_tag, "  <thead>", "    <tr>" + head + "</tr>", "  </thead>", "  <tbody>"]
    for r in range(1, height):
        lines.append("    <tr>")
        for c in range(width):
            if rows[r][c] in (TABLE_ROWSPAN, TABLE_COLSPAN):
                continue
            lines.append("      " + cell("td", r, c))
        lines.append("    </tr>")
    lines.append("  </tbody>")
    lines.append("</table>")
    # The first line lands where the old <table> started, so it carries no pad.
    return lines[0] + "".join("\n" + pad + line for line in lines[1:])


def backup_note(path):
    """A dated copy of a lesson page before an edit lands on it, named so that
    a second edit inside the same second cannot take the first copy's name:
    the survivor of that collision held the INTERMEDIATE page, and the text
    from before either edit, the one a person restoring wants, was gone. The
    three callers run under `WRITE_LOCK`, so the claim is against the clock
    here rather than against a thread. Bytes, not text, so the copy is the
    file and not a re-encoding of it."""
    path = Path(path)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    fd, bak = claim_backup(lambda n: split_lessons.backup_target(
        path, "%s.%s%s.bak" % (path.name, stamp, n)))
    with os.fdopen(fd, "wb") as out, open(path, "rb") as src:
        shutil.copyfileobj(src, out)
    return bak


def apply_table_edit(cfg, payload, doc_id, path):
    """Replace a whole <table>. Addressed by its position among the tables in
    .wrap rather than by a block index, because it has no block index, and
    guarded by the same rule as a paragraph edit: the caller sends the text it
    believes is there and a mismatch is refused rather than written."""
    index = payload.get("t")
    if not isinstance(index, int) or index < 0:
        raise ValueError("bad table index")
    expect = re.sub(r"\s+", " ", str(payload.get("expect") or "")).strip()
    if not expect:
        raise ValueError("an edit must say what it expects to replace")

    rows = parse_md_table(payload.get("text") or "")

    with WRITE_LOCK:
        source = path.read_text(encoding="utf-8")
        tables = find_tables(source)
        if index >= len(tables):
            raise ValueError("that table is not in the file (found %d)" % len(tables))
        table = tables[index]
        found = html_to_text(source[table["start"]:table["end"]])
        if found != expect:
            raise ValueError(
                "the table on disk is not the one you edited, so nothing was written. "
                "Reload the page and try again.")

        line_start = source.rfind("\n", 0, table["start"]) + 1
        new_html = table_to_html(rows, table["open_tag"], table["start"] - line_start)
        if not html_to_text(new_html):
            raise ValueError("an edit cannot empty a table")

        bak = backup_note(path)
        updated = source[:table["start"]] + new_html + source[table["end"]:]
        tmp = path.with_suffix(".html.tmp")
        tmp.write_text(updated, encoding="utf-8")
        os.replace(tmp, path)

    log_edit(cfg, doc_id, {
        "t": index,
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "how": payload.get("how") or "manual",
        "kind": "table",
        "instruction": str(payload.get("instruction") or "")[:1000],
        "was": found[:4000],
        "was_hash": hashlib.sha1(found.encode("utf-8")).hexdigest(),
        "now": html_to_text(new_html)[:4000],
    })
    # 🔴 The page reloads after this rather than repainting, because replacing a
    # table changes how many `td` and `th` blocks the document has and every
    # mark after it is addressed by block index. A reload renumbers and lets
    # reanchor() find each mark by its own text, which is the mechanism that
    # already exists for exactly this.
    return {"ok": True, "backup": bak.name, "table": index, "html": new_html, "reload": True}


RANGE_TAGS = ("p", "h2", "h3", "h4", "blockquote")


def md_to_blocks(md):
    """Markdown paragraphs into block elements, for the range editor.

    Deliberately small: blank lines separate blocks; "## " to "#### " open
    headings; "- " runs become a list; everything else is a paragraph, inline
    markup via the same md_to_inline every other edit uses. This is not a
    markdown engine and refuses nothing: a chunk it does not recognise is a
    paragraph, which is honest and visible on the page."""
    out = []
    for chunk in re.split(r"\n\s*\n", str(md or "").strip()):
        c = chunk.strip()
        if not c:
            continue
        m = re.match(r"^(#{2,4})\s+(.+)$", c, re.S)
        if m:
            tag = "h%d" % len(m.group(1))
            out.append("<%s>%s</%s>"
                       % (tag, md_to_inline(" ".join(m.group(2).split())), tag))
            continue
        if re.match(r"^[-*]\s", c):
            items = re.split(r"\n(?=[-*]\s)", c)
            lis = "".join(
                "<li>%s</li>" % md_to_inline(
                    " ".join(re.sub(r"^[-*]\s+", "", i.strip(), count=1).split()))
                for i in items if i.strip())
            out.append("<ul>%s</ul>" % lis)
            continue
        out.append("<p>%s</p>" % md_to_inline(" ".join(c.split())))
    return out


CORE_IDEAS_SUFFIX = "-core-ideas.md"


def core_ideas_path(cfg, unit_id):
    """Where a week's or a topic's core ideas live: `<unit>-core-ideas.md`.

    The convention is the outlines' (`<DOC>-outline.md`, read for the course
    digest) one level up, at the week and topic ids `unit_ids()` already
    derives and the ratings are already keyed on. So nothing new is invented to
    name the file, and a course whose ids are not W/T/P gets no core ideas for
    the same reason it gets no week ratings, rather than a file under a guess.

    🔴 The id is CHECKED rather than trusted, because this builds a path and
    an id can reach here from an imported pack, which is a stranger's text.
    Returns None for an id this refuses.
    """
    if not DOC_ID_RE.match(str(unit_id or "")):
        return None
    return cfg["notes_dir"] / (unit_id + CORE_IDEAS_SUFFIX)


def core_ideas_html(cfg, unit_id):
    """The rendered blocks for a unit, or "" when there is nothing written.

    ⚠️ The empty string carries the whole of the no-pill rule, which is the
    entry's own: a week or topic with nothing written shows NO pill, rather
    than a pill that opens an empty panel. So a file that exists but holds only
    whitespace has to come back empty too, and an unreadable one must not raise
    on a page that has nothing else to do with core ideas.
    """
    path = core_ideas_path(cfg, unit_id)
    try:
        if path is None or not path.is_file():
            return ""
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    return "".join(md_to_blocks(text))


def heading_words(head_html):
    """The plain words of a rendered heading, for a label read aloud.

    🔴 Stripping the TAGS is not enough, and the half that is missing is
    invisible on screen. A week heading is built as `Week 2 &middot; Normal
    child development`, so tag-stripping alone leaves the ENTITY in the text;
    the caller then escapes that text for an attribute and it becomes
    `&amp;middot;`, which a screen reader announces literally. Unescape before
    handing plain text to something that will escape it again.
    """
    return html_mod.unescape(re.sub(r"<[^>]+>", "", head_html)).strip()


def core_ideas_panel(cfg, unit_id, label):
    """The pill and its in-place panel, or "" for a unit with nothing written.

    `<details>`/`<summary>` is the in-place pattern this surface already uses
    (the gear, the glossary sections, the help page), which is what EH's
    *"not to open as a separate lesson page"* asks for without inventing a
    third interaction.
    """
    body = core_ideas_html(cfg, unit_id) if unit_id else ""
    if not body:
        return ""
    return ('<details class="hci"><summary class="hcip">Core ideas</summary>'
            '<div class="hcib" aria-label="%s">%s</div></details>'
            % (html_mod.escape(label, quote=True), body))


def apply_range_edit(cfg, payload, doc_id, path):
    """Replace a contiguous run of top-level blocks with new ones; the count
    may change (EH's queued design: "rewrite several paragraphs as one unit",
    with the constraint that a range edit must be able to change the block
    count, then reload-and-reanchor). Guarded like every edit: the caller
    sends what it believes each block says, and any mismatch refuses the
    whole write."""
    b0, b1 = payload.get("b0"), payload.get("b1")
    if not (isinstance(b0, int) and isinstance(b1, int) and 0 <= b0 < b1):
        raise ValueError("bad block range")
    expects = payload.get("expects")
    if not (isinstance(expects, list) and len(expects) == b1 - b0 + 1
            and all(isinstance(e, str) and e.strip() for e in expects)):
        raise ValueError("a range edit must say what it expects each "
                         "paragraph to hold")
    new_blocks = md_to_blocks(payload.get("text"))
    if not new_blocks:
        raise ValueError("an edit cannot empty a run of paragraphs")

    with WRITE_LOCK:
        source = path.read_text(encoding="utf-8")
        blocks = find_blocks(source)
        if b1 >= len(blocks):
            raise ValueError("that range is not in the file (found %d blocks)"
                             % len(blocks))
        run = blocks[b0:b1 + 1]
        for i, (blk, exp) in enumerate(zip(run, expects)):
            want = re.sub(r"\s+", " ", exp).strip()
            if html_to_text(blk["inner"]) != want:
                raise ValueError(
                    "paragraph %d on disk is not the one you edited, so "
                    "nothing was written. Reload the page and try again."
                    % (b0 + i))
            if blk["tag"] not in RANGE_TAGS:
                raise ValueError(
                    "paragraph %d is a %s, which a range edit cannot rewrite; "
                    "edit it on its own" % (b0 + i, blk["tag"]))
        # 🔴 The run must be structural siblings: if anything but whitespace
        # sits between two of its blocks (a closing section div, a figure),
        # cutting the outer spans would delete it and corrupt the document.
        # Refused with a reason a person can act on.
        for a, b in zip(run, run[1:]):
            between = source[a["outer_end"]:b["outer_start"]]
            if between.strip():
                raise ValueError(
                    "those paragraphs are separated by other content (a "
                    "section boundary or a figure), so they cannot be "
                    "rewritten as one. Rewrite them in smaller runs.")

        indent = " " * max(0, run[0]["outer_start"]
                           - (source.rfind("\n", 0, run[0]["outer_start"]) + 1))
        joined = ("\n\n" + indent).join(new_blocks)
        updated = (source[:run[0]["outer_start"]] + joined
                   + source[run[-1]["outer_end"]:])

        # Prove the surgery before writing: the block list must shrink and
        # grow by exactly the counts involved, and still parse. 🔴 Counted
        # with the SAME rule the page uses (a <ul> is not a block, its <li>
        # items are), by parsing the replacement inside a wrap of its own:
        # the readings blockmap learned this the same way.
        new_n = len(find_blocks('<div class="wrap">%s</div>' % joined))
        before_n = len(blocks)
        after = find_blocks(updated)
        if len(after) != before_n - len(run) + new_n:
            raise ValueError("the replacement did not parse into the expected "
                             "blocks, so nothing was written")

        bak = backup_note(path)
        tmp = path.with_suffix(".html.tmp")
        tmp.write_text(updated, encoding="utf-8")
        os.replace(tmp, path)

    was = " / ".join(html_to_text(b["inner"]) for b in run)
    log_edit(cfg, doc_id, {
        "b0": b0, "b1": b1,
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "how": payload.get("how") or "manual",
        "instruction": str(payload.get("instruction") or "")[:1000],
        "was": was[:4000],
        "was_hash": hashlib.sha1(was.encode("utf-8")).hexdigest(),
        "now": " / ".join(html_to_text(h) for h in new_blocks)[:4000],
        "blocks_before": len(run),
        "blocks_after": len(find_blocks('<div class="wrap">%s</div>' % joined)),
    })
    # 🔴 Reload, never repaint: the count changed (or may have), and every
    # mark after the range is addressed by block index. Same mechanism as a
    # table edit, and reanchor() finds each mark by its own text after.
    return {"ok": True, "backup": bak.name, "reload": True}


def apply_edit(cfg, payload):
    doc_id = payload.get("doc", "")
    path = note_path(cfg, doc_id)
    if payload.get("target") == "table":
        return apply_table_edit(cfg, payload, doc_id, path)
    if payload.get("target") == "range":
        return apply_range_edit(cfg, payload, doc_id, path)
    index = payload.get("b")
    if not isinstance(index, int) or index < 0:
        raise ValueError("bad block index")

    expect = re.sub(r"\s+", " ", str(payload.get("expect") or "")).strip()
    if not expect:
        raise ValueError("an edit must say what it expects to replace")

    if payload.get("text") is not None:
        new_inner = md_to_inline(str(payload.get("text")))
    else:
        new_inner = sanitise_inner(str(payload.get("html") or ""))
    if not html_to_text(new_inner):
        raise ValueError("an edit cannot empty a paragraph")

    with WRITE_LOCK:
        source = path.read_text(encoding="utf-8")
        blocks = find_blocks(source)
        if index >= len(blocks):
            raise ValueError("that paragraph is not in the file (found %d blocks)" % len(blocks))
        block = blocks[index]
        found = html_to_text(block["inner"])
        if found != expect:
            # Either the file changed under the page, or the two block-numbering
            # implementations disagree. Both mean: do not write.
            raise ValueError(
                "the paragraph on disk is not the one you edited, so nothing was "
                "written. Reload the page and try again.")

        bak = backup_note(path)
        updated = source[:block["start"]] + new_inner + source[block["end"]:]
        tmp = path.with_suffix(".html.tmp")
        tmp.write_text(updated, encoding="utf-8")
        os.replace(tmp, path)

    log_edit(cfg, doc_id, {
        "b": index,
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "how": payload.get("how") or "manual",
        "instruction": str(payload.get("instruction") or "")[:1000],
        "was": found[:4000],
        "was_hash": hashlib.sha1(found.encode("utf-8")).hexdigest(),
        "now": html_to_text(new_inner)[:4000],
    })
    # The page repaints from exactly what was written, so screen and file agree
    # without a reload.
    return {"ok": True, "backup": bak.name, "block": index, "html": new_inner}


def log_edit(cfg, doc_id, entry):
    """Every rewrite recorded, so a later regeneration of the note can tell an
    untouched paragraph from one he changed, and so there is a trail."""
    path = sidecar_path(cfg, doc_id, "edits")
    doc = read_json_sidecar(path, {"doc": doc_id, "edits": []})
    doc.setdefault("edits", []).append(entry)
    doc["edits"] = doc["edits"][-500:]
    doc["doc"] = doc_id
    write_json_sidecar(path, doc)


REWRITE_PROMPT = """You are rewriting one paragraph of a study lesson for the course {course}. The reader wrote the instruction below because the paragraph as it stands did not work for them.

THE PARAGRAPH AS IT STANDS:
{current}

WHERE IT SITS:
{context}

WHAT THEY ASKED FOR:
{instruction}

Return ONLY the replacement paragraph. No preamble, no explanation, no quotation marks around it.

House rules this note is written to, which the replacement must also obey:
- It must stand alone. Never mention lectures, slides, decks, transcripts or any lecturer by name. If the current text does, that is a defect to fix, not a style to copy.
- Transmit, never point. Never refer to something the reader cannot see. Carry the content across instead.
- Assert rather than recount. Say each thing once. No sentence whose only job is to introduce the next one.
- Dense, not narrative. They can read the source if they want the story.
- British spelling.
- NO EM DASHES, anywhere, ever. Use commas, parentheses, colons or separate sentences.
- Plain prose. **bold** and *italic* are the only markup allowed, used sparingly for the claim that matters.
"""

REWRITE_RANGE_PROMPT = """You are rewriting a RUN of consecutive paragraphs of a study lesson for the course {course}, as one unit. The reader wrote the instruction below because the run as it stands did not work for them.

THE PARAGRAPHS AS THEY STAND, separated by blank lines:
{current}

WHERE THEY SIT:
{context}

WHAT THEY ASKED FOR:
{instruction}

Return ONLY the replacement, as paragraphs separated by blank lines. No preamble, no explanation, no code fence. You may merge paragraphs, split them, reorder them, or change how many there are: that is the point of rewriting the run as one unit. A line starting "## ", "### " or "#### " is a heading at that level; a run of lines starting "- " is a bulleted list; keep a heading or a list only where one is genuinely wanted.

House rules this lesson is written to, which the replacement must also obey:
- It must stand alone. Never mention lectures, slides, decks, transcripts or any lecturer by name. If the current text does, that is a defect to fix, not a style to copy.
- Transmit, never point. Never refer to something the reader cannot see. Carry the content across instead.
- Assert rather than recount. Say each thing once. No sentence whose only job is to introduce the next one.
- Every claim in the run as it stands must survive unless the instruction asks for it to go. Do not quietly drop one to make the prose tidier.
- Dense, not narrative. British spelling.
- NO EM DASHES, anywhere, ever. Use commas, parentheses, colons or separate sentences.
- Plain prose. **bold** and *italic* are the only inline markup, used sparingly.
"""

REWRITE_TABLE_PROMPT = """You are rewriting one TABLE of a study lesson for the course {course}. The reader wrote the instruction below because the table as it stands did not work for them.

THE TABLE AS IT STANDS, as a pipe table:
{current}

WHERE IT SITS:
{context}

WHAT THEY ASKED FOR:
{instruction}

Return ONLY the replacement table as a pipe table, in the same grammar as the one above. No preamble, no explanation, no code fence, no prose before or after it.

The grammar, which is not ordinary markdown and matters:
- The first row is the header. Every row has the same number of cells.
- A cell containing only ^ means "the same cell as the one above continues down here". That is how a row-spanning cell is written.
- A cell containing only < means "the cell to my left continues across into this column".
- Cell content takes **bold**, *italic*, [label](https://url) and `code`. A literal <br> and the lesson's own <span class="..."> may be used and must be preserved where they already appear.
- A literal | inside a cell is written \\|.

You may change the number of rows and columns, the headers, and the order, if that is what the instruction asks for. Reshaping the table is usually the right answer when the reader says they cannot follow it.

House rules this note is written to, which the replacement must also obey:
- It must stand alone. Never mention lectures, slides, decks, transcripts or any lecturer by name.
- Transmit, never point. Never refer to something the reader cannot see.
- Every claim in the table as it stands must survive unless the instruction asks for it to go. Do not quietly drop a row to make the table tidier.
- British spelling.
- NO EM DASHES, anywhere, ever. Use commas, parentheses, colons or separate sentences.
"""


def do_rewrite(cfg, payload, host=HOST_HERE):
    kind, why = ask_backend(cfg, host)
    if not kind:
        return {"ok": False, "error": why}

    is_table = payload.get("target") == "table"
    is_range = payload.get("target") == "range"
    # 🔴 A table's newlines ARE its rows, so the whitespace collapse that is
    # right for a paragraph would arrive as one long line and come back as one.
    # A range's BLANK lines are its paragraph boundaries, so those survive too.
    if is_table:
        current = str(payload.get("current") or "")[:12000]
    elif is_range:
        paras = re.split(r"\n\s*\n", str(payload.get("current") or ""))
        current = "\n\n".join(" ".join(p.split()) for p in paras if p.strip())[:20000]
    else:
        current = re.sub(r"\s+", " ", str(payload.get("current") or ""))[:6000]
    instruction = re.sub(r"\s+", " ", str(payload.get("instruction") or ""))[:2000]
    context = re.sub(r"\s+", " ", str(payload.get("context") or ""))[:2000]
    if not current.strip():
        return {"ok": False, "error": "Nothing to rewrite."}
    if not instruction:
        return {"ok": False, "error": "Say what is wrong with it first."}

    course = str(cfg.get("module_name") or cfg.get("module") or "their course")
    tmpl = (REWRITE_TABLE_PROMPT if is_table
            else REWRITE_RANGE_PROMPT if is_range else REWRITE_PROMPT)
    prompt = tmpl.format(
        course=course, current=current, instruction=instruction,
        context=context or "(no surrounding context)")

    out = ask_claude(cfg, prompt, "the rewrite", host)
    if not out.get("ok"):
        return out
    text = out["text"]
    if is_range:
        return {"ok": True, "text": text, "model": chosen_model(cfg)}
    if is_table:
        # A model asked for a table will sometimes wrap it in a fence however
        # firmly it was told not to, and a fence is not a row.
        text = re.sub(r"^```[a-z]*\s*\n|\n?```\s*$", "", text.strip())
        try:
            parse_md_table(text)
        except ValueError as err:
            return {"ok": False, "error": "That came back in a shape a table cannot take: %s" % err}
        return {"ok": True, "text": text.strip(), "model": chosen_model(cfg)}
    return {"ok": True, "text": text, "html": md_to_inline(text), "model": chosen_model(cfg)}


def additions_path(cfg, doc_id):
    if not DOC_ID_RE.match(doc_id or ""):
        raise ValueError("bad doc id")
    return cfg["notes_dir"] / ("%s-additions.json" % doc_id)


def read_additions(cfg, doc_id):
    path = additions_path(cfg, doc_id)
    if not path.exists():
        return {"doc": doc_id, "items": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return {"doc": doc_id, "items": []}


def merge_additions(disk, sent, base):
    """Everything the writer sent, plus the kept notes on disk it never saw.

    🔴 THE SAME RULE, THE SAME HELPER, A DIFFERENT IDENTITY: **a write may
    delete only what the writer knows about.** `_unseen` is shared so there is
    one copy of that sentence; `addition_key` argues its own case.

    🔴 **AND ONE THING NEITHER OF THE FIRST TWO SIDECARS NEEDED: THE ADOPTED
    ROWS ARE RENUMBERED.** A kept note carries an `id` minted from the local
    list, so a row arriving from the other device can land on an id the writer
    is already using. **The page removes by id** (`adds.items.filter(x =>
    x.id !== it.id)`), so one press of Remove would take BOTH rows and the save
    behind it would delete the other device's note from the file. **That is
    exactly the defect QA found in the bookmarks pair**, arriving here through a
    different field, and it is why `_renumber` is called rather than the rows
    being appended as they are.

    ⚠️ **THE COUNTING IN `_unseen` IS NOT LOAD-BEARING HERE EITHER**, said
    rather than assumed: two kept notes share a key only by sharing a block AND
    a creation millisecond, which is the same note. The invariant the tests pin
    is that no two rows on disk ever share a key.

    🔴 A base-less writer adopts everything IT CAN NAME, and deletes nothing it
    can name, exactly as for marks and bookmarks. It is the only correct reading
    of the only thing an absent base can mean, which is "this client is too old
    to say". ⚠️ The qualifier was missing until 2026-09-08 and it is
    load-bearing: `_unseen` skips a disk row `addition_key` cannot name, so such
    a write does drop it. Why that is the right trade is at `_unseen`.
    """
    agreed = _agreed_from(base)
    rows = disk.get("items")
    keep = _unseen(rows if isinstance(rows, list) else [], sent, agreed,
                   addition_key)
    if not keep:
        return sent, 0
    return sent + _renumber(keep, sent), len(keep)


def write_additions(cfg, doc_id, payload):
    """The sidecar exists so a regenerated note never destroys his additions.
    The HTML is mine to rewrite; this file is his."""
    path = additions_path(cfg, doc_id)
    items = payload.get("items")
    if not isinstance(items, list) or len(items) > 500:
        raise ValueError("bad items")

    # 🔴 READ, MERGE, WRITE, under one lock, for the third time and for the
    # reason the marks path records: this is read-modify-write on one file
    # inside a threading server, so two devices saving together would both read
    # the old file and the second would erase what the first adopted.
    #
    # ⚠️ The 500 above is a limit on what a client may SEND, not on what the
    # file may hold afterwards. A merge can push it past 500, and truncating
    # there would drop the adopted rows, which are the other device's notes and
    # the one thing this whole unit exists to keep. It is bounded: a merge only
    # ever adopts rows that are already on the disk, so the file settles at the
    # union of the devices rather than growing on every write.
    if payload.get("base") is None:
        # 🔴 THE SAME LOG LINE AS THE OTHER THREE, and for the same reason:
        # every internal caller says what it knew, so a base-less write is by
        # construction a page older than this merge, still open somewhere and
        # still saving. It is how "are there stale clients out there" gets an
        # answer off a log rather than an argument about how long a tab lives.
        log(cfg, "additions %s: a write with NO BASE, from a page older than "
                 "the merge. It adopts what it never saw and can NAME, and "
                 "deletes nothing it can name."
            % doc_id)
    with ADDITIONS_LOCK:
        items, adopted = merge_additions(read_json_sidecar(path, {}, cfg),
                                         items, payload.get("base"))
        doc = {
            "doc": doc_id,
            "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "items": items,
        }
        # 🔴 Audit §3, and this is the worst of the four: `additions` is the
        # only sidecar holding content the reader CHOSE to keep and cannot
        # regenerate, and the docstring above promises a rewrite never destroys
        # it. A failed load presenting as `[]` plus one more addition kept that
        # promise about the rewrite and broke it about everything else.
        #
        # 🟢 IT NOW PASSES AN IDENTITY, which it could not before today. The
        # guard's own docstring says why that had to wait: a key must be chosen
        # against what CHANGES it, so the five remaining sidecars were five
        # separate questions. This one is answered, so the guard here is a
        # comparison rather than a tally, and a write that swaps one kept note
        # for another at equal count is no longer silent.
        #
        # 🔴 Against the MERGED document, because that is what is about to be
        # written. Comparing the payload would measure the disk against
        # something that never reaches it.
        #
        # ⚠️ ONE ENTRY PER ROW, `None` INCLUDED, which is the guard's contract
        # and the opposite of what the merge wants. The guard buckets an
        # unnameable row under `NO_IDENTITY` so that losing one is still
        # visible; dropping them here would make a row with no `ts` free to
        # disappear.
        keep_the_losing_copy(cfg, path, "additions", doc_id,
                             lambda d: len(d.get("items") or []),
                             doc,
                             keys=lambda d: [
                                 addition_key(x) if isinstance(x, dict) else None
                                 for x in (d.get("items") or [])])
        write_json_sidecar(path, doc)
    # `adopted` is reported on every save, zero included, so its absence is
    # visible: a silent success and a merge that never ran look identical
    # otherwise, which is how a check stops being one.
    return {"ok": True, "count": len(items), "adopted": adopted}


# --------------------------------------------------------------------------
# the reader shell: ONE copy of the reader, wrapped around content-only lessons
# --------------------------------------------------------------------------
#
# EH's decision, 2026-08-17: "the content lives in a file, and the actual
# system that displays it is something that's like the server and the display."
#
# A lesson file holds the note and nothing else: its title, the facts about it,
# its CSS and its `.wrap`. The reader (the highlight engine plus the local
# layer) lives here, in two files, and is wrapped around the content when the
# page is served. What the browser receives is byte for byte what the stamped
# files used to be, which is the whole safety argument: same DOM, same block
# numbering, so every existing mark still resolves. `split_lessons.py --check`
# is what proves it, and it proved it for all 29 before anything was split.
#
# 🔴 The practical win: editing `local-layer.html` now takes effect on the next
# page load. No apply_layer.py run, no 29 rewritten files, no 29 backups, and
# no possibility of a lesson drifting from the others.
#
# A file that still carries the old stamped layer is served exactly as it is, so
# a folder half migrated works, and so does going back.

VENDOR_DIR = Path(__file__).resolve().parent / "reader" / "vendor"
VENDOR_LOCK = VENDOR_DIR / "VENDOR.json"

# 🔴 The version is read from the lockfile, never typed here, and it appears in
# the URL rather than in the filename. That buys immutable caching for a 1.8MB
# asset without a rename in three places on every upgrade: bump the lockfile and
# every composed page asks for a URL no browser has cached. It also means a page
# composed before an upgrade cannot be handed the NEW file under the old URL,
# which is the quiet half of a cache-busting bug.
VENDOR_FILES = {"pdf.min.mjs", "pdf.worker.min.mjs", "LICENSE-pdfjs.txt"}


def vendor_version():
    """The pinned pdf.js version, or "" if the vendor folder is not there.

    Empty is a real answer rather than an error: a kit built without the vendor
    files still serves lessons, and the reader is told there is no viewer instead
    of asking for a file that cannot arrive."""
    try:
        data = json.loads(VENDOR_LOCK.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    return str((data.get("pdfjs") or {}).get("version") or "")


SHELL_PATH = Path(__file__).resolve().parent / "reader" / "shell.html"
LAYER_PATH = Path(__file__).resolve().parent / "local-layer.html"
PLAYER_PATH = Path(__file__).resolve().parent / "reader" / "player-controls.html"
# The lecture's clock, injected BEFORE `player.js` so the player captures it.
# A fourth reader part: it deploys on save like the other three, so it is in
# `READER_PARTS` for the same reason they are (the stamp), and the route reads
# it through the same cache.
CLOCK_PATH = Path(__file__).resolve().parent / "reader" / "player-clock.html"

_READER_LOCK = threading.Lock()
_READER_CACHE = {}      # path -> (mtime_ns, size, text, digest)

# The reader files that DEPLOY ON SAVE, and so are exactly the ones an open tab
# can go stale against. `study_server.py` itself is not here: the Python needs a
# restart, and `BUILD_ID` already digests it.
READER_PARTS = (LAYER_PATH, SHELL_PATH, PLAYER_PATH, CLOCK_PATH)


PACKAGE_ANCHOR = '<div id="content"></div>'
PACKAGE_MARK = "sv-player-controls"
PACKAGE_RATE_TOKEN = "@@PLAYBACK_RATE@@"
# 🔴 THE RATE THE DOCUMENT ACTUALLY PLAYS AT, which is a different fact
# from the one above and from the rate in the URL. The URL says what the
# frame was ASKED for; this says what it GOT. They differ when the rebuild
# raises and the package is served unscaled, and the reader's next speed
# change then scales from a rate the timeline was never built at and lands
# silently in the wrong place. Filled from what was DONE (`clocked` or
# `rebuilt`), never from the query.
#
# ⚠️ Since the clock (plans/11) this is the rate the STRIP sets the clock to
# at boot, so on a clocked document it is the whole mechanism rather than a
# report: an unfilled token is NaN on the page, the strip sets nothing, and
# the lecture plays at 1 with the shim inert.
PACKAGE_LECTURE_RATE_TOKEN = "@@LECTURE_RATE@@"

# 🔴 THE SECOND ANCHOR, AND WHY THE FIRST ONE CANNOT SERVE. The whole-lecture
# speed by clock (plans/11) works by replacing `Date.now` BEFORE `player.js`
# is parsed, because the player captures it into a closure variable as it is
# evaluated (`var Pa = Date.now || ...`, all seven builds). The content div is
# AFTER that script, so a shim placed there arrives one script too late and is
# never seen. This anchor is the opening of the tag itself, without the
# exporter's cache hash, which differs per package: `<script
# src="data/player.js?715B870A"></script>` is one of seven spellings of the
# rest. Measured 2026-09-05: present exactly once in 38 of 38, on the line
# before the content div in every one.
PACKAGE_CLOCK_ANCHOR = '<script src="data/player.js'
PACKAGE_CLOCK_MARK = "sv-player-clock"
# The query that asks for the clock rather than the rebuild. Its PRESENCE is
# the switch: a document served with it carries the shim and an untouched
# blob; one served without it is today's rebuild, byte for byte. The rate
# inside it fills the token above.
PACKAGE_CLOCK_QUERY = "clock_rate"


def inject_player_clock(html, shim):
    """Put the clock shim in front of a package's `player.js`.

    Returns the new HTML, or the ORIGINAL unchanged when it cannot be done,
    for the reason `inject_player_controls` gives: the package must always
    serve. A shim that cannot be placed leaves a document with no clock, the
    strip then announces `lectureLive: false`, and the reader's page falls
    back to the reload it has today.

    ⚠️ The shim is inert on its own. It starts at rate 1 and only the strip,
    which reads the rate from the token, ever moves it, so a document that
    somehow got the shim and not the strip plays exactly as before.

    Idempotent, by the script's own id: a document already carrying it is
    returned untouched.
    """
    if not html or not shim or PACKAGE_CLOCK_MARK in html:
        return html
    at = html.find(PACKAGE_CLOCK_ANCHOR)
    if at < 0:
        return html
    return html[:at] + shim + html[at:]


def inject_player_controls(html, controls, rate=None, lecture_rate=None):
    """Put our control strip into a mirrored package's own document.

    Returns the new HTML, or the ORIGINAL unchanged when it cannot be done.
    Every branch here fails towards "serve the package as it came", because a
    package that will not render is a lecture the reader cannot watch, and no
    control is worth that.

    🔴 WHY THE ANCHOR IS `<div id="content"></div>` AND NOT THE END OF THE
    BODY. The package boots from an inline script that runs at parse time,
    BEFORE `</body>`, and the whole point of this injection is to be running
    before it: `PresentationPlayer.start` has to be wrapped to keep the player
    handle, which the export's own `onPlayerInit` stub receives and drops on the
    floor (`(player);`). The content div sits after `player.js` has loaded and
    before that inline script, which is the only window there is.

    🟢 MEASURED ACROSS EVERY PACKAGE, not sampled: the anchor,
    `function onPlayerInit(player)`, `PresentationPlayer.start(presInfo` and
    `var presInfo = "` are each present in **38 of 38**. ⚠️ That mattered, because
    the packages are NOT one exporter version -- there are SEVEN, and the one
    every probe so far has used covers three of them. The structure is uniform
    where this depends on it and the API is uniform where the strip depends on
    it; both were checked rather than assumed. See `PROJECT-NOTES`.

    Idempotent: a document already carrying the mark is returned untouched, so
    a package that somehow ships one cannot get two.
    """
    if not html or PACKAGE_MARK in html or PACKAGE_ANCHOR not in html:
        return html
    if not controls:
        return html
    # 🔴 THE RATE IS RE-CLEANED HERE rather than trusted from the caller,
    # and it is not defensive habit: this value is interpolated into a script in
    # somebody else's document. `clean_rate` can only ever return a float from a
    # fixed list, so there is nothing a caller can pass that becomes anything
    # but a number. An unfilled token needs no guard of its own either:
    # `parseFloat("@@PLAYBACK_RATE@@")` is NaN, which is not in the list, so a
    # kit copy or a stale file falls back to 1 by construction.
    controls = controls.replace(PACKAGE_RATE_TOKEN, "%g" % clean_rate(rate))
    # 🔴 `lecture_rate` is the rate this document's TIMELINE was built at, so
    # the caller passes what it actually rebuilt rather than what was asked
    # for. Defaulting to 1 is the truth for an unscaled package, and it is
    # also what an unfilled token parses to on the page.
    controls = controls.replace(PACKAGE_LECTURE_RATE_TOKEN,
                                "%g" % clean_rate(lecture_rate))
    return html.replace(PACKAGE_ANCHOR, PACKAGE_ANCHOR + controls, 1)


def _reader_entry(path):
    """`(text, digest)` for one reader file, cached by mtime and size.

    So a saved edit is picked up without a restart, an unchanged file is not
    read 29 times a session, and 🔴 **the digest is paid ONCE PER SAVE rather
    than once per request**, which is what keeps `page_stamp()` off the hot
    path. Measured 2026-09-03 (`server/measure_page_cost.py`): the three files
    are 679KB, sha256 over all of them including the disk read is 0.278ms, and
    composing one lesson is 7.027ms. **A warm call here is 0.0015ms, a dict
    lookup, and that is what a request actually pays.**

    ⚠️ **The known limit, considered rather than missed**: mtime and size cannot
    see a file edited and reverted inside the same second at the same byte
    count. That is a hand-editing accident nobody has hit; the alternative is
    hashing 679KB on every request to catch it, which is the cost this design
    exists to avoid.
    """
    st = path.stat()
    key = str(path)
    with _READER_LOCK:
        hit = _READER_CACHE.get(key)
        if hit and hit[0] == st.st_mtime_ns and hit[1] == st.st_size:
            return hit[2], hit[3]
    text = path.read_text(encoding="utf-8")
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    with _READER_LOCK:
        _READER_CACHE[key] = (st.st_mtime_ns, st.st_size, text, digest)
    return text, digest


def read_reader_part(path):
    return _reader_entry(path)[0]


def page_stamp():
    """ONE value covering everything a composed page is made of.

    🔴 **Ruled by the manager 2026-09-03 (`4453c56`): one stamp, not two.**
    The notice used to compare `BUILD_ID` alone, which digests the PYTHON, so it
    was structurally blind to every layer, shell and player-controls change:
    exactly the class that deploys on save and therefore the only class that can
    go stale in a tab that is already open. **It was watching the half of the
    system that does not have the problem.**

    🟢 **Two comparisons would be two places to be half-right**, and would make
    "a server-only change still tells" depend on somebody remembering to keep
    both alive. One stamp makes those the same test.

    🔴 **Both halves are CONTENT-derived, never process-derived**, which is the
    fifth proof point the ruling added: a restart with no code change must not
    move it. Nothing here reads the clock, the pid or the start time, and
    `BUILD_ID` is itself a digest of the Python files.

    Never raises, for the same reason `_compute_build_id` does not: a health
    endpoint that fails is an outage of its own making.
    """
    h = hashlib.sha256()
    h.update(BUILD_ID.encode("utf-8") + b"\0")
    for path in READER_PARTS:
        h.update(path.name.encode("utf-8") + b"\0")
        try:
            h.update(_reader_entry(path)[1].encode("utf-8") + b"\0")
        except OSError:
            # A missing file is a DIFFERENT stamp from an empty one, so the
            # marker is not "".
            h.update(b"<unreadable>\0")
    return h.hexdigest()[:12]


# The home page. Deliberately one string with no assets: it is the page that has
# to render when everything else is misconfigured, including when there are no
# modules at all, so it depends on nothing it could fail to find. It follows the
# lessons' own palette and both themes, because a front door in the wrong colours
# reads as a different application.
# --------------------------------------------------------------------------
# One navigation, every page (plans/06, EH approved 2026-08-23)
# --------------------------------------------------------------------------
#
# Three slots, identical everywhere: crumbs (where you are; every segment is
# the way up), places (Core readings, Notebook, in one fixed order, scoped to
# where you are), and one gear menu holding setup and settings. Self-contained:
# the bar carries its own style, so six templates cannot drift apart.

NAV_CSS = """<style>
  .snav { display:flex; align-items:center; gap:14px; flex-wrap:wrap;
          margin:0 0 20px; font:500 13.5px var(--text, sans-serif); }
  .snav a { color:var(--accent, #1C6D61); text-decoration:none; }
  .snav a:hover { text-decoration:underline; }
  .sn-crumbs { display:flex; align-items:center; gap:7px; flex-wrap:wrap;
               min-width:0; }
  .sn-crumbs .sep { color:var(--muted, inherit); }
  .sn-crumbs .here { color:var(--muted, inherit); }
  .sn-places { display:flex; gap:14px; margin-left:auto; }
  .sn-places a.on { color:var(--ink, inherit); pointer-events:none; }
  .sn-gear { position:relative; }
  .sn-gear summary { list-style:none; cursor:pointer; color:var(--accent, #1C6D61);
                     font-size:16px; line-height:1; padding:2px 4px;
                     border-radius:6px; }
  .sn-gear summary::-webkit-details-marker { display:none; }
  .sn-gear[open] summary { background:var(--accent-wash, transparent); }
  .sn-menu { position:absolute; right:0; top:26px; z-index:60; min-width:200px;
             background:var(--surface, #fff); border:1px solid var(--rule, #8884);
             border-radius:10px; padding:6px; box-shadow:0 8px 28px #0003; }
  .sn-menu a { display:block; padding:8px 11px; border-radius:7px;
               color:var(--ink, inherit); }
  .sn-menu a:hover { background:var(--accent-wash, transparent);
                     text-decoration:none; }
</style>"""


def nav_bar(base_cfg, module_id=None, course_name=None, here=None):
    """The bar. `here` names the current sub-page as the final, unlinked crumb
    and lights the matching place. No module: the home level."""
    esc = html_mod.escape
    murl = module_url(base_cfg, module_id) if module_id else ""

    crumbs = ['<a href="/">Courses</a>']
    if module_id:
        crumbs.append('<span class="sep">&rsaquo;</span>')
        crumbs.append('<a href="%s">%s</a>'
                      % (esc(murl, quote=True), esc(course_name or module_id)))
    if here:
        crumbs.append('<span class="sep">&rsaquo;</span>')
        crumbs.append('<span class="here">%s</span>' % esc(here))

    places = []
    if module_id:
        places.append('<a href="%sreadings"%s>Core readings</a>'
                      % (esc(murl, quote=True),
                         ' class="on"' if here == "Core readings" else ""))
        places.append('<a href="%snotebook"%s>Notebook</a>'
                      % (esc(murl, quote=True),
                         ' class="on"' if here == "Notebook" else ""))
    else:
        places.append('<a href="/notebook"%s>Notebook</a>'
                      % (' class="on"' if here == "Notebook" else ""))

    gear = []
    if module_id:
        gear.append('<a href="%sstart">Course setup</a>' % esc(murl, quote=True))
        gear.append('<a href="%shelp">Step-by-step guide</a>' % esc(murl, quote=True))
    else:
        gear.append('<a href="/addcourse">Add a course</a>')
        gear.append('<a href="/help">Step-by-step guide</a>')
    gear.append('<a href="/settings">Settings</a>')

    return (NAV_CSS
            + '<div class="snav">'
            + '<nav class="sn-crumbs" aria-label="Where you are">%s</nav>' % "".join(crumbs)
            + '<nav class="sn-places" aria-label="Places in this course">%s</nav>' % "".join(places)
            + '<details class="sn-gear"><summary aria-label="Setup and settings">'
              '&#9881;</summary><nav class="sn-menu">%s</nav></details>' % "".join(gear)
            + '</div>')


HOME_PAGE = """<!-- study-home -->
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
%(icons)s<title>%(tab)s</title>
<style>
  :root {
    --paper:#F1F4F3; --surface:#FBFCFC; --ink:#1A2830; --ink-soft:#3E535C; --muted:#5F7178;
    --rule:#D8E0DE; --accent:#1C6D61; --accent-wash:#DDEBE7;
    --display:"Iowan Old Style","Palatino Linotype",Palatino,"Book Antiqua",Georgia,serif;
    --text:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      --paper:#12191C; --surface:#182126; --ink:#E7EEEC; --ink-soft:#B7C6C4; --muted:#8AA0A0;
      --rule:#26343A; --accent:#5FBFAE; --accent-wash:#173029;
    }
  }
  :root[data-theme="dark"] {
    --paper:#12191C; --surface:#182126; --ink:#E7EEEC; --ink-soft:#B7C6C4; --muted:#8AA0A0;
    --rule:#26343A; --accent:#5FBFAE; --accent-wash:#173029;
  }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--paper); color:var(--ink); font-family:var(--text);
         line-height:1.5; -webkit-font-smoothing:antialiased; }
  .wrap { max-width: 880px; margin: 0 auto; padding: 48px 20px 64px; }
  h1 { font-family:var(--display); font-weight:600; font-size:2rem; margin:0 0 4px;
       letter-spacing:-.01em; display:flex; align-items:center; gap:11px; }
  /* Beside the name, not above it: a stacked mark pushes the first course card
     under the fold on a phone, and the cards are what the page is for. */
  h1 .mark { display:inline-flex; width:1.05em; height:1.05em; color:var(--accent);
             flex:0 0 auto; }
  h1 .mark svg { width:100%%; height:100%%; display:block; }
  .sub { color:var(--muted); margin:0; font-size:.95rem; }
  /* 2026-08-21: the home page had no way to reach Settings and no way to add a
     course, which on a phone left it with nothing to do but open a lesson.
     The nav wraps under the title rather than shrinking it. */
  .topbar { display:flex; align-items:flex-start; gap:16px; flex-wrap:wrap;
            margin:0 0 32px; }
  .titles { flex:1 1 auto; min-width:0; }
  .topnav { display:flex; gap:14px; flex-wrap:wrap; align-items:baseline;
            padding-top:6px; }
  .topnav a, .topnav button {
    font:600 .85rem var(--text); color:var(--accent); background:none; border:0;
    padding:0; text-decoration:none; cursor:pointer; white-space:nowrap;
    border-bottom:1px solid transparent;
  }
  .topnav a:hover, .topnav button:hover { border-bottom-color:var(--accent); }
  /* The add-course CARD was retired 2026-08-30 (EH: "it's a little annoying").
     What is left is a button; the three questions live at /addcourse. */
  .addcta { margin-top:22px; display:flex; gap:14px; align-items:baseline;
            flex-wrap:wrap; }
  .addcta p { margin:0; color:var(--muted); font-size:.85rem; }
  /* 🔴 THE INK IS A TOKEN, NOT A LITERAL, AND THIS IS THE ONE PLACE
     THE REASON IS WRITTEN DOWN. `--accent` is `#1C6D61` in light and `#5FBFAE`
     in dark: it is deliberately lightened for dark, where it works as a border
     or a text colour and STOPS WORKING as a background under white. White on
     the dark accent is 2.20:1 against a 4.5 requirement. `--paper` moves with
     it (`#F1F4F3` / `#12191C`), giving 5.57 light and 8.08 dark.

     🟢 Those two numbers are not this file's arithmetic alone. QA measured
     the focus ring, which is the same pair inverted, at 5.57 / 8.08 in a live
     browser. A computed value and a measurement agreeing to two decimal places
     is why this shipped as a token swap rather than as a new colour.

     🔴 SEVEN rules had this pair and all seven were found by PROPERTY,
     not from a list: every block with `background: var(--accent)` and a literal
     white ink. A list of seven selectors is unbounded by construction, and the
     eighth is written by whoever adds the next button. `test_contrast.py`
     asserts the RATIO, so a future palette that is equally unreadable fails
     even though no name in it matches anything. */
  .addbtn { display:inline-block; font-size:.92rem; font-weight:600;
            padding:9px 18px; border-radius:9px; text-decoration:none;
            color:var(--paper); background:var(--accent); border:1px solid var(--accent); }
  .addbtn:hover { filter:brightness(1.08); }
  .addbtn:focus-visible { outline:2px solid var(--accent); outline-offset:2px; }
  .grid { display:grid; gap:14px; grid-template-columns:repeat(auto-fill,minmax(260px,1fr)); }
  .card { background:var(--surface); border:1px solid var(--rule); border-radius:12px;
          overflow:hidden; display:flex; flex-direction:column; }
  .card .main { display:block; padding:18px 18px 16px; text-decoration:none; color:inherit; }
  .card .main:hover { background:var(--accent-wash); }
  .card h2 { font-family:var(--display); font-size:1.15rem; font-weight:600; margin:0 0 6px;
             line-height:1.3; }
  .card .code { margin:0; color:var(--muted); font-size:.78rem; letter-spacing:.06em;
                text-transform:uppercase; }
  .card .n { margin:10px 0 0; color:var(--ink-soft); font-size:.9rem; }
  .card .cont { display:block; padding:10px 18px; border-top:1px solid var(--rule);
                color:var(--accent); text-decoration:none; font-size:.85rem; }
  .card .cont:hover { background:var(--accent-wash); }
  .empty { background:var(--surface); border:1px dashed var(--rule); border-radius:12px;
           padding:28px; grid-column:1 / -1; }
  .empty h2 { font-family:var(--display); margin:0 0 8px; font-weight:600; }
  .empty p { margin:0; color:var(--ink-soft); max-width:52ch; }
  code { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:.85em;
         background:var(--accent-wash); padding:1px 5px; border-radius:4px; }
  footer { margin-top:40px; padding-top:14px; border-top:1px solid var(--rule);
           color:var(--muted); font-size:.78rem; line-height:1.7; }
  footer b { font-weight:600; color:var(--ink-soft); }
</style>
<div class="wrap">
  %(navbar)s
  <div class="topbar">
    <div class="titles">
      <h1>%(title)s</h1>
      <p class="sub">%(sub)s</p>
    </div>
  </div>
  <div class="grid">%(body)s</div>
  %(extra)s
<div class="upnote" id="upnote" hidden></div>
<style>
  .upnote { position:fixed; right:16px; bottom:16px; z-index:70; max-width:340px;
            background:var(--surface); border:1px solid var(--accent);
            border-radius:12px; padding:14px 16px; box-shadow:0 8px 28px #0003;
            font-size:.88rem; color:var(--ink-soft); }
  .upnote[hidden] { display:none; }
  .upnote b { color:var(--ink); }
  .upnote a { color:var(--accent); }
  .upnote .dismiss { float:right; border:0; background:none; color:var(--muted);
                     cursor:pointer; font-size:1rem; padding:0 0 0 8px; }
</style>
<script>
window.addEventListener('load', function () {
  /* Quiet, honest, and off by default: the server answers "off" until an
     update URL is configured, and a broken feed never nags. Dismissing keeps
     it away until a NEWER version appears than the one dismissed. */
  var box = document.getElementById('upnote');
  if (!box) return;
  var headers = window.STUDYTOKEN ? window.STUDYTOKEN.headers({}) : {};
  fetch('/api/update', { headers: headers })
    .then(function (r) { return r.json(); })
    .then(function (r) {
      if (!r || !r.ok || !r.newer) return;
      try {
        if (localStorage.getItem('sv-update-dismissed') === r.latest) return;
      } catch (e) {}
      var link = r.url ? '<a href="' + String(r.url).replace(/"/g, '&quot;') +
        '" target="_blank" rel="noopener">Get it</a> \u00b7 ' : '';
      box.innerHTML = '<button type="button" class="dismiss" aria-label="Dismiss">\u00d7</button>' +
        '<b>A newer Study Hub is out</b> (' +
        String(r.latest).replace(/[<>&]/g, '') + ').' +
        (r.note ? ' ' + String(r.note).replace(/[<>&]/g, '') : '') + '<br>' + link +
        'To upgrade: replace the kit\u2019s server and .claude folders with the new ones. ' +
        '<b>Your lessons, marks and settings are not in those folders</b> and are never touched.';
      box.hidden = false;
      box.querySelector('.dismiss').addEventListener('click', function () {
        box.hidden = true;
        try { localStorage.setItem('sv-update-dismissed', r.latest); } catch (e) {}
      });
    })
    .catch(function () {});
});
</script>
  <footer>
    <b>Courses folder</b> %(root)s<br>
    <b>Server</b> %(server)s<br>
    <b>Vault</b> %(vault)s
  </footer>
</div>
"""


# --------------------------------------------------------------------------
# R51: everything he has marked, across a module or across all of them
# --------------------------------------------------------------------------
#
# Asked 2026-08-16: "we also need the ability to look up all of the different
# notebook items for a whole course or all ones courses."
#
# The Notebook panel shows one lesson's notes, cards, highlights and bookmarks.
# There has never been a view of the module, so "where did I write something
# about quetiapine" meant opening 29 lessons. This gathers them.
#
# 🔴 Read only, and it never opens a lesson's HTML. Titles come from the
# materials index, which already holds one per doc, so gathering 29 lessons'
# worth of marks costs four small JSON reads each and nothing else.

NOTEBOOK_KINDS = ("note", "highlight", "card", "bookmark")

# The facts a lesson carries about itself: week, week title, topic, topic number,
# part. Read from the `lesson-meta` block at the top of each content file, which
# is authoritative and is what the vault writer already uses. Only the head of
# each file is read, so indexing 29 lessons is 29 small reads.
_META_LOCK = threading.Lock()
_META_CACHE = {}          # (folder, mtime_ns) -> {doc: meta}


def lesson_meta_index(mcfg):
    folder = mcfg["notes_dir"]
    try:
        key = (str(folder), folder.stat().st_mtime_ns)
    except OSError:
        return {}
    with _META_LOCK:
        hit = _META_CACHE.get(key)
    if hit is not None:
        return hit
    out = {}
    for path in split_lessons.lessons_in(folder):
        try:
            with path.open("r", encoding="utf-8") as fh:
                head = fh.read(4096)
        except OSError:
            continue
        i = head.find(split_lessons.META_OPEN)
        j = head.find(split_lessons.META_CLOSE, i) if i >= 0 else -1
        if i < 0 or j < 0:
            continue
        try:
            meta = json.loads(head[i + len(split_lessons.META_OPEN):j])
        except ValueError:
            continue
        if isinstance(meta, dict) and meta.get("doc"):
            meta["file"] = path.name
            out[meta["doc"]] = meta
    with _META_LOCK:
        _META_CACHE.clear()
        _META_CACHE[key] = out
    return out


# R51, extended the same day on his instruction: "I'd also allow
# grouping/searching by topic and week as well." So a notebook item carries its
# week and topic, the search matches them, and the page can be grouped by either.
GROUPINGS = ("lesson", "week", "topic", "kind")




def notebook_items(cfg, module_id):
    """Every notebook item in one module, newest lesson order preserved."""
    mcfg = module_cfg(cfg, module_id)
    folder = mcfg["notes_dir"]
    titles = {}
    try:
        docs = json.loads((folder / "materials.json").read_text(encoding="utf-8")).get("docs", {})
        titles = {d: (v.get("title") or "") for d, v in docs.items() if isinstance(v, dict)}
    except (OSError, ValueError):
        pass

    meta_by_doc = lesson_meta_index(mcfg)
    out = []
    seen_docs = set()
    for path in sorted(folder.glob("*-marks.json")):
        if ".bak" in path.name or ".shrank-" in path.name:
            continue
        seen_docs.add(path.name[:-len("-marks.json")])
    for path in sorted(folder.glob("*-cards.json")):
        if ".bak" not in path.name:
            seen_docs.add(path.name[:-len("-cards.json")])
    for path in sorted(folder.glob("*-bookmarks.json")):
        if ".bak" not in path.name:
            seen_docs.add(path.name[:-len("-bookmarks.json")])

    for doc in sorted(seen_docs):
        if not DOC_ID_RE.match(doc):
            continue
        meta = meta_by_doc.get(doc) or {}
        href = meta.get("file") or ""
        if not href:
            try:
                href = note_path(mcfg, doc).name
            except ValueError:
                # 🔴 READINGS is a document with no file: it is composed from
                # readings.json on request. Without this its notebook items
                # would list with no link, which reads as broken.
                href = "readings" if doc == "READINGS" else ""
        title = titles.get(doc) or meta.get("title") or (
            "Core readings" if doc == "READINGS" else doc)
        week = str(meta.get("week") or "").lstrip("0") or (
            doc.split("-")[0][1:] if doc.startswith("W") else "")
        week_title = meta.get("weekTitle") or ""
        topic = meta.get("topic") or ""
        topic_no = str(meta.get("topicNo") or "")
        part = str(meta.get("part") or "")
        where = {"week": week, "weekTitle": week_title, "topic": topic,
                 "topicNo": topic_no, "part": part}
        marks = read_json_sidecar(sidecar_path(mcfg, doc, "marks"), {})
        for it in marks.get("items") or []:
            out.append(dict(where, **{"kind": "highlight", "doc": doc, "title": title, "href": href,
                        "text": it.get("t") or "", "note": it.get("n") or "",
                        "colour": it.get("c") or "k", "block": it.get("b"),
                        "orphan": bool(it.get("orphan")), "id": it.get("id")}))
        for n in marks.get("notes") or []:
            out.append(dict(where, **{"kind": "note", "doc": doc, "title": title, "href": href,
                        "text": n.get("text") or "", "at": n.get("ts"), "id": n.get("id")}))
        cards = read_json_sidecar(sidecar_path(mcfg, doc, "cards"), {}).get("cards") or {}
        if isinstance(cards, dict):
            for term, c in cards.items():
                if not isinstance(c, dict):
                    continue
                out.append(dict(where, **{"kind": "card", "doc": doc, "title": title, "href": href,
                            "text": c.get("term") or term, "note": c.get("def") or "",
                            "at": c.get("at"), "source": c.get("source") or "",
                            "url": c.get("url") or ""}))
        marksb = read_json_sidecar(sidecar_path(mcfg, doc, "bookmarks"), {})
        for b in marksb.get("items") or marksb.get("bookmarks") or []:
            if not isinstance(b, dict):
                continue
            out.append(dict(where, **{"kind": "bookmark", "doc": doc, "title": title, "href": href,
                        "text": b.get("t") or b.get("text") or "", "block": b.get("b"),
                        "at": b.get("at"), "id": b.get("id")}))
    return out


# --------------------------------------------------------------------------
# searching the lessons themselves, not only what he marked in them
# --------------------------------------------------------------------------
#
# Asked 2026-08-16: "There is a search function I noticed on the lesson level,
# but I'm not sure what it actually searches."
#
# 🔴 The hub's box searches a `hay` string per lesson: the code, the week title,
# the topic title and a hand-written key-terms list. Across 29 lessons that is
# about 2,100 words. **The prose of the lessons has never been searchable at
# all**, which is why "where did quetiapine come up" had no answer.
#
# This searches the text of every block, which is the same unit a highlight
# anchors to, so a hit can be linked to the exact paragraph. Plain substring
# matching, case and accent folded: the whole corpus is about 1.2MB, so an index
# would be a maintenance burden for no measurable gain.

_TEXT_LOCK = threading.Lock()
_TEXT_CACHE = {}          # (folder, mtime_ns) -> [(doc, file, block_i, text)]


def lesson_text_index(mcfg):
    folder = mcfg["notes_dir"]
    try:
        key = (str(folder), folder.stat().st_mtime_ns)
    except OSError:
        return []
    with _TEXT_LOCK:
        hit = _TEXT_CACHE.get(key)
    if hit is not None:
        return hit
    rows = []
    for path in sorted(folder.glob("W*-T*-P*.html")):
        if ".bak" in path.name or ".lesson." in path.name:
            continue
        doc = path.name.split("-")
        doc = "-".join(doc[:3]) if len(doc) >= 3 else path.stem
        try:
            src = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for i, b in enumerate(find_blocks(src)):
            text = html_to_text(b["inner"])
            if text:
                rows.append((doc, path.name, i, text))
    with _TEXT_LOCK:
        _TEXT_CACHE.clear()
        _TEXT_CACHE[key] = rows
    return rows


def search_lessons(cfg, module_id, query, limit=60):
    """Blocks whose text contains the query, with a snippet around each hit."""
    q = (query or "").strip().lower()
    if len(q) < 2:
        return []
    out = []
    for mid in ([module_id] if module_id else sorted(resolve_modules(cfg))):
        try:
            mcfg = module_cfg(cfg, mid)
        except ValueError:
            continue
        titles = lesson_meta_index(mcfg)
        for doc, fname, i, text in lesson_text_index(mcfg):
            at = text.lower().find(q)
            if at < 0:
                continue
            start = max(0, at - 90)
            end = min(len(text), at + len(q) + 130)
            snippet = ("…" if start else "") + text[start:end] + ("…" if end < len(text) else "")
            meta = titles.get(doc) or {}
            out.append({"module": mid, "url": module_url(cfg, mid), "doc": doc, "file": fname,
                        "block": i, "snippet": snippet, "at": at - start + (1 if start else 0),
                        "hit": text[at:at + len(q)],
                        "title": meta.get("title") or doc,
                        "week": str(meta.get("week") or "").lstrip("0"),
                        "weekTitle": meta.get("weekTitle") or "",
                        "topic": meta.get("topic") or "",
                        "topicNo": str(meta.get("topicNo") or "")})
            if len(out) >= limit:
                return out
    return out

def notebook_group_key(item, how):
    """(sort key, heading) for one item under a given grouping."""
    if how == "week":
        w = str(item.get("week") or "")
        head = ("Week " + w + (": " + item["weekTitle"] if item.get("weekTitle") else "")
                if w else "No week recorded")
        return ("%03d" % int(w) if w.isdigit() else "999", head)
    if how == "topic":
        w = str(item.get("week") or "")
        tn = str(item.get("topicNo") or "")
        head = (("Week %s, topic %s" % (w, tn) if w and tn else "Topic")
                + (": " + item["topic"] if item.get("topic") else ""))
        return ("%03d-%03d" % (int(w) if w.isdigit() else 999,
                               int(tn) if tn.isdigit() else 999), head)
    if how == "kind":
        k = item.get("kind") or "item"
        return (k, {"note": "Notes", "highlight": "Highlights",
                    "card": "Cards", "bookmark": "Bookmarks"}.get(k, k.title()))
    doc = item.get("doc") or ""
    if doc == "READINGS":
        # The doc id is scaffolding here: "READINGS Core readings" says the
        # same thing twice, once in the wrong voice.
        return (doc, item.get("title") or "Core readings")
    return (doc, "%s  %s" % (doc, item.get("title") or ""))


def notebook_summary(cfg, module_id=None, query=""):
    """One module, or every module. `query` filters on the item's own words."""
    mods = [module_id] if module_id else sorted(resolve_modules(cfg))
    q = (query or "").strip().lower()
    groups = []
    counts = dict.fromkeys(NOTEBOOK_KINDS, 0)
    for mid in mods:
        try:
            items = notebook_items(cfg, mid)
        except ValueError:
            continue
        if q:
            # Searching matches the words he wrote, the words he marked, and
            # WHERE they are: "bipolar" should find the topic as well as the
            # sentence, per his instruction to search by topic and week too.
            items = [i for i in items if any(
                q in str(i.get(f) or "").lower()
                for f in ("text", "note", "title", "topic", "weekTitle", "doc", "week"))]
        for i in items:
            counts[i["kind"]] = counts.get(i["kind"], 0) + 1
        mcfg = module_cfg(cfg, mid)
        groups.append({"id": mid, "name": mcfg.get("module_name") or mid,
                       "url": module_url(cfg, mid), "items": items})
    return {"ok": True, "modules": groups, "counts": counts,
            "total": sum(counts.values()), "q": query or ""}



# The notebook page. Same reasoning as the home page: one string, no assets, so
# it renders when everything else is misconfigured. It is a PAGE rather than a
# panel in the reader because the question it answers ("everything I have marked
# on this course") is not about the lesson you happen to have open.
NOTEBOOK_PAGE = """<!-- study-notebook -->
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
%(icons)s<title>%(title)s</title>
<style>
  :root {
    --paper:#F1F4F3; --surface:#FBFCFC; --ink:#1A2830; --ink-soft:#3E535C; --muted:#5F7178;
    --rule:#D8E0DE; --accent:#1C6D61; --accent-wash:#DDEBE7; --broken:#A25E14;
    --display:"Iowan Old Style","Palatino Linotype",Palatino,"Book Antiqua",Georgia,serif;
    --text:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
    --mono:ui-monospace,SFMono-Regular,Menlo,monospace;
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      --paper:#12191C; --surface:#182126; --ink:#E7EEEC; --ink-soft:#B7C6C4; --muted:#8AA0A0;
      --rule:#26343A; --accent:#5FBFAE; --accent-wash:#173029; --broken:#DBA463;
    }
  }
  :root[data-theme="dark"] {
    --paper:#12191C; --surface:#182126; --ink:#E7EEEC; --ink-soft:#B7C6C4; --muted:#8AA0A0;
    --rule:#26343A; --accent:#5FBFAE; --accent-wash:#173029; --broken:#DBA463;
  }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--paper); color:var(--ink); font-family:var(--text);
         line-height:1.5; -webkit-font-smoothing:antialiased; }
  .wrap { max-width: 860px; margin: 0 auto; padding: 40px 20px 72px; }
  h1 { font-family:var(--display); font-size:1.8rem; font-weight:600; margin:0 0 4px; }
  .sub { color:var(--muted); margin:0 0 22px; font-size:.9rem; }
  .sub a { color:var(--accent); }
  form { display:flex; gap:8px; margin:0 0 26px; }
  input[type=search] { flex:1; font:15px var(--text); color:var(--ink); background:var(--surface);
    border:1px solid var(--rule); border-radius:8px; padding:10px 13px; }
  input[type=search]:focus { outline:2px solid var(--accent); outline-offset:1px; }
  button { font:600 14px var(--text); color:var(--ink); background:var(--accent-wash);
    border:1px solid var(--rule); border-radius:8px; padding:10px 16px; cursor:pointer; }
  .tabs { display:flex; gap:6px; flex-wrap:wrap; margin:0 0 22px; }
  .tabs a { font:600 12.5px var(--text); text-decoration:none; color:var(--ink-soft);
    background:var(--surface); border:1px solid var(--rule); border-radius:999px; padding:6px 13px; }
  .tabs a.on { background:var(--accent-wash); color:var(--ink); border-color:var(--accent); }
  h2 { font-family:var(--display); font-size:1.05rem; font-weight:600; margin:26px 0 10px;
       padding-bottom:6px; border-bottom:1px solid var(--rule); }
  h2 a { color:inherit; text-decoration:none; }
  h2 a:hover { color:var(--accent); }
  .item { background:var(--surface); border:1px solid var(--rule); border-left-width:3px;
    border-radius:8px; padding:11px 14px; margin:0 0 8px; }
  .item .k { font:600 10.5px var(--mono); letter-spacing:.08em; text-transform:uppercase;
    color:var(--muted); }
  .item p { margin:4px 0 0; font-size:14.5px; }
  .item .n { margin:6px 0 0; font-size:13px; color:var(--ink-soft); border-left:2px solid var(--rule);
    padding-left:9px; }
  .item a.go { font-size:12.5px; color:var(--accent); text-decoration:none; }
  .item.orphan { border-left-color:var(--broken); }
  .empty { color:var(--muted); padding:28px 0; }
  footer { margin-top:44px; padding-top:14px; border-top:1px solid var(--rule);
    color:var(--muted); font-size:.78rem; }
</style>
<div class="wrap">
  %(navbar)s
  <h1>%(title)s</h1>
  <p class="sub">%(sub)s</p>
  <form method="get" action="">
    <input type="search" name="q" value="%(q)s"
           placeholder="Search what you marked, and the lessons themselves"
           aria-label="Search your notebook and the lessons">
    <button type="submit">Search</button>
  </form>
  <div class="tabs">%(tabs)s</div>
  %(body)s
  <footer>%(foot)s</footer>
</div>
"""


# --------------------------------------------------------------------------
# The token, asked for wherever it is wanted
# --------------------------------------------------------------------------
#
# 🔴 Reported 2026-08-21: adding a course failed with "this server wants its
# token. Open a lesson once on this device and paste it there, then come back."
# On a fresh kit there IS no lesson to open, so the instruction is impossible;
# and on his own machine, after a token rotation, it sent him away from the page
# he was on to hunt for another one. The token prompt lived only in the reader
# layer, and the layer is composed onto lessons alone.
#
# So the prompt moves into a block any page can carry. Same words, same key,
# same one-time-per-device promise as the reader's, because two prompts for one
# credential that describe it differently is how a person concludes there are
# two credentials. The difference from the reader's: this one RETRIES the thing
# you were doing, rather than reloading, so the course you were adding is still
# in the boxes when it works.
TOKEN_BAR = """
<style>
  .sv-tokenbar { position:fixed; left:0; right:0; bottom:0; z-index:99;
                 background:var(--surface); border-top:1px solid var(--rule);
                 padding:16px 18px calc(16px + env(safe-area-inset-bottom));
                 box-shadow:0 -6px 24px rgba(0,0,0,.12); }
  .sv-tokenbar[hidden] { display:none; }
  .sv-tokenbar .in { max-width:760px; margin:0 auto; }
  .sv-tokenbar p { margin:0 0 10px; color:var(--ink-soft); font-size:.9rem; line-height:1.55; }
  .sv-tokenbar strong { color:var(--ink); }
  .sv-tokenbar .cmd { display:block; margin:8px 0; padding:8px 10px; overflow-x:auto;
                      white-space:pre; font-size:.75rem; }
  .sv-tokenbar .row { display:flex; gap:8px; flex-wrap:wrap; }
  .sv-tokenbar input { flex:1 1 220px; min-width:0; font:inherit; padding:11px 12px;
                       border:1px solid var(--rule); border-radius:9px;
                       background:var(--paper); color:var(--ink); }
  .sv-tokenbar button { font:inherit; padding:11px 18px; border:0; border-radius:9px;
                        background:var(--accent); color:var(--paper); cursor:pointer; }
</style>
<!-- 🔴 The bar is MARKUP, hidden, not a string built in JavaScript. The command
     below carries both kinds of quote, and writing it as a JS string literal
     inside a Python string means two layers of escaping over one line: the first
     version of this shipped a broken quote that took the whole page's JavaScript
     down with it, silently, because a page whose script does not parse still
     renders. Written as HTML there is nothing to escape and nothing to get
     wrong. -->
<div class="sv-tokenbar" id="sv-tokenbar" hidden>
  <div class="in">
    <p><strong>This device needs the study server\u2019s token.</strong>
       You do this once here, and never again on this device. To print it, run
       this in a terminal on the machine running the server:</p>
    <code class="cmd">python3 -c "import json,pathlib;print(json.loads(pathlib.Path.home().joinpath('.kcl-study/config.json').read_text())['token'])"</code>
    <div class="row">
      <input type="password" autocomplete="off" spellcheck="false"
             placeholder="paste the token" aria-label="The study server token">
      <button type="button">Save</button>
    </div>
  </div>
</div>
<script>
(function () {
  var KEY = 'kcl-study-token';
  var bar = document.getElementById('sv-tokenbar');
  var field = bar.querySelector('input');
  var pending = null;

  function get() {
    try { return window.localStorage.getItem(KEY) || ''; } catch (e) { return ''; }
  }
  function headers(h) {
    h = h || {};
    var tk = get();
    if (tk) { h['Authorization'] = 'Bearer ' + tk; }
    return h;
  }

  /* `retry` is whatever you were doing when the 401 came back, so the course you
     were adding is still in the boxes when it works. Deliberately not
     window.prompt: a modal blocks every event in the page until it is answered. */
  function ask(retry) {
    pending = retry || null;
    bar.hidden = false;
    field.focus();
  }
  function save() {
    var v = field.value.trim();
    if (!v) { field.focus(); return; }
    try { window.localStorage.setItem(KEY, v); } catch (e) {}
    field.value = '';
    bar.hidden = true;
    var again = pending; pending = null;
    if (typeof again === 'function') { again(); } else { window.location.reload(); }
  }
  bar.querySelector('button').addEventListener('click', save);
  field.addEventListener('keydown', function (e) { if (e.key === 'Enter') save(); });

  window.STUDYTOKEN = { get: get, headers: headers, ask: ask };
}());
</script>
"""


# 🔴 The token lives in localStorage under the key the reader already uses, and
# this page is the same origin, so a browser that has read a lesson is already
# authorised here. On a loopback install there is no token at all and the header
# is simply absent. Asking for it is the reader's own flow, kept in one shape.
# EH, 2026-08-30: "we have an 'Add a Course' card on the main homepage of Study
# Hub. I would get rid of that there. It's a little annoying, and just create an
# 'Add a Course' button that then feeds into the rest of the wizard, including
# collecting the course code and the name."
#
# So the home page keeps a BUTTON and the three questions move to their own page,
# which is the wizard's step 0. What was here was a permanent card carrying a
# heading, two paragraphs, two inputs and a 60-line hint, sitting under the
# course list on every visit whether or not anybody was adding anything.
ADD_COURSE_CTA = """
  <div class="addcta">
    <a class="addbtn" href="/addcourse">Add a course</a>
    <p>It asks three questions, then walks you through getting the lessons in.</p>
  </div>
"""


# --------------------------------------------------------------------------
# Step 0 of the wizard: the three names, before the folder exists
# --------------------------------------------------------------------------
#
# EH, 2026-08-30, in the same message: "Currently, the name is optional. It
# should not be. We should ask for the name first: the full name, the short
# name, and a course code, and should explain what each one of them is."
#
# 🔴 It cannot live inside WIZARD_PAGE, and the reason is the whole shape of
# this page: the wizard is addressed as `/m/<CODE>/start`, so it needs the code
# to exist before it can be reached. This page is what runs BEFORE there is a
# course, and it hands over to the wizard the moment there is one.
#
# The order is EH's and it is deliberate: the two easy questions first, the
# irreversible one last. 🔴 That order carries its own risk, recorded in the
# queue entry - asking for the code last must not turn it into an afterthought -
# so the code is the only field on the page that says it cannot be changed, and
# the short name is the only one that says it can.
ADD_COURSE_PAGE = """<!-- study-addcourse -->
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
%(icons)s<title>Add a course</title>
<style>
  :root {
    --paper:#F1F4F3; --surface:#FBFCFC; --ink:#1A2830; --ink-soft:#3E535C; --muted:#5F7178;
    --rule:#D8E0DE; --accent:#1C6D61; --accent-wash:#DDEBE7; --broken:#A25E14;
    --display:"Iowan Old Style","Palatino Linotype",Palatino,"Book Antiqua",Georgia,serif;
    --text:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      --paper:#12191C; --surface:#182126; --ink:#E7EEEC; --ink-soft:#B7C6C4; --muted:#8AA0A0;
      --rule:#26343A; --accent:#5FBFAE; --accent-wash:#173029; --broken:#D69A55;
    }
  }
  :root[data-theme="dark"] {
    --paper:#12191C; --surface:#182126; --ink:#E7EEEC; --ink-soft:#B7C6C4; --muted:#8AA0A0;
    --rule:#26343A; --accent:#5FBFAE; --accent-wash:#173029; --broken:#D69A55;
  }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--paper); color:var(--ink); font-family:var(--text);
         line-height:1.6; -webkit-text-size-adjust:100%%; }
  .wrap { max-width:640px; margin:0 auto; padding:44px 20px 80px; }
  a { color:var(--accent); }
  h1 { font-family:var(--display); font-size:1.7rem; font-weight:600; margin:0 0 6px; }
  .sub { color:var(--muted); margin:0 0 26px; font-size:.95rem; max-width:56ch; }
  .step { background:var(--surface); border:1px solid var(--rule); border-radius:12px;
          padding:24px; }
  .q + .q { margin-top:26px; padding-top:22px; border-top:1px solid var(--rule); }
  .q label { display:block; font-weight:600; font-size:1.02rem; margin:0 0 4px; }
  .q .why { margin:0 0 12px; color:var(--ink-soft); font-size:.88rem; max-width:56ch; }
  .q .why b { color:var(--ink); font-weight:600; }
  .q input {
    width:100%%; font:inherit; font-size:.95rem; padding:9px 12px; color:var(--ink);
    background:var(--paper); border:1px solid var(--rule); border-radius:9px;
  }
  .q input:focus-visible { outline:2px solid var(--accent); outline-offset:1px; }
  .q .eg { margin:6px 0 0; color:var(--muted); font-size:.8rem; }
  /* The one field that is allowed to be empty says so in its own label, so a
     reader is not left wondering whether they have missed something. */
  .q .opt { font-weight:400; font-size:.78rem; color:var(--muted);
            text-transform:uppercase; letter-spacing:.06em; margin-left:6px; }
  .warn { margin:12px 0 0; padding:10px 12px; border-radius:9px; font-size:.84rem;
          color:var(--ink-soft); background:var(--accent-wash);
          border-left:3px solid var(--broken); max-width:56ch; }
  .warn b { color:var(--ink); font-weight:600; }
  .go { margin-top:26px; padding-top:22px; border-top:1px solid var(--rule);
        display:flex; gap:12px; align-items:center; flex-wrap:wrap; }
  .go button {
    font:inherit; font-size:.95rem; font-weight:600; padding:10px 20px; cursor:pointer;
    color:var(--paper); background:var(--accent); border:1px solid var(--accent); border-radius:9px;
  }
  .go button:disabled { opacity:.55; cursor:default; }
  .go .plain { color:var(--accent); background:none; }
  .says { margin:0; font-size:.88rem; color:var(--ink-soft); min-height:1.4em; }
  .says.bad { color:var(--broken); }
</style>
<div class="wrap">
  %(navbar)s
  <h1>Add a course</h1>
  <p class="sub">Three questions, then the rest of the setup. Two of them you can
     change whenever you like; the page says which one you cannot.</p>

  <div class="step">
    <div class="q">
      <label for="cfull">Its full name</label>
      <p class="why">The whole title, the way your institution writes it. It heads
         the course page and it is what you see when there is room for it.</p>
      <input id="cfull" type="text" autocomplete="off"
             placeholder="The whole title"
             aria-label="The course&#39;s full name">
      <p class="eg">For example: Psychology and Neuroscience of Affective Disorders</p>
    </div>

    <div class="q">
      <label for="cshort">Its short name</label>
      <p class="why">What you actually call it. It stands in for the full name
         wherever there is not room, and it names the notes this course publishes
         to your vault. <b>You can change this whenever you like</b>, so a good
         guess now costs nothing.</p>
      <input id="cshort" type="text" autocomplete="off"
             placeholder="What you call it"
             aria-label="The course&#39;s short name">
      <p class="eg">For example: Affective Disorders</p>
    </div>

    <div class="q">
      <label for="ccode">Its course code</label>
      <p class="why"><b>The code is not a label.</b> It names the folder your
         lessons live in, it is the web address of the course, and it is what
         keeps this course&#8217;s highlights separate from every other
         course&#8217;s.</p>
      <input id="ccode" type="text" autocomplete="off" spellcheck="false"
             autocapitalize="off" placeholder="The code your institution uses"
             aria-label="The course code">
      <p class="eg">For example: PSY101</p>
      <p class="warn"><b>This is the one you cannot change later.</b> Pick the one
         your institution uses, and pick it once: changing it afterwards means
         moving the folder and losing what you have marked.</p>
    </div>

    <div class="q">
      <label for="cvault">Its folder in your vault <span class="opt">optional</span></label>
      <p class="why">Study Hub can publish the notes and highlights you make here
         into your Obsidian vault. This names the project they are filed under.
         It fills itself in from the short name, so most of the time you can
         leave it alone.</p>
      <input id="cvault" type="text" autocomplete="off"
             placeholder="KCL - Affective Disorders"
             aria-label="The vault project this course&#39;s notes are filed under">
      <p class="eg">For example: KCL &#8211; Affective Disorders</p>
      <p class="why"><b>Empty is a real answer.</b> Clear the box if this course
         does not go to a vault, and nothing will ask you about it again.</p>
    </div>

    <div class="go">
      <button type="button" id="makeit">Create the course</button>
      <a class="plain" href="/home">Not now</a>
    </div>
    <p class="says" id="says" role="status"></p>
  </div>
</div>
%(tokenbar)s
<script>
(function () {
  var full = document.getElementById('cfull');
  var short_ = document.getElementById('cshort');
  var code = document.getElementById('ccode');
  var vault = document.getElementById('cvault');
  var btn = document.getElementById('makeit');
  var says = document.getElementById('says');

  function tell(msg, bad) {
    says.textContent = msg || '';
    says.classList.toggle('bad', !!bad);
  }

  /* All three are required, and the message NAMES the missing one rather than
     saying "fill in the form": three fields in one card is exactly where a
     generic complaint makes somebody hunt. */
  /* The vault name follows the short name until the reader touches it, and
     then stops for good. EH standardised the shape on 2026-09-08: "I think we
     should standardize the name to KCL." So the common case is zero keystrokes
     and the box still shows what will be saved, rather than a default applied
     invisibly on the server where nobody can see or refuse it. */
  var vaultTouched = false;
  vault.addEventListener('input', function () { vaultTouched = true; });
  function followShort() {
    if (vaultTouched) { return; }
    var v = short_.value.trim();
    vault.value = v ? ('KCL - ' + v) : '';
  }
  short_.addEventListener('input', followShort);

  function missing() {
    if (!full.value.trim()) {
      return [full, 'Give the course its full name first.'];
    }
    if (!short_.value.trim()) {
      return [short_, 'Give it a short name too. It is what you will see most of the time.'];
    }
    if (!code.value.trim()) {
      return [code, 'Give it a course code. This is the one you cannot change later.'];
    }
    return null;
  }

  function make() {
    var gap = missing();
    if (gap) { tell(gap[1], true); gap[0].focus(); return; }
    btn.disabled = true;
    tell('Creating\\u2026');
    fetch('/api/modules', {
      method: 'POST',
      headers: window.STUDYTOKEN.headers({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ id: code.value.trim(),
                             name: full.value.trim(),
                             class_name: short_.value.trim(),
                             /* Always sent, even empty: an empty box is the
                                reader saying "no vault", which is an answer,
                                and it must not arrive looking like silence. */
                             project_link: vault.value.trim() })
    }).then(function (r) {
      return r.json().then(function (j) { return { status: r.status, body: j }; });
    }).then(function (r) {
      if (r.status === 401) {
        /* Ask here and retry the same course. What was typed stays in the
           boxes, which is the whole point of retrying rather than reloading. */
        btn.disabled = false;
        tell('');
        window.STUDYTOKEN.ask(make);
        return;
      }
      if (!r.body || !r.body.ok) {
        tell((r.body && r.body.error) || 'That did not work.', true);
        btn.disabled = false;
        return;
      }
      tell('Made. Opening its setup\\u2026');
      /* Straight into the rest of the wizard, which is the moment its questions
         are cheap. It is a door, not a gate: its skip link is the course page. */
      window.location.href = (r.body.url || '/') + 'start';
    }).catch(function () {
      tell('Could not reach the server.', true);
      btn.disabled = false;
    });
  }

  btn.addEventListener('click', make);
  [full, short_, code].forEach(function (el) {
    el.addEventListener('keydown', function (ev) {
      if (ev.key === 'Enter') { ev.preventDefault(); make(); }
    });
  });
  full.focus();
}());
</script>
"""


# --------------------------------------------------------------------------
# The wizard: three questions at the one moment they are cheap
# --------------------------------------------------------------------------
#
# Plan 04 \u00a72, accepted by EH on 2026-08-22 ("go ahead and do your idea
# for the wizard thing"). A wizard is the wrong shape for settings and the right
# shape for arrival: nobody wants five steps to change the model, everybody
# wants to be told what to do next when they are looking at an empty course.
#
# So: NAME (only if it has none), WHAT DO YOU HAVE (the routes that already
# exist, asked at the moment the question is in the person's head), and then the
# right prompt, already filled in with this course's code, with a Copy button.
# The wizard ends by putting a sentence on the clipboard, which is the whole
# design: it is a different door onto the help page's material, not new
# machinery.
#
# 🔴 It is never a gate. Every step has a way straight to the course,
# and the course works identically for somebody who never sees this page. The
# one deliberate deviation from plan 04: the materials folder is not stored on
# the course, because nothing reads it; it goes into the prompt instead, where
# the session it is meant for actually receives it.
WIZARD_PAGE = """<!-- study-wizard -->
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
%(icons)s<title>Set up %(course)s</title>
<style>
  :root {
    --paper:#F1F4F3; --surface:#FBFCFC; --ink:#1A2830; --ink-soft:#3E535C; --muted:#5F7178;
    --rule:#D8E0DE; --accent:#1C6D61; --accent-wash:#DDEBE7; --broken:#A25E14;
    --display:"Iowan Old Style","Palatino Linotype",Palatino,"Book Antiqua",Georgia,serif;
    --text:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      --paper:#12191C; --surface:#182126; --ink:#E7EEEC; --ink-soft:#B7C6C4; --muted:#8AA0A0;
      --rule:#26343A; --accent:#5FBFAE; --accent-wash:#173029; --broken:#D69A55;
    }
  }
  :root[data-theme="dark"] {
    --paper:#12191C; --surface:#182126; --ink:#E7EEEC; --ink-soft:#B7C6C4; --muted:#8AA0A0;
    --rule:#26343A; --accent:#5FBFAE; --accent-wash:#173029; --broken:#D69A55;
  }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--paper); color:var(--ink); font-family:var(--text);
         line-height:1.6; -webkit-text-size-adjust:100%%; }
  .wrap { max-width:640px; margin:0 auto; padding:44px 20px 80px; }
  a { color:var(--accent); }
  h1 { font-family:var(--display); font-size:1.7rem; font-weight:600; margin:0 0 6px; }
  .sub { color:var(--muted); margin:0 0 26px; font-size:.95rem; }
  .step { background:var(--surface); border:1px solid var(--rule); border-radius:12px;
          padding:24px; }
  .step[hidden] { display:none; }
  h2 { font-family:var(--display); font-size:1.2rem; font-weight:600; margin:0 0 8px; }
  p { margin:8px 0 0; color:var(--ink-soft); font-size:.93rem; }
  p b { color:var(--ink); }
  .row { display:flex; gap:10px; flex-wrap:wrap; margin:16px 0 0; align-items:center; }
  input[type=text] { flex:1 1 220px; min-width:0; font:inherit; padding:11px 12px;
                     border:1px solid var(--rule); border-radius:9px;
                     background:var(--paper); color:var(--ink); }
  input[type=text]:focus-visible { outline:2px solid var(--accent); outline-offset:1px; }
  button { font:inherit; padding:11px 18px; border:0; border-radius:9px;
           background:var(--accent); color:var(--paper); cursor:pointer; }
  button.plain { background:transparent; color:var(--ink-soft);
                 border:1px solid var(--rule); }
  .choices { display:flex; flex-direction:column; gap:10px; margin:16px 0 0; }
  .choices button { text-align:left; background:var(--paper); color:var(--ink);
                    border:1px solid var(--rule); padding:14px 16px; border-radius:10px; }
  .choices button:hover { border-color:var(--accent); background:var(--accent-wash); }
  .choices button b { display:block; }
  .choices button span { display:block; color:var(--muted); font-size:.85rem;
                         margin-top:2px; }
  .prompt { margin:14px 0 0; padding:14px; border:1px solid var(--rule);
            border-radius:9px; background:var(--paper); font-size:.83rem;
            white-space:pre-wrap;
            font-family:ui-monospace,SFMono-Regular,Menlo,monospace; }
  .says { margin:12px 0 0; font-size:.85rem; color:var(--ink-soft); min-height:1.2em; }
  .says.bad { color:var(--broken); }
  .skip { display:block; margin-top:22px; font-size:.88rem; }
  .note { border-left:3px solid var(--broken); padding-left:14px; margin:16px 0 0;
          color:var(--ink-soft); font-size:.88rem; }
  .piece { border:1px solid var(--rule); border-radius:10px; padding:14px 16px;
           margin:14px 0 0; background:var(--paper); }
  .ptop { display:flex; gap:10px; align-items:baseline; font-size:.95rem;
          color:var(--ink); cursor:pointer; }
  .ptop .already { color:var(--muted); font-size:.82rem; margin-left:auto; }
  /* 🔴 What a job COSTS, said where the tick happens. The manager's obligation
     on EH's captions ask: a checkbox that looks like its neighbours and then
     takes forty minutes is a trap, and the reader who does not know assumes it
     has hung. Styled as a quiet chip rather than a warning, because it is
     information and not an error: a red badge on two of five jobs would read as
     something being wrong. `nowrap` so "tens of minutes" cannot break across
     two lines and lose its shape at a narrow width. */
  .ptop .cost { font-weight:600; font-size:.72rem; color:var(--muted);
                border:1px solid var(--rule); border-radius:999px;
                padding:1px 7px; margin-left:6px; white-space:nowrap; }
  .popts { margin:8px 0 0 26px; display:flex; flex-direction:column; gap:6px; }
  /* An author display beats the UA's [hidden] rule, so el.hidden alone never
     hid these. Every option block was visible whatever its checkbox said. */
  .popts[hidden] { display:none; }
  .popts label { display:flex; gap:8px; align-items:baseline; font-size:.88rem;
                 color:var(--ink-soft); cursor:pointer; }
  .popts .hint { color:var(--muted); font-size:.84rem; margin:0; }
  .popts .sz { color:var(--muted); }
  .popts input[type=text] { margin-left:0; font-size:.86rem; padding:9px 11px;
                            flex:0 0 auto; width:100%%; }
  input[type=checkbox], input[type=radio] { accent-color:var(--accent); }
  body.dropping .step { outline:2px dashed var(--accent); outline-offset:4px; }
</style>
<div class="wrap">
  %(navbar)s
  <h1>Set up %(course)s</h1>
  <p class="sub">Tick what this course should include, say what you already
     have, and one prompt does the rest.</p>

  <div class="step" id="st-name" hidden>
    <h2>What is this course called?</h2>
    <p>The code, <b>%(code)s</b>, stays: it is the folder and the web address.
       The name is only what you read on the page, and you can change it any
       time in Settings.</p>
    <div class="row">
      <input type="text" id="cname" placeholder="e.g. Affective Disorders"
             autocomplete="off" aria-label="Course name">
      <button type="button" id="namego">Save and continue</button>
      <button type="button" class="plain" id="nameskip">Skip</button>
    </div>
    <p class="says" id="nsays" role="status"></p>
  </div>

  <div class="step" id="st-build" hidden>
    <h2>What should this course include?</h2>
    <p class="pieces" id="pieces" role="status"></p>

    <div class="piece">
      <label class="ptop"><input type="checkbox" id="w-lessons">
        <span><b>Lessons</b>, written from the slides and transcripts</span>
        <span class="already" id="a-lessons"></span></label>
      <div class="popts" id="o-lessons">
        <label><input type="radio" name="src-lessons" value="local" checked>
          I have the folder downloaded</label>
        <input type="text" id="f-lessons" placeholder="Paste the slides folder path (optional)"
               autocomplete="off" autocapitalize="off" spellcheck="false"
               aria-label="Where the slides and transcripts are">
        <label><input type="radio" name="src-lessons" value="site">
          They are on my course site (KEATS, Moodle, Canvas); download them</label>
        <input type="text" id="f-site"
               placeholder="Paste your course page address (optional)"
               autocomplete="off" autocapitalize="off" spellcheck="false"
               aria-label="The address of your course page">
      </div>
    </div>

    <div class="piece">
      <label class="ptop"><input type="checkbox" id="w-videos">
        <span><b>Lecture videos</b>, playing beside each lesson</span>
        <span class="already" id="a-videos"></span></label>
      <div class="popts" id="o-videos">
        <p class="hint">Their addresses are collected from your course site.
           Already have a links file? <b>Drop it
           anywhere on this page</b> or
           <a href="#" id="pickvideos">choose the file</a>; once it is in,
           this ticks itself off.</p>
        <label><input type="checkbox" id="w-dl">
          Also download copies to this machine</label>
        <div class="popts" id="o-dl">
          <label><input type="checkbox" id="w-dl-videos" checked>
            <b>Video files.</b> Recordings play inside Study Hub without being
            downloaded; a copy also works offline and keeps playing if the
            course site ever closes. <span class="sz" id="sz-videos"></span></label>
          <label><input type="checkbox" id="w-dl-packs" checked>
            <b>Presentations with audio.</b> Narrated slide shows cannot play
            inside Study Hub unless they are downloaded; without this they
            only open on the course site. <span class="sz" id="sz-packs"></span></label>
        </div>
      </div>
    </div>

    <div class="piece">
      <label class="ptop"><input type="checkbox" id="w-captions">
        <span><b>Captions</b> on the lectures, in the lecturer&#8217;s own words
          <b class="cost">tens of minutes</b></span>
        <span class="already" id="a-captions"></span></label>
      <div class="popts" id="o-captions">
        <p class="hint">Taken from the transcript and timed against the
           recording, so they are not a machine&#8217;s guess at what was said.
           Narrated slide packages and plain recordings alike, skipping any
           lecture that already has them. The count beside the tick is lectures
           carrying <b>some</b> captions; the Captions section in Settings says
           which are finished and which are only part-way.
           <b>This one is slow</b>: it listens to every lecture in the course,
           one at a time, so expect tens of minutes rather than seconds. You can
           leave it running.</p>
      </div>
    </div>

    <div class="piece" id="p-pack"%(pack_hidden)s>
      <label class="ptop"><input type="checkbox" id="w-pack">
        <span><b>Brain-region pictures</b>, one download shared by every course
          <b class="cost">once</b></span></label>
      <div class="popts" id="o-pack">
        <p class="hint">Labelled plates of the brain regions, so a region named
           in a lesson opens on a picture chosen for that purpose rather than
           whatever Wikipedia leads with, and its definition with it. Downloaded
           once for every course you have, not once per course, and kept when
           you upgrade. The plates have not had a formal review by a domain
           expert: study from them, do not cite them.
           <span class="sz" id="sz-pack"></span></p>
      </div>
    </div>

    <div class="piece">
      <label class="ptop"><input type="checkbox" id="w-consol">
        <span><b>Consolidated PDFs</b>, one per week and one for the whole
          course, for slides and for transcripts
          <b class="cost">a few minutes</b></span>
        <span class="already" id="a-consol"></span></label>
      <div class="popts" id="o-consol">
        <p class="hint">Built from the downloaded slides and transcripts after
           they are cleaned up, so a week reads and prints as one document.
           Needs the course material downloaded (the Lessons job above does
           that, or say where your folder is in the prompt).
           Every page is re-read to make it searchable, which takes a few
           minutes for a course rather than seconds.</p>
      </div>
    </div>

    <div class="piece">
      <label class="ptop"><input type="checkbox" id="w-readings">
        <span><b>Core readings</b>, summarised and skimmable</span>
        <span class="already" id="a-readings"></span></label>
      <div class="popts" id="o-readings">
        <label><input type="radio" name="src-readings" value="local" checked>
          I have the PDFs downloaded</label>
        <input type="text" id="f-readings" placeholder="Paste the readings folder path (optional)"
               autocomplete="off" autocapitalize="off" spellcheck="false"
               aria-label="Where the reading PDFs are">
        <label><input type="radio" name="src-readings" value="site">
          Get them from my course site</label>
      </div>
    </div>

    <h2 style="margin-top:22px" id="ptitle">Paste this into Claude</h2>
    <div class="prompt" id="goprompt"></div>
    <div class="row">
      <button type="button" id="gocopy">Copy the prompt</button>
      <a id="goread" href="/help" target="_blank" rel="noopener">Read the full instructions</a>
    </div>
    <div class="note"><b>First time?</b> Claude needs installing and pointing at
       Study Hub once. The <a href="help">step by step guide</a> walks through
       it, path and all.</div>
    <p><b>Somebody sent you a file?</b> A lesson, a whole shared course as a
       .zip, a links file or a readings file needs no Claude at all:
       <b>drop it anywhere on this page</b>, or
       <a href="#" id="pickfile">choose the file</a>.</p>
    <p class="says" id="isays" role="status"></p>
    <input type="file" id="filein" multiple hidden>
  </div>

  <div class="step" style="margin-top:18px" id="st-mats">
    <h2>Where the slides and transcripts come from</h2>
    <p>Two ways, and <b>you pick one</b>. This is not a preference the system
       falls back through: whichever you choose is the only place it looks, so
       you can always tell what you are reading.</p>
    <div class="popts" id="o-mats">
      <label><input type="radio" name="matsrc" value="keats" checked>
        <b>From the course site.</b> The Materials pane links out to wherever
        the course keeps them. Nothing is stored on this machine.</label>
      <label><input type="radio" name="matsrc" value="local">
        <b>From a folder on this machine.</b> The pane shows the files
        themselves, and works with no internet and after the course site
        closes.</label>
      <input type="text" id="matdir"
             placeholder="Where that folder is (leave empty for the default)"
             autocomplete="off" autocapitalize="off" spellcheck="false"
             aria-label="Where the downloaded materials are">
      <p class="hint" id="mathint"></p>
    </div>
    <div class="addrow">
      <button type="button" id="matsave">Save this choice</button>
    </div>
    <p class="says" id="matsays" role="status"></p>
    <p class="hint"><b>Nothing downloaded yet?</b> Ask your Claude session to
       "download the course materials from KEATS" and the download-keats skill
       fills that folder, naming each file after the part it belongs to. This
       page then finds them without being told anything else.</p>
  </div>

  <div class="step" style="margin-top:18px" id="st-share">
    <h2>Share this course with a coursemate</h2>
    <p>One button makes one file with everything that is yours to share: every
       lesson, the glossary, the readings summaries and the mistakes page.
       <b>Your highlights and notes never travel</b>, and neither do the
       downloaded slides, transcripts or videos, which belong to your
       institution.</p>
    <div class="row">
      <button type="button" id="sharego">Make the share file</button>
      <label><input type="checkbox" id="sharecaps"> Include the caption cues
        (the lecturer's words with timings; nothing of yours)</label>
    </div>
    <p class="says" id="ssays" role="status"></p>
    <p><b>Then send the .zip however you like.</b> The person you send it to
       opens their own Study Hub, adds the course (or opens it), and <b>drags
       the .zip onto the course page</b>. Everything lands in one go. The
       lecture links inside work only for someone enrolled on the module,
       which is exactly right. <b>Your own Google Drive addresses for the
       slides and transcripts are not included</b>, so the recipient points
       their own copy at their own materials instead of reading yours.</p>
    <p><b>They do not have Study Hub yet?</b> Send them the kit first; its
       README starts at zero. Ask your Claude session to "build the kit zip"
       and it will tell you where it is.</p>
  </div>

  <a class="skip" href="%(back)s" id="skip">Skip this: go straight to the course &rarr;</a>
</div>
%(tokenbar)s
<script>
(function () {
  var FRAGS = %(frags)s;
  var STATUS = %(status)s;
  var ASKNAME = %(askname)s;

  var steps = { name: document.getElementById('st-name'),
                build: document.getElementById('st-build') };
  function show(which) {
    Object.keys(steps).forEach(function (k) { steps[k].hidden = (k !== which); });
  }

  /* ---- the name, asked only when there is none ---- */
  var nsays = document.getElementById('nsays');
  function tellN(msg, bad) {
    nsays.textContent = msg || '';
    nsays.classList.toggle('bad', !!bad);
  }
  function saveName() {
    var v = document.getElementById('cname').value.trim();
    if (!v) { show('build'); return; }
    tellN('Saving\u2026');
    fetch('/api/module', {
      method: 'POST',
      headers: window.STUDYTOKEN.headers({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ id: %(codejs)s, name: v })
    }).then(function (r) { return r.json().then(function (j) { return {s:r.status, b:j}; }); })
      .then(function (r) {
        if (r.s === 401) { tellN(''); window.STUDYTOKEN.ask(saveName); return; }
        if (!r.b || !r.b.ok) { tellN((r.b && r.b.error) || 'That did not save.', true); return; }
        show('build');
      }).catch(function () { tellN('Could not reach the server.', true); });
  }
  document.getElementById('namego').addEventListener('click', saveName);
  document.getElementById('cname').addEventListener('keydown', function (e) {
    if (e.key === 'Enter') { e.preventDefault(); saveName(); }
  });
  document.getElementById('nameskip').addEventListener('click', function () { show('build'); });

  /* ---- the checklist: what to include, and whether it is downloaded ---- */
  var box = { lessons: document.getElementById('w-lessons'),
              videos: document.getElementById('w-videos'),
              captions: document.getElementById('w-captions'),
              pack: document.getElementById('w-pack'),
              consol: document.getElementById('w-consol'),
              readings: document.getElementById('w-readings') };
  var opts = { lessons: document.getElementById('o-lessons'),
               videos: document.getElementById('o-videos'),
               captions: document.getElementById('o-captions'),
               pack: document.getElementById('o-pack'),
               consol: document.getElementById('o-consol'),
               readings: document.getElementById('o-readings') };
  var already = { lessons: document.getElementById('a-lessons'),
                  videos: document.getElementById('a-videos'),
                  captions: document.getElementById('a-captions'),
                  consol: document.getElementById('a-consol'),
                  readings: document.getElementById('a-readings') };
  var folders = { lessons: document.getElementById('f-lessons'),
                  readings: document.getElementById('f-readings') };
  var TOKENS = { lessons: '<paste the slides folder here>',
                 readings: '<paste the readings folder here>' };
  /* 🔴 EH, 2026-09-08, while adding a real course: "the prompt doesn't actually
     have the course address ... We should have that as a field in our wizard".
     The wizard knew how to ask questions and this one question was left in its
     own output, so a newcomer is handed a prompt containing an instruction to
     themselves. It is the same shape as `folders` above and deliberately so. */
  var sites = { lessons: document.getElementById('f-site') };
  var SITE_TOKENS = { lessons: '<paste the course address here>' };

  /* Defaults are the truth: a piece the course lacks starts ticked, a piece
     it already has starts unticked and says so, and either can be changed. */
  box.lessons.checked = !STATUS.lessons;
  box.videos.checked = !STATUS.videos;
  box.readings.checked = !STATUS.readings;
  /* 🔴 CHANGED 2026-09-08, and it supersedes "never pre-ticked, whatever the
     course has" (EH, 2026-08-23). Ruled by the manager on EH's own statement of
     what onboarding is for: it serves "the downloading, organization and
     processing of course materials for the student as well". A student who
     onboards a course should end up with the readable, printable documents
     WITHOUT having to know such a thing exists to ask for. So it is turned OFF
     rather than on, and now behaves like every other piece: ticked when the
     course lacks it, unticked and saying so when it already has it. */
  box.consol.checked = !STATUS.consolidated;
  /* The same rule as every other piece: ticked when the course lacks it,
     unticked and saying so when it already has it. */
  box.captions.checked = !STATUS.captions;
  already.lessons.textContent = STATUS.lessons
    ? 'already in: ' + STATUS.lessons + ' lesson' + (STATUS.lessons === 1 ? '' : 's') : '';
  already.videos.textContent = STATUS.videos
    ? 'already wired for ' + STATUS.videos : '';
  already.readings.textContent = STATUS.readings
    ? 'already in: ' + STATUS.readings : '';
  /* 🔴 THE ONE LABEL ON THIS PAGE THAT IS NOT ASSEMBLED HERE. Its wording says
     which question the number answers ("some captions", not "finished"), which
     is a requirement rather than a flourish, so it is built in
     `caption_count_line` where a test can run it. */
  already.captions.textContent = STATUS.captions_line || '';
  /* 🔴 THE ONE PIECE THAT IS NOT A COURSE PIECE, and the one hard-coded
     default on this page, both on purpose. The picture pack is shared by every
     course on the machine, so "what this course lacks" is the wrong question:
     when the pack is installed the whole piece is hidden (`p-pack`), and when
     it is not, it starts UNTICKED. The entry that specified it (EH, 2026-09-17:
     "give people the option to download") says DEFAULT OFF, because it is 44 MB
     of anatomy that a person should choose rather than receive, and the
     course-pieces rule above was never about it. */
  box.pack.checked = false;
  already.consol.textContent = STATUS.consolidated
    ? 'already built: ' + STATUS.consolidated + ' PDF'
      + (STATUS.consolidated === 1 ? '' : 's') : '';
  /* Measured totals, so choosing a download states its cost up front (EH,
     2026-08-23). Absent measurements say nothing rather than guessing. */
  function human(n) {
    if (n >= 1073741824) { return (n / 1073741824).toFixed(1) + ' GB'; }
    return Math.round(n / 1048576) + ' MB';
  }
  if (STATUS.video_bytes) {
    document.getElementById('sz-videos').textContent =
      'About ' + human(STATUS.video_bytes) + ' for this course, measured.';
  }
  if (STATUS.pack_bytes) {
    document.getElementById('sz-packs').textContent =
      'About ' + human(STATUS.pack_bytes) + ' for this course, measured from '
      + 'the mirrored copies.';
  }
  /* The picture pack's size comes from the update feed, which names the asset
     and its bytes, rather than from a number typed here that would go stale
     the first time the pack is rebuilt. Silent when the feed is off, cannot be
     reached, or names no pack: a missing size says nothing rather than guessing. */
  fetch('/api/update', { headers: window.STUDYTOKEN.headers({}) })
    .then(function (r) { return r.ok ? r.json() : null; })
    .then(function (d) {
      var p = d && d.packs && d.packs['brain-regions'];
      if (p && p.bytes) {
        document.getElementById('sz-pack').textContent =
          'About ' + human(p.bytes) + ', measured.';
      }
    })
    .catch(function () {});
  document.getElementById('pieces').textContent = 'This course so far: '
    + (STATUS.lessons ? STATUS.lessons + ' lesson' + (STATUS.lessons === 1 ? '' : 's') : 'no lessons yet')
    + ' \u00b7 ' + (STATUS.videos ? 'videos wired for ' + STATUS.videos : 'no videos yet')
    + ' \u00b7 ' + (STATUS.readings ? STATUS.readings + ' readings' : 'no readings yet');

  function src(name) {
    var r = document.querySelector('input[name="src-' + name + '"]:checked');
    return r ? r.value : 'local';
  }
  var dl = { gate: document.getElementById('w-dl'),
             videos: document.getElementById('w-dl-videos'),
             packs: document.getElementById('w-dl-packs') };
  /* 🔴 A FUNCTION replacement, and it is not a style choice. `String.replace`
     reads `$` in the REPLACEMENT as a substitution pattern, so a pasted value
     containing one rewrites the prompt around it. Measured 2026-09-09 against
     the real template: `$&` puts the placeholder BACK, so the reader who has
     just typed their address is handed a prompt still telling them to paste it,
     and `$'` duplicates the whole rest of the prompt. A function replacement is
     immune, and it is the only correct fix; escaping the value would change
     what the reader typed.

     ⚠️ This also repairs the folder field, which has had the same bug since it
     shipped and where a Windows path or a `$` in a folder name reaches it.

     An empty value leaves the placeholder alone, on purpose: the field is
     optional, and a reader who skips it must get today's prompt rather than a
     sentence with a hole in it. */
  function put(text, token, value) {
    if (!value) { return text; }
    return text.replace(token, function () { return value; });
  }

  function frag(piece) {
    if (piece === 'consol') { return FRAGS.consol; }
    if (piece === 'captions') { return FRAGS.captions; }
    if (piece === 'pack') { return FRAGS.pack; }
    if (piece === 'videos') {
      /* A course that already has its links wired is here for the downloads:
         the job must not tell Claude to collect what is already in. */
      var text = STATUS.videos ? FRAGS.videos_have : FRAGS.videos;
      if (dl.gate.checked && dl.packs.checked) { text += ' ' + FRAGS.videos_packs; }
      if (dl.gate.checked && dl.videos.checked) { text += ' ' + FRAGS.videos_files; }
      return text;
    }
    var which = piece + '_' + src(piece);
    var text = FRAGS[which];
    if (src(piece) === 'local' && folders[piece]) {
      text = put(text, TOKENS[piece], folders[piece].value.trim());
    } else if (src(piece) === 'site' && sites[piece]) {
      text = put(text, SITE_TOKENS[piece], sites[piece].value.trim());
    }
    return text;
  }
  var copyBtn = document.getElementById('gocopy');
  var promptEl = document.getElementById('goprompt');
  function compose() {
    ['lessons', 'readings'].forEach(function (p) {
      opts[p].hidden = !box[p].checked;
      folders[p].hidden = (src(p) !== 'local');
      if (sites[p]) { sites[p].hidden = (src(p) !== 'site'); }
    });
    opts.videos.hidden = !box.videos.checked;
    opts.captions.hidden = !box.captions.checked;
    opts.pack.hidden = !box.pack.checked;
    opts.consol.hidden = !box.consol.checked;
    document.getElementById('o-dl').hidden = !dl.gate.checked;
    var jobs = [];
    ['lessons', 'videos', 'captions', 'consol', 'readings', 'pack'].forEach(function (p) {
      if (box[p].checked) { jobs.push(frag(p)); }
    });
    if (!jobs.length) {
      promptEl.textContent = 'Tick at least one thing above and the prompt appears here.';
      copyBtn.disabled = true;
      return;
    }
    copyBtn.disabled = false;
    promptEl.textContent = FRAGS.header + '\\n\\n' + jobs.map(function (t, i) {
      return (i + 1) + '. ' + t;
    }).join('\\n\\n');
  }
  Array.prototype.forEach.call(
    document.querySelectorAll('#st-build input'),
    function (el) {
      el.addEventListener('change', compose);
      if (el.type === 'text') { el.addEventListener('input', compose); }
    });
  compose();

  /* ---- copy, honestly: execCommand reports failure by returning false ---- */
  copyBtn.addEventListener('click', function () {
    var text = promptEl.textContent;
    var was = copyBtn.textContent;
    function flash(word) {
      copyBtn.textContent = word;
      setTimeout(function () { copyBtn.textContent = was; }, 2400);
    }
    function fallback() {
      var ta = document.createElement('textarea');
      ta.value = text;
      ta.setAttribute('readonly', '');
      ta.style.cssText = 'position:fixed;top:0;left:0;opacity:0';
      document.body.appendChild(ta);
      ta.select();
      ta.setSelectionRange(0, text.length);
      var ok = false;
      try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
      ta.remove();
      if (ok) { flash('Copied'); return; }
      flash('Select and copy it');
      try {
        var range = document.createRange();
        range.selectNodeContents(promptEl);
        var sel = window.getSelection();
        sel.removeAllRanges();
        sel.addRange(range);
      } catch (e) {}
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(function () { flash('Copied'); }, fallback);
    } else { fallback(); }
  });

  /* ---- where the slides come from: two options, one of them chosen ----
     EH, 2026-08-28: "the choice is theirs". So the page never guesses on their
     behalf; it shows what is stored, and it says what that choice actually
     yields in this folder (38 of 50 have slides) so a wrong path is visible
     here rather than as an empty tab three lessons later. */
  var matSave = document.getElementById('matsave');
  var matSays = document.getElementById('matsays');
  var matHint = document.getElementById('mathint');
  var matDir = document.getElementById('matdir');
  function matPaint(rep) {
    if (!rep) { matHint.textContent = ''; return; }
    var radios = document.getElementsByName('matsrc');
    for (var i = 0; i < radios.length; i++) {
      radios[i].checked = (radios[i].value === rep.source);
    }
    if (document.activeElement !== matDir) { matDir.value = ''; }
    matDir.placeholder = rep.dir || 'Where that folder is';
    if (!rep.exists) {
      matHint.textContent = 'That folder is not there yet: ' + rep.dir;
      return;
    }
    var f = rep.found || {};
    matHint.textContent = rep.dir + ' holds slides for ' + (f.slides || 0) +
      ' of ' + (rep.parts || 0) + ' parts, and transcripts for ' +
      (f.transcript || 0) + '.';
  }
  fetch('/api/materials-source?module=' + encodeURIComponent(%(codejs)s),
        { headers: window.STUDYTOKEN ? window.STUDYTOKEN.headers({}) : {} })
    .then(function (r) { return r.json(); })
    .then(function (r) { if (r && r.ok) { matPaint(r.report); } })
    .catch(function () {});
  matSave.addEventListener('click', function () {
    var picked = 'keats';
    var radios = document.getElementsByName('matsrc');
    for (var i = 0; i < radios.length; i++) {
      if (radios[i].checked) { picked = radios[i].value; }
    }
    matSave.disabled = true;
    matSays.classList.remove('bad');
    matSays.textContent = 'Saving\u2026';
    fetch('/api/materials-source', {
      method: 'POST',
      headers: window.STUDYTOKEN
        ? window.STUDYTOKEN.headers({ 'Content-Type': 'application/json' })
        : { 'Content-Type': 'application/json' },
      body: JSON.stringify({ module: %(codejs)s, source: picked,
                             dir: matDir.value.trim() })
    })
      .then(function (r) { return r.json(); })
      .then(function (r) {
        matSave.disabled = false;
        if (!r.ok) {
          matSays.textContent = r.error || 'That did not work.';
          matSays.classList.add('bad');
          if (window.STUDYTOKEN && /token/i.test(r.error || '')) window.STUDYTOKEN.ask();
          return;
        }
        matPaint(r.report);
        matSays.textContent = r.report.source === 'local'
          ? 'Saved. The Materials pane now reads that folder, and shows no slide tab for a part whose file is not in it.'
          : 'Saved. The Materials pane links out to the course site.';
      })
      .catch(function () {
        matSave.disabled = false;
        matSays.textContent = 'Could not reach the server.';
        matSays.classList.add('bad');
      });
  });

  /* ---- share: one button, one zip, and the page says where it landed ---- */
  var shareBtn = document.getElementById('sharego');
  var shareCaps = document.getElementById('sharecaps');
  var ssays = document.getElementById('ssays');
  shareBtn.addEventListener('click', function () {
    shareBtn.disabled = true;
    ssays.textContent = 'Packing the course\u2026';
    ssays.classList.remove('bad');
    var headers = window.STUDYTOKEN ? window.STUDYTOKEN.headers({}) : {};
    var q = '/api/share?module=' + encodeURIComponent(%(codejs)s);
    if (shareCaps && shareCaps.checked) q += '&captions=1';
    fetch(q, { method: 'POST', headers: headers })
      .then(function (r) { return r.json(); })
      .then(function (r) {
        shareBtn.disabled = false;
        if (!r.ok) {
          ssays.textContent = r.error || 'That did not work.';
          ssays.classList.add('bad');
          if (window.STUDYTOKEN && /token/i.test(r.error || '')) window.STUDYTOKEN.ask();
          return;
        }
        var rep = r.report || {};
        var msg = 'Ready: ' + (r.zip || '') + ', holding ' +
          (rep.lessons || 0) + ' lessons, ' + (rep.glossary || 0) +
          ' glossary terms, ' + (rep.readings || 0) + ' readings and ' +
          (rep.mistakes || 0) + ' mistakes-page entries' +
          (rep.captions ? ', and captions for ' + rep.captions + ' lecture' +
            (rep.captions === 1 ? '' : 's') + ' (' + (rep.cues || 0) + ' cues)' : '') +
          '.';
        /* Said here rather than left to be discovered: the recipient's Materials
           pane will have the lecture link and not the slides, and that is the
           design, not a fault. */
        if (rep.drive_withheld) {
          msg += ' ' + rep.drive_withheld + ' lesson' +
            (rep.drive_withheld === 1 ? "'s" : "s'") +
            ' slides and transcripts are NOT included: those are addresses on your' +
            ' own Google Drive. The lecture links are included.';
        }
        ssays.textContent = msg;
      })
      .catch(function () {
        shareBtn.disabled = false;
        ssays.textContent = 'Could not reach the server.';
        ssays.classList.add('bad');
      });
  });

  /* ---- the file lands HERE (EH, 2026-08-22: "why not just have them drop
     it in there?"). Same endpoint as the course page: the SERVER reads the
     bytes and decides what the file is, and a success refreshes this page so
     the checklist re-defaults to the new truth. ---- */
  var isays = document.getElementById('isays');
  function itell(msg, bad) {
    isays.textContent = msg || '';
    isays.classList.toggle('bad', !!bad);
  }
  function iurl(file, force) {
    return '/api/import?name=' + encodeURIComponent(file.name)
         + '&module=' + encodeURIComponent(%(codejs)s)
         + (force ? '&force=1' : '');
  }
  var imported = 0;
  function isend(files, i) {
    if (i >= files.length) {
      if (imported) {
        itell('Done. Refreshing so the checklist matches\u2026');
        setTimeout(function () { window.location.reload(); }, 1200);
      }
      return;
    }
    var f = files[i];
    itell('Importing ' + f.name + '\u2026');
    fetch(iurl(f, false), { method: 'POST',
                            headers: window.STUDYTOKEN.headers({}), body: f })
      .then(function (r) { return r.json().then(function (j) { return {s: r.status, b: j}; }); })
      .then(function (r) {
        if (r.s === 401) {
          itell('');
          window.STUDYTOKEN.ask(function () { isend(files, i); });
          return;
        }
        if (r.s === 409) {
          itell(f.name + ' is already in this course. ', true);
          var b = document.createElement('button');
          b.type = 'button'; b.textContent = 'Replace it';
          b.addEventListener('click', function () {
            fetch(iurl(f, true), { method: 'POST',
                                   headers: window.STUDYTOKEN.headers({}), body: f })
              .then(function (r2) { return r2.json(); })
              .then(function (j2) {
                if (j2 && j2.ok) { imported += 1; }
                isend(files, i + 1);
              }).catch(function () { itell('Could not reach the server.', true); });
          });
          isays.appendChild(b);
          return;
        }
        if (!r.b || !r.b.ok) {
          itell(f.name + ': ' + ((r.b && r.b.error) || 'that did not import.'), true);
          isend(files, i + 1);
          return;
        }
        imported += 1;
        itell('In: ' + f.name + (r.b.kind ? ' (' + r.b.kind + ')' : ''));
        isend(files, i + 1);
      }).catch(function () { itell('Could not reach the server.', true); });
  }
  var filein = document.getElementById('filein');
  function pick(e) { e.preventDefault(); filein.click(); }
  document.getElementById('pickfile').addEventListener('click', pick);
  document.getElementById('pickvideos').addEventListener('click', pick);
  filein.addEventListener('change', function () {
    imported = 0;
    isend(Array.prototype.slice.call(filein.files), 0);
    filein.value = '';
  });
  ['dragover', 'dragenter'].forEach(function (ev) {
    document.addEventListener(ev, function (e) {
      e.preventDefault();
      document.body.classList.add('dropping');
    });
  });
  ['dragleave', 'drop'].forEach(function (ev) {
    document.addEventListener(ev, function (e) {
      if (ev === 'drop') { e.preventDefault(); }
      document.body.classList.remove('dropping');
    });
  });
  document.addEventListener('drop', function (e) {
    var files = e.dataTransfer && e.dataTransfer.files;
    if (files && files.length) {
      imported = 0;
      isend(Array.prototype.slice.call(files), 0);
    }
  });

  show(ASKNAME ? 'name' : 'build');
  if (ASKNAME) { document.getElementById('cname').focus(); }
})();
</script>
"""


# --------------------------------------------------------------------------
# The readings, as CONTENT the reader composes
# --------------------------------------------------------------------------
#
# 🔴 EH, 2026-08-22: "I just want to make sure that the paper
# summaries have all the same highlighting, acting, and marking mechanisms that
# we have for the rest of the lesson... essentially use the same reader wrapper
# functionality." So the readings page stopped being its own page and became a
# DOCUMENT: this function renders readings.json into the exact content format a
# lesson file holds (title, lesson-meta, CSS, .wrap), and the route passes it
# through compose_lesson, the same door every lesson goes through. Highlights,
# notes, cards, chats and bookmarks all follow, because to the reader this IS a
# lesson.
#
# What that decides, and why:
#
# - **The doc id is READINGS**, one document per course. Marks live in
#   READINGS-marks.json beside the lessons, keyed like any other doc.
# - **Only block-selector elements carry prose.** The page numbers blocks with
#   the .wrap selector (p, li, h1-h4, dd, dt, figcaption, blockquote, td, th),
#   so every sentence someone might highlight is inside one of those. Section
#   bodies are <p>, terms are <dt>/<dd>. A <div> of prose would be invisible to
#   the mark model.
# - **Regeneration moves block indices, and that is already defended.** Adding
#   a reading shifts every block after it; each mark stores its own text and
#   reanchor() finds it again, or honestly orphans it. Same contract as editing
#   a lesson.
# - **The vault meta is deliberately empty.** These marks have no vault home
#   yet (the layer now skips publishing for a doc with no week rather than
#   erroring forever); where they should publish is an open question in the
#   queue.
def readings_content(cfg, with_map=False):
    """The course's readings as a content-format document, or None if none.

    One implementation, used by the route that serves it, by verify_notes, and
    by the vault publisher, so the numbers the browser sees, the numbers the
    checker sees, and the reading each mark is attributed to cannot drift
    apart. Same reasoning as split_lessons itself.

    With with_map=True, returns (text, map) where map is a list of
    {"id", "title", "start", "end"}: the half-open block-index range each
    reading's card occupies, counted by the same find_blocks rule the page and
    the checker use."""
    import readings as R
    esc = html_mod.escape
    items = R.read_all(cfg["notes_dir"])
    if not items:
        return (None, []) if with_map else None
    course = str(cfg.get("module_name") or cfg.get("module_code") or "this course")

    def notice_glyph(r):
        n = r.get("notice") or {}
        if not isinstance(n, dict) or not n.get("kind"):
            return ""
        return ('<span class="rcaution" title="This paper has a published %s" '
                'aria-label="This paper has a published %s">&#9888;</span>'
                % (esc(n["kind"]), esc(n["kind"])))

    cards = []
    for r in items:
        bits = []
        bits.append('<h2>%s%s</h2>' % (esc(str(r.get("title") or r["id"])),
                                       notice_glyph(r)))
        who = [esc(str(r.get(k))) for k in ("authors", "year", "venue") if r.get(k)]
        bits.append('<p class="who"><span class="kind">%s</span>%s</p>'
                    % (esc(str(r.get("kind") or "reading")), " &middot; ".join(who)))
        n = r.get("notice") or {}
        if isinstance(n, dict) and n.get("kind"):
            # EH's design, 2026-08-23: the caution wears the glyph, the entry
            # states the fact, and the notice itself is one click away.
            nurl = n.get("url") or ("https://doi.org/" + n["doi"]
                                    if n.get("doi") else "")
            link = (' <a href="%s" target="_blank" rel="noopener">Read the '
                    'notice &rarr;</a>' % esc(nurl, quote=True)) if nurl else ""
            an = "An" if n["kind"][0] in "aeiou" else "A"
            bits.append('<p class="rnotice">&#9888; %s %s was published for '
                        'this paper.%s</p>' % (an, esc(n["kind"]), link))
        if r.get("file"):
            href, label = esc(r["file"], quote=True), "Open the reading"
        elif r.get("url"):
            href = esc(r["url"], quote=True)
            label = "Open it at doi.org" if r.get("doi") else "Open the reading"
        else:
            href = ""
        bits.append(('<p class="openit"><a href="%s" target="_blank" '
                     'rel="noopener">%s &rarr;</a></p>' % (href, label)) if href
                    else '<p class="openit none">No copy linked yet.</p>')
        if r.get("claim"):
            bits.append('<p class="claim">%s</p>' % esc(r["claim"]))
        if r.get("why"):
            bits.append('<p class="why">%s</p>' % esc(r["why"]))
        if r.get("takeaways"):
            bits.append('<ul class="take">%s</ul>'
                        % "".join("<li>%s</li>" % esc(t) for t in r["takeaways"]))
        for sec in r.get("sections") or []:
            bits.append('<details class="d"><summary>%s</summary>'
                        '<p class="dbody">%s</p></details>'
                        % (esc(sec["h"]), esc(sec["body"])))
        if r.get("terms"):
            rows = "".join('<dt>%s</dt><dd>%s</dd>' % (esc(t["t"]), esc(t["d"]))
                           for t in r["terms"])
            bits.append('<details class="d"><summary>Terms it introduces (%d)'
                        '</summary><dl class="terms">%s</dl></details>'
                        % (len(r["terms"]), rows))
        if r.get("links"):
            metas = lesson_meta_index(cfg)
            rel = ["<a href=\"%s\">%s</a>" % (esc(metas[d]["file"], quote=True),
                                              esc(str(metas[d].get("title") or d)))
                   for d in r["links"] if metas.get(d, {}).get("file")]
            if rel:
                bits.append('<p class="rel">Read alongside: %s</p>' % " ".join(rel))
        cards.append(('<div class="r" id="r-%s">%s</div>'
                      % (esc(str(r["id"]), quote=True), "".join(bits)), r))

    n = len(items)
    deep = sum(1 for r in items if r.get("sections"))
    meta = {"doc": "READINGS", "title": "Core readings",
            "week": "", "topicNo": "", "topic": "Core readings",
            "weekTitle": "", "part": ""}
    # The index: one row per reading, grouped by week, each opening that
    # reading on its own. Rows link by hash so the browser's back button is
    # the way back, with no server round trip.
    ix = []
    last_week = object()
    for r in items:
        wk = str(r.get("week") or "").strip()
        if wk != last_week:
            last_week = wk
            ix.append('<p class="rixw">%s</p>'
                      % (('Week ' + esc(wk)) if wk else 'Unassigned'))
        who = " &middot; ".join(esc(str(r.get(k))) for k in ("authors", "year")
                                if r.get(k))
        ix.append('<a class="rixrow" href="#r=%s">'
                  '<span class="rixtitle">%s%s</span>'
                  '<span class="rixwho">%s</span>%s</a>'
                  % (esc(str(r["id"]), quote=True),
                     esc(str(r.get("title") or r["id"])),
                     notice_glyph(r),
                     who,
                     ('<span class="rixclaim">%s</span>' % esc(r["claim"]))
                     if r.get("claim") else ""))
    ix.append('<p class="rixall"><a href="#all">Show them all on one page</a></p>')

    text = (
        "<!-- study-lesson:v1 -->\n"
        "<title>Core readings: %s</title>\n"
        '<script type="application/json" id="lesson-meta">\n%s\n</script>\n'
        "<style>\n%s</style>\n"
        '<div class="wrap">\n'
        '<h1>Core readings</h1>\n'
        '<p class="rsub">%d reading%s, %d opening into detail. Pick one from the '
        "list and it opens on its own; highlights and notes work there like any "
        "lesson.</p>\n"
        '<p class="rback"><a href="#">&larr; Back to the reading list</a></p>\n'
        '<nav class="rix">%s</nav>\n%s\n</div>\n'
        "<script>\n"
        "(function () {\n"
        "  var wrap = document.querySelector('.wrap');\n"
        "  function mode() {\n"
        "    var m = /^#r=(.+)$/.exec(location.hash);\n"
        "    wrap.classList.remove('rix-list', 'rix-one', 'rix-all');\n"
        "    Array.prototype.forEach.call(wrap.querySelectorAll('.r'),\n"
        "      function (c) { c.classList.remove('on'); });\n"
        "    if (m) {\n"
        "      var open = document.getElementById('r-' + decodeURIComponent(m[1]));\n"
        "      if (open) {\n"
        "        wrap.classList.add('rix-one');\n"
        "        open.classList.add('on');\n"
        "        window.scrollTo(0, 0);\n"
        "        return;\n"
        "      }\n"
        "    }\n"
        "    if (location.hash === '#all') { wrap.classList.add('rix-all'); return; }\n"
        "    wrap.classList.add('rix-list');\n"
        "    window.scrollTo(0, 0);\n"
        "  }\n"
        "  window.addEventListener('hashchange', mode);\n"
        "  mode();\n"
        "})();\n"
        "</script>\n"
        % (esc(course), json.dumps(meta, indent=2), READINGS_CONTENT_CSS,
           n, "" if n == 1 else "s", deep, "".join(ix),
           "\n".join(html for html, _ in cards)))
    if not with_map:
        return text
    # The head of the document (h1 + the sub line) takes the first blocks, and
    # each card takes the next len(find_blocks(card)) of them. Counted with the
    # SAME rule everything else uses, on the same markup, so a card added above
    # cannot silently shift attribution below.
    head_end = text.index('<div class="r"')
    at = len(find_blocks(text[:head_end] + "</div>"))
    blockmap = []
    for html, r in cards:
        # 🔴 BlockFinder counts only blocks inside .wrap, so a bare fragment
        # counts as zero. Caught by the covering assertion on first run: every
        # card mapped to an empty range and every mark would have been
        # attributed to no reading. The fragment is counted inside a wrap of
        # its own, which is exactly how it sits in the real document.
        nblocks = len(find_blocks('<div class="wrap">%s</div>' % html))
        blockmap.append({"id": r["id"], "title": str(r.get("title") or r["id"]),
                         "doi": r.get("doi", ""), "url": r.get("url", ""),
                         "authors": r.get("authors", ""), "year": r.get("year"),
                         "kind": r.get("kind", ""),
                         "start": at, "end": at + nblocks})
        at += nblocks
    return text, blockmap


# The document's own stylesheet, as every lesson carries its own. Base tokens
# copied from lesson-base.css so the composed page holds up alone, plus the
# reading-card classes.
# The generated hub's tree, filter and view switch. In the body rather than
# in HOME_PAGE because the home page shares that template and lists courses,
# not lessons. The view choice is the viewer's, remembered per browser
# (localStorage, guarded: a blocked store must never break the hub).
HUB_TREE_CSS = """<style>
  .hubwrap { grid-column: 1 / -1; min-width: 0; }
  .hubtop { display: flex; gap: 10px; align-items: center; margin: 16px 0 4px; }
  #hfind { flex: 1; font: 400 14px var(--text, sans-serif); color: var(--ink, inherit);
           background: transparent; border: 1px solid var(--rule, #8884);
           border-radius: 8px; padding: 8px 12px; min-width: 0; }
  .hviews { display: flex; gap: 4px; }
  .hviews button { font: 500 12px var(--text, sans-serif); color: var(--ink, inherit);
                   background: transparent; border: 1px solid var(--rule, #8884);
                   border-radius: 6px; padding: 6px 10px; cursor: pointer; }
  .hviews button[aria-pressed="true"] { border-color: var(--accent, #1C6D61);
                                        color: var(--accent, #1C6D61); }
  .hprog { font: 400 13px var(--text, sans-serif); color: var(--muted, inherit); margin: 4px 0 10px; }
  .hprog a { color: var(--accent, #1C6D61); }
  .hmist { margin: 22px 0 0; font: 500 13.5px var(--text, sans-serif); }
  .hmist a { color: var(--accent, #1C6D61); text-decoration: none; }
  .hmist a:hover { text-decoration: underline; }

  /* Where he is in the course, at a glance: one slim meter per kind of done. */
  .hmeters { display: flex; flex-direction: column; gap: 6px; margin: 0 0 8px; max-width: 520px; }
  /* The tally sits with the bars, because it is the same idea in time rather
     than in count. Muted and one line: it is a fact about the course, not a
     control. */
  .htime { margin: 0 0 18px; max-width: 520px; color: var(--muted); font-size: .93em; }
  .htime .htt { color: var(--ink); }
  .htgap { display: block; margin-top: 3px; color: var(--broken); }
  .hmeter { display: flex; align-items: center; gap: 10px; }
  .hml { font: 600 11px var(--text, sans-serif); color: var(--muted, inherit);
         text-transform: uppercase; letter-spacing: .06em; width: 60px; }
  .hbar { flex: 1; height: 7px; border-radius: 4px; background: var(--rule, #8884);
          overflow: hidden; }
  .hbar i { display: block; height: 100%; background: var(--accent, #1C6D61);
            border-radius: 4px; transition: width .25s; }
  .hmn { font: 500 12px var(--text, sans-serif); color: var(--muted, inherit);
         min-width: 62px; text-align: right; font-variant-numeric: tabular-nums; }

  /* EH, 2026-08-23: "the topic rows need to be more prominent... it's just one
     long list." The week is a landmark, the topic is a band, the parts are the
     quiet rows under them. */
  .hweek { margin: 30px 0 0; }
  .hwh { font: 700 21px var(--display, serif); margin: 0 0 2px;
         padding-bottom: 8px; border-bottom: 2px solid var(--accent, #1C6D61); }
  .hwt { font-weight: 400; font-size: 16px; color: var(--muted, inherit); }
  .hth { font: 600 13px var(--text, sans-serif); color: var(--accent, #1C6D61);
         text-transform: uppercase; letter-spacing: .05em;
         background: var(--accent-wash, transparent);
         border-left: 3px solid var(--accent, #1C6D61);
         border-radius: 0 6px 6px 0; padding: 7px 11px; margin: 16px 0 8px; }

  .hrow { display: flex; align-items: center; gap: 10px;
          border: 1px solid var(--rule, #8884); border-radius: 8px;
          padding: 8px 12px; margin: 0 0 6px; background: var(--surface, transparent); }
  .hrow:hover { border-color: var(--accent, #1C6D61); }
  .hpart { display: block; flex: 1; min-width: 0; text-decoration: none; color: inherit; }
  .hpart .ht { display: block; font: 600 14px/1.35 var(--text, sans-serif); }
  .hpart .hm { display: block; font: 400 11.5px var(--text, sans-serif);
               color: var(--muted, inherit); margin-top: 2px; }
  .hrow.seen .ht { opacity: .75; }

  .hctl { display: flex; align-items: center; gap: 6px; flex: 0 0 auto; }
  .hflag { font: 500 11px var(--text, sans-serif); color: var(--muted, inherit);
           background: transparent; border: 1px solid var(--rule, #8884);
           border-radius: 999px; padding: 3px 9px; cursor: pointer; }
  .hflag[aria-pressed="true"] { color: var(--accent, #1C6D61);
      border-color: var(--accent, #1C6D61); background: var(--accent-wash, transparent); }
  /* Two scales, one shape. On a PART they stack, because Read and Watched
     already take the width; on a topic or a week they sit side by side, where
     EH pointed out there is more room and no flags competing for it. */
  .hrateset { display: flex; flex-direction: column; align-items: flex-end;
              gap: 1px; margin-left: 2px; }
  .hrateset.side { flex-direction: row; align-items: center; gap: 8px;
                   margin-left: 10px; vertical-align: middle; }
  /* 🔴 A GRID, so glyph five sits above glyph five whatever the glyphs are.
     EH, 2026-09-08, with a screenshot: "the stars and the light bulbs that are
     one over the other on the right side don't align, and that looks weird."

     THE CAUSE, read out of these two rules rather than guessed: `.hrates` was
     an `inline-flex` of five buttons each sized by its own content, and the two
     scales do not have the same content. The star is a TEXT glyph (U+2605) at
     15px; the bulb is an EMOJI (U+1F4A1) at 13px, and an emoji is drawn from a
     different font with a different advance width. Five of one and five of the
     other are therefore two different widths, and because `.hrateset` is
     `align-items: flex-end` the difference all lands on the LEFT, which is
     exactly the drift in his screenshot.

     ⚠️ NOT FIXED BY TUNING LETTER-SPACING ON ONE ROW. That holds for today's
     two glyphs and breaks the next time either changes, or the first time a
     reader's emoji font is not Apple's. Equal TRACKS cannot come apart: the
     track is 20px because the star at 15px is the wider of the two and needs
     it, and both scales now sit on the same five columns. */
  .hrates { display: grid; grid-template-columns: repeat(5, 20px); justify-items: stretch; }
  .hrate { font-size: 15px; line-height: 1; padding: 1px 0; cursor: pointer;
           background: none; border: 0; width: 100%; text-align: center; }
  .hrate:hover { transform: scale(1.15); }
  .hstar.on { color: var(--accent, #1C6D61); }

  /* 🔴 The OFF state of both scales, measured rather than eyeballed. QA's
     finding, 2026-08-30, and the manager's ruling on it: the off star was
     `--rule` on `--paper`, which is **1.21:1 in light and 1.38:1 in dark**. It
     was not that the bulb was faint beside a healthy star; the star was already
     a ghost and the bulb was then dimmed further. A control nobody can see
     before they have used it is not consistent styling.

     The target is 3:1 for both, in both themes, at the SAME measured weight,
     and every number below was measured in Chrome on the real hub rather than
     picked: the star by its colour against `--paper`, the bulb by rendering the
     same glyph through the same filter onto the same ground and taking the
     luminance of its strongest tenth of pixels, which is what the eye picks the
     shape out by.

     🔴 `opacity` is gone and `brightness` replaced it, and that is not a taste
     call. A greyscaled 💡 on a LIGHT page tops out at **1.98:1 at opacity 1**:
     there is no opacity that reaches 3:1, because the glyph's own greys are
     lighter than the target. `brightness` moves the tone itself, so one knob per
     theme lands both scales together.

     🔴 The ON state does not change and must not: EH's lit bulbs at full colour
     against an unlit row are the part that already works. Hence `:not(.on)` on
     every rule here rather than a later rule undoing an earlier one.

     🔴 **Levelled UP on 2026-08-30, never down, and that direction is a rule.**
     The first pass matched the two scales in dark by DIMMING the bulb from 3.76
     to 3.25 to meet a star at 3.30. QA caught it: the target sentence had said
     to match them by dropping the bulb's `opacity` stack, not by dimming the
     star to fit, and a rule born from an invisible control must never be the
     reason a control gets less visible. So the dark bulb is back at the weight
     the opacity stack gave it (`brightness(.46)`, 3.75 against 3.76 before) and
     the dark STAR rose to meet it (#6C767A, 3.78).

     🔴 **And the light header bulb was moved off the line rather than argued
     about.** Three sessions measured the same glyph and got 3.29, 2.99 and 3.69,
     because "the strongest tenth of an emoji's pixels" is not one method: it
     depends on the rendered size, the device pixel ratio and whether
     antialiasing counts. All three are defensible and one of them was under 3.
     When two honest methods straddle a threshold the cheap answer is margin,
     not a convention nobody can check, so the light header bulb went .74 -> .70.
     Do not re-state the threshold as "3:1 by our convention"; that is how a
     number on the line becomes permanent. */
  .hrate:not(.on) { color: #828786; }                  /* 3.30:1 on #F1F4F3 */
  .hbulb { font-size: 13px; }
  .hbulb:not(.on) { filter: grayscale(1) brightness(.68); opacity: 1; }
  .hbulb.on { filter: none; opacity: 1; }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) .hrate:not(.on) { color: #6C767A; }
    :root:not([data-theme="light"]) .hbulb:not(.on) {
      filter: grayscale(1) brightness(.46); }
  }
  :root[data-theme="dark"] .hrate:not(.on) { color: #6C767A; }
  :root[data-theme="dark"] .hbulb:not(.on) { filter: grayscale(1) brightness(.46); }
  /* The headings are uppercase small caps; the controls must not inherit that. */
  .hth .hrateset, .hwh .hrateset { text-transform: none; letter-spacing: normal; }
  /* Label left, controls right, on ONE line at both levels. Flex rather than a
     float so the topic heading matches the week heading instead of wrapping its
     controls onto a second line. `gap` keeps them apart when the title is long,
     and wrapping is still allowed so a very long topic name does not squash
     them. */
  .hth, .hwh { display: flex; align-items: center; justify-content: space-between;
               gap: 12px; flex-wrap: wrap; }
  .hth .hrateset.side, .hwh .hrateset.side { margin-left: auto; }

  /* Core ideas: a pill beside the name that opens IN PLACE, which is what EH
     asked for ("a little core ideas pill to the right of the name, not to open
     as a separate lesson page"). `<details>` so it needs no script and keeps
     the keyboard behaviour the browser already gives it.

     🔴 The ORDER is what puts the pill in the right place in both states, and
     it is the whole trick: closed, it sits right after the name; open, the
     panel takes a full line of the wrapping flex row so it reads underneath
     rather than squeezing the ratings. Without this the panel would open into
     whatever width was left beside the heading. */
  .hwhn, .hthn { font: inherit; color: inherit; margin: 0; letter-spacing: inherit; }
  .hci { order: 1; }
  .hci[open] { order: 3; flex-basis: 100%; }
  .hcip { cursor: pointer; display: inline-block; list-style: none;
          font: 600 11px var(--text, sans-serif); letter-spacing: .04em;
          text-transform: uppercase; color: var(--accent, #1C6D61);
          border: 1px solid var(--accent, #1C6D61); border-radius: 999px;
          padding: 2px 10px; white-space: nowrap; }
  .hcip::-webkit-details-marker { display: none; }
  .hcip:hover, .hci[open] .hcip { background: var(--accent, #1C6D61);
                                  color: var(--surface, #fff); }
  .hcib { font: 400 14px/1.55 var(--text, sans-serif); color: var(--ink, inherit);
          text-transform: none; letter-spacing: normal;
          background: var(--surface, transparent);
          border: 1px solid var(--rule, #8884); border-left: 3px solid var(--accent, #1C6D61);
          border-radius: 0 8px 8px 0; padding: 10px 14px; margin: 8px 0 2px; }
  .hcib > :first-child { margin-top: 0; }
  .hcib > :last-child { margin-bottom: 0; }
  .hcib p { margin: 0 0 8px; }
  .hcib ul { margin: 0 0 8px; padding-left: 20px; }
  .hcib li { margin: 0 0 4px; }
  .hcib h2, .hcib h3, .hcib h4 { font: 600 14px var(--text, sans-serif);
                                 margin: 10px 0 4px; }

  .htree[data-view="cards"] .htopic { display: grid; gap: 8px;
      grid-template-columns: repeat(auto-fill, minmax(230px, 1fr)); }
  .htree[data-view="cards"] .hth { grid-column: 1 / -1; margin-bottom: 0; }
  .htree[data-view="cards"] .hrow { margin: 0; flex-direction: column;
      align-items: stretch; gap: 8px; }
  .htree[data-view="cards"] .hctl { justify-content: space-between; }
  .hrow[hidden], .htopic[hidden], .hweek[hidden] { display: none; }
</style>"""

HUB_TREE_JS = """<script>
(function () {
  var tree = document.getElementById('htree');
  var find = document.getElementById('hfind');
  var btns = document.querySelectorAll('.hviews button');
  function setView(v) {
    tree.setAttribute('data-view', v);
    for (var i = 0; i < btns.length; i++) {
      btns[i].setAttribute('aria-pressed', String(btns[i].getAttribute('data-hv') === v));
    }
    try { localStorage.setItem('kcl-hub-view', v); } catch (e) {}
  }
  var v0 = 'list';
  try { v0 = localStorage.getItem('kcl-hub-view') || 'list'; } catch (e) {}
  setView(v0 === 'cards' ? 'cards' : 'list');
  for (var i = 0; i < btns.length; i++) {
    btns[i].addEventListener('click', function () { setView(this.getAttribute('data-hv')); });
  }
  find.addEventListener('input', function () {
    var q = find.value.trim().toLowerCase();
    var weeks = tree.querySelectorAll('.hweek');
    for (var w = 0; w < weeks.length; w++) {
      var wAny = false;
      var topics = weeks[w].querySelectorAll('.htopic');
      for (var t = 0; t < topics.length; t++) {
        var tAny = false;
        var rows = topics[t].querySelectorAll('.hrow');
        for (var p = 0; p < rows.length; p++) {
          var a = rows[p].querySelector('.hpart');
          var hit = !q || (a.getAttribute('data-find') || '').indexOf(q) !== -1;
          rows[p].hidden = !hit;
          if (hit) { tAny = true; }
        }
        topics[t].hidden = !tAny;
        if (tAny) { wAny = true; }
      }
      weeks[w].hidden = !wAny;
    }
  });

  /* ---- read, watched, stars: EH's declared state, saved on the server ---- */
  function post(doc, patch, done) {
    /* The token bar's script sits AFTER the body in the template, so at parse
       time STUDYTOKEN does not exist yet; anything that posts at load time
       must wait for it. Found by the migration silently dying on it. */
    if (!window.STUDYTOKEN) { return; }
    patch.module = window.HUBMODULE;
    patch.doc = doc;
    fetch('/api/lessonstate', {
      method: 'POST',
      headers: window.STUDYTOKEN.headers({ 'Content-Type': 'application/json' }),
      body: JSON.stringify(patch)
    }).then(function (r) {
      if (r.status === 401) {
        window.STUDYTOKEN.ask(function () { post(doc, patch, done); });
        return null;
      }
      return r.json();
    }).then(function (j) {
      if (j && j.ok) { done(j.state || {}); }
    }).catch(function () {});
  }
  function recount() {
    ['read', 'watched'].forEach(function (k) {
      var count = tree.querySelectorAll('.hflag[data-k="' + k + '"][aria-pressed="true"]').length;
      var n = document.querySelector('.hmn[data-n="' + k + '"]');
      var bar = document.querySelector('.hbar i[data-bar="' + k + '"]');
      if (n) { n.textContent = count + ' of ' + window.HUBTOTAL; }
      if (bar) { bar.style.width = (window.HUBTOTAL ? Math.round(100 * count / window.HUBTOTAL) : 0) + '%'; }
    });
    retally();
  }
  /* 🔴 "10 h 9 min", and it must say exactly what the Python says: the page is
     rendered by `hours_and_minutes()` on the server and re-rendered by this
     when EH presses Watched, so a reader pressing the button would otherwise
     watch the wording change under them. `test_video_tally.py` drives both over
     the same table. */
  function hm(mins) {
    mins = Math.max(0, Math.round(Number(mins) || 0));
    if (mins <= 0) { return 'none'; }
    var h = Math.floor(mins / 60), m = mins % 60;
    if (!h) { return m + ' min'; }
    if (!m) { return h + ' h'; }
    return h + ' h ' + m + ' min';
  }
  /* A quantity and its label wrap as one thing or the label orphans: measured
     at 382px, where "left" landed alone on the next line and the figure above
     it read as a bare number. Every space in the phrase, not just the last
     one, because "9 h 53 min" has two of its own. */
  function nb(s) { return String(s).replace(/ /g, '\u00a0'); }
  /* The minutes are re-derived from the rows for the same reason the counts
     are: the row is the only thing that knows whether it is watched NOW, and a
     total carried in a variable is a second copy that goes stale the moment
     something else changes a flag. The TOTAL is left alone, because marking a
     lesson watched does not change how long the course is. */
  function retally() {
    var line = document.querySelector('.htime');
    if (!line) { return; }
    var watched = 0;
    var rows = tree.querySelectorAll('.hrow[data-min]');
    for (var i = 0; i < rows.length; i++) {
      var on = rows[i].querySelector('.hflag[data-k="watched"][aria-pressed="true"]');
      if (on) { watched += Number(rows[i].getAttribute('data-min')) || 0; }
    }
    var totalEl = line.querySelector('.htt');
    var wEl = line.querySelector('.htw');
    var lEl = line.querySelector('.htl');
    var total = 0;
    for (var j = 0; j < rows.length; j++) { total += Number(rows[j].getAttribute('data-min')) || 0; }
    if (wEl) {
      wEl.setAttribute('data-watchedmin', String(watched));
      wEl.textContent = nb(hm(watched) + ' watched');
    }
    if (lEl) { lEl.textContent = nb(hm(Math.max(0, total - watched)) + ' left'); }
    /* Only the FIGURE, because "across these 38 lessons" is static text beside
       this span and the count is the server's, derived from the same number the
       bars print. Rewriting it here would be a second copy of that count. */
    if (totalEl) { totalEl.textContent = nb(hm(total)); }
  }
  /* One paint for both scales at all three levels. `set` is the .hrateset for
     one unit, so a week's bulbs cannot repaint a part's. */
  function paintRates(set, kind, value) {
    var word = (kind === 'stars') ? 'hstar' : 'hbulb';
    var all = set.querySelectorAll('.hrate[data-k="' + kind + '"]');
    for (var i = 0; i < all.length; i++) {
      all[i].className = 'hrate ' + word +
        (Number(all[i].getAttribute('data-n')) <= value ? ' on' : '');
    }
  }
  tree.addEventListener('click', function (e) {
    var flag = e.target.closest ? e.target.closest('.hflag') : null;
    var rate = e.target.closest ? e.target.closest('.hrate') : null;
    if (!flag && !rate) { return; }
    if (flag) {
      var row = e.target.closest('.hrow');
      var k = flag.getAttribute('data-k');
      var want = flag.getAttribute('aria-pressed') !== 'true';
      var patch = {};
      patch[k] = want;
      post(row.getAttribute('data-doc'), patch, function () {
        flag.setAttribute('aria-pressed', String(want));
        recount();
      });
      return;
    }
    /* 🔴 The nearest [data-unit], not the nearest .hrow. A part is a row, but a
       topic and a week are headings with no row of their own, and walking to
       .hrow from a week's bulbs would either miss or, worse, find the first
       lesson underneath and rate that instead. */
    var set = rate.closest('[data-unit]');
    if (!set) { return; }
    var kind = rate.getAttribute('data-k');
    var n = Number(rate.getAttribute('data-n'));
    var cur = set.querySelectorAll('.hrate[data-k="' + kind + '"].on').length;
    var next = (n === cur) ? 0 : n;       /* tapping the current value clears */
    var patch = {};
    patch[kind] = next;
    post(set.getAttribute('data-unit'), patch, function () {
      paintRates(set, kind, next);
    });
  });

  /* ---- one-time migration from the retired hand-written hub ----
     Its state lived in this browser under 'kcl-affective-hub': overrides he
     clicked, keyed by doc code. Server rows win; only codes the server has
     nothing for are carried over, liked becomes five stars, and the flag
     below stops it ever running twice in this browser. */
  function migrate() {
    var raw = null;
    try {
      if (localStorage.getItem('kcl-hub-migrated-' + window.HUBMODULE)) { return; }
      raw = localStorage.getItem('kcl-affective-hub');
    } catch (e) { return; }
    if (!raw) { return; }
    var data;
    try { data = JSON.parse(raw) || {}; } catch (e) { return; }
    var todo = [];
    Object.keys(data).forEach(function (code) {
      var row = tree.querySelector('.hrow[data-doc="' + code + '"]');
      if (!row) { return; }
      var flags = row.querySelectorAll('.hflag[aria-pressed="true"]').length;
      var stars = row.querySelectorAll('.hstar.on').length;
      if (flags || stars) { return; }
      var o = data[code] || {};
      var patch = {};
      if (o.read) { patch.read = true; }
      if (o.watched) { patch.watched = true; }
      if (o.liked) { patch.stars = 5; }
      if (Object.keys(patch).length) { todo.push([code, patch]); }
    });
    function step() {
      if (!todo.length) {
        try { localStorage.setItem('kcl-hub-migrated-' + window.HUBMODULE, '1'); } catch (e) {}
        location.reload();
        return;
      }
      var next = todo.shift();
      post(next[0], next[1], step);
    }
    if (todo.length) { step(); }
    else {
      try { localStorage.setItem('kcl-hub-migrated-' + window.HUBMODULE, '1'); } catch (e) {}
    }
  }
  window.addEventListener('load', migrate);
})();
</script>"""

READINGS_CONTENT_CSS = """
  :root {
    --paper:#F1F4F3; --surface:#FBFCFC; --ink:#1A2830; --ink-soft:#3E535C; --muted:#5F7178;
    --rule:#D8E0DE; --rule-soft:#E6ECEA; --regulated:#1C6D61; --reg-wash:#DDEBE7;
    --broken:#A25E14;
    --display:"Iowan Old Style","Palatino Linotype",Palatino,"Book Antiqua",Georgia,serif;
    --body:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
    --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      --paper:#121A1E; --surface:#182328; --ink:#DFE7E7; --ink-soft:#B4C4C6; --muted:#85989C;
      --rule:#27353B; --rule-soft:#1F2C32; --regulated:#6EC0B0; --reg-wash:#17332F;
      --broken:#DBA463;
    }
  }
  :root[data-theme="dark"] {
    --paper:#121A1E; --surface:#182328; --ink:#DFE7E7; --ink-soft:#B4C4C6; --muted:#85989C;
    --rule:#27353B; --rule-soft:#1F2C32; --regulated:#6EC0B0; --reg-wash:#17332F;
    --broken:#DBA463;
  }
  * { box-sizing:border-box; }
  body { background:var(--paper); color:var(--ink); font-family:var(--body);
         font-size:16.5px; line-height:1.62; margin:0; padding:0 24px 96px;
         -webkit-font-smoothing:antialiased; }
  .wrap { max-width:760px; margin:0 auto; padding-top:34px; }
  .wrap h1 { font-family:var(--display); font-size:1.9rem; font-weight:600;
             margin:0 0 6px; line-height:1.25; }
  .rsub { color:var(--muted); font-size:.93rem; margin:0 0 26px; }
  .wrap a { color:var(--regulated); }
  .r { background:var(--surface); border:1px solid var(--rule); border-radius:12px;
       padding:22px; margin:0 0 16px; }
  .r h2 { font-family:var(--display); font-size:1.15rem; font-weight:600;
          margin:0 0 4px; line-height:1.35; }
  .who { color:var(--muted); font-size:.85rem; margin:0 0 12px; }
  .who .kind { text-transform:uppercase; letter-spacing:.07em; font-size:.72rem;
               border:1px solid var(--rule); border-radius:999px; padding:2px 9px;
               margin-right:8px; }
  .openit { margin:0 0 14px; font-size:.88rem; }
  .openit.none { color:var(--muted); }
  .claim { font-family:var(--display); font-size:1.05rem; line-height:1.55;
           margin:0 0 12px; }
  .why { color:var(--ink-soft); font-size:.9rem; margin:0 0 14px; }
  ul.take { margin:0 0 14px; padding-left:20px; color:var(--ink-soft);
            font-size:.92rem; }
  ul.take li { margin:0 0 6px; }
  details.d { border-top:1px solid var(--rule); }
  details.d > summary { padding:11px 0; cursor:pointer; color:var(--regulated);
                        font-size:.88rem; list-style:none; }
  details.d > summary::-webkit-details-marker { display:none; }
  details.d > summary::before { content:"+ "; font-weight:700; }
  details.d[open] > summary::before { content:"\\2212 "; }
  .dbody { padding:0 0 14px; margin:0; color:var(--ink-soft); font-size:.93rem;
           white-space:pre-wrap; }
  dl.terms { margin:0 0 12px; }
  dl.terms dt { font-weight:600; padding-top:8px; border-top:1px dotted var(--rule); }
  dl.terms dd { margin:2px 0 8px; color:var(--ink-soft); font-size:.9rem; }
  .rel { margin:12px 0 0; font-size:.85rem; color:var(--muted); }
  .rel a { margin-right:10px; }

  /* The list-first shape (EH, 2026-08-23): the page opens as an index, each
     reading opens on its own, #all is the old everything-at-once view. With
     no JavaScript nothing has a state class and everything shows, which is
     the old page, so the fallback is honest. */
  .rcaution { color: var(--broken); font-size: .85em; margin-left: 7px; }
  .rnotice { font: 500 13px/1.5 var(--body); color: var(--broken);
             background: color-mix(in srgb, var(--broken) 9%, transparent);
             border-left: 3px solid var(--broken); border-radius: 0 6px 6px 0;
             padding: 7px 11px; margin: 8px 0 0; }
  .rnotice a { color: var(--broken); }
  .rix { margin: 18px 0 0; }
  .rixw { font: 600 12px var(--body); color: var(--muted);
          text-transform: uppercase; letter-spacing: .06em; margin: 20px 0 8px; }
  .rixrow { display: block; padding: 11px 13px; border: 1px solid var(--rule-soft);
            border-radius: 8px; margin: 0 0 8px; text-decoration: none;
            background: var(--surface); }
  .rixrow:hover { border-color: var(--regulated); }
  .rixtitle { display: block; font: 600 15.5px/1.35 var(--display); color: var(--ink); }
  .rixwho { display: block; font: 500 12px var(--body); color: var(--muted); margin-top: 2px; }
  .rixclaim { display: block; font: 400 13px/1.5 var(--body); color: var(--ink-soft); margin-top: 5px; }
  .rixall { margin: 16px 0 0; font: 500 12.5px var(--body); }
  .rixall a, .rback a { color: var(--regulated); }
  .rback { display: none; font: 500 12.5px var(--body); margin: 14px 0 0; }
  .wrap.rix-list .r { display: none; }
  .wrap.rix-one .r { display: none; }
  .wrap.rix-one .r.on { display: block; }
  .wrap.rix-one .rix, .wrap.rix-all .rix { display: none; }
  .wrap.rix-one .rback, .wrap.rix-all .rback { display: block; }
"""


def mistakes_content(cfg):
    """The course's recorded source defects as a content-format document, or
    None if none are filed. One implementation for the route, the hub's count,
    and verify_notes, same reasoning as readings_content: the numbers the
    browser sees and the numbers the checker sees cannot drift apart."""
    import mistakes as M
    esc = html_mod.escape
    items = M.read_all(cfg["notes_dir"])
    if not items:
        return None
    course = str(cfg.get("module_name") or cfg.get("module_code") or "this course")
    metas = lesson_meta_index(cfg)

    def where_line(rec):
        doc = str(rec.get("doc") or "").strip()
        if not doc:
            return ""
        m = metas.get(doc) or {}
        if m.get("file"):
            return (' <a href="%s">%s</a>'
                    % (esc(m["file"], quote=True),
                       esc(str(m.get("title") or doc))))
        return " " + esc(doc)

    groups = {}
    for rec in items:
        wk = str(rec.get("week") or "").strip()
        groups.setdefault(wk, []).append(rec)

    parts = []
    ordered = ([""] if "" in groups else []) + sorted(
        (k for k in groups if k), key=lambda s: (len(s), s))
    for wk in ordered:
        parts.append('<p class="mkw">%s</p>'
                     % ("The course itself" if not wk else "Week " + esc(wk)))
        for rec in groups[wk]:
            bits = ['<h2>%s</h2>' % esc(str(rec["label"]))]
            bits.append('<p class="who"><span class="kind">%s</span>%s</p>'
                        % (esc(str(rec.get("kind") or "source defect")),
                           where_line(rec)))
            # NOTE-SPEC section C: both sides in their own terms, nobody
            # adjudicates. Rendered structurally so every entry says it the
            # same way (the ingest session's request, 2026-08-24).
            sides = []
            if rec.get("source_says"):
                sides.append('<div class="mside"><span class="mtag">The '
                             'material says</span><p>%s</p></div>'
                             % esc(str(rec["source_says"])))
            if rec.get("paper_says"):
                sides.append('<div class="mside"><span class="mtag">The '
                             'source says</span><p>%s</p></div>'
                             % esc(str(rec["paper_says"])))
            if sides:
                bits.append('<div class="mvs">%s</div>' % "".join(sides))
            if rec.get("text"):
                bits.append('<p class="mtext">%s</p>' % esc(str(rec["text"])))
            refs = rec.get("refs") or []
            if refs:
                links = ", ".join('<a href="https://doi.org/%s" target="_blank" '
                                  'rel="noopener">%s</a>'
                                  % (esc(d, quote=True), esc(d)) for d in refs)
                bits.append('<p class="mrefs">Check it: %s</p>' % links)
            parts.append('<div class="mk" id="mk-%s">%s</div>'
                         % (esc(str(rec["id"]), quote=True), "".join(bits)))

    n = len(items)
    meta = {"doc": "MISTAKES", "title": "Mistakes found in this course",
            "week": "", "topicNo": "", "topic": "Mistakes found",
            "weekTitle": "", "part": ""}
    return (
        "<!-- study-lesson:v1 -->\n"
        "<title>Mistakes found: %s</title>\n"
        '<script type="application/json" id="lesson-meta">\n%s\n</script>\n'
        "<style>\n%s</style>\n"
        '<div class="wrap">\n'
        '<h1>Mistakes found in this course</h1>\n'
        '<p class="msub">%d defect%s in the course&rsquo;s own material, found '
        "while these lessons were built and checked against the sources. "
        "Nothing was silently corrected: each entry gives both sides, and the "
        "lessons transmit what the source supports.</p>\n%s\n</div>\n"
        % (esc(course), json.dumps(meta, indent=2), MISTAKES_CONTENT_CSS,
           n, "" if n == 1 else "s", "\n".join(parts)))


# The mistakes page's own stylesheet. Shares the reading page's tokens and card
# look so the two course-level pages read as one product; the two-sided box is
# its own shape because nothing else renders an argument between two sources.
MISTAKES_CONTENT_CSS = READINGS_CONTENT_CSS + """
  .msub { color:var(--muted); font-size:.93rem; margin:0 0 26px; }
  .mkw { font: 600 12px var(--body); color: var(--muted);
         text-transform: uppercase; letter-spacing: .06em; margin: 26px 0 10px; }
  .mk { background:var(--surface); border:1px solid var(--rule);
        border-radius:12px; padding:22px; margin:0 0 16px; }
  .mk h2 { font-family:var(--display); font-size:1.12rem; font-weight:600;
           margin:0 0 4px; line-height:1.35; }
  .mvs { display:flex; gap:12px; flex-wrap:wrap; margin:0 0 12px; }
  .mside { flex:1 1 240px; border-left:3px solid var(--rule);
           background:var(--rule-soft); border-radius:0 8px 8px 0;
           padding:10px 13px; }
  .mside:last-child { border-left-color:var(--regulated); }
  .mtag { display:block; font: 600 11px var(--body); color:var(--muted);
          text-transform:uppercase; letter-spacing:.06em; margin:0 0 4px; }
  .mside p { margin:0; font-size:.92rem; color:var(--ink-soft); }
  .mtext { color:var(--ink-soft); font-size:.93rem; margin:0 0 12px; }
  .mrefs { margin:0; font-size:.85rem; color:var(--muted); }
"""


# --------------------------------------------------------------------------
# Core readings: skim first, open what you need
# --------------------------------------------------------------------------
#
# EH, 2026-08-22: "a lot of these courses have core readings... the core idea
# would be to generate summaries as well as provide links to the actual core
# readings. The summaries should have the ability to have sections that can be
# opened up to go into more detail."
#
# 🔴 The whole design is in what is OPEN when the page loads. A reading list
# where everything is expanded is a wall nobody reads, and one where everything
# is collapsed is a bibliography, which they already had. So: the claim and the
# three takeaways are always visible, and everything under them folds. You can
# read the whole list in a minute and still be one tap from the method section.
#
# The link to the actual reading sits at the TOP of each card, not the bottom.
# The summary is a way in, never a replacement, and a summary that buries the
# paper is quietly encouraging somebody not to read it.
READINGS_PAGE = """<!-- study-readings -->
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
%(icons)s<title>%(tab)s</title>
<style>
  :root {
    --paper:#F1F4F3; --surface:#FBFCFC; --ink:#1A2830; --ink-soft:#3E535C; --muted:#5F7178;
    --rule:#D8E0DE; --accent:#1C6D61; --accent-wash:#DDEBE7; --broken:#A25E14;
    --display:"Iowan Old Style","Palatino Linotype",Palatino,"Book Antiqua",Georgia,serif;
    --text:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      --paper:#12191C; --surface:#182126; --ink:#E7EEEC; --ink-soft:#B7C6C4; --muted:#8AA0A0;
      --rule:#26343A; --accent:#5FBFAE; --accent-wash:#173029; --broken:#D69A55;
    }
  }
  :root[data-theme="dark"] {
    --paper:#12191C; --surface:#182126; --ink:#E7EEEC; --ink-soft:#B7C6C4; --muted:#8AA0A0;
    --rule:#26343A; --accent:#5FBFAE; --accent-wash:#173029; --broken:#D69A55;
  }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--paper); color:var(--ink); font-family:var(--text);
         line-height:1.6; -webkit-text-size-adjust:100%%; }
  .wrap { max-width:760px; margin:0 auto; padding:40px 20px 80px; }
  a { color:var(--accent); }
  .back { display:inline-block; margin-bottom:20px; color:var(--accent);
          text-decoration:none; font-size:.9rem; }
  h1 { font-family:var(--display); font-size:1.9rem; font-weight:600; margin:0 0 6px; }
  .sub { color:var(--muted); margin:0 0 30px; font-size:.95rem; }
  .r { background:var(--surface); border:1px solid var(--rule); border-radius:12px;
       padding:22px; margin:0 0 16px; }
  .r > h2 { font-family:var(--display); font-size:1.15rem; font-weight:600;
            margin:0 0 4px; line-height:1.35; }
  .who { color:var(--muted); font-size:.85rem; margin:0 0 12px; }
  .who .kind { text-transform:uppercase; letter-spacing:.07em; font-size:.72rem;
               border:1px solid var(--rule); border-radius:999px; padding:2px 9px;
               margin-right:8px; }
  .open { display:inline-block; margin:0 0 14px; font-size:.88rem; }
  .open.none { color:var(--muted); }
  .claim { font-family:var(--display); font-size:1.05rem; line-height:1.55;
           margin:0 0 12px; }
  .why { color:var(--ink-soft); font-size:.9rem; margin:0 0 14px; }
  ul.take { margin:0 0 14px; padding-left:20px; color:var(--ink-soft); font-size:.92rem; }
  ul.take li { margin:0 0 6px; }
  details.d { border-top:1px solid var(--rule); }
  details.d > summary { padding:11px 0; cursor:pointer; color:var(--accent);
                        font-size:.88rem; list-style:none; }
  details.d > summary::-webkit-details-marker { display:none; }
  details.d > summary::before { content:"+ "; font-weight:700; }
  details.d[open] > summary::before { content:"\u2212 "; }
  details.d .body { padding:0 0 14px; color:var(--ink-soft); font-size:.93rem;
                    white-space:pre-wrap; }
  .terms { margin:0; padding:0; list-style:none; }
  .terms li { padding:8px 0; border-top:1px dotted var(--rule); font-size:.9rem; }
  .terms b { color:var(--ink); }
  .rel { margin:12px 0 0; font-size:.85rem; color:var(--muted); }
  .rel a { margin-right:10px; }
  .empty { background:var(--surface); border:1px dashed var(--rule); border-radius:12px;
           padding:28px; }
  .empty h2 { font-family:var(--display); margin:0 0 8px; font-weight:600; }
  .empty p { margin:0 0 10px; color:var(--ink-soft); max-width:60ch; }
  code { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:.85em;
         background:var(--accent-wash); padding:1px 5px; border-radius:4px; }
</style>
<div class="wrap">
  %(navbar)s
  <h1>Core readings</h1>
  <p class="sub">%(sub)s</p>
  %(body)s
</div>
"""


# --------------------------------------------------------------------------
# The instructions, written for somebody who has never opened a terminal
# --------------------------------------------------------------------------
#
# 🔴 EH, 2026-08-22, and he is right twice over:
#
#   "Did you just say to tell Claude 'download my course from KEATS' or 'write
#   lessons from these'? I don't think that's enough information to give Claude."
#
#   "Most users don't understand what it means to open a folder in a Claude Code
#   session, so we're going to need to do more hand holding than that. I want
#   this to be workable for non-technical classmates."
#
# The instructions existed the whole time, in `.claude/skills/`, and nothing in
# the product had ever pointed at them. A skill is loaded automatically when a
# session is opened on this folder, so the SHORT prompt is enough IF they got
# the folder right, and getting the folder right is the entire difficulty. So
# this page does three things a link to a skill cannot:
#
#   1. **It prints the exact folder to choose**, with a copy button, because
#      this server knows where it is and they do not.
#   2. **It gives the prompt to paste**, so there is nothing to compose.
#   3. **It offers the full instructions as a file**, for the case where their
#      Claude has no skills loaded and needs to be handed the whole thing.
#
# Written at the reading level of somebody who has been sent a link by a
# coursemate. No jargon that is not immediately unpacked, and no step that
# assumes they know what a folder picker is for.
# The three prompts, written to be pasted by somebody who will not edit them.
#
# 🔴 Each one NAMES ITS SKILL and then repeats the essentials in plain words. The
# naming is what makes it short when the session is opened on this folder, where
# the skill loads itself. The repetition is what makes it survive being pasted
# into a session that has no skills at all, which is what happens the first time
# somebody tries this on a phone or in a browser tab. A prompt that only works
# in the lucky case is a prompt that teaches people this does not work.
#
# %%(course)s is the course code, filled in per page, so a person who has three
# courses cannot paste the wrong one.
PROMPT_KEATS = """Use the download-keats skill.

I want my KEATS module collected for the course "%(course)s" in my study reader.
The module page is: <paste the KEATS address of your module here>

Please:
1. Open it in Chrome and let me sign in myself. Never ask me for my password.
2. Show me the list of what you found BEFORE downloading anything.
3. Save the slides and transcripts, keeping the course's own numbering.
4. Do not download the lecture videos. Collect their links and Kaltura entry ids
   into a file called %(course)s-video-links.json, in the shape the video-links
   skill describes.
5. Write the materials table into the course folder for %(course)s.
6. If the module has a reading list, get the core readings too, per the
   download-readings skill: download the PDFs my enrolment serves, run them
   through the pdf-fix step, then summarise them with the core-readings skill.
7. Tell me at the end how many lectures will play in the reader, how many
   readings came down, and name anything you skipped."""

PROMPT_WRITE = """Use the write-lesson skill, and read NOTE-SPEC.md beside it before writing.

I have a folder of lecture slides and transcripts. Please turn them into lessons
for the course "%(course)s" in my study reader.

The folder is: <drag the folder in, or paste its path here>

Please:
1. Tell me what you found in there and how you plan to split it into lessons,
   before you write any of them.
2. Follow the house standard exactly: every lesson readable on its own, no
   mention of slides or lecturers or transcripts, and every citation a DOI you
   have actually checked.
3. Run the verifier when you are done and show me the result.
4. Tell me if any lecture had no transcript, rather than writing round it."""

PROMPT_READINGS = """Use the core-readings skill.

I want the core readings for the course "%(course)s" in my study reader
summarised, so I can skim them and open the ones I need in detail.

The readings are in: <drag the folder in, or paste its path here>

Please:
1. Tell me what you found in there before you summarise anything.
2. Actually read each one, not just its abstract or its first page.
3. For each: one sentence saying what it CLAIMS, one saying why it is set on this
   course, and three takeaways that make sense on their own.
4. Then the detail underneath: what they did, what they found with the actual
   numbers, and what to be careful about. Never leave out the last one.
5. Check every DOI resolves. If one will not, leave it out and tell me, rather
   than guessing.
6. Tell me which ones you could not open, and never summarise from a title alone.
7. Write it to %(course)s-readings.json and show me the list."""

# Appendable forms of the follow-on jobs, for the wizard's one-prompt
# composition (EH, 2026-08-22: "There's got to be a workflow where I can just
# paste one prompt to Claude and just have it do what I need it to do in my
# particular situation"). Each reads as a numbered step after another route's
# prompt; the standalone PROMPT_* forms remain for the routes chosen directly.
ADD_VIDEOS = ("Collect the lecture videos so they play here, per the "
              "video-links skill: find each lecture's video on my course site "
              "(I sign in myself), collect the links and entry ids into "
              "%(course)s-video-links.json, download nothing, and import that "
              "file into the course.")
ADD_READINGS = ("Get the core readings, per the download-readings skill: if I "
                "hand you a folder of PDFs, use those; otherwise find the "
                "module's reading list on my course site and download what my "
                "own login serves. Run every PDF through the pdf-fix step, "
                "then summarise them with the core-readings skill, keeping "
                "each reading's week.")

# The wizard's one-prompt composition (EH, 2026-08-22, third shape in a day
# and this one is his design: "something which lets someone choose what they
# want included and whether they have those things downloaded or not"). Each
# fragment is one job, readable as a numbered step; the page composes header
# plus the ticked jobs into a single prompt.
WIZ_HEADER = ('I am setting up the course "%(course)s" in Study Hub, my study '
              'reader. Work through these jobs in order, in this one session. '
              'After each job, tell me what you actually did, and never report '
              'a step you skipped as done.')
WIZ_LESSONS_SITE = ('Get the course material and write the lessons: use the '
                    'download-keats skill (KEATS, Moodle, Canvas and similar '
                    'all work). My course page is: <paste the course address '
                    'here>. I sign in myself; show me what you found before '
                    'downloading anything; save the slides and transcripts '
                    "with the course's own numbering; write the materials "
                    'table; then write the lessons with the write-lesson '
                    'skill, reading NOTE-SPEC.md beside it first, every '
                    'citation a DOI you have checked.')
WIZ_LESSONS_LOCAL = ('Write the lessons from my downloaded folder of slides '
                     'and transcripts: use the write-lesson skill, reading '
                     'NOTE-SPEC.md beside it first. The folder is: <paste the '
                     'slides folder here>. Tell me how you plan to split it '
                     'into lessons before you write any; every lesson must '
                     'read on its own; every citation a DOI you have checked; '
                     'run the verifier at the end and show me the result.')
# 🔴 Item 10 of the course-ingest retrospective, approved by EH 2026-08-23: say
# that a course may mix delivery formats within a single week, because one of
# his does. It serves 9 lectures as Kaltura recordings and 29 as HTML packages,
# sometimes inside the same week, and its Media Gallery reports ZERO media, so a
# session that checks the gallery concludes there are no videos and is wrong about
# all 38. The prompt has to say this: a session cannot infer it from the course.
WIZ_VIDEOS = ('Wire up the lecture videos so they play beside the lessons: '
              'use the video-links skill. Find each lecture\'s video on my '
              'course site (I sign in myself), collect the links and entry '
              'ids into %(course)s-video-links.json, and import that file '
              'into the course. Expect the course to mix formats: some '
              'lectures are recordings and some are narrated slide packages '
              'with no video at all, sometimes within the same week. Work '
              'from the actual activity list rather than a media gallery, '
              'which can report nothing while the lectures are all there.')

# The two download options under the videos piece, appended to the job when
# ticked. EH's design, 2026-08-23: downloading media is an explicit choice,
# two checkboxes pre-ticked once the person opts in, each saying plainly what
# it buys. Recordings stream without being downloaded; packages cannot play
# in the reader at all unless mirrored.
# The base for a course whose links are ALREADY in: ticking videos then is
# about the downloads, not about collecting again (EH, 2026-08-23: "I also
# want to make sure we have a way to download videos or any content for any
# course that we already have"). Course setup is that way: it stays reachable
# after creation, and the checklist composes from what the course has.
WIZ_VIDEOS_HAVE = ('The video links are already imported for this course; '
                   'check them only if a lecture is missing.')

WIZ_VIDEOS_PACKS = ('Then mirror every lecture that is a narrated slide '
                    'package rather than a recording: use the mirror-packages '
                    'skill, so those parts play inside the reader with no '
                    'course-site login.')
WIZ_VIDEOS_FILES = ('Then download a copy of each real recording with '
                    'server/fetch_videos.py, so the videos also play from '
                    'this machine, offline, even if the course site closes.')
# The consolidated PDFs (EH's design, 2026-08-23): weekly and whole-course PDFs
# for slides and transcripts, built AFTER pdf_fix so they inherit clean
# orientation and OCR. Re-askable later like every piece, which is what "joins
# that checklist" means.
# 🔴 They were "an option, never a default" until 2026-09-08. They are now
# DEFAULT ON for a course that has none: see the reset in WIZARD_PAGE for the
# ruling and the reason.
WIZ_CONSOL = ('Build the consolidated PDFs: after the slides and transcripts '
              'are downloaded and have been through the pdf-fix step, run '
              'server/consolidate_pdfs.py on their folder with --module '
              '%(course)s. It writes one PDF per week and one for the whole '
              'course, for the slide decks, the transcripts and the handouts, '
              "into the course's consolidated folder, and verifies every page "
              'count. Show me its output.')

# 🔴 EH widened the captions entry 2026-09-08 from "a button in settings" to
# "a checkbox in the wizard when you load a new course which should, in turn,
# call the right scripts to be run". So this fragment NAMES the script and its
# arguments rather than gesturing at "generate captions": a session told to
# generate captions will invent a way, and the two pipelines underneath are
# exactly what `caption_course.py` exists to hide.
#
# ⚠️ It says the cost too. The label says it in the wizard, and this says it in
# the prompt, because the person who runs the prompt may not be the person who
# ticked the box.
WIZ_CAPTIONS = ('Build the captions for this course: run '
                'server/caption_course.py --build %(course)s from the repository '
                'root. It captions narrated slide packages and plain recordings '
                'alike, skips any lecture that already has them, and writes '
                "into the course's own captions folder. Do not write a caption "
                'file by hand and do not summarise the transcript: the words '
                'must be the lecturer\'s own, timed against the recording. It '
                'fetches one lecture at a time and pauses between, so a whole '
                'course runs for tens of minutes; show me its report when it '
                'finishes. If it says the caption engine is not installed, run '
                'server/caption_course.py --install first: a one-time download '
                'of a few hundred MB, and it prints the size before it starts; '
                'then build.')

# The picture pack is not course work and the verb is not a build: one fetch,
# checked against the feed, into the kit's own folder. Named exactly, because
# a session told to "download the brain pictures" will find some other way.
# 🟢 It says the review line's meaning up front, so the sentence the verb
# prints does not surprise the person reading the session's output.
WIZ_PACK = ('Install the brain-region pictures: run '
            'python3 server/regionpack.py --install from the repository root. '
            'One download of about 44 MB, shared by every course on this '
            'machine and kept across upgrades; it prints the size, checks the '
            'download against the feed, and needs no restart. It also prints a '
            'line saying the plates have not had a formal domain review, which '
            'is a fact about the pack and not an error: show me that line. Do '
            'not fetch pictures from anywhere else.')

WIZ_READINGS_LOCAL = ('Summarise the core readings I already have downloaded: '
                      'the folder is: <paste the readings folder here>. Run '
                      'each PDF through the pdf-fix step first, then use the '
                      'core-readings skill: read each one properly, say what '
                      'it claims and why it is set, three takeaways, the '
                      'detail underneath, every DOI checked, and keep each '
                      'reading\'s week.')
WIZ_READINGS_SITE = ('Get the core readings from my course site: use the '
                     'download-readings skill. Find the reading list (I sign '
                     'in myself), download the PDFs my own enrolment serves, '
                     'file them in this course\'s readings folder, run them '
                     'through the pdf-fix step, then summarise them with the '
                     'core-readings skill, keeping each reading\'s week.')

PROMPT_READINGS_FETCH = """Use the download-readings skill.

I want the core readings for the course "%(course)s" in my study reader, and I
do not have the PDFs yet. My course site (KEATS, Moodle, Canvas) should carry
the module's reading list.

Please:
1. Find the reading list on the course site. I will sign in myself if it asks.
2. Show me what you found, week by week, before downloading anything.
3. Download the PDFs my own enrolment serves, one at a time, and file them in
   this course's readings folder, named "W<n> - Author (Year).pdf".
4. Anything you cannot download, record as a checked DOI link and tell me why.
5. Run every downloaded PDF through the pdf-fix step.
6. Then use the core-readings skill to summarise them, keeping each one's week.
"""

PROMPT_VIDEOS = """Use the video-links skill.

I already have the material for the course "%(course)s" in my study reader, but
the lecture recordings do not play. Please collect their links.
The module page is: <paste the KEATS address of your module here>

Please:
1. Open it in Chrome and let me sign in myself. Never ask me for my password.
2. Do not download any video.
3. For each lecture find its Kaltura entry id, the 1_ followed by eight
   characters. That is the field that decides whether it plays.
4. Use the same lesson numbering the course already uses.
5. Write it all to %(course)s-video-links.json and show me the list.
6. Tell me which lectures you could not find an id for."""


HELP_PAGE = """<!-- study-help -->
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
%(icons)s<title>How to get your lectures in</title>
<style>
  :root {
    --paper:#F1F4F3; --surface:#FBFCFC; --ink:#1A2830; --ink-soft:#3E535C; --muted:#5F7178;
    --rule:#D8E0DE; --accent:#1C6D61; --accent-wash:#DDEBE7; --broken:#A25E14;
    --display:"Iowan Old Style","Palatino Linotype",Palatino,"Book Antiqua",Georgia,serif;
    --text:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      --paper:#12191C; --surface:#182126; --ink:#E7EEEC; --ink-soft:#B7C6C4; --muted:#8AA0A0;
      --rule:#26343A; --accent:#5FBFAE; --accent-wash:#173029; --broken:#D69A55;
    }
  }
  :root[data-theme="dark"] {
    --paper:#12191C; --surface:#182126; --ink:#E7EEEC; --ink-soft:#B7C6C4; --muted:#8AA0A0;
    --rule:#26343A; --accent:#5FBFAE; --accent-wash:#173029; --broken:#D69A55;
  }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--paper); color:var(--ink); font-family:var(--text);
         line-height:1.6; -webkit-text-size-adjust:100%%; }
  .wrap { max-width:680px; margin:0 auto; padding:40px 20px 80px; }
  .back { display:inline-block; margin-bottom:22px; color:var(--accent);
          text-decoration:none; font-size:.9rem; }
  a { color:var(--accent); }
  h1 { font-family:var(--display); font-size:1.8rem; font-weight:600; margin:0 0 10px;
       line-height:1.2; }
  .sub { color:var(--ink-soft); margin:0 0 34px; }
  details.sec { background:var(--surface); border:1px solid var(--rule);
                border-radius:12px; margin:0 0 12px; overflow:hidden; }
  details.sec > summary { padding:18px 20px; cursor:pointer; list-style:none;
                          display:block; }
  details.sec > summary::-webkit-details-marker { display:none; }
  details.sec > summary b { font-family:var(--display); font-size:1.1rem;
                            font-weight:600; display:block; }
  details.sec > summary span { display:block; margin-top:4px; color:var(--muted);
                               font-size:.86rem; }
  details.sec > summary:hover b { color:var(--accent); }
  details.sec > summary:focus-visible { outline:2px solid var(--accent);
                                        outline-offset:-3px; }
  /* The twisty is drawn rather than relying on the default marker, which the
     list-style:none above removes so the whole row can be the target. */
  details.sec > summary { position:relative; padding-right:46px; }
  details.sec > summary::after { content:""; position:absolute; right:22px; top:24px;
                                 width:8px; height:8px; border-right:2px solid var(--muted);
                                 border-bottom:2px solid var(--muted);
                                 transform:rotate(45deg); transition:transform .15s; }
  details.sec[open] > summary::after { transform:rotate(-135deg); top:28px; }
  details.sec[open] > summary { border-bottom:1px solid var(--rule); }
  details.sec .in { padding:20px; }
  .askwhat { margin:26px 0 12px; color:var(--ink-soft); font-size:.95rem; }
  .askwhat b { color:var(--ink); }
  details.sec .in > p:first-child { margin-top:0; }
  ol { margin:0; padding-left:22px; }
  ol > li { margin:0 0 16px; }
  ol > li > b { color:var(--ink); }
  p { margin:6px 0 0; color:var(--ink-soft); }
  .pathbox { display:flex; gap:8px; align-items:center; flex-wrap:wrap; margin:10px 0 0; }
  .pathbox code { flex:1 1 240px; min-width:0; overflow-x:auto; white-space:nowrap;
                  padding:9px 11px; border:1px solid var(--rule); border-radius:8px;
                  background:var(--paper); font-size:.78rem; }
  button.copy { font:inherit; font-size:.85rem; padding:9px 15px; border:0;
                border-radius:8px; background:var(--accent); color:var(--paper); cursor:pointer; }
  button.copy.done { background:var(--muted); }
  .prompt { margin:12px 0 0; padding:14px; border:1px solid var(--rule);
            border-radius:9px; background:var(--paper); font-size:.84rem;
            white-space:pre-wrap; font-family:ui-monospace,SFMono-Regular,Menlo,monospace; }
  .row { display:flex; gap:10px; flex-wrap:wrap; margin:12px 0 0; align-items:center; }
  a.file { color:var(--accent); font-size:.85rem; }
  code { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:.85em;
         background:var(--accent-wash); padding:1px 5px; border-radius:4px; }
  .note { border-left:3px solid var(--broken); padding-left:14px; margin:16px 0 0;
          color:var(--ink-soft); font-size:.9rem; }
  .note b { color:var(--ink); }
</style>
<div class="wrap">
  %(navbar)s
  <h1>Getting your lectures in</h1>
  <p class="sub">Set Claude up once, then open the one section that matches what
     you already have. Nothing here needs a terminal, and nothing here asks you
     for a password.</p>

  <details class="sec" data-k="install">
    <summary><b>Do you have Claude installed?</b>
      <span>Skip this if you already use it. One-off.</span></summary>
    <div class="in">
      <ol>
        <li><b>Get it</b> from
          <a href="https://claude.ai/download" target="_blank" rel="noopener">claude.ai/download</a>
          and sign in. It is free to start.</li>
        <li><b>You want the Code part, not an ordinary chat.</b> A normal chat
          cannot open files on your computer. A Code session can, and that is the
          whole difference between the two.</li>
      </ol>
    </div>
  </details>

  <details class="sec" data-k="folder">
    <summary><b>Point Claude at Study Hub</b>
      <span>Do this once. It is the only fiddly step, and everything else is
        pasting.</span></summary>
    <div class="in">
      <p>Start a <b>Code</b> session. It will ask which folder to work in.
         Choose the folder Study Hub is installed in:</p>
      <div class="pathbox">
        <code id="kitpath">%(kit)s</code>
        <button type="button" class="copy" data-copy="kitpath">Copy</button>
      </div>
      <p>If it asks you to type the folder rather than pick it, paste that.</p>
      <div class="note"><b>The instructions are already in there, and there is
        nothing to install.</b> A Code session opened on this folder finds them by
        itself: ask it <i>"what skills can you see?"</i> and it should answer
        <code>download-keats</code>, <code>setup</code>, <code>video-links</code>
        and <code>write-lesson</code>. That is why the prompts below are short.
        <b>If it cannot see them, you have the wrong folder</b>, and the fix is to
        start again with the path above rather than to install anything.</div>
    </div>
  </details>

  <p class="askwhat">Now pick the one that matches <b>what you already have</b>.</p>

  <details class="sec" data-k="have-nothing">
    <summary><b>I have nothing yet, and my course is online</b>
      <span>KEATS, Moodle, Canvas, Blackboard: anything you sign in to.</span></summary>
    <div class="in">
      <p><b>This gets everything in one go.</b> It downloads all the slides and
         transcripts for the course, and collects a list of the lecture videos so
         they play right here in Study Hub. Then it writes the lessons.</p>
      <p>Claude opens Chrome and <b>you</b> sign in yourself. It shows you a list
         of what it found before it downloads anything.</p>
      <div class="prompt" id="p1">%(prompt1)s</div>
      <div class="row">
        <button type="button" class="copy" data-copy="p1">Copy the prompt</button>
        <a class="file" href="/help/download-keats.md" target="_blank" rel="noopener">Read the full instructions</a>
      </div>
      <div class="note"><b>It will stop and wait for you</b> at the login, and at
        anything else that checks you are a person. That is deliberate: it never
        types a password and never gets past a check on your behalf.</div>
    </div>
  </details>

  <details class="sec" data-k="have-files">
    <summary><b>I have the slides and transcripts already</b>
      <span>A folder of files, from anywhere. It does not have to be KEATS.</span></summary>
    <div class="in">
      <p>A folder from a coursemate, your own downloads, or a shared drive.
         Claude reads what is in it and writes the lessons.</p>
      <p><b>It does not care where the files came from.</b> Slides, transcripts,
         papers, book chapters, lecture notes: if it is a readable file in a
         folder, it can be turned into lessons. Nothing here is tied to any one
         university's system.</p>
      <div class="prompt" id="p2">%(prompt2)s</div>
      <div class="row">
        <button type="button" class="copy" data-copy="p2">Copy the prompt</button>
        <a class="file" href="/help/write-lesson.md" target="_blank" rel="noopener">Read the full instructions</a>
        <a class="file" href="/help/note-spec.md" target="_blank" rel="noopener">and the writing standard</a>
      </div>
      <div class="note">🔴 <b>A folder of slides has no lecture recordings in it,
        and it never will.</b> They are streamed by your institution, not files
        anybody can hand you. So when this finishes, do the next one as well.</div>
    </div>
  </details>

  <details class="sec" data-k="have-lessons">
    <summary><b>I have the lessons, but the videos will not play</b>
      <span>The commonest gap. Nothing is downloaded.</span></summary>
    <div class="in">
      <p>This is what is missing whenever somebody hands you material, or when
         you have written lessons from a folder. It collects the addresses of the
         recordings into one small file, which you then drop onto the course
         page.</p>
      <div class="prompt" id="p3">%(prompt3)s</div>
      <div class="row">
        <button type="button" class="copy" data-copy="p3">Copy the prompt</button>
        <a class="file" href="/help/video-links.md" target="_blank" rel="noopener">Read the full instructions</a>
      </div>
      <div class="note"><b>When it hands you the file</b>, come back to the course
        page and drag it anywhere on the page. You do not need to put it in a
        folder or give it a particular name.</div>
    </div>
  </details>

  <details class="sec" data-k="readings">
    <summary><b>My course has core readings</b>
      <span>Papers and chapters. Skim them all in a minute.</span></summary>
    <div class="in">
      <p>Most courses set papers or book chapters. This reads them and writes a
         summary of each: <b>one sentence saying what it claims</b>, why it is on
         your course, and three takeaways, with the method, the numbers and the
         caveats folded underneath for when you need them. Every one links
         straight to the reading itself.</p>
      <div class="prompt" id="p4">%(prompt4)s</div>
      <div class="row">
        <button type="button" class="copy" data-copy="p4">Copy the prompt</button>
        <a class="file" href="/help/core-readings.md" target="_blank" rel="noopener">Read the full instructions</a>
      </div>
      <div class="note"><b>It is a way in, not a replacement.</b> The summary is
        there so you can tell in a minute which two you have to read properly this
        week, and follow the lecture on the other nine.</div>
    </div>
  </details>

  <details class="sec" data-k="have-lesson-file">
    <summary><b>Somebody sent me a lesson</b>
      <span>One file. No Claude needed for this one.</span></summary>
    <div class="in">
      <p>Go to the course page and <b>drag the file onto it</b>, or use the
         button there. It goes into the right folder with its links, and you do
         not have to rename anything.</p>
      <p>A lesson keeps its links to the lecture and the slides, so those still
         work if you are enrolled on that course. It never carries the sender's
         highlights or notes.</p>
    </div>
  </details>

  <details class="sec" data-k="files">
    <summary><b>Handing the instructions over yourself</b>
      <span>For a Claude that is not looking at this folder.</span></summary>
    <div class="in">
      <p>Every <b>Read the full instructions</b> link above opens the real
         instruction file in a tab, so you can select all and paste it into any
         Claude, anywhere, even one that has never seen this folder.</p>
      <div class="note"><b>Why they open instead of downloading.</b> This reader
        runs over plain <code>http</code> on your own network, and Chrome refuses
        downloads that start from a page like that: you get a stray
        <code>.crdownload</code> and "Insecure download blocked". Nothing this
        server sends can change that, because the block is about how the page is
        served, not about the file. Opening the file in a tab is not a download,
        so it always works.</div>
      <p>The files are also on this machine, if you want them as files:</p>
      <div class="pathbox">
        <code id="skillpath">%(skills)s</code>
        <button type="button" class="copy" data-copy="skillpath">Copy</button>
      </div>
    </div>
  </details>

  <details class="sec" data-k="trouble">
    <summary><b>If something goes wrong</b>
      <span>Five things that actually happen.</span></summary>
    <div class="in">
      <ol>
        <li><b>Claude cannot see the skills.</b> You are almost certainly in the
          wrong folder. Copy the path from "Point Claude at this folder" and start
          the session again. Nothing needs installing.</li>
        <li><b>Claude says it cannot find the folder.</b> Paste the path from
          "Point Claude at Study Hub" rather than typing it. A space in a folder
          name is the usual culprit.</li>
        <li><b>It asks you for your university password.</b> Do not give it one.
          It should be asking you to sign in yourself, in the browser window it
          opened. If it asks in the chat, tell it no and give it the full
          instructions instead.</li>
        <li><b>The lessons appeared but the videos do not play.</b> That is
          "I have the lessons, but the videos will not play", and it is the
          commonest gap by a distance.</li>
        <li><b>Nothing at all is happening.</b> Ask for help in plain words. "The
          reader will not open" is enough to go on.</li>
      </ol>
    </div>
  </details>
</div>
<script>
(function () {
  /* Which sections were open, remembered per device. Somebody working through
     Route 3 should not have to reopen it every time they come back to check the
     next step, and a page of seven closed boxes is only friendly the first time.
     Wrapped because localStorage throws outright in some privacy modes. */
  var KEY = 'kcl-help-open';
  var open = {};
  try { open = JSON.parse(window.localStorage.getItem(KEY) || '{}') || {}; } catch (e) {}
  document.querySelectorAll('details.sec').forEach(function (d) {
    var k = d.getAttribute('data-k');
    if (open[k]) { d.open = true; }
    d.addEventListener('toggle', function () {
      open[k] = d.open;
      try { window.localStorage.setItem(KEY, JSON.stringify(open)); } catch (e) {}
    });
  });

  document.querySelectorAll('button.copy').forEach(function (b) {
    b.addEventListener('click', function () {
      var el = document.getElementById(b.getAttribute('data-copy'));
      if (!el) return;
      var text = el.textContent;
      var was = b.textContent;
      var flash = function (word) {
        b.textContent = word;
        b.classList.add('done');
        setTimeout(function () { b.textContent = was; b.classList.remove('done'); }, 2400);
      };
      var done = function () { flash('Copied'); };
      var failed = function () { flash('Select and copy it'); };
      /* navigator.clipboard needs a secure context, and this server is plain
         http on a private address, so it is often simply absent. The old
         execCommand path is not a nicety here, it is the one that runs. */
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(done, function () {
          fallback(text, el, done, failed);
        });
      } else {
        fallback(text, el, done, failed);
      }
    });
  });
  function fallback(text, el, done, failed) {
    var ta = document.createElement('textarea');
    ta.value = text;
    ta.setAttribute('readonly', '');
    ta.style.cssText = 'position:fixed;top:0;left:0;opacity:0';
    document.body.appendChild(ta);
    ta.select();
    ta.setSelectionRange(0, text.length);
    /* 🔴 execCommand REPORTS failure by returning false, without throwing, and
       the first version of this called done() regardless. A button that says
       "Copied" when nothing was copied sends somebody to paste an empty
       clipboard into Claude and wonder what they did wrong. It also needs a real
       user gesture, so it works on a click and never from a script: verified
       2026-08-22 on this page, where navigator.clipboard is absent entirely
       because plain http on a private address is not a secure context. */
    var ok = false;
    try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
    ta.remove();
    if (ok) { done(); return; }
    /* Nothing copied, so leave the text selected on the page for them to copy
       by hand and say so, rather than pretending. */
    failed();
    try {
      var range = document.createRange();
      range.selectNodeContents(el);
      var sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
    } catch (e) {}
  }
}());
</script>
"""


# --------------------------------------------------------------------------
# The three ways lessons get into a course
# --------------------------------------------------------------------------
#
# 🔴 Reported 2026-08-21: "you just end up in this blank screen, which tells you
# to either import a lesson sent to you or ask Claude to write one from a deck
# and transcript. There are no instructions for how to import the lesson someone
# sent you. There's nothing that tells you to put it somewhere. There's no folder
# selection process, and there are no instructions."
#
# All three routes existed. None of them was reachable from the product: the
# import was a command line, and the other two were skills you had to know the
# name of. A route nobody can find is a route nobody has. So this block puts the
# import where the dead end was, as a file picker and a drop target, and prints
# the exact sentence to say for the other two rather than gesturing at them.
#
# %(module)s is the course this is for. The block is shown open on a course with
# no lessons, and folded on one that has some, because it stops being the point
# of the page the moment there is something to read.
IMPORT_BLOCK = """
<!-- 🔴 Self-contained on purpose: its own CSS, its own class names, and a
     fallback on every custom property. It is appended to hubs it knows nothing
     about, including hand-written ones using a different palette (this module's
     own hub calls its accent --regulated, not --accent), so a block that
     inherited its colours would render invisible text on one of them. -->
<style>
  .sv-addfold { margin:22px 0 0; background:var(--surface,#FBFCFC);
                border:1px solid var(--rule,#D8E0DE); border-radius:12px; }
  .sv-addfold > summary { padding:16px 18px; cursor:pointer; font-size:.9rem;
                          color:var(--accent,var(--regulated,#1C6D61)); }
  .sv-addfold[open] > summary { border-bottom:1px solid var(--rule,#D8E0DE); }
  .sv-add { padding:18px; color:var(--ink,#1A2830);
            font-family:var(--text,-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif); }
  .sv-addfold .sv-add { padding-top:16px; }
  .sv-add > h2 { font-family:var(--display,Georgia,serif); font-size:1.05rem;
                 font-weight:600; margin:0 0 10px; color:var(--ink,#1A2830); }
  .sv-add h3 { font-size:.76rem; font-weight:700; letter-spacing:.08em;
               text-transform:uppercase; color:var(--muted,#5F7178);
               margin:22px 0 8px; }
  .sv-add h3:first-of-type { margin-top:6px; }
  .sv-add p { margin:0 0 12px; color:var(--ink-soft,#3E535C); font-size:.9rem;
              max-width:62ch; line-height:1.6; }
  .sv-add b { color:var(--ink,#1A2830); font-weight:600; }
  .sv-add code { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:.85em;
                 background:var(--accent-wash,var(--reg-wash,#DDEBE7)); padding:1px 5px;
                 border-radius:4px; }
  .sv-add .foot { color:var(--muted,#5F7178); font-size:.82rem; }
  .sv-drop { display:flex; gap:12px; align-items:center; flex-wrap:wrap;
             border:1px dashed var(--rule,#D8E0DE); border-radius:10px; padding:14px;
             background:var(--paper,#F1F4F3); transition:border-color .12s, background .12s; }
  .sv-drop.over { border-color:var(--accent,var(--regulated,#1C6D61));
                  background:var(--accent-wash,var(--reg-wash,#DDEBE7)); }
  .sv-drop button { font:inherit; padding:10px 16px; border:0; border-radius:9px;
                    background:var(--accent,var(--regulated,#1C6D61)); color:#fff; cursor:pointer; }
  .sv-drop .or { color:var(--muted,#5F7178); font-size:.85rem; }
  .sv-says { margin:10px 0 0; font-size:.85rem; color:var(--ink-soft,#3E535C);
             min-height:1.2em; }
  .sv-says.bad { color:var(--broken,#A25E14); }
  .sv-says button { font:inherit; font-size:.85rem; padding:4px 10px; border:0;
                    border-radius:7px; background:var(--accent,var(--regulated,#1C6D61));
                    color:#fff; cursor:pointer; margin-left:4px; }
  .sv-routes { margin:0 0 12px; padding-left:20px; color:var(--ink-soft,#3E535C);
               font-size:.9rem; line-height:1.6; max-width:62ch; }
  .sv-routes li { margin:0 0 7px; }
  .sv-foot { color:var(--muted,#5F7178); font-size:.85rem; }
  .sv-go { margin:16px 0 0; }
  .sv-btn { display:inline-block; padding:11px 18px; border-radius:9px;
            background:var(--accent,var(--regulated,#1C6D61)); color:#fff;
            text-decoration:none; font-size:.9rem; }
  .sv-btn:hover { filter:brightness(1.08); }
</style>
  <div class="sv-add">
    <h2>Getting lessons into this course</h2>

    <h3>You have been sent a file</h3>
    <p>Two kinds of file belong here: <b>a lesson</b> somebody sent you (an
       <code>.html</code> file, one per lesson) and <b>a list of lecture videos</b>
       (a <code>.json</code> file, one per course). Drop either one anywhere on
       this page. <b>You do not have to find a folder, rename anything, or know
       which kind you have</b>: this works it out and files it.</p>
    <div class="sv-drop" id="sv-drop">
      <input type="file" id="sv-pick" accept=".html,.json,.txt,.md" multiple hidden>
      <button type="button" id="sv-pickbtn">Choose a file</button>
      <span class="or">or drag it anywhere on this page</span>
    </div>
    <p class="sv-says" id="sv-imsays" role="status"></p>

    <h3>You do not have anything yet</h3>
    <p>Then the lessons have to be made, and Claude does that on your own
       computer, in your own browser, with you signing in to anything that asks.
       There are three routes and you probably need one of them:</p>
    <ul class="sv-routes">
      <li><b>Your course is on KEATS.</b> It collects the slides and transcripts
          your enrolment gives you, and the addresses of the recordings.</li>
      <li><b>You already have slides and transcripts.</b> It reads them and
          writes the lessons.</li>
      <li><b>The lessons are here but the videos will not play.</b> It collects
          just the video addresses into a file you drop above. This is the
          commonest gap, because a folder of slides never contains them.</li>
    </ul>
    <p class="sv-foot"><b>None of it needs a terminal, and none of it asks for
       your password.</b> The instructions walk through installing Claude, which
       folder to point it at (with the exact path, ready to copy) and the words to
       paste, and they are written for somebody who has not done this before.</p>
    <div class="sv-go">
      <a class="sv-btn" href="%(help)s">Show me how, step by step</a>
    </div>
  </div>
<script>
(function () {
  var MODULE = %(module)s;
  var says = document.getElementById('sv-imsays');
  var pick = document.getElementById('sv-pick');
  var drop = document.getElementById('sv-drop');

  function tell(msg, bad) {
    says.textContent = msg || '';
    says.classList.toggle('bad', !!bad);
  }
  /* One endpoint for both kinds of file. The page promises it works out what
     you dropped, so the SERVER reads the file and decides; guessing here from
     the extension would be guessing from the one thing the person is free to
     get wrong. */
  function url(file, force) {
    return '/api/import?name=' + encodeURIComponent(file.name)
         + (MODULE ? '&module=' + encodeURIComponent(MODULE) : '')
         + (force ? '&force=1' : '');
  }

  /* One file at a time, in order, so the messages describe one thing and the
     server is never asked to write two lessons into one folder at once. */
  function send(files, i, done) {
    if (i >= files.length) { done(); return; }
    var f = files[i];
    tell('Importing ' + f.name + '\u2026');
    fetch(url(f, false), { method: 'POST', headers: window.STUDYTOKEN.headers({}), body: f })
      .then(function (r) { return r.json().then(function (j) { return {s: r.status, b: j}; }); })
      .then(function (r) {
        if (r.s === 401) {
          tell('');
          window.STUDYTOKEN.ask(function () { send(files, i, done); });
          return;
        }
        if (r.s === 409) {
          /* The one refusal that is a question rather than a fault. Answered
             with a button rather than a confirm(): a modal dialog blocks every
             event in the page until it is answered. */
          says.textContent = f.name + ' is already in this course. ';
          says.classList.add('bad');
          var b = document.createElement('button');
          b.type = 'button'; b.textContent = 'Replace it';
          b.addEventListener('click', function () {
            tell('Replacing ' + f.name + '\u2026');
            fetch(url(f, true), { method: 'POST', headers: window.STUDYTOKEN.headers({}), body: f })
              .then(function (rr) { return rr.json(); })
              .then(function (j) {
                if (!j || !j.ok) { tell((j && j.error) || 'That did not work.', true); return; }
                send(files, i + 1, done);
              }).catch(function () { tell('Could not reach the server.', true); });
          });
          says.appendChild(b);
          return;
        }
        if (!r.b || !r.b.ok) { tell((r.b && r.b.error) || 'That did not work.', true); return; }
        if (r.b.readings) {
          results.push(r.b.count + ' core reading' + (r.b.count === 1 ? '' : 's')
                       + ' on this course now');
        }
        if (r.b.lectures) {
          /* A links file says something worth reading, because the useful number
             is how many will actually PLAY, and that is not the same as how many
             were in the file. */
          results.push(r.b.playable + ' of ' + r.b.parts + ' lectures will play in the panel'
                       + (r.b.skipped && r.b.skipped.length
                          ? '; ' + r.b.skipped.length + ' line(s) skipped' : ''));
        }
        send(files, i + 1, done);
      }).catch(function () { tell('Could not reach the server.', true); });
  }

  var results = [];
  function take(files) {
    var list = [];
    for (var i = 0; i < files.length; i++) { list.push(files[i]); }
    if (!list.length) return;
    results = [];
    send(list, 0, function () {
      if (results.length) {
        /* Said before the reload, and left on screen after it would be gone, so
           it is said in a way that survives: the reload is what makes the new
           lessons appear, and a message nobody reads is not a message. */
        tell(results.join('. ') + '. Reloading\u2026');
        setTimeout(function () { window.location.reload(); }, 2200);
      } else {
        tell('Imported. Reloading\u2026');
        window.location.reload();
      }
    });
  }

  document.getElementById('sv-pickbtn').addEventListener('click', function () { pick.click(); });
  pick.addEventListener('change', function () { take(pick.files); });

  /* The whole page is the drop target, not just the box: aiming at a rectangle
     is the part of a drag people get wrong, and there is nothing else on this
     page a dropped file could sensibly mean. */
  ['dragenter', 'dragover'].forEach(function (ev) {
    document.addEventListener(ev, function (e) {
      e.preventDefault(); drop.classList.add('over');
    });
  });
  ['dragleave', 'drop'].forEach(function (ev) {
    document.addEventListener(ev, function (e) {
      e.preventDefault(); drop.classList.remove('over');
    });
  });
  document.addEventListener('drop', function (e) {
    if (e.dataTransfer && e.dataTransfer.files) { take(e.dataTransfer.files); }
  });
}());
</script>
"""


# --------------------------------------------------------------------------
# Captions for a whole course, driven from Settings
# --------------------------------------------------------------------------
# EH asked for one control rather than two (2026-09-04): *"there should be a
# system or option inside the web interface to download captions for more than
# one type of video."* A narrated package and a plain recording are captioned by
# two different pipelines, and the point of the ask is that nobody using the
# reader should have to know that.
#
# 🔴 **THIS SHELLS OUT TO `caption_course.py` AND NEVER IMPORTS IT.**
# `test_caption_course.py` walks this file's transitive import closure and fails
# the day somebody replaces the subprocess with an import, because it would be
# two fewer lines. The reason is not tidiness: importing it would make whisper,
# ffmpeg and pdftotext dependencies of OPENING A LESSON on a recipient's machine,
# and the reader is stdlib-only so that it does not need any of them.
#
# ⚠️ **Two verbs, deliberately.** GET asks and starts nothing; POST starts a run
# and only ever from a person's click with a token. Nothing here runs on a timer
# or on a page load, which is the brief's *"it must never run on a recipient's
# machine by surprise."*

CAPTION_TOOL = "caption_course.py"
CAPTION_ASK_TIMEOUT = 30
CAPTION_START_TIMEOUT = 20


def caption_tool_path():
    return Path(__file__).resolve().parent / CAPTION_TOOL


def caption_root(cfg):
    """The folder the caption tool should treat as the repository root.

    It wants the folder holding `courses/` and `materials/`, which is the parent
    of the courses directory. Derived rather than configured, so a machine that
    moved its courses folder does not need a second setting that can disagree
    with the first.
    """
    root = cfg.get("courses_dir")
    return Path(root).parent if root else Path(cfg["notes_dir"]).parent.parent


def caption_ask(cfg, args, timeout=CAPTION_ASK_TIMEOUT):
    """Ask the caption orchestrator something, as a subprocess, and return its JSON.

    ⚠️ Every failure comes back as `{"ok": False, "error": ...}` rather than an
    exception, because this is rendered on a settings page: a person who has no
    ffmpeg should read a sentence about ffmpeg, not lose the page.
    """
    tool = caption_tool_path()
    if not tool.is_file():
        return {"ok": False,
                "error": "The caption tool is not installed on this machine."}
    root = caption_root(cfg)
    argv = ([sys.executable, str(tool)] + [str(a) for a in args]
            + ["--root", str(root), "--json"])
    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              timeout=timeout, cwd=str(root))
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "The caption tool did not answer in time."}
    except OSError as err:
        return {"ok": False, "error": "Could not run the caption tool: %s" % err}
    try:
        return json.loads((proc.stdout or "").strip())
    except ValueError:
        detail = (proc.stderr or "").strip().splitlines()
        return {"ok": False,
                "error": detail[-1] if detail else "The caption tool said nothing."}


# 🔴 Settings used to be reachable ONLY from inside a lesson, because the sheet
# lives in the reader layer and the layer is composed onto lessons alone. Most of
# what it holds is not about a lesson at all: the model, how answers are pitched,
# the panel's text size and vault publishing are machine-wide, stored in the
# courses root's settings.json. So a global setting needed you to open a lesson
# first, which on a phone is several taps into the wrong place (reported
# 2026-08-21).
#
# This page is a SECOND VIEW of the same state, not a second store: it reads and
# writes `/api/settings`, exactly as the sheet does. The palette is deliberately
# shown read-only and edited in a lesson, where the colours sit on real text.
# The words the reader's own Settings sheet uses for these, so the two views of
# one setting cannot describe it differently.
LEVEL_LABELS = [
    {"id": "plain",     "label": "Plain: no background assumed"},
    {"id": "explained", "label": "Technical, every term explained as it goes"},
    {"id": "technical", "label": "Technical: assumes the vocabulary"},
]

SETTINGS_PAGE = """<!-- study-settings -->
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
%(icons)s<title>Settings</title>
<style>
  :root {
    --paper:#F1F4F3; --surface:#FBFCFC; --ink:#1A2830; --ink-soft:#3E535C; --muted:#5F7178;
    --rule:#D8E0DE; --accent:#1C6D61; --accent-wash:#DDEBE7; --broken:#A25E14;
    --display:"Iowan Old Style","Palatino Linotype",Palatino,"Book Antiqua",Georgia,serif;
    --text:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      --paper:#12191C; --surface:#182126; --ink:#E7EEEC; --ink-soft:#B7C6C4; --muted:#8AA0A0;
      --rule:#26343A; --accent:#5FBFAE; --accent-wash:#173029; --broken:#DBA463;
    }
  }
  :root[data-theme="dark"] {
    --paper:#12191C; --surface:#182126; --ink:#E7EEEC; --ink-soft:#B7C6C4; --muted:#8AA0A0;
    --rule:#26343A; --accent:#5FBFAE; --accent-wash:#173029; --broken:#DBA463;
  }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--paper); color:var(--ink); font-family:var(--text);
         line-height:1.5; -webkit-font-smoothing:antialiased; }
  .wrap { max-width:640px; margin:0 auto; padding:48px 20px 64px; }
  h1 { font-family:var(--display); font-weight:600; font-size:2rem; margin:0 0 4px; }
  .sub { color:var(--muted); margin:0 0 28px; font-size:.95rem; }
  .back { display:inline-block; margin-bottom:26px; color:var(--accent);
          text-decoration:none; font-size:.85rem; font-weight:600; }
  .back:hover { text-decoration:underline; }
  section { background:var(--surface); border:1px solid var(--rule); border-radius:12px;
            padding:18px; margin:0 0 14px; }
  h2 { font-family:var(--display); font-size:1.05rem; font-weight:600; margin:0 0 4px; }
  .hint { margin:0 0 14px; color:var(--ink-soft); font-size:.86rem; max-width:56ch; }
  .opts { display:flex; gap:8px; flex-wrap:wrap; }
  .opts button {
    font:600 13px var(--text); color:var(--ink); background:var(--paper);
    border:1px solid var(--rule); border-radius:8px; padding:9px 14px; cursor:pointer;
  }
  .opts button[aria-pressed="true"] {
    background:var(--accent-wash); border-color:var(--accent); color:var(--accent);
  }
  label.row { display:flex; align-items:center; gap:10px; cursor:pointer;
              font-size:.95rem; }
  .swatches { display:flex; gap:8px; flex-wrap:wrap; margin:0 0 10px; }
  .sw { display:inline-flex; align-items:center; gap:7px; font-size:.82rem;
        color:var(--ink-soft); border:1px solid var(--rule); border-radius:99px;
        padding:5px 11px 5px 6px; }
  .dot { width:13px; height:13px; border-radius:50%%; display:inline-block;
         border:1px solid rgba(0,0,0,.15); }
  .facts { margin:0; font-size:.84rem; color:var(--muted); line-height:1.8; }
  .facts b { color:var(--ink-soft); font-weight:600; }
  h3 { font-family:var(--text); font-size:.76rem; font-weight:700; letter-spacing:.08em;
       text-transform:uppercase; color:var(--muted); margin:22px 0 8px; }
  section > h3:first-of-type { margin-top:0; }
  .hint b { color:var(--ink); font-weight:600; }
  .pathrow { display:flex; gap:8px; flex-wrap:wrap; align-items:center; }
  .pathrow input { flex:1 1 260px; min-width:0; font:inherit; font-size:.86rem;
                   padding:10px 12px; border:1px solid var(--rule); border-radius:9px;
                   background:var(--paper); color:var(--ink); }
  .pathrow input:focus-visible { outline:2px solid var(--accent); outline-offset:1px; }
  .pathrow button { font:inherit; padding:10px 16px; border:0; border-radius:9px;
                    background:var(--accent); color:var(--paper); cursor:pointer; }
  .crow { display:flex; gap:10px; align-items:center; margin:0 0 10px; }
  .crow label { flex:0 0 auto; min-width:5.5em; font-size:.8rem; letter-spacing:.06em;
                text-transform:uppercase; color:var(--muted); }
  .crow input { flex:1 1 180px; min-width:0; font:inherit; font-size:.9rem;
                padding:9px 11px; border:1px solid var(--rule); border-radius:9px;
                background:var(--paper); color:var(--ink); }
  .crow input:focus-visible { outline:2px solid var(--accent); outline-offset:1px; }
  .cfacts { margin:-4px 0 16px calc(5.5em + 10px); font-size:.78rem;
            color:var(--muted); line-height:1.6; overflow-wrap:anywhere; }
  .cfacts code { font-size:.95em; }
  .ask { margin:12px 0 0; padding:14px; border:1px solid var(--broken);
         border-radius:10px; background:var(--surface); }
  .ask p { margin:0 0 12px; font-size:.86rem; color:var(--ink-soft); line-height:1.6; }
  .ask .row2 { display:flex; gap:8px; flex-wrap:wrap; }
  .ask button { font:inherit; font-size:.85rem; padding:9px 14px; border:0;
                border-radius:8px; background:var(--accent); color:var(--paper); cursor:pointer; }
  .ask button.plain { background:transparent; color:var(--ink-soft);
                      border:1px solid var(--rule); }
  .says { margin:12px 0 0; font-size:.85rem; color:var(--ink-soft); min-height:1.2em; }
  .says.bad { color:var(--broken); }
  .danger button {
    font:600 13px var(--text); color:var(--broken); background:var(--paper);
    border:1px solid var(--rule); border-radius:8px; padding:9px 14px; cursor:pointer;
  }
  .danger button:hover { border-color:var(--broken); }
  .caplist { margin:12px 0 0; font-size:.8rem; }
  .caprow { display:flex; gap:10px; align-items:baseline; padding:5px 0;
            border-top:1px solid var(--rule); }
  .caprow b { flex:0 0 7.5em; font-weight:600; color:var(--ink-soft);
              font-variant-numeric:tabular-nums; }
  .capstate { flex:0 0 5.5em; font-size:.72rem; font-weight:700; letter-spacing:.06em;
              text-transform:uppercase; }
  .capstate.done { color:var(--accent); }
  .capstate.failed { color:var(--broken); }
  .capstate.missing, .capstate.blocked { color:var(--muted); }
  .capwhy { color:var(--muted); overflow-wrap:anywhere; }
  .capsum { margin:12px 0 0; font-size:.84rem; color:var(--ink-soft); }
  .caplog { margin:12px 0 0; max-height:12em; overflow:auto; padding:10px 12px;
            font:.74rem/1.45 ui-monospace, SFMono-Regular, Menlo, monospace;
            color:var(--muted); background:var(--surface); border:1px solid var(--rule);
            border-radius:9px; white-space:pre-wrap; overflow-wrap:anywhere; }
  .pathrow select { flex:1 1 200px; min-width:0; font:inherit; font-size:.86rem;
                    padding:10px 12px; border:1px solid var(--rule); border-radius:9px;
                    background:var(--paper); color:var(--ink); }
  .pathrow button[disabled] { opacity:.5; cursor:default; }
</style>
<div class="wrap">
  %(navbar)s
  <h1>Settings</h1>
  <p class="sub">These apply everywhere, on every course on this machine.</p>

  <section>
    <h2>How answers are pitched</h2>
    <p class="hint">What Explain and the chat assume you already know.</p>
    <div class="opts" id="level"></div>
  </section>

  <section>
    <h2>Text size in the right-hand panel</h2>
    <p class="hint">The notes, cards and chat panel beside a lesson.</p>
    <div class="opts" id="size"></div>
  </section>

  <section>
    <h2>Model</h2>
    <p class="hint">Which model answers. %(explain)s</p>
    <div class="opts" id="model"></div>
  </section>

  <section>
    <h2>Reaching Claude</h2>
    <p class="hint">Explain, the chat beside a lesson and Rewrite need one of two
       routes: <b>Claude Code</b> installed and signed in on %(host)s, which
       uses the subscription you already pay for; or an <b>API key</b> of your
       own, where each question is charged to that key. %(route)s</p>
    <div class="opts" id="backend"></div>
    <h3>API key</h3>
    <p class="hint">Kept in the config file on %(host)s, which only its owner
       can read. It is never shown again here, never written to the log, and
       never sent anywhere but api.anthropic.com. %(keystate)s</p>
    <div class="pathrow">
      <input type="password" id="apikey" value="" placeholder="paste a key"
             spellcheck="false" autocomplete="off" autocapitalize="off"
             aria-label="API key">
      <button type="button" id="apikeysave">Save</button>
      <button type="button" id="apikeyclear" class="plain">Remove</button>
    </div>
    <p class="says" id="keysays" role="status"></p>
  </section>

  %(vault)s

  <section>
    <h2>Highlight colours</h2>
    <p class="hint">Shown here so you can see what you have. They are edited
       inside a lesson, where the colours sit on real text.</p>
    <div class="swatches">%(palette)s</div>
  </section>

  %(courses)s
  %(captions)s
  %(pack)s

  <section>
    <h2>This machine</h2>

    <h3>Your courses folder</h3>
    <p class="hint">Your <b>lessons</b> live here, and so does <b>everything you
       have marked on them</b>: highlights, notes, cards, bookmarks and chats, as
       a file per lesson sitting beside it. One folder per course inside.
       <b>This is the folder to back up.</b> Readings PDFs your Claude files
       into a course live here too, in that course’s <code>readings/</code>
       folder, and dated safety copies in its <code>backups/</code>. Slides and
       transcripts you download are not kept here: they stay wherever you put
       them, and the lessons link to them.</p>
    <div class="pathrow">
      <input type="text" id="coursespath" value="%(root)s" spellcheck="false"
             autocomplete="off" autocapitalize="off"
             aria-label="Where your courses are kept">
      <button type="button" id="coursessave">Save</button>
    </div>
    <div id="coursesask"></div>

    <h3>Server</h3>
    <p class="facts">%(server)s</p>

    <p class="hint" style="margin:18px 0 10px">Restarting is how a changed folder
       takes effect. Your highlights are on disk and are not affected.</p>
    <div class="danger"><button type="button" id="restart">Restart the server</button></div>
  </section>

  <p class="says" id="says" role="status"></p>
</div>
%(tokenbar)s
<script>
(function () {
  /* Captions for a whole course. The page NEVER computes any of this: it asks
     /api/captions, which shells out. Two verbs, and only the button starts work. */
  var sel = document.getElementById('capcourse');
  if (!sel) { return; }
  var list = document.getElementById('caplist'), sum = document.getElementById('capsum');
  var go = document.getElementById('capgo'), says = document.getElementById('capsays');
  var inst = document.getElementById('capinstall'), log = document.getElementById('caplog');
  var timer = null;

  function tellCap(msg, bad) {
    says.textContent = msg || '';
    says.classList.toggle('bad', !!bad);
  }

  function draw(d) {
    list.innerHTML = '';
    if (!d || !d.ok) {
      sum.textContent = '';
      go.disabled = true;
      tellCap((d && d.error) || 'Could not read the captions.', true);
      return;
    }
    var counts = d.counts || {}, bits = [];
    ['done', 'missing', 'failed', 'blocked'].forEach(function (k) {
      if (counts[k]) { bits.push(counts[k] + ' ' + k); }
    });
    sum.textContent = bits.join(' \u00b7 ') || 'No lectures in this course can carry captions.';
    (d.lectures || []).forEach(function (r) {
      var row = document.createElement('div');
      row.className = 'caprow';
      var n = document.createElement('b');
      n.textContent = r.doc;
      var st = document.createElement('span');
      st.className = 'capstate ' + r.state;
      st.textContent = r.state;
      var why = document.createElement('span');
      why.className = 'capwhy';
      /* A row always says something. Without the fallback the commonest row on
         the page (a lecture waiting to be built) would be a blank half-line. */
      why.textContent = r.reason
        || (r.kind === 'recording' ? 'a plain recording' : 'a narrated slide package');
      row.appendChild(n); row.appendChild(st); row.appendChild(why);
      list.appendChild(row);
    });
    var miss = d.missing_tools || [], hints = d.hints || {}, eng = d.install || {};
    /* The engine is the one missing tool this page can put right itself. While
       an install is going the tool's own output is shown, and when it is done
       the ordinary branches below take over on the next poll. */
    inst.hidden = true; log.hidden = true;
    var tail = (eng.log_tail || []).join('\\n');
    if (eng.running) {
      go.disabled = true; inst.hidden = false; inst.disabled = true;
      log.hidden = false; log.textContent = tail;
      tellCap('Installing the caption engine. It downloads a few hundred MB, once; '
              + 'you can leave this page.');
    } else if (d.install_needed) {
      go.disabled = true; inst.hidden = false; inst.disabled = false;
      if (eng.state === 'failed') {
        log.hidden = false; log.textContent = tail;
        tellCap('The install did not finish: ' + (eng.error || 'see its last lines above')
                + '. You can try again.', true);
      } else {
        tellCap('The caption engine is not installed on this machine. Installing it '
                + 'downloads a few hundred MB, once, and nothing happens until you click.');
      }
    } else if (miss.length) {
      go.disabled = true;
      var help = miss.map(function (k) { return hints[k]; }).filter(Boolean);
      tellCap('Captions cannot be built on this machine: ' + miss.join(', ')
              + ' not found.' + (help.length ? ' ' + help.join(' ') : ''), true);
    } else if (d.running) {
      go.disabled = true;
      tellCap('Building now. It takes a while, and you can leave this page.');
    } else if (!d.buildable) {
      go.disabled = true;
      tellCap('Every lecture that can have captions has them.');
    } else {
      go.disabled = false;
      tellCap('');
    }
    clearTimeout(timer);
    /* Only while something is actually going: a page left open on a finished
       course must not poll a subprocess for ever. */
    if (d.running || eng.running) { timer = setTimeout(load, 5000); }
  }

  function load() {
    fetch('/api/captions?module=' + encodeURIComponent(sel.value),
          { headers: window.STUDYTOKEN.headers({}) })
      .then(function (r) {
        if (r.status === 401) { window.STUDYTOKEN.ask(load); return null; }
        return r.json();
      })
      .then(function (d) { if (d) { draw(d); } })
      .catch(function () { tellCap('Could not reach the server.', true); });
  }

  go.addEventListener('click', function () {
    go.disabled = true;
    tellCap('Starting\u2026');
    fetch('/api/captions?module=' + encodeURIComponent(sel.value),
          { method: 'POST', headers: window.STUDYTOKEN.headers({}) })
      .then(function (r) {
        if (r.status === 401) { window.STUDYTOKEN.ask(function () { go.disabled = false; }); return null; }
        return r.json();
      })
      .then(function (d) {
        if (!d) { return; }
        if (!d.ok) { tellCap(d.error || 'It would not start.', true); go.disabled = false; return; }
        tellCap('Started.');
        load();
      })
      .catch(function () { tellCap('Could not reach the server.', true); go.disabled = false; });
  });
  inst.addEventListener('click', function () {
    inst.disabled = true;
    tellCap('Starting the install\u2026');
    fetch('/api/captions/install',
          { method: 'POST', headers: window.STUDYTOKEN.headers({}) })
      .then(function (r) {
        if (r.status === 401) { window.STUDYTOKEN.ask(function () { inst.disabled = false; }); return null; }
        return r.json();
      })
      .then(function (d) {
        if (!d) { return; }
        if (!d.ok) { tellCap(d.error || 'It would not start.', true); inst.disabled = false; return; }
        tellCap('Installing.');
        load();
      })
      .catch(function () { tellCap('Could not reach the server.', true); inst.disabled = false; });
  });
  sel.addEventListener('change', load);
  load();
}());

(function () {
  /* The picture pack row. GET /api/packs reads the disk and the feed and starts
     nothing; POST /api/packs/install starts the fetch in its own process, on a
     click and nowhere else, the shape of the caption engine's install above. */
  var btn = document.getElementById('packinstall');
  if (!btn) { return; }
  var sum = document.getElementById('packsum'), says = document.getElementById('packsays');
  var log = document.getElementById('packlog');
  var timer = null;

  function tellPack(msg, bad) {
    says.textContent = msg || '';
    says.classList.toggle('bad', !!bad);
  }
  function mb(n) { return Math.round(n / 1048576) + ' MB'; }

  function draw(d) {
    btn.hidden = true; log.hidden = true;
    if (!d || !d.ok) {
      sum.textContent = '';
      tellPack((d && d.error) || 'Could not read the pack.', true);
      return;
    }
    var tail = (d.log_tail || []).join('\\n');
    var feed = d.feed || null;
    if (d.installed) {
      sum.textContent = 'Installed: version ' + d.version + ', ' + d.plates
        + ' plates for ' + d.regions + ' regions.';
      tellPack('');
    } else if (d.running) {
      sum.textContent = 'Not installed yet.';
      btn.hidden = false; btn.disabled = true;
      log.hidden = false; log.textContent = tail;
      tellPack('Downloading the pictures. You can leave this page.');
    } else if (!feed) {
      sum.textContent = 'Not installed.';
      tellPack('The update feed names no pictures to fetch, or cannot be reached, '
               + 'so there is nothing to install from here.', true);
    } else {
      sum.textContent = 'Not installed. One download of about ' + mb(feed.bytes || 0)
        + ', shared by every course.';
      btn.hidden = false; btn.disabled = false;
      if (d.state === 'failed') {
        log.hidden = false; log.textContent = tail;
        tellPack('The last install did not finish: ' + (d.error || 'see its last lines above')
                 + '. You can try again.', true);
      } else {
        tellPack('Nothing happens until you click.');
      }
    }
    clearTimeout(timer);
    if (d.running) { timer = setTimeout(load, 3000); }
  }

  function load() {
    fetch('/api/packs', { headers: window.STUDYTOKEN.headers({}) })
      .then(function (r) {
        if (r.status === 401) { window.STUDYTOKEN.ask(load); return null; }
        return r.json();
      })
      .then(function (d) { if (d) { draw(d); } })
      .catch(function () { tellPack('Could not reach the server.', true); });
  }

  btn.addEventListener('click', function () {
    btn.disabled = true;
    tellPack('Starting the download\u2026');
    fetch('/api/packs/install',
          { method: 'POST', headers: window.STUDYTOKEN.headers({}) })
      .then(function (r) {
        if (r.status === 401) { window.STUDYTOKEN.ask(function () { btn.disabled = false; }); return null; }
        return r.json();
      })
      .then(function (d) {
        if (!d) { return; }
        if (!d.ok) { tellPack(d.error || 'It would not start.', true); btn.disabled = false; return; }
        tellPack('Downloading.');
        load();
      })
      .catch(function () { tellPack('Could not reach the server.', true); btn.disabled = false; });
  });
  load();
}());

(function () {
  var LEVELS = %(levels)s, SIZES = %(sizes)s, MODELS = %(models)s;
  var state = %(state)s;
  var says = document.getElementById('says');

  function tell(msg, bad) {
    says.textContent = msg || '';
    says.classList.toggle('bad', !!bad);
  }
  function post(body, then) {
    return fetch('/api/settings', {
      method:'POST',
      headers: window.STUDYTOKEN.headers({ 'Content-Type': 'application/json' }),
      body: JSON.stringify(body)
    })
      .then(function (r) { return r.json().then(function (j) { return {s:r.status, b:j}; }); })
      .then(function (r) {
        if (r.s === 401) {
          /* Ask on this page, then make the same change again. */
          tell('');
          window.STUDYTOKEN.ask(function () { post(body, then); });
          return;
        }
        if (!r.b || !r.b.ok) { tell((r.b && r.b.error) || 'That did not save.', true); return; }
        state = r.b;
        paint();
        tell('Saved.');
        if (then) { then(r.b); }
      }).catch(function () { tell('Could not reach the server.', true); });
  }

  /* One painter for all three rows, so a saved value and a drawn value cannot
     disagree: every button is redrawn from whatever the server just returned. */
  function row(id, items, key) {
    var host = document.getElementById(id);
    host.innerHTML = '';
    items.forEach(function (it) {
      var b = document.createElement('button');
      b.type = 'button';
      b.textContent = it.label;
      b.setAttribute('aria-pressed', String(state[key] === it.id));
      b.addEventListener('click', function () {
        var patch = {}; patch[key] = it.id;
        post(patch);
      });
      host.appendChild(b);
    });
  }
  function paint() {
    row('level', LEVELS, 'level');
    row('size', SIZES, 'panelSize');
    row('model', MODELS, 'model');
    var v = document.getElementById('vaultbox');
    if (v) { v.checked = state.vaultEnabled !== false; }
    paintBackend();
  }

  /* ---- the route to Claude ----------------------------------------------
     Drawn like the rows above and written like the machine paths below: it
     lives in the config file, not the settings store, but unlike the paths it
     takes effect at once, so nothing here says "restart". The key field is
     write-only on purpose: the page never learns the key, only whether one
     is set. */
  var BACKENDS = [
    { id: 'auto', label: 'Whichever is set up' },
    { id: 'cli', label: 'Claude Code only' },
    { id: 'api', label: 'API key only' }
  ];
  var keysays = document.getElementById('keysays');
  function tellK(msg, bad) {
    if (!keysays) return;
    keysays.textContent = msg || '';
    keysays.classList.toggle('bad', !!bad);
  }
  function paintBackend() {
    var host = document.getElementById('backend');
    if (!host) return;
    host.innerHTML = '';
    BACKENDS.forEach(function (it) {
      var b = document.createElement('button');
      b.type = 'button';
      b.textContent = it.label;
      b.setAttribute('aria-pressed', String((state.askBackend || 'auto') === it.id));
      b.addEventListener('click', function () {
        tellK('Saving\\u2026');
        machine({ askBackend: it.id }, function (r) {
          if (!r.ok) { tellK(r.error || 'That did not save.', true); return; }
          state.askBackend = it.id;
          paintBackend();
          tellK(r.message + (r.explain ? '' : ' Asking is off: ' + r.explainWhy));
        });
      });
      host.appendChild(b);
    });
  }
  var ksave = document.getElementById('apikeysave');
  var kclear = document.getElementById('apikeyclear');
  var kbox = document.getElementById('apikey');
  function sendKey(value) {
    tellK('Saving\\u2026');
    machine({ apiKey: value }, function (r) {
      if (!r.ok) { tellK(r.error || 'That did not save.', true); return; }
      kbox.value = '';
      tellK(r.message + (r.explain
        ? (r.backend === 'api' ? ' The API answers from now on.' : ' Claude Code still answers first.')
        : ' Asking is off: ' + r.explainWhy));
    });
  }
  if (ksave) {
    ksave.addEventListener('click', function () {
      var v = kbox.value.trim();
      if (!v) { kbox.focus(); return tellK('Paste a key first, or use Remove.', true); }
      sendKey(v);
    });
    kbox.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') { e.preventDefault(); ksave.click(); }
    });
  }
  if (kclear) { kclear.addEventListener('click', function () { sendKey(''); }); }
  paint();

  var v = document.getElementById('vaultbox');
  if (v) {
    v.addEventListener('change', function () {
      post({ vaultEnabled: v.checked });
    });
  }

  /* ---- course names -----------------------------------------------------
     Saved on blur and on Enter rather than behind a button: there is one field
     per course and a row of Save buttons is a row of things to forget. */
  var csays = document.getElementById('csays');
  function tellC(msg, bad) {
    if (!csays) return;
    csays.textContent = msg || '';
    csays.classList.toggle('bad', !!bad);
  }
  document.querySelectorAll('.crow input').forEach(function (el) {
    var was = el.value;
    function saveName() {
      var v = el.value.trim();
      if (v === was) return;
      if (!v) { el.value = was; return; }
      fetch('/api/module', {
        method: 'POST',
        headers: window.STUDYTOKEN.headers({ 'Content-Type': 'application/json' }),
        body: JSON.stringify({ id: el.getAttribute('data-id'), name: v })
      }).then(function (r) { return r.json().then(function (j) { return {s:r.status, b:j}; }); })
        .then(function (r) {
          if (r.s === 401) { tellC(''); window.STUDYTOKEN.ask(saveName); return; }
          if (!r.b || !r.b.ok) { tellC((r.b && r.b.error) || 'That did not save.', true);
                                 el.value = was; return; }
          was = r.b.name;
          el.value = r.b.name;
          tellC('Saved.');
        }).catch(function () { tellC('Could not reach the server.', true); });
    }
    el.addEventListener('blur', saveName);
    el.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') { e.preventDefault(); el.blur(); }
    });
  });

  /* ---- the two paths that are facts about this machine ------------------
     A different endpoint from the one above, because these are written into
     the config file rather than the settings store, they need the server
     restarted to take effect, and one of them can ask a question back. */
  function machine(body, done) {
    return fetch('/api/machine', {
      method: 'POST',
      headers: window.STUDYTOKEN.headers({ 'Content-Type': 'application/json' }),
      body: JSON.stringify(body)
    })
      .then(function (r) { return r.json().then(function (j) { return {s:r.status, b:j}; }); })
      .then(function (r) {
        if (r.s === 401) {
          tell('');
          window.STUDYTOKEN.ask(function () { machine(body, done); });
          return;
        }
        done(r.b || {});
      }).catch(function () { tell('Could not reach the server.', true); });
  }

  var vsave = document.getElementById('vaultsave');
  if (vsave) {
    vsave.addEventListener('click', function () {
      tell('Saving\\u2026');
      machine({ vaultCourses: document.getElementById('vaultpath').value }, function (b) {
        if (!b.ok) { tell(b.error || 'That did not save.', true); return; }
        tell(b.message + ' Restart for it to take effect.');
      });
    });
  }

  /* The courses folder can come back with a QUESTION rather than an answer:
     there are courses in the old folder, and moving them is his call, not this
     page's. Nothing has been written when that happens. */
  var askbox = document.getElementById('coursesask');
  function askAbout(b, path) {
    askbox.innerHTML = '';
    var box = document.createElement('div');
    box.className = 'ask';
    var p = document.createElement('p');
    p.textContent = b.message;
    box.appendChild(p);
    var row = document.createElement('div');
    row.className = 'row2';
    function choice(label, body, plain) {
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.textContent = label;
      if (plain) { btn.className = 'plain'; }
      btn.addEventListener('click', function () {
        askbox.innerHTML = '';
        tell('Working\\u2026');
        machine(body, function (bb) {
          if (bb.ask) { askAbout(bb, path); tell(''); return; }
          if (!bb.ok) { tell(bb.error || 'That did not work.', true); return; }
          tell(bb.message);
        });
      });
      row.appendChild(btn);
    }
    if (b.ask === 'create') {
      choice('Make the folder', { coursesDir: path, answer: 'create' });
    } else {
      choice('Move them across', { coursesDir: path, answer: 'move' });
      choice('Leave them where they are', { coursesDir: path, answer: 'leave' }, true);
    }
    var cancel = document.createElement('button');
    cancel.type = 'button'; cancel.className = 'plain'; cancel.textContent = 'Cancel';
    cancel.addEventListener('click', function () { askbox.innerHTML = ''; tell(''); });
    row.appendChild(cancel);
    box.appendChild(row);
    askbox.appendChild(box);
  }

  var csave = document.getElementById('coursessave');
  if (csave) {
    csave.addEventListener('click', function () {
      var path = document.getElementById('coursespath').value;
      askbox.innerHTML = '';
      tell('Checking\\u2026');
      machine({ coursesDir: path }, function (b) {
        if (b.ask) { askAbout(b, path); tell(''); return; }
        if (!b.ok) { tell(b.error || 'That did not save.', true); return; }
        tell(b.message);
      });
    });
  }

  function restart() {
    tell('Restarting\\u2026');
    fetch('/api/restart', { method:'POST', headers: window.STUDYTOKEN.headers({}) })
      .then(function (r) {
        /* 🔴 Asking for the token has to end the chain, not fall into the catch
           below: "could not reach the server" printed under a token prompt reads
           as two faults, and sends you to look for the wrong one. */
        if (r.status === 401) { tell(''); window.STUDYTOKEN.ask(restart); return; }
        setTimeout(function () {
          fetch('/healthz').then(function () { tell('Back up.'); })
                           .catch(function () { tell('Not answering yet. Give it a moment.', true); });
        }, 1200);
      }).catch(function () { tell('Could not reach the server.', true); });
  }
  document.getElementById('restart').addEventListener('click', restart);
}());
</script>
"""


def modules_summary(cfg):
    # 🔴 Imported here, not at the top. `readings` imports this module, so a
    # module-level import either way round is a cycle: it happens to resolve
    # today because neither uses the other at import time, and that is exactly
    # the kind of thing that stops being true during an unrelated edit.
    import readings

    """What the home page draws: one entry per module, with enough to choose by.

    Plan §10b. `visits.json` already records what was opened and when, per
    module, so "continue where you left off" costs nothing new."""
    out = []
    for mid, folder in sorted(resolve_modules(cfg).items()):
        mcfg = module_cfg(cfg, mid)
        lessons = split_lessons.lessons_in(folder)
        # Reuses the summary the hub already runs on, so "where you left off"
        # cannot disagree between the home page and the module.
        summary = visits_summary(mcfg)
        last = None
        if summary.get("last"):
            last = {"doc": summary["last"], "at": summary["lastAt"], "file": ""}
            try:
                last["file"] = note_path(mcfg, summary["last"]).name
            except ValueError:
                # The lesson he last opened is not in the folder any more. Say
                # nothing rather than offering a link to a 404.
                last = None
        out.append({
            "id": mid,
            "name": mcfg.get("module_name") or mid,
            "code": mcfg.get("module_code") or mid,
            "lessons": len(lessons),
            # Counted from the file rather than remembered, so a course whose
            # readings were deleted by hand stops advertising them.
            "readings": len(readings.read_all(folder)),
            "url": module_url(cfg, mid),
            "last": last,
        })
    return {"ok": True, "modules": out,
            "root": str(cfg.get("courses_dir") or cfg["notes_dir"]),
            # 🔴 Empty when publishing is off, so the status line says "off"
            # rather than naming a vault folder the reader will never write to.
            # A recipient with no Obsidian was otherwise shown a path into a
            # vault they do not have, which reads as a misconfiguration.
            "vault": (str(cfg.get("vault_courses") or "")
                      if vault_on(cfg) else ""),
            "server": "http://%s:%s/" % (cfg["bind_ip"], cfg["port"])}


# --------------------------------------------------------------------------
# what to call the machine this server runs on
# --------------------------------------------------------------------------
#
# 🔴 The reader said "the Mini" in three reader-facing places, hardcoded, and
# the Mini is EH's machine. On the friend's laptop, running the kit, "Kept in
# resources/PACKCRS/W1-T1-P1 on the Mini" names a computer he has never heard
# of. Screenshotted on a rig built to his shape while driving `abd068e`.
#
# It was never simply WRONG, which is what made it a decision rather than a
# typo: for EH the word is correct and useful, because he reads on the MacBook
# while the server and the files are on the Mini, so "on this machine" would be
# false for him. Neither word is right for everybody, so the page derives it.
#
# 🔴 The half that needs no name at all is the common one. A reader arriving on
# LOOPBACK is on the machine holding the files, always, because nobody can
# reach another machine's 127.0.0.1; the implication only runs that way, and
# that is the direction the sentence needs. The kit's launcher opens
# `http://127.0.0.1:<port>/`, so that is every recipient, every time, and they
# are told "on this machine" without this function being consulted.
#
# This name is for the other half: EH on the MacBook today, his phone and his
# iPad next, where "on the study server" would not say WHICH machine to go and
# look on. macOS's ComputerName is the name he already sees in Finder's sidebar
# and in AirDrop, so it needs no explaining and no setting to keep in step when
# the machine is renamed. Empty is a perfectly good answer: the page falls back
# to "the study server", which is true everywhere and dull.

_MACHINE_NAME = None


def scutil_computer_name():
    """macOS's human-facing machine name, or "" anywhere it cannot be asked.

    Not an error worth reporting: on any other platform the caller simply moves
    on to the next probe.
    """
    out = subprocess.run(["/usr/sbin/scutil", "--get", "ComputerName"],
                         capture_output=True, text=True, timeout=5)
    return out.stdout if out.returncode == 0 else ""


# 🔴 Apple's default ComputerName is `<FirstName>'s <Model>`, so the derivation's
# normal output on a mac carries a PERSON'S NAME: `scutil` answers
# `<his first name>’s Mac mini` on this one, read rather than assumed. 🔴 That
# name is spelled around rather than out, because the kit's personal-data
# audit blocks his first name in a shipped file and this file ships. The
# real string is in `_admin/PROJECT-NOTES.md`, which does not. That name
# would go
# into every composed page for a non-loopback reader. Ruled by the manager on
# 2026-08-30 while this was being built, and it is the `project_link` identity
# leak of a week earlier arriving through a new door.
#
# 🔴 macOS writes a CURLY apostrophe, U+2019, not ASCII. A fixture spelled with
# an ASCII quote passes while the real machine leaks, which is why the proof for
# this is the real probe on the real machine and not a fixture agreeing with
# itself. Both are stripped; only the curly one is evidence.
# One optional word before the owner, so `Dr Smith's Mac` strips too. Apple's
# own default needs no more than that, and a wider pattern starts renaming
# machines whose owners typed something deliberate.
# 🔴 `[sS]`, not a lowercase `s` and not `re.IGNORECASE`. The pattern ended in a
# lowercase literal and was compiled UNICODE-only, so `ALEX’S MAC MINI` came back
# whole and the owner's first name went into the Files pane of every lesson they
# share. Naming a Mac in capitals in System Settings is an ordinary thing to do.
# Found by study-hub-qa 2026-08-30, measured through `derive_machine_name()`.
#
# 🔴 The example is a PLACEHOLDER name, and it has to be. The real case
# named this machine's owner, and `build_kit.py`'s blocking personal-data audit
# refuses the author's own first name in any shipped file: `665f5a0` wrote the
# real name into this comment and left `--build` REFUSING for the rest of the
# day, because that unit ran the test suite and not the ship gate. The
# behaviour under test is unchanged - `test_machine_word.py` still drives the
# real string, and test files are excluded from the kit by the manifest.
#
# 🔴 Do NOT simplify this to `re.IGNORECASE` later. The two are functionally
# identical here (the pattern has exactly one cased character), so the reason is
# not behaviour: `[sS]` says in the pattern itself which letter may vary, and a
# reader can see the whole rule without knowing the flags it was compiled with.
# A flag would silently govern every letter anybody adds later, and this is a
# strip whose job is keeping a person's name off a stranger's screen.
POSSESSIVE = re.compile(r"^(?:\S+\s+)?\S+['’ʼ][sS]\s+")


def strip_possessive(name):
    """`<owner>’s Mac mini` becomes `Mac mini`, and still names the machine EH
    walks to. Never strips away everything: a machine actually called
    `Someone's` keeps its name rather than losing it."""
    rest = POSSESSIVE.sub("", name).strip()
    return rest or name


def derive_machine_name(probes=None):
    """The first probe that names the machine honestly, or "".

    The probes are injectable because the interesting cases cannot be produced
    on the machine running the tests: a mac that answers, a mac that does not,
    and a host that declines to name itself.

    🔴 A door left open knowingly, because closing it needs a guess. macOS's
    LocalHostName is ComputerName with the apostrophe dropped and spaces
    hyphenated (`<owner>s-Mac-mini`), which no possessive strip can see. It can
    only reach here if `scutil` fails ON a mac, and `/usr/sbin/scutil` ships
    with every macOS, so the fallback is effectively non-mac territory where the
    shape does not arise. Stripping a leading `<word>s-` instead would rename a
    machine legitimately called `Physics-Lab-3`, which is the silent-wrong
    trade this project refuses.
    """
    for probe in (probes or (scutil_computer_name, socket.gethostname)):
        try:
            name = strip_possessive(str(probe() or "").strip())
        except Exception:
            continue
        if name.endswith(".local"):
            name = name[:-len(".local")]
        # 🔴 A hostname of "localhost" is the machine declining to name itself,
        # and "Kept on localhost" reads worse than saying nothing: it names a
        # place the reader cannot walk to. Keep looking, then give up.
        if name and name.lower() not in ("localhost", "localhost.localdomain"):
            return name
    return ""


def machine_name():
    """Cached for the life of the process. It is one subprocess, and the answer
    cannot change under a running server without somebody renaming the machine,
    which the next restart picks up."""
    global _MACHINE_NAME
    if _MACHINE_NAME is None:
        _MACHINE_NAME = derive_machine_name()
    return _MACHINE_NAME


def compose_lesson(cfg, text, name="<lesson>", served_from=""):
    """See render(). The course NAME is taken from the cfg the module resolved,
    so the header can print "Mood and Neuroscience" where it used to print the
    enrolment code EH found confusing."""
    """The page a browser gets. `served_from` is the origin it is being composed
    FOR, which is what tells the reader's layer it is on a server; see render().
    It defaults to empty because the verifier composes pages only to parse them,
    and a page nobody is serving should not claim to be served."""
    meta, body, title = split_lessons.read_content(text, name)
    # 🔴 The neighbours, derived here and stamped into the page, because
    # `/api/materials` is token-gated and an unpaired reader could otherwise
    # read the lesson it landed on and go nowhere. Same source of truth as the
    # API and the hub: `derived_neighbours` over `lesson_order`, never a chain
    # baked into the pack.
    #
    # Never fatal. The verifier composes pages with a cfg that resolves no
    # module, and a page that cannot work out its neighbours should still
    # render; the layer falls back to asking the API exactly as before.
    nav = None
    state = None
    try:
        doc_id = str(meta.get("doc") or "")
        nav = derived_neighbours(cfg, doc_id) or None
        state = read_lesson_state(cfg).get(doc_id) or None
    except Exception:
        pass
    return split_lessons.render(
        read_reader_part(SHELL_PATH),
        read_reader_part(LAYER_PATH).rstrip("\n"),
        meta, body, title=title,
        cls=str(cfg.get("class_name") or ""),
        store_prefix=str(cfg.get("store_prefix")
                         or split_lessons.DEFAULT_STORE_PREFIX),
        served_from=served_from,
        course_name=str(cfg.get("module_name") or ""),
        # 🔴 Only ever read by a reader that is NOT on loopback; see
        # machine_name() above for why the common case never needs it.
        machine_name=machine_name(),
        # 🔴 The one caller that passes this: a page composed BY a running
        # server is the only page that can be stale against one. A rebuild or the
        # kit's builder leaves it empty and the layer runs no check.
        # 🔴 The STAMP, not `BUILD_ID`: the page must be able to notice a
        # layer, shell or player-controls change, and those never move the
        # build id. Named `build_id` still because the token and the shell's
        # variable are; the rename is filed as its own entry.
        stamp=page_stamp(),
        nav=nav, state=state,
        # Read from the lockfile at compose time rather than cached in a global:
        # it is one small file read, and a version that could go stale in a
        # long-running process is the shape of bug this project keeps meeting.
        pdfjs=vendor_version())


# --------------------------------------------------------------------------
# the update check: quiet, daily, and off until a URL exists
# --------------------------------------------------------------------------

_UPDATE_CACHE = {"at": 0.0, "answer": None}
UPDATE_TTL = 12 * 3600


def own_version():
    """The kit stamps server/version.json at build time; the repo has none and
    reads as "dev", which the check treats as never-outdated (the repo IS the
    source)."""
    try:
        data = json.loads((Path(__file__).resolve().parent / "version.json")
                          .read_text(encoding="utf-8"))
        return str(data.get("version") or "dev")
    except (OSError, ValueError):
        return "dev"


def update_status(cfg, fetch=None):
    """{version, check, newer?, latest?, url?, note?}. Cached for half a day:
    an update notice is not worth a network round trip per page view."""
    import time
    ver = own_version()
    url = str(cfg.get("update_url") or "").strip()
    if not url or ver == "dev":
        return {"ok": True, "version": ver, "check": "off"}
    now = time.time()
    if _UPDATE_CACHE["answer"] and now - _UPDATE_CACHE["at"] < UPDATE_TTL:
        return _UPDATE_CACHE["answer"]

    def default_fetch(u):
        req = urllib.request.Request(u, headers={"User-Agent": "study-hub-update"})
        with urllib.request.urlopen(req, timeout=6) as r:
            return json.loads(r.read().decode("utf-8"))

    try:
        remote = (fetch or default_fetch)(url)
        latest = str(remote.get("version") or "").strip()
        # Versions are date-stamped (2026.08.28+hash), so the date part
        # compares lexically; anything unparseable is treated as not newer,
        # because a broken feed must never nag.
        newer = bool(latest) and latest.split("+")[0] > ver.split("+")[0]
        packs = remote.get("packs")
        answer = {"ok": True, "version": ver, "check": "on", "newer": newer,
                  "latest": latest,
                  "url": str(remote.get("url") or "")[:500],
                  "note": str(remote.get("note") or "")[:300],
                  # What the feed says can be fetched beside the kit (the
                  # picture pack), passed through for the wizard's size label
                  # and the Settings row; absent from an older feed, so {}.
                  "packs": packs if isinstance(packs, dict) else {}}
    except (OSError, ValueError):
        answer = {"ok": True, "version": ver, "check": "unreachable"}
    _UPDATE_CACHE["at"] = now
    _UPDATE_CACHE["answer"] = answer
    return answer


# --------------------------------------------------------------------------
# the other half of the same sentence: is the code on disk the code running
# --------------------------------------------------------------------------
#
# 🔴 THESE ARE TWO DIFFERENT FACTS AND THE READER NEEDS BOTH, which is EH's own
# reading (2026-09-01): "maybe something similar happens when there is a newer
# version of the software available". A NEW VERSION is somebody else's work,
# available whenever he likes. A RESTART OWED is his own machine: the change is
# already on his disk and is NOT in effect, which is the more urgent of the two
# and the one that produces "I fixed that, why is it still broken".
#
# 🟢 The value this rests on already exists and is already proven. `BUILD_ID` is
# snapshotted at import from the code that was actually loaded, and
# `test_build_id.py` pins that it is never re-read: re-reading per request would
# report the new bytes while the old code ran, which is the exact lie a build id
# exists to expose. So the comparison is `BUILD_ID` (what is RUNNING) against a
# fresh `_compute_build_id()` (what is on DISK), and neither side is guessed.
#
# ⚠️ `local-layer.html`, `reader/shell.html` and the player parts deploy on
# SAVE and never move `BUILD_ID`; they are covered by `page_stamp()` and the
# reader's existing stale-page notice. This one is about the PYTHON only, which
# is the half a reload cannot fix.
_RESTART_CACHE = {"at": 0.0, "id": None}
RESTART_TTL = 5.0


def restart_status(now_id=None):
    """{restart_owed, running_build, disk_build}. Never raises.

    Cached for a few seconds because it hashes every module file, and a reader
    opening three lessons in a row must not pay for that three times. The window
    is short on purpose: the answer changes the moment a coder saves a file, and
    a reader who has just been told to relaunch should not be told again for
    five seconds after doing it.

    `now_id` is for the tests, and it is the same shape as `update_status`'s
    `fetch`: the thing that reaches outside is injectable, so the behaviour can
    be driven without arranging the outside world.
    """
    try:
        if now_id is not None:
            disk = str(now_id)
        else:
            now = time.time()
            if _RESTART_CACHE["id"] and now - _RESTART_CACHE["at"] < RESTART_TTL:
                disk = _RESTART_CACHE["id"]
            else:
                disk = _compute_build_id()
                _RESTART_CACHE["at"] = now
                _RESTART_CACHE["id"] = disk
    except Exception:
        # 🔴 Silence is the honest answer to "I cannot tell", the same rule the
        # update check follows: a notice nobody can act on is noise about our
        # own plumbing.
        return {"restart_owed": False, "running_build": BUILD_ID, "disk_build": ""}
    # ⚠️ `unknown` is what `_compute_build_id` returns when it cannot read the
    # files, and it is NOT a build that differs: comparing it would tell every
    # reader on a machine with an unreadable module to relaunch, for ever.
    owed = bool(disk) and disk != "unknown" and disk != BUILD_ID
    return {"restart_owed": owed, "running_build": BUILD_ID, "disk_build": disk}


# --------------------------------------------------------------------------
# http
# --------------------------------------------------------------------------

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    # The vendored pdf.js ships as ES modules. Served with the wrong type a
    # module does not fail visibly, it fails as "expected a JavaScript module
    # script but the server responded with a MIME type of ...", which is a
    # sentence nobody sees unless a console is open.
    ".mjs": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    # R8: the kinds an attached file actually arrives as. Anything not listed is
    # served as application/octet-stream, which the browser offers to download
    # rather than guessing at, and which the reader labels "open it in a tab".
    ".pdf": "application/pdf",
    ".txt": "text/plain; charset=utf-8",
    ".csv": "text/csv; charset=utf-8",
    ".tsv": "text/tab-separated-values; charset=utf-8",
    ".log": "text/plain; charset=utf-8",
    ".gif": "image/gif",
    ".jpeg": "image/jpeg",
    ".avif": "image/avif",
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".webm": "video/webm",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".wav": "audio/wav",
    ".aac": "audio/aac",
    # Mirrored slide packages carry their own fonts.
    # Captions. Not required for the route above, which sets the type itself,
    # but a `.vtt` reaching any other route should not be offered as a download.
    ".vtt": "text/vtt; charset=utf-8",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".otf": "font/otf",
}


def parse_range(header, size):
    """One `Range: bytes=` request against a representation of `size` bytes.

    Returns `(start, end, status)`: the whole thing at `200` when there is no
    usable range header, `(start, end, 206)` for a satisfiable one, and **None**
    when the range cannot be satisfied, which the caller answers with `416`.

    🔴 **SHARED BY BOTH ROUTES THAT SERVE MEDIA, and that is the point rather
    than tidiness.** `_course_video` had this logic and `_package` did not, and
    the second was a live defect for a year: **Chrome will not seek inside a
    resource whose server refuses ranges, so it restarts it from byte 0**, and
    since each slide of a lecture is its own mp3, byte 0 is the start of that
    slide. EH reported it as the progress bar restarting the slide; QA measured
    the route answering `200` with the whole file to a `Range` request; the same
    export served by KEATS to the same browser was fine.

    ⚠️ **A second copy of range parsing is a second place for the 416 edge to be
    wrong**, which is why this is one function and not a paste.

    ⚠️ **One deliberate difference from the code it replaces**: a malformed spec
    (`bytes=-`, or a non-integer) now returns the whole representation at `200`
    rather than at `206`. Answering `206` to a request naming no range is a
    partial-content reply that is not partial, and RFC 9110 says an invalid
    Range is ignored.
    """
    whole = (0, size - 1, 200)
    if not header or not header.startswith("bytes=") or "," in header:
        return whole
    spec = header[len("bytes="):].strip()
    try:
        first, _, last = spec.partition("-")
        if first:
            start = int(first)
            end = int(last) if last else size - 1
        elif last:
            # `bytes=-500`: the LAST 500 bytes, not "up to 500".
            start = max(0, size - int(last))
            end = size - 1
        else:
            return whole
    except ValueError:
        return whole
    if start > end or start >= size:
        return None
    return start, min(end, size - 1), 206

# The first bytes of the kinds that are worth recognising when the NAME lies.
# A file whose suffix this server does not know is served as octet-stream, and
# every browser downloads that instead of showing it, with no error anywhere:
# the pane just sits blank while a download bar appears. That happened for real
# on 2026-08-29, to a PDF named `... .pdf)` with a stray bracket.
#
# 🔴 Two rules hold this safe, and neither is optional.
#
# 1. **Sniffing is a FALLBACK for an unknown suffix, never an override of a
#    known one.** A file called `.txt` is text even if it starts with `%PDF-`.
#    Overriding would let a name the user chose be second-guessed by content
#    they may not control, which is the whole reason `nosniff` exists.
# 2. **Nothing in this table is a type a browser EXECUTES.** No text/html, no
#    javascript, no SVG. Those are the types where guessing turns a file into
#    code, and all three have suffixes nobody misspells by accident. The cost
#    of being wrong here is a picture that does not render; the cost of being
#    wrong about HTML is script running on this origin.
MAGIC_TYPES = (
    (b"%PDF-", "application/pdf"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)

# 16 bytes covers every signature above with room to spare, and it is the slice
# a caller has to hand over; asking for the whole file to name it would be a
# different and worse trade.
MAGIC_PEEK = 16


def content_type_for(name, head=b""):
    """The content type to serve `name` with: its suffix first, its first bytes
    second, `application/octet-stream` last.

    `head` is the start of the file (`MAGIC_PEEK` bytes is enough). Callers that
    do not have the bytes to hand pass nothing and get the suffix behaviour,
    which is what this server did everywhere until 2026-08-30.
    """
    ctype = CONTENT_TYPES.get(Path(name).suffix.lower())
    if ctype:
        return ctype
    for magic, sniffed in MAGIC_TYPES:
        if head.startswith(magic):
            return sniffed
    return "application/octet-stream"


class Handler(BaseHTTPRequestHandler):
    server_version = "kcl-study"
    protocol_version = "HTTP/1.1"
    # `base_cfg` is the machine's configuration and never changes. `cfg` is
    # REQUEST SCOPED: every request points it at the module it is about, so the
    # sixty places that read cfg["notes_dir"] keep working unchanged. It starts
    # as the base so that anything reached before resolution still has a cfg.
    base_cfg = None
    cfg = None

    def log_message(self, fmt, *args):
        pass

    # -- helpers ---------------------------------------------------------

    def _json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _text(self, text, status=200, ctype="text/plain; charset=utf-8"):
        if isinstance(text, str) and ctype.startswith("text/html"):
            text = self._stamp_page(text)
        body = text.encode("utf-8") if isinstance(text, str) else text
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _allowed_hosts(self):
        # 🔴 Every name is granted on a PORT, and that is deliberate:
        # a Host header is `name:port` and the port is half the identity of an
        # origin. The https listener therefore needs its own entries rather
        # than inheriting the name's, which is why `ports` is a list and not a
        # number. Added only when TLS is actually configured, so a server with
        # https off does not accept a Host for a port nothing answers.
        ports = [self.cfg["port"]]
        tls = tls_port(self.cfg)
        if tls:
            ports.append(tls)
        hosts = set()
        for port in ports:
            hosts.update({"127.0.0.1:%d" % port, "localhost:%d" % port,
                          "[::1]:%d" % port})
            if not is_loopback(self.cfg["bind_ip"]):
                hosts.add("%s:%d" % (self.cfg["bind_ip"], port))
            for h in (self.cfg.get("extra_hosts") or []):
                h = str(h).strip().lower().rstrip(".")
                if h:
                    hosts.add("%s:%d" % (h, port))
        return hosts

    # 🔴 The one origin this server manufactures for ITSELF, and the only place
    # it is ever accepted.
    #
    # A mirrored package is served with `Content-Security-Policy: sandbox
    # allow-scripts` on purpose (2026-08-22, load-bearing: the player must not be
    # able to read localStorage or reach the API with the token). That puts the
    # package document in an OPAQUE origin, and every programmatic fetch from an
    # opaque origin arrives as `Origin: null`. The player XHRs each `slideN.css`
    # with a cache-buster, and fonts are CORS-mode always. So a gate that refuses
    # `null` refuses this server's own page reading this server's own files.
    #
    # 🔴 It cost every package its TEXT for two days and nobody saw it, which is
    # the part worth remembering: `<script>` and `<img>` send no Origin at all,
    # so the images, the audio and the player itself kept working, while the
    # slide CSS and the fonts 403'd. A slide with no CSS and no glyphs is not a
    # broken page, it is a picture with the words missing, and every proof this
    # project had (the mirror audit, `verify_packages`, image counts) was looking
    # at files on disk rather than at what a browser could actually read.
    SANDBOX_ORIGIN = "null"

    # 🔴 TWO ROUTES, AND THE SECOND ONE COST THE SAME MISTAKE TWICE.
    # `/captions/` was added 2026-09-03, and it was added because the captions
    # feature DID NOT WORK without it: the injected script fetches a `.vtt` from
    # inside the sandbox, so its request arrives as `Origin: null` and this gate
    # 403'd every one. **The reader was told "No captions were made for this
    # lecture" on a lecture with eleven caption files on disk.**
    #
    # ⚠️ THE FAILURE IS THE ONE THE COMMENT ABOVE ALREADY DESCRIBES, which is
    # why it is worth writing down again rather than just fixing. A `curl` with
    # no `Origin` header returned 200 and was read as proof the route worked;
    # the browser sends the header and gets 403. **A probe that does not send
    # what the browser sends is not a witness.** Same shape as the two days of
    # missing slide text: the thing on disk was fine and unreachable.
    SANDBOX_READS = ("/packages/", "/captions/")

    def _is_sandbox_read(self, path):
        """`/m/<CODE>/packages/...` or `/m/<CODE>/captions/...`: a mirrored
        package's own file, or the caption file belonging to one. Nothing else is
        ever readable from the sandboxed origin.

        🟢 The two are the same KIND of thing and the widening is real but small:
        both are course material this server already serves to anyone who can
        reach it, both already carry `Access-Control-Allow-Origin: *` for the same
        reason, and both are read and never written. A stranger's sandboxed frame
        that could read a `.vtt` could already read the narration MP3 it was
        transcribed from, which is the same words in a heavier format.
        """
        m = self.MODULE_PATH_RE.match(path or "")
        if not m:
            return False
        rest = m.group(2) or ""
        return any(rest.startswith(prefix) for prefix in self.SANDBOX_READS)

    def _host_ok(self, path=""):
        """DNS rebinding is the whole threat model for a server that can run a
        subprocess. A stranger's page can reach an IP, but it cannot forge these
        headers.

        🔴 The Host half is absolute and has no exemptions: it is what actually
        stops a rebinding attack, and nothing below touches it."""
        allowed = self._allowed_hosts()
        host = (self.headers.get("Host") or "").strip().lower()
        if host not in allowed:
            return False
        origin = (self.headers.get("Origin") or "").strip().lower()
        # 🔴 BOTH SCHEMES, and the `https` half was added 2026-08-30 ahead
        # of the certificate unit rather than inside it. The set used to be
        # `{"http://" + h}`, and the failure shape that produced is the worst
        # one available: a same-origin GET sends no `Origin` header at all, so
        # an https page READ perfectly while every POST 403'd. Highlights,
        # notes, cards and vault publishing all stop on a page that looks
        # healthy, and a read-only QA pass calls it green.
        #
        # 🔴 It widens nothing. Each entry is still one host and one
        # port out of `_allowed_hosts`, and an `https://<that host>:<that port>`
        # origin can only be produced by a page this server itself served over
        # TLS on that address. A stranger cannot mint it: they would have to be
        # us. What it removes is a gate that refuses our own future page.
        if not origin or origin in {s + h for h in allowed
                                    for s in ("http://", "https://")}:
            return True
        # 🔴 The exemption is as narrow as it can be made: this one origin, the
        # two sandbox-readable routes in `SANDBOX_READS`, and GET only. `null` is not a name anybody owns, so a
        # stranger's page CAN produce it (by sandboxing an iframe of its own),
        # which is exactly why it stays refused for `/api`, for every page, and
        # for every POST. What it reaches here is course material this server
        # already serves to anyone who can reach it, and which already carries
        # `Access-Control-Allow-Origin: *` for the same reason.
        return (origin == self.SANDBOX_ORIGIN
                and self.command == "GET"
                and self._is_sandbox_read(path))

    def _stamp_page(self, html):
        """Every HTML page this server sends says which origin it was composed for.

        🔴 EH's requirement, 2026-08-29: the stamp rides EVERY composed page type,
        not just lessons. Done at the ONE point every page leaves through rather
        than in each page builder, which is the whole reason it is worth having: a
        page added next year cannot forget it. The bug this closes happened
        because two halves of one mechanism were edited separately and nobody
        owned the join.

        Lessons already carry the stamp from `render()`, so they are left alone.
        Nothing on the other pages reads it today; it is an invariant to be able
        to check, and `_page_origin` is the single answer both paths give."""
        if "var SERVED_FROM" in html:
            return html
        origin = self._page_origin()
        if not origin:
            return html
        tag = "<script>var SERVED_FROM = %s;</script>\n" % json.dumps(origin)
        # 🔴 These pages are FRAGMENTS: no <html>, no <head>, just a marker
        # comment and a <meta>, with the browser implying the rest. An earlier
        # version of this anchored on "<head" and silently stamped nothing,
        # which is the same shape of mistake as the bug it is here to prevent:
        # a guard that looks for something that was never there. So it goes
        # after the marker comment when there is one, and at the very top when
        # there is not.
        if html.startswith("<!--"):
            shut = html.find("-->")
            if shut >= 0:
                cut = shut + 3
                if html[cut:cut + 1] == "\n":
                    cut += 1
                return html[:cut] + tag + html[cut:]
        return tag + html

    def _page_origin(self):
        """The origin this request came in on, which is the one the browser will
        read the page back from.

        🔴 Taken from the request's own Host rather than from `bind_ip`, and that
        is the entire point: one bound address answers to several names (the IP,
        the MagicDNS name, loopback), and a page composed for the wrong one would
        switch its own reader off. `_host_ok` has already refused anything not in
        the allow-list by the time this is called, so the header is trusted here
        because it was checked there."""
        host = (self.headers.get("Host") or "").strip()
        return ("http://" + host) if host else ""

    def _auth_ok(self):
        """On loopback, being on the machine is the credential. On the tailnet,
        every other device is also 'on the machine', so /api needs the token.
        The pages themselves stay readable: they are study notes, the tailnet is
        his own devices, and a top-level navigation cannot carry a header.

        🔴 The credential is a fact about THIS CONNECTION, not about the config.
        This asked `is_loopback(cfg["bind_ip"])` until 2026-08-30, which is the
        same question only while the server has exactly one listener. It now has
        two (see `start_loopback_listener`), and the config answer would have
        refused every request arriving on the loopback one, which is the entire
        point of adding it. Asking the peer is also the more accurate reading of
        the sentence above: what earns the exemption is being on this machine.

        A tailnet peer is never loopback, including this machine talking to its
        own tailnet address, so nothing that needed the token stops needing it.

        🔴 **Anything placed IN FRONT of this server on loopback authenticates
        every request it forwards.** A reverse proxy connects from 127.0.0.1, so
        each request it passes on is a loopback peer and this returns True before
        the token is ever looked at. That is not an edge case of a hypothetical
        deployment: it IS `plans/07-remote-access.md` §Lane 1(b), whose two named
        candidates (a local Caddy, a Cloudflare Tunnel) both run on this machine
        and forward to a local port. There is no version of the recommended shape
        that does not spring it.

        **Adopting 07(b) means re-gating `/api` in the same change**, as a
        precondition and not a follow-up: the proxy's own login becomes the only
        thing standing in front of every write route. The plan says so too.

        🔴 And read the peer from the SOCKET, never from a header.
        `X-Forwarded-For` and its relatives are attacker-controlled over the
        wire, and the natural edit for somebody wiring up that proxy is to trust
        whichever one they read about first.
        `TheExemptionIsAboutTheSOCKETNotAHeader` in `test_loopback_listener.py`
        is what fails when they do; it did not exist until 2026-08-30, and until
        then that edit passed the whole suite.

        A proxy on a DIFFERENT machine connects from the tailnet and changes
        none of this. The trap is same-machine proxies only, which is exactly
        the shape the plan recommends."""
        if is_loopback(self.client_address[0]):
            return True
        want = str(self.cfg.get("token") or "")
        got = (self.headers.get("Authorization") or "").strip()
        if got.lower().startswith("bearer "):
            got = got[7:].strip()
        else:
            got = ""
        return bool(want) and hmac.compare_digest(want, got)

    # -- which module is this request about? ------------------------------
    #
    # 🔴 The reader does not know it is in a module, and it does not need to.
    # Its own fetches carry a Referer, and the Referer carries the module, so
    # `/m/PSY101/W3-T3-P4-….html` asking for `/api/marks?doc=W3-T3-P4` is
    # answered out of that module's folder with nothing added to the client.
    # An explicit module wins where a caller has one, which is how the home page
    # and any tooling address a module directly. 🔴 WHICH ONE IS READ DEPENDS ON
    # THE VERB, and the earlier version of this comment said "query or body" for
    # both, which was false for POST and is the defect QA found by seeding a rig:
    # marks POSTed with `?module=RIGY` landed in the default course and the reply
    # said `{"ok": true}`.
    #
    #   GET, HEAD          the QUERY (`?module=`), then the Referer, then default
    #   POST, generic      the BODY (`"module"`), then the Referer, then default
    #   POST, early routes the QUERY, because they carry no JSON body to name it:
    #                      `/api/resources`, `/api/share`, `/api/import`,
    #                      `/api/links` and `/api/captions` each parse it
    #                      themselves and refuse a course they cannot resolve
    #                      rather than falling through to another one.
    #                      `/api/restart` names none on purpose: a restart is not
    #                      about a course.
    #
    # 🔴 THE QUERY IS IGNORED BY THE GENERIC POST BRANCH, DELIBERATELY, and this
    # comment says so rather than being accurate by omission. Honouring it there
    # would hand a cross-origin POST its choice of course, where today it gets
    # whichever course the Referer names. **A write landing in the wrong course is
    # silent**: `ok: true`, a plausible count, and the marks in another folder.
    # ⚠️ It stops being safe the day a caller POSTs a JSON body to the generic
    # branch while naming its course only in the query. There is none today,
    # checked at every call site rather than assumed, and `test_module_source.py`
    # is what keeps this paragraph honest.

    MODULE_PATH_RE = re.compile(r"^/m/([A-Za-z0-9][A-Za-z0-9._-]{0,63})(/.*)?$")

    def _module_from_referer(self):
        ref = self.headers.get("Referer") or ""
        try:
            path = urllib.parse.urlparse(ref).path
        except ValueError:
            return None
        m = self.MODULE_PATH_RE.match(path or "")
        return m.group(1) if m else None

    def host_word(self):
        """The word for the machine this server runs on, for THIS reader."""
        return host_word(self.client_address[0])

    def _use_module(self, want=None):
        """Point this request's cfg at a module. Falls back to the default one,
        so a request that names nothing behaves exactly as it did before modules
        existed."""
        base = self.base_cfg
        mods = resolve_modules(base)
        mid = want if want in mods else None
        if mid is None:
            ref = self._module_from_referer()
            mid = ref if ref in mods else default_module(base)
        if mid is None:
            self.cfg = base
            return None
        try:
            self.cfg = module_cfg(base, mid)
        except ValueError:
            self.cfg = base
            return None
        return mid

    def _body(self, limit=500_000):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > limit:
            # Nothing was read, and nothing is going to be: a body this size is
            # not going to be drained just to keep a connection alive.
            self.close_connection = True
            raise ValueError("bad content length")
        raw = self.rfile.read(length)
        self._body_consumed = True
        return json.loads(raw.decode("utf-8"))

    def _drain_body(self):
        """Read and discard the body of a request that is being refused.

        🔴 Found 2026-08-16, and it is why an untokened device saw an error page
        where the lesson should be. This server speaks HTTP/1.1 with keep-alive,
        so a refused POST whose body is left on the socket makes the NEXT
        request on that connection parse from the middle of this one's JSON:
        the browser asks for the page and gets "Bad request syntax" with its own
        marks quoted back at it. The reader looks broken; nothing is wrong with
        it. The reply must consume what it declined to read.
        """
        if getattr(self, "_body_consumed", False):
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > 5_000_000:
            self.close_connection = True
            return
        while length > 0:
            chunk = self.rfile.read(min(length, 65536))
            if not chunk:
                self.close_connection = True
                return
            length -= len(chunk)
        self._body_consumed = True

    # -- routes ----------------------------------------------------------

    def _https_redirect(self, parsed):
        """Where this request should have gone, or None to serve it here.

        🔴 THE WHOLE POINT OF THE SECOND PORT. `http://<name>:8795`
        is the address in every bookmark on three devices, and under the
        same-port answer it would have failed with a TLS error. It redirects
        instead, path and query preserved.

        🔴 FOUR THINGS IT MUST NOT DO, and each is a way to take the
        reader offline rather than move them:

          - not when https is OFF, and OFF INCLUDES AN EXPIRED CERTIFICATE.
            `tls_port` reads the certificate and its DATES, so a config naming a
            port with nothing valid to serve on it redirects nobody. Shipping
            the redirect before provisioning is exactly how the tailnet reader
            goes dark, and until 2026-08-30 so was letting it run past the
            certificate's last day: measured, the listener still started, this
            guard was still satisfied, and every reader was sent to a port their
            browser refused.
          - not on LOOPBACK. `127.0.0.1:8795` is its own listener, is already a
            secure context, and is the desk path. It stays plain http.
          - not on a request that ARRIVED over TLS. The same Handler serves
            both listeners, so without this the https port redirects to itself
            for ever.
          - not on `/api`, and not on anything that is not a GET. A redirect
            answers a navigation; a redirected POST loses its body in some
            clients and its `Authorization` header in others, and the reader
            would see saves fail rather than a page move.
        """
        port = tls_port(self.cfg)
        if not port:
            return None
        if is_loopback(self.client_address[0]):
            return None
        if isinstance(getattr(self, "connection", None), ssl.SSLSocket):
            return None
        if self.command != "GET" or (parsed.path or "").startswith("/api/"):
            return None
        host = (self.headers.get("Host") or "").strip().lower()
        name = host.rsplit(":", 1)[0] if host else ""
        if not name:
            return None
        target = "https://%s:%d%s" % (name, port, parsed.path or "/")
        if parsed.query:
            target += "?" + parsed.query
        return target

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(parsed.path)

        if path == "/healthz":
            # ⚠️ Answered BEFORE the host gate, deliberately and unchanged: the
            # supervisor has to be able to ask. Two digests of our own shipped
            # files and nothing else, which says which code is running without
            # saying anything about the reader, the course or the machine.
            #
            # 🔴 THE ORDER IS LOAD-BEARING AND IS NOT THE PAGE'S BUSINESS.
            # `BUILD_ID` stays FIRST because the restart ritual in
            # `_admin/PROJECT-NOTES.md`, the kit's start script and QA's
            # independent witness all read this line to answer "which Python is
            # live", and moving a different value into that slot would have them
            # report a stale deploy that had not happened.
            # 🟢 The PAGE compares exactly ONE value, the stamp in the second
            # slot, which is the manager's ruling: the build id here is for
            # operators and is never a second comparison.
            return self._text("ok %s %s\n" % (BUILD_ID, page_stamp()))

        # 🟢 The bookmarks keep working. Inert until a certificate is
        # actually configured and readable; see `_https_redirect`.
        where = self._https_redirect(parsed)
        if where is not None:
            self.send_response(308)
            self.send_header("Location", where)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        # The mark. Generated rather than stored: it is fifty lines of geometry,
        # and a file on disk is one more thing to keep in step with the drawing.
        # Cached hard because it never changes between restarts, and a tab icon
        # re-fetched on every page load is the sort of thing that shows up as a
        # mystery in a log a year later.
        if path in ("/favicon.svg", "/favicon.ico", "/apple-touch-icon.png"):
            if path == "/favicon.svg":
                body, ctype = icon.svg().encode("utf-8"), "image/svg+xml"
            elif path == "/favicon.ico":
                body, ctype = icon.ico(32), "image/x-icon"
            else:
                body, ctype = icon.png(180), "image/png"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            self.wfile.write(body)
            return

        if not self._host_ok(path):
            return self._text("bad host\n", 403)

        # The vendored reader assets. Global, not per-course: one copy serves
        # every module, and the version in the path is the cache key. Cached for
        # a year and marked immutable, which is honest here in a way it usually
        # is not: the URL changes whenever the bytes do.
        if path.startswith("/reader/vendor/"):
            return self._vendor_asset(path[len("/reader/vendor/"):])

        # A plate out of the brain-region pack. Unauthenticated for the same
        # reason the pages are: a top-level `<img>` cannot carry a header, these
        # are generated diagrams rather than anybody's data, and the tailnet is
        # his own devices. The token still guards every `/api` route below.
        if path.startswith(regionpack.ROUTE):
            return self._pack_image(path[len(regionpack.ROUTE):])

        query = urllib.parse.parse_qs(parsed.query)

        if path.startswith("/api/"):
            if not self._auth_ok():
                return self._json({"ok": False, "error": "token required"}, 401)
            self._use_module((query.get("module") or [None])[0])
            if path == "/api/update":
                # 🔴 ONE endpoint, two producers, because the reader has ONE
                # surface for both and a second round trip to fill the same line
                # would be a second thing to fail. The two answers are kept in
                # separate keys and separately silent: an unreachable feed says
                # `check: unreachable` and nothing else, and a build id that
                # cannot be read says `restart_owed: false`.
                answer = dict(update_status(self.base_cfg))
                answer.update(restart_status())
                return self._json(answer)
            if path == "/api/packs":
                # The picture pack: is it on this disk, is an install going, and
                # what the feed offers. Reads and starts nothing. Course-
                # independent: one pack serves every course.
                answer = dict(regionpack.install_status())
                answer["ok"] = True
                answer["feed"] = (update_status(self.base_cfg).get("packs") or {}
                                  ).get(regionpack.PACK_ID)
                return self._json(answer)
            if path == "/api/captions":
                # Asks and starts nothing, so the settings page may call it as
                # often as it likes, including while a run is going.
                #
                # ⚠️ A course that does not exist is a 404, not a quiet fallback
                # to whichever one is default. `_use_module` resolves an unknown
                # id to the default, so without this check asking about `NOPE`
                # returns another course's lectures under that name, and the page
                # would render them as if they were the answer. The POST below
                # already refused it; these two now agree.
                want = (query.get("module") or [None])[0]
                mod = self.cfg.get("module") or ""
                if want and want != mod:
                    return self._json({"ok": False,
                                       "error": "no course called %r" % want}, 404)
                if not mod:
                    return self._json({"ok": False, "error": "no course chosen"})
                return self._json(caption_ask(self.base_cfg, ["--status", mod]))
            if path == "/api/modules":
                return self._json(modules_summary(self.base_cfg))
            if path == "/api/lessons":
                # Which lessons this module actually has, so the reader can tell
                # a cross-reference that will open from one that will 404. A
                # shared lesson links to its siblings, and a recipient given one
                # part of a topic has links to parts they were never sent.
                metas = lesson_meta_index(self.cfg)
                return self._json({
                    "ok": True,
                    "module": self.cfg.get("module") or "",
                    "files": sorted(m.get("file") or "" for m in metas.values()),
                    "titles": {(m.get("file") or ""): (m.get("title") or d)
                               for d, m in metas.items()},
                })
            if path == "/api/search":
                mid = (query.get("module") or [None])[0]
                q = (query.get("q") or [""])[0]
                return self._json({"ok": True, "q": q,
                                   "hits": search_lessons(self.base_cfg,
                                                          mid if mid else None, q)})
            if path == "/api/notebook":
                mid = (query.get("module") or [None])[0]
                return self._json(notebook_summary(
                    self.base_cfg, mid if mid else None, (query.get("q") or [""])[0]))
            return self._api_get(path, query)

        # R8. An attached file is served from its own root, never from notes/,
        # which is what keeps EH's "keeps the data clean" true at the level
        # that matters: a bug in one path cannot reach the other's directory.
        if path.startswith("/resources/"):
            self._use_module()
            return self._resource(path)

        # 🔴 `/` redirects to the only module, which left the modules page
        # reachable only by knowing `?home=1`. Asked 2026-08-16: "How do I access
        # the page with a list of modules/classes?" `/home` is the answer, and it
        # is linked from the reader's Settings sheet and from the notebook page.
        if path == "/home":
            self._use_module()
            if self.base_cfg.get("courses_dir") is None:
                return self._text("There is one module, and this is it.\n", 404)
            return self._home_page()

        if path == "/settings":
            self._use_module()
            return self._settings_page()

        # Step 0 of the wizard: the three names, asked before the folder exists.
        # It has to be its own address because the rest of the wizard lives at
        # `/m/<CODE>/start` and cannot be reached until the code is real.
        if path == "/addcourse":
            self._use_module()
            return self._add_course_page()

        # The instructions. `/help` on its own, and `/m/<CODE>/help` so the
        # prompts can name the course you are actually looking at.
        if path == "/help" or path == "/help/":
            mid = self._use_module((query.get("module") or [None])[0])
            return self._help_page(mid)

        if path.startswith("/help/"):
            self._use_module()
            return self._help_file(path[len("/help/"):])

        if path == "/notebook":
            self._use_module()
            return self._notebook_page(None, {k: v[0] for k, v in query.items()})

        # /m/<module>/… is a module's own space: its hub at the bare path, its
        # lessons and sidecars under it.
        m = self.MODULE_PATH_RE.match(path)
        if m:
            mid = self._use_module(m.group(1))
            if mid != m.group(1):
                return self._text("no such module\n", 404)
            rest = m.group(2) or "/"
            if rest == "/notebook":
                return self._notebook_page(mid, {k: v[0] for k, v in query.items()})
            if rest in ("/help", "/help/"):
                return self._help_page(mid)
            if rest in ("/readings", "/readings/"):
                return self._readings_page(mid)
            if rest in ("/mistakes", "/mistakes/"):
                return self._mistakes_page(mid)
            if rest in ("/start", "/start/"):
                return self._wizard_page(mid)
            if rest in ("/", "/index.html"):
                # 🔴 The GENERATED hub is the only hub, for every course
                # (EH, 2026-08-23: "we need to ensure standardization for the
                # whole system"). Hand-written hubs used to win, and the one
                # course that had one looked like a different product from
                # every course that did not; the retired one sits in that
                # course's backups/. Lessons stay hand-written documents; what
                # LISTS them is derived from their own metadata, so it cannot
                # diverge per course.
                return self._module_index(mid)
            if rest.startswith("/packages/"):
                return self._package(rest, query)
            if rest.startswith("/captions/"):
                return self._captions(rest)
            if rest.startswith("/videos/"):
                return self._course_video(rest)
            if rest.startswith("/materials/"):
                return self._course_material(rest)
            # The course-scoped address for an attachment. `/resources/<DOC>/…`
            # still works and is what a single-course install serves; this is
            # the form that survives being opened in a new tab, where the
            # Referer is the only other thing naming the course.
            if rest.startswith("/resources/"):
                return self._resource(rest)
            return self._static(rest)

        self._use_module()
        if path in ("/", "/index.html"):
            if self.base_cfg.get("courses_dir") is not None:
                # 🔴 `/` is the home page, full stop. It briefly redirected past
                # it when there was only one module, on the reasoning that a
                # one-card page was a click that existed only to be got past.
                # EH overruled that on 2026-08-16 ("takes me to the module
                # instead of Home"), and he was right: he had had to ask how to
                # reach the page at all, which is what a front door nobody can
                # find looks like. The card carries "continue where you left
                # off", so the click buys something.
                return self._home_page()
            path = "/index.html"
        return self._static(path)

    def do_POST(self):
        self._body_consumed = False
        if not self._host_ok():
            self._drain_body()
            return self._text("bad host\n", 403)
        if not self._auth_ok():
            self._drain_body()
            return self._json({"ok": False, "error": "token required"}, 401)
        path = urllib.parse.urlparse(self.path).path

        # 🔴 R9 is the one POST whose body is not JSON, so it is handled before
        # _body() runs. Reading it as JSON first would consume the bytes and then
        # fail on the first one, which is a confusing way for an upload to break.
        if path == "/api/resources":
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            self._use_module((query.get("module") or [None])[0])
            return self._upload(query)

        # 🔴 The same shape, and for the same reason: a lesson somebody sent you
        # arrives as a file, and until 2026-08-21 the only way in was a terminal
        # command printed on a page that also promised you would never need one.
        if path == "/api/share":
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            want = (query.get("module") or [None])[0]
            mid = self._use_module(want)
            self._drain_body()
            if want and mid != want:
                return self._json({"ok": False, "error":
                                   "no course called %r" % want}, 404)
            import lesson_packs
            dest = share_destination(self.cfg)
            # 🟢 The one component the page offers beyond the prose: the caption
            # cues, the lecturer's words with timings and nothing of the
            # reader's. OFF unless the checkbox says so, because forgetting a
            # box should under-share (the same default `--with-captions` has).
            # The manager's pick for the 2026-09-17 sharing entry.
            captions = (query.get("captions") or [""])[0] in ("1", "true", "yes")
            try:
                # 🔴 `with_drive` is left at its default of False: a shared course
                # carries the KEATS links (which gate on the recipient's own
                # enrolment) and NOT the exporter's own Drive addresses. EH's
                # ruling, 2026-08-28. There is deliberately no way to turn it on
                # from the page: the flag exists for `lesson_packs.py` copying
                # between one person's own installs.
                if captions:
                    zip_path, report = lesson_packs.export_course(
                        self.cfg, dest, contents={"captions": True})
                else:
                    zip_path, report = lesson_packs.export_course(self.cfg, dest)
            except lesson_packs.Problem as err:
                return self._json({"ok": False, "error": str(err)}, 400)
            log(self.cfg, "share %s -> %s" % (mid, zip_path))
            return self._json({"ok": True, "zip": str(zip_path),
                               "report": report})

        if path == "/api/import":
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            want = (query.get("module") or [None])[0]
            mid = self._use_module(want)
            return self._import_lesson(query, want, mid)

        # The other half of "somebody sent you a file": a list of lecture-video
        # links, which is the one thing a folder of slides and transcripts can
        # never contain. Same shape of request, same drop target on the page.
        if path == "/api/links":
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            want = (query.get("module") or [None])[0]
            mid = self._use_module(want)
            return self._import_links(query, want, mid)

        # 🔴 Restart takes no body, and every POST below is parsed as JSON before
        # the path is looked at, so `fetch('/api/restart', {method:'POST'})` came
        # back "bad request body" and nothing restarted. The Settings page's
        # Restart button had therefore never once worked, and it went unnoticed
        # because the page says "Restarting…" the moment it is clicked and the
        # health check that follows finds the OLD server answering perfectly.
        #
        # Found on 2026-08-21 by clicking it, in the same hour that changing the
        # courses folder made a working restart the thing the change depends on.
        # The same shape as the silent-restart trap in PROJECT-NOTES: a healthy
        # answer from the process you were trying to replace.
        if path == "/api/restart":
            self._drain_body()
            self._use_module(None)
            return self._restart()

        if path == "/api/captions":
            # 🔴 The only thing on this server that starts an hour of network
            # work, and it starts it in ITS OWN process group: an HTTP handler
            # must not hold a fetch, and restarting the server must not kill one
            # halfway through somebody's lecture.
            #
            # ⚠️ The module comes off the QUERY and is resolved before the body is
            # read, the same shape `/api/share` uses, because this POST has no
            # JSON body to carry it.
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            want = (query.get("module") or [None])[0]
            mid = self._use_module(want)
            self._drain_body()
            if want and mid != want:
                return self._json({"ok": False,
                                   "error": "no course called %r" % want}, 404)
            if not mid:
                return self._json({"ok": False, "error": "no course chosen"})
            return self._json(caption_ask(self.base_cfg, ["--start", mid],
                                          CAPTION_START_TIMEOUT))

        if path == "/api/captions/install":
            # The caption engine (torch and the forced aligner, a few hundred
            # MB) installed ONCE into ~/.kcl-study/captions-venv, in its own
            # process group exactly as a build is, and only on a person's
            # click: this is the second of the three verbs that start work
            # here. Course-independent, so no module.
            self._drain_body()
            self._use_module(None)
            return self._json(caption_ask(self.base_cfg, ["--install-start"],
                                          CAPTION_START_TIMEOUT))

        if path == "/api/packs/install":
            # The third, added 2026-09-18: the picture pack (44 MB, once,
            # shared by every course) fetched into the kit's own folder, in
            # its own process group like the two above, and only on a click.
            # The feed it fetches from is the config's update feed, so the
            # verb and the reader's update notice can never disagree about
            # where a release lives. Course-independent, so no module.
            self._drain_body()
            self._use_module(None)
            url = str(self.base_cfg.get("update_url") or "").strip()
            if not url:
                return self._json({"ok": False, "error": "the update feed is off "
                                   "in this config, so there is nowhere to fetch "
                                   "the pictures from"})
            return self._json(regionpack.start_install(url))

        try:
            payload = self._body()
        except (ValueError, UnicodeDecodeError):
            return self._json({"ok": False, "error": "bad request body"}, 400)

        self._use_module(payload.get("module") if isinstance(payload, dict) else None)

        try:
            if path == "/api/materials-source":
                out = write_materials_source(self.cfg, payload)
                log(self.cfg, "materials source -> %s (%s)"
                    % (out["source"], out["dir"]))
                return self._json({"ok": True, "report": out})

            if path == "/api/vault":
                out = write_vault(self.cfg, payload)
                log(self.cfg, "vault write %s -> %s" % (payload.get("doc"), out["file"]))
                return self._json(out)

            if path == "/api/explain":
                out = do_ask(self.cfg, payload, self.host_word())
                log(self.cfg, "ask %r ok=%s"
                    % (str(payload.get("question") or payload.get("term") or "")[:60], out.get("ok")))
                return self._json(out, 200 if out.get("ok") else 503)

            if path == "/api/annotations":
                doc = payload.get("doc", "")
                out = write_additions(self.cfg, doc, payload)
                log(self.cfg, "annotations %s count=%d" % (doc, out["count"]))
                return self._json(out)

            if path == "/api/marks":
                doc = payload.get("doc", "")
                if not DOC_ID_RE.match(doc or ""):
                    raise ValueError("bad doc id")
                out = write_marks(self.cfg, doc, payload)
                log(self.cfg, "marks %s items=%d notes=%d kept=%d"
                    % (doc, out["items"], out["notes"], out["kept"]))
                if doc == "READINGS":
                    # One vault note per reading (EH, 2026-08-22). 🔴 A vault
                    # failure must never fail the marks save: the marks are the
                    # precious thing and the vault copy is derived. Failures go
                    # to the log and into the reply, never into the status code.
                    try:
                        out["vault"] = publish_readings_vault(self.cfg)
                    except Exception as exc:
                        log(self.cfg, "readings vault publish FAILED: %s" % exc)
                        out["vault"] = {"ok": False, "error": str(exc)}
                    else:
                        v = out["vault"]
                        if v.get("written") or v.get("removed"):
                            log(self.cfg, "readings vault: %d written, %d cleared"
                                % (len(v.get("written", [])), len(v.get("removed", []))))
                return self._json(out)

            if path == "/api/colour-purge":
                out = colour_purge(self.cfg, payload)
                log(self.cfg, "palette purge %s removed=%d lessons=%d"
                    % (out["colour"], out["removed"], len(out["lessons"])))
                return self._json(out)

            if path == "/api/resources/remove":
                doc = payload.get("doc", "")
                out = delete_resource(self.cfg, doc, payload.get("name", ""))
                log(self.cfg, "resource removed %s/%s" % (doc, out["name"]))
                return self._json(out)

            if path == "/api/resources/about":
                doc = payload.get("doc", "")
                out = write_manifest(self.cfg, doc, payload.get("name", ""),
                                     title=payload.get("title"),
                                     note=payload.get("note"),
                                     order=payload.get("order"))
                log(self.cfg, "resource described %s/%s" % (doc, out["name"]))
                return self._json(out)

            if path == "/api/settings":
                out = write_settings(self.cfg, payload)
                log(self.cfg, "settings model=%s level=%s size=%s"
                    % (out["model"], out["level"], out["panelSize"]))
                return self._json(out)

            if path == "/api/machine":
                # The two paths that are facts about this machine, changed from
                # the Settings page rather than from a text editor. See
                # set_vault_path and set_courses_path for why this is careful.
                raw = read_raw_config()
                out = {"ok": True, "restart": False, "message": ""}
                if "vaultCourses" in payload:
                    msg, needs = set_vault_path(raw, payload["vaultCourses"])
                    out["message"], out["restart"] = msg, needs
                elif "coursesDir" in payload:
                    ans = payload.get("answer")
                    if ans not in (None, "create", "move", "leave"):
                        raise ValueError("answer must be create, move or leave")
                    out = set_courses_path(raw, payload["coursesDir"], ans,
                                           self.base_cfg.get("courses_dir"))
                    if not out.get("ok") or out.get("noop"):
                        # A question, or a change that is not a change. Either
                        # way nothing was written, so nothing is saved and no
                        # backup is made.
                        return self._json(out, 200)
                elif "apiKey" in payload or "askBackend" in payload:
                    # The route to Claude and the key for it. Written to the
                    # file AND into the running config, so the next question
                    # uses them: nothing here needs a restart. 🔴 The key is
                    # never in the reply, the status or the log line below;
                    # the log names the FIELD.
                    bits = []
                    if "askBackend" in payload:
                        bits.append(set_ask_backend(raw, payload["askBackend"]))
                        self.base_cfg["ask_backend"] = raw["ask_backend"]
                    if "apiKey" in payload:
                        bits.append(set_api_key(raw, payload["apiKey"]))
                        self.base_cfg["api_key"] = raw["api_key"]
                    out["message"] = " ".join(bits)
                    out.update(explain_status(self.base_cfg, self.host_word()))
                else:
                    raise ValueError("nothing to change")
                backup = save_raw_config(raw)
                log(self.cfg, "machine config changed (%s), previous kept at %s"
                    % (", ".join(sorted(k for k in ("vaultCourses", "coursesDir",
                                                    "apiKey", "askBackend")
                                        if k in payload)), backup.name))
                out["backup"] = backup.name
                return self._json(out)

            if path == "/api/module":
                # Rename a course. Separate from /api/modules (which creates
                # one) because creating and renaming are different rights over
                # different fields: this one cannot touch the code.
                root = self.base_cfg.get("courses_dir")
                if root is None:
                    raise ValueError("this install holds one course, which has "
                                     "its name in its own settings file")
                mid = str(payload.get("id") or "").strip()
                name = rename_module(root, mid, payload.get("name"))
                log(self.cfg, "course renamed %s -> %r" % (mid, name))
                return self._json({"ok": True, "id": mid, "name": name})

            if path == "/api/modules":
                # Add a course from the home page. Creating the folder is all
                # this does: the lessons arrive afterwards, by import, by the
                # download skill, or by being written.
                root = self.base_cfg.get("courses_dir")
                if root is None:
                    raise ValueError(
                        "this install holds one course and has no courses "
                        "folder to add another to")
                # 🔴 EH, 2026-08-30: "Currently, the name is
                # optional. It should not be." Enforced HERE rather than in
                # `create_module`, deliberately: `lesson_packs.py` calls that
                # with no name at all, because importing a pack into a course
                # that does not exist is the kit's whole first-run path and
                # there is nobody there to ask. A rule in the form alone would
                # hold only in the browser; a rule in `create_module` would
                # break the kit.
                name = str(payload.get("name") or "").strip()
                if not name:
                    raise ValueError("a course needs a name")
                # ⚠️ Absent key and empty string are DIFFERENT here, so this
                # cannot use `payload.get(...) or ""`: that would turn "the
                # form did not send it" and "the reader cleared the box" into
                # one value, which is exactly the distinction the field exists
                # to keep. See `create_module`.
                link = payload.get("project_link")
                folder = create_module(root, str(payload.get("id") or "").strip(),
                                       name,
                                       str(payload.get("class_name") or "").strip(),
                                       project_link=(None if link is None
                                                     else str(link).strip()))
                _MODULE_CACHE.clear()      # the listing is cached by mtime
                mid = folder.name
                log(self.cfg, "course created %s" % mid)
                return self._json({"ok": True, "id": mid,
                                   "url": module_url(self.base_cfg, mid)})

            if path == "/api/chatmarks":
                doc = payload.get("doc", "")
                if not DOC_ID_RE.match(doc or ""):
                    raise ValueError("bad doc id")
                out = write_chatmarks(self.cfg, doc, payload)
                log(self.cfg, "chatmarks %s marks=%d chats=%d"
                    % (doc, out["marks"], out["chats"]))
                return self._json(out)

            if path == "/api/bookmarks":
                doc = payload.get("doc", "")
                if not DOC_ID_RE.match(doc or ""):
                    raise ValueError("bad doc id")
                out = write_bookmarks(self.cfg, doc, payload)
                log(self.cfg, "bookmarks %s count=%d" % (doc, out["marks"]))
                return self._json(out)

            if path == "/api/cards":
                doc = payload.get("doc", "")
                if not DOC_ID_RE.match(doc or ""):
                    raise ValueError("bad doc id")
                out = write_cards(self.cfg, doc, payload)
                log(self.cfg, "cards %s count=%d adopted=%d"
                    % (doc, out["cards"], out["adopted"]))
                return self._json(out)

            if path == "/api/visit":
                out = record_visit(self.cfg, payload.get("doc", ""))
                log(self.cfg, "visit %s count=%d" % (out["doc"], out["count"]))
                return self._json(out)

            if path == "/api/lessonstate":
                out = write_lesson_state(self.cfg, payload.get("doc", ""),
                                         payload)
                log(self.cfg, "lessonstate %s %s" % (out["doc"], out["state"]))
                return self._json(out)

            if path == "/api/chats":
                doc = payload.get("doc", "")
                if not DOC_ID_RE.match(doc or ""):
                    raise ValueError("bad doc id")
                out = write_chats(self.cfg, doc, payload)
                log(self.cfg, "chats %s count=%d adopted=%d"
                    % (doc, out["chats"], out.get("adopted", 0)))
                return self._json(out)

            if path == "/api/followup":
                out = do_ask(self.cfg, payload, self.host_word())
                log(self.cfg, "followup %r ok=%s"
                    % (str(payload.get("question", ""))[:60], out.get("ok")))
                return self._json(out, 200 if out.get("ok") else 503)

            if path == "/api/rewrite":
                out = do_rewrite(self.cfg, payload, self.host_word())
                log(self.cfg, "rewrite %s block=%s ok=%s"
                    % (payload.get("doc"), payload.get("b"), out.get("ok")))
                return self._json(out, 200 if out.get("ok") else 503)

            if path == "/api/edit":
                out = apply_edit(self.cfg, payload)
                log(self.cfg, "edit %s block=%s how=%s backup=%s"
                    % (payload.get("doc"), payload.get("b"),
                       payload.get("how"), out["backup"]))
                return self._json(out)

        except ValueError as err:
            return self._json({"ok": False, "error": str(err)}, 400)
        except OSError as err:
            log(self.cfg, "ERROR %s: %s" % (path, err))
            return self._json({"ok": False, "error": str(err)}, 500)

        return self._json({"ok": False, "error": "no such route"}, 404)

    def _api_get(self, path, query):
        if path == "/api/status":
            st = explain_status(self.cfg, self.host_word())
            return self._json({
                "ok": True,
                "explain": st["explain"],
                "backend": st["backend"],
                "explainWhy": st["explainWhy"],
                "model": self.cfg.get("explain_model"),
                "vaultEnabled": vault_on(self.cfg),
                "vault": str(self.cfg["vault_courses"]),
                "vaultName": vault_display_name(self.cfg),
                "vaultOk": (vault_on(self.cfg)
                            and self.cfg["vault_courses"].parent.is_dir()),
                "glossary": glossary_path(self.cfg).exists(),
            })

        if path == "/api/lookup":
            term = (query.get("q") or [""])[0]
            return self._json(do_lookup(self.cfg, term))

        if path == "/api/chatmarks":
            doc = (query.get("doc") or [""])[0]
            if not DOC_ID_RE.match(doc or ""):
                return self._json({"ok": False, "error": "bad doc id"}, 400)
            return self._json(read_chatmarks(self.cfg, doc))

        if path == "/api/bookmarks":
            doc = (query.get("doc") or [""])[0]
            if not DOC_ID_RE.match(doc or ""):
                return self._json({"ok": False, "error": "bad doc id"}, 400)
            return self._json(read_bookmarks(self.cfg, doc))

        if path == "/api/cards":
            doc = (query.get("doc") or [""])[0]
            if not DOC_ID_RE.match(doc or ""):
                return self._json({"ok": False, "error": "bad doc id"}, 400)
            return self._json(read_cards(self.cfg, doc))

        if path == "/api/visits":
            return self._json(visits_summary(self.cfg))

        if path == "/api/resources":
            doc = (query.get("doc") or [""])[0]
            try:
                return self._json(read_resources(self.cfg, doc))
            except ValueError as err:
                return self._json({"ok": False, "error": str(err)}, 400)

        if path == "/api/materials":
            doc = (query.get("doc") or [""])[0]
            try:
                return self._json(read_materials(self.cfg, doc))
            except ValueError as err:
                return self._json({"ok": False, "error": str(err)}, 400)

        if path == "/api/annotations":
            doc = (query.get("doc") or [""])[0]
            try:
                return self._json(read_additions(self.cfg, doc))
            except ValueError as err:
                return self._json({"ok": False, "error": str(err)}, 400)

        if path == "/api/marks":
            doc = (query.get("doc") or [""])[0]
            try:
                return self._json(read_marks(self.cfg, doc))
            except ValueError as err:
                return self._json({"ok": False, "error": str(err)}, 400)

        if path == "/api/settings":
            return self._json(read_settings(self.cfg))

        if path == "/api/materials-source":
            return self._json({"ok": True,
                               "report": local_materials_report(self.cfg)})

        if path == "/api/colour-uses":
            c = (query.get("c") or [""])[0]
            if not PALETTE_ID_RE.match(c or ""):
                return self._json({"ok": False, "error": "bad colour id"}, 400)
            return self._json(colour_uses(self.cfg, c))

        if path == "/api/chats":
            doc = (query.get("doc") or [""])[0]
            try:
                return self._json(read_chats(self.cfg, doc))
            except ValueError as err:
                return self._json({"ok": False, "error": str(err)}, 400)

        return self._json({"ok": False, "error": "no such route"}, 404)

    def _upload(self, query):
        """R9. One file, raw bytes in the body, name in the query string.

        🔴 Raw bytes rather than multipart/form-data, deliberately. A multipart
        parser is a hundred lines of boundary handling that this server would own
        forever, and `fetch(url, {method:'POST', body: file})` sends the file
        exactly as it is with no encoding step, so the browser side is simpler
        too. The name has to travel somewhere, and the query string is the one
        place that survives a proxy without a custom header.
        """
        doc = (query.get("doc") or [""])[0]
        name = (query.get("name") or [""])[0]
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._json({"ok": False, "error": "bad content length"}, 400)
        if length <= 0:
            return self._json({"ok": False, "error": "no file was sent"}, 400)
        if length > RESOURCE_MAX_BYTES:
            # Refuse before reading it, so a 2GB body is not pulled into memory
            # to be told it is too big.
            return self._json({"ok": False, "error":
                               "too big: %.1f MB, and the limit is %d MB"
                               % (length / 1048576.0, RESOURCE_MAX_BYTES // 1048576)}, 413)
        try:
            data = self.rfile.read(length)
        except OSError:
            return self._json({"ok": False, "error": "the upload was cut off"}, 400)

        try:
            out = write_resource(self.cfg, doc, name, data)
        except ValueError as err:
            return self._json({"ok": False, "error": str(err)}, 400)
        except OSError as err:
            return self._json({"ok": False, "error": "could not write it: %s" % err}, 500)
        log(self.cfg, "resource added %s/%s (%d bytes)" % (doc, out["name"], out["size"]))
        return self._json(out)

    def _restart(self):
        """Replace this process with a fresh one, reading the config again.

        Its own method because it is dispatched before the JSON body is read:
        it is the one POST that carries nothing, and parsing a body it does not
        have is what stopped it working."""
        why = restart_guard()
        if why:
            log(self.cfg, "restart REFUSED, file will not parse: %s" % why)
            return self._json(
                {"ok": False,
                 "error": "This file will not parse, so restarting would take the "
                          "server down with no way to bring it back. " + why},
                409)
        log(self.cfg, "restart requested")
        self._json({"ok": True, "pid": os.getpid()})
        try:
            self.wfile.flush()
        except Exception:
            pass
        # Answer first, then replace the process. The delay is for the response
        # to reach the browser, which the exec would otherwise cut off.
        threading.Timer(0.5, do_restart, args=(self.cfg,)).start()
        return

    def _import_lesson(self, query, wanted_module, module_id):
        """Put a lesson pack into this course, sent from the browser.

        🔴 Reported 2026-08-21: a course with no lessons in it told you to
        "import a lesson somebody sent you" and gave you nothing to do it with.
        No button, no folder to drop the file in, no instructions. The route
        existed the whole time as `lesson_packs.py --import`, which is a terminal
        command on a page belonging to a product whose promise is that you never
        need one.

        Raw bytes with the name in the query string, exactly as `_upload` does,
        and the same reasoning: no multipart parser to own.

        The import itself is `lesson_packs.import_packs`, unchanged and shared
        with the command line, so a lesson that arrives this way and a lesson
        that arrives that way cannot land differently. It is the function that
        knows a pack from a stray file, backs up anything it replaces, and folds
        the pack's links into `materials.json`."""
        import tempfile
        import lesson_packs

        # 🔴 Every early return below drains first. This server speaks keep-alive,
        # and a refused POST whose body is left on the socket makes the NEXT
        # request parse from the middle of this one (the 2026-08-16 finding in
        # _drain_body). An upload is the largest body this server ever refuses,
        # so it is the one most able to wreck the connection behind it.
        #
        # 🔴 The named course has to be the course it lands in. Caught by my own
        # test on 2026-08-21, before this shipped: `_use_module` FALLS BACK when
        # it cannot resolve what it was given, so a pack aimed at a course that
        # does not exist was accepted with a cheerful 200 and filed into whichever
        # course happened to be the default. A silent wrong-course import is worse
        # than a refusal, because the lesson is somewhere, just not where anybody
        # will look. A fallback is right for reading a page and wrong for writing
        # a file.
        if wanted_module and module_id != wanted_module:
            self._drain_body()
            return self._json({"ok": False, "error":
                               "there is no course called %r on this machine, so "
                               "nothing was written." % wanted_module}, 404)
        if self.base_cfg.get("courses_dir") is not None and module_id is None:
            self._drain_body()
            return self._json({"ok": False, "error":
                               "which course is this lesson for? The page did not "
                               "say, so nothing was written."}, 400)

        # 🔴 The name is NOT what decides which kind of file this is. It used to
        # be, and that made a promise on the course page ("this works out which
        # kind you have") into a lie the moment somebody saved their links as
        # `videos.html`. The CONTENT decides, below; the name only ever becomes a
        # filename. `..` and separators go with basename, and the rest is rebuilt
        # from safe characters rather than trusted.
        name = os.path.basename((query.get("name") or [""])[0].strip())
        safe = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-.") or "lesson.html"
        if not safe.lower().endswith(".html"):
            # Drop a misleading extension rather than stacking on top of it:
            # a lesson saved as `lesson.json` should land as `lesson.html`, not
            # as `lesson.json.html`, which looks like something went wrong.
            safe = re.sub(r"\.(json|txt|md|htm)$", "", safe, flags=re.I) + ".html"

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._drain_body()
            return self._json({"ok": False, "error": "bad content length"}, 400)
        if length <= 0:
            return self._json({"ok": False, "error": "no file was sent"}, 400)
        if length > LESSON_MAX_BYTES:
            # Refused before it is read, so an enormous body is not pulled into
            # memory to be told it is too big. Nothing is left readable after
            # that, so the connection ends here rather than desynchronising.
            self.close_connection = True
            return self._json({"ok": False, "error":
                               "too big: %.1f MB, and a lesson should be under %d MB"
                               % (length / 1048576.0, LESSON_MAX_BYTES // 1048576)}, 413)
        try:
            data = self.rfile.read(length)
        except OSError:
            self.close_connection = True
            return self._json({"ok": False, "error": "the upload was cut off"}, 400)
        self._body_consumed = True

        folder = self.cfg["notes_dir"]
        force = (query.get("force") or [""])[0] in ("1", "true", "yes")

        # 🔴 The page promises "this works out which kind of file you have", and
        # a promise on a page is a promise the code has to keep. Routing by file
        # extension is right nearly always and wrong exactly when somebody saved
        # their links as `videos.html` or their lesson as `lesson.txt`. So each
        # importer checks what it actually got and hands over rather than
        # refusing with a technicality the person cannot act on.
        if data[:4] == b"PK\x03\x04":
            # A whole shared course as one dragged file (EH's design,
            # 2026-08-28): lesson packs plus the course pack, zipped by the
            # Share button on the other person's setup page.
            return self._import_zip(data, name, force)
        if split_lessons.META_OPEN not in data[:8192].decode("utf-8", "replace"):
            handed = self._maybe_course_pack(data)
            if handed is None:
                handed = self._maybe_readings(data)
            if handed is None:
                handed = self._maybe_links(data)
            if handed is not None:
                return handed
            # Neither, so say what each of them looks like. "Not a lesson pack"
            # on its own leaves somebody who dropped the wrong thing with no
            # idea what the right thing would have been.
            return self._json({"ok": False, "error":
                               "%s is none of the things this takes. A shared "
                               "course is one .zip; a lesson is one .html file; "
                               "a video list and a set of core readings are "
                               ".json files your Claude session writes."
                               % (name or "that file")}, 400)

        with tempfile.TemporaryDirectory() as tmp:
            staged = Path(tmp) / safe
            try:
                staged.write_bytes(data)
            except OSError as err:
                return self._json({"ok": False, "error":
                                   "could not stage it: %s" % err}, 500)
            try:
                done, skipped, merged, _ = lesson_packs.import_packs(
                    {"notes_dir": str(folder)}, [str(staged)],
                    force=force, quiet=True)
            except lesson_packs.Problem as err:
                return self._json({"ok": False, "error": str(err)}, 400)
            except (OSError, ValueError) as err:
                return self._json({"ok": False, "error":
                                   "could not import it: %s" % err}, 500)

        _MODULE_CACHE.clear()
        if skipped and not done:
            # The only reason import_packs skips is that the lesson is already
            # here, and that is a question rather than a failure: it is the one
            # case where the answer might be "yes, replace it".
            return self._json({"ok": False, "exists": True,
                               "error": skipped[0][1],
                               "name": skipped[0][0]}, 409)
        log(self.cfg, "lesson imported %s into %s (%d links merged)"
            % (safe, folder.name, merged))
        return self._json({"ok": True, "imported": [t.name for _, t, _, _ in done],
                           "docs": [d for _, _, d, _ in done],
                           "links": merged})

    def _maybe_course_pack(self, data):
        """A dropped course pack: the glossary, readings and mistakes that ride
        beside a shared course's lessons. Recognised by its own marker, never
        by filename."""
        import lesson_packs
        try:
            doc = json.loads(data.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None
        if not (isinstance(doc, dict) and doc.get("course_pack")):
            return None
        counts = lesson_packs.import_course_pack(self.cfg["notes_dir"], doc)
        _MODULE_CACHE.clear()
        # ⚠️ Core ideas are named only when some arrived. The other three are
        # always printed because a zero there is informative (the pack carried
        # no glossary); a zero here would report on a feature most packs will
        # not use for a while yet.
        msg = ("In: %d glossary terms, %d readings, %d mistakes."
               % (counts["glossary"], counts["readings"], counts["mistakes"]))
        if counts.get("core_ideas"):
            msg = msg[:-1] + (", core ideas for %d week%s or topic%s."
                              % (counts["core_ideas"],
                                 "" if counts["core_ideas"] == 1 else "s",
                                 "" if counts["core_ideas"] == 1 else "s"))
        return self._json({"ok": True, "kind": "course-pack", "counts": counts,
                           "message": msg})

    def _import_zip(self, data, name, force=False):
        """A shared course, one dragged file. Unpacked by
        `lesson_packs.unpack_shared_zip` (every member flat under its basename,
        except a `captions/<DOC>/` cue file which keeps that one level, so a
        hostile path cannot leave the temp folder), then imported by the same
        `import_packs` the command line uses: lessons, the `captions/` tree and
        the course pack, in one call.

        🔴 Until 2026-09-17 the captions never arrived. This unpacked every
        member to its basename, which threw away `captions/<DOC>/`, and then
        swallowed `import_packs`'s refusal in a bare `except: pass`, so a zip of
        nothing this recognised reported "0 lessons in" as a success. Now a
        refusal is a 400 that says why."""
        import io
        import tempfile
        import zipfile
        import lesson_packs
        counts = {"lessons": 0, "skipped": 0, "links": 0,
                  "glossary": 0, "readings": 0, "mistakes": 0, "core_ideas": 0,
                  "captions": 0, "captions_skipped": 0}
        try:
            with tempfile.TemporaryDirectory() as tmp:
                tmpdir = Path(tmp)
                try:
                    members = lesson_packs.unpack_shared_zip(
                        io.BytesIO(data), tmpdir, max_bytes=LESSON_MAX_BYTES)
                except lesson_packs.Problem as err:
                    return self._json({"ok": False, "error": str(err)}, 400)
                if not members:
                    return self._json({"ok": False,
                                       "error": "that zip is empty"}, 400)
                try:
                    done, skipped, merged, extras = lesson_packs.import_packs(
                        {"notes_dir": str(self.cfg["notes_dir"])},
                        [str(tmpdir)], force=force, quiet=True)
                except lesson_packs.Problem as err:
                    # The refusal names the folder it looked in, which here is
                    # a temp directory nobody can open: say the zip's name.
                    return self._json({"ok": False, "error": str(err).replace(
                        str(tmpdir), name or "that zip")}, 400)
                counts["lessons"], counts["skipped"] = len(done), len(skipped)
                counts["links"] = merged
                counts["captions"] = len(extras["captions"])
                counts["captions_skipped"] = len(extras["captions_skipped"])
                for k in ("glossary", "readings", "mistakes", "core_ideas"):
                    counts[k] += extras["course"][k]
        except zipfile.BadZipFile:
            return self._json({"ok": False,
                               "error": "%s is not a zip this can read"
                               % (name or "that file")}, 400)
        _MODULE_CACHE.clear()
        bits = ["%d lesson%s in" % (counts["lessons"],
                                    "" if counts["lessons"] == 1 else "s")]
        if counts["skipped"]:
            bits.append("%d already here and left alone" % counts["skipped"])
        if counts["glossary"] or counts["readings"] or counts["mistakes"]:
            bits.append("%d glossary terms, %d readings, %d mistakes"
                        % (counts["glossary"], counts["readings"],
                           counts["mistakes"]))
        if counts["core_ideas"]:
            bits.append("core ideas for %d week%s or topic%s"
                        % (counts["core_ideas"],
                           "" if counts["core_ideas"] == 1 else "s",
                           "" if counts["core_ideas"] == 1 else "s"))
        if counts["captions"]:
            bits.append("captions for %d lecture%s"
                        % (counts["captions"],
                           "" if counts["captions"] == 1 else "s"))
        if counts["captions_skipped"]:
            bits.append("captions for %d lecture%s already here and left alone"
                        % (counts["captions_skipped"],
                           "" if counts["captions_skipped"] == 1 else "s"))
        return self._json({"ok": True, "kind": "course-zip", "counts": counts,
                           "message": "; ".join(bits) + "."})

    def _maybe_readings(self, data):
        """If what arrived is a set of core-reading summaries, file them.

        Tried BEFORE the video links, because a readings file is the more
        specific shape: it must carry a `readings` key, where a links file is
        recognised generously and would otherwise swallow anything with an id in
        it. Order is the whole guard."""
        import readings as R
        try:
            records, complaints = R.parse(data.decode("utf-8", "replace"))
        except (R.Problem, ValueError):
            return None
        try:
            out = R.merge(self.cfg["notes_dir"], records)
        except (R.Problem, OSError, ValueError) as err:
            return self._json({"ok": False, "error": str(err)}, 500)
        log(self.cfg, "readings merged into %s: %d added, %d re-summarised"
            % (self.cfg["notes_dir"].name, out["added"], out["replaced"]))
        out["ok"] = True
        out["skipped"] = complaints
        out["readings"] = len(records)
        return self._json(out)

    def _maybe_links(self, data):
        """If what arrived is a list of lecture videos, merge it and say so.

        Returns a response, or None when this is not a links file and the caller
        should carry on with what it was doing."""
        import video_links
        try:
            records, complaints = video_links.parse(data.decode("utf-8", "replace"))
        except (video_links.Problem, ValueError):
            return None
        try:
            out = video_links.merge(self.cfg["notes_dir"], records)
        except (video_links.Problem, OSError, ValueError) as err:
            return self._json({"ok": False, "error": str(err)}, 500)
        log(self.cfg, "video links merged into %s (dropped as a lesson): %d fields"
            % (self.cfg["notes_dir"].name, out["added"]))
        out["ok"] = True
        out["skipped"] = complaints
        out["lectures"] = len(records)
        return self._json(out)

    def _import_links(self, query, wanted_module, module_id):
        """Merge a file of lecture-video links into this course.

        🔴 The collecting is not this program's job and cannot be: EH's
        correction on 2026-08-22 is that KEATS courses are not all built the same
        way, so a scraper would work on one and quietly mis-read the next. A
        person's own Claude session, driving their own browser, does the
        collecting, where a human can see when a page is shaped unexpectedly.
        What arrives here is its written-down result.

        `video_links.parse` is shared with the command line, so a file that
        arrives by drag and a file that arrives by terminal cannot land
        differently, and both are checked by the same rules."""
        import video_links

        if wanted_module and module_id != wanted_module:
            self._drain_body()
            return self._json({"ok": False, "error":
                               "there is no course called %r on this machine, so "
                               "nothing was written." % wanted_module}, 404)
        if self.base_cfg.get("courses_dir") is not None and module_id is None:
            self._drain_body()
            return self._json({"ok": False, "error":
                               "which course is this for? The page did not say, "
                               "so nothing was written."}, 400)
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._drain_body()
            return self._json({"ok": False, "error": "bad content length"}, 400)
        if length <= 0:
            return self._json({"ok": False, "error": "no file was sent"}, 400)
        if length > LINKS_MAX_BYTES:
            self.close_connection = True
            return self._json({"ok": False, "error":
                               "that is far too big for a list of links (%.1f MB)"
                               % (length / 1048576.0)}, 413)
        try:
            data = self.rfile.read(length)
        except OSError:
            self.close_connection = True
            return self._json({"ok": False, "error": "the upload was cut off"}, 400)
        self._body_consumed = True

        # The same courtesy in the other direction: a lesson dropped in as
        # `something.json` is a lesson, and saying so is more use than
        # complaining that its first line is not a doc id.
        if split_lessons.META_OPEN in data[:8192].decode("utf-8", "replace"):
            return self._json({"ok": False, "error":
                               "that is a lesson, not a list of video links. "
                               "Rename it to end in .html and drop it again."}, 400)
        try:
            records, complaints = video_links.parse(data.decode("utf-8", "replace"))
        except video_links.Problem as err:
            return self._json({"ok": False, "error": str(err)}, 400)
        try:
            out = video_links.merge(self.cfg["notes_dir"], records)
        except (video_links.Problem, OSError, ValueError) as err:
            return self._json({"ok": False, "error": str(err)}, 500)

        log(self.cfg, "video links merged into %s: %d fields, %d playable of %d"
            % (self.cfg["notes_dir"].name, out["added"], out["playable"], out["parts"]))
        out["ok"] = True
        out["skipped"] = complaints
        out["lectures"] = len(records)
        return self._json(out)

    def _resource(self, path):
        """R8. Serves one attached file, out of resources_dir and nothing else.

        Separate from _static because the roots are different and must stay that
        way. The path shape is fixed at /resources/<DOC>/<name>: anything deeper,
        shallower, or with a doc id that is not a doc id does not get looked up
        at all, so the containment check below is the second line of defence and
        not the first.
        """
        parts = [p for p in path.split("/") if p]
        if len(parts) != 3 or parts[0] != "resources":
            return self._text("not found\n", 404)
        target = resolve_resource(self.cfg, parts[1], parts[2])
        if target is None:
            return self._text("not found\n", 404)

        try:
            data = target.read_bytes()
        except OSError:
            return self._text("not found\n", 404)
        # An attached file is named by whoever attached it, so the same wrong
        # suffix that hit the materials pane hits this one. The sniff cannot
        # reach the two types the sandbox below is keyed on: see MAGIC_TYPES.
        ctype = content_type_for(target.name, data[:MAGIC_PEEK])
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        # 🔴 An attached file is content this server did not write and cannot vouch
        # for, and it is served from the same origin as the reader, which holds his
        # marks and can reach the whole API with his token. nosniff goes on
        # everything, so a mislabelled file is never re-guessed into something
        # executable.
        self.send_header("X-Content-Type-Options", "nosniff")
        self._sandbox_if_scriptable(ctype)
        self.end_headers()
        self.wfile.write(data)

    def _sandbox_if_scriptable(self, ctype):
        """The CSP for a file this server did not write, on the origin that
        holds EH's marks and can reach the whole API with his token.

        🔴 The sandbox goes ONLY on the two types that can carry script, and
        scoping it that way is a bug fix rather than a nicety: applied to
        everything it also broke the PDF viewer, which is a browser component
        that needs to run, so every attached paper displayed as a blank frame.
        `sandbox` with no allow-list is the strong form: an opaque origin, so an
        attached page cannot read localStorage, cannot use the token, and cannot
        call the API. allow-same-origin was in the first version and undoes
        exactly that, which is the opposite of the point.

        🔴 A METHOD rather than two copies of an `if`, because the two routes
        already disagreed once. QA, 2026-08-30: `_resource` sandboxed html and
        svg and `_course_material` did not, on the same origin, for the same
        file types, and the materials folder is a CONFIGURED path, so it is not
        necessarily a folder of PDFs a download skill wrote. Two routes on one
        origin disagreeing about whether the same types are dangerous is the
        shape that gets exploited later. Anything else serving a file this
        server did not write calls this, and then it cannot drift."""
        if ctype.startswith("text/html") or ctype == "image/svg+xml":
            self.send_header("Content-Security-Policy", "sandbox")

    def _captions(self, rest):
        """`/captions/<PART>/soundN.vtt`: one narration clip's cues, and nothing else.

        🔴 **`Access-Control-Allow-Origin: *` is REQUIRED here, not a courtesy.**
        The page that fetches these is the lecture package, which we serve with
        `Content-Security-Policy: sandbox allow-scripts` and no
        `allow-same-origin`, so it runs in an OPAQUE ORIGIN and every request it
        makes is cross-origin. Measured 2026-09-02: `fetch()` from inside that
        sandbox succeeds against a route that sends this header and cannot
        succeed against one that does not.

        🔴 **`no-store`, and the reason is a defect this project already carries
        an entry about.** A caption file changes whenever the aligner improves,
        and an hour of browser cache would serve yesterday's cues out of a file
        whose own mtime says it is current. That is the stale-deploy failure
        arriving through the cache instead of through a process, and the package
        route's injected document is `no-store` for the same reason.

        ⚠️ **Captions are generated on the machine that has the KCL materials and
        are never shipped**: `build_kit.py` refuses one, by name, and a test
        plants one to prove it. A recipient runs the generator over materials
        they downloaded themselves. EH's ruling, 2026-09-02.
        """
        parts = [p for p in rest.split("/") if p]
        if len(parts) != 3 or parts[0] != "captions" or ".." in parts:
            return self._text("not found\n", 404)
        if not parts[2].endswith(".vtt"):
            return self._text("not found\n", 404)
        root = (self.cfg["notes_dir"] / "captions").resolve()
        candidate = (self.cfg["notes_dir"] / Path(*parts)).resolve()
        if root != candidate and root not in candidate.parents:
            return self._text("forbidden\n", 403)
        if not candidate.is_file():
            # 🟢 A lecture with no usable transcript has NO file, on purpose: an
            # empty track is indistinguishable from a clip nobody has run. The
            # reader treats a 404 as "no captions for this clip".
            return self._text("not found\n", 404)
        try:
            data = candidate.read_bytes()
        except OSError:
            return self._text("not found\n", 404)
        self.send_response(200)
        self.send_header("Content-Type", "text/vtt; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def _range_not_satisfiable(self, size):
        """The 416 both media routes answer, in one place.

        🔴 **`Content-Length: 0` is the whole reason this is a method.** Without
        it a 416 carries no body and no length, so an HTTP/1.1 client keeps the
        connection open waiting for one: **measured on the live server, `curl`
        received the 416 and then hung until its own timeout.** It was latent on
        the videos route for a year because an unsatisfiable range is rare there;
        putting range handling on the package route put it in front of every
        reader.

        ⚠️ **Sharing `parse_range` fixed the DECISION and left the RESPONSE
        duplicated**, which is exactly the "second place for the 416 edge to be
        wrong" the ruling warned about, one layer along from where I looked."""
        self.send_response(416)
        self.send_header("Content-Range", "bytes */%d" % size)
        self.send_header("Content-Length", "0")
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()

    def _package(self, rest, query=None):
        """One file of a mirrored slide package, out of packages/ and nothing else.

        A package is an iSpring HTML export fetched whole from the course site,
        so this is the one place the server serves third-party HTML that has to
        RUN: the player is script, and sandboxing it away would leave a grey
        rectangle. The header below is the compromise that keeps it honest:

        🔴 `sandbox allow-scripts` WITHOUT allow-same-origin. The player runs,
        but in an opaque origin: it cannot read localStorage, cannot see the
        token, cannot call this server's API as the reader. It is the same
        stance _resource takes on attached files, weakened by exactly one grant
        because a package that cannot run is not a view of anything.

        The `Access-Control-Allow-Origin: *` is a consequence of that choice,
        not a separate one: from an opaque origin every load is cross-origin,
        and fonts are the one subresource browsers refuse without CORS consent.
        It goes on package files only, which are course material this server
        already shows to anyone who can reach it.

        🟢 **`?lecture_rate=` rebuilds the timeline.** One of `PLAYBACK_RATES`,
        cleaned by the same function the audio control uses, and **absent or 1
        means the mirrored bytes are served untouched.** See the block below and
        `timeline.rebuild` for why a speed is a rebuild rather than a setting."""
        parts = [p for p in rest.split("/") if p]
        if len(parts) < 3 or parts[0] != "packages" or ".." in parts:
            return self._text("not found\n", 404)
        root = (self.cfg["notes_dir"] / "packages").resolve()
        candidate = (self.cfg["notes_dir"] / Path(*parts)).resolve()
        if root != candidate and root not in candidate.parents:
            return self._text("forbidden\n", 403)
        if not candidate.is_file():
            return self._text("not found\n", 404)
        ctype = CONTENT_TYPES.get(candidate.suffix.lower(), "application/octet-stream")
        try:
            data = candidate.read_bytes()
        except OSError:
            return self._text("not found\n", 404)
        # 🔴 THE PACKAGE'S OWN DOCUMENT IS THE ONLY PLACE A CONTROL CAN GO,
        # and that is the sandbox's doing rather than a preference. The package
        # runs in an OPAQUE ORIGIN (see the header below), so the lesson page
        # around it cannot reach into it: a speed control in the chrome could
        # not touch those `<audio>` elements, and a skip button could not call
        # the player. Injecting here keeps the control inside the same sandbox,
        # which is the property the sandbox exists for.
        #
        # 🟢 Every failure serves the package UNCHANGED: a bad decode, a
        # missing anchor, an unreadable controls file. A lecture that will not
        # render is worse than a lecture with no speed control.
        injected = False
        rebuilt = False
        # 🔴 THE LECTURE SPEED IS A REBUILT PACKAGE, and it arrives in the URL
        # rather than in the settings file. ⚠️ SINCE 2026-09-05 THIS IS THE
        # BACKUP MECHANISM: the clock below (plans/11) is what the reader's page
        # asks for, and this whole rebuild runs only when it does not. The
        # reasoning stays because the mechanism stays. That is forced rather than chosen:
        # the player has no rate property at all and `playbackRate` appears zero
        # times in its 1.67MB, so every timing a lecture is driven by is a
        # literal number inside `presInfo`. Changing the speed means handing the
        # loader different numbers, and the loader reads them ONCE, at boot.
        #
        # 🟢 So the rate belongs to the LOAD, which is what makes EH's
        # "lock it once you start playing" the right design rather than a
        # limitation: the reload a rebuild costs is paid at a moment he chose,
        # before he is watching, and never mid-lecture.
        #
        # 🔴 AND THE VOICE MOVES WITH IT, which is not an extra feature: EH asked
        # for a control over "the whole lecture, slides AND voice". A rebuild
        # alone would run the slides fast over a voice at 1x, which is the defect
        # he reported (voice ahead of slides) in mirror image, and shipping it
        # backwards would be worse than not shipping it. So a scaled package
        # starts its narration at the same rate.
        #
        # 🔴 SINCE 2026-09-05 NIGHT, AT RATE 1 TOO. Until then a lecture served
        # at 1 booted its voice at the saved `playbackRate` (the recording's
        # "Speed"), because the reader's row had a voice-only picker that showed
        # and could move it. EH removed that picker (*"we have a comprehensive
        # speed change that works"*), so a saved 1.5 would have left the
        # narration out of step with the slides with nothing on the page to say
        # so. The voice of a lecture now boots at the lecture rate, ALWAYS, and
        # this route no longer reads the settings file at all: `playbackRate`
        # is a recording's preference and nothing else.
        #
        # ⚠️ No rate in the URL means nothing here runs: `timeline.rebuild`
        # returns its argument at rate 1, and the bytes are today's bytes.
        lecture_rate = clean_rate(((query or {}).get("lecture_rate") or [None])[0])
        # 🟢 THE CLOCK, SINCE 2026-09-05 (plans/11), AND THE REBUILD ABOVE IS
        # KEPT INTACT AS ITS BACKUP. EH: *"Don't delete what we currently have
        # because it works relatively well. Let's save it as a backup but go
        # ahead and build this."* The two mechanisms never meet in one document:
        # `?clock_rate=` present means the shim goes in and the blob is NOT
        # rebuilt, whatever `?lecture_rate=` says beside it, because a scaled
        # blob under a scaled clock is a lecture at the SQUARE of the rate.
        # Absent, and every line below runs exactly as it did before the clock
        # existed. The reader's page chooses which query to send
        # (`LECTURE_SPEED_BY_CLOCK` in `local-layer.html`), so the way back is
        # one word there and nothing here.
        clock_raw = ((query or {}).get(PACKAGE_CLOCK_QUERY) or [None])[0]
        by_clock = clock_raw is not None
        clock_rate = clean_rate(clock_raw)
        clocked = False
        if ctype.startswith("text/html"):
            try:
                original = data.decode("utf-8")
                text = original
                # 🟢 On the PRISTINE document, and in its own guard. The two
                # edits are independent regions (the blob, and the content div),
                # and a rebuild that fails must still leave the reader the strip,
                # exactly as a strip that fails still leaves them the lecture.
                if by_clock:
                    try:
                        text = inject_player_clock(text, read_reader_part(CLOCK_PATH))
                        clocked = text is not original
                    except Exception:
                        text, clocked = original, False
                else:
                    try:
                        text = timeline.rebuild(text, lecture_rate)
                        rebuilt = text is not original
                    except Exception:
                        text, rebuilt = original, False
                # The rate this document will PLAY at: what was done, never
                # what was asked. A clock that could not be placed and a
                # rebuild that raised both leave a lecture at 1.
                played = clock_rate if clocked else (lecture_rate if rebuilt else 1)
                # 🔴 THE SAME NUMBER FOR BOTH TOKENS, and what was DONE, not
                # what was asked. The voice token is the lecture rate because
                # the voice of a lecture has no control of its own any more
                # (the long comment above, 2026-09-05); the lecture token is
                # the rate the timeline was actually built at, because a rate
                # that was asked for and whose rebuild then raised leaves a
                # rate-1 timeline, and telling the page otherwise is exactly
                # the out-of-step state the token exists to close. On the clock
                # it is stronger: this is the number the strip SETS the clock
                # to. `read_settings` is not called here at all: it raises
                # `KeyError` on a `cfg` without `explain_model`, and a package
                # needs nothing from disk.
                out = inject_player_controls(
                    text, read_reader_part(PLAYER_PATH), played, played)
                injected = out is not text
                # 🔴 `original`, not `text`. A package whose anchor is missing
                # gets no strip, and comparing against `text` there would throw
                # the REBUILD (or the clock) away too and serve the lecture at
                # 1x with nothing saying so.
                if out is not original:
                    data = out.encode("utf-8")
            # 🔴 EVERY exception, and that is the rule rather than a shrug.
            # The first version listed the three I could think of
            # (`UnicodeDecodeError`, `OSError`, `ValueError`) and an existing
            # test found the fourth within the hour: reading the saved rate
            # needs a full settings read, and a `cfg` without `explain_model`
            # raises `KeyError` straight out through the handler, so a lecture
            # did not serve AT ALL. Enumerating the failures I imagined is
            # exactly the mistake this whole block exists to prevent; the
            # requirement is "the package always serves", and that is a rule
            # about the outcome, not a list of causes.
            except Exception:
                pass
        # 🔴 RANGE, and it is why a lecture used to jump back to the start of a
        # slide. Chrome will not seek inside a media resource whose server
        # refuses ranges: it restarts it from byte 0, and each slide of a
        # narrated lecture is its own mp3, so byte 0 IS the start of that slide.
        # EH reported it as the export's progress bar restarting the audio; the
        # same export served by KEATS to the same browser was fine, which is what
        # put the cause on our side of the wire.
        #
        # 🟢 Shared with `_course_video` through `parse_range` rather than copied.
        # That route carried the whole reasoning in its docstring and the
        # identical argument was never extended to a package's per-slide audio.
        #
        # ⚠️ The size is the length of what is SERVED, not of the file on disk,
        # because an injected document is a different representation from the
        # bytes behind it. A media element never sends Range for HTML, but a
        # Content-Range naming the wrong total would be a lie either way.
        size = len(data)
        got = parse_range(self.headers.get("Range", ""), size)
        if got is None:
            return self._range_not_satisfiable(size)
        start, end, status = got
        if status == 206:
            data = data[start:end + 1]
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Accept-Ranges", "bytes")
        if status == 206:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        # A mirrored file never changes in place (a re-mirror replaces the
        # folder), and a narration MP3 re-fetched on every slide is the reader
        # feeling slow for no reason.
        #
        # 🔴 EXCEPT the one we compose. An injected document is no longer
        # the mirrored file: it changes whenever the controls change, and an
        # hour of browser cache would serve yesterday's strip out of a file
        # whose own mtime says it is current. That is the stale-deploy failure
        # this project already has a build id for, arriving through the cache
        # instead of through a process.
        # 🔴 `rebuilt` as well as `injected`. A rebuilt document is not the
        # mirrored file either, and while the rate sits in the query string (so
        # each speed is its own cache key), an hour of browser cache on a
        # composed representation is the stale-deploy failure this project
        # already has a build id for.
        self.send_header("Cache-Control",
                         "no-store" if (injected or rebuilt) else "public, max-age=3600")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Access-Control-Allow-Origin", "*")
        if ctype.startswith("text/html") or ctype == "image/svg+xml":
            self.send_header("Content-Security-Policy", "sandbox allow-scripts")
        self.end_headers()
        self.wfile.write(data)

    def _vendor_asset(self, rest):
        """`<version>/<name>` out of server/reader/vendor, and nothing else.

        🔴 The name is checked against a LIST rather than sanitised. There are
        three of them and there will not be many more, so an allow-list costs
        nothing and cannot be got wrong; a traversal check on a route serving
        this server's own directory is the one that has to be right forever."""
        parts = [x for x in rest.split("/") if x]
        want = vendor_version()
        if len(parts) != 2 or parts[1] not in VENDOR_FILES:
            return self._text("not found\n", 404)
        if not want or parts[0] != want:
            # A page composed against an older pin asking for a file that has
            # been upgraded under it. Loudly nothing, rather than quietly the
            # wrong bytes: reloading the page fixes it, and the reader says so.
            return self._text("not found\n", 404)
        try:
            data = (VENDOR_DIR / parts[1]).read_bytes()
        except OSError:
            return self._text("not found\n", 404)
        self.send_response(200)
        # 🔴 Suffix only, deliberately. This route serves a THREE-NAME allow-list
        # of files this project vendored and hash-audits at build time, and every
        # one of those names carries a suffix the table knows, so the first-bytes
        # sniff could never fire here anyway. QA, 2026-08-30: it was calling
        # `content_type_for` and so counted as a third sniffing route, which made
        # the guard test's scope sentence untrue. An unreachable branch on a route
        # that serves executable JavaScript is not worth keeping to save a line.
        self.send_header("Content-Type",
                         CONTENT_TYPES.get(Path(parts[1]).suffix.lower(),
                                           "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _pack_image(self, rest):
        """`<version>/<file>` out of the brain-region pack, and nothing else.

        🔴 THE NAME IS CHECKED AGAINST THE PACK'S OWN CROSS-REFERENCE, not
        sanitised and not resolved. A route that can only serve names
        `regions.json` already points at cannot be walked out of whatever
        arrives in the path, and it also cannot serve something dropped into the
        images directory by hand. That is the same doctrine as
        `_vendor_asset`'s three-name allow-list, applied to a list that is 47
        long and comes from data.

        ⚠️ A version that does not match is 404, not a redirect: a page composed
        against an older pack asking for a plate that has been rebuilt under it
        gets loudly nothing, and a reload fixes it.
        """
        # 🔴 NOT UNQUOTED AGAIN HERE, and this line is the whole reason the
        # comment exists. `do_GET` already decoded the path once
        # (`urllib.parse.unquote(parsed.path)`), so decoding the pieces a second
        # time is the classic way a path guard is walked past: `%252e%252e%252f`
        # survives the first decode as `%2e%2e%2f`, which contains no slash and
        # so passes the two-part check below, and a second decode turns it into
        # `../`. Caught by a mutation that deleted the allow-list and killed
        # nothing, which is how I found out the guard was load-bearing for a
        # traversal I had introduced myself.
        parts = [x for x in rest.split("/") if x]
        if len(parts) != 2:
            return self._text("not found\n", 404)
        want = regionpack.version()
        if not want or parts[0] != want:
            return self._text("not found\n", 404)
        name = parts[1]
        data = regionpack.image_bytes(name)
        if data is None:
            return self._text("not found\n", 404)
        self.send_response(200)
        # Suffix only, from the same table and for the same reason as the vendor
        # route: every name on this allow-list is a `.png` the pack builder
        # wrote, so there is nothing here for a first-bytes sniff to decide.
        self.send_header("Content-Type",
                         CONTENT_TYPES.get(Path(name).suffix.lower(),
                                           "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        # Honest, because the version is in the URL: a rebuilt pack serves its
        # plates from an address no browser has seen.
        self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _course_material(self, rest):
        """One downloaded slide deck or transcript, out of the course's own
        materials folder and nothing else.

        🔴 The folder is CONFIGURED rather than fixed under the course, so the
        containment check is the only thing standing between this route and the
        rest of the disk. It resolves both sides and demands the file be inside,
        exactly as the videos and packages routes do; the difference is that
        those two roots cannot be pointed anywhere, and this one can."""
        parts = [p for p in rest.split("/") if p]
        if len(parts) != 2 or parts[0] != "materials" or ".." in parts:
            return self._text("not found\n", 404)
        folder = local_materials_dir(self.cfg)
        try:
            root = folder.resolve()
            candidate = (folder / parts[1]).resolve()
        except OSError:
            return self._text("not found\n", 404)
        if root not in candidate.parents:
            return self._text("forbidden\n", 403)
        if not candidate.is_file():
            return self._text("not found\n", 404)
        try:
            data = candidate.read_bytes()
        except OSError:
            return self._text("not found\n", 404)
        ctype = content_type_for(candidate.name, data[:MAGIC_PEEK])
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        # Shown in the pane, never offered as a download: the file is already on
        # their disk, and a download button for a file you own is a confusion.
        self.send_header("Content-Disposition", "inline")
        self.send_header("X-Content-Type-Options", "nosniff")
        self._sandbox_if_scriptable(ctype)
        self.end_headers()
        self.wfile.write(data)

    def _course_video(self, rest):
        """A downloaded lecture recording, out of videos/ and nothing else.

        Honours Range, via `parse_range`. A recording is hundreds of megabytes
        and a <video> seeks by asking for byte ranges; served whole with a 200,
        seeking degrades to "download everything first". Single ranges only,
        which is all a player sends.

        🔴 It is no longer "the one route that does": `_package` shares the same
        parser, because a lecture's per-slide mp3s need it for exactly the same
        reason and never had it. **The two routes are one behaviour now**, so the
        416 edge exists in one place."""
        parts = [p for p in rest.split("/") if p]
        if len(parts) != 2 or parts[0] != "videos" or ".." in parts:
            return self._text("not found\n", 404)
        root = (self.cfg["notes_dir"] / "videos").resolve()
        candidate = (self.cfg["notes_dir"] / Path(*parts)).resolve()
        if root != candidate and root not in candidate.parents:
            return self._text("forbidden\n", 403)
        if not candidate.is_file():
            return self._text("not found\n", 404)
        try:
            size = candidate.stat().st_size
        except OSError:
            return self._text("not found\n", 404)
        ctype = CONTENT_TYPES.get(candidate.suffix.lower(), "application/octet-stream")
        got = parse_range(self.headers.get("Range", ""), size)
        if got is None:
            return self._range_not_satisfiable(size)
        start, end, status = got
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if status == 206:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.send_header("Cache-Control", "public, max-age=3600")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        try:
            with candidate.open("rb") as f:
                f.seek(start)
                left = length
                while left > 0:
                    chunk = f.read(min(1 << 20, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (OSError, BrokenPipeError, ConnectionResetError):
            pass

    def _module_index(self, module_id):
        """THE course hub, generated from data, the same product for every course.

        Until 2026-08-23 this was the fallback for a course that came without a
        hand-written index.html, and the one course that had one looked like a
        different product from every course that did not. EH:
        "we need to ensure standardization for the whole system". So the
        navigation surface is now DERIVED, always: lessons stay hand-written
        documents, but what lists them is built from their own metadata, and a
        new course gets exactly what the old ones get. The retired hand-written
        hub is in that course's backups/.

        What it shows, in order: resume (the most recently opened lesson),
        progress, a find-as-you-type filter, and the course tree (week, topic,
        part) in either list or card form; the choice is the viewer's and is
        remembered per browser."""
        mcfg = self.cfg
        esc = html_mod.escape
        metas = lesson_meta_index(mcfg)
        seen = read_visits(mcfg)
        lstate = read_lesson_state(mcfg)

        # 🔴 The same function the reader's prev/next uses, so the hub and the
        # navigation cannot disagree about what follows what. They did until
        # 2026-08-29, and only a reader could see it.
        order = lesson_order(mcfg, metas)
        mats_docs = {}
        try:
            mats = json.loads((mcfg["notes_dir"] / "materials.json")
                              .read_text(encoding="utf-8"))
            mats_docs = mats.get("docs", {}) or {}
        except (OSError, ValueError):
            pass

        # Resume and progress come from the same visits file the reader writes.
        opened = [d for d in order if (seen.get(d) or {}).get("last")]
        resume = max(opened, key=lambda d: str(seen[d].get("last")), default=None)

        # The tree: week, then topic, then parts, grouped in serving order so
        # a course with no week metadata degrades to one flat run of parts.
        def show_title(m, doc):
            # Older lessons carry their doc id in the title ("W2 T3 P1: ...");
            # the hub already prints the part on its own line, so the prefix
            # would read twice. Display-only: the lesson itself is untouched.
            title = str(m.get("title") or doc)
            return re.sub(r"^W\d+\s*T\d+\s*P\d+\s*[:.\-]\s*", "", title) or title

        weeks = []
        for doc in order:
            m = metas[doc]
            wk = ((str(m.get("week") or "").strip().lstrip("0")
                   or str(m.get("week") or "").strip()),
                  str(m.get("weekTitle") or "").strip())
            if not weeks or weeks[-1][0] != wk:
                weeks.append((wk, []))
            weeks[-1][1].append(doc)

        rows = []
        for (wk, wktitle), docs in weeks:
            whead = ("Week %s" % esc(wk)) if wk else "Lessons"
            if wktitle:
                whead += ' <span class="hwt">&middot; %s</span>' % esc(wktitle)
            # The week's own unit id, from the first lesson in it. EH asked for
            # bulbs and stars on topics and weeks too, "next to each other
            # because there's more room there".
            wk_id = unit_ids(docs[0])[0] if docs else None
            wrate = ""
            if wk_id:
                wrate = ('<span class="hrateset side" data-unit="%s">%s</span>'
                         % (esc(wk_id, quote=True),
                            ratings_html(lstate.get(wk_id),
                                         lambda w: "Rate this week: %s" % w)))
            wci = core_ideas_panel(mcfg, wk_id,
                                   "Core ideas for %s" % heading_words(whead))
            # 🔴 The heading is a WRAPPER now, not the <h2> itself: `<details>` is
            # flow content and an <h2> takes phrasing, so the panel cannot live
            # inside the heading element. `.hwh` keeps every rule it had (the
            # flex row, the rule under it); `.hwhn` is the text that used to be
            # its own content.
            rows.append('<section class="hweek"><div class="hwh">'
                        '<h2 class="hwhn">%s</h2>%s%s</div>'
                        % (whead, wci, wrate))
            last_topic = None
            open_topic = False
            for doc in docs:
                m = metas[doc]
                tp = (str(m.get("topicNo") or "").strip(),
                      str(m.get("topic") or "").strip())
                if tp != last_topic:
                    if open_topic:
                        rows.append("</div>")
                    last_topic = tp
                    open_topic = True
                    thead = esc(tp[1]) if tp[1] else ""
                    if tp[0]:
                        thead = ("Topic %s" % esc(tp[0])) + (" &middot; " + thead if thead else "")
                    tp_id = unit_ids(doc)[1]
                    trate = ""
                    if tp_id:
                        trate = ('<span class="hrateset side" data-unit="%s">%s</span>'
                                 % (esc(tp_id, quote=True),
                                    ratings_html(lstate.get(tp_id),
                                                 lambda w: "Rate this topic: %s" % w)))
                    # A topic with no heading still gets its controls, or a
                    # course whose topics are unnamed could rate weeks and parts
                    # but not the level between them.
                    tci = core_ideas_panel(
                        mcfg, tp_id,
                        "Core ideas for %s" % (heading_words(thead)
                                               or (tp_id or "this topic")))
                    rows.append('<div class="htopic">'
                                + ('<div class="hth"><h3 class="hthn">%s</h3>%s%s</div>'
                                   % (thead, tci, trate)
                                   if (thead or trate or tci) else ""))
                title = show_title(m, doc)
                bits = []
                if m.get("part"):
                    bits.append("Part %s" % esc(str(m["part"])))
                mins = (mats_docs.get(doc) or {}).get("minutes")
                if mins:
                    bits.append("%s min" % esc(str(mins)))
                was = (seen.get(doc) or {}).get("last")
                bits.append("opened" if was else "not opened yet")
                st = lstate.get(doc) or {}
                # 🔴 Two rows here, not side by side: EH asked for "two rows,
                # one of light bulbs and then one of stars" on the part boxes,
                # where the Read and Watched buttons already take the width.
                # Topics and weeks get them side by side for the same reason
                # in reverse.
                starbtns = ('<span class="hrateset" data-unit="%s">%s</span>'
                            % (esc(doc, quote=True),
                               ratings_html(st, lambda w: "Rate this lesson: %s" % w)))
                rows.append(
                    # 🔴 `data-min` is on the ROW, not summed on the server and
                    # handed over as a number, because the watched tally has to
                    # move when EH presses Watched and the page never reloads.
                    # `recount()` already re-derives the counts from the DOM for
                    # exactly that reason; the minutes ride along the same way,
                    # so the two can never drift apart. Empty means this lesson
                    # has no duration, which is a state the tally SAYS rather
                    # than silently treating as zero.
                    '<div class="hrow%s" data-doc="%s" data-min="%s">'
                    '<a class="hpart" href="%s" data-find="%s">'
                    '<span class="ht">%s</span><span class="hm">%s</span></a>'
                    '<span class="hctl">'
                    '<button type="button" class="hflag" data-k="read" '
                    'aria-pressed="%s">Read</button>'
                    '<button type="button" class="hflag" data-k="watched" '
                    'aria-pressed="%s">Watched</button>'
                    '%s'
                    '</span></div>'
                    % (" seen" if was else "",
                       esc(doc, quote=True),
                       esc(str(mins), quote=True) if mins else "",
                       esc(m.get("file") or "", quote=True),
                       esc(" ".join([doc, title, str(m.get("title") or ""),
                                     tp[1], wktitle]).lower(), quote=True),
                       esc(title), " &middot; ".join(bits),
                       "true" if st.get("read") else "false",
                       "true" if st.get("watched") else "false",
                       starbtns))
            rows.append(("</div>" if open_topic else "") + "</section>")
        tree = "".join(rows)

        empty = ('<div class="empty"><h2>No lessons in this course yet</h2>'
                 '<p>Everything you need is below: drop in a file somebody sent '
                 'you, or follow the steps to make the lessons from your own '
                 'course. Prefer to be asked? <a href="start">Set this course '
                 'up, step by step</a>.</p></div>')

        n = len(order)
        nread = sum(1 for d in order if (lstate.get(d) or {}).get("read"))
        nwatched = sum(1 for d in order if (lstate.get(d) or {}).get("watched"))

        total_min, watched_min, no_minutes = lecture_time(order, mats_docs, lstate)

        def hours_and_minutes(mins):
            """"10 h 9 min", because "609 minutes" is not a number anybody
            decides an evening with.

            🔴 The JS in HUB_JS says this same sentence for the live update and
            the two must agree exactly, so `test_video_tally.py` drives both
            over the same table of values. Zero is "none", never "0 min": the
            tally says "none watched yet", which reads as a state rather than
            as an odd measurement."""
            mins = int(mins or 0)
            if mins <= 0:
                return "none"
            h, m = divmod(mins, 60)
            if not h:
                return "%d min" % m
            if not m:
                return "%d h" % h
            return "%d h %d min" % (h, m)

        def tally():
            """Total, watched, remaining: EH's three figures, in his order.

            ⚠️ Remaining is DERIVED here and never stored. A stored remaining is
            a third number that can disagree with the other two, and the day it
            does, nobody can tell which one is lying."""
            if not order or not total_min:
                return ""
            left = max(0, total_min - watched_min)
            gap = ""
            if no_minutes:
                # 🔴 A tally with holes in it is worse than no tally, because a
                # reader trusts a number. It cannot happen in either course
                # today (every lesson has a duration); it will the first time a
                # course is onboarded badly, and then this line is the whole
                # difference between "wrong" and "honest".
                gap = ('<span class="htgap"> %d %s in this course %s no '
                       'length recorded, so these figures are lower than the '
                       'real ones.</span>'
                       % (no_minutes,
                          "lesson" if no_minutes == 1 else "lessons",
                          "has" if no_minutes == 1 else "have"))
            # 🔴 "of lecture" read as a claim about the COURSE and the course
            # holds more. The number was right and the word was wrong, and
            # labelling it "across these 38 lessons" alone is true and still
            # never tells the reader the rest exists. So: BOTH numbers, each
            # labelled with what it counts. See `unlisted_time`.
            # ⚠️ The COUNT is deliberately not printed, ruled 2026-09-09 on
            # QA's question. EH asked for a time tally, so the minutes ARE the
            # answer and the count was never the point. `unlisted_time` still
            # returns it and the tests still pin it, because it is what proves
            # these minutes came from whole lectures rather than arithmetic.
            extra_min = unlisted_time(order, mats_docs)[0]
            more = ""
            if extra_min:
                # 🔴 This clause used to say "in 12 lectures with no lesson
                # yet", which asked the reader to hold "lecture" and "lesson"
                # apart in one breath. Outside this project they are the same
                # word, and only ONE of them has a referent on screen: the page
                # is a list of lessons. So the sentence now says the STATE
                # against the word the page itself teaches by showing it, and
                # still never says who writes them.
                #    🔴 AND THE FIGURE IS FROZEN TO THE FIRST WORD OF ITS
                #    LABEL, not just internally. Measured in a browser over
                #    251 container widths: with only "2 h 24 min" joined, the
                #    figure ends a line with no label beside it at 7 of them,
                #    and the clause it REPLACED did the same at 10. Nobody had
                #    caught that, because the width sweep that passed this line
                #    treated this clause as prose and only checked the three
                #    whole figure spans. Joining one more word takes it to 0 of
                #    251 and costs nothing: same three lines, same 67px at
                #    384px, no line ever wider than its box.
                more = ('<span class="htx">, and %s yet covered by a '
                        'lesson</span>'
                        % nb(hours_and_minutes(extra_min) + " not"))
            # 🔴 The lesson count is derived from `n`, the same number the bars
            # three lines up print, so the two can never disagree. It is
            # deliberately OUTSIDE `.htt`, because nothing about watching
            # changes it and the live update must not have to recompute it.
            return ('<p class="htime">'
                    '<span class="htt">%s</span> across %s &middot; '
                    '<span class="htw" data-watchedmin="%d">%s</span> &middot; '
                    '<span class="htl">%s</span>%s%s</p>'
                    % (nb(hours_and_minutes(total_min)),
                       "this lesson" if n == 1 else "these %d lessons" % n,
                       watched_min,
                       nb(hours_and_minutes(watched_min) + " watched"),
                       nb(hours_and_minutes(left) + " left"), more, gap))

        def meter(label, count):
            pct = int(round(100.0 * count / n)) if n else 0
            return ('<div class="hmeter"><span class="hml">%s</span>'
                    '<div class="hbar"><i data-bar="%s" style="width:%d%%"></i></div>'
                    '<span class="hmn" data-n="%s">%d of %d</span></div>'
                    % (label, label.lower(), pct, label.lower(), count, n))

        head = ""
        if order:
            head = ('<div class="hubtop">'
                    '<input id="hfind" type="search" placeholder="Find a lesson" '
                    'autocomplete="off" aria-label="Find a lesson in this course">'
                    '<div class="hviews" role="group" aria-label="How to show the lessons">'
                    '<button type="button" data-hv="list">List</button>'
                    '<button type="button" data-hv="cards">Cards</button></div></div>'
                    '<p class="hprog">Opened %d of %d.%s</p>'
                    '<div class="hmeters">%s%s</div>%s'
                    % (len(opened), n,
                       (' Continue where you left off: <a href="%s">%s</a>.'
                        % (esc(metas[resume].get("file") or "", quote=True),
                           esc(show_title(metas[resume], resume))))
                       if resume else "",
                       meter("Read", nread), meter("Watched", nwatched),
                       tally()))

        # 🔴 HOME_PAGE's body slot is a card GRID (the home page lists course
        # cards in it). Handing it loose children chops the hub into 260px
        # grid cells: the search box vanished into one, the tree into another
        # (EH's screenshots, 2026-08-23). One full-width child, always.
        # The mistakes page's door, at the END, which is where EH put it
        # ("a per-course 'mistakes found in this course' page at the end").
        # Linked only when something is on it: an empty ledger is not a page.
        import mistakes as _mist
        mist_n = len(_mist.read_all(mcfg["notes_dir"]))
        mist_link = (('<p class="hmist"><a href="mistakes">Mistakes found in '
                      'this course (%d) &rarr;</a></p>' % mist_n)
                     if mist_n else "")

        body = (HUB_TREE_CSS
                + '<div class="hubwrap">'
                + head
                + ('<div class="htree" id="htree" data-view="list">%s</div>' % tree
                   if order else empty)
                + mist_link
                + '</div>'
                + ('<script>var HUBMODULE = %s, HUBTOTAL = %d;</script>'
                   % (json.dumps(module_id), n))
                + (HUB_TREE_JS if order else ""))

        imports = IMPORT_BLOCK % {"module": json.dumps(module_id),
                          "help": "help"}
        page = HOME_PAGE % {
            "icons": HEAD_ICONS,
            "tab": "%s: %s" % (str(mcfg.get("module_name") or module_id), PRODUCT),
            "title": esc(str(mcfg.get("module_name") or module_id)),
            "sub": "%d lesson%s in this course." % (n, "" if n == 1 else "s"),
            "body": body,
            "root": esc(str(mcfg["notes_dir"])),
            "server": esc("http://%s:%s/" % (mcfg["bind_ip"], mcfg["port"])),
            "vault": esc((str(mcfg.get("vault_courses") or "")
                          if vault_on(mcfg) else "") or "off"),
            "navbar": nav_bar(self.base_cfg, module_id,
                              str(mcfg.get("module_name") or module_id)),
            # An empty course keeps the inline import block, because putting
            # something in is the only thing to do here. A populated course
            # drops the bottom fold entirely (EH, 2026-08-23): the wizard is
            # the import surface (it is a drop target itself) and the gear
            # menu reaches it. TOKEN_BAR stays regardless: the hub's own
            # read/watched/stars controls post with it.
            "extra": TOKEN_BAR + (imports if not order else ""),
        }
        return self._text(page, 200, "text/html; charset=utf-8")

    def _skills_dir(self):
        """Where the instruction files actually are on this machine.

        Two roots for the same reason `_help_file` has two: the kit puts
        `.claude/` beside `server/`, and this repo keeps the same files under
        `kit/.claude/` because it also holds the module they were grown from."""
        for root in (REPO / ".claude" / "skills", REPO / "kit" / ".claude" / "skills"):
            if root.is_dir():
                return root
        return None

    def _wizard_page(self, module_id):
        """The course checklist: what to include, and whether each piece is
        already downloaded. Composes ONE prompt from the answers (EH,
        2026-08-22, after two lesser shapes the same day: "something which
        lets someone choose what they want included and whether they have
        those things downloaded or not"). See WIZARD_PAGE."""
        esc = html_mod.escape
        mcfg = self.cfg
        code = module_id
        name = str(mcfg.get("module_name") or "")
        # The name question is asked only when there is nothing but the code:
        # a course that arrived named has answered it already.
        askname = (not name) or (name == code)
        course = name if (name and name != code) else code
        back = module_url(self.base_cfg, module_id)

        # What the course already holds, so the checklist can default to the
        # truth and the page never pretends.
        folder = mcfg["notes_dir"]
        nlessons = len(split_lessons.lessons_in(folder))
        nvideos = 0
        vid_bytes = pack_bytes = 0
        try:
            mats = json.loads((folder / "materials.json").read_text(encoding="utf-8"))
            for row in (mats.get("docs") or {}).values():
                if not isinstance(row, dict):
                    continue
                if row.get("video") or row.get("entry"):
                    nvideos += 1
                b = row.get("media_bytes")
                if isinstance(b, int) and b > 0:
                    # An entry means a recording; a measured row without one
                    # is a mirrored package (media_sizes.py's split).
                    if row.get("entry"):
                        vid_bytes += b
                    else:
                        pack_bytes += b
        except Exception:
            pass
        nreadings = 0
        try:
            import readings as _R
            nreadings = len(_R.read_all(folder))
        except Exception:
            pass
        nconsol = len(list((folder / "consolidated").glob("*.pdf"))
                      if (folder / "consolidated").is_dir() else [])
        # 🟢 Counted from the disk rather than from a sidecar's claim about
        # itself, and by a function rather than inline: the rule was stated
        # correctly in a comment here and implemented loosely one line below it,
        # where nothing could test the difference.
        ncaptions = captioned_docs(folder)
        status = {"lessons": nlessons, "videos": nvideos,
                  "readings": nreadings, "consolidated": nconsol,
                  "captions": ncaptions,
                  # 🔴 The NUMBER decides the tick; the SENTENCE is what the
                  # reader acts on, and it is built where it can be tested.
                  "captions_line": caption_count_line(ncaptions),
                  "video_bytes": vid_bytes, "pack_bytes": pack_bytes}

        fill = {"course": code}
        frags = {
            "header": WIZ_HEADER % fill,
            "lessons_local": WIZ_LESSONS_LOCAL % fill,
            "lessons_site": WIZ_LESSONS_SITE % fill,
            "videos": WIZ_VIDEOS % fill,
            "videos_have": WIZ_VIDEOS_HAVE % fill,
            "videos_packs": WIZ_VIDEOS_PACKS % fill,
            "videos_files": WIZ_VIDEOS_FILES % fill,
            "readings_local": WIZ_READINGS_LOCAL % fill,
            "readings_site": WIZ_READINGS_SITE % fill,
            "consol": WIZ_CONSOL % fill,
            "captions": WIZ_CAPTIONS % fill,
            "pack": WIZ_PACK % fill,
        }

        page = WIZARD_PAGE % {
            "icons": HEAD_ICONS,
            "navbar": nav_bar(self.base_cfg, module_id,
                              course, here="Course setup"),
            "course": esc(course),
            "code": esc(code),
            "back": esc(back, quote=True),
            "backjs": json.dumps(back),
            "codejs": json.dumps(code),
            "askname": "true" if askname else "false",
            "frags": json.dumps(frags),
            "status": json.dumps(status),
            "tokenbar": TOKEN_BAR,
            # Shown only while the pack is absent: read from the disk on every
            # render, so a wizard opened after an install does not offer it.
            "pack_hidden": " hidden" if regionpack.install_status()["installed"] else "",
        }
        return self._text(page, 200, "text/html; charset=utf-8")

    def _readings_page(self, module_id):
        """The course's core readings, served through the same shell and layer
        as a lesson, so highlighting, notes, cards, chats and bookmarks all work
        on it (EH's instruction, 2026-08-22). The plain page survives only
        as the empty state, where there is nothing to mark."""
        text = readings_content(self.cfg)
        if text is not None:
            try:
                page = compose_lesson(self.cfg, text, "readings",
                                      served_from=self._page_origin())
            except split_lessons.Problem as exc:
                return self._text("could not compose the readings page: %s\n" % exc,
                                  500)
            return self._text(page, 200, "text/html; charset=utf-8")

        esc = html_mod.escape
        course = str(self.cfg.get("module_name") or module_id or "this course")
        empty = (
            '<div class="empty"><h2>No core readings yet</h2>'
            '<p>Core readings are the papers and chapters your course sets. Put '
            'them here and you get a page you can skim in a minute, with each one '
            'opening into as much detail as you want, and a link straight to the '
            'reading itself.</p>'
            '<p>Open Study Hub in a Claude Code session and say <b>&ldquo;summarise '
            'my core readings&rdquo;</b>, with the folder they are in. It reads '
            'them, writes the summaries, and puts them here. The '
            '<a href="help">step by step guide</a> has the details.</p></div>')
        page = READINGS_PAGE % {
            "icons": HEAD_ICONS,
            "navbar": nav_bar(self.base_cfg, module_id, course,
                              here="Core readings"),
            "tab": "Core readings: %s" % course,
            "back": module_url(self.base_cfg, module_id) if module_id else "/",
            "course": esc(course),
            "sub": "Nothing here yet.",
            "body": empty,
        }
        return self._text(page, 200, "text/html; charset=utf-8")

    def _mistakes_page(self, module_id):
        """The course's recorded source defects, served through the same shell
        as a lesson so highlighting and notes work on it. No entries means a
        plain 404 rather than an empty shrine: the hub only links this page
        when there is something on it, and a course build files entries as it
        finds them (EH's design, 2026-08-23: "without going out of the way")."""
        text = mistakes_content(self.cfg)
        if text is None:
            return self._text("No mistakes recorded in this course's material "
                              "yet. A course build files them as it finds "
                              "them.\n", 404)
        try:
            page = compose_lesson(self.cfg, text, "mistakes",
                                  served_from=self._page_origin())
        except split_lessons.Problem as exc:
            return self._text("could not compose the mistakes page: %s\n" % exc,
                              500)
        return self._text(page, 200, "text/html; charset=utf-8")

    def _help_page(self, module_id):
        """The instructions, for somebody who has never opened a terminal.

        The one thing this page can do that a document cannot: **print the exact
        folder to point Claude at**, because this server knows where it is
        running from and the reader does not. Getting that folder right is the
        whole of the difficulty, and everything else is paste-and-wait."""
        esc = html_mod.escape
        code = module_id or (default_module(self.base_cfg) or "your course")
        multi = self.base_cfg.get("courses_dir") is not None
        page = HELP_PAGE % {
            "icons": HEAD_ICONS,
            "navbar": nav_bar(self.base_cfg,
                              module_id if multi else None,
                              code if multi else None,
                              here="Step-by-step guide"),
            "back": module_url(self.base_cfg, module_id) if (multi and module_id) else "/",
            "backlabel": esc(code) if (multi and module_id) else "your courses",
            "kit": esc(str(REPO)),
            "skills": esc(str(self._skills_dir() or (REPO / ".claude" / "skills"))),
            "prompt1": esc(PROMPT_KEATS % {"course": code}),
            "prompt2": esc(PROMPT_WRITE % {"course": code}),
            "prompt3": esc(PROMPT_VIDEOS % {"course": code}),
            "prompt4": esc(PROMPT_READINGS % {"course": code}),
        }
        return self._text(page, 200, "text/html; charset=utf-8")

    # Which instruction file answers to which name. Explicit rather than derived
    # from the path, so a request can never name a file this did not choose to
    # publish.
    HELP_FILES = {
        "download-keats.md": ("download-keats", "SKILL.md"),
        "write-lesson.md":   ("write-lesson", "SKILL.md"),
        "note-spec.md":      ("write-lesson", "NOTE-SPEC.md"),
        "video-links.md":    ("video-links", "SKILL.md"),
        "core-readings.md":  ("core-readings", "SKILL.md"),
        "download-readings.md": ("download-readings", "SKILL.md"),
        "mirror-packages.md": ("mirror-packages", "SKILL.md"),
        "setup.md":          ("setup", "SKILL.md"),
    }

    def _help_file(self, name):
        """One instruction file, for handing to a Claude that has no skills.

        🔴 Two roots, and both are real. On a recipient's machine the kit's own
        `.claude/` sits beside `server/`, which is what `build_kit` assembles. In
        THIS repo the same files live under `kit/.claude/`, because the repo also
        holds the module it was grown from. Looking in one place would mean the
        feature worked for recipients and 404ed here, or the reverse, and either
        way it would be found by the person it was broken for."""
        want = self.HELP_FILES.get(name)
        if not want:
            return self._text("not found\n", 404)
        skill, filename = want
        for root in (REPO / ".claude" / "skills", REPO / "kit" / ".claude" / "skills"):
            target = root / skill / filename
            if target.is_file():
                try:
                    body = target.read_bytes()
                except OSError:
                    break
                # 🔴 Served INLINE, never as an attachment. Reported 2026-08-22:
                # the download arrived as a stray `.crdownload` and Chrome said
                # "Insecure download blocked". This server is plain http on a
                # private address, and Chrome blocks downloads begun from a
                # non-secure context; nothing this server can send changes that,
                # because the block is about the transport and not the file.
                #
                # A navigation is not a download, so text/plain inline is shown
                # in the tab and can be selected, copied, or saved by the browser
                # the ordinary way. The page also prints the file's path on this
                # machine, for the person who wants the actual file.
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Content-Disposition",
                                 'inline; filename="%s"' % name)
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(body)
                return
        return self._text("That instruction file is not in this copy.\n", 404)

    def _settings_page(self):
        """The global preferences, reachable without opening a lesson.

        Reads and writes `/api/settings`, which is the same store the reader's
        own sheet uses, so this is a second view rather than a second copy."""
        esc = html_mod.escape
        cur = read_settings(self.base_cfg)
        st = explain_status(self.base_cfg, self.host_word())
        explain_on = st["explain"]
        here = self.host_word()

        pal = []
        for entry in cur.get("palette", []):
            if entry.get("retired"):
                continue
            pal.append('<span class="sw"><i class="dot" style="background:%s"></i>%s</span>'
                       % (esc(str(entry.get("fill") or entry.get("colour") or "#ccc"),
                              quote=True),
                          esc(str(entry.get("label") or entry.get("id") or ""))))

        # 🔴 Always shown, and it did not used to be: the section appeared only
        # when a vault had already been found, so the person who most needed to
        # say where their vault was (the one whose vault is somewhere this could
        # not guess) was the one person the page hid the question from. Reported
        # 2026-08-21: "it doesn't let you select the location of the vault.
        # Different people might have the vaults in different places."
        vpath = Path(str(self.base_cfg.get("vault_courses") or ""))
        vault_section = (
            '<section><h2>Vault publishing</h2>'
            '<p class="hint">Copies your highlights and notes into an Obsidian '
            'vault as you read, one note per topic. Off means nothing is written '
            'there at all, and nothing else changes.</p>'
            '<label class="row"><input type="checkbox" id="vaultbox"> '
            'Publish highlights and notes to %s</label>'
            '<h3>Where they go</h3>'
            '<p class="hint">The <code>Courses</code> folder inside your vault. '
            'Give the vault itself and it will use the Courses folder in it, and '
            'make that folder if it is not there yet. %s</p>'
            '<div class="pathrow">'
            '<input type="text" id="vaultpath" value="%s" spellcheck="false" '
            'autocomplete="off" autocapitalize="off" '
            'aria-label="Where in your vault the notes go">'
            '<button type="button" id="vaultsave">Save</button></div>'
            '</section>'
            % (esc(vault_display_name(self.base_cfg)),
               ("" if vpath.parent.is_dir() else
                "<b>There is nothing at this path on this machine</b>, so nothing "
                "is being published: point it at your vault, or leave publishing "
                "off."),
               esc(str(vpath), quote=True)))

        multi = self.base_cfg.get("courses_dir") is not None

        # 🔴 Names live here rather than on each course hub, because a course
        # with a hand-written hub never renders the generated one and would have
        # been the one course you could not rename. One list, every course.
        courses_section = ""
        if multi:
            rows = []
            for mid, folder in sorted(resolve_modules(self.base_cfg).items()):
                nm = str(module_facts_of(folder).get("name") or "")
                # The facts a course cannot show anywhere else: where it lives,
                # what is in it, and where its readings PDFs go. Counted from
                # the folder, not from a summary, and a broken readings.json
                # must not take Settings down with it.
                nlessons = len(split_lessons.lessons_in(folder))
                nread = nfiled = 0
                try:
                    import readings as _R
                    rl = _R.read_all(folder)
                    nread = len(rl)
                    nfiled = sum(1 for r in rl if r.get("file"))
                except Exception:
                    pass
                facts = ['%d lesson%s' % (nlessons, "" if nlessons == 1 else "s")]
                if nread:
                    facts.append('%d readings summarised' % nread)
                    if nfiled:
                        facts.append('%d PDFs filed in <code>%s</code>'
                                     % (nfiled, esc(str(folder / "readings"))))
                elif (folder / "readings").is_dir():
                    facts.append('readings PDFs go in <code>%s</code>'
                                 % esc(str(folder / "readings")))
                rows.append(
                    '<div class="crow"><label for="nm-%s">%s</label>'
                    '<input type="text" id="nm-%s" data-id="%s" value="%s" '
                    'placeholder="Give it a name" autocomplete="off" '
                    'aria-label="Name for %s"></div>'
                    '<p class="cfacts">Lives in <code>%s</code><br>%s</p>'
                    % (esc(mid, quote=True), esc(mid), esc(mid, quote=True),
                       esc(mid, quote=True), esc(nm, quote=True), esc(mid, quote=True),
                       esc(str(folder)), " \u00b7 ".join(facts)))
            courses_section = (
                '<section><h2>Your courses</h2>'
                '<p class="hint">The <b>name</b> is only what you read, and you can '
                'change it whenever you like. The <b>code</b> beside it cannot be '
                'changed here: it is the folder your lessons are in and what keeps '
                'this course\u2019s highlights apart from every other course\u2019s, '
                'so changing it would strand what you have marked. Under each '
                'course: the folder it lives in on this machine, and what it '
                'holds.</p>'
                '%s<p class="says" id="csays" role="status"></p></section>'
                % "".join(rows)) if rows else ""

        # 🔴 RENDERED EMPTY AND FILLED BY `/api/captions`, deliberately. The
        # status needs a subprocess, and a settings page that shells out on every
        # render is a page that gets slower each time somebody adds a course. The
        # HTML carries the course list; every fact about a lecture arrives over
        # the API, from the one file that knows how a course is captioned.
        cap_opts = []
        for mid, folder in sorted(resolve_modules(self.base_cfg).items()):
            nm = str(module_facts_of(folder).get("name") or "") or mid
            cap_opts.append('<option value="%s">%s</option>'
                            % (esc(mid, quote=True),
                               esc(mid if nm == mid else "%s (%s)" % (nm, mid))))
        captions_section = (
            '<section><h2>Captions</h2>'
            '<p class="hint">Captions are the <b>lecturer\u2019s own words</b>, taken '
            'from the transcript and timed against the recording, so they are not '
            'a machine\u2019s guess at what was said. This builds them for every '
            'lecture in a course that can have them, narrated slide packages and '
            'plain recordings alike, and skips any that already has them. '
            'It fetches one lecture at a time and pauses between, so a whole '
            'course takes a while; you can leave the page while it runs.</p>'
            '<div class="pathrow">'
            '<select id="capcourse" aria-label="Course">%s</select>'
            '<button id="capinstall" hidden>Install the caption engine</button>'
            '<button id="capgo" disabled>Build the missing captions</button></div>'
            '<p class="capsum" id="capsum"></p>'
            '<div class="caplist" id="caplist"></div>'
            '<pre class="caplog" id="caplog" hidden></pre>'
            '<p class="says" id="capsays" role="status"></p></section>'
            % "".join(cap_opts)) if cap_opts else ""
        # One row for the picture pack: what is installed, or the one button
        # that fetches it. The state is asked of /api/packs by the script, so
        # the markup carries no claim the disk could contradict a minute later.
        pack_section = (
            '<section><h2>Brain-region pictures</h2>'
            '<p class="hint">Labelled plates of the brain regions, with their '
            'definitions, so a region named in a lesson opens on a picture chosen '
            'for that purpose rather than whatever Wikipedia leads with. One '
            'download shared by every course on this machine, kept across '
            'upgrades. The plates have not had a formal review by a domain '
            'expert: study from them, do not cite them.</p>'
            '<div class="pathrow">'
            '<button id="packinstall" hidden>Install the pictures</button></div>'
            '<p class="capsum" id="packsum"></p>'
            '<pre class="caplog" id="packlog" hidden></pre>'
            '<p class="says" id="packsays" role="status"></p></section>')

        page = SETTINGS_PAGE % {
            "icons": HEAD_ICONS,
            "navbar": nav_bar(self.base_cfg, here="Settings"),
            "back": "/" if multi else "/",
            "backlabel": "All your courses" if multi else "Back to the lessons",
            "explain": ("" if explain_on else "Asking is off: " + st["explainWhy"]),
            "host": esc(here),
            "route": esc({"cli": "Right now Claude Code on %s answers." % here,
                          "api": "Right now the API answers, with the key set here.",
                          "": "Right now asking is off. " + st["explainWhy"]
                          }[st["backend"]]),
            "keystate": ("A key is set." if api_key(self.base_cfg)
                         else "No key is set."),
            "palette": "".join(pal) or '<span class="sw">none yet</span>',
            "root": esc(str(self.base_cfg.get("courses_dir")
                            or self.base_cfg["notes_dir"])),
            "server": esc("http://%s:%s/" % (self.base_cfg["bind_ip"],
                                             self.base_cfg["port"])),
            "vaultpath": esc(str(self.base_cfg["vault_courses"])
                             if vault_on(self.base_cfg) else "off"),
            "vault": vault_section,
            "levels": json.dumps(LEVEL_LABELS),
            "sizes": json.dumps([{"id": s["id"], "label": s["label"]}
                                 for s in PANEL_SIZES]),
            "models": json.dumps([{"id": m["id"], "label": m["label"]}
                                  for m in MODELS]),
            "courses": courses_section,
            "captions": captions_section,
            "pack": pack_section,
            "tokenbar": TOKEN_BAR,
            "state": json.dumps({"level": cur.get("level"),
                                 "panelSize": cur.get("panelSize"),
                                 "model": cur.get("model"),
                                 "vaultEnabled": cur.get("vaultEnabled", True),
                                 "askBackend": (self.base_cfg.get("ask_backend")
                                                or "auto")}),
        }
        return self._text(page, 200, "text/html; charset=utf-8")

    def _add_course_page(self):
        """The three names, before there is a course to hang them on.

        🔴 A single-course install has nowhere to put a second one,
        and `POST /api/modules` says so. Saying it HERE as well means nobody
        fills in three fields only to be refused at the end. See
        ADD_COURSE_PAGE."""
        if self.base_cfg.get("courses_dir") is None:
            return self._text("This install holds one course and has no courses "
                              "folder to add another to.\n", 404)
        page = ADD_COURSE_PAGE % {
            "icons": HEAD_ICONS,
            "navbar": nav_bar(self.base_cfg),
            "tokenbar": TOKEN_BAR,
        }
        return self._text(page, 200, "text/html; charset=utf-8")

    def _home_page(self):
        """The front door when there is more than a folder of notes: one card per
        module (plan §10b), a real empty state, and a status line a
        non-technical person can read down the phone (§10d items 3 and 4).

        A courses root may hold a hand-written `index.html`, and if it does that
        wins: this is a default, not a fixture."""
        root = self.base_cfg["courses_dir"]
        own = root / "index.html"
        if own.is_file():
            return self._static_file(own)

        info = modules_summary(self.base_cfg)
        esc = html_mod.escape
        cards = []
        for m in info["modules"]:
            cards.append(
                '<div class="card"><a class="main" href="%s"><h2>%s</h2>'
                '<p class="code">%s</p><p class="n">%d lesson%s</p></a>%s</div>'
                % (esc(m["url"]), esc(m["name"]), esc(m["code"]),
                   m["lessons"], "" if m["lessons"] == 1 else "s",
                   (('<a class="cont" href="%s%s">Continue: %s</a>'
                     % (esc(m["url"]), esc(m["last"]["file"]), esc(m["last"]["doc"]))
                     if m["last"] else "")
                    + ('<a class="cont" href="%sreadings">%d core reading%s</a>'
                       % (esc(m["url"]), m["readings"], "" if m["readings"] == 1 else "s")
                       if m.get("readings") else "")
                    + '<a class="cont" href="%snotebook">Notebook</a>' % esc(m["url"]))))

        empty = (
            '<div class="empty"><h2>No modules yet</h2>'
            '<p>A module is a folder of lessons inside <code>%s</code>. '
            'Ask Claude to download one from KEATS, or point it at a folder you '
            'already have, and it will appear here.</p>'
            '<p><b>Or start one yourself:</b> <a href="/addcourse">add a course</a>, '
            'and the setup walks you through getting the lessons in.</p>'
            '</div>' % esc(info["root"]))

        body = "".join(cards) if cards else empty
        n = len(info["modules"])
        page = HOME_PAGE % {
            "icons": HEAD_ICONS,
            "tab": PRODUCT,
            "title": '<span class="mark">%s</span>%s' % (icon.svg(theme_aware=False),
                                                         esc(PRODUCT)),
            "sub": "%d module%s on this machine." % (n, "" if n == 1 else "s"),
            "body": body,
            "root": esc(info["root"]),
            "server": esc(info["server"]),
            "vault": esc(info["vault"] or "off"),
            "navbar": nav_bar(self.base_cfg),
            "extra": TOKEN_BAR + ADD_COURSE_CTA,
        }
        return self._text(page, 200, "text/html; charset=utf-8")

    def _notebook_page(self, module_id, query):
        info = notebook_summary(self.base_cfg, module_id, query.get("q", ""))
        esc = html_mod.escape
        q = query.get("q", "")
        kind = query.get("kind", "")
        how = query.get("group", "lesson")
        if how not in GROUPINGS:
            how = "lesson"

        def link(**over):
            bits = {"q": q, "kind": kind, "group": how}
            bits.update(over)
            keep = {k: v for k, v in bits.items() if v and not (k == "group" and v == "lesson")}
            return "?" + urllib.parse.urlencode(keep) if keep else "?"

        tabs = ['<a class="%s" href="%s">%s</a>'
                % ("on" if kind == k else "", esc(link(kind=k)), lbl)
                for k, lbl in (("", "Everything"), ("note", "Notes"), ("highlight", "Highlights"),
                               ("card", "Cards"), ("bookmark", "Bookmarks"))]
        tabs.append('<span style="flex:1"></span>')
        tabs += ['<a class="%s" href="%s">%s</a>'
                 % ("on" if how == g else "", esc(link(group=g)), lbl)
                 for g, lbl in (("lesson", "By lesson"), ("week", "By week"),
                                ("topic", "By topic"), ("kind", "By kind"))]

        body, shown = [], 0
        for mod in info["modules"]:
            items = [i for i in mod["items"] if not kind or i["kind"] == kind]
            if not items:
                continue
            if module_id is None and len(info["modules"]) > 1:
                body.append("<h2>%s</h2>" % esc(mod["name"]))
            groups = {}
            for i in items:
                k, head = notebook_group_key(i, how)
                groups.setdefault((k, head), []).append(i)
            for (_, head), rows in sorted(groups.items()):
                first = rows[0]
                target = mod["url"] + (first.get("href") or "")
                body.append('<h2><a href="%s">%s</a></h2>'
                            % (esc(target), esc(head)) if how == "lesson"
                            else "<h2>%s</h2>" % esc(head))
                for i in rows:
                    shown += 1
                    go = mod["url"] + (i.get("href") or "")
                    label = {"note": "note", "highlight": "highlight",
                             "card": "card", "bookmark": "bookmark"}.get(i["kind"], i["kind"])
                    if how != "lesson":
                        label += " &middot; " + esc(i.get("doc") or "")
                    body.append(
                        '<div class="item%s"><span class="k">%s</span>'
                        '<p>%s</p>%s<p><a class="go" href="%s">Open %s</a></p></div>'
                        % (" orphan" if i.get("orphan") else "", label,
                           esc((i.get("text") or "").strip()) or "<i>empty</i>",
                           ('<p class="n">%s</p>' % esc(i["note"])) if i.get("note") else "",
                           esc(go), esc("the readings" if i.get("doc") == "READINGS"
                                        else (i.get("doc") or "the lesson"))))
        if not shown:
            body = ['<p class="empty">%s</p>'
                    % ("Nothing you have marked matches %s." % esc(q) if q
                       else "Nothing marked yet. Highlights, notes, cards and bookmarks "
                            "made while reading show up here.")]

        # The other half of a search: where the words appear in the lessons
        # themselves, which nothing has ever been able to answer.
        if q:
            hits = search_lessons(self.base_cfg, module_id, q)
            body.append('<h2>In the lessons themselves (%d%s)</h2>'
                        % (len(hits), "+" if len(hits) >= 60 else ""))
            if not hits:
                body.append('<p class="empty">The word does not appear in any lesson.</p>')
            for h in hits:
                s = h["snippet"]
                a, b2 = h["at"], h["at"] + len(h["hit"])
                marked = (esc(s[:a]) + "<b>" + esc(s[a:b2]) + "</b>" + esc(s[b2:])
                          if 0 <= a < b2 <= len(s) else esc(s))
                where = h["doc"] + (" &middot; week %s" % h["week"] if h["week"] else "")
                body.append(
                    '<div class="item"><span class="k">lesson &middot; %s</span>'
                    '<p>%s</p><p><a class="go" href="%s%s">Open %s</a></p></div>'
                    % (where, marked, esc(h["url"]), esc(h["file"]), esc(h["title"][:60])))

        c = info["counts"]
        page = NOTEBOOK_PAGE % {
            "icons": HEAD_ICONS,
            "navbar": nav_bar(self.base_cfg, module_id,
                              module_id, here="Notebook"),
            "title": "Notebook" if module_id is None else "Notebook &middot; " + esc(module_id),
            "sub": ('%d highlights, %d notes, %d cards, %d bookmarks.'
                    % (c.get("highlight", 0), c.get("note", 0), c.get("card", 0),
                       c.get("bookmark", 0))),
            "q": esc(q, quote=True),
            "tabs": "".join(tabs),
            "body": "".join(body),
            "foot": esc(str(self.base_cfg.get("courses_dir") or self.base_cfg["notes_dir"])),
        }
        return self._text(page, 200, "text/html; charset=utf-8")

    def _static_file(self, candidate):
        ctype = CONTENT_TYPES.get(candidate.suffix.lower(), "application/octet-stream")
        try:
            data = candidate.read_bytes()
        except OSError:
            return self._text("not found\n", 404)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _static(self, path):
        root = self.cfg["notes_dir"]
        candidate = (root / path.lstrip("/")).resolve()
        if root != candidate and root not in candidate.parents:
            return self._text("forbidden\n", 403)
        if not candidate.is_file():
            return self._text("not found\n", 404)
        ctype = CONTENT_TYPES.get(candidate.suffix.lower(), "application/octet-stream")
        try:
            data = candidate.read_bytes()
        except OSError:
            return self._text("not found\n", 404)

        # A content-only lesson is wrapped in the reader before it goes out. A
        # stamped one already carries its own copy and is served untouched.
        if candidate.suffix.lower() == ".html":
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                text = None
            if text is not None and split_lessons.is_content_file(text):
                try:
                    data = compose_lesson(self.cfg, text, candidate.name,
                                          served_from=self._page_origin()
                                          ).encode("utf-8")
                except (split_lessons.Problem, OSError, ValueError) as exc:
                    log(self.cfg, "compose FAILED %s: %s" % (candidate.name, exc))
                    # Loud, and specific about which of the three files is wrong.
                    # Serving the bare content instead would render a lesson with
                    # no reader on it, which looks like lost highlights.
                    return self._text(
                        "<h1>This lesson could not be assembled</h1><p>%s</p>"
                        "<p>The note is fine; the reader could not be wrapped "
                        "around it. Check <code>server/reader/shell.html</code> "
                        "and <code>server/local-layer.html</code>.</p>"
                        % html_mod.escape(str(exc)),
                        500, "text/html; charset=utf-8")

        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)


# --- R16: relaunching the server from Settings -----------------------------------------
#
# Asked 2026-08-13: "let's add something into settings where I can relaunch the server."
#
# 🔴 The need is real and it is specifically HIS need. Every change to this file is inert
# until the process restarts, and he cannot do it himself: he reads from the MacBook,
# where this project does not exist, and the one time he tried, his `pkill` matched
# nothing. The server only ever runs on the Mini.
#
# A server cannot restart itself and then answer, so this re-execs instead: it replaces
# its own process image, which keeps the same pid and the same parent terminal and needs
# no launchd agent, no supervisor and no change to his machine's configuration. The
# listening socket closes automatically, because Python has made sockets non-inheritable
# across exec since 3.4, and the fresh process rebinds under SO_REUSEADDR.
#
# 🔴 It refuses to restart into a file that will not parse, and that guard is the whole
# reason this is safe to hand to a button. The failure it prevents is the bad one: a
# restart is the only way to load new code, so restarting into a broken file would take
# the server down with no way to bring it back from the browser that just killed it. A
# syntax error is caught while the OLD process is still serving, and it declines.


def restart_guard():
    """Returns None if this file will parse, otherwise why it will not."""
    src = Path(__file__).resolve()
    try:
        compile(src.read_text(encoding="utf-8"), str(src), "exec")
    except SyntaxError as exc:
        return "line %s: %s" % (exc.lineno, exc.msg)
    except OSError as exc:
        return "cannot read %s: %s" % (src, exc)
    return None


def do_restart(cfg):
    log(cfg, "restart: re-exec now")
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass
    try:
        os.execv(sys.executable, [sys.executable] + sys.argv)
    except Exception as exc:                       # pragma: no cover
        # Nothing can be reported to anyone at this point: the response went out
        # before this ran and the socket is gone. The log is the only witness.
        log(cfg, "restart FAILED, still running the old code: %s" % exc)


# How close to expiry the certificate has to be before the start says so. The
# renewal path is not built, so this line is the only thing that puts the
# ~90-day clock in front of a person before it runs out.
TLS_EXPIRY_WARN_DAYS = 14

# (path, mtime, size) -> (notBefore, notAfter), or None when they could not be
# read. Kept at a single entry; see `tls_window`.
_TLS_WINDOW = {}


def tls_files(cfg):
    """(cert, key) as configured and readable, WITHOUT asking whether the
    certificate is in date.

    🔴 HALF-CONFIGURED IS OFF, AND IT SAYS SO. One path without the
    other, or a path that is not readable, means https does not start. The
    alternative is a server that believes it is serving TLS and is not, which
    on this project's own history is the shape that costs a night: the reader
    would be told to use an address that answers nothing.

    Split out of `tls_paths` on 2026-08-30 so the start can tell "no certificate
    configured" apart from "the certificate expired", which are otherwise the
    same silence. 🔴 **Every other caller wants `tls_paths`**, which asks
    both questions; this one exists for the log line and for nothing else.
    """
    cert = str(cfg.get("tls_cert") or "").strip()
    key = str(cfg.get("tls_key") or "").strip()
    if not cert or not key:
        return None
    cp, kp = Path(cert).expanduser(), Path(key).expanduser()
    if not (cp.is_file() and kp.is_file()):
        return None
    return cp, kp


def tls_window(cert):
    """(notBefore, notAfter) in epoch seconds, or None when they cannot be read.

    🔴 CACHED ON (path, mtime, size), because `tls_paths` is asked on
    every GET through `_https_redirect` and parsing a certificate per request is
    not affordable. Keying on the mtime rather than on the path alone means a
    REPLACED file is seen without a restart.

    🔴 THAT IS A HAZARD AND I FIRST REPORTED IT AS A BENEFIT. The
    manager and QA both caught it within the hour of `fda4c8d`. The listener
    binds its certificate ONCE, at start, so at the moment a renewal is written:
    this cache turns the gate and the redirect back ON while the listener is
    still presenting the OLD, expired certificate, or was never bound at all
    because the process started after expiry. **The reader is redirected to a
    port that refuses them, which is the total outage this function exists to
    prevent, moved to the moment somebody believes they have just fixed it.**
    Measured by QA: a valid pair written to the same paths brings the 308 back
    within two seconds while the port still answers "certificate has expired".

    ⚠️ **Do not "fix" this by caching on the path alone.** That trades a
    hazard at renewal for one at expiry, which is worse because expiry is
    certain and arrives unattended. The shape of the answer is an ORDER
    (renewal writes, then restarts) plus a redirect that refuses to fire while
    the file on disk differs from the one the listener actually bound. It is a
    filed queue entry, "The redirect comes back on before the listener does",
    and it is not reachable until a certificate exists.

    ⚠️ `ssl` HAS NO PUBLIC ACCESSOR for a certificate file's dates.
    Checked on 3.14.7 rather than assumed: `cert_time_to_seconds` is public and
    the decode is not, so OpenSSL's own decoder is reached through the private
    entry point. **Any failure returns None**, and the caller reads that as "not
    in date"; the direction is argued at `tls_in_date`.
    """
    try:
        st = cert.stat()
    except OSError:
        return None
    key = (str(cert), st.st_mtime_ns, st.st_size)
    if key not in _TLS_WINDOW:
        try:
            decoded = ssl._ssl._test_decode_cert(str(cert))
            window = (ssl.cert_time_to_seconds(decoded["notBefore"]),
                      ssl.cert_time_to_seconds(decoded["notAfter"]))
        except Exception:
            window = None
        # One certificate is ever in play, so this stays a single entry rather
        # than growing for the life of the process.
        _TLS_WINDOW.clear()
        _TLS_WINDOW[key] = window
    return _TLS_WINDOW[key]


def tls_in_date(cert, now=None):
    """Is this certificate valid at `now`? MEASURED, not derived, 2026-08-30.

    🔴 AN EXPIRED CERTIFICATE IS NOT "https off", and until this existed
    the difference took the whole tailnet down. **The https listener STARTED, the
    308 KEPT FIRING at it, and a verifying client got `certificate has
    expired`**, so every bookmarked http page sent the reader to a port no
    browser would talk to. The readability check the guards had could not see any
    of it: the files are still there and they still load.

    🔴 MEASURED BY QA AT `0428a4a`, AND THE METHOD IS THE POINT.
    Two leaf certificates from ONE throwaway CA the client trusts, identical in
    every extension and differing only in their dates. **A bare self-signed pair
    cannot prove this**: an untrusted certificate is refused whatever its dates,
    so the obvious experiment confirms the wrong cause. Pinning one self-signed
    certificate as its own trust anchor separates them too, checked here: in date
    it hands back TLSv1.3, out of date it says `certificate has expired`, and
    with no trust granted it says `self-signed certificate`.

    ⚠️ **Read the REASON STRING, never the exit code.** `curl` exits
    60 for expiry and for untrusted alike, so an rc in a rig log distinguishes
    nothing.

    🔴 UNREADABLE DATES MEAN NOT IN DATE, and the direction is the whole
    safety argument. "Off" costs https and leaves the reader on a working http
    page; "on" is a check that cannot fire, which is the failure this project
    refuses everywhere else. It is loud rather than silent: the start says which
    of the two it was.
    """
    window = tls_window(cert)
    if window is None:
        return False
    now = time.time() if now is None else now
    return window[0] <= now <= window[1]


def tls_why_invalid(cert, now=None):
    """Which end of the window failed, in words for the log.

    🔴 THE FIRST VERSION OF THIS SAID "expired" FOR BOTH ENDS, and
    a rig caught it inside ten minutes: a certificate valid from 2027 was logged
    as `expired 2027-02-01`, which is a false sentence in the one place somebody
    goes to find out what happened. A clock set wrongly is a real way to land in
    the not-yet-valid case, and it is the case where a misleading log costs the
    most, because the certificate really is fine.
    """
    window = tls_window(cert)
    if window is None:
        return "its dates could not be read"
    now = time.time() if now is None else now
    when = lambda t: time.strftime("%Y-%m-%d", time.gmtime(t))
    if now < window[0]:
        return "not valid until %s" % when(window[0])
    return "expired %s" % when(window[1])


def tls_paths(cfg, now=None):
    """(cert, key) as Paths, or None when https is off, half-configured, or the
    certificate is not valid right now.

    🔴 THE ONE CHOKE POINT, deliberately. `tls_port`, `_allowed_hosts`,
    `start_tls_listener` and `_https_redirect` all hang off this, so validity had
    to land HERE rather than in any one of them: a guard that knows about expiry
    while its three siblings do not is exactly how the gate and the redirect come
    to disagree about whether https is on.
    """
    paths = tls_files(cfg)
    if paths is None:
        return None
    return paths if tls_in_date(paths[0], now) else None


def tls_port(cfg):
    """The https port, or 0 when https is off.

    🔴 Reads the CERTIFICATE, not just the port number, so every
    caller asks one question and cannot get a yes from a config that names a
    port and has nothing to serve on it. `_allowed_hosts` and the redirect both
    hang off this, and they must agree.
    """
    if tls_paths(cfg) is None:
        return 0
    try:
        port = int(cfg.get("tls_port") or 0)
    except (TypeError, ValueError):
        return 0
    return port if 1 <= port <= 65535 and port != cfg["port"] else 0


def start_tls_listener(cfg, handler_cls):
    """The https listener, on its own port beside the http one.

    🔴 A SECOND PORT, ruled 2026-08-30, and the reason is the third
    origin rather than the fourth. `https://<name>:<any port>` is a new origin
    whichever port is chosen, because an origin is scheme AND host AND port. So
    the fourth origin is not a cost that distinguishes the options. What
    distinguishes them is whether `http://<name>:8795` keeps working, and only
    a second port does: it leaves the http listener free to redirect, path
    preserved, instead of failing with a TLS error on three devices.

    🟢 Stdlib only, checked before it was designed around:
    `ssl.SSLContext.wrap_socket` with `PROTOCOL_TLS_SERVER`, no dependency.

    ⚠️ Failure to start is logged and never fatal, exactly as the
    loopback listener's is. A certificate that has expired or been replaced
    badly must not take the http reader down with it.
    """
    paths = tls_paths(cfg)
    port = tls_port(cfg)
    if paths is None or not port:
        # 🔴 SAY WHICH SILENCE THIS IS. "No certificate configured" and
        # "the certificate expired" produced the identical nothing until
        # 2026-08-30, and the second one is the state in which the redirect used
        # to keep firing at a port no browser would accept.
        files = tls_files(cfg)
        if files is not None and not tls_in_date(files[0]):
            log(cfg, "tls off: the certificate is not valid (%s); "
                     "the redirect stays off too" % tls_why_invalid(files[0]))
            print("  (no https listener: the certificate is not valid, %s)"
                  % tls_why_invalid(files[0]))
        return None
    cert, key = paths
    # ⚠️ The renewal path is NOT built: this process reads the files
    # once, and a Let's Encrypt certificate lasts about 90 days. Until renewal
    # is decided, this line is the only thing that puts the clock in front of a
    # person, and it only speaks at a start.
    window = tls_window(cert)
    if window:
        days_left = int((window[1] - time.time()) // 86400)
        if days_left <= TLS_EXPIRY_WARN_DAYS:
            log(cfg, "tls certificate expires in %d day(s), on %s"
                % (days_left, time.strftime("%Y-%m-%d", time.gmtime(window[1]))))
            print("  (https certificate expires in %d day(s))" % days_left)
    try:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        # 🔴 TLS 1.2 floor. Everything reaching this is a browser on
        # EH's own tailnet, and 1.0/1.1 are refused by those browsers anyway;
        # naming it here means the answer does not depend on the default of
        # whichever OpenSSL the machine happens to carry.
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(certfile=str(cert), keyfile=str(key))
        httpsd = ThreadingHTTPServer((cfg["bind_ip"], port), handler_cls)
        httpsd.socket = ctx.wrap_socket(httpsd.socket, server_side=True)
    except Exception as exc:
        log(cfg, "tls listener not started: %s" % exc)
        print("  (no https listener: %s)" % exc)
        return None
    threading.Thread(target=httpsd.serve_forever, daemon=True,
                     name="tls-listener").start()
    return httpsd


def start_loopback_listener(cfg, handler_cls):
    """A SECOND listener on 127.0.0.1, alongside the configured one.

    🔴 Why a second listener and not a change of `bind_ip`, and never `0.0.0.0`.
    The server binds exactly one address, and when that address is the tailnet
    one, `127.0.0.1:<port>` answers nothing at all. Two things follow that this
    fixes together:

      - **Nothing on this machine can reach `/api` without the token**, because
        the token is the credential for every non-loopback peer. That is why QA
        verifies every pane change against rigs built to EH's shape and never
        against his real data, and it is where the `site:` family of defects
        lived. Loopback is the one address where being on the machine IS the
        credential.
      - **The Mac stops depending on the tailnet to talk to itself.** Open since
        2026-08-28: when Tailscale stopped, the server was fine and unreachable,
        and nothing could tell "server broken" from "network gone". A loopback
        listener answers that question from the machine itself.

    `0.0.0.0` would do both and is refused: it puts this server, which can spawn
    a subprocess and write files, on every interface including whatever café
    network the laptop is on. The tailnet surface is unchanged by this; what is
    added is an address only processes on this machine can reach.

    Returns the server (already serving on a daemon thread) or None. A failure
    to bind is logged and never fatal: the configured listener is the one that
    matters, and the machine may legitimately have something else on that port.
    """
    if is_loopback(cfg["bind_ip"]):
        return None                     # the configured listener already is one
    try:
        extra = ThreadingHTTPServer(("127.0.0.1", cfg["port"]), handler_cls)
    except OSError as exc:
        # An error path that raises is worse than the error it reports, and this
        # one runs at every start.
        if cfg.get("log_path") is not None:
            log(cfg, "loopback listener not started: %s" % exc)
        print("  (no loopback listener: %s)" % exc)
        return None
    threading.Thread(target=extra.serve_forever, daemon=True,
                     name="loopback-listener").start()
    return extra


def main():
    ap = argparse.ArgumentParser(description="KCL study server")
    ap.add_argument("--init", action="store_true", help="scaffold the config and exit")
    ap.add_argument("--config", default=None, help="config path")
    args = ap.parse_args()

    # The flag names the config this process reads AND writes: bound into the
    # module before anything loads or saves, never a local variable again.
    path = bind_config_path(args.config)
    if args.init:
        return init_config(path)

    cfg = load_config(path)
    tidy_stray_baks(cfg)
    place_legacy_resources(cfg)
    Handler.base_cfg = cfg
    Handler.cfg = cfg
    httpd = ThreadingHTTPServer((cfg["bind_ip"], cfg["port"]), Handler)
    url = "http://%s:%d/" % (cfg["bind_ip"], cfg["port"])
    log(cfg, "start %s" % url)
    print("Study notes at %s" % url)
    loopback = start_loopback_listener(cfg, Handler)
    if loopback is not None:
        log(cfg, "start http://127.0.0.1:%d/ (loopback, same server)" % cfg["port"])
        print("Also on http://127.0.0.1:%d/ from this machine" % cfg["port"])
    tlsd = start_tls_listener(cfg, Handler)
    if tlsd is not None:
        # 🟢 Same process, same Handler, one more socket. The http
        # listener stays up on purpose: it is what redirects the bookmarks.
        tp = tls_port(cfg)
        log(cfg, "start https://%s:%d/ (tls)" % (cfg["bind_ip"], tp))
        print("Securely on https://%s:%d/" % (cfg["bind_ip"], tp))
        print("  (http on %d now redirects there, except on loopback)" % cfg["port"])
    print("Vault target: %s" % cfg["vault_courses"])
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("")
    finally:
        log(cfg, "stop")


if __name__ == "__main__":
    main()
