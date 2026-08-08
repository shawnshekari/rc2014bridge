# rc2014bridge

A serial bridge for an RC2014/RomWBW retro computer: a pygame terminal
for a human, a local control API for an LLM (Claude, or any local model),
both driving the same serial console **concurrently** — no trading
exclusive access, no relaying text back and forth by hand.

Built for an RC2014 Pro running RomWBW, but the serial/XMODEM layer isn't
board-specific. See [`ROADMAP.md`](ROADMAP.md) for the design history and
what's planned next (an on-device agent, and possibly custom RCBus
hardware).

## What it does

- Owns the serial port permanently (replaces minicom), feeding incoming
  bytes into an embedded [`pyte`](https://github.com/selectel/pyte)
  terminal emulator that tracks real screen state — a character grid and
  cursor position, not a raw byte blob.
- Renders that screen in a pygame window and forwards local keystrokes to
  the port, same as any terminal program.
- Exposes a local Unix-socket JSON API alongside the GUI, so a model can
  drive the exact same session a human is watching.
- Implements XMODEM send *and* receive from scratch — not a wrapper
  around `sx`/minicom — because getting real file transfer working
  against actual RomWBW hardware surfaced quirks a generic wrapper
  wouldn't (see below).

## Quick start

```
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m rc2014bridge.app --port /dev/ttyUSB0
```

A window opens showing the console. Type into it like any terminal.

## Using the API

While the app is running, drive it from another shell:

```
python client.py get_screen
python client.py send_text '2\r'                     # boot disk unit 2
python client.py wait_for 'Boot \[H=Help\]:' --timeout 5
python client.py xmodem_send /path/to/file.rom
python client.py xmodem_receive /path/to/save.bin
```

Or hit the socket directly — newline-delimited JSON in, newline-delimited
JSON out:

```json
{"cmd": "send_text", "text": "DIR\r"}
{"cmd": "wait_for", "pattern": "B>\\s*$", "timeout": 5}
```

Commands: `send_text`, `get_screen`, `wait_for(pattern, timeout)`,
`xmodem_send(path)`, `xmodem_receive(path)`.

## Real hardware bugs found building this

All reproduced and confirmed against a physical RC2014, not guessed:

1. **RomWBW's `XM.COM` sends `C` then a trailing `K`** on its first poke,
   hinting it'd prefer 1K/STX blocks. A sender is allowed to ignore that
   hint and use plain 128-byte blocks, but the `K` byte still has to be
   drained or it gets misread as the response to block 1.
2. **The RC2014's UART cannot absorb a full ~133-byte XMODEM block
   written as one unpaced burst at 115200 baud.** It silently drops
   bytes, which shows up as a deterministic NAK on every retry, not
   random corruption. This isn't a bug in this code specifically — `sx`
   (lrzsz, a mature and independent implementation) fails identically
   against the same receiver. Fixed by pacing writes in small chunks with
   a delay between them; confirmed empirically (1ms delay: 15/15 trials
   failed; the shipped default of 8-byte chunks / 10ms delay: reliable).
3. **Switching to XMODEM mode can race with in-flight terminal text** —
   the receiver's own banner contains a literal capital `C` (in
   "Ctrl-X"), which could get caught by the handshake detector instead of
   the real protocol poke. Fixed with a settle-and-drain before trusting
   anything read off the wire.
4. **A failed transfer used to leave the receiver hanging mid-session.**
   Fixed: any failure or exception now sends a cancel (`CAN CAN`) before
   returning, so the next command starts from a clean state.

## Layout

```
rc2014bridge/
  link.py      serial port owner: pyte screen state, wait_for, XMODEM send/receive
  api.py       Unix-socket JSON control server
  display.py   pygame rendering + keyboard forwarding
  app.py       entry point wiring the above together
client.py      CLI for calling the API
```
