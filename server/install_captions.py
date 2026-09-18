#!/usr/bin/env python3
"""Install the caption engine once, into a folder outside the kit.

**EH's rule, 2026-09-17, in his words:** *"Everything that's not content should be
part of the kit, including everything that generates, optimizes, or organizes
content."* The caption modules ship with the kit. The engine underneath them
(torch, and the wav2vec2 forced aligner `align_ctc.py` drives) is a few hundred
megabytes, and a person reading a lesson should not carry it. So it is installed
ONCE, into `~/.kcl-study/captions-venv`, on a person's click in Settings or on the
wizard's tick, and 🔴 **nothing here runs by surprise**: there is no timer and no
page load that reaches this file.

**What one install does, in order, saying each step before it starts:**

1. picks an interpreter this Mac already has (`pick_python`; it never installs
   Python, and says plainly where to get one when none will do),
2. creates the virtual environment,
3. asks pip what it WOULD download for the pinned wheels, sums the sizes, and
   🟢 **prints the total BEFORE a byte moves**,
4. installs the pins, wheels only, so nothing is ever compiled here,
5. puts a static `ffmpeg` beside the venv's python (out of the `imageio-ffmpeg`
   wheel, so Homebrew is not needed),
6. warms the speech model, so the first build does not pay for it then,
7. prints the readiness report, naming anything still missing (`pdftotext` is
   the one thing this cannot install; it says where it comes from).

🔴 **A failed pip step REMOVES the venv this run created**, so a half-installed
engine never reads as installed: `align_ctc.find_python` treats the venv's
existence as readiness, and this file keeps that true. A venv that existed before
the run is never removed. A failure AFTER pip (the model download dropping) keeps
the venv: the engine works, and the model is fetched on the first build instead.

⚠️ **Two tiers of pins, and why.** `torch` stopped building for Intel Macs at
2.2.2 and for Python 3.9 at the same release, while `numpy` 2.5 needs Python 3.12.
So: Apple silicon with Python 3.12 to 3.14 gets the current tier, which is the
one measured on this project's own machine; anything else that torch still
builds for (Python 3.9 to 3.12, either chip) gets the last tier that runs
everywhere. **Every pin carries its release date and the day it was pinned**, and
`test_install_captions.py` refuses any pin under fourteen days old on that day,
which is the machine-wide dependency rule applied where a pin actually lives.

Usage:
    python3 server/caption_course.py --install       # the documented verb
    python3 server/install_captions.py --install     # the same thing
    python3 server/install_captions.py --plan        # say what it would do; download nothing
    python3 server/install_captions.py --status --json
"""

import datetime
import json
import os
import platform
import shutil
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fetch_videos                                              # noqa: E402


VENV = os.path.join("~", ".kcl-study", "captions-venv")
LOG = os.path.join("~", ".kcl-study", "captions-install.log")
RECORD = os.path.join("~", ".kcl-study", "captions-install.json")

# The day every pin below was chosen. The test measures each release date
# against THIS, not against today: a pin that was old enough when chosen stays
# chosen, and one that was not is refused on the day it lands.
PINNED_ON = "2026-09-18"
MIN_AGE_DAYS = 14

# name, version, release date (PyPI, read on PINNED_ON). Wheels only.
TIERS = {
    # Apple silicon, Python 3.12 to 3.14. Measured on this project's machine
    # with 3.12 (torch 2.11.0 + torchaudio 2.11.0, 2026-09-15); 3.13 and 3.14
    # have the wheels and are otherwise unverified.
    "current": [
        ("torch", "2.11.0", "2026-03-23"),
        ("torchaudio", "2.11.0", "2026-03-23"),
        ("numpy", "2.5.2", "2026-08-09"),
        ("imageio-ffmpeg", "0.6.0", "2025-01-16"),
    ],
    # Python 3.9 to 3.12 on either chip: the last torch that built for Intel
    # Macs and for 3.9. ⚠️ The Intel wheels cannot be exercised on this
    # machine; the arm64 wheels of the same tier can and were.
    "legacy": [
        ("torch", "2.2.2", "2024-03-27"),
        ("torchaudio", "2.2.2", "2024-03-27"),
        ("numpy", "1.26.4", "2024-02-05"),
        ("imageio-ffmpeg", "0.6.0", "2025-01-16"),
    ],
}

# The pip that comes with Python 3.9 (21.2.4) predates `--dry-run --report`, so
# on such an interpreter the venv's pip is raised to this ONE pinned release
# first; a newer bundled pip is left alone.
PIP_PIN = ("pip", "26.0", "2026-01-31")
PIP_FLOOR = (22, 2)

