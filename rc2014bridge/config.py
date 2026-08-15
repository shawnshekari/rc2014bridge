"""INI config file support.

Lets the CLI flags in app.py be set once in a file instead of retyped on
every launch. An explicit CLI flag always wins over the config file, which
in turn wins over the built-in default - see _apply_config_defaults() in
app.py. Section names exist purely for human readability; load() flattens
all of them into one dict keyed by option name, so a key can live under any
section.
"""

from __future__ import annotations

import configparser
import logging
import os

logger = logging.getLogger("rc2014bridge")

DEFAULT_CONFIG_PATH = "rc2014bridge.ini"


def load(path: str) -> dict:
    """Flatten every section of the INI file at `path` into a single str ->
    str dict. A missing file is not an error - it just means the built-in
    defaults (or CLI flags) apply."""
    if not path or not os.path.isfile(path):
        return {}
    parser = configparser.ConfigParser()
    try:
        parser.read(path)
    except configparser.Error as e:
        logger.warning("Failed to parse config file %s: %s", path, e)
        return {}
    flat: dict[str, str] = {}
    for section in parser.sections():
        flat.update(parser.items(section))
    return flat


def update_value(path: str, section: str, key: str, value: str) -> None:
    """Set section/key = value in the INI file at `path`, creating the
    file/section/key as needed, while leaving every other line - including
    comments - untouched. Used for the settings the GUI can change live
    (port/baud/rtscts) so a restart remembers the last value picked; a full
    configparser read-modify-write round-trip would silently drop comments
    and reorder the whole file, which is worse for a file meant to be
    hand-edited.
    """
    lines = []
    if os.path.isfile(path):
        with open(path) as f:
            lines = f.readlines()

    section_header = f"[{section}]"
    sec_start = None
    sec_end = len(lines)
    for i, line in enumerate(lines):
        if line.strip() == section_header:
            sec_start = i
            sec_end = len(lines)
            for j in range(i + 1, len(lines)):
                stripped = lines[j].strip()
                if stripped.startswith("[") and stripped.endswith("]"):
                    sec_end = j
                    break
            break

    new_line = f"{key} = {value}\n"

    if sec_start is None:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        if lines:
            lines.append("\n")
        lines.append(section_header + "\n")
        lines.append(new_line)
    else:
        key_line = None
        for i in range(sec_start + 1, sec_end):
            stripped = lines[i].strip()
            if not stripped or stripped.startswith("#") or stripped.startswith(";"):
                continue
            existing_key = stripped.split("=", 1)[0].strip()
            if existing_key == key:
                key_line = i
                break
        if key_line is not None:
            lines[key_line] = new_line
        else:
            lines.insert(sec_end, new_line)

    with open(path, "w") as f:
        f.writelines(lines)
