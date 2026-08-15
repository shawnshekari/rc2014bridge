---
name: rc2014bridge_api
description: How to control an RC2014 vintage computer (RomWBW / HBIOS / ZSDOS / CP/M) through the rc2014bridge MCP server.
---

# Controlling an RC2014 through rc2014bridge

Use this skill when interacting with, controlling, or querying an RC2014 Z80
vintage computer running RomWBW, HBIOS, ZSDOS, or CP/M via the `rc2014bridge`
daemon.

## Connecting

`rc2014bridge` exposes a single control surface: an **MCP server** on port
8014 of the host running the bridge.

- Stateless streamable HTTP (2026-07-28 spec): `http://<host-ip>:8014/mcp`

A human may be watching the same console in a pygame window, so everything
you type is visible to them.

## The one rule

**Use `rc2014_run_command` for anything that returns to a shell prompt.** It
sends the command, waits for the prompt to come back, and returns only that
command's output. Do not send a command and then poll `rc2014_get_screen` —
that was the old pattern and it cost 3-5 calls per command.

```
rc2014_run_command(command="DIR A:")
-> {"ok": true, "output": "A: LEDSHOW  COM : TEST     TXT", "prompt": "A>",
    "state": "cpm", "duration_s": 1.2, "timed_out": false}
```

`timeout` defaults to 15s. A long-running program needs a longer one.

### When run_command is the wrong tool

It waits for a shell prompt, so it **times out** on anything that doesn't
return to one:

- Interactive programs — `MBASIC`, `ED`, `DDT`
- The HBIOS `Boot [H=Help]:` menu
- A command that stops to ask a question

For those, use `rc2014_send_text` (raw text, Enter appended, returns
immediately) then `rc2014_get_screen` or `rc2014_wait_for` to see what
happened.

```
rc2014_send_text(text="MBASIC")
rc2014_get_screen(max_lines=15)
rc2014_send_text(text="PRINT 2+2")
rc2014_get_screen(max_lines=5)
rc2014_send_text(text="SYSTEM")          # back to CP/M
```

## Interrupting something

`rc2014_send_keys` sends control characters with nothing appended. It is the
only tool that still works while another operation owns the wire, because
interrupting one is what it is for.

```
rc2014_send_keys(keys="^C")              # break out of a running program
rc2014_send_keys(keys="^X<PAUSE>^X")     # abort a stuck XM transfer
rc2014_send_keys(keys="<ESC>")           # leave an editor's insert mode
rc2014_send_keys(keys="Y<CR>")           # answer a Y/N prompt
```

Mnemonics: `^A`..`^Z` (plus `^[ ^] ^?`), `<NUL> <BS> <TAB> <LF> <CR> <ESC>
<CAN> <EOF> <SPACE> <DEL>`, `<PAUSE>` for a brief wait, `^^` for a literal
caret. Anything else is sent as typed. `rc2014_send_text` cannot do this — it
always appends a CR, and XM ignores `0x18` followed by `0x0D`.

## Reading the screen

`rc2014_get_screen(max_lines=40)` returns a state header plus screen lines:

```
[state=cpm prompt='A>' mode=terminal operation=idle xmodem=idle]
A>DIR
A: LEDSHOW  COM : TEST     TXT
A>
```

The header tells you whether an OS is running (`state=cpm` vs `hbios` vs
`flash_util`), whether a transfer is in flight, and whether another operation
owns the wire. `max_lines=0` returns the entire scrollback — thousands of
lines, so only when you need it.

## Waiting for something

`rc2014_wait_for(pattern, timeout)` matches a regex against output arriving
**from the moment you call it** — a prompt already on screen will not match.
Use it after `rc2014_send_text`, for example after a reboot:

```
rc2014_reboot()
rc2014_wait_for(pattern="Boot \\[H=Help\\]:", timeout=30)
rc2014_send_text(text="2")               # boot disk unit 2 (unit differs per machine)
rc2014_wait_until_ready()                # NOT just wait_for a prompt - see below
```