# The speech model `align_ctc.load_model` fetches on first use, measured on
# this machine 2026-09-15: torch.hub's checkpoints folder, one file.
MODEL_NAME = "wav2vec2_fairseq_base_ls960_asr_ls960.pth"
MODEL_BYTES = 377664473

# The interpreters worth looking for, most preferred first. 3.12 first because
# it is the one this project measured; the 3.9 the Command Line Tools install
# is last because it needs the legacy tier, but it is enough.
PYTHON_MINORS = (12, 13, 14, 11, 10, 9)
ENV_PYTHON = "STUDY_HUB_CAPTIONS_PYTHON"


def alive(pid):
    """Is a process with this pid running. False for None, 0 and a dead pid.
    (`caption_course.alive` is this function, kept here so the two records
    that hold a pid share one answer.)"""
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except (OSError, ValueError, TypeError):
        return False
    return True


# --------------------------------------------------------------------------
# Which python, which tier
# --------------------------------------------------------------------------

def tier_for(version, machine):
    """The pin tier for an interpreter, or None when torch has no wheel for it."""
    major, minor = version[0], version[1]
    if major != 3:
        return None
    if machine == "arm64" and 12 <= minor <= 14:
        return "current"
    if 9 <= minor <= 12:
        return "legacy"
    return None


def candidates(env=os.environ):
    """Where an interpreter might be, most preferred first, duplicates dropped."""
    out = []
    named = env.get(ENV_PYTHON)
    if named:
        out.append(named)
    for minor in PYTHON_MINORS:
        name = "python3.%d" % minor
        out.append(shutil.which(name))
        out.append("/opt/homebrew/bin/%s" % name)
        out.append("/opt/homebrew/opt/python@3.%d/bin/%s" % (minor, name))
        out.append("/usr/local/bin/%s" % name)
        out.append("/Library/Frameworks/Python.framework/Versions/3.%d/bin/%s"
                   % (minor, name))
    out.append(sys.executable)
    out.append(shutil.which("python3"))
    out.append("/usr/bin/python3")
    seen, uniq = set(), []
    for cand in out:
        if cand and cand not in seen and os.path.exists(cand):
            seen.add(cand)
            uniq.append(cand)
    return uniq


PROBE = ("import sys, platform, venv, ensurepip\n"
         "print(sys.version_info[0], sys.version_info[1], platform.machine())\n")


