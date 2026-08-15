import unittest
from rc2014bridge.pixel_stream import PixelStreamDecoder, PALETTE, END_OF_ROW, END_OF_FRAME, VERSION_BANNER


class TestPixelStreamDecoder(unittest.TestCase):
    def setUp(self):
        self.decoder = PixelStreamDecoder()

    def test_single_pixel(self):
        # 0x00 (index 0, run 1), 0xF8 (END_OF_ROW)
        self.decoder.feed([0x00, END_OF_ROW])
        snap = self.decoder.get_current_frame_snapshot()
        self.assertEqual(snap["rows"], 1)
        self.assertEqual(snap["cols"], 1)
        self.assertEqual(snap["rgb_data"], [[PALETTE[0]]])

    def test_short_run(self):
        # 0x2A (index 5, run 3), 0xF8
        self.decoder.feed([0x2A, END_OF_ROW])
        snap = self.decoder.get_current_frame_snapshot()
        self.assertEqual(snap["rows"], 1)
        self.assertEqual(snap["cols"], 3)
        self.assertEqual(snap["rgb_data"], [[PALETTE[5]] * 3])

    def test_max_direct_run(self):
        # 0x1E (index 3, run 7), 0xF8
        self.decoder.feed([0x1E, END_OF_ROW])
        snap = self.decoder.get_current_frame_snapshot()
        self.assertEqual(snap["rows"], 1)
        self.assertEqual(snap["cols"], 7)
        self.assertEqual(snap["rgb_data"], [[PALETTE[3]] * 7])

    def test_extended_run(self):
        # 0x3F (index 7, code 7), 0x08 (run 8), 0xF8
        self.decoder.feed([0x3F, 0x08, END_OF_ROW])
        snap = self.decoder.get_current_frame_snapshot()
        self.assertEqual(snap["rows"], 1)
        self.assertEqual(snap["cols"], 8)
        self.assertEqual(snap["rgb_data"], [[PALETTE[7]] * 8])

    def test_mixed_row(self):
        # 0x03 (idx 0, 4 px), 0x09 (idx 1, 2 px), 0xF0 (idx 30, 1 px), 0xF8
        self.decoder.feed([0x03, 0x09, 0xF0, END_OF_ROW])
        snap = self.decoder.get_current_frame_snapshot()
        self.assertEqual(snap["rows"], 1)
        self.assertEqual(snap["cols"], 7)
        expected = [PALETTE[0]] * 4 + [PALETTE[1]] * 2 + [PALETTE[30]] * 1
        self.assertEqual(snap["rgb_data"], [expected])

    def test_two_rows_and_end_of_frame(self):
        # 0x02 (idx 0, 3 px), END_OF_ROW, 0xF0 (idx 30, 1 px), END_OF_ROW, END_OF_FRAME
        self.decoder.feed([0x02, END_OF_ROW, 0xF0, END_OF_ROW, END_OF_FRAME])
        snap = self.decoder.get_current_frame_snapshot()
        self.assertTrue(snap["is_complete"])
        self.assertEqual(snap["frame_count"], 1)
        self.assertEqual(snap["rows"], 2)
        self.assertEqual(snap["rgb_data"][0], [PALETTE[0]] * 3)
        self.assertEqual(snap["rgb_data"][1], [PALETTE[30]] * 1)

    def test_version_banner(self):
        self.decoder.feed([VERSION_BANNER, 0x01])
        self.assertEqual(self.decoder.version, 1)


if __name__ == "__main__":
    unittest.main()
