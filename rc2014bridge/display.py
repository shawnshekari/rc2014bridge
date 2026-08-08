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
