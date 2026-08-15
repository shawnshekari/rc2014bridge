# Zmodem investigation — open questions

Working notes for whether to add Zmodem support, requiring companion app(s) on
the device. Z80 (RC2014 Pro) first; Z180 (SC700 series) optimizations later.
See `ROADMAP.md` for how this fits the broader Phase II ("on-device agent")
plan, and `README.md` for the existing XMODEM implementation and the real
hardware bugs it already surfaced.

## Where this started

Zmodem's draw over XMODEM: streaming transfer (no per-block ACK wait), larger
windows, CRC-32, filename/size negotiation, resume. That maps directly onto a
gap `ROADMAP.md` already names for Phase II — "a purpose-built file-transfer
protocol that beats XMODEM's overhead." The risk: Zmodem's speed comes from
*not* pausing for turnaround, which is exactly the behavior that already
caused silent byte-loss on this hardware with XMODEM (see `README.md` bug #2).
Whether that risk is real turned out to depend on hardware flow control, which
is what most of the investigation below is about.

## Summary of where we are

- **Confirmed 2026-08-12** — hardware RTS flow control works, on the
  existing RC2014 Pro board, with no purchase and no soldering. Added an
  opt-in `--rtscts` flag (`link.py`, `app.py`), restarted the bridge against
  the real board, and ran `rc2014_calibrate_pacing`: all six candidate
  pacing settings passed byte-exact verification, including the fully
  unpaced `(256, 0.000)` setting — one write per 128-byte block, no delay at
  all, the exact burst pattern that caused silent byte-loss in the original
  bug report. Confirmed further with a real ~17.8KB file (`README.md`, not
  the calibration tool's synthetic payload): `rc2014_upload` to `B:` then
  `rc2014_download` back both reported sha256
  `88fab7ae...fef4de4`, matching the local file exactly, in both
  directions. The adapter is an FTDI FT232R (`ftdi_sio`), well-supported for
  hardware flow control on Linux, and the cable has 6 wires. See "Z80 —
  confirmed facts" below for what this does and doesn't prove.
- **Confirmed 2026-08-12, same day** — repeated on the SC700 (Z180) with the
  same FTDI cable moved over: console is on ASCI0 (Channel A, the one with
  real RTS/CTS in Z180 silicon), RomWBW's ASCI driver implements the same
  flow-control pattern as the SIO driver, and the full pacing sweep +
  real-file round trip passed cleanly there too. Both boards are now
  confirmed clean on device→host flow control with zero hardware changes.
  See "Z180 — confirmed facts" below.
- **Confirmed 2026-08-12, same day** — a third board, an SC140 (Z8S180-N,
  config `SCZ180_sc140`, console on `ASCI0 IO=0xC0` — same channel-A pattern
  as the SC700), bridge already running with `--rtscts`. Full pacing sweep
  passed every candidate including the fully unpaced `(256, 0.0)` burst, at
  791 bytes/sec — identical throughput to the SC700, consistent with both
  being turnaround-limited rather than pacing-limited. Real-file round trip
  (`README.md`, upload to `B:` then download) matched sha256
  `88fab7ae...fef4de4` exactly, same file as the Pro's earlier round trip.
  Three for three now on device→host flow control with zero hardware
  changes. See "Z180 — SC140" below.
- Host side (`link.py:495`, now fixed): `serial.Serial()` previously had no
  `rtscts` argument, so pyserial defaulted it to `False`. Hardware flow
  control was off by default; it's now available via `--rtscts` (off by
  default, opt-in until proven safe across more scenarios).
- RomWBW's Zilog SIO driver (`sio.asm`) already implements real flow control
  in the firmware currently running on the RC2014 Pro — this was not
  something we assumed, it was traced directly:
  - `SIO_INTRCV` deasserts RTS (`SIO_RTSOFF` → WR5) once the receive ring
    buffer crosses half full; `SIO_IN` reasserts it (`SIO_RTSON`) once usage
    drops below a quarter. Interrupt-driven, already active.
  - WR3's Auto-Enables bit (chip-level: transmitter gated by its own CTS
    input, no software polling needed) is turned on by the `SER_RTS` config
    bit. `cfg_RCZ80.asm:48` sets `DEFSERCFG = SER_115200_8N1 | SER_RTS`, and
    `SIO0ACFG` (the console) inherits it — confirmed active for this board.
