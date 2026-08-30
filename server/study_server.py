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

Config lives outside the repo and outside Dropbox (it is machine state, and
one day may hold a key): ~/.kcl-study/config.json, mode 0600.

    python3 server/study_server.py --init     # scaffold the config
    python3 server/study_server.py            # run it
"""

import argparse
import hashlib
import hmac
import html as html_mod
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# The content/reader split lives next door: one implementation of what a page is
# made of, shared by the server that serves them and the tool that made them.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import split_lessons                                            # noqa: E402
import icon                                                     # noqa: E402

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

CONFIG_PATH = Path(os.environ.get("KCL_STUDY_CONFIG", "~/.kcl-study/config.json")).expanduser()
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
# vault wins on a machine that has more than one. "The Box" is in it because
# this project was built inside one and its author's config predates the scan,
# so keeping it guarantees his machine resolves exactly as it always has.
PREFERRED_VAULT_NAMES = ("The Box",)


def find_vault_courses():
    """Where to publish highlights, worked out from what is actually on the disk.

    Nothing is created here: if no vault is found the config carries a best
    guess and the server refuses to write until the path is real, rather than
    growing a convincing empty copy of somebody's notes beside it.

    🔴 This used to look ONLY for a folder called "The Box", which is the name
    of one person's vault. It therefore found nothing on any other machine, and
    since `--init` turns publishing on only when a vault is found, a recipient
    with a perfectly good vault called something else got the feature switched
    off with no way to discover why (plan 02 §9).

    So: the named places first, which keeps his machines resolving exactly as
    they did, then any real Obsidian vault in the usual roots. `.obsidian` is
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
    # The root that holds one folder per module. Empty means the single-module
    # world this project started in, where `notes_dir` IS the module and is
    # served from `/`. Both work; see resolve_modules().
    "courses_dir": "",
    "notes_dir": str(REPO / "notes"),
    # R8/R9. EH's choice of name and of a directory of its own, 2026-08-14.
    "resources_dir": str(REPO / "resources"),
    # 🔴 Off is the right default for anyone who is not EH (plan §10d item 1):
    # a recipient has no Obsidian vault, and a reader that boots with a vault
    # badge and a "vault not found" warning looks broken on arrival. His config
    # has it on; --init turns it on only when The Box is actually found.
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

# The shape this module happens to use, kept where a feature genuinely needs it
# (the vault's week and topic numbering) rather than as a gate on the whole
# reader.
WTP_RE = re.compile(r"^W\d{1,2}-T\d{1,2}-P\d{1,2}\Z")
# A colon is allowed because four of the module's topic titles contain one, and
# rejecting it silently blocked every vault write for those lessons: the badge
# said "not saved" and the highlights never reached The Box (found 2026-08-13).
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

def load_config(path=CONFIG_PATH):
    if not path.exists():
        sys.exit(
            "No config at %s\nRun:  python3 %s --init" % (path, Path(__file__).name)
        )
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(json.loads(path.read_text(encoding="utf-8")))

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


def init_config(path=CONFIG_PATH):
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
    # a machine with no Obsidian otherwise boots showing "The Box: vault not
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
    print("  claude: %s" % (cfg["claude_bin"] or "not found, Explain will be off"))


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


def create_module(root, module_id, name="", class_name=""):
    """Make a course folder and its identity file. Returns the folder.

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

def read_raw_config(path=CONFIG_PATH):
    """The config file exactly as it is on disk, with no defaults merged in.

    🔴 Defaults must not be merged here. This dict is written straight back, and
    a merged default would silently become a written setting: the difference
    between "not set, so it follows the default" and "pinned to what the default
    happened to be the day you changed your vault path"."""
    return json.loads(Path(path).read_text(encoding="utf-8"))


def save_raw_config(data, path=CONFIG_PATH):
    """Write the config, keeping a dated copy of what it said before.

    The backup is beside the original and is left there: a person cleans those
    up, not the process that made them. 0600 on both, because the token is in
    both."""
    path = Path(path)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(path.name + ".%s.bak" % stamp)
    shutil.copy2(path, backup)
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

    \U0001f534 A course with no `class_name` is the normal case, not the odd one: only
    the original module carries one, and `create_module` does not write it, so
    every course a new user makes arrives without it. Interpolating an empty
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
    # \U0001f534 Guarded for the same reason `new_reading_header` guards it: a
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


