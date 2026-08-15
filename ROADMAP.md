# RC2014 Model-Control Interface — Roadmap & Design Notes

Working notes from the design discussion that led to this repo. Captures
the context needed to pick back up, the paths considered and set aside,
and the phased plan this project is following. See `README.md` for what
the code actually does today and how to run it.

## Why this came up

While updating an RC2014 Pro's RomWBW firmware from `v3.5.0-dev.18` to
`v3.7.0-dev.13`, every step was: human reads minicom output, pastes it to
Claude, Claude says what to type next, human types it, repeat. That's fine
once, but it doesn't scale, and it's the whole reason this project
started — how do we give a model direct, structured control of the
machine.

## Hardware / software context

- **Board**: RC2014 Pro, RCBus, config `RCZ80_std` — Z80 @ 7.372MHz, 0 MEM
  W/S, 1 I/O W/S, INT MODE 1, Z2 MMU, 512KB ROM / 512KB RAM (in-system
  flashable module — no EEPROM programmer needed, see below).
- **Serial**: `SIO0` at IO 0x80 is the console, wired to `/dev/ttyUSB0` at
  115200 8N1. `SIO1` at IO 0x82 exists on the board and is **currently
  unused** — the natural candidate for a dedicated control channel (see
  Phase II), so a control link and the human-visible console never
  contend for the same wire.
- **Storage**: `IDE0` is a 123MB CompactFlash card. Slice 0 (`A:`) runs a
  single ZSDOS system; slices 1–7 (`D:`–`J:`) are empty. `MD0`/`MD1` are
  the RAM disk and 384KB ROM disk (the ROM disk ships `XM.COM`,
  `SYSCOPY.COM`, `FLASH.COM`, `ASSIGN`, `MODE`, `RTC`, etc. — baked into
  the ROM image itself, always available regardless of what's on the CF
  card).
- **RomWBW itself**: source at `github.com/wwarthen/RomWBW`. Any
  firmware-level work discussed below (Phase II/III) happens in a
  checkout of that tree, not in this repo. There is **no hardware
  debugger** for RomWBW/HBIOS changes — the iteration loop is edit
  assembly → rebuild → flash via the XMODEM updater → power-cycle →
  observe. That constraint is a first-order factor in every proposal
  below that touches HBIOS or the boot loader.

## Relevant facts uncovered while reading the RomWBW source

These matter because they're the existing plumbing any future phase
should reuse rather than reinvent:

- **The boot loader already has a command parser.** `Source/HBIOS/romldr.asm`
  implements the `Boot [H=Help]:` prompt as a small command loop:
  numeric/`<unit>[.<slice>]` to boot a specific disk and slice, `X` to
  enter the XModem Flash Updater, `I <u> [<b>]` to switch console
  unit/baud, `V [<v>]` for HBIOS diagnostic verbosity, `R` to reboot. A
  few more exist but are compiled out of the help text: `L` (list ROM
  applications — confirmed still live), `N` (network boot), `D` (device
  inventory), `S` (slice inventory), `W` (RomWBW configure). This is the
  existing "control surface that exists before any OS is booted."
- **The XModem Flash Updater** (`Source/HBIOS/updater.asm`, by Phil
  Summers) is a full submenu reachable from the loader, and it already
  supports directing transfer progress to **a separate console/serial
  unit** from the one being flashed — i.e. the precedent for "use SIO1
  for control/status while SIO0 does something else" already exists in
  this exact subsystem.
- **`FLASH.COM`** (FLASH4 by Will Sowerbutts) is the CP/M-hosted
  equivalent — explicitly documents support for "RC2014 with 512KB ROM
  512KB RAM module" (this board), doing sector-level writes with
  automatic verify.
- **Serial I/O is already interrupt-driven and buffered per unit** in
  HBIOS's UART/SIO/ACIA drivers. This means SIO1 receiving bytes in the
  background, while some other unit is the active console, requires **no
  new low-level driver work** for Phase II.
- **There's an existing console-takeover mechanism** referenced as
  `AUTOCON` / `conpoll` in the loader's main wait loop. Not yet
  investigated in depth, but it's a plausible reuse point for "a second
  unit can assert control" — worth reading fully before designing Phase
  II from scratch.
- **HBIOS has a periodic timer interrupt** already in use for the boot
  countdown and RTC ticking — the other natural hook for anything that
  needs to run "in the background."
- **ROM space is genuinely tight.** An `RCZ80_std` build reported as
  little as ~9,926–10,034 bytes of slack remaining across different HBIOS
  banks. Any new feature needs to be conditionally compiled the way every
  other optional driver already is.
- **A SIMH (simulator) build target exists** for at least the `SBC`
  platform. Not confirmed whether an equivalent exists for `RCZ80` — worth
  checking, since it would let HBIOS/loader changes be iterated against a
  simulated serial port instead of the physical board.

## Proof of concept that led to Phase I

Before writing any of this code, the core mechanic was validated directly
against the physical board with nothing but throwaway scripts:

- Confirmed `/dev/ttyUSB0` is `root:dialout`, and the invoking user just
  needs to be in `dialout` — no `sudo` needed to open the port.