**Always `rc2014_wait_until_ready()` after booting.** A boot profile keeps
running programs after the OS banner and prompt appear, and the board is not
reading input during that window — a command sent then is silently mangled (a
real case: `DIR B:` arrived as `IR B:`). Waiting for a prompt to *appear* is not
enough; this waits for the machine to go quiet.

**At the `Boot [H=Help]:` prompt, send single keys only.** `rc2014_run_command("DIR
B:")` there runs `D` (device inventory) off the first character and still
returns a prompt, looking like it worked. `REN A=B` would run `R` — reboot.
`run_command` returns a `warning` if you do this; heed it.

The loader reads a line, not a raw keystroke: it needs a terminating Enter
before it acts. `rc2014_send_text` always appends one, so `rc2014_send_text(text="D")`
does the right thing without you having to think about it — it just looks
like "one keystroke, one action" from the tool's side. Anything that sends
raw bytes without a CR (e.g. driving `link.send_text(..., append_enter=False)`
directly, bypassing the MCP layer) will instead see the letter sit on the
prompt line, unacted-on, until a `\r` arrives — confirmed against firmware
v3.7.0-dev.13.

### Finding things in the loader

`H` shows help, and as of firmware v3.7.0-dev.13 it lists everything in one
screen — no separate `L` needed for ROM applications anymore (older builds,
e.g. v3.5.0, split some entries like the XModem Flash Updater off into a
second `L` listing). Current `H` output on an SC700:

```
<u>[.<s>]   - Boot from Disk <Unit>[.<Slice>]
D           - Device Inventory
S           - Slice Inventory
W           - RomWBW Configure
O           - Hardware Monitor
M           - Monitor
C           - CP/M 2.2
Z           - Z-System
N           - Network Boot
B           - BASIC
T           - Tasty BASIC
F           - Forth
P           - Play a Game
X           - XModem Flash Updater
U           - User App
I <u> [<b>] - Console Interface <Unit> [<Baud>]
V [<v>]     - View/Set HBIOS Diagnostic [Verbosity>]
R           - Reboot System
```

`D` gives the device inventory — use it to find which disk unit to boot, since
the number differs per machine (unit 2 on one RC2014, unit 4 on an SC700). You
can also boot a unit directly with `<u>[.<s>]` (e.g. `rc2014_send_text(text="4")`)
instead of going through `C`/`Z` first.

## File transfer

`rc2014_upload` and `rc2014_download` are content in, content out — no host
file path involved. **Do not run `XM` yourself first**; both tools handle the
whole sequence internally, and getting the drive/user prefixes right by hand
is error-prone.

**`name` is a bare 8.3 filename, never `"B:HELLO.TXT"`.** This breaks from
every other tool here (`rc2014_read_text_file`, `rc2014_run_command`, ...),
which all take one combined `cpm_path`/command string with the drive baked
in. These two split it: drive and user area are separate parameters.

```
rc2014_upload(name="HELLO.TXT", drive="B", content="line one\nline two\n")
-> {"ok": true, "target": "B:HELLO.TXT", "existed_before": false,
    "bytes_raw": 20, "bytes_wire": 132, "compressed": true,
    "verified": true, "sha256": "..."}

rc2014_download(name="HELLO.TXT", drive="B")
-> {"ok": true, "target": "B:HELLO.TXT", "content": "line one\nline two\n",
    "binary": false, "bytes_raw": 20, "sha256": "..."}
```

Both require an OS to be running (`state=cpm`). `upload` always zips the
content and `UNZIP`s it on the board — a whole-file CRC check on the way in,
essentially free at these sizes. `download` always uses XMODEM's 1K-block
mode (`XM SK`), since that only exists on the board's send verb and download
is the direction where the board sends.

