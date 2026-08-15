#!/usr/bin/env python3
"""Reference encoder for the mandel-pixel-stream protocol (see
mandel_pixel_stream.yaml). Used to generate/verify that YAML file's
test_vectors section - both the Z80 encoder and the bridge's Python
decoder must agree with these exact bytes.

Run with no arguments to print the current test vectors' encoded bytes,
for comparison against what's checked into the YAML.
"""

END_OF_ROW = 0xF8
END_OF_FRAME = 0xF9
VERSION_BANNER = 0xFA


def encode_row(values: list[int]) -> list[int]:
    """RLE-encode one row's worth of raw iteration-count values (0-30)
    into mandel-pixel-stream tokens, including the trailing END_OF_ROW
    control token."""
    out = []
    i = 0
    while i < len(values):
        j = i
        while j < len(values) and values[j] == values[i]:
            j += 1
        run = j - i
        idx = values[i]
        if not (0 <= idx <= 30):
            raise ValueError(f"index {idx} out of range 0-30")
        if run <= 7:
            out.append((idx << 3) | (run - 1))
        else:
            if run > 255:
                raise ValueError(f"run length {run} exceeds 255 (extension byte is 8 bits)")
            out.append((idx << 3) | 7)
            out.append(run)
        i = j
    out.append(END_OF_ROW)
    return out


def encode_frame(rows: list[list[int]]) -> list[int]:
    """Encode a full render: one or more rows, each terminated by
    END_OF_ROW, followed by a single END_OF_FRAME."""
    out = []
    for row in rows:
        out.extend(encode_row(row))
    out.append(END_OF_FRAME)
    return out


def hexstr(bs: list[int]) -> str:
    return " ".join(f"{b:02X}" for b in bs)


if __name__ == "__main__":
    vectors = [
        ("single_pixel", [0]),
        ("short_run", [5, 5, 5]),
        ("max_direct_run", [3, 3, 3, 3, 3, 3, 3]),
        ("extended_run", [7, 7, 7, 7, 7, 7, 7, 7]),
        ("mixed_row", [0, 0, 0, 0, 1, 1, 30]),
    ]
    for name, vals in vectors:
        print(f"{name} -> {hexstr(encode_row(vals))}")

    print(f"two_rows_and_end_of_frame -> {hexstr(encode_frame([[0, 0, 0], [30]]))}")
    print(f"version_banner_prefix -> {hexstr([VERSION_BANNER, 1])}")
