# SC140 (SCZ180_sc140_std) — RomWBW update notes

Companion to `RCZ80_std-Update-Notes.md` and `SCZ180_sc700_std-Update-Notes.md`,
for the Small Computer Central SC140. Same procedure, different platform/config,
plus a real board-specific gotcha (the flash write-protect jumper) not seen on
the other two boards.

## The board

- **Small Computer SC140** (Z50Bus card), config `SCZ180_sc140_std` —
  Z8S180-N @ 18.432MHz, Z180 MMU, 0 MEM W/S, 2 I/O W/S, INT MODE 2, 512KB ROM /
  512KB RAM. Separate RTC daughter-board.
- **Console is the Z180's on-chip ASCI**, same pattern as the SC700:
  `ASCI0: IO=0xC0` at 115200 8N1 (`ASCI1: IO=0xC1` unused).
- **Storage is a 15GB SDHC card** (`SD0: SDHC NAME=SU16G SIZE=15193MB`) as
  **disk unit 4**, slices 0–7 mapped `A:` and `D:`–`J:`. Boot with `4` at the
  loader, same as the SC700.
- Firmware before this update: **RomWBW v3.4.0 (2023-12-31)**, CBIOS v3.4.0 —
  older than the SC700's pre-update v3.5.0 baseline. Notably, `REBOOT.COM`
  didn't exist anywhere on this board (not on `A:`, not on the `C:` ROM disk)
  before the update — a gap the SC700 didn't have (it was at least present on
  `C:` there).
- Already set up with the mandel project on `J:` (`SD0:7`) before this update
  — untouched by the whole procedure below, as expected (flashing only
  touches the ROM chip; `SYSCOPY` only touches `A:`'s system tracks).

## Part 0 — Flash chip write-protect jumper (SC140-specific)

**Not present on the RC2014 Pro or SC700.** The SC140 has a physical write-protect
jumper, **JP1**, gating the flash chip's `/WE` (write-enable) line to either Vcc
(protected) or the CPU's `/WR` (writable). Confirmed from the vendor page
(smallcomputercentral.com): green shunt position = write-protected, red = write-enabled.
Flash chip is an **SST39SF040** (512KB), explicitly in RomWBW's supported chip-ID
table (`updater.asm:1442`, `$BFB7`).

With the jumper in the write-protect position, the XModem Flash Updater doesn't
give a jumper-specific error — it reports **`FLASH CHIP NOT SUPPORTED`**, identical
to what you'd see with a genuinely incompatible chip. Root cause: the updater's
chip-identify routine works by toggling `/WE` through a software-ID entry sequence;
with `/WE` tied to Vcc, that sequence can't execute, so the identify read comes back
invalid. **If this error appears, check the write-protect jumper before assuming the
chip itself is unsupported.**

Fix: move JP1 to the write-enable (red) position, power-cycle the board, retry.
Move it back to write-protect once done — the flash isn't touched again until the
next update.

## Part 1 — Build the image

```bash
cd ~/tools/RomWBW
make --directory Tools                     # once
make --directory Source shared             # once per checkout; ~2 min
cd Source/HBIOS && ROM_PLATFORM=SCZ180 ROM_CONFIG=sc140_std bash Build.sh
```

Built 2026-08-12 from checkout `v3.7.0-dev.13` (`05b49f3e`) — same commit already
used for the SC700 build. The `SCZ180_sc140_std` config already existed in the
checkout; no source changes needed.

```
strings Binary/SCZ180_sc140_std.rom | grep -i "HBIOS v"
# RomWBW HBIOS v3.7.0-dev.13, 2026-08-12
sha256sum Binary/SCZ180_sc140_std.rom
# 526a17dc666948f475c419ebca74a19bbbe49f4fafbdb9dd0319494cf79d3071
```

## Part 2 — Flash via the loader's XModem updater

Same path as the SC700: `L` then `X` (not on the `H` help menu).

1. At `Boot [H=Help]:` press `L`, then `X` to enter the updater.
2. Press `U` to begin — it prints `START TRANSFER OF YOUR UPDATE IMAGE OR ROM`.
3. Send `Binary/SCZ180_sc140_std.rom`.
4. Verify with the updater's own CRC32 option (`1`, Flash #1 — this board has a
   single 512KB chip, so the whole image lands there) before rebooting.
