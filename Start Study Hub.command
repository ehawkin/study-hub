#!/bin/zsh
# Double-click this file to start Study Hub. That is the whole thing.
#
# You do not need to know what Python is. If your Mac does not have it yet, this
# script says so and waits while macOS installs it for you.
#
# Nothing here is permanent: it starts the reader, opens it in your browser, and
# you can close the Terminal window afterwards. Double-click again any time.

# 🔴 ${0:A} is the resolved absolute path of THIS file, and :h is its folder.
# A .command opened from Finder starts in your home directory, not here, and the
# folder this lives in may contain spaces ("Study Hub" does), so every path below
# is quoted and derived from this one line rather than from the working directory.
HERE="${0:A:h}"
cd "$HERE" || { print -r -- "Could not open $HERE"; read "?Press return to close."; exit 1 }

SERVER="$HERE/server/study_server.py"
STATE="$HOME/.kcl-study"
CONFIG="$STATE/config.json"
LOG="$STATE/stdout.log"

pause_exit () { print -r -- ""; read "?Press return to close this window."; exit ${1:-0} }

print -r -- "Study Hub"
print -r -- "  folder: $HERE"
print -r -- ""

[ -f "$SERVER" ] || {
  print -r -- "This file is not sitting in the Study Hub folder any more:"
  print -r -- "  expected to find $SERVER"
  print -r -- "Move it back beside the 'server' folder and try again."
  pause_exit 1
}

# ---- Python -----------------------------------------------------------------
#
# 🔴 `command -v python3` is NOT the test. macOS ships a stub at /usr/bin/python3
# that exists whether or not the developer tools are installed; RUNNING it is what
# triggers Apple's installer dialog. So the check runs it and looks at the result,
# and the first run is allowed to be the thing that pops the dialog.
if ! python3 -c 'import sys; sys.exit(0)' >/dev/null 2>&1; then
  print -r -- "One thing first: your Mac needs Python, which it does not have yet."
  print -r -- ""
  print -r -- "  A grey Apple dialog is about to appear saying something like"
  print -r -- "  \"The python3 command requires the command line developer tools\"."
  print -r -- "  Click INSTALL and wait for it to finish. It is Apple's own"
  print -r -- "  installer, it takes a few minutes, and you only do it once."
  print -r -- ""
  read "?Press return to bring up that dialog."
  python3 --version >/dev/null 2>&1
  print -r -- ""
  print -r -- "When the install has finished, double-click Start Study Hub again."
  pause_exit 0
fi

# ---- first run --------------------------------------------------------------
if [ ! -f "$CONFIG" ]; then
  print -r -- "First run: setting up your copy."
  python3 "$SERVER" --init || { print -r -- "Setup did not finish."; pause_exit 1 }
  print -r -- ""
fi

# The port is whatever the config says, so a machine that changed it still works.
PORT=$(python3 -c 'import json,os,sys
try:
    print(json.load(open(os.path.expanduser("~/.kcl-study/config.json")))["port"])
except Exception:
    print(8795)' 2>/dev/null)
[ -n "$PORT" ] || PORT=8795
URL="http://127.0.0.1:$PORT/"

# ---- already running? -------------------------------------------------------
#
# Idempotent by design: a second double-click should open the reader, not start a
# second server that cannot bind the port. Asked by the port, which is the honest
# question; process-name matching has been unreliable on this machine.
if curl -sf -m 3 "${URL}healthz" >/dev/null 2>&1; then
  print -r -- "Study Hub is already running. Opening it."
  open "$URL"
  pause_exit 0
fi

# ---- start ------------------------------------------------------------------
mkdir -p "$STATE"
print -r -- "Starting Study Hub..."
nohup python3 "$SERVER" >>"$LOG" 2>&1 &
disown

for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
  sleep 1
  if curl -sf -m 3 "${URL}healthz" >/dev/null 2>&1; then
    print -r -- "Ready. Opening $URL"
    open "$URL"
    print -r -- ""
    print -r -- "You can close this window; Study Hub keeps running."
    print -r -- "To stop it, restart your Mac, or quit from the Settings page."
    pause_exit 0
  fi
done

print -r -- "Study Hub did not answer within 15 seconds."
print -r -- "The last few lines of its log ($LOG):"
print -r -- ""
tail -n 15 "$LOG" 2>/dev/null
pause_exit 1