`binary=false` (the default, both directions) applies CP/M's CRLF +
trailing-`0x1A`-EOF-marker convention for text, matching what `TYPE` and
other tools expect — and encodes as **latin-1**: an em-dash, curly quotes, or
anything else outside that charset is silently replaced with `?` on upload,
with no error. Confirmed the hard way, sending this very file's README
through it. Use `binary=true` (base64) for any content that has to survive
exactly.

`binary=true` passes raw bytes through untouched (`content` is base64 on the
way in and out) — but is only byte-exact to the file's 128-byte CP/M record
size, confirmed against real hardware. CP/M has no sub-record length anywhere
in the filesystem (`STAT` only ever reports whole records), so a binary file
whose true length isn't a multiple of 128 can come back with trailing
padding. That's what the CRLF/EOF convention exists to solve for text;
there's no equivalent for arbitrary binary data.

The download result's `block_size` only reflects the *last* block received,
not the whole transfer — RomWBW's sender drops to 128-byte blocks for a short
tail even when the bulk of the file went over in 1K blocks. Don't read it as
"1K blocks didn't happen" just because it says 128.

`UNZIP` won't replace an existing file any more than `XM` will, so
`rc2014_upload` erases the target first when `overwrite=true` and reports
`replaced_existing`; the default is `overwrite=false` (fails instead of
replacing). Uploads to the ROM disk (`MD1`, usually `C:`) always fail — it is
read-only. `user` (0-15) addresses a CP/M user area directly, e.g.
`drive="A", user=1` reaches `A1:` without a separate `USER` command.

For a quick text peek without a transfer, `rc2014_read_text_file` uses `TYPE`
— cheaper, but **not byte-exact**: tabs are expanded and reading stops at the
0x1A EOF marker. Use `rc2014_download` when the exact bytes matter.

## System information

- `rc2014_get_hardware_info()` — RomWBW version, CPU, memory/MMU, HBIOS
  devices, ZSDOS/CBIOS versions, TPA size, drive mappings, last drive scan.
- `rc2014://docs/hardware-overview` — the same data as prose, rendered from
  the boot banner the bridge captured. **Read this rather than assuming a
  standard build.**
- `rc2014_survey()` — runs RomWBW's `SURVEY` (~7s, one command) and is the best
  single source of system detail: per-drive bytes used / file count / bytes free,
  the 64K memory map, BIOS and BDOS addresses, TPA size, and the active I/O port
  map. **Its per-drive file counts cover every CP/M user area.**
- `rc2014_scan_drives()` — catalogue every mapped drive with free space,
  access mode, a file sample, and a guess at its purpose. Takes tens of
  seconds. For one drive, `rc2014_run_command("DIR B:")` is far faster.

### User areas matter here

`DIR` — and therefore `rc2014_scan_drives` — only lists the **current CP/M user
area**. On this system `A:` shows 50 files in user 0 but holds 210 in total (a
HI-TECH C install in user 1, games in user 3). If a file you expect is missing,
try `USER 1`, `USER 3`, and so on, or use `rc2014_survey` for real totals.

Changing user area changes the prompt: `A>` becomes `C2>` (drive letter, then
user number). Set it back with `USER 0` when you're done, so later commands and
scans see what you expect.

## Only one operation at a time

The bridge serializes anything that owns the serial wire. A tool returning
`{"busy": true, "error": "busy: XMODEM SEND in progress"}` means wait and
retry — the machine is fine. Human keystrokes are never blocked, so a person
can always type or press Ctrl-X to cancel a transfer.

## CP/M reminders

- Filenames are uppercase 8.3. Run programs by bare name (`MBASIC`, not
  `MBASIC.COM`).
- The RAM disk (`MD0`) survives a *reset* — it's static RAM and only a real
  power cycle clears it. Don't assume a reboot gives a clean slate.
- The ROM disk (`MD1`) is genuinely read-only.
- `ERA` supports wildcards and does not ask for confirmation.
