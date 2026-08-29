#!/usr/bin/env python3
"""Record how big each lecture's media actually is, split by kind.

EH's design, 2026-08-23: capture file sizes at link-collection time where
knowable, "so that if someone wants to download everything post the original
run, they can be told up front what the total size will be", keeping the
full-videos against slide-packages split he wants preserved everywhere.

What "knowable" honestly means, per kind:

- **A recording** (a row with a Kaltura entry id): one HEAD request against
  the same stream URL fetch_videos.py downloads, no login needed. Costs one
  round trip per lecture and nothing on disk.
- **A slide package**: the size exists only once the package is mirrored,
  because the package is hundreds of small files behind a login until then.
  Mirrored packages are measured from disk; unmirrored ones are reported as
  unknown rather than guessed.

Sizes land in `materials.json` as `media_bytes` per doc, which is the table
everything else already reads; the wizard's download checkboxes state the
totals. Re-running re-measures: a HEAD is cheap and disk is truth.
"""

import argparse
import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import study_server as S                                        # noqa: E402
import fetch_videos as F                                        # noqa: E402


def head_size(url):
    """Content-Length after redirects, or None. Falls back to a 1-byte GET,
    because some CDNs refuse HEAD while serving ranges happily."""
    for method, headers in (("HEAD", {}), ("GET", {"Range": "bytes=0-0"})):
        req = urllib.request.Request(url, method=method,
                                     headers=dict({"User-Agent":
                                                   "study-hub-sizes"},
                                                  **headers))
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                if method == "GET":
                    cr = r.headers.get("Content-Range", "")
                    if "/" in cr and cr.rsplit("/", 1)[1].isdigit():
                        return int(cr.rsplit("/", 1)[1])
                cl = r.headers.get("Content-Length")
                if cl and cl.isdigit() and int(cl) > 0:
                    return int(cl)
        except OSError:
            continue
    return None


def dir_size(folder):
    return sum(p.stat().st_size for p in Path(folder).rglob("*")
               if p.is_file())


def human(n):
    if n < 1024:
        return "%d bytes" % n
    for unit in ("KB", "MB", "GB", "TB"):
        n /= 1024.0
        if n < 1024 or unit == "TB":
            return "%.1f %s" % (n, unit)


def measure(folder, do_net=True):
    """Fill media_bytes for every doc it can. Returns the report dict."""
    folder = Path(folder)
    path = folder / "materials.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise SystemExit("%s has no readable materials.json" % folder)
    docs = data.get("docs") or {}

    rec = {"n": 0, "measured": 0, "bytes": 0}
    pack = {"n": 0, "measured": 0, "bytes": 0}
    for doc, row in docs.items():
        if not isinstance(row, dict):
            continue
        pdir = folder / "packages" / doc
        if pdir.is_dir():
            pack["n"] += 1
            size = dir_size(pdir)
            row["media_bytes"] = size
            pack["measured"] += 1
            pack["bytes"] += size
        elif row.get("entry"):
            rec["n"] += 1
            local = folder / "videos" / ("%s.mp4" % doc)
            size = local.stat().st_size if local.is_file() else \
                (head_size(F.stream_url(row["entry"])) if do_net else None)
            if size:
                row["media_bytes"] = size
                rec["measured"] += 1
                rec["bytes"] += size
        elif row.get("video"):
            # Linked, but neither a known recording nor a mirrored package:
            # usually a package still behind the course site, whose size is
            # knowable only after mirroring. Counted, never guessed.
            pack["n"] += 1
    return data, path, rec, pack


def main():
    ap = argparse.ArgumentParser(description="record each lecture's media "
                                             "size in materials.json")
    # 🔴 Required, same reasoning as its siblings: sizes land inside a course.
    ap.add_argument("--module", required=True, help="which course")
    ap.add_argument("--no-net", action="store_true",
                    help="disk only: skip the per-recording HEAD requests")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--config", default=str(S.CONFIG_PATH))
    args = ap.parse_args()

    cfg = S.load_config(Path(args.config).expanduser())
    mods = S.resolve_modules(cfg)
    if args.module not in mods:
        raise SystemExit("no course called %r. There %s: %s"
                         % (args.module, "is" if len(mods) == 1 else "are",
                            ", ".join(sorted(mods)) or "none yet"))

    data, path, rec, pack = measure(mods[args.module],
                                    do_net=not args.no_net)
    print("recordings: %d of %d measured, %s total"
          % (rec["measured"], rec["n"], human(rec["bytes"])))
    unmirrored = pack["n"] - pack["measured"]
    print("slide packages: %d of %d measured from disk, %s total%s"
          % (pack["measured"], pack["n"], human(pack["bytes"]),
             ("; %d not yet mirrored, size unknowable until they are"
              % unmirrored) if unmirrored else ""))
    if args.dry_run:
        print("dry run, nothing written")
        return
    S.write_json_sidecar(path, data)
    print("written to %s" % path)


if __name__ == "__main__":
    main()