def reading_marks_md(items, palette):
    """The fenced body of a reading note, from its highlights.

    🔴 Bucketed by the course PALETTE, not by three colour literals. The
    client-side builder (buildVaultMd in shell.html) buckets only k, q and d
    and silently drops custom colours, which is a logged defect; this publisher
    must not inherit it. Bucket order is palette order, labels are the
    palette's own labels, and the define bucket keeps the vault's term:::
    syntax so the harvest finds it."""
    by_colour = {}
    for it in sorted(items, key=lambda i: (i.get("b", 0), i.get("s", 0))):
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
    items = [i for i in marks.get("items") or [] if not i.get("orphan")]
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

    🔴 The badge used to say "The Box", which is the name of ONE person's
    Obsidian vault. On anybody else's machine that is a proper noun they have
    never seen, attached to a feature they may have just switched on, and the
    reader looked like it had been built for somebody else because it had.

    `vault_courses` points at `<vault>/Courses`, so the vault's own folder name
    is its parent, and deriving it means EH still reads "The Box" while a
    recipient reads whatever they called theirs. "Vault" is the fallback for a
    path shaped in a way this cannot read."""
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
    # A wrong path would otherwise grow a convincing empty copy of The Box, and
    # every highlight would land in it unnoticed.
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
            # Back up before a substantial rewrite, per the house rule. One per
            # day is enough; the vault is also in Dropbox with version history.
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


_glossary_cache = {"mtime": 0.0, "data": {}}

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
        mtime = path.stat().st_mtime
        if mtime != _glossary_cache["mtime"]:
            _glossary_cache["data"] = json.loads(path.read_text(encoding="utf-8"))
            _glossary_cache["mtime"] = mtime
    except (OSError, ValueError):
        return None

    data = _glossary_cache["data"]
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
    nothing."""
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
            key = "img:" + "|".join([
                (local.get("wiki") or local["title"]).lower(),
                local.get("pic", ""), local.get("picOf", "")])
            shot = cache_get(cfg, key)
            if shot is None:
                shot = region_image(local.get("wiki") or local["title"],
                                    cfg["lookup_timeout"],
                                    pic=local.get("pic"),
                                    pic_of=local.get("picOf")) or {}
                cache_put(cfg, key, shot)
            if shot:
                local["image"] = shot
        sources.append(local)

    cached = cache_get(cfg, term.lower())
    if cached is not None:
        return {"term": term, "sources": sources + cached, "cached": True,
                "suggest": suggest}

    remote = []
    timeout = cfg["lookup_timeout"]
    for fn in (mesh_lookup, wikipedia_lookup):
        try:
            hit = fn(term, timeout)
            if hit:
                remote.append(hit)
        except Exception:
            continue
    cache_put(cfg, term.lower(), remote)
    return {"term": term, "sources": sources + remote, "suggest": suggest}


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
    preferences rather than machine state: they belong in Dropbox where both
    Macs see the same ones, and nothing in here is a secret.

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
    width = clean_panel_width(data.get("panelWidth"), DEFAULT_PANEL_W)
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
        "panelWidth": width,
        "panelWidthMin": PANEL_W_MIN,
        "panelWidthMax": PANEL_W_MAX,
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
    # Clamped rather than rejected: a drag that ends outside the bounds should
    # settle at the bound, not throw away the whole save (which carries the
    # palette with it).
    width = clean_panel_width(payload.get("panelWidth"), current["panelWidth"])
    vault_on = payload.get("vaultEnabled", current["vaultEnabled"])
    if not isinstance(vault_on, bool):
        raise ValueError("vaultEnabled must be true or false")
    # 🔴 Merged into what is already in the file, never written over it. The same
    # file can carry a `module` block (its id, name, class and store prefix), and
    # replacing the whole document would delete the module's identity every time
    # he dragged the panel wider.
    keep = dict(read_json_sidecar(settings_path(cfg), {}))
    keep.update({
        "model": model, "level": lvl, "palette": palette, "lastColour": last,
        "panelSize": size, "panelWidth": width, "vaultEnabled": vault_on,
    })
    write_json_sidecar(settings_path(cfg), keep)
    return {"ok": True, "model": model, "level": lvl,
            "palette": palette, "lastColour": last, "panelSize": size,
            "panelWidth": width, "vaultEnabled": vault_on}


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


def run_cli(cfg, prompt):
    """One shot at the Claude Code CLI. No API key: it uses the subscription the
    CLI is already signed in with."""
    if not cfg.get("explain_enabled"):
        return {"ok": False, "error": "Explain is switched off in the config."}
    binary = cfg.get("claude_bin") or ""
    if not binary or not Path(binary).exists():
        from shutil import which
        binary = which("claude") or ""
    if not binary:
        return {"ok": False, "error": "The Claude Code CLI was not found. Set claude_bin in the config."}

    cwd = cfg["cache_dir"] / "explain-cwd"
    cwd.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.run(
            [binary, "-p", prompt, "--model", chosen_model(cfg)],
            cwd=str(cwd), capture_output=True, text=True,
            timeout=int(cfg["explain_timeout"]),
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "Timed out waiting for an answer."}
    except OSError as err:
        return {"ok": False, "error": "Could not run the CLI: %s" % err}

    if proc.returncode != 0:
        detail = (proc.stderr or "").strip().splitlines()
        return {"ok": False, "error": detail[-1] if detail else "The CLI exited with an error."}

    text = (proc.stdout or "").strip()
    if not text:
        return {"ok": False, "error": "Empty answer."}
    return {"ok": True, "text": text.replace("—", ", ")}


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


def do_ask(cfg, payload):
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

    out = run_cli(cfg, prompt)
    if out.get("ok"):
        out["model"] = chosen_model(cfg)
        out["term"] = term
    return out


# --------------------------------------------------------------------------
# annotations: kept beside the note, never inside it
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# marks: the source of truth, on disk, in Dropbox
# --------------------------------------------------------------------------

def sidecar_path(cfg, doc_id, suffix):
    if not DOC_ID_RE.match(doc_id or ""):
        raise ValueError("bad doc id")
    return cfg["notes_dir"] / ("%s-%s.json" % (doc_id, suffix))


def read_json_sidecar(path, fallback):
    if not path.exists():
        return dict(fallback)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else dict(fallback)
    except (ValueError, OSError):
        return dict(fallback)


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
            res = folder / "resources"
            if res.is_dir():
                roots.extend(p for p in res.iterdir() if p.is_dir()
                             and p.name != split_lessons.BACKUP_DIRNAME)
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


