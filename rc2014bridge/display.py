"""
Pygame front end: renders the SerialLink's pyte screen buffer and forwards
local keystrokes to the port. This is the "human" half of the bridge -
the model drives the same SerialLink concurrently through the MCP server.

Keystrokes go straight to the port, deliberately bypassing the link's
operation lock: a person must be able to type at any time, including Ctrl-X
to cancel a transfer an agent started. Menu actions do take the lock, so the
human can't kick off a drive scan in the middle of an agent's transfer.

The one exception is during an XMODEM transfer, where ordinary typing is held
back - it can only muddy the protocol stream, and it is never useful there.
Ctrl-C and Ctrl-X still go through, because a human who cannot interrupt a
stuck transfer has no way to rescue the machine. That is the whole reason this
is a guard and not a lock.
"""

import os
import shutil
import subprocess
import threading
import time

import pygame
from pygame._sdl2.video import Renderer, Texture, Window

from rc2014bridge import config as bridge_config
from rc2014bridge.link import list_serial_ports, probe_serial_connection

FG = (110, 255, 110)
BG = (10, 12, 10)
CURSOR = (110, 255, 110)
FONT_SIZE = 18
CURSOR_BLINK_MS = 500

ANSI_COLORS = {
    "black": (0, 0, 0),
    "red": (205, 0, 0),
    "green": (0, 205, 0),
    "brown": (205, 205, 0),  # pyte uses 'brown' for ANSI yellow
    "blue": (0, 0, 238),
    "magenta": (205, 0, 205),
    "cyan": (0, 205, 205),
    "white": (229, 229, 229),
    "brightblack": (127, 127, 127),
    "brightred": (255, 0, 0),
    "brightgreen": (0, 255, 0),
    "brightbrown": (255, 255, 0),
    "brightblue": (92, 92, 255),
    "brightmagenta": (255, 0, 255),
    "brightcyan": (0, 255, 255),
    "brightwhite": (255, 255, 255),
}

STANDARD_TO_BRIGHT = {
    "black": "brightblack",
    "red": "brightred",
    "green": "brightgreen",
    "brown": "brightbrown",
    "blue": "brightblue",
    "magenta": "brightmagenta",
    "cyan": "brightcyan",
    "white": "brightwhite",
}


def _resolve_color(color_val: str, default_rgb: tuple[int, int, int], is_bold: bool = False) -> tuple[int, int, int]:
    if color_val == "default":
        return default_rgb
    color_name = color_val
    if is_bold and color_name in STANDARD_TO_BRIGHT:
        color_name = STANDARD_TO_BRIGHT[color_name]
    if color_name in ANSI_COLORS:
        return ANSI_COLORS[color_name]
    if len(color_val) == 6:
        try:
            return (int(color_val[0:2], 16), int(color_val[2:4], 16), int(color_val[4:6], 16))
        except ValueError:
            pass
    return default_rgb

# name -> bytes, for keys that don't have a sensible event.unicode
SPECIAL_KEYS = {
    pygame.K_RETURN: b"\r",
    pygame.K_KP_ENTER: b"\r",
    pygame.K_BACKSPACE: b"\x08",
    pygame.K_TAB: b"\t",
    pygame.K_ESCAPE: b"\x1b",
    pygame.K_UP: b"\x1b[A",
    pygame.K_DOWN: b"\x1b[B",
    pygame.K_RIGHT: b"\x1b[C",
    pygame.K_LEFT: b"\x1b[D",
}


# Keys that must reach the board even mid-transfer: XM's cancel is Ctrl-X twice,
# and Ctrl-C is the general escape from a running program. Never suppress these -
# a human locked out of interrupting a stuck transfer has no way back.
INTERRUPT_BYTES = frozenset({0x03, 0x18})  # Ctrl-C, Ctrl-X


def operation_badge_label(mode_label: str, current_op: str) -> str | None:
    """The operation badge text, or None when it would only repeat the mode badge.

    During a transfer both are derived from the same state - the mode badge says
    "XMODEM-SEND" and the operation is "XMODEM send" - so showing both just says
    the same thing twice.
    """
    if not current_op:
        return None

    def key(text: str) -> str:
        return "".join(ch for ch in text.upper() if ch.isalnum())

    return None if key(current_op) == key(mode_label) else current_op.upper()


def _allowed_during_transfer(data: bytes) -> bool:
    """Whether a keystroke may go out while an XMODEM transfer owns the wire.

    Ordinary typing lands between blocks, where a receiver ignores non-SOH bytes,
    so it usually does no harm - but it is never *useful* mid-transfer, and it
    muddies the protocol stream. Interrupts are the exception, and the reason
    this is a guard rather than a block.
    """
    return len(data) == 1 and data[0] in INTERRUPT_BYTES


def _key_to_bytes(event) -> bytes:
    if event.key in SPECIAL_KEYS:
        return SPECIAL_KEYS[event.key]
    if event.mod & pygame.KMOD_CTRL and pygame.K_a <= event.key <= pygame.K_z:
        return bytes([event.key - pygame.K_a + 1])  # Ctrl-A..Ctrl-Z -> 0x01..0x1A
    if event.unicode:
        try:
            return event.unicode.encode("latin-1")
        except UnicodeEncodeError:
            return b""
    return b""


TOP_MENU_HEIGHT = 32
STATUS_BAR_HEIGHT = 32

# Presets offered in the Settings screen's baud dropdown. Standard rates a
# RC2014 UART (Z80 SIO/ACIA or 16550-alike) can plausibly be run at; any
# other rate is still reachable via --baud or the config file, this is just
# convenient presets for the common ones.
BAUD_CHOICES = (1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200, 230400)

MENU_DATA = [
    {
        "title": "File",
        "items": [
            {"label": "Scan Drives (CP/M)... (F6)", "action": "SCAN_DRIVES"},
            {"label": "Reboot RC2014... (F4)", "action": "CONFIRM_REBOOT"},
            {"label": "Quit", "action": "QUIT"},
        ],
    },
    {
        "title": "Transfer",
        "items": [
            {"label": "Send File (XMODEM)... (F2)", "action": "PROMPT_SEND"},
            {"label": "Receive File (XMODEM)... (F3)", "action": "PROMPT_RECEIVE"},
        ],
    },
    {
        "title": "View",
        "items": [
            {"label": "RC2014 Hardware & Disk Info... (F5)", "action": "SHOW_HW_INFO"},
            {"label": "Toggle Mandel Pixel-Stream Mode (F7)", "action": "TOGGLE_PIXEL_STREAM"},
            {"label": "Reset Scrollback (Shift+End)", "action": "RESET_SCROLLBACK"},
        ],
    },
    {
        "title": "Settings",
        "items": [
            {"label": "Connection Settings... (F8)", "action": "SHOW_SETTINGS"},
        ],
    },
]