- The current, already-installed rc2014.co.uk Dual Serial Module (SIO/2)
  schematic shows `RTSA` (chip pin 17) wired out to the P2 header, but
  `CTSA`/`DCDA`/`DTRA` left floating (`DCDA` tied to GND via JP1). Same
  pattern on channel B. So device→host RTS (device telling host to pause —
  the direction that matters for the documented upload byte-drop bug) is
  already wired on hardware you own. Host→device CTS is not.

## Z80 — RC2014 Pro

### Confirmed facts

- SIO/2 chip (Z80 SIO/2) on the currently-installed rc2014.co.uk Dual Serial
  Module. Schematic-confirmed: RTS wired out on both channels, CTS floating
  on both channels (not a chip limitation — a board-layout choice specific
  to this module).
- Alternative modules found with CTS *and* RTS both wired to the header,
  confirmed from their schematics:
  - **SC104** (Small Computer Central) — same chip, Z80 SIO/2. Closest to a
    drop-in swap since it matches your current chip exactly.
  - **SC132** (Small Computer Central) — Z80 SIO/0 instead. Historically the
    "full modem control" SIO variant. Different chip, same wiring outcome.
  - Both use an identical resistor/header pattern (100k pull, 2k2 series) —
    looks like a design Cousins reuses across his serial module line.
- **Confirmed 2026-08-12**: with `--rtscts` enabled, `rc2014_calibrate_pacing`
  passed every candidate up to and including a fully unpaced burst, and a
  real-file upload+download round trip was byte-exact. This validates the
  device→host direction — the RC2014's `RTSA` output (wired on this board,
  per the schematic) reaching the host's CTS input, honored by pyserial
  before writing. It does **not** validate host→device (the device's `CTSA`
  input is still floating on this board per the schematic, unchanged) —
  downloads succeeding doesn't newly confirm that path, since XMODEM receive
  was never the direction with an overflow problem; the host side has far
  more buffering headroom than the RC2014's UART did. SC104/SC132 (or a
  soldered wire to `CTSA`, see below) still matter for genuine bidirectional
  flow control, but the originally-documented upload bug this investigation
  started from looks solved on the existing, unmodified board.
- **Soldering option raised**: the schematic showing no net on `CTSA` means
  no copper connects it to anything — it doesn't tell us whether a spare pad
  exists near the P2 header's 6th (currently unused) pin position. Worth
  checking with a multimeter against the physical board (chip package type —
  DIP-40 vs PLCC-44 — matters for how hard the solder job is) before
  concluding a purchase is required. Cheaper and faster than an SC104/SC132
  order if the pad is reachable.
- Also found, not yet evaluated in depth: **SC612** (68B50 ACIA, bidirectional
  RTS/CTS documented) and the **16C550 single serial module** (rc2014.co.uk) —
  real UART with FIFO, RomWBW ≥3.4.0 has a built-in HBIOS driver for it, but
  stock CP/M/BASIC/SCM (not RomWBW/HBIOS) doesn't support it.
- ESP8266 WiFi module (v1.2, owned) — no flow control lines at all. Jumper-
  routes to UART1 (ACIA header) or UART2, which is very likely SIO channel B
  — the same port `ROADMAP.md` already flags as "SIO1... currently unused...
  the natural candidate for a dedicated control channel" for Phase II. Not a
  flow-control fix; a possible alternate transport for the Phase II
  companion-agent control channel.

### Open questions

1. ~~Does the actual USB-to-serial cable/adapter on `ttyUSB0` physically
   carry the RTS wire through to the host?~~ **Answered 2026-08-12: yes.**
   6-wire cable, FTDI FT232R adapter, RTS confirmed live end-to-end.