def write_cards(cfg, doc_id, payload):
    cards = payload.get("cards")
    if not isinstance(cards, dict):
        raise ValueError("cards must be an object")
    if len(cards) > MAX_CARDS:
        raise ValueError("too many cards")
    clean = {}
    for key, row in cards.items():
        if not isinstance(row, dict):
            continue
        # Two kinds of key, because a card can come from two places (R5). A card
        # made on the page is keyed by its mark id, an integer the page issues.
        # A card made inside a chat answer has no mark to key on, so it carries
        # its own "c" + base36 stamp. Keeping them in one file means one Cards
        # list rather than two, which is what he asked for.
        if not re.match(r"^(\d{1,9}|c[a-z0-9]{1,24})$", str(key)):
            continue
        src = str(row.get("from") or "")
        clean[str(key)] = {
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
    write_json_sidecar(sidecar_path(cfg, doc_id, "cards"),
                       {"doc": doc_id, "cards": clean,
                        "saved": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    return {"ok": True, "doc": doc_id, "cards": len(clean)}


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
    write_json_sidecar(sidecar_path(cfg, doc_id, "bookmarks"),
                       {"doc": doc_id, "marks": clean,
                        "saved": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    return {"ok": True, "doc": doc_id, "marks": len(clean)}


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
        cleanchats.append({"c": c,
                           "t": str((row or {}).get("t") or "")[:300] if isinstance(row, dict) else "",
                           "at": str((row or {}).get("at") or "")[:40] if isinstance(row, dict) else ""})

    write_json_sidecar(sidecar_path(cfg, doc_id, "chatmarks"),
                       {"doc": doc_id, "marks": clean, "chats": cleanchats,
                        "saved": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    return {"ok": True, "doc": doc_id, "marks": len(clean), "chats": len(cleanchats)}


def read_chats(cfg, doc_id):
    """Saved Explain conversations for one note. Same sidecar pattern as marks
    and additions: a plain JSON file next to the note, in Dropbox, so a
    conversation is still there next week and is readable without the page."""
    return read_json_sidecar(sidecar_path(cfg, doc_id, "chats"),
                             {"doc": doc_id, "chats": []})


def write_chats(cfg, doc_id, payload):
    chats = payload.get("chats")
    if not isinstance(chats, list):
        raise ValueError("chats must be a list")
    if len(chats) > 200:
        raise ValueError("too many chats")
    clean = []
    for c in chats[:200]:
        if not isinstance(c, dict):
            continue
        turns = c.get("turns")
        if not isinstance(turns, list):
            continue
        clean.append({
            "id": c.get("id"),
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
    doc = {
        "doc": doc_id,
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "chats": clean,
    }
    write_json_sidecar(sidecar_path(cfg, doc_id, "chats"), doc)
    return {"ok": True, "chats": len(clean)}


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


def resources_root(cfg):
    return Path(cfg.get("resources_dir") or (REPO / "resources")).expanduser()


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
                "url": "/resources/%s/%s" % (doc_id, urllib.parse.quote(p.name)),
            })
    # Anything given an order comes first, in that order; everything else keeps
    # its alphabetical place underneath. So ordering a few files by hand does not
    # oblige him to order all of them.
    out.sort(key=lambda f: (f["order"] is None, f["order"] if f["order"] is not None else 0))
    return {"ok": True, "doc": doc_id, "files": out, "count": len(out)}


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
    # in this project.
    with WRITE_LOCK:
        if target.exists():
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            target.replace(split_lessons.backup_target(
                target, "%s.%s.bak" % (target.name, stamp)))
        target.write_bytes(data)
    return {"ok": True, "doc": doc_id, "name": safe, "size": len(data),
            "kind": resource_kind(target.suffix),
            "url": "/resources/%s/%s" % (doc_id, urllib.parse.quote(safe))}


def delete_resource(cfg, doc_id, name):
    target = resolve_resource(cfg, doc_id, name)
    if target is None:
        raise ValueError("no such file")
    # 🔴 Renamed, never unlinked. He may have attached the only copy of something,
    # and this button is one tap away from a list on a phone.
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    with WRITE_LOCK:
        target.replace(split_lessons.backup_target(
            target, "%s.%s.bak" % (target.name, stamp)))
    return {"ok": True, "doc": doc_id, "name": target.name, "removed": True}


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
    """`<DOC> - <Word> (whatever).pdf` in that folder, or None.

    Matched by PREFIX because the skill keeps the download's original name in
    parentheses after the standard part, so the tail is not predictable. The
    match is case-insensitive and the first hit in sorted order wins, so two
    files for one part behave the same way on every run rather than depending on
    the order the filesystem happens to hand them back."""
    if folder is None:
        return None
    want = ("%s - %s" % (doc_id, word)).lower()
    try:
        hits = sorted(q for q in folder.iterdir()
                      if q.is_file() and q.name.lower().startswith(want))
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
    if not path.exists():
        # 🔴 The old message said "run build_materials.py", which is advice only
        # this machine can take. A module built from shared lesson packs has no
        # materials file and never will unless somebody imports links or scans
        # KEATS, and its reader should say which.
        return {"ok": False, "error": "This module has no materials index, so there "
                "is nothing to point at yet. Import lessons with their links, or "
                "scan the module on KEATS."}
    try:
        docs = json.loads(path.read_text(encoding="utf-8")).get("docs", {})
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": "materials.json is unreadable: %s" % exc}
    entry = docs.get(doc_id)
    if not entry:
        return {"ok": False, "error": "no materials recorded for %s" % doc_id}
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
                # access_for() already answers "local" for anything that is not
                # an http address, so the note the pane shows needs no new case.
                for field in (key, key + "_embed"):
                    kind, note = access_for(url)
                    out["access"][field] = kind
                    if note:
                        out["access_notes"][kind] = note
    return out


def read_marks(cfg, doc_id):
    """Highlights and free notes, anchored. This is the copy that can rebuild the
    page; the vault note is a readable publication with the offsets thrown away,
    so it cannot."""
    path = sidecar_path(cfg, doc_id, "marks")
    return read_json_sidecar(path, {"doc": doc_id, "updated": 0, "items": [], "notes": []})


def write_marks(cfg, doc_id, payload):
    items = payload.get("items")
    notes = payload.get("notes")
    if not isinstance(items, list) or not isinstance(notes, list):
        raise ValueError("items and notes must both be lists")
    if len(items) > 2000 or len(notes) > 500:
        raise ValueError("too many marks")
    path = sidecar_path(cfg, doc_id, "marks")

    # Today's snapshot, if it has not been taken yet. Before the write, so the
    # copy is of what was there rather than of what is about to replace it.
    snapshot_sidecars(cfg)

    # A shrinking write is either a deliberate deletion or a stale browser
    # overwriting good data, and this function cannot tell which. On 2026-08-13
    # it was the second: a browser profile holding a two-mark cache from days
    # earlier replaced fifteen marks and two free notes, and nothing here kept a
    # copy, so the only routes back were Dropbox history and the vault export.
    #
    # So keep one. This does not choose a winner and does not change what the
    # client may do; it only means the losing copy still exists afterwards.
    try:
        before = read_json_sidecar(path, None)
    except Exception:
        before = None
    if before:
        was = len(before.get("items", [])) + len(before.get("notes", []))
        now = len(items) + len(notes)
        if was > now:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            bak = split_lessons.backup_target(
                path, "%s.shrank-%s.bak" % (path.name, stamp))
            try:
                bak.write_text(json.dumps(before, indent=2, ensure_ascii=False),
                               encoding="utf-8")
                log(cfg, "marks %s SHRANK %d -> %d, kept %s"
                    % (doc_id, was, now, bak.name))
            except OSError as exc:
                log(cfg, "marks %s shrank %d -> %d and the backup FAILED: %s"
                    % (doc_id, was, now, exc))

    doc = {
        "doc": doc_id,
        "updated": int(payload.get("updated") or 0),
        "saved": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "items": items,
        "notes": notes,
    }
    write_json_sidecar(path, doc)
    return {"ok": True, "items": len(items), "notes": len(notes)}


def colour_uses(cfg, colour):
    """R45. How many highlights carry this colour, across every lesson. The
    delete-a-colour confirm shows this so 'also delete its highlights' is a
    decision about a known number, not a guess."""
    count, lessons = 0, 0
    for path in sorted(cfg["notes_dir"].glob("*-marks.json")):
        data = read_json_sidecar(path, None) or {}
        n = sum(1 for it in data.get("items", [])
                if isinstance(it, dict) and it.get("c") == colour)
        if n:
            count += n
            lessons += 1
    return {"ok": True, "colour": colour, "count": count, "lessons": lessons}


def colour_purge(cfg, payload):
    """R45. Remove every highlight of one colour, in every lesson, going through
    write_marks per document so the shrink guard keeps a dated backup of each
    file it shrinks. The palette entry itself is retired by the client in the
    same breath; this only touches marks."""
    colour = str(payload.get("c") or "")
    if not PALETTE_ID_RE.match(colour):
        raise ValueError("bad colour id")
    if colour == "k":
        raise ValueError("the default colour cannot be purged")
    removed, touched = 0, []
    for path in sorted(cfg["notes_dir"].glob("*-marks.json")):
        data = read_json_sidecar(path, None) or {}
        items = data.get("items", [])
        keep = [it for it in items
                if not (isinstance(it, dict) and it.get("c") == colour)]
        if len(keep) == len(items):
            continue
        doc_id = str(data.get("doc") or path.name.rsplit("-marks.json", 1)[0])
        write_marks(cfg, doc_id, {"items": keep,
                                  "notes": data.get("notes", []),
                                  "updated": data.get("updated") or 0})
        removed += len(items) - len(keep)
        touched.append(doc_id)
        log(cfg, "palette purge %s: %s lost %d mark(s)"
            % (colour, doc_id, len(items) - len(keep)))
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
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = split_lessons.backup_target(path, "%s.%s.bak" % (path.name, stamp))
    bak.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
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


def do_rewrite(cfg, payload):
    if not cfg.get("explain_enabled"):
        return {"ok": False, "error": "The Claude CLI is switched off in the config."}
    binary = cfg.get("claude_bin") or ""
    if not binary or not Path(binary).exists():
        from shutil import which
        binary = which("claude") or ""
    if not binary:
        return {"ok": False, "error": "The Claude Code CLI was not found."}

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

    cwd = cfg["cache_dir"] / "explain-cwd"
    cwd.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.run(
            [binary, "-p", prompt, "--model", chosen_model(cfg)],
            cwd=str(cwd), capture_output=True, text=True,
            timeout=int(cfg["explain_timeout"]),
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "Timed out waiting for the rewrite."}
    except OSError as err:
        return {"ok": False, "error": "Could not run the CLI: %s" % err}

    if proc.returncode != 0:
        detail = (proc.stderr or "").strip().splitlines()
        return {"ok": False, "error": detail[-1] if detail else "The CLI exited with an error."}

    text = (proc.stdout or "").strip()
    if not text:
        return {"ok": False, "error": "Empty answer."}
    if "—" in text:
        text = text.replace("—", ", ")
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


def write_additions(cfg, doc_id, payload):
    """The sidecar exists so a regenerated note never destroys his additions.
    The HTML is mine to rewrite; this file is his."""
    path = additions_path(cfg, doc_id)
    items = payload.get("items")
    if not isinstance(items, list) or len(items) > 500:
        raise ValueError("bad items")
    doc = {
        "doc": doc_id,
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "items": items[:500],
    }
    with WRITE_LOCK:
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    return {"ok": True, "count": len(items)}


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

SHELL_PATH = Path(__file__).resolve().parent / "reader" / "shell.html"
LAYER_PATH = Path(__file__).resolve().parent / "local-layer.html"

_READER_LOCK = threading.Lock()
_READER_CACHE = {}      # path -> (mtime_ns, size, text)


def read_reader_part(path):
    """Cached by mtime and size, so a saved edit is picked up without a restart
    and an unchanged file is not read 29 times a session."""
    st = path.stat()
    key = str(path)
    with _READER_LOCK:
        hit = _READER_CACHE.get(key)
        if hit and hit[0] == st.st_mtime_ns and hit[1] == st.st_size:
            return hit[2]
    text = path.read_text(encoding="utf-8")
    with _READER_LOCK:
        _READER_CACHE[key] = (st.st_mtime_ns, st.st_size, text)
    return text


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
        gear.append('<a href="#addcourse">Add a course</a>')
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
  .addbox { margin-top:18px; background:var(--surface); border:1px solid var(--rule);
            border-radius:12px; padding:16px 18px; }
  .addbox h2 { font-family:var(--display); font-size:1.05rem; font-weight:600;
               margin:0 0 6px; }
  .addbox p { margin:0 0 12px; color:var(--ink-soft); font-size:.9rem; max-width:56ch; }
  .addrow { display:flex; gap:10px; flex-wrap:wrap; align-items:center; }
  .addrow input {
    flex:1 1 190px; min-width:0; font:15px var(--text); color:var(--ink);
    background:var(--paper); border:1px solid var(--rule); border-radius:8px;
    padding:11px 12px;
  }
  .addrow input:focus-visible { outline:2px solid var(--accent); outline-offset:1px; }
  .addrow button {
    font:600 14px var(--text); color:var(--paper); background:var(--accent);
    border:1px solid var(--accent); border-radius:8px; padding:11px 18px;
    cursor:pointer; flex:0 0 auto;
  }
  .addrow button:disabled { opacity:.55; cursor:default; }
  .says { margin:10px 0 0; font-size:.85rem; color:var(--ink-soft); min-height:1.2em; }
  .says.bad { color:var(--broken,#A25E14); }
  .addbox .hint { margin:14px 0 0; font-size:.82rem; color:var(--muted);
                  max-width:62ch; line-height:1.6; }
  .addbox .hint b { color:var(--ink-soft); font-weight:600; }
  .addbox b { color:var(--ink); font-weight:600; }
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
                        background:var(--accent); color:#fff; cursor:pointer; }
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
ADD_COURSE_BLOCK = """
  <div class="addbox" id="addcourse">
    <h2>Add a course</h2>
    <p>This makes the folder. The lessons come afterwards, and the course page
       tells you the three ways to get them in.</p>
    <div class="addrow">
      <input id="newid" type="text" placeholder="Course code, e.g. PSY101"
             autocomplete="off" spellcheck="false" aria-label="Course code">
      <input id="newname" type="text" placeholder="Its name (optional)"
             autocomplete="off" aria-label="Course name">
      <button type="button" id="addbtn">Add</button>
    </div>
    <p class="hint"><b>The code is not a label.</b> It names the folder your
       lessons live in, it is the web address of the course, and it is what keeps
       this course’s highlights separate from every other course’s. Pick the one
       your institution uses, and pick it once: changing it later means moving the
       folder and losing what you have marked. The name is only what you see, and
       you can change that whenever you like.</p>
    <p class="says" id="says" role="status"></p>
  </div>
<script>
(function () {
  var idEl = document.getElementById('newid');
  var nameEl = document.getElementById('newname');
  var btn = document.getElementById('addbtn');
  var says = document.getElementById('says');

  function tell(msg, bad) {
    says.textContent = msg;
    says.classList.toggle('bad', !!bad);
  }

  function add() {
    var id = (idEl.value || '').trim();
    if (!id) { tell('Give the course a code first.', true); idEl.focus(); return; }
    btn.disabled = true;
    tell('Adding\\u2026');
    fetch('/api/modules', {
      method: 'POST',
      headers: window.STUDYTOKEN.headers({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ id: id, name: (nameEl.value || '').trim() })
    }).then(function (r) {
      return r.json().then(function (j) { return { status: r.status, body: j }; });
    }).then(function (r) {
      if (r.status === 401) {
        /* Ask here, and try the same course again once it is saved. What was
           typed stays in the boxes, which is the whole point of retrying rather
           than reloading. */
        btn.disabled = false;
        tell('');
        window.STUDYTOKEN.ask(add);
        return;
      }
      if (!r.body || !r.body.ok) {
        tell((r.body && r.body.error) || 'That did not work.', true);
        btn.disabled = false;
        return;
      }
      tell('Added. Opening it\\u2026');
      /* Straight into the wizard, which is the moment its questions are cheap.
         It is a door, not a gate: its skip link is the course page. */
      window.location.href = (r.body.url || '/') + 'start';
    }).catch(function () {
      tell('Could not reach the server.', true);
      btn.disabled = false;
    });
  }

  btn.addEventListener('click', add);
  [idEl, nameEl].forEach(function (el) {
    el.addEventListener('keydown', function (ev) {
      if (ev.key === 'Enter') { ev.preventDefault(); add(); }
    });
  });
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
# \U0001F534 It is never a gate. Every step has a way straight to the course,
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
           background:var(--accent); color:#fff; cursor:pointer; }
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
      <label class="ptop"><input type="checkbox" id="w-consol">
        <span><b>Consolidated PDFs</b>, one per week and one for the whole
          course, for slides and for transcripts</span>
        <span class="already" id="a-consol"></span></label>
      <div class="popts" id="o-consol">
        <p class="hint">Built from the downloaded slides and transcripts after
           they are cleaned up, so a week reads and prints as one document.
           Needs the course material downloaded (the Lessons job above does
           that, or say where your folder is in the prompt).</p>
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
              consol: document.getElementById('w-consol'),
              readings: document.getElementById('w-readings') };
  var opts = { lessons: document.getElementById('o-lessons'),
               videos: document.getElementById('o-videos'),
               consol: document.getElementById('o-consol'),
               readings: document.getElementById('o-readings') };
  var already = { lessons: document.getElementById('a-lessons'),
                  videos: document.getElementById('a-videos'),
                  consol: document.getElementById('a-consol'),
                  readings: document.getElementById('a-readings') };
  var folders = { lessons: document.getElementById('f-lessons'),
                  readings: document.getElementById('f-readings') };
  var TOKENS = { lessons: '<paste the slides folder here>',
                 readings: '<paste the readings folder here>' };

  /* Defaults are the truth: a piece the course lacks starts ticked, a piece
     it already has starts unticked and says so, and either can be changed. */
  box.lessons.checked = !STATUS.lessons;
  box.videos.checked = !STATUS.videos;
  box.readings.checked = !STATUS.readings;
  /* Consolidated PDFs are an OPTION a person selects (EH, 2026-08-23), like
     the media downloads: never pre-ticked, whatever the course has. */
  box.consol.checked = false;
  already.lessons.textContent = STATUS.lessons
    ? 'already in: ' + STATUS.lessons + ' lesson' + (STATUS.lessons === 1 ? '' : 's') : '';
  already.videos.textContent = STATUS.videos
    ? 'already wired for ' + STATUS.videos : '';
  already.readings.textContent = STATUS.readings
    ? 'already in: ' + STATUS.readings : '';
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
  function frag(piece) {
    if (piece === 'consol') { return FRAGS.consol; }
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
    if (src(piece) === 'local') {
      var f = folders[piece].value.trim();
      if (f) { text = text.replace(TOKENS[piece], f); }
    }
    return text;
  }
  var copyBtn = document.getElementById('gocopy');
  var promptEl = document.getElementById('goprompt');
  function compose() {
    ['lessons', 'readings'].forEach(function (p) {
      opts[p].hidden = !box[p].checked;
      folders[p].hidden = (src(p) !== 'local');
    });
    opts.videos.hidden = !box.videos.checked;
    opts.consol.hidden = !box.consol.checked;
    document.getElementById('o-dl').hidden = !dl.gate.checked;
    var jobs = [];
    ['lessons', 'videos', 'consol', 'readings'].forEach(function (p) {
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
  var ssays = document.getElementById('ssays');
  shareBtn.addEventListener('click', function () {
    shareBtn.disabled = true;
    ssays.textContent = 'Packing the course\u2026';
    ssays.classList.remove('bad');
    var headers = window.STUDYTOKEN ? window.STUDYTOKEN.headers({}) : {};
    fetch('/api/share?module=' + encodeURIComponent(%(codejs)s),
          { method: 'POST', headers: headers })
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
          (rep.mistakes || 0) + ' mistakes-page entries.';
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
# \U0001F534 EH, 2026-08-22: "I just want to make sure that the paper
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
  .hmeters { display: flex; flex-direction: column; gap: 6px; margin: 0 0 18px; max-width: 520px; }
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
  .hrates { display: inline-flex; }
  .hrate { font-size: 15px; line-height: 1; padding: 1px 1px; cursor: pointer;
           background: none; border: 0; color: var(--rule, #8884); }
  .hrate:hover { transform: scale(1.15); }
  .hstar.on { color: var(--accent, #1C6D61); }
  /* 🔴 A bulb is an emoji and carries its own colour, so `color` cannot dim it
     the way it dims a star. Greyed and faded is the off state, full colour the
     on one, which reads at a glance without inventing a second glyph. */
  .hbulb { filter: grayscale(1); opacity: .4; font-size: 13px; }
  .hbulb.on { filter: none; opacity: 1; }
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
# The consolidated-PDFs option (EH's design, 2026-08-23): weekly and whole-
# course PDFs for slides and transcripts, built AFTER pdf_fix so they inherit
# clean orientation and OCR. An option, never a default; re-askable later like
# every piece, which is what "joins that checklist" means.
WIZ_CONSOL = ('Build the consolidated PDFs: after the slides and transcripts '
              'are downloaded and have been through the pdf-fix step, run '
              'server/consolidate_pdfs.py on their folder with --module '
              '%(course)s. It writes one PDF per week and one for the whole '
              'course, for the slide decks and for the transcripts, into the '
              "course's consolidated folder, and verifies every page count. "
              'Show me its output.')

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
                border-radius:8px; background:var(--accent); color:#fff; cursor:pointer; }
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
                    background:var(--accent); color:#fff; cursor:pointer; }
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
                border-radius:8px; background:var(--accent); color:#fff; cursor:pointer; }
  .ask button.plain { background:transparent; color:var(--ink-soft);
                      border:1px solid var(--rule); }
  .says { margin:12px 0 0; font-size:.85rem; color:var(--ink-soft); min-height:1.2em; }
  .says.bad { color:var(--broken); }
  .danger button {
    font:600 13px var(--text); color:var(--broken); background:var(--paper);
    border:1px solid var(--rule); border-radius:8px; padding:9px 14px; cursor:pointer;
  }
  .danger button:hover { border-color:var(--broken); }
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

  %(vault)s

  <section>
    <h2>Highlight colours</h2>
    <p class="hint">Shown here so you can see what you have. They are edited
       inside a lesson, where the colours sit on real text.</p>
    <div class="swatches">%(palette)s</div>
  </section>

  %(courses)s

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
  }
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
            # "The Box" they do not have, which reads as a misconfiguration.
            "vault": (str(cfg.get("vault_courses") or "")
                      if vault_on(cfg) else ""),
            "server": "http://%s:%s/" % (cfg["bind_ip"], cfg["port"])}


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
        nav=nav, state=state)


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
        answer = {"ok": True, "version": ver, "check": "on", "newer": newer,
                  "latest": latest,
                  "url": str(remote.get("url") or "")[:500],
                  "note": str(remote.get("note") or "")[:300]}
    except (OSError, ValueError):
        answer = {"ok": True, "version": ver, "check": "unreachable"}
    _UPDATE_CACHE["at"] = now
    _UPDATE_CACHE["answer"] = answer
    return answer


# --------------------------------------------------------------------------
# http
# --------------------------------------------------------------------------

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
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
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".otf": "font/otf",
}


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
        port = self.cfg["port"]
        hosts = {"127.0.0.1:%d" % port, "localhost:%d" % port, "[::1]:%d" % port}
        if not is_loopback(self.cfg["bind_ip"]):
            hosts.add("%s:%d" % (self.cfg["bind_ip"], port))
        for h in (self.cfg.get("extra_hosts") or []):
            h = str(h).strip().lower().rstrip(".")
            if h:
                hosts.add("%s:%d" % (h, port))
        return hosts

    def _host_ok(self):
        """DNS rebinding is the whole threat model for a server that can run a
        subprocess. A stranger's page can reach an IP, but it cannot forge these
        headers."""
        allowed = self._allowed_hosts()
        host = (self.headers.get("Host") or "").strip().lower()
        if host not in allowed:
            return False
        origin = (self.headers.get("Origin") or "").strip().lower()
        if origin and origin not in {"http://" + h for h in allowed}:
            return False
        return True

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
        his own devices, and a top-level navigation cannot carry a header."""
        if is_loopback(self.cfg["bind_ip"]):
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
    # An explicit `module=` (query) or `"module"` (body) wins where a caller has
    # one, which is how the home page and any tooling address a module directly.

    MODULE_PATH_RE = re.compile(r"^/m/([A-Za-z0-9][A-Za-z0-9._-]{0,63})(/.*)?$")

    def _module_from_referer(self):
        ref = self.headers.get("Referer") or ""
        try:
            path = urllib.parse.urlparse(ref).path
        except ValueError:
            return None
        m = self.MODULE_PATH_RE.match(path or "")
        return m.group(1) if m else None

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

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(parsed.path)

        if path == "/healthz":
            return self._text("ok\n")

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

        if not self._host_ok():
            return self._text("bad host\n", 403)

        query = urllib.parse.parse_qs(parsed.query)

        if path.startswith("/api/"):
            if not self._auth_ok():
                return self._json({"ok": False, "error": "token required"}, 401)
            self._use_module((query.get("module") or [None])[0])
            if path == "/api/update":
                return self._json(update_status(self.base_cfg))
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
                return self._package(rest)
            if rest.startswith("/videos/"):
                return self._course_video(rest)
            if rest.startswith("/materials/"):
                return self._course_material(rest)
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
            dest = Path.home() / "Desktop"
            if not dest.is_dir():
                dest = Path.home()
            try:
                # 🔴 `with_drive` is left at its default of False: a shared course
                # carries the KEATS links (which gate on the recipient's own
                # enrolment) and NOT the exporter's own Drive addresses. EH's
                # ruling, 2026-08-28. There is deliberately no way to turn it on
                # from the page: the flag exists for `lesson_packs.py` copying
                # between one person's own installs.
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
                out = do_ask(self.cfg, payload)
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
                log(self.cfg, "marks %s items=%d notes=%d" % (doc, out["items"], out["notes"]))
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
                else:
                    raise ValueError("nothing to change")
                backup = save_raw_config(raw)
                log(self.cfg, "machine config changed (%s), previous kept at %s"
                    % (", ".join(sorted(k for k in ("vaultCourses", "coursesDir")
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
                folder = create_module(root, str(payload.get("id") or "").strip(),
                                       str(payload.get("name") or "").strip(),
                                       str(payload.get("class_name") or "").strip())
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
                log(self.cfg, "cards %s count=%d" % (doc, out["cards"]))
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
                log(self.cfg, "chats %s count=%d" % (doc, out["chats"]))
                return self._json(out)

            if path == "/api/followup":
                out = do_ask(self.cfg, payload)
                log(self.cfg, "followup %r ok=%s"
                    % (str(payload.get("question", ""))[:60], out.get("ok")))
                return self._json(out, 200 if out.get("ok") else 503)

            if path == "/api/rewrite":
                out = do_rewrite(self.cfg, payload)
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
            from shutil import which
            binary = self.cfg.get("claude_bin") or which("claude") or ""
            return self._json({
                "ok": True,
                "explain": bool(self.cfg.get("explain_enabled")) and bool(binary),
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
            return self._import_zip(data, name)
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
                done, skipped, merged = lesson_packs.import_packs(
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
        return self._json({"ok": True, "kind": "course-pack", "counts": counts,
                           "message": "In: %d glossary terms, %d readings, %d "
                                      "mistakes." % (counts["glossary"],
                                                     counts["readings"],
                                                     counts["mistakes"])})

    def _import_zip(self, data, name):
        """A shared course, one dragged file. Unpacked flat into a temp folder
        (member names are reduced to basenames, so a hostile path cannot leave
        it), then imported by the same two functions the pieces use alone."""
        import io
        import tempfile
        import zipfile
        import lesson_packs
        counts = {"lessons": 0, "skipped": 0, "links": 0,
                  "glossary": 0, "readings": 0, "mistakes": 0}
        try:
            with tempfile.TemporaryDirectory() as tmp:
                tmpdir = Path(tmp)
                packs = []
                with zipfile.ZipFile(io.BytesIO(data)) as z:
                    for m in z.infolist():
                        base = os.path.basename(m.filename)
                        if m.is_dir() or not base or base.startswith("."):
                            continue
                        if m.file_size > LESSON_MAX_BYTES:
                            return self._json(
                                {"ok": False, "error": "%s inside the zip is "
                                 "too big to be a lesson" % base[:60]}, 400)
                        (tmpdir / base).write_bytes(z.read(m))
                        packs.append(base)
                if not packs:
                    return self._json({"ok": False,
                                       "error": "that zip is empty"}, 400)
                try:
                    done, skipped, merged = lesson_packs.import_packs(
                        {"notes_dir": str(self.cfg["notes_dir"])},
                        [str(tmpdir)], quiet=True)
                    counts["lessons"], counts["skipped"] = len(done), len(skipped)
                    counts["links"] = merged
                except lesson_packs.Problem:
                    pass    # a zip of only sidecars is legitimate
                for f in tmpdir.glob("*.json"):
                    try:
                        doc = json.loads(f.read_text(encoding="utf-8"))
                    except ValueError:
                        continue
                    if isinstance(doc, dict) and doc.get("course_pack"):
                        got = lesson_packs.import_course_pack(
                            self.cfg["notes_dir"], doc)
                        for k in ("glossary", "readings", "mistakes"):
                            counts[k] += got[k]
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

        ctype = CONTENT_TYPES.get(target.suffix.lower(), "application/octet-stream")
        try:
            data = target.read_bytes()
        except OSError:
            return self._text("not found\n", 404)
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
        # 🔴 The sandbox goes ONLY on the two types that can carry script, and
        # scoping it that way is a bug fix rather than a nicety: applied to
        # everything it also broke the PDF viewer, which is a browser component
        # that needs to run, so every attached paper displayed as a blank frame.
        # `sandbox` with no allow-list is the strong form: an opaque origin, so an
        # attached page cannot read localStorage, cannot use the token, and cannot
        # call the API. allow-same-origin was in the first version and undoes
        # exactly that, which is the opposite of the point.
        if ctype.startswith("text/html") or ctype == "image/svg+xml":
            self.send_header("Content-Security-Policy", "sandbox")
        self.end_headers()
        self.wfile.write(data)

    def _package(self, rest):
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
        already shows to anyone who can reach it."""
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
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        # A mirrored file never changes in place (a re-mirror replaces the
        # folder), and a narration MP3 re-fetched on every slide is the reader
        # feeling slow for no reason.
        self.send_header("Cache-Control", "public, max-age=3600")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Access-Control-Allow-Origin", "*")
        if ctype.startswith("text/html") or ctype == "image/svg+xml":
            self.send_header("Content-Security-Policy", "sandbox allow-scripts")
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
        ctype = CONTENT_TYPES.get(candidate.suffix.lower(), "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        # Shown in the pane, never offered as a download: the file is already on
        # their disk, and a download button for a file you own is a confusion.
        self.send_header("Content-Disposition", "inline")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _course_video(self, rest):
        """A downloaded lecture recording, out of videos/ and nothing else.

        The one route that honours Range. A recording is hundreds of megabytes
        and a <video> seeks by asking for byte ranges; served whole with a 200,
        seeking degrades to "download everything first". Single ranges only,
        which is all a player sends."""
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
        start, end, status = 0, size - 1, 200
        rng = self.headers.get("Range", "")
        if rng.startswith("bytes=") and "," not in rng:
            spec = rng[len("bytes="):].strip()
            try:
                a, _, b = spec.partition("-")
                if a:
                    start = int(a)
                    end = int(b) if b else size - 1
                elif b:
                    start = max(0, size - int(b))
                if start > end or start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", "bytes */%d" % size)
                    self.end_headers()
                    return
                end = min(end, size - 1)
                status = 206
            except ValueError:
                start, end, status = 0, size - 1, 200
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
            rows.append('<section class="hweek"><h2 class="hwh">%s%s</h2>'
                        % (whead, wrate))
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
                    rows.append('<div class="htopic">'
                                + ('<h3 class="hth">%s%s</h3>' % (thead, trate)
                                   if (thead or trate) else ""))
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
                    '<div class="hrow%s" data-doc="%s">'
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
                    '<div class="hmeters">%s%s</div>'
                    % (len(opened), n,
                       (' Continue where you left off: <a href="%s">%s</a>.'
                        % (esc(metas[resume].get("file") or "", quote=True),
                           esc(show_title(metas[resume], resume))))
                       if resume else "",
                       meter("Read", nread), meter("Watched", nwatched)))

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
        status = {"lessons": nlessons, "videos": nvideos,
                  "readings": nreadings, "consolidated": nconsol,
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
        from shutil import which
        binary = self.base_cfg.get("claude_bin") or which("claude") or ""
        explain_on = bool(self.base_cfg.get("explain_enabled")) and bool(binary)

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

        page = SETTINGS_PAGE % {
            "icons": HEAD_ICONS,
            "navbar": nav_bar(self.base_cfg, here="Settings"),
            "back": "/" if multi else "/",
            "backlabel": "All your courses" if multi else "Back to the lessons",
            "explain": ("" if explain_on else
                        "Explain is off on this machine, because the Claude "
                        "command was not found."),
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
            "tokenbar": TOKEN_BAR,
            "state": json.dumps({"level": cur.get("level"),
                                 "panelSize": cur.get("panelSize"),
                                 "model": cur.get("model"),
                                 "vaultEnabled": cur.get("vaultEnabled", True)}),
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
            'already have, and it will appear here.</p></div>' % esc(info["root"]))

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
            "extra": TOKEN_BAR + ADD_COURSE_BLOCK,
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


def main():
    ap = argparse.ArgumentParser(description="KCL study server")
    ap.add_argument("--init", action="store_true", help="scaffold the config and exit")
    ap.add_argument("--config", default=None, help="config path")
    args = ap.parse_args()

    path = Path(args.config).expanduser() if args.config else CONFIG_PATH
    if args.init:
        return init_config(path)

    cfg = load_config(path)
    tidy_stray_baks(cfg)
    Handler.base_cfg = cfg
    Handler.cfg = cfg
    httpd = ThreadingHTTPServer((cfg["bind_ip"], cfg["port"]), Handler)
    url = "http://%s:%d/" % (cfg["bind_ip"], cfg["port"])
    log(cfg, "start %s" % url)
    print("Study notes at %s" % url)
    print("Vault target: %s" % cfg["vault_courses"])
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("")
    finally:
        log(cfg, "stop")


if __name__ == "__main__":
    main()