- Confirmed minicom holds an advisory lock and is a real, separate
  OS-level exclusive owner while running — two processes reading/writing
  the port concurrently would race and garble both. The human has to
  exit minicom before a host-side controller can safely take the port,
  at least in a turn-taking model.
- With minicom closed, `pyserial` from a plain script could: send a bare
  `\r` and get the boot loader's full help menu back; boot a disk unit
  and get the OS's complete boot banner back; and directly run a program
  on the board — confirmed working by eye against real hardware (an LED
  light show on the RC2014's Digital I/O module).
- **Learned along the way:** the RAM disk survives a board *reset* (it's
  static RAM, only clears on an actual power cycle) — don't assume a
  reset gives a clean slate when testing. Also: CP/M program invocation
  is by bare name, not the `.COM` extension.

This fully validated a host-side daemon as directionally correct — which
became **Phase I**, this repo.

## Paths considered and set aside

1. ~~**MCP server instead of a plain socket.**~~ **Resolved 2026-08-09 — MCP
   won.** Phase I originally shipped a Unix-socket JSON API plus a
   `client.py` CLI, on the reasoning that "the GUI already needed some
   local IPC." That reasoning turned out to be wrong: the GUI runs in the
   same process and calls `SerialLink` directly, so nothing ever consumed
   the socket. Once the MCP server existed, the socket was a second
   protocol with no consumer, and every new capability cost three
   implementations (link method, socket dispatch, CLI subcommand). Both
   `api.py` and `client.py` were deleted; MCP is the only control surface.
2. **On-device CP/M-hosted agent, built before host-side control.**
   Considered running a `.COM` program on the CF card that owns SIO1 as a
   control channel from the start. Set aside in favor of proving host-side
   control first (Phase I) — it's now **Phase II** below, a deliberate
   later step rather than an abandoned idea.
3. **Extending the boot loader's command parser (`romldr.asm`)** as a
   lower-cost alternative to a full on-device agent. Real limitation: the
   boot loader only runs *before* an OS boots — once CP/M/ZSDOS is
   running, the loader's parser isn't executing anymore, so this only
   ever covers "no OS booted yet" operations (ROM flashing, disk
   selection), not anything while an OS is running. Not pursued for now;
   noted here in case it's useful once Phase II needs a lower-cost first
   step.
4. **Full interrupt-driven background command dispatcher in HBIOS** —
   hooking the timer/SIO1-receive interrupts so commands are serviced
   *at any time* regardless of foreground OS. This is the only approach
   that would satisfy "outside of CP/M" in the strongest sense, but it's
   real, risky firmware work (interrupt re-entrancy against whatever's
   running in the foreground, competing for already-scarce ROM space, no
   debugger). Treat as a further-out option under Phase II if turn-taking
   ever proves limiting — not a prerequisite for it.

## The roadmap: three phases

### Phase I — pygame app over the existing serial link (done, 2026-08-08)

This repo. A single Python process is the sole, permanent owner of
`/dev/ttyUSB0`, replacing minicom entirely, and does double duty as both
the human-facing display and Claude's control surface — the
port-contention/handoff friction from the proof-of-concept above is gone
for good, since both the human and the model go through the same process
instead of trading exclusive access. Confirmed live: Claude booted the CF
card via the API while the human watched it happen in the pygame window,
then the human typed `DIR` directly into that same window and it rendered
correctly — genuine concurrent access, not turn-taking. See `README.md`
for the implementation and the tool surface.

**Revised 2026-08-09** after reviewing a real agent session against the
code. Three things were wrong in the first cut:

- The tool surface made the model poll. `send_text` followed by two to
  four guessed `get_screen(max_lines=?)` calls per command, with no way
  to know when a command had finished. Replaced by `run_command`, which
  waits for the prompt to return and hands back only that command's
  output. `wait_for` had been unusable for this because it drained a
  buffer nothing else consumed, so its first call matched instantly
  against scrollback from minutes earlier.
- XMODEM upload was silently corrupting files. The block sequence number
  was never incremented, so a conforming receiver discarded everything
  after the first 128 bytes while the transfer reported success.
- Screen-scraping the XMODEM setup (`XM R`/`XM S` with the right drive
  prefixes) was left to the model and failed repeatedly. Now encoded once
  in `upload`/`download`, which resolve `XM`'s location from the captured
  drive mappings, arm the receiver, transfer, and verify.

None of that changes the Phase II premise below — screen-scraping CP/M's
human-formatted output is still the underlying limitation — but it does
push out when that becomes the bottleneck.

Still a real limitation, not yet lifted: can't show anything the RC2014
doesn't itself print to the console — e.g. no way to mirror real Digital
I/O LED state unless something running on the board reports it as text.
That's exactly what Phase III would provide.

### Phase II — on-device binary agent for efficiency

Once Phase I's screen-scraping (parsing `DIR` tables, regexing prompts out
of human-formatted CP/M output) becomes the bottleneck, put a small
resident program on the RC2014 that speaks a compact, structured
request/response protocol instead — plus a purpose-built file-transfer
protocol that beats XMODEM's overhead for routine pushes.

