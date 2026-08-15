# Mandel Pixel-Stream Protocol - Design Doc

Read this first if you're picking up either side of this cold. The
machine-readable contract is `mandel_pixel_stream.yaml` in this same
directory - this file explains why it looks the way it does, who's
responsible for what, and how to verify your side is correct without
needing the other side (or real hardware) in the loop.

## Why this exists

`mandel_z80.asm` (the RC2014/Z80 fork of this repo's Mandelbrot renderer)
sends its output over a 115200-baud serial link to `rc2014bridge`, which
currently parses it as plain ANSI terminal text via `pyte` and renders it
as a character grid via `pygame`. A round of optimization work measured
that render's output overhead directly (`OUTPUT=0` compute-only builds
vs. full builds) and found it's now bound by UART transmission time, not
CPU-side overhead - meaning the only lever left to speed up output, or to
afford a higher render resolution within the same time budget, is
sending fewer bytes. This protocol replaces the ANSI/text output with a
compact binary run-length-encoded stream, and moves color/character
rendering entirely to the receiving side, which also unlocks true-RGB
pixel rendering in `pygame` instead of a text-character stand-in.

## What each side is responsible for

**Sender (`mandel_z80.asm`, or a future variant)**:
- Computes the iteration count (0-30) per pixel exactly as today.
- RLE-encodes the raw iteration-count stream per the spec below. No
  color lookup, no character lookup, no ANSI escape codes - `hsv` and
  `chartable` are not needed by the sender under this protocol at all.
- Knows nothing about RGB, pygame, or how the receiver will render
  anything. The sender's only job is emitting correct index/run tokens.

**Scope note for whoever builds the sender side**: the new variant
(something like `mandel_z80_pixelstream.asm`) should start as a copy of
the current `mandel_z80.asm` at `main`, not a fresh minimal build. Keep
every compute-side optimization already in that file (cardioid/bulb
pre-check, `square_16`, the ESC-per-row relocation) - none of that is
output-encoding-specific, and none of it should be lost porting to this
protocol. Only the output path changes: `hsv`, `chartable`,
`colorpixel`, `setcolor`, and `printdec` all go away, replaced by a
small streaming RLE encoder in `showpixel`'s place that tracks
`(last_index, run_count)` across pixels and appends 1-2 byte tokens to a
much smaller buffer than the ANSI-based design needed (row width + a
small margin is enough now - see `mandel_pixel_stream.yaml`'s framing
notes on why worst case is bounded by row width, not a large multiplier
per pixel).

**Receiver (`rc2014bridge`)**:
- Decodes the token stream (see `mandel_pixel_stream.yaml`'s
  `encoding` section) back into `(index, run_length)` pairs per row.
