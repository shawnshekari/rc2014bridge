import unittest
import pygame

from rc2014bridge.display import _key_to_bytes


class TestKeyEvents(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        pygame.init()

    @classmethod
    def tearDownClass(cls):
        pygame.quit()

    def test_shifted_and_colon_keys(self):
        # Shift+; -> ':'
        event_colon = pygame.event.Event(pygame.KEYDOWN, key=pygame.K_SEMICOLON, mod=pygame.KMOD_SHIFT, unicode=":")
        self.assertEqual(_key_to_bytes(event_colon), b":")

        # Shift+c -> 'C'
        event_cap_c = pygame.event.Event(pygame.KEYDOWN, key=pygame.K_c, mod=pygame.KMOD_SHIFT, unicode="C")
        self.assertEqual(_key_to_bytes(event_cap_c), b"C")

        # CapsLock 'C'
        event_caps_c = pygame.event.Event(pygame.KEYDOWN, key=pygame.K_c, mod=pygame.KMOD_CAPS, unicode="C")
        self.assertEqual(_key_to_bytes(event_caps_c), b"C")

    def test_special_keys(self):
        event_return = pygame.event.Event(pygame.KEYDOWN, key=pygame.K_RETURN, mod=0, unicode="\r")
        self.assertEqual(_key_to_bytes(event_return), b"\r")

        event_ctrl_c = pygame.event.Event(pygame.KEYDOWN, key=pygame.K_c, mod=pygame.KMOD_CTRL, unicode="\x03")
        self.assertEqual(_key_to_bytes(event_ctrl_c), b"\x03")


if __name__ == "__main__":
    unittest.main()