- Runs over **SIO1**, not the console (SIO0) — keeps it off the
  human-visible console entirely.
- Start as a plain CP/M-hosted foreground program (turn-taking, cheap to
  write/iterate) — *not* the interrupt-driven always-on firmware version
  described in "paths considered and set aside" above. That remains a
  further-out option if turn-taking ever proves limiting, not a
  prerequisite for Phase II.

**Candidate driving use case: single-step Z80 debugger with a bridge-
side graphical front end.** Surfaced 2026-08-14 while designing the
`mandel` repo's pixel-stream protocol (see `~/src/mandel/protocol/
DESIGN.md` and that repo's PLAN.md for the sibling work this came out
of) — same architectural shape (target streams compact binary state,
bridge does the heavy rendering/UI lift) applied to debugging instead of
graphics: an on-device stub reports register state, memory contents, and
step/breakpoint events over SIO1, and this app renders a real register
panel, memory view, and step/continue/breakpoint controls instead of the
target having to format any of that as human-readable text. Naturally
non-competing with the pixel-stream work — that stays on the console
(SIO0), this would live on SIO1 per Phase II's existing design.

Worth checking before designing a debug stub from scratch: does this
board's ZSDOS build already ship `DDT`/`ZSID` (standard CP/M debuggers -
single-step, RST-patched software breakpoints, register dump)? If so,
"efficient protocol" might mean driving those existing primitives
programmatically instead of reimplementing single-step/breakpoint
mechanics from zero - the same "structured binary beats screen-scraping
human-formatted text" lesson that motivated Phase II in the first place.
Also answers/depends on this file's existing open question about
`conpoll`/`AUTOCON` and whether the loader's parser is reachable without
a full reboot.

Not started - logged here as a concrete motivating use case for Phase
II's still-undesigned wire protocol, not a commitment to build it next.

### Phase III — dedicated hardware on the RCBus

The only phase that provides genuine bus-level visibility — real
memory/IO access as it happens, not inferred from console text. This is
a real hardware project (schematic/PCB/firmware for whatever bridging MCU
is used), a different skill set than anything in Phase I/II.

Before committing to a from-scratch board:

- Check whether the RetroBrew/RC2014 community already has a bus-monitor
  module.
- Cheaper precursor: clip a logic analyzer onto the RCBus header pins for
  passive, read-only visibility — no custom PCB — to validate whether
  bus-level visibility is worth it before fabricating anything.

**Candidate shortcut — repurposing an owned TMSEMU module**
(<https://peacockmedia.software/RC2014/TMSEMU/>): a TMS9918A VDP emulator
module already on hand.

- It's I/O-port-based (jumper-selectable between 98/99 MSX, 08/09 Tatung
  Einstein, BE/BF Colecovision ranges), meaning it already contains real
  RCBus I/O-decode logic fast enough for VDP-register timing — exactly
  the kind of bus interfacing Phase III needs.
- **Confirmed 2026-08-08**: pulled the "Dr.VIP" daughter-module (socketed,
  resembles a DIP40 chip) and read the die marking directly:
  `RP2-B2 22/01 PCB261.00` — that's the RP2040 (B2 stepping).
- Practical implication: RP2040 boards are normally user-reflashable over
  USB via the BOOTSEL/UF2 bootloader baked into the chip's boot ROM — not
  something the vendor needs to support or publish source for.
- **Important caution before reflashing anything**: the BOOTSEL
  mass-storage mode only accepts a *new* `.uf2` — it doesn't expose a way
  to read the existing flash contents back out. Without a hardware SWD
  debug probe, reflashing is a one-way trip for this physical unit unless
  the vendor separately publishes a stock firmware `.uf2` to revert to.
  Check for that **before** touching BOOTSEL.

**Still to verify before committing this unit to Phase III:**
1. ~~Physically confirm the MCU~~ — done, confirmed RP2040 above.
2. Check for a published stock firmware `.uf2` — needed to preserve the
   option to revert this unit back to being a TMS9918 card later.
3. Decide the real tradeoff: repurposing this unit for bus access means
   giving up its current job as a video card (real-time VDP emulation
   almost certainly saturates the RP2040's timing budget) — likely an
   either/or per physical unit, not both at once.

## Open questions for next discussion

- Does any path exist to reach the loader's command parser again *without
  a full reboot* once an OS has booted (NMI, monitor re-entry, etc.)? If
  yes, it changes what a lower-cost Phase II step could look like.
- Fully read `conpoll`/`AUTOCON` in `romldr.asm` before designing Phase
  II — it may already be most of the "second unit takes control"
  mechanism.
- Does a SIMH (or other simulator) target exist or can one be added for
  `RCZ80`, to get a real edit/test loop for Phase II/III firmware work?
- Is there an existing console input ring buffer HBIOS already exposes
  that synthetic keystrokes could be injected into for Phase II, or would
  that need to be built?
- Decide the actual wire protocol (framing, checksums, command IDs) for
  the Phase II on-device agent — deliberately left undesigned here.