- Owns the palette (`mandel_pixel_stream.yaml`'s `palette` section) -
  looks up RGB for each index and draws directly to the pygame surface,
  bypassing `pyte`/the character-grid display entirely for this mode.
- Owns activation: deciding when incoming bytes should be routed through
  this decoder instead of the normal `pyte` terminal path (see
  "Activation" below - this needs to be explicit, not auto-detected).

This is a clean split: the sender never needs to change if the palette
changes, and the receiver never needs to change if the render resolution
or fixed-point scale changes. Only changes to the *framing/encoding*
itself require both sides to move together, which is exactly what
`version` in the YAML is for.

## Encoding summary (see the YAML for the authoritative spec)

Every byte is self-describing: `iiiiirrr` - 5 bits index, 3 bits
run/control code. Index 0-30 is a real palette index; index 31 (`0b11111`)
is a reserved control namespace (`END_OF_ROW`, `END_OF_FRAME`,
`VERSION_BANNER`). Run codes 0-6 mean a direct run of 1-7 identical
pixels in one byte; code 7 means "read the next byte as the actual run
length (0-255)". There is no ANSI, no CR/LF, no literal text anywhere in
this protocol - it's fully binary, which is also why framing had to move
off CR/LF: byte value 13 (0x0D) is a perfectly legal encoded token
(index=1, run=6), so literal CR/LF framing would have collided with real
data. That's why row/frame boundaries are control tokens instead.

Why 5+3 bits and not something else: `chartable`/`hsv` are indexed by
the same value (iteration count), so color and character were never two
independent things to encode - RLE-ing that one shared index directly,
in a fixed-width binary code, needs neither an escape byte doing double
duty as data (ambiguity risk) nor separate framing for color-vs-char
(there's only one axis of information per pixel). Measured against real
captured render data (see PLAN.md's RLE backlog entry) this design beat
both a naive escape-byte RLE scheme and the same scheme without
extension bytes, on both flat and detail-heavy rows.

## Constraints this protocol imposes

- **`iteration_max` must stay <= 30.** The 5-bit index field has exactly
  31 non-control values (0-30). If a future build needs more iterations,
  that's a protocol version bump (wider index field), not something to
  work around silently.
- **Row width has no hard limit** the format cares about - rows are
  self-terminated by `END_OF_ROW`, not a fixed byte count. Worst case
  (no repeats at all) is 1 byte per pixel plus 1 end-of-row byte, i.e.
  strictly *no worse* than the current unencoded-character approach, and
  usually much better. This is worth knowing for buffer sizing: the
  `line_buf`-equivalent in a sender implementation can be sized as
  `row_width_pixels + margin`, not the ~12-bytes/pixel worst case the
  ANSI-based ad-hoc RLE proposal needed - a meaningful simplification on
  the Z80 side alone.
- **Board-agnostic on purpose.** Nothing in the encoding assumes a
  specific CPU or clock speed. A future Z180 port (hardware `MLT`,
  36.8MHz - see PLAN.md/README.md for why that board was set aside
  originally) should be able to speak the exact same protocol without a
  version bump, generating its own `.asm` variant from the same YAML.
  Not in scope for the current build - noted so nobody accidentally
  bakes in an RC2014-specific assumption while implementing v1.

## Activation: how the bridge knows to switch modes

Auto-detecting this protocol from arbitrary incoming bytes is explicitly
**not** the recommended design - `rc2014bridge` talks to many different
CP/M programs, and false-triggering a full display-mode switch (leaving
`pyte`, drawing raw pixels instead) from a coincidental byte sequence
would be a bad failure mode. Recommended instead: an explicit, caller-
declared opt-in - e.g. a new parameter on the run-command path
(`decode_protocol="mandel-pixel-stream-v1"` or similar) that tells the
bridge "route incoming bytes through this decoder and the pixel-view
renderer until `END_OF_FRAME` or a timeout, instead of the normal
terminal path." The `VERSION_BANNER` control token exists as a second,
defense-in-depth check on top of that explicit opt-in - not a
replacement for it: once told to expect this protocol, the bridge can
still confirm the version banner matches before trusting the rest of the
stream.

This is a real open decision, not settled by this doc - the bridge-side
implementer should propose the exact mechanism (new MCP tool parameter,
a separate tool entirely, etc.) since it depends on `rc2014bridge`'s
existing `SerialLink`/MCP tool architecture more than this repo's.

## Where the decode hook goes (bridge side)

`SerialLink._read_loop()` in `rc2014bridge/link.py` currently does:
```python
data = self._ser.read(4096)
...
self._stream.feed(text)   # text-decoded, fed to pyte
```
The pixel-stream decoder should sit before that feed call, as an
alternate path taken only while the mode described above is active -
decode tokens, look up RGB via the palette, draw to the pygame surface
directly. `pyte`/the character-grid path should be completely untouched
when this mode isn't active.

## Testing strategy (before any hardware is involved)

1. **Golden vectors first.** `mandel_pixel_stream.yaml`'s `test_vectors`
   section and `generate_test_vectors.py` (this directory) are the
   starting point - both a Z80 encoder and the bridge's Python decoder
   should be checked against these exact bytes before anything else.
   `generate_test_vectors.py` is also the place to add new vectors if
   the spec grows (do this before hand-writing new expected bytes -
   let the script compute them, the way these were verified).
2. **Round-trip against real render data.** Take an actual captured row
   from a real `mandel_z80.asm` run (plenty of examples in this
   session's PLAN.md history and commit messages), encode it, decode it
   in Python, and confirm you get back the original iteration-count
   sequence exactly.
3. **Z80 encoder output vs. Python reference encoder output**, same
   pattern already used for `square_16` (verified against `x*x` across
   all 65536 16-bit values before ever touching hardware) - the Z80
   encoder's output for a real captured row should match
   `generate_test_vectors.py`'s `encode_row()` byte-for-byte.
4. **Real hardware, but not via `rc2014_run_command`'s live capture -
   write to a file and download it instead.** This is a real gap, not a
   hypothetical: `rc2014_run_command`'s prompt-detection reads from
   `pyte`'s *rendered* screen (`CPM_PROMPT_RE.search(lines[-1])` in
   `rc2014bridge/link.py`), and `pyte` will interpret every byte of this
   binary protocol as terminal input - line-wrapping at 80 columns,
   rendering high bytes (including this protocol's own `0xF8-0xFF`
   control range) as Latin-1 printable characters - completely
   independent of this protocol's actual row/frame structure. A
   sufficiently long or unlucky byte sequence could render something
   that looks like a CP/M prompt after `pyte`'s own line-wrapping,
   silently truncating the capture mid-stream. This is *not* a flaw in
   the protocol or the production architecture - the real bridge decoder
   intercepts bytes before they ever reach `pyte`, exactly as designed
   above - it's specifically that `rc2014_run_command` isn't a safe
   verification tool for raw binary output until a working bridge
   decoder exists on the other end to consume it live. Until then: have
   the test build write its encoded output to a file on the CP/M device
   instead of streaming it to the console, and use `rc2014_download`
   (XMODEM, checksum-verified, completely decoupled from prompt-
   detection heuristics) to retrieve it byte-exact for comparison against
   the Python reference encoder. Switch to real live-streaming
   verification only once the bridge side has a working decoder to
   receive it, per the plan to converge to one agent for that final
   integration/test pass.

## File sync between the two repos

The canonical copy of `mandel_pixel_stream.yaml` (and this doc) lives in
the `mandel` repo. A mirrored copy should exist in `rc2014bridge` at the
same relative path (`protocol/`). Keep them byte-for-byte identical -
edit the `mandel` copy, bump `version` if the encoding changed, then
copy the whole file over. Do not hand-edit the two copies independently;
if the copies ever disagree, trust `mandel`'s as canonical.

## Open questions (not yet resolved - resolve before/during implementation)

1. Exact mechanism for the bridge-side activation opt-in (new tool
   parameter vs. separate tool vs. something else) - bridge
   implementer's call, propose it in `rc2014bridge`'s own planning.
2. Whether `VERSION_BANNER` is sent unconditionally at the start of
   every render, or only when explicitly requested - leaning toward
   "always sent, cheap (2 bytes), and lets the bridge fail loudly on a
   version mismatch instead of silently misrendering."
3. Resolution/`x_step`/`y_step` are unconstrained by this protocol, but
   increasing them increases *compute* time roughly linearly (this
   protocol only affects output time, which is already the minority of
   total runtime - see PLAN.md's RLE backlog entry for the actual
   numbers). Worth deciding, before chasing a big resolution bump,
   whether this is meant to stay a fast/interactive render or is allowed
   to take longer as a one-shot high-detail showcase - that changes what
   else is worth optimizing alongside this.