def probe_python(path, runner=subprocess.run):
    """(version tuple, machine) for an interpreter, or None if it cannot say.
    `venv` and `ensurepip` are imported in the probe on purpose: an interpreter
    without them cannot build the engine and is not a candidate."""
    try:
        r = runner([path, "-I", "-c", PROBE], capture_output=True, text=True,
                   timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if getattr(r, "returncode", 1) != 0:
        return None
    bits = (getattr(r, "stdout", "") or "").split()
    try:
        return (int(bits[0]), int(bits[1])), bits[2]
    except (IndexError, ValueError):
        return None


def pick_python(cands=None, probe=probe_python):
    """The first interpreter torch has wheels for, as a dict, or a refusal.

    Returns `{"python", "version", "machine", "tier"}` or
    `{"error": <a sentence a person can act on>, "tried": [...]}`.
    """
    tried = []
    for cand in (candidates() if cands is None else cands):
        got = probe(cand)
        if not got:
            tried.append("%s: not a usable Python" % cand)
            continue
        version, machine = got
        tier = tier_for(version, machine)
        if tier:
            return {"python": cand, "version": list(version), "machine": machine,
                    "tier": tier}
        tried.append("%s: Python %d.%d on %s, no torch build"
                     % (cand, version[0], version[1], machine))
    return {"error": ("No Python this engine can use was found. It needs Python "
                      "3.12 to 3.14 on Apple silicon, or 3.9 to 3.12 on an Intel Mac; "
                      "the installer at python.org gives you one, and Apple's "
                      "Command Line Tools give you 3.9. Then run the install again."),
            "tried": tried}


def pins_for(tier):
    return ["%s==%s" % (name, version) for name, version, _ in TIERS[tier]]


def pin_ages(on=PINNED_ON):
    """Days between each pin's release and the day it was pinned, by name."""
    day = datetime.date.fromisoformat(on)
    out = {}
    for tier, rows in TIERS.items():
        for name, version, released in rows:
            out["%s==%s" % (name, version)] = (day - datetime.date.fromisoformat(released)).days
    name, version, released = PIP_PIN
    out["%s==%s" % (name, version)] = (day - datetime.date.fromisoformat(released)).days
    return out


# --------------------------------------------------------------------------
# What it would download, said before it does
# --------------------------------------------------------------------------

def head_size(url, opener=urllib.request.urlopen):
    """Content-Length of a URL by a HEAD request, or None."""
    try:
        req = urllib.request.Request(url, method="HEAD")
        with opener(req, timeout=30) as resp:
            return int(resp.headers.get("Content-Length") or 0) or None
    except (OSError, ValueError):
        return None


def pip_version(pip, runner=subprocess.run):
    try:
        r = runner([pip, "--version"], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    words = (getattr(r, "stdout", "") or "").split()
    if len(words) < 2 or getattr(r, "returncode", 1) != 0:
        return None
    try:
        return tuple(int(x) for x in words[1].split(".")[:2])
    except ValueError:
        return None


def estimate(pip, pins, runner=subprocess.run, head=head_size):
    """What pip WOULD fetch for `pins`, by name, with sizes, without fetching.

    Asks `pip install --dry-run --report -` for the resolved wheel URLs (that is
    every transitive dependency too, at the versions pip would pick today) and
    HEADs each for its size. Returns `{"files": [(filename, bytes-or-None)],
    "bytes": total, "unsized": n}`, or `{"error": ...}` when pip would not say.
    """
    cmd = [pip, "install", "--dry-run", "--quiet", "--report", "-",
           "--only-binary=:all:"] + list(pins)
    try:
        r = runner(cmd, capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"error": "pip could not plan the download: %s" % exc}
    if getattr(r, "returncode", 1) != 0:
        tail = (getattr(r, "stderr", "") or "").strip().splitlines()[-3:]
        return {"error": "pip could not plan the download: %s" % " / ".join(tail)}
    try:
        report = json.loads(getattr(r, "stdout", "") or "")
        rows = report["install"]
    except (ValueError, KeyError, TypeError):
        return {"error": "pip's plan was not readable"}
    files, total, unsized = [], 0, 0
    for row in rows:
        url = ((row.get("download_info") or {}).get("url")) or ""
        name = url.rsplit("/", 1)[-1] or "%s-%s" % (
            (row.get("metadata") or {}).get("name"), (row.get("metadata") or {}).get("version"))
        size = head(url) if url else None
        if size:
            total += size
        else:
            unsized += 1
        files.append((name, size))
    return {"files": files, "bytes": total, "unsized": unsized}


def mb(n):
    return "%.0f MB" % (n / 1e6)


def plan_lines(picked, est):
    """The sentences printed before anything is fetched."""
    out = ["Python %d.%d at %s (%s), the %s pins: %s"
           % (picked["version"][0], picked["version"][1], picked["python"],
              picked["machine"], picked["tier"], ", ".join(pins_for(picked["tier"])))]
    if est.get("error"):
        out.append("Could not add up the download beforehand (%s); the wheels alone "
                   "are roughly %s on this tier." % (est["error"], mb(rough_bytes(picked))))
    else:
        out.append("This will download %s in %d files:" % (mb(est["bytes"]), len(est["files"])))
        for name, size in est["files"]:
            out.append("  %10s  %s" % (mb(size) if size else "?", name))
        if est.get("unsized"):
            out.append("  (%d of them would not say their size)" % est["unsized"])
    out.append("Then the speech model, about %s, once, into torch's cache. Nothing "
               "else is fetched, ever." % mb(MODEL_BYTES))
    return out


# Wheel sizes read off PyPI on PINNED_ON, the four pins alone, for the sentence
# printed when pip will not plan. The estimate proper comes from pip.
ROUGH = {("current", "arm64"): 80606338 + 684226 + 11903109 + 21113891,
         ("legacy", "arm64"): 59706698 + 1807296 + 13971216 + 21113891,
         ("legacy", "x86_64"): 150789334 + 3399439 + 20636301 + 24932969}


def rough_bytes(picked):
    return ROUGH.get((picked["tier"], picked["machine"]), 120000000)


# --------------------------------------------------------------------------
# The install itself
# --------------------------------------------------------------------------

def venv_dir():
    return os.path.expanduser(VENV)


def venv_python(root=None):
    return os.path.join(root or venv_dir(), "bin", "python")


def venv_pip(root=None):
    return os.path.join(root or venv_dir(), "bin", "pip")


def read_record():
    try:
        with open(os.path.expanduser(RECORD), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_record(data):
    path = os.path.expanduser(RECORD)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".part"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
    os.replace(tmp, path)


def log_tail(n=14):
    try:
        with open(os.path.expanduser(LOG), encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return []
    return [ln for ln in lines if ln.strip()][-n:]


def installed():
    """The same test `align_ctc.find_python` applies: the venv's python exists."""
    return os.path.exists(venv_python())


def install_status():
    """What the settings page asks on every render. Reads the disk, starts nothing."""
    rec = read_record()
    state = rec.get("state") or ("done" if installed() else "never")
    running = state == "running" and alive(rec.get("pid"))
    if state == "running" and not running:
        state = "failed"
        rec = dict(rec, error=rec.get("error") or "the install stopped without finishing")
    return {"installed": installed(), "running": running, "state": state,
            "started": rec.get("started"), "finished": rec.get("finished"),
            "error": rec.get("error") or "", "venv": venv_dir(),
            "log": os.path.expanduser(LOG), "log_tail": log_tail() if state != "never" else []}


def _stream(cmd, say, runner=subprocess.run, cwd=None, timeout=3600):
    """Run a long command with its output going where `say` goes (the terminal, or
    the install log when detached). Returns the return code."""
    sys.stdout.flush()
    try:
        r = runner(cmd, cwd=cwd, timeout=timeout, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        say("🔴 gave up after %ds: %s" % (timeout, " ".join(cmd[:3])))
        return 124
    except OSError as exc:
        say("🔴 could not run %s: %s" % (cmd[0], exc))
        return 127
    return getattr(r, "returncode", 1)


def install(say=print, runner=subprocess.run, head=head_size, picked=None,
            warm=True):
    """The whole install, foreground, saying each step first. Returns rc."""
    root = venv_dir()
    had_venv = installed()
    write_record({"pid": os.getpid(), "state": "running",
                  "started": time.strftime("%Y-%m-%d %H:%M:%S"), "log": os.path.expanduser(LOG)})

    def finish(rc, error=""):
        rec = read_record()
        rec.update({"state": "done" if rc == 0 else "failed", "error": error,
                    "finished": time.strftime("%Y-%m-%d %H:%M:%S")})
        write_record(rec)
        return rc

    picked = picked or pick_python()
    if picked.get("error"):
        say("🔴 " + picked["error"])
        for line in picked.get("tried") or []:
            say("   " + line)
        return finish(2, picked["error"])

    if had_venv:
        say("The engine's folder is already here: %s. Checking what it still needs." % root)
    else:
        say("Creating %s with %s" % (root, picked["python"]))
        rc = _stream([picked["python"], "-m", "venv", root], say, runner, timeout=600)
        if rc != 0 or not os.path.exists(venv_python(root)):
            shutil.rmtree(root, ignore_errors=True)
            return finish(1, "python could not create the virtual environment")

    pip = venv_pip(root)
    have = pip_version(pip, runner)
    if have and have < PIP_FLOOR:
        say("This Python's pip (%s) predates the download plan; raising it to %s==%s "
            "(released %s)." % (".".join(map(str, have)), PIP_PIN[0], PIP_PIN[1], PIP_PIN[2]))
        rc = _stream([pip, "install", "--quiet", "%s==%s" % PIP_PIN[:2]], say, runner)
        if rc != 0:
            if not had_venv:
                shutil.rmtree(root, ignore_errors=True)
            return finish(1, "pip could not be raised to %s" % PIP_PIN[1])

    pins = pins_for(picked["tier"])
    est = estimate(pip, pins, runner, head)
    for line in plan_lines(picked, est):
        say(line)
    say("Installing.")
    rc = _stream([pip, "install", "--only-binary=:all:"] + pins, say, runner)
    if rc != 0:
        if not had_venv:
            say("🔴 the install failed, so the half-made folder is removed again.")
            shutil.rmtree(root, ignore_errors=True)
        return finish(1, "pip could not install the pins (see the log)")

    err = place_ffmpeg(root, say, runner)
    if err:
        # The engine is usable; only the video path lacks its tool. Say so,
        # keep the venv, and let readiness name ffmpeg.
        say("⚠️ " + err)

    if warm:
        say("Fetching the speech model, about %s, once." % mb(MODEL_BYTES))
        rc = _stream([venv_python(root), os.path.join(HERE, "align_ctc.py"), "--warm"],
                     say, runner, cwd=HERE, timeout=3600)
        if rc != 0:
            say("⚠️ the model did not download this time; the first build fetches it instead.")

    for line in readiness_lines():
        say(line)
    return finish(0)


HERE = os.path.dirname(os.path.abspath(__file__))


def place_ffmpeg(root, say, runner=subprocess.run):
    """Copy the imageio-ffmpeg wheel's static ffmpeg to `fetch_videos.VENV_FFMPEG`,
    where every finder in this project looks. Returns an error sentence or ""."""
    dest = os.path.expanduser(fetch_videos.VENV_FFMPEG)
    try:
        r = runner([venv_python(root), "-c",
                    "import imageio_ffmpeg,sys;sys.stdout.write(imageio_ffmpeg.get_ffmpeg_exe())"],
                   capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return "could not ask for the bundled ffmpeg: %s" % exc
    src = (getattr(r, "stdout", "") or "").strip()
    if getattr(r, "returncode", 1) != 0 or not src or not os.path.exists(src):
        return "the imageio-ffmpeg wheel did not yield an ffmpeg"
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    shutil.copy2(src, dest)
    os.chmod(dest, 0o755)
    try:
        v = runner([dest, "-version"], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return "the copied ffmpeg would not run: %s" % exc
    if getattr(v, "returncode", 1) != 0:
        return "the copied ffmpeg would not run"
    first = ((getattr(v, "stdout", "") or "").splitlines() or [""])[0]
    say("ffmpeg placed at %s (%s)" % (dest, first[:60]))
    return ""


def readiness_lines():
    """The report that ends an install, from the same probe Settings uses."""
    import caption_course
    have = caption_course.tools()
    out = []
    for name in ("caption engine", "ffmpeg", "pdftotext"):
        path = have.get(name)
        out.append("  %-14s %s" % (name, path or "🔴 not found"))
    miss = caption_course.missing_tools(have)
    if miss:
        out.append("🔴 not ready to build: %s missing." % ", ".join(miss))
        if "pdftotext" in miss:
            out.append("   pdftotext comes with poppler, which this cannot install: "
                       "`brew install poppler` on a Mac with Homebrew, or set "
                       "STUDY_HUB_PDFTOTEXT to one you have.")
    else:
        out.append("🟢 ready: every tool a build needs is here.")
    return out


# --------------------------------------------------------------------------
# Detached, for the settings page
# --------------------------------------------------------------------------

def start_install(popen=subprocess.Popen, python_bin=None):
    """Start the install in its OWN process and return at once, the shape of
    `caption_course.start`: an HTTP handler must not hold a 500 MB download."""
    now = install_status()
    if now["running"]:
        return {"ok": False, "error": "an install is already going", "pid": read_record().get("pid")}
    logfile = os.path.expanduser(LOG)
    os.makedirs(os.path.dirname(logfile), exist_ok=True)
    argv = [python_bin or sys.executable, os.path.abspath(__file__), "--install"]
    with open(logfile, "a", encoding="utf-8") as handle:
        handle.write("\n==== %s ====\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
        handle.flush()
        proc = popen(argv, stdout=handle, stderr=subprocess.STDOUT,
                     stdin=subprocess.DEVNULL, start_new_session=True, cwd=HERE)
    pid = getattr(proc, "pid", None)
    write_record({"pid": pid, "state": "running",
                  "started": time.strftime("%Y-%m-%d %H:%M:%S"), "log": logfile})
    return {"ok": True, "pid": pid, "log": logfile}


def say(*bits):
    print(*bits)
    sys.stdout.flush()


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--install", action="store_true", help="install the engine now, in this process")
    ap.add_argument("--start", action="store_true", help="install in a detached process; answer at once")
    ap.add_argument("--plan", action="store_true", help="say which Python and which pins; fetch nothing")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-warm", action="store_true", help="skip the model download")
    a = ap.parse_args(argv)
    if a.status:
        data = install_status()
        if a.json:
            json.dump(data, sys.stdout, indent=2, sort_keys=True)
            sys.stdout.write("\n")
        else:
            print("caption engine: %s%s" % (data["state"], " (running)" if data["running"] else ""))
            for line in data["log_tail"]:
                print("  " + line)
        return 0
    if a.plan:
        picked = pick_python()
        if a.json:
            json.dump(picked, sys.stdout, indent=2, sort_keys=True)
            sys.stdout.write("\n")
            return 0 if not picked.get("error") else 2
        if picked.get("error"):
            print("🔴 " + picked["error"])
            return 2
        for line in plan_lines(picked, {"error": "nothing was asked; this is --plan"}):
            print(line)
        return 0
    if a.start:
        out = start_install()
        json.dump(out, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
        return 0 if out.get("ok") else 1
    if a.install:
        return install(say, warm=not a.no_warm)
    ap.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