5. Press `R` for a cold reboot. Confirm the banner reads `RomWBW HBIOS v3.7.0-dev.13`.

### Pacing — clean with `--rtscts`, no manual tuning needed

Unlike the RC2014 Pro and SC700 (calibrated at `32:5` and similar before flow
control was confirmed), this board was driven via `rc2014bridge`'s MCP tools
directly with `--rtscts` already enabled at launch. `rc2014_calibrate_pacing`
passed every candidate including the fully unpaced `(256, 0.0)` burst, 791 bytes/sec
— identical to the SC700's number. A real-file round trip (`README.md`, upload+
download, sha256 match) confirmed both directions clean before touching the ROM.
See `ZMODEM.md`'s "Z180 — SC140" section for the full writeup.

At 791 B/s the 512KB image transferred in ~7.5 minutes via `rc2014_xmodem_send`
(the raw-send MCP tool, called after manually arming the updater with `send_text`/
`send_keys` — not `rc2014_upload`, which drives CP/M's `XM.COM` and won't work at
the loader). The transfer itself ran as an MCP call that exceeded the client's
inline timeout and was automatically moved to a background task, completing with a
notification rather than needing the pygame-window workaround the SC700 notes
recommended — that workaround was about the *client* timing out, and background-task
handling solves the same problem for a long MCP call.

### The XMODEM-mode/terminal-mode race — looked hung, wasn't

After the transfer's EOT was ACKed (confirmed at the raw serial level:
`rc2014bridge`'s log showed `EOT sequence finished (ACKed: True)`), **no further
output ever appeared in the bridge's screen buffer** — not the updater's
`COMPLETED WITHOUT ERRORS` message, nothing. `rc2014_wait_for` with a bare `.`
wildcard pattern (matches any incoming byte at all) came back empty even after
several minutes, which looked exactly like a genuine firmware hang right after
the ROM write.

It wasn't. Root cause, found by reading `link.py`: during an XMODEM send, incoming
bytes are routed to a separate protocol-only queue, not into the terminal/pyte
screen buffer, and the bridge only switches back to `"terminal"` mode *after*
finishing the EOT/ACK exchange. On the Z180 side, `updater.asm`'s response to EOT
is: send ACK, then — in the same instruction stream, microseconds later — print
`COMPLETED WITHOUT ERRORS` and loop back to the menu. That's almost certainly
faster than the Python-side mode switch, so the success message was very likely
sent and simply landed in the wrong queue, never reaching the visible screen buffer.

Confirmed harmless in this case because RomWBW writes and verifies each 4KB flash
sector *as it arrives* (every 32 XMODEM packets), not as one batch after EOT — so
by the time EOT is ACKed, all writing is already done and verified; a "hang" at
that point (real or apparent) is not a mid-write hang. Recovery was a plain `<CR>`
sent via `rc2014_send_keys` (confirmed safe first by reading the menu-dispatch code
in `updater.asm` — no key including CR is bound to a destructive action, unrecognized
input just loops back to redisplay the menu) — which did return the menu, confirming
the device was alive throughout, not actually hung.

