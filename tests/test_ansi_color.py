import unittest
import pyte

from rc2014bridge.display import _resolve_color, ANSI_COLORS, FG, BG


class TestAnsiColor(unittest.TestCase):
    def test_color_resolution(self):
        # Default fallbacks
        self.assertEqual(_resolve_color("default", FG), FG)
        self.assertEqual(_resolve_color("default", BG), BG)

        # Standard colors
        self.assertEqual(_resolve_color("red", FG), ANSI_COLORS["red"])
        self.assertEqual(_resolve_color("green", FG), ANSI_COLORS["green"])

        # Bold upgrades standard colors to bright variants
        self.assertEqual(_resolve_color("red", FG, is_bold=True), ANSI_COLORS["brightred"])
        self.assertEqual(_resolve_color("green", FG, is_bold=True), ANSI_COLORS["brightgreen"])

        # Hex 256/truecolor parsing
        self.assertEqual(_resolve_color("ff0000", FG), (255, 0, 0))
        self.assertEqual(_resolve_color("00ff80", FG), (0, 255, 128))

    def test_screen_runs_extraction(self):
        screen = pyte.Screen(80, 24)
        stream = pyte.Stream(screen)
        stream.feed("\x1b[31mRedText \x1b[1;32mBrightGreen \x1b[7mReverse \x1b[4mUnderline\x1b[0m Plain")

        runs = []
        for r in range(24):
            row_runs = []
            current_run = None
            for c in range(80):
                char = screen.buffer[r][c]
                style = (char.fg, char.bg, char.bold, char.underscore, char.reverse)
                if current_run is None:
                    current_run = {
                        "text": char.data,
                        "fg": char.fg,
                        "bg": char.bg,
                        "bold": char.bold,
                        "underscore": char.underscore,
                        "reverse": char.reverse,
                    }
                elif (current_run["fg"], current_run["bg"], current_run["bold"], current_run["underscore"], current_run["reverse"]) == style:
                    current_run["text"] += char.data
                else:
                    row_runs.append(current_run)
                    current_run = {
                        "text": char.data,
                        "fg": char.fg,
                        "bg": char.bg,
                        "bold": char.bold,
                        "underscore": char.underscore,
                        "reverse": char.reverse,
                    }
            if current_run is not None:
                row_runs.append(current_run)
            runs.append(row_runs)

        row0 = runs[0]
        self.assertEqual(row0[0]["text"], "RedText ")
        self.assertEqual(row0[0]["fg"], "red")
        self.assertFalse(row0[0]["bold"])

        self.assertEqual(row0[1]["text"], "BrightGreen ")
        self.assertEqual(row0[1]["fg"], "green")
        self.assertTrue(row0[1]["bold"])

        self.assertEqual(row0[2]["text"], "Reverse ")
        self.assertTrue(row0[2]["reverse"])

        self.assertEqual(row0[3]["text"], "Underline")
        self.assertTrue(row0[3]["underscore"])


if __name__ == "__main__":
    unittest.main()