2. ~~What adapter chip is it, and does its driver honor hardware RTS/CTS?~~
   **Answered 2026-08-12:** FTDI FT232R (`ftdi_sio`), confirmed working.
3. ~~Does `rtscts=True` change the safe pacing setting?~~ **Answered
   2026-08-12:** yes — every candidate up to a fully unpaced burst now
   passes byte-exact verification, vs. earlier boards needing real pacing.
4. If SC104 or SC132 is purchased: any RomWBW config/driver differences
   between SIO/2 and SIO/0 that need re-validating (e.g. SIO/0's combined
   `/RTXCB` pin on channel B vs SIO/2's separate `/RXCB`/`/TXCB`), or is it
   fully plug-compatible with the existing `RCZ80_std` config?
5. Has host→device flow control (downloads) ever actually been observed to
   fail, or has byte-drop only ever shown up on uploads? Determines how
   urgent the CTS-wiring (board-swap or solder) half of the fix actually is.
   Working theory as of 2026-08-12: probably low urgency — the host side has
   far more buffer headroom than the RC2014's UART did, so the failure mode
   that motivated this whole investigation was always upload-direction, and
   that direction is now confirmed fixed. Not yet proven downloads are safe
   under adversarial conditions, just that nothing points to them being at risk.
6. Does an existing historical CP/M Zmodem implementation exist to adapt
   (BBS-era tools — MEX, DSZ ports, ZCPR utilities), or is this a from-scratch
   Z80 implementation? Not yet searched.
7. Given RomWBW's SIO driver already does hardware-level flow control
   (Auto-Enables + interrupt-driven RTS), how much does that shrink the
   "companion program" scope versus the original assumption that Zmodem
   would need hand-rolled in-band XON/XOFF logic?
8. ~~Does real RTS/CTS make the existing `_write_paced` chunk/delay pacing
   unnecessary, or does it just raise the ceiling?~~ **Answered 2026-08-12:**
   with `--rtscts` on, pacing stopped mattering past 32/5ms — timing
   converged to ~21.8s regardless of tighter settings, including fully
   unpaced. Matches `README.md`'s existing prediction that per-block
   turnaround (not pacing) is the real ceiling once byte-loss stops being a
   risk. Open follow-up: does this hold at 1K/STX block sizes too, or only
   the 128-byte blocks tested so far?
9. Is the ESP8266 module (on the currently-unused second SIO channel) a
   better home for the Phase II on-device control channel than a second
   wired serial cable? Separate design thread, not decided.

## Z180 — SC700 series (SC722 CPU module)

### Confirmed facts

- Z180 ASCI channels A and B, both 115200 8N1.
- Port A: RTS/CTS hardware flow control supported and recommended by the
  vendor.
- Port B: **the Z180 CPU itself does not support RTS/CTS flow control** —
  a silicon limitation, not a board-wiring choice. No jumper fixes this.
- JP2/JP3 jumpers can route Port A's RTS/CTS to alternate bus pins (TX2/RX2),
  meaning routing is configurable but channel choice still matters.
- **Confirmed 2026-08-12**: console is `ASCI0` (`Char 0`), confirmed directly
  from the live boot banner over the connection being used to read it — not
  inferred. That's Channel A, the channel with real RTS/CTS support, both
  per the SC722 vendor docs and per RomWBW's own driver source (see below).
  Best-case outcome: the console sits on the one channel that actually
  supports flow control.
- **Confirmed 2026-08-12**: RomWBW's `asci.asm` mirrors `sio.asm`'s approach
  almost exactly — interrupt-driven RTS gating on receive (deassert at half
  full, reassert below a quarter) and an Auto-CTS-equivalent config bit, but
  explicitly bypassed for the odd-addressed ("secondary") port:
  `; THE SECONDARY ASCI PORT ON Z180 ACTUALLY HAS NO RTS LINE` /
  `JR NZ,ASCI_INTRCV2 ; IF SO, THIS IS SEC SERIAL, NO RTS!`. Confirms the
  Channel B limitation independently of the vendor page. `cfg_SCZ180.asm`
  sets `ASCI0CFG = DEFSERCFG = SER_115200_8N1 | SER_RTS`, same pattern as
  the Z80 board's `cfg_RCZ80.asm`.
- **Confirmed 2026-08-12**: this SC700 boots ZSDOS from an SD card (`SD0`,
  Disk 4), not a CompactFlash/IDE card — `IDE0`/`IDE1` report `NO MEDIA` on
  this board. Worth remembering when interpreting drive info — different
  from the RC2014 Pro's IDE0-based setup.
- **Data hygiene note**: the default `hardware_info.json` path is shared
  across boards. Restarting the bridge against the SC700 without a distinct
  `--hw-info` file left stale Pro data in place (old `pacing` block, wrong
  drive list). Fixed by using `--hw-info hardware_info_sc700.json` going
  forward — worth remembering any time testing switches between boards.
- **Confirmed 2026-08-12** — same experiment as the Pro, same result: with
  `--rtscts` and a fresh `--hw-info hardware_info_sc700.json`,
  `rc2014_calibrate_pacing` passed every candidate including the fully
  unpaced `(256, 0.0)` burst (791 bytes/sec once turnaround-limited, ~1.55x
  the default pacing's throughput). Confirmed again with a real ~17.8KB file
  (`README.md`) — `rc2014_upload` to `B:` then `rc2014_download` back both
  reported the same sha256 as the local file. Both host↔device directions
  clean on this board too.

### Open questions

1. ~~Which ASCI channel is the SC700's console wired to?~~ **Answered
   2026-08-12: ASCI0 (Channel A)**, confirmed from the live boot banner.
2. What are JP2/JP3 currently set to on the physical board? Not checked
   directly — moot for the current cable/connection since it's already
   confirmed working end-to-end, but worth knowing before assuming anything
   about how this board would behave with a different cable.
3. ~~Does RomWBW's Z180 ASCI driver implement the same interrupt-driven RTS
   gating as `sio.asm`?~~ **Answered 2026-08-12: yes**, confirmed directly
   from `asci.asm` source, restricted correctly to the channel that has RTS
   in silicon.
4. N/A — console is on Channel A, so the B-channel tradeoff question doesn't
   apply for the console link itself. Still relevant if Channel B is ever
   used as a second link (e.g. a Phase II control channel) — that channel
   would need software-driven (XON/XOFF-style) flow control if pacing ever
   becomes a problem there, since hardware RTS/CTS isn't available on it.
5. ~~Does the physical USB-serial adapter/cable used for the SC700 carry
   RTS/CTS?~~ **Answered 2026-08-12: yes** — same FTDI cable reused from the
   Pro, confirmed working via the calibration sweep and file round trip.
6. Original z180-optimization framing: since the documented bottleneck is
   per-block turnaround and UART pacing, not CPU speed, what does the Z180's
   extra clock actually buy a Zmodem implementation here — mainly CRC-32
   compute cost, not transfer throughput itself. Partial data point now: the
   SC700's per-block throughput (791 B/s) beat the Pro's once both were
   turnaround-limited, suggesting some of the Z180's speed advantage does
   show up even in the existing 128-byte-block XMODEM path. Worth
   quantifying further only once a working Z80 baseline exists.

## Z180 — SC140

### Confirmed facts

- Small Computer Central SC140, `SCZ180_sc140` config — Z8S180-N @ 18.432MHz,
  Z180 MMU, same family as the SC700 but a distinct physical board (different
  vendor product page, separate RTC daughter-board).
- Console is `ASCI0: IO=0xC0` — Channel A, same channel the SC700 confirmed
  has real RTS/CTS in Z180 silicon (see SC700 section above; the same
  `asci.asm` firmware runs on both, so the Channel A vs. B distinction is a
  chip-level fact, not board-specific).
- **Confirmed 2026-08-12**: bridge was already running against this board
  with `--rtscts` set (no restart needed — flag was passed at launch).
  `rc2014_calibrate_pacing` passed every candidate up to and including the
  fully unpaced `(256, 0.0)` burst, 791 bytes/sec — matching the SC700's
  number exactly, reinforcing that per-block turnaround, not pacing, is the
  shared ceiling across same-family Z180 boards.
- **Confirmed 2026-08-12**: real-file round trip (`README.md`, ~17.8KB) —
  `rc2014_upload` to `B:` then `rc2014_download` back, both reporting sha256
  `88fab7ae...fef4de4`, matching the local file and the Pro's earlier round
  trip on the same source file. Both host↔device directions clean.
- Not yet checked: which physical USB-serial cable/adapter is in use for this
  board (assumed the same FTDI FT232R pattern given the clean result, but
  not individually confirmed the way the Pro's cable was).

### Open questions

1. Same JP2/JP3-style routing question as the SC700 — not checked, moot for
   the current working connection.
2. `hardware_info.json` is the shared default path and currently holds this
   board's data, overwriting whatever the SC700/Pro sessions last wrote —
   same data-hygiene gap noted in the SC700 section below, still unresolved
   as of this writing.

## Cross-cutting / protocol-level questions

1. Full Zmodem (ZDLE escaping, CRC-32, sliding window, subpacket resync) is a
   substantially bigger implementation than XMODEM was, even with hardware
   flow control removing the need for hand-rolled software throttling. No
   effort estimate yet.
2. Is Zmodem now the answer to `ROADMAP.md`'s Phase II "purpose-built
   file-transfer protocol," or a separate, simpler thing bolted onto Phase
   I's existing XMODEM code path? Affects scope and where the work lives.
3. Relationship between the file-transfer channel and Phase II's proposed
   SIO1 control channel — same channel, or kept separate?

## Recommended next steps (cheapest first)

1. ~~Identify the USB-serial adapter/cable model on the RC2014 Pro; confirm
   which wires are present.~~ **Done 2026-08-12** — FTDI FT232R, 6-wire cable.
2. ~~Flip `rtscts=True` and rerun `rc2014_calibrate_pacing`.~~ **Done
   2026-08-12** — full pass including unpaced burst; confirmed again with a
   real-file round trip. Upload byte-drop bug looks solved on the RC2014 Pro
   as-is.
3. ~~Same adapter/cable identification, for the SC700's port.~~ **Done
   2026-08-12** — same FTDI cable reused, confirmed working.
4. ~~Run `rc2014_get_hardware_info` against the SC700 to find the console's
   ASCI channel.~~ **Done 2026-08-12** — ASCI0 (Channel A), the one with
   RTS/CTS support. Best case: no channel-swap tradeoff needed.
5. ~~Read RomWBW's Z180 ASCI driver for flow-control behavior.~~ **Done
   2026-08-12** — mirrors the SIO driver's approach, correctly restricted to
   Channel A.
6. ~~Test `--rtscts` + `rc2014_calibrate_pacing` on the SC700.~~ **Done
   2026-08-12** — full pass including unpaced burst, confirmed again with a
   real-file round trip. Both boards are now clean on device→host flow
   control with zero hardware changes.
7. Decide whether host→device (CTS) flow control is still worth pursuing on
   either board — Pro via SC104/SC132 or a soldered `CTSA` wire; SC700 is
   moot since its console channel has no CTS-side gap to close in the first
   place (Channel A already has both directions in silicon — only Channel B
   lacks RTS entirely, and nothing currently uses Channel B). Lower urgency
   than originally scoped; revisit only if a future streaming protocol
   genuinely needs bidirectional hardware flow control.
8. Search for an existing CP/M Zmodem implementation to adapt before
   committing to writing one from scratch. Now the main remaining unknown on
   both platforms — the flow-control investigation that motivated it is
   essentially closed out.
