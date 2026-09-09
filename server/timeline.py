"""The timing blob inside an iSpring package, and how to rebuild it at a speed.

🔴 **THIS MODULE EXISTS BECAUSE THE SERVER MAY NOT IMPORT `presinfo.py`**, and
that line is pinned by a test rather than promised in a docstring
(`test_presinfo.py::TheSERVERMUSTNEVERIMPORTTHIS`). `presinfo` is an inspection
tool for this machine: it globs the course tree, shells out to `ffprobe` and
parses arguments. **A recipient's server must not depend on any of that.**

So the split is by AUDIENCE, not by topic. What lives here is what a REQUEST
needs: the blob's format, the field list, and the scaler. What stays in
`presinfo` is what a PERSON needs: the survey, the audio join, the self-test,
the CLI. `presinfo` imports this file, so 🟢 **`TIME_FIELDS` still has exactly
one home** and the mutation-proved tests written against `presinfo.scale` go on
covering the code the server now runs.

**What the blob is**: `index.html` carries `var presInfo = "eNrt..."`, base64 of
a zlib stream, decompressing to JSON with a top-level `s` array, one object per
slide. It is handed to `PresentationPlayer.start()` once, at boot, and never
re-read. **That is what makes a speed change a rebuild rather than a setting**:
there is no rate property anywhere in the player, and `playbackRate` appears
zero times in its 1.67MB (probe, 2026-09-02).
"""

import base64
import json
import re
import zlib

BLOB = re.compile(r'var presInfo = "([A-Za-z0-9+/=]+)"')

# Every field the player multiplies by a time, named from the player's own key
# schema (`bN.prototype.ka` in data/player.js) and confirmed against the loader
# that reads it.
# 🔴 The list is the whole point: a rebuild that scales a subset desynchronises
# the lecture in a way that looks like a bad export rather than like our bug.
TIME_FIELDS = (
    ("s[].e[].p", "step play time, seconds"),
    ("s[].e[].a", "pause after the step, seconds"),
    ("s[].i.m.s[].d", "same step duration again, second copy"),
    ("s[].i.m.s[].a[].t.u", "delay before an element's animation"),
    ("s[].i.m.s[].a[].t.d", "an element's animation duration"),
    ("s[].i.m.s[].a[].t.l", "repeat interval"),
    ("s[].i.m.s[].a[].t.w", "hold between an auto-reversed pair"),
    ("s[].i.i[].s[].d", "trigger-sequence step duration"),
    ("s[].i.i[].s[].a[].t.d", "trigger-sequence animation duration"),
    ("s[].i.i[].s[].a[].t.u", "trigger-sequence delay"),
    ("s[].q.d", "slide transition"),
    ("s[].wo[].to", "web object start offset"),
)

# 🔴 Numbers that look like times and are NOT. Scaling any of these is a silent
# defect.
NOT_TIME_FIELDS = (
    ("s[].i.m.s[].a[].b[].t.s", "FRACTION of the action's own duration, not a time"),
    ("s[].i.m.s[].a[].b[].t.e", "FRACTION of the action's own duration, not a time"),
    ("s[].i.m.s[].a[].t.a", "accelerate, a fraction"),
    ("s[].i.m.s[].a[].t.c", "decelerate, a fraction"),
    ("s[].i.m.s[].a[].t.p", "repeat COUNT"),
    ("s[].S[].d", "the audio file's real length; speed is the audio element's job"),
)


def decode(b64):
    return json.loads(zlib.decompress(base64.b64decode(b64)).decode("utf-8"))


def encode(obj):
    """Back to a blob the player accepts. Measured 2026-09-02: it does."""
    raw = json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return base64.b64encode(zlib.compress(raw, 9)).decode("ascii")


def positive_rate(rate):
    """`float(rate)`, or `ValueError`.

    🔴 `bool` is an `int` in Python, so `scale(pres, True)` would quietly mean
    rate 1: a caller that passed the wrong thing would get a package back
    rather than an error. The walkers in this file exclude bools for the same
    reason.
    """
    if isinstance(rate, bool) or not isinstance(rate, (int, float)) or rate <= 0:
        raise ValueError("rate must be a positive number, not %r" % (rate,))
    return float(rate)


def scale(pres, rate):
    """A copy of the blob with every TIME field divided by `rate`.

    🔴 **TWELVE fields, not four**, and the list is `TIME_FIELDS` rather than a
    guess made here: a scaler that touches only the obvious ones ships a package
    whose slides and voice disagree in the cases nobody opened. **And six more
    fields look like times and are not** (`NOT_TIME_FIELDS`): fractions of an
    action's own duration, an acceleration, a repeat COUNT, and the audio file's
    real length, which does not change because the file does not change.

    ⚠️ **`s[].S[].d` is the one people will reach for.** It is the mp3's true
    length in seconds; **speed is the audio ELEMENT's job** (`playbackRate`), and
    rewriting it here would tell the player the file is shorter than it is.

    🟢 **Divide rather than multiply**: rate 1.5 means everything happens in
    two-thirds of the time. `scale(pres, 1)` returns an equal copy, which is the
    identity the tests pin.

    Never mutates its argument.
    """
    rate = positive_rate(rate)
    names = {p for p, _ in TIME_FIELDS}

    def walk(node, path):
        if isinstance(node, dict):
            return {k: walk(v, path + "." + k) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(v, path + "[]") for v in node]
        if isinstance(node, (int, float)) and not isinstance(node, bool) and path in names:
            return node / rate
        return node

    out = dict(pres)
    out["s"] = walk(pres.get("s", []), "s")
    return out


def rebuild(html, rate):
    """A package's `index.html` whose TIMELINE runs at `rate`, as text.

    🔴 **RATE 1 RETURNS THE ARGUMENT ITSELF, byte for byte, and that is a
    decision rather than an optimisation.** Re-encoding at rate 1 does not
    reproduce the exporter's bytes: `4` divided by `1.0` is `4.0`, which JSON
    writes differently, and zlib at level 9 is not obliged to agree with
    whatever compressed the original. So a reader who has not asked for a speed
    would be served a document THIS PROJECT rewrote, for no benefit, on the one
    route that serves somebody else's HTML. **Nothing is touched until a rate is
    chosen.**

    🔴 **Always from the ORIGINAL blob.** The route reads the mirrored file on
    every request, so a second speed change starts from the export rather than
    compounding on the first (1.5 then 1.5 giving 2.25). `scale` being
    multiplicative is what makes that safe, and it is pinned.

    ⚠️ **A package with no blob is returned unchanged rather than refused.** It
    cannot be sped up, but it can still be watched, and that trade is the whole
    posture of this route.

    ⚠️ **This is a COPY, not a live mutation, and it was measured three ways:**
    a rate changed while playing eats the second half of every animated slide,
    because element animations are baked at load. Do not re-litigate it.

    Raises on a bad rate, and on a blob that will not decode. The caller decides
    what a failure costs; on the server it costs the speed and not the lecture.
    """
    if positive_rate(rate) == 1.0:
        return html
    m = BLOB.search(html)
    if not m:
        return html
    # Spliced by span rather than `str.replace`: the base64 is the only capture,
    # and replacing by value would depend on it appearing exactly once.
    return html[:m.start(1)] + encode(scale(decode(m.group(1)), rate)) + html[m.end(1):]