**Worth fixing in `rc2014bridge` itself**: this is a real gap, not just a one-off
surprise — the mode-switch-vs-incoming-bytes race can silently drop the one message
that matters most (transfer success/failure) for any raw XMODEM operation, and
`rc2014_reboot` separately returned `{"ok": true}` on this board even when nothing
actually rebooted (it fires `C:REBOOT /C` without confirming the reboot happened,
which surfaced here specifically because `REBOOT.COM` didn't exist pre-update).

### Verification: CRC32 instead of trusting ACKs alone

The updater's built-in CRC32 option (`1`/`2`/`3` on its menu) uses the standard
reflected CRC-32 algorithm (poly `0xEDB88320`, init/final `0xFFFFFFFF` — confirmed
by reading `CALCCRC`/`CRC32` in `updater.asm`), i.e. exactly what Python's
`zlib.crc32()` computes. Locally: `zlib.crc32(open(rom_path,'rb').read())` →
`FFE006BB`. Device-reported CRC32 for Flash #1 after the write: **`FFE006BB`** —
exact match, byte-perfect flash confirmed independent of the transfer's own
ACK/NAK bookkeeping.

Which flash chip a `U`pdate targets isn't asked at the time — `MENULP` resets the
write pointer to bank `$0000`/sector 0 every time the menu is (re)displayed, the
same starting address `(1)`'s CRC check uses for Flash #1. Since the image is
exactly 512KB (16 banks, this board's one populated chip), a `U` immediately after
the menu appears always targets Flash #1.

## Part 3 — Boot tracks on the SD card

Same as the SC700: flashing ROM doesn't touch the SD card, so CBIOS stayed at
v3.4.0 and the boot printed:

```
*** WARNING: HBIOS/CBIOS Version Mismatch ***
```

Fixed identically:

```
A>C:SYSCOPY A:=C:ZSYS.SYS
Transfer system image from C:ZSYS.SYS to A: (Y/N)? Y
Reading image... Writing image... Done
```

Disk buffer space went from 1461 to 1859 bytes free — same numbers as the SC700's
update, consistent given both are the same CBIOS build. Warning gone on reboot;
`J:` files untouched.

## Part 4 — Refreshing the HBIOS utilities on A:

Same 13 apps as the SC700 list: `ASSIGN MODE RTC SYSCOPY XM FDU FORMAT SURVEY
SYSGEN TALK TIMER CPUSPD REBOOT`. On this board **all 13 needed refreshing**
(vs. 7 on the SC700), and `REBOOT.COM` was being *added* to `A:` for the first
time rather than replaced, since it didn't exist in the v3.4.0 ROM disk's app set.

```
A>C:PIP A:=C:ASSIGN.COM[O]        (repeat per app, one at a time)
A>A:COMPARE A:ASSIGN.COM=C:ASSIGN.COM     -> FILES MATCH, LENGTH IS ... BYTES
```

**Multi-file PIP syntax doesn't work on this build** — `PIP A:=C:A.COM,C:B.COM,...`
(repeating the drive per file) and `PIP A:=C:A.COM,B.COM,...` (drive once, bare
names after) both fail with `INVALID FORMAT: C:ASSIGN.COM,`, and leftover
unconsumed input from the rejected command gets interpreted as a follow-up command,
producing garbage but recovering cleanly to a fresh prompt on its own. One-file-at-a-time
`PIP A:=C:file.COM[O]` is what actually works; that's what was used for all 13.

All 13 copied and byte-verified with `COMPARE` on 2026-08-12. `[O]` (no header
verification) was used consistently, matching the SC700 precedent.

**Same caution as the SC700 notes**: don't blanket-copy `C:*.COM` to `A:` — some
`A:` utilities carry embedded `ZCNFG` configuration a copy would silently discard.

## Part 5 — Startup and datestamps

`A:PROFILE.SUB` on this board:

```
ZPATH /D=D0,A0,$$
LDTIM
RELOG
```

(No `USER` line, and `ZPATH` order is `D0,A0` rather than the SC700's `A0,D0` —
minor board-specific difference, not something to "fix" to match.) Confirmed
working unchanged after the update — `LDTIM` loads `RomWBW HBIOS Clock 1.1` from
`CLOCKS.DAT` successfully, `A:TD` reports the correct date/time, `J:`'s mandel
files are all present and untouched.

**Same critical warning as the other two boards**: never send a keystroke during
startup — a stray CR while `PROFILE.SUB` is running silently aborts it partway,
and the clock driver then fails to load. Use `rc2014_wait_until_ready` after any
boot rather than a fixed delay.
