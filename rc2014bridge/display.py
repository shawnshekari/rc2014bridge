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


def run(link, title: str = "RC2014 Bridge"):
    pygame.init()
    pygame.display.set_caption(title)
    font = pygame.font.SysFont("monospace", FONT_SIZE)
    cell_w, cell_h = font.size("M")
    screen_w, screen_h = link.cols * cell_w, link.rows * cell_h
    surface = pygame.display.set_mode((screen_w, screen_h))
    clock = pygame.time.Clock()

    running = True
    while running:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            elif event.type == pygame.KEYDOWN:
                data = _key_to_bytes(event)
                if data:
                    link.send_text(data.decode("latin-1"))

        state = link.get_screen()
        surface.fill(BG)
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

        if (pygame.time.get_ticks() // CURSOR_BLINK_MS) % 2 == 0:
            cx, cy = state["cursor"]["x"], state["cursor"]["y"]
            rect = pygame.Rect(cx * cell_w, cy * cell_h, cell_w, cell_h)
            pygame.draw.rect(surface, CURSOR, rect, width=2)

        pygame.display.flip()
        clock.tick(30)

    pygame.quit()

