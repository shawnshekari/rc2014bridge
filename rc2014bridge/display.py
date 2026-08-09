"""
Pygame front end: renders the SerialLink's pyte screen buffer and forwards
local keystrokes to the port. This is the "human" half of the bridge -
the model drives the same SerialLink concurrently through api.py.
"""

import pygame

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


STATUS_BAR_HEIGHT = 28


def run(link, title: str = "RC2014 Bridge"):
    pygame.init()
    pygame.display.set_caption(title)
    font = pygame.font.SysFont("monospace", FONT_SIZE)
    status_font = pygame.font.SysFont("monospace", 13, bold=True)
    cell_w, cell_h = font.size("M")
    screen_w = link.cols * cell_w
    term_h = link.rows * cell_h
    screen_h = term_h + STATUS_BAR_HEIGHT
    surface = pygame.display.set_mode((screen_w, screen_h))
    clock = pygame.time.Clock()

    scroll_offset = 0
    running = True
    while running:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            elif hasattr(pygame, "MOUSEWHEEL") and event.type == pygame.MOUSEWHEEL:
                if event.y > 0:
                    scroll_offset += 3 * event.y
                elif event.y < 0:
                    scroll_offset = max(0, scroll_offset + 3 * event.y)
            elif event.type == pygame.MOUSEBUTTONDOWN:
                if event.button == 4:  # Wheel up
                    scroll_offset += 3
                elif event.button == 5:  # Wheel down
                    scroll_offset = max(0, scroll_offset - 3)
            elif event.type == pygame.KEYDOWN:
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
                if data:
                    link.send_text(data.decode("latin-1"))

        state = link.get_screen(scroll_offset=scroll_offset)
        scroll_offset = state.get("scroll_offset", 0)
        surface.fill(BG)

        # --------------------------------------------------------------
        # 1. Render Terminal Viewport
        # --------------------------------------------------------------
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
                    y_pos = row * cell_h
                    surface.blit(img, (x_pos, y_pos))

                    if underscore:
                        run_w = len(text) * cell_w
                        pygame.draw.line(surface, fg_rgb, (x_pos, y_pos + cell_h - 1), (x_pos + run_w, y_pos + cell_h - 1))

                    col_offset += len(text)
        else:
            for row, line in enumerate(state["lines"]):
                if line.strip():
                    img = font.render(line, False, FG, BG)
                    surface.blit(img, (0, row * cell_h))

        if scroll_offset == 0 and (pygame.time.get_ticks() // CURSOR_BLINK_MS) % 2 == 0:
            cx, cy = state["cursor"]["x"], state["cursor"]["y"]
            rect = pygame.Rect(cx * cell_w, cy * cell_h, cell_w, cell_h)
            pygame.draw.rect(surface, CURSOR, rect, width=2)

        if scroll_offset > 0:
            badge_text = f" SCROLLBACK: -{scroll_offset} lines (Shift+End to exit) "
            badge_img = font.render(badge_text, True, (255, 255, 255), (140, 30, 30))
            badge_rect = badge_img.get_rect(topright=(screen_w - 5, 5))
            surface.blit(badge_img, badge_rect)

        # --------------------------------------------------------------
        # 2. Render Status Bar Panel
        # --------------------------------------------------------------
        bar_rect = pygame.Rect(0, term_h, screen_w, STATUS_BAR_HEIGHT)
        pygame.draw.rect(surface, (22, 26, 32), bar_rect)
        pygame.draw.line(surface, (55, 65, 80), (0, term_h), (screen_w, term_h), width=1)

        # Left Info: Port & Baud
        port_name = state.get("port", "/dev/ttyUSB0")
        baud_rate = state.get("baud", 115200)
        conn_text = f"{port_name} @ {baud_rate} 8N1"
        conn_img = status_font.render(conn_text, True, (170, 185, 200))
        surface.blit(conn_img, (8, term_h + 6))

        # Mode Badge
        mode = state.get("mode", "terminal").upper()
        xp = state.get("xmodem_progress", {})
        if xp.get("active"):
            mode_label = f"XMODEM-{xp.get('direction', 'TRANSFER')}"
            badge_bg = (180, 110, 20)
            badge_fg = (255, 240, 200)
        elif mode == "XMODEM":
            mode_label = "XMODEM"
            badge_bg = (180, 110, 20)
            badge_fg = (255, 240, 200)
        else:
            mode_label = "TERMINAL"
            badge_bg = (24, 85, 45)
            badge_fg = (140, 255, 170)

        badge_txt_img = status_font.render(f" {mode_label} ", True, badge_fg, badge_bg)
        badge_x = 8 + conn_img.get_width() + 12
        surface.blit(badge_txt_img, (badge_x, term_h + 5))

        # Middle: XMODEM Progress Bar (if active)
        if xp.get("active"):
            filename = xp.get("filename", "file")
            cur_b = xp.get("current_block", 0)
            tot_b = xp.get("total_blocks", 0)
            pct = (cur_b / tot_b * 100.0) if tot_b > 0 else 0.0

            pbar_w, pbar_h = 240, 16
            pbar_x = screen_w // 2 - pbar_w // 2
            pbar_y = term_h + 6

            pygame.draw.rect(surface, (40, 48, 58), (pbar_x, pbar_y, pbar_w, pbar_h))
            if pct > 0:
                fill_w = int(pbar_w * (pct / 100.0))
                pygame.draw.rect(surface, (0, 150, 220), (pbar_x, pbar_y, fill_w, pbar_h))
            pygame.draw.rect(surface, (80, 95, 115), (pbar_x, pbar_y, pbar_w, pbar_h), width=1)

            prog_str = f"{filename}: {int(pct)}% ({cur_b}/{tot_b})" if tot_b > 0 else f"{filename}: {cur_b} blks"
            prog_img = status_font.render(prog_str, True, (255, 255, 255))
            prog_rect = prog_img.get_rect(center=(screen_w // 2, term_h + 14))
            surface.blit(prog_img, prog_rect)

        # Right: RX / TX Indicators
        rx_active = state.get("rx_active", False)
        tx_active = state.get("tx_active", False)

        rx_bg = (0, 180, 80) if rx_active else (32, 42, 36)
        rx_fg = (255, 255, 255) if rx_active else (80, 105, 90)
        rx_img = status_font.render(" RX ", True, rx_fg, rx_bg)

        tx_bg = (220, 130, 0) if tx_active else (45, 38, 28)
        tx_fg = (255, 255, 255) if tx_active else (110, 95, 75)
        tx_img = status_font.render(" TX ", True, tx_fg, tx_bg)

        rx_x = screen_w - rx_img.get_width() - 8
        tx_x = rx_x - tx_img.get_width() - 6

        surface.blit(tx_img, (tx_x, term_h + 5))
        surface.blit(rx_img, (rx_x, term_h + 5))

        pygame.display.flip()
        clock.tick(30)

    pygame.quit()