def _settings_modal_layout(screen_w: int, screen_h: int) -> dict:
    """Rects for the Connection Settings modal - shared between the click
    handler and the renderer so hit-testing and drawing never drift apart.

    Column/button widths are sized for the longest label text they actually
    hold ("Flow Control (RTS/CTS):" at ~230px, "Apply & Reconnect" at
    ~171px in the 16pt bold monospace menu/status font), plus padding -
    not guessed round numbers, which is what let the label and button text
    overlap their neighbors before.
    """
    box_w, box_h = min(600, screen_w - 20), min(300, screen_h - 40)
    box_x = (screen_w - box_w) // 2
    box_y = (screen_h - box_h) // 2
    field_x = box_x + 250
    field_w = box_w - 250 - 16

    port_field = pygame.Rect(field_x, box_y + 44, field_w, 26)
    baud_field = pygame.Rect(field_x, box_y + 78, field_w, 26)
    rtscts_box = pygame.Rect(field_x, box_y + 112, 20, 20)

    btn_h, gap = 28, 10
    test_btn = pygame.Rect(box_x + 12, box_y + box_h - btn_h - 12, 180, btn_h)
    apply_btn = pygame.Rect(test_btn.right + gap, test_btn.y, 200, btn_h)
    cancel_btn = pygame.Rect(apply_btn.right + gap, test_btn.y, 100, btn_h)

    return {
        "box": pygame.Rect(box_x, box_y, box_w, box_h),
        "port_field": port_field,
        "baud_field": baud_field,
        "rtscts_box": rtscts_box,
        "test_btn": test_btn,
        "apply_btn": apply_btn,
        "cancel_btn": cancel_btn,
    }


def _dropdown_option_rects(anchor: "pygame.Rect", options: list) -> list:
    """One 24px-tall rect per option, stacked directly below `anchor`."""
    return [pygame.Rect(anchor.x, anchor.bottom + i * 24, anchor.w, 24) for i in range(len(options))]


def _draw_dropdown_list(surface, anchor, options: list, current_str: str, item_font):
    rects = _dropdown_option_rects(anchor, options)
    if not rects:
        return
    list_rect = pygame.Rect(anchor.x, anchor.bottom, anchor.w, rects[-1].bottom - anchor.bottom)
    pygame.draw.rect(surface, (24, 28, 34), list_rect)
    pygame.draw.rect(surface, (60, 75, 95), list_rect, width=1)
    mx, my = pygame.mouse.get_pos()
    for opt, r in zip(options, rects):
        hover = r.collidepoint(mx, my)
        if hover:
            pygame.draw.rect(surface, (45, 95, 165), r)
        color = (255, 255, 255) if (hover or str(opt) == current_str) else (200, 215, 230)
        img = item_font.render(f" {opt}", True, color)
        surface.blit(img, (r.x + 4, r.y + 3))


def _open_system_file_dialog(mode: str = "open", title: str = "Select File") -> str | None:
    # 1. Try zenity (native GTK dialog on Linux)
    zenity_path = shutil.which("zenity")
    if zenity_path:
        cmd = [zenity_path, "--file-selection", f"--title={title}"]
        if mode == "save":
            cmd.append("--save")
            cmd.append("--confirm-overwrite")
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, check=False)
            if res.returncode == 0:
                p = res.stdout.strip()
                if p:
                    return p
            return None
        except Exception:
            pass

    # 2. Try kdialog (native KDE dialog)
    kdialog_path = shutil.which("kdialog")
    if kdialog_path:
        cmd = [kdialog_path, "--getsavefilename" if mode == "save" else "--getopenfilename", os.getcwd(), f"--title={title}"]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, check=False)
            if res.returncode == 0:
                p = res.stdout.strip()
                if p:
                    return p
            return None
        except Exception:
            pass

    # 3. Try tkinter
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        if mode == "save":
            p = filedialog.asksaveasfilename(title=title)
        else:
            p = filedialog.askopenfilename(title=title)
        root.destroy()
        return p if p else None
    except Exception:
        pass

    return None


# mandel_z80's x_step:y_step samples a non-square grid - each cell is 2x
# taller than wide in real coordinate space. The old ANSI/character output
# hid this on purpose (a monospace terminal cell is itself ~2:1 tall:wide,
# chosen to match), so a bare 1:1 square-pixel render looks squished. True
# RGB pixel drawing doesn't get that accidental correction for free, so it's
# applied explicitly here instead. See protocol/DESIGN.md's Activation
# section note and mandel's PLAN.md for the sender-side step sizes.
PIXEL_ASPECT_Y_OVER_X = 2


