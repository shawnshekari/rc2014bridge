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


TOP_MENU_HEIGHT = 32
STATUS_BAR_HEIGHT = 32

MENU_DATA = [
    {
        "title": "File",
        "items": [
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
            {"label": "Reset Scrollback (Shift+End)", "action": "RESET_SCROLLBACK"},
        ],
    },
]


def run(link, title: str = "RC2014 Bridge"):
    pygame.init()
    pygame.display.set_caption(title)
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
    active_menu_idx = None
    toast_text = None
    toast_expires = 0.0
    running = True

    def _trigger_action(action: str):
        nonlocal prompt_mode, prompt_text, scroll_offset, running, active_menu_idx
        active_menu_idx = None
        if action == "PROMPT_SEND":
            prompt_mode = "SEND"
            prompt_text = ""
        elif action == "PROMPT_RECEIVE":
            prompt_mode = "RECEIVE"
            prompt_text = ""
        elif action == "RESET_SCROLLBACK":
            scroll_offset = 0
        elif action == "QUIT":
            running = False

    def _on_xmodem_done(res: dict):
        nonlocal toast_text, toast_expires
        import time
        if res.get("ok"):
            count = res.get("bytes", res.get("blocks", 0))
            unit = "bytes" if "bytes" in res else "blocks"
            toast_text = f"XMODEM Success: Transferred {count} {unit}"
        else:
            toast_text = f"XMODEM Error: {res.get('error', 'Failed')}"
        toast_expires = time.time() + 4.0

    while running:
        import time
        mouse_pos = pygame.mouse.get_pos()

        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            elif hasattr(pygame, "MOUSEWHEEL") and event.type == pygame.MOUSEWHEEL:
                if event.y > 0:
                    scroll_offset += 3 * event.y
                elif event.y < 0:
                    scroll_offset = max(0, scroll_offset + 3 * event.y)
            elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                mx, my = event.pos
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
                            _trigger_action(items[item_idx]["action"])
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
                item_hover = item_rect.collidepoint(mx, my)

                if item_hover:
                    pygame.draw.rect(surface, (45, 95, 165), item_rect)
                    lbl_img = menu_font.render(f" {item['label']}", True, (255, 255, 255))
                else:
                    lbl_img = menu_font.render(f" {item['label']}", True, (200, 215, 230))
                surface.blit(lbl_img, (top_x + 6, item_y + 3))

        # --------------------------------------------------------------
        # 4. Render Interactive Path Prompt Banner (if active)
        # --------------------------------------------------------------
        if prompt_mode is not None:
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
        # 5. Render Notification Toast (if active)
        # --------------------------------------------------------------
        if toast_text and time.time() < toast_expires:
            toast_img = status_font.render(f" {toast_text} ", True, (255, 255, 255), (20, 120, 60) if "Success" in toast_text else (160, 30, 30))
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
        badge_x = 10 + conn_img.get_width() + 14
        surface.blit(badge_txt_img, (badge_x, status_y + (STATUS_BAR_HEIGHT - badge_txt_img.get_height()) // 2))

        # Middle: XMODEM Progress Bar (if active)
        if xp.get("active"):
            filename = xp.get("filename", "file")
            cur_b = xp.get("current_block", 0)
            tot_b = xp.get("total_blocks", 0)
            pct = (cur_b / tot_b * 100.0) if tot_b > 0 else 0.0

            pbar_w, pbar_h = 260, 20
            pbar_x = screen_w // 2 - pbar_w // 2
            pbar_y = status_y + (STATUS_BAR_HEIGHT - pbar_h) // 2

            pygame.draw.rect(surface, (40, 48, 58), (pbar_x, pbar_y, pbar_w, pbar_h))
            if pct > 0:
                fill_w = int(pbar_w * (pct / 100.0))
                pygame.draw.rect(surface, (0, 150, 220), (pbar_x, pbar_y, fill_w, pbar_h))
            pygame.draw.rect(surface, (80, 95, 115), (pbar_x, pbar_y, pbar_w, pbar_h), width=1)

            prog_str = f"{filename}: {int(pct)}% ({cur_b}/{tot_b})" if tot_b > 0 else f"{filename}: {cur_b} blks"
            prog_img = status_font.render(prog_str, True, (255, 255, 255))
            prog_rect = prog_img.get_rect(center=(screen_w // 2, status_y + STATUS_BAR_HEIGHT // 2))
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

        rx_x = screen_w - rx_img.get_width() - 10
        tx_x = rx_x - tx_img.get_width() - 8

        surface.blit(tx_img, (tx_x, status_y + (STATUS_BAR_HEIGHT - tx_img.get_height()) // 2))
        surface.blit(rx_img, (rx_x, status_y + (STATUS_BAR_HEIGHT - rx_img.get_height()) // 2))

        pygame.display.flip()
        clock.tick(30)

    pygame.quit()