def _pixel_cell_size(rgb_grid, max_w: int, max_h: int) -> tuple[int, int, int, int]:
    """Aspect-correct (cell_w, cell_h, p_cols, p_rows) fitting rgb_grid's
    grid into max_w x max_h, each cell PIXEL_ASPECT_Y_OVER_X times taller
    than wide. cell_w is 0 if the grid is empty or doesn't fit at all."""
    p_rows = len(rgb_grid)
    p_cols = max((len(r) for r in rgb_grid), default=0)
    if p_rows == 0 or p_cols == 0:
        return 0, 0, p_rows, p_cols
    cell_w = max(1, min(max_w // p_cols, max_h // (p_rows * PIXEL_ASPECT_Y_OVER_X)))
    return cell_w, cell_w * PIXEL_ASPECT_Y_OVER_X, p_cols, p_rows


def _draw_pixel_grid(surface, rgb_grid, origin: tuple[int, int], cell_w: int, cell_h: int):
    """Draw rgb_grid onto surface as cell_w x cell_h rects starting at origin."""
    ox, oy = origin
    for r_idx, row in enumerate(rgb_grid):
        for c_idx, rgb in enumerate(row):
            rect = pygame.Rect(ox + c_idx * cell_w, oy + r_idx * cell_h, cell_w, cell_h)
            pygame.draw.rect(surface, rgb, rect)


class PixelStreamWindow:
    """A standalone, non-modal popup window for exactly one Mandel pixel-
    stream render, identified by the decoder's render_id. Running MANDELPX
    (or similar) N times produces N of these, each keeping its own finished
    image on screen rather than one being overwritten by the next - unlike
    the main window's embedded pixel view, which always shows whichever
    render is currently live in the shared link/decoder.

    Deliberately simple: shows a placeholder immediately on open, then does
    exactly one real draw once its render completes (checked via
    is_complete on the shared decoder's snapshot) and stops updating -  no
    per-frame live redraw/resize while data is still streaming in. The
    embedded main view already covers "watch it fill in live"; a popup's
    job is just "keep this result around to compare against others."
    """

    MAX_W, MAX_H = 900, 700
    PLACEHOLDER_SIZE = (420, 90)

    def __init__(self, render_id: int, font):
        self.render_id = render_id
        self._font = font
        self._done = False
        self.closed = False
        self.window = Window(f"Mandel Pixel-Stream - render #{render_id}",
                              size=self.PLACEHOLDER_SIZE, resizable=True)
        self.renderer = Renderer(self.window)
        self._draw_placeholder()

    def _present(self, surf):
        tex = Texture.from_surface(self.renderer, surf)
        self.renderer.clear()
        tex.draw()
        self.renderer.present()

    def _draw_placeholder(self):
        surf = pygame.Surface(self.PLACEHOLDER_SIZE)
        surf.fill((10, 10, 15))
        msg = self._font.render("Awaiting Mandel Pixel-Stream data...", True, (150, 180, 210))
        surf.blit(msg, ((self.PLACEHOLDER_SIZE[0] - msg.get_width()) // 2,
                         (self.PLACEHOLDER_SIZE[1] - msg.get_height()) // 2))
        self._present(surf)

    def handle_event(self, event):
        if event.type == pygame.WINDOWCLOSE and getattr(event, "window", None) is self.window:
            self.closed = True

    def update(self, link):
        """Poll once per tick; no-op after the first real draw or once this
        render has been superseded without ever completing."""
        if self.closed or self._done:
            return
        snap = link.get_pixel_frame()
        if snap.get("render_id") != self.render_id:
            # A newer render started before this one ever completed (e.g.
            # aborted mid-stream) - leave the placeholder up rather than
            # showing a different render's data in this window.
            self._done = True
            return
        if not snap.get("is_complete"):
            return

        rgb_grid = snap.get("rgb_data", [])
        cell_w, cell_h, p_cols, p_rows = _pixel_cell_size(rgb_grid, self.MAX_W, self.MAX_H)
        if cell_w <= 0:
            self._done = True
            return

        # elapsed_s brackets VERSION_BANNER (reset(), i.e. first byte) to
        # END_OF_FRAME (last byte) on the decoder - see pixel_stream.py's
        # comment on started_at/completed_at for what this is (and isn't)
        # measuring. Host-side wall clock, not a device timestamp.
        elapsed_s = snap.get("elapsed_s")
        elapsed_str = f"{elapsed_s:.2f}s" if elapsed_s is not None else "?"
        self.window.title = f"Mandel Pixel-Stream - render #{self.render_id} ({elapsed_str})"

        px_w, px_h = p_cols * cell_w, p_rows * cell_h
        tag_text = f" PIXEL-STREAM v{snap.get('version') or 1} | {p_cols}x{p_rows} | {elapsed_str} "
        tag_img = self._font.render(tag_text, True, (255, 255, 255), (30, 90, 160))
        content_w = max(px_w, tag_img.get_width())
        content_h = px_h + tag_img.get_height() + 2

        self.window.size = (content_w, content_h)
        surf = pygame.Surface((content_w, content_h))
        surf.fill((10, 10, 15))
        surf.blit(tag_img, (0, 0))
        _draw_pixel_grid(surf, rgb_grid, (0, tag_img.get_height() + 2), cell_w, cell_h)
        self._present(surf)
        self._done = True

    def destroy(self):
        self.window.destroy()


def window_title(link, base: str = "RC2014 Bridge") -> str:
    """Name the machine in the title bar.

    Just its RomWBW build identifier (e.g. "SCZ180_sc700_std") - that names the
    board and CPU family in one token. The port and baud already live in the
    status bar, so repeating them here would only crowd the title. Falls back to
    the port until a boot banner has been captured.
    """
    hw = getattr(link, "hardware_info", {}) or {}
    machine = hw.get("config") or hw.get("platform") or ""
    return f"{base} - {machine}" if machine else f"{base} - {link.port}"


def run(link, config_path: str = bridge_config.DEFAULT_CONFIG_PATH, title: str = "RC2014 Bridge"):
    pygame.init()
    caption = window_title(link, title)
    pygame.display.set_caption(caption)
    font = pygame.font.SysFont("monospace", FONT_SIZE)
    menu_font = pygame.font.SysFont("monospace", 16, bold=True)
    status_font = pygame.font.SysFont("monospace", 16, bold=True)
    cell_w, cell_h = font.size("M")
    screen_w = link.cols * cell_w
    term_h = link.rows * cell_h
    term_y = TOP_MENU_HEIGHT
    screen_h = TOP_MENU_HEIGHT + term_h + STATUS_BAR_HEIGHT
    surface = pygame.display.set_mode((screen_w, screen_h))
    clock = pygame.time.Clock()

    scroll_offset = 0
    prompt_mode = None  # None, "SEND", "RECEIVE"
    prompt_text = ""
    show_reboot_modal = False
    show_hw_info_modal = False
    active_menu_idx = None
    toast_text = None
    toast_expires = 0.0
    running = True

    # Connection Settings modal state. settings_port/_baud/_rtscts are the
    # in-progress edits (only committed to the live link on Apply); the
    # dropdowns are mutually exclusive so at most one is open at a time.
    show_settings_modal = False
    settings_port = link.port
    settings_baud = link.baud
    settings_rtscts = getattr(link, "rtscts", False)
    settings_ports_cache: list[str] = []
    settings_port_open = False
    settings_baud_open = False
    settings_status = ("", None)  # (message, ok: True/False/None-in-progress)

    # Each new Mandel pixel-stream render (detected via a render_id change)
    # gets its own popup window - see PixelStreamWindow's docstring.
    # Seeded from the decoder's current render_id (starts at 1, not 0/None,
    # per PixelStreamDecoder.__init__) so no phantom popup spawns on launch
    # before any real render has happened.
    pixel_windows: list[PixelStreamWindow] = []
    last_seen_render_id = link.get_pixel_frame().get("render_id")

    # Watched every frame below so a toast fires for *any* connection
    # change - not just ones started from the Settings screen. In
    # particular, link.py can reconfigure the port on its own when it sees
    # HBIOS's console-baud-change sequence go by (see
    # SerialLink._follow_hbios_baud_change), which never calls into this
    # file at all.
    last_seen_connection = (link.port, link.baud)

    def _show_toast(text: str, seconds: float = 4.0):
        nonlocal toast_text, toast_expires
        toast_text = text
        toast_expires = time.time() + seconds

    def _test_settings_connection(port: str, baud: int, rtscts: bool):
        nonlocal settings_status
        settings_status = ("Testing...", None)

        def _worker():
            nonlocal settings_status
            res = probe_serial_connection(port, baud, rtscts)
            if res.get("ok"):
                settings_status = (f"OK: {port} @ {baud} baud opened successfully", True)
            else:
                settings_status = (f"Failed: {res.get('error', 'unknown error')}", False)

        threading.Thread(target=_worker, daemon=True).start()

    def _apply_settings(port: str, baud: int, rtscts: bool):
        nonlocal settings_status
        settings_status = ("Reconnecting...", None)

        def _on_done(res: dict):
            nonlocal settings_status
            if res.get("ok"):
                settings_status = (f"Connected: {res['port']} @ {res['baud']} baud", True)
                bridge_config.update_value(config_path, "serial", "port", res["port"])
                bridge_config.update_value(config_path, "serial", "baud", str(res["baud"]))
                bridge_config.update_value(config_path, "serial", "rtscts", "true" if res["rtscts"] else "false")
                # No toast here - the port/baud watcher in the main loop
                # covers it, and also catches HBIOS's own auto-followed baud
                # changes (see link.py's _follow_hbios_baud_change), which
                # never go through this method at all.
            else:
                error = res.get("error", "unknown error")
                settings_status = (f"Failed: {error}", False)
                # The modal closes as soon as Apply is clicked (reconnect
                # happens in the background), so a failure needs a toast too -
                # settings_status alone would go unseen.
                _show_toast(f"Reconnect failed: {error}", 6.0)

        link.reconfigure_async(port=port, baud=baud, rtscts=rtscts, callback=_on_done)

    def _on_xmodem_done(res: dict):
        if res.get("ok"):
            count = res.get("bytes", res.get("blocks", 0))
            unit = "bytes" if "bytes" in res else "blocks"
            _show_toast(f"XMODEM Success: Transferred {count} {unit}")
        else:
            _show_toast(f"XMODEM Error: {res.get('error', 'Failed')}")

    def _on_scan_done(res: dict):
        if res.get("ok"):
            _show_toast(f"Scan Success: {len(res.get('drives', []))} drives cataloged")
        else:
            _show_toast(f"Scan Error: {res.get('error', 'Failed')}")

    def _do_reboot():
        res = link.reboot() or {}
        if res.get("ok"):
            _show_toast("Reboot command sent to RC2014", 3.0)
        else:
            _show_toast(f"Reboot error: {res.get('error', 'Failed')}")

    def _trigger_action(action: str):
        nonlocal prompt_mode, prompt_text, scroll_offset, running, active_menu_idx, show_reboot_modal, show_hw_info_modal, toast_text, toast_expires
        nonlocal show_settings_modal, settings_port, settings_baud, settings_rtscts
        nonlocal settings_ports_cache, settings_port_open, settings_baud_open, settings_status
        active_menu_idx = None
        if action == "PROMPT_SEND":
            def _send_worker():
                nonlocal prompt_mode, prompt_text
                path = _open_system_file_dialog(mode="open", title="Select File to Send via XMODEM")
                if path:
                    link.xmodem_send_async(path, callback=_on_xmodem_done)
                elif not shutil.which("zenity") and not shutil.which("kdialog"):
                    prompt_mode = "SEND"
                    prompt_text = ""
            threading.Thread(target=_send_worker, daemon=True).start()
        elif action == "PROMPT_RECEIVE":
            def _recv_worker():
                nonlocal prompt_mode, prompt_text
                path = _open_system_file_dialog(mode="save", title="Select Save Location for XMODEM Receive")
                if path:
                    link.xmodem_receive_async(path, callback=_on_xmodem_done)
                elif not shutil.which("zenity") and not shutil.which("kdialog"):
                    prompt_mode = "RECEIVE"
                    prompt_text = ""
            threading.Thread(target=_recv_worker, daemon=True).start()
        elif action == "SCAN_DRIVES":
            _show_toast("Scanning RC2014 drives (A: .. J:)...", 6.0)
            link.scan_drives_async(callback=_on_scan_done)
        elif action == "CONFIRM_REBOOT":
            show_reboot_modal = True
        elif action == "SHOW_HW_INFO":
            show_hw_info_modal = True
        elif action == "TOGGLE_PIXEL_STREAM":
            cur_shown = getattr(link, "_show_pixel_view", False)
            new_en = not cur_shown
            link.enable_pixel_stream(new_en)
            if new_en:
                _show_toast("Mandel Pixel-Stream mode ENABLED (F7 to toggle)")
            else:
                _show_toast("Pixel-Stream mode DISABLED (terminal restored)")
        elif action == "RESET_SCROLLBACK":
            scroll_offset = 0
        elif action == "SHOW_SETTINGS":
            settings_port = link.port
            settings_baud = link.baud
            settings_rtscts = getattr(link, "rtscts", False)
            settings_ports_cache = list_serial_ports()
            if settings_port not in settings_ports_cache:
                settings_ports_cache = settings_ports_cache + [settings_port]
            settings_port_open = False
            settings_baud_open = False
            settings_status = ("", None)
            show_settings_modal = True
        elif action == "QUIT":
            running = False

    while running:
        mouse_pos = pygame.mouse.get_pos()

        for event in pygame.event.get():
            # The primary window's own events always have event.window is
            # None (confirmed empirically); only secondary popup windows
            # set it, to the Window object itself.
            event_window = getattr(event, "window", None)
            if event_window is not None:
                for pw in pixel_windows:
                    if event_window is pw.window:
                        pw.handle_event(event)
                        break
                continue
            if event.type == pygame.QUIT:
                running = False
            elif hasattr(pygame, "MOUSEWHEEL") and event.type == pygame.MOUSEWHEEL:
                if event.y > 0:
                    scroll_offset += 3 * event.y
                elif event.y < 0:
                    scroll_offset = max(0, scroll_offset + 3 * event.y)
            elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                mx, my = event.pos
                if show_hw_info_modal:
                    show_hw_info_modal = False
                    continue
                if show_reboot_modal:
                    if my < TOP_MENU_HEIGHT + 36:
                        _do_reboot()
                    show_reboot_modal = False
                    continue

                if show_settings_modal:
                    layout = _settings_modal_layout(screen_w, screen_h)

                    if settings_port_open:
                        opt_rects = _dropdown_option_rects(layout["port_field"], settings_ports_cache)
                        for i, r in enumerate(opt_rects):
                            if r.collidepoint(mx, my):
                                settings_port = settings_ports_cache[i]
                                break
                        settings_port_open = False
                        continue
                    if settings_baud_open:
                        opt_rects = _dropdown_option_rects(layout["baud_field"], BAUD_CHOICES)
                        for i, r in enumerate(opt_rects):
                            if r.collidepoint(mx, my):
                                settings_baud = BAUD_CHOICES[i]
                                break
                        settings_baud_open = False
                        continue

                    if layout["port_field"].collidepoint(mx, my):
                        settings_port_open = True
                        settings_baud_open = False
                    elif layout["baud_field"].collidepoint(mx, my):
                        settings_baud_open = True
                        settings_port_open = False
                    elif layout["rtscts_box"].collidepoint(mx, my):
                        settings_rtscts = not settings_rtscts
                    elif layout["test_btn"].collidepoint(mx, my):
                        _test_settings_connection(settings_port, settings_baud, settings_rtscts)
                    elif layout["apply_btn"].collidepoint(mx, my):
                        _apply_settings(settings_port, settings_baud, settings_rtscts)
                        show_settings_modal = False
                    elif layout["cancel_btn"].collidepoint(mx, my):
                        show_settings_modal = False
                    elif not layout["box"].collidepoint(mx, my):
                        show_settings_modal = False
                    continue

                clicked_menu_item = False
                if active_menu_idx is not None:
                    top_x = 8
                    for idx in range(active_menu_idx):
                        header_w = menu_font.size(f"  {MENU_DATA[idx]['title']}  ")[0]
                        top_x += header_w + 4

                    items = MENU_DATA[active_menu_idx]["items"]
                    max_item_w = max(menu_font.size(f" {it['label']} ")[0] for it in items) + 20
                    dd_h = len(items) * 26 + 8
                    dd_rect = pygame.Rect(top_x, TOP_MENU_HEIGHT, max_item_w, dd_h)

                    if dd_rect.collidepoint(mx, my):
                        item_idx = (my - TOP_MENU_HEIGHT - 4) // 26
                        if 0 <= item_idx < len(items):
                            it_act = items[item_idx]["action"]
                            if not (it_act == "SCAN_DRIVES" and getattr(link, "_system_state", "cpm") != "cpm"):
                                _trigger_action(it_act)
                                clicked_menu_item = True

                if not clicked_menu_item:
                    if my < TOP_MENU_HEIGHT:
                        top_x = 8
                        for idx, menu in enumerate(MENU_DATA):
                            header_w = menu_font.size(f"  {menu['title']}  ")[0]
                            if top_x <= mx <= top_x + header_w:
                                active_menu_idx = idx if active_menu_idx != idx else None
                                break
                            top_x += header_w + 4
                    else:
                        active_menu_idx = None
            elif event.type == pygame.MOUSEBUTTONDOWN:
                if event.button == 4:  # Wheel up
                    scroll_offset += 3
                elif event.button == 5:  # Wheel down
                    scroll_offset = max(0, scroll_offset - 3)
            elif event.type == pygame.KEYDOWN:
                if show_hw_info_modal:
                    show_hw_info_modal = False
                    continue

                if show_reboot_modal:
                    if event.key in (pygame.K_RETURN, pygame.K_KP_ENTER, pygame.K_y):
                        _do_reboot()
                    show_reboot_modal = False
                    continue

                if show_settings_modal:
                    if event.key == pygame.K_ESCAPE:
                        show_settings_modal = False
                    continue

                if prompt_mode is not None:
                    if event.key == pygame.K_ESCAPE:
                        prompt_mode = None
                        prompt_text = ""
                    elif event.key in (pygame.K_RETURN, pygame.K_KP_ENTER):
                        target_path = prompt_text.strip()
                        current_mode = prompt_mode
                        prompt_mode = None
                        prompt_text = ""
                        if target_path:
                            if current_mode == "SEND":
                                link.xmodem_send_async(target_path, callback=_on_xmodem_done)
                            elif current_mode == "RECEIVE":
                                link.xmodem_receive_async(target_path, callback=_on_xmodem_done)
                    elif event.key == pygame.K_BACKSPACE:
                        prompt_text = prompt_text[:-1]
                    elif event.unicode:
                        prompt_text += event.unicode
                    continue

                if event.key == pygame.K_F2:
                    _trigger_action("PROMPT_SEND")
                    continue
                elif event.key == pygame.K_F3:
                    _trigger_action("PROMPT_RECEIVE")
                    continue
                elif event.key == pygame.K_F4:
                    _trigger_action("CONFIRM_REBOOT")
                    continue
                elif event.key == pygame.K_F5:
                    _trigger_action("SHOW_HW_INFO")
                    continue
                elif event.key == pygame.K_F6:
                    if getattr(link, "_system_state", "cpm") == "cpm":
                        _trigger_action("SCAN_DRIVES")
                    else:
                        _show_toast("Scan error: System must be in CP/M / ZSDOS mode")
                    continue
                elif event.key == pygame.K_F7:
                    _trigger_action("TOGGLE_PIXEL_STREAM")
                    continue
                elif event.key == pygame.K_F8:
                    _trigger_action("SHOW_SETTINGS")
                    continue

                if event.mod & pygame.KMOD_SHIFT:
                    if event.key == pygame.K_PAGEUP:
                        scroll_offset += 10
                        continue
                    elif event.key == pygame.K_PAGEDOWN:
                        scroll_offset = max(0, scroll_offset - 10)
                        continue
                    elif event.key == pygame.K_UP:
                        scroll_offset += 1
                        continue
                    elif event.key == pygame.K_DOWN:
                        scroll_offset = max(0, scroll_offset - 1)
                        continue
                    elif event.key == pygame.K_END:
                        scroll_offset = 0
                        continue

                scroll_offset = 0
                data = _key_to_bytes(event)
                if not data:
                    continue

                # Mid-transfer, hold ordinary typing back - it can only muddy the
                # protocol stream, and pressing Enter during an agent's command
                # can even truncate what that agent reads. Interrupts still go
                # through, so cancelling a stuck transfer is always possible.
                if link.is_transferring() and not _allowed_during_transfer(data):
                    _show_toast("Transfer in progress - keys held back (Ctrl-X twice to cancel)", 2.5)
                    continue

                link.send_text(data.decode("latin-1"), append_enter=False)

        current_connection = (link.port, link.baud)
        if current_connection != last_seen_connection:
            last_seen_connection = current_connection
            _show_toast(f"Connected: {link.port} @ {link.baud} baud")

        # Spawn a new popup the moment a new render starts (render_id
        # change), independent of whether the main window's embedded view
        # is even showing pixel-stream mode right now. Then let every open
        # popup do its own polling/redraw, and drop ones the user closed.
        pixel_snap = link.get_pixel_frame()
        current_render_id = pixel_snap.get("render_id")
        if current_render_id is not None and current_render_id != last_seen_render_id:
            last_seen_render_id = current_render_id
            pixel_windows.append(PixelStreamWindow(current_render_id, font))
        for pw in pixel_windows:
            pw.update(link)
        still_open = []
        for pw in pixel_windows:
            if pw.closed:
                pw.destroy()
            else:
                still_open.append(pw)
        pixel_windows = still_open

        state = link.get_screen(scroll_offset=scroll_offset)
        scroll_offset = state.get("scroll_offset", 0)
        surface.fill(BG)

        # The banner arrives after startup, so the machine's identity only
        # becomes known once it has booted - refresh the caption when it changes.
        new_caption = window_title(link, title)
        if new_caption != caption:
            caption = new_caption
            pygame.display.set_caption(caption)

        # --------------------------------------------------------------
        # 1. Render Terminal / Pixel Stream Viewport
        # --------------------------------------------------------------
        if getattr(link, "_show_pixel_view", False):
            snap = link.get_pixel_frame()
            rgb_grid = snap.get("rgb_data", [])
            view_h = screen_h - TOP_MENU_HEIGHT - STATUS_BAR_HEIGHT
            if rgb_grid:
                pcell_w, pcell_h, p_cols, p_rows = _pixel_cell_size(rgb_grid, screen_w, view_h)
                if pcell_w > 0:
                    px_w = p_cols * pcell_w
                    px_h = p_rows * pcell_h
                    start_x = (screen_w - px_w) // 2
                    start_y = term_y + (view_h - px_h) // 2

                    pygame.draw.rect(surface, (10, 10, 15), (0, term_y, screen_w, view_h))
                    _draw_pixel_grid(surface, rgb_grid, (start_x, start_y), pcell_w, pcell_h)

                    tag_ver = snap.get("version") or 1
                    elapsed_s = snap.get("elapsed_s")
                    elapsed_str = f"{elapsed_s:.2f}s" if elapsed_s is not None else "in progress"
                    tag_text = f" PIXEL-STREAM v{tag_ver} | Frame {snap.get('frame_count')} | {p_cols}x{p_rows} | {elapsed_str} "
                    tag_img = font.render(tag_text, True, (255, 255, 255), (30, 90, 160))
                    surface.blit(tag_img, (start_x, max(term_y + 4, start_y - tag_img.get_height() - 2)))
            else:
                pygame.draw.rect(surface, (10, 10, 15), (0, term_y, screen_w, view_h))
                msg_img = font.render("Awaiting Mandel Pixel-Stream data...", True, (150, 180, 210))
                surface.blit(msg_img, ((screen_w - msg_img.get_width()) // 2, term_y + (view_h - msg_img.get_height()) // 2))
        else:
            runs = state.get("runs")
            if runs:
                for row, row_runs in enumerate(runs):
                    col_offset = 0
                    for run_data in row_runs:
                        text = run_data["text"]
                        if not text:
                            continue
                        fg_val = run_data.get("fg", "default")
                        bg_val = run_data.get("bg", "default")
                        bold = run_data.get("bold", False)
                        reverse = run_data.get("reverse", False)
                        underscore = run_data.get("underscore", False)

                        fg_rgb = _resolve_color(fg_val, FG, is_bold=bold)
                        bg_rgb = _resolve_color(bg_val, BG)

                        if reverse:
                            fg_rgb, bg_rgb = bg_rgb, fg_rgb

                        img = font.render(text, False, fg_rgb, bg_rgb)
                        x_pos = col_offset * cell_w
                        y_pos = term_y + row * cell_h
                        surface.blit(img, (x_pos, y_pos))

                        if underscore:
                            run_w = len(text) * cell_w
                            pygame.draw.line(surface, fg_rgb, (x_pos, y_pos + cell_h - 1), (x_pos + run_w, y_pos + cell_h - 1))

                        col_offset += len(text)
            else:
                for row, line in enumerate(state["lines"]):
                    if line.strip():
                        img = font.render(line, False, FG, BG)
                        surface.blit(img, (0, term_y + row * cell_h))

            if scroll_offset == 0 and (pygame.time.get_ticks() // CURSOR_BLINK_MS) % 2 == 0:
                cx, cy = state["cursor"]["x"], state["cursor"]["y"]
                rect = pygame.Rect(cx * cell_w, term_y + cy * cell_h, cell_w, cell_h)
                pygame.draw.rect(surface, CURSOR, rect, width=2)

        if scroll_offset > 0:
            badge_text = f" SCROLLBACK: -{scroll_offset} lines (Shift+End to exit) "
            badge_img = font.render(badge_text, True, (255, 255, 255), (140, 30, 30))
            badge_rect = badge_img.get_rect(topright=(screen_w - 5, term_y + 5))
            surface.blit(badge_img, badge_rect)

        # --------------------------------------------------------------
        # 2. Render Top Menu Header Bar
        # --------------------------------------------------------------
        top_bar_rect = pygame.Rect(0, 0, screen_w, TOP_MENU_HEIGHT)
        pygame.draw.rect(surface, (28, 33, 40), top_bar_rect)
        pygame.draw.line(surface, (55, 65, 80), (0, TOP_MENU_HEIGHT - 1), (screen_w, TOP_MENU_HEIGHT - 1), width=1)

        cur_x = 8
        mx, my = mouse_pos
        for idx, menu in enumerate(MENU_DATA):
            title_text = f"  {menu['title']}  "
            header_w, header_h = menu_font.size(title_text)
            is_hover = (cur_x <= mx <= cur_x + header_w and my < TOP_MENU_HEIGHT)
            is_active = (active_menu_idx == idx)

            if is_active or is_hover:
                pygame.draw.rect(surface, (45, 95, 165), (cur_x, 3, header_w, TOP_MENU_HEIGHT - 6))
                title_img = menu_font.render(title_text, True, (255, 255, 255))
            else:
                title_img = menu_font.render(title_text, True, (190, 205, 220))

            surface.blit(title_img, (cur_x, (TOP_MENU_HEIGHT - header_h) // 2))
            cur_x += header_w + 4

        # --------------------------------------------------------------
        # 3. Render Open Dropdown Menu Overlay
        # --------------------------------------------------------------
        if active_menu_idx is not None:
            top_x = 8
            for idx in range(active_menu_idx):
                header_w = menu_font.size(f"  {MENU_DATA[idx]['title']}  ")[0]
                top_x += header_w + 4

            items = MENU_DATA[active_menu_idx]["items"]
            max_item_w = max(menu_font.size(f" {it['label']} ")[0] for it in items) + 20
            dd_h = len(items) * 26 + 8
            dd_rect = pygame.Rect(top_x, TOP_MENU_HEIGHT, max_item_w, dd_h)

            pygame.draw.rect(surface, (24, 28, 34), dd_rect)
            pygame.draw.rect(surface, (60, 75, 95), dd_rect, width=1)

            for i_idx, item in enumerate(items):
                item_y = TOP_MENU_HEIGHT + 4 + i_idx * 26
                item_rect = pygame.Rect(top_x + 1, item_y, max_item_w - 2, 26)
                is_disabled = (item["action"] == "SCAN_DRIVES" and getattr(link, "_system_state", "cpm") != "cpm")
                item_hover = item_rect.collidepoint(mx, my) and not is_disabled

                if is_disabled:
                    lbl_img = menu_font.render(f" {item['label']}", True, (110, 115, 125))
                elif item_hover:
                    pygame.draw.rect(surface, (45, 95, 165), item_rect)
                    lbl_img = menu_font.render(f" {item['label']}", True, (255, 255, 255))
                else:
                    lbl_img = menu_font.render(f" {item['label']}", True, (200, 215, 230))
                surface.blit(lbl_img, (top_x + 6, item_y + 3))

        # --------------------------------------------------------------
        # 4. Render Interactive Path Prompt / Reboot Confirmation Banner
        # --------------------------------------------------------------
        if show_reboot_modal:
            prompt_h = 36
            prompt_rect = pygame.Rect(0, term_y, screen_w, prompt_h)
            pygame.draw.rect(surface, (180, 40, 30), prompt_rect)
            pygame.draw.line(surface, (255, 255, 255), (0, term_y + prompt_h - 1), (screen_w, term_y + prompt_h - 1), width=1)

            hdr_img = menu_font.render("REBOOT RC2014 HARDWARE?", True, (255, 255, 200))
            surface.blit(hdr_img, (12, term_y + (prompt_h - hdr_img.get_height()) // 2))

            hint_img = status_font.render("(Enter: Reboot Immediately | Esc: Cancel)", True, (255, 255, 255))
            surface.blit(hint_img, (screen_w - hint_img.get_width() - 12, term_y + (prompt_h - hint_img.get_height()) // 2))
        elif prompt_mode is not None:
            prompt_h = 36
            prompt_rect = pygame.Rect(0, term_y, screen_w, prompt_h)
            banner_bg = (180, 100, 15) if prompt_mode == "SEND" else (20, 100, 160)
            pygame.draw.rect(surface, banner_bg, prompt_rect)
            pygame.draw.line(surface, (255, 255, 255), (0, term_y + prompt_h - 1), (screen_w, term_y + prompt_h - 1), width=1)

            hdr_text = f"XMODEM {prompt_mode} PATH:"
            hdr_img = status_font.render(hdr_text, True, (255, 255, 200))
            surface.blit(hdr_img, (10, term_y + (prompt_h - hdr_img.get_height()) // 2))

            input_text = prompt_text + "_"
            input_img = font.render(input_text, True, (255, 255, 255))
            surface.blit(input_img, (10 + hdr_img.get_width() + 10, term_y + (prompt_h - input_img.get_height()) // 2))

            hint_img = status_font.render("(Enter: Start | Esc: Cancel)", True, (240, 240, 240))
            surface.blit(hint_img, (screen_w - hint_img.get_width() - 10, term_y + (prompt_h - hint_img.get_height()) // 2))

        # --------------------------------------------------------------
        # 5. Render Hardware & Disk Info Modal Overlay (if active)
        # --------------------------------------------------------------
        if show_hw_info_modal:
            hw_info = getattr(link, "hardware_info", {})
            box_w, box_h = min(720, screen_w - 20), min(500, screen_h - 40)
            box_x = (screen_w - box_w) // 2
            box_y = (screen_h - box_h) // 2
            modal_rect = pygame.Rect(box_x, box_y, box_w, box_h)

            pygame.draw.rect(surface, (20, 24, 30), modal_rect)
            pygame.draw.rect(surface, (60, 110, 180), modal_rect, width=2)

            # Header Bar
            hdr_rect = pygame.Rect(box_x, box_y, box_w, 32)
            pygame.draw.rect(surface, (30, 50, 80), hdr_rect)
            pygame.draw.line(surface, (60, 110, 180), (box_x, box_y + 31), (box_x + box_w, box_y + 31))

            hdr_img = menu_font.render(" RC2014 Hardware, OS & Disk Drive Inventory ", True, (255, 255, 255))
            surface.blit(hdr_img, (box_x + 8, box_y + 6))

            close_hint = status_font.render("[Esc/F5 to Close]", True, (180, 210, 240))
            surface.blit(close_hint, (box_x + box_w - close_hint.get_width() - 10, box_y + 6))

            # Hardware & ZSDOS specs, as captured from the boot banner. Shown as
            # "not captured" rather than a plausible-looking guess, so the panel
            # never claims specs for a machine it hasn't actually read.
            y_curr = box_y + 38
            not_captured = "not captured - reboot to capture"
            os_line = hw_info.get("zsdos_version") or ""
            if os_line and hw_info.get("tpa"):
                os_line = f"{os_line} ({hw_info['tpa']})"
            lines_to_show = [
                ("RomWBW Version:", hw_info.get("version") or not_captured),
                ("CPU Architecture:", hw_info.get("cpu") or not_captured),
                ("Memory / MMU:", hw_info.get("memory") or not_captured),
                ("ZSDOS / CBIOS:", os_line or not_captured),
            ]

            for label, val in lines_to_show:
                lbl_img = menu_font.render(f" {label:<18}", True, (140, 180, 220))
                val_img = menu_font.render(f"{val}", True, (255, 255, 255))
                surface.blit(lbl_img, (box_x + 12, y_curr))
                surface.blit(val_img, (box_x + 200, y_curr))
                y_curr += 24

            # Scanned Drive Inventory Table
            y_curr += 6
            dev_hdr = menu_font.render(" Cataloged Disk Drive Inventory (F6 to Scan):", True, (140, 180, 220))
            surface.blit(dev_hdr, (box_x + 12, y_curr))
            y_curr += 24

            drives = hw_info.get("drives", [])
            if not drives:
                drives_map = hw_info.get("drive_mappings", {})
                drives = [
                    {"drive": f"{k}:", "device": v, "files_count": "?", "purpose": "Unscanned (Press F6 to Catalog)"}
                    for k, v in list(drives_map.items())[:10]
                ]

            for drv in drives[:10]:
                d_name = drv.get("drive", "")
                d_dev = drv.get("device", "")
                d_count = drv.get("files_count", 0)
                d_free = drv.get("free_space", "")
                d_purp = drv.get("purpose", "")

                space_str = f", {d_free} free" if d_free and d_free != "?" else ""
                lbl_img = font.render(f"   * {d_name:<3} [{d_dev:<7}] ({d_count:>2} files{space_str})", True, (200, 230, 200))
                purp_img = font.render(f" -> {d_purp}", True, (255, 215, 120))
                surface.blit(lbl_img, (box_x + 12, y_curr))
                surface.blit(purp_img, (box_x + 12 + lbl_img.get_width() + 10, y_curr))
                y_curr += 22

        # --------------------------------------------------------------
        # 5b. Render Connection Settings Modal Overlay (if active)
        # --------------------------------------------------------------
        if show_settings_modal:
            layout = _settings_modal_layout(screen_w, screen_h)
            box = layout["box"]

            pygame.draw.rect(surface, (20, 24, 30), box)
            pygame.draw.rect(surface, (60, 110, 180), box, width=2)

            hdr_rect = pygame.Rect(box.x, box.y, box.w, 32)
            pygame.draw.rect(surface, (30, 50, 80), hdr_rect)
            pygame.draw.line(surface, (60, 110, 180), (box.x, box.y + 31), (box.x + box.w, box.y + 31))
            hdr_img = menu_font.render(" Connection Settings ", True, (255, 255, 255))
            surface.blit(hdr_img, (box.x + 8, box.y + 6))
            close_hint = status_font.render("[Esc to Close]", True, (180, 210, 240))
            surface.blit(close_hint, (box.x + box.w - close_hint.get_width() - 10, box.y + 6))

            def _settings_label(text, y):
                img = menu_font.render(text, True, (140, 180, 220))
                surface.blit(img, (box.x + 12, y + 4))

            for field_rect, value in (
                (layout["port_field"], settings_port),
                (layout["baud_field"], settings_baud),
            ):
                pygame.draw.rect(surface, (35, 40, 48), field_rect)
                pygame.draw.rect(surface, (80, 95, 115), field_rect, width=1)
                val_img = font.render(f" {value}", True, (255, 255, 255))
                surface.blit(val_img, (field_rect.x + 4, field_rect.y + 3))
                arrow_img = menu_font.render("v", True, (170, 185, 200))
                surface.blit(arrow_img, (field_rect.right - 20, field_rect.y + 3))

            _settings_label("Serial Port:", layout["port_field"].y)
            _settings_label("Baud Rate:", layout["baud_field"].y)
            _settings_label("Flow Control (RTS/CTS):", layout["rtscts_box"].y - 2)

            rtscts_box = layout["rtscts_box"]
            pygame.draw.rect(surface, (35, 40, 48), rtscts_box)
            pygame.draw.rect(surface, (80, 95, 115), rtscts_box, width=1)
            if settings_rtscts:
                pygame.draw.line(surface, (110, 255, 110), rtscts_box.topleft, rtscts_box.bottomright, width=2)
                pygame.draw.line(surface, (110, 255, 110), rtscts_box.topright, rtscts_box.bottomleft, width=2)

            status_msg, status_ok = settings_status
            if status_msg:
                if status_ok is True:
                    status_color = (140, 220, 140)
                elif status_ok is False:
                    status_color = (240, 120, 110)
                else:
                    status_color = (255, 200, 120)
                status_img = status_font.render(status_msg, True, status_color)
                surface.blit(status_img, (box.x + 12, layout["test_btn"].y - 22))

            for label, rect in (
                ("Test Connection", layout["test_btn"]),
                ("Apply & Reconnect", layout["apply_btn"]),
                ("Cancel", layout["cancel_btn"]),
            ):
                hover = rect.collidepoint(mx, my)
                pygame.draw.rect(surface, (45, 95, 165) if hover else (40, 46, 55), rect)
                pygame.draw.rect(surface, (80, 95, 115), rect, width=1)
                lbl_img = status_font.render(label, True, (255, 255, 255))
                surface.blit(lbl_img, (rect.x + (rect.w - lbl_img.get_width()) // 2,
                                        rect.y + (rect.h - lbl_img.get_height()) // 2))

            # Dropdown overlays drawn last so they sit above the buttons/fields.
            if settings_port_open:
                _draw_dropdown_list(surface, layout["port_field"], settings_ports_cache, settings_port, font)
            if settings_baud_open:
                _draw_dropdown_list(surface, layout["baud_field"], list(BAUD_CHOICES), str(settings_baud), font)

        # --------------------------------------------------------------
        # 6. Render Notification Toast (if active)
        # --------------------------------------------------------------
        if toast_text and time.time() < toast_expires:
            toast_img = status_font.render(f" {toast_text} ", True, (255, 255, 255), (20, 120, 60) if "Success" in toast_text or "Reboot" in toast_text else (160, 30, 30))
            toast_rect = toast_img.get_rect(topright=(screen_w - 10, term_y + 10))
            surface.blit(toast_img, toast_rect)

        # --------------------------------------------------------------
        # 6. Render Status Bar Panel
        # --------------------------------------------------------------
        status_y = term_y + term_h
        bar_rect = pygame.Rect(0, status_y, screen_w, STATUS_BAR_HEIGHT)
        pygame.draw.rect(surface, (22, 26, 32), bar_rect)
        pygame.draw.line(surface, (55, 65, 80), (0, status_y), (screen_w, status_y), width=1)

        # Left Info: Port & Baud
        port_name = state.get("port", "/dev/ttyUSB0")
        baud_rate = state.get("baud", 115200)
        conn_text = f"{port_name} @ {baud_rate} 8N1"
        conn_img = status_font.render(conn_text, True, (170, 185, 200))
        surface.blit(conn_img, (10, status_y + (STATUS_BAR_HEIGHT - conn_img.get_height()) // 2))

        # Mode & System Environment Badge
        mode = state.get("mode", "terminal").upper()
        sys_env = state.get("system_state", "unknown").upper()
        xp = state.get("xmodem_progress", {})
        if xp.get("active"):
            mode_label = f"XMODEM-{xp.get('direction', 'TRANSFER')}"
            badge_bg = (180, 110, 20)
            badge_fg = (255, 240, 200)
        elif mode == "XMODEM":
            mode_label = "XMODEM"
            badge_bg = (180, 110, 20)
            badge_fg = (255, 240, 200)
        elif mode in ("PIXEL_STREAM", "PIXEL-STREAM"):
            mode_label = "PIXEL-STREAM"
            badge_bg = (30, 110, 180)
            badge_fg = (210, 240, 255)
        elif sys_env == "CPM":
            mode_label = "CPM/ZSDOS"
            badge_bg = (24, 85, 45)
            badge_fg = (140, 255, 170)
        elif sys_env == "HBIOS":
            mode_label = "HBIOS"
            badge_bg = (30, 80, 150)
            badge_fg = (200, 230, 255)
        elif sys_env == "FLASH_UTIL":
            mode_label = "FLASH-UTIL"
            badge_bg = (130, 40, 120)
            badge_fg = (255, 210, 250)
        else:
            mode_label = "TERMINAL"
            badge_bg = (40, 50, 60)
            badge_fg = (180, 200, 220)

        def _blit_middle(img, x):
            surface.blit(img, (x, status_y + (STATUS_BAR_HEIGHT - img.get_height()) // 2))

        badge_txt_img = status_font.render(f" {mode_label} ", True, badge_fg, badge_bg)
        badge_x = 10 + conn_img.get_width() + 14
        _blit_middle(badge_txt_img, badge_x)
        left_edge = badge_x + badge_txt_img.get_width()

        # Active operation badge - so the human can see when the agent is driving
        op_label = operation_badge_label(mode_label, state.get("current_op") or "")
        if op_label:
            op_img = status_font.render(f" {op_label} ", True, (255, 245, 210), (120, 85, 20))
            _blit_middle(op_img, left_edge + 8)
            left_edge += 8 + op_img.get_width()

        # Right: RX / TX indicators. Drawn before the progress bar so the bar
        # knows how much room is genuinely free.
        rx_active = state.get("rx_active", False)
        tx_active = state.get("tx_active", False)

        rx_bg = (0, 180, 80) if rx_active else (32, 42, 36)
        rx_fg = (255, 255, 255) if rx_active else (80, 105, 90)
        rx_img = status_font.render(" RX ", True, rx_fg, rx_bg)

        tx_bg = (220, 130, 0) if tx_active else (45, 38, 28)
        tx_fg = (255, 255, 255) if tx_active else (110, 95, 75)
        tx_img = status_font.render(" TX ", True, tx_fg, tx_bg)

        rx_x = screen_w - rx_img.get_width() - 10
        tx_x = rx_x - tx_img.get_width() - 8
        _blit_middle(tx_img, tx_x)
        _blit_middle(rx_img, rx_x)
        right_edge = tx_x

        # XMODEM progress, fitted to the gap between the badges and TX/RX rather
        # than centred on the window - centring on the whole width made it
        # overlap the mode badge on a narrow window.
        if xp.get("active"):
            filename = xp.get("filename", "file")
            cur_b = xp.get("current_block", 0)
            tot_b = xp.get("total_blocks", 0)
            pct = (cur_b / tot_b * 100.0) if tot_b > 0 else 0.0
            prog_str = (f"{filename}: {int(pct)}% ({cur_b}/{tot_b})" if tot_b > 0
                        else f"{filename}: {cur_b} blks")
            prog_img = status_font.render(prog_str, True, (255, 255, 255))

            gap_start, gap_end = left_edge + 10, right_edge - 10
            available = gap_end - gap_start
            pbar_h = 20
            pbar_y = status_y + (STATUS_BAR_HEIGHT - pbar_h) // 2
            # Prefer the label's own width so the text always fits inside the bar.
            pbar_w = min(max(prog_img.get_width() + 16, 160), available)

            if pbar_w >= 60:
                pbar_x = gap_start + (available - pbar_w) // 2
                pygame.draw.rect(surface, (40, 48, 58), (pbar_x, pbar_y, pbar_w, pbar_h))
                if pct > 0:
                    pygame.draw.rect(surface, (0, 150, 220),
                                     (pbar_x, pbar_y, int(pbar_w * (pct / 100.0)), pbar_h))
                pygame.draw.rect(surface, (80, 95, 115), (pbar_x, pbar_y, pbar_w, pbar_h), width=1)

                if prog_img.get_width() <= pbar_w - 6:
                    surface.blit(prog_img, prog_img.get_rect(
                        center=(pbar_x + pbar_w // 2, status_y + STATUS_BAR_HEIGHT // 2)))

        pygame.display.flip()
        clock.tick(30)

    for pw in pixel_windows:
        pw.destroy()
    pygame.quit()




