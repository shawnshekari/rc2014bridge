---
name: rc2014bridge_api
description: Instructions and CLI/Socket API reference for controlling and interacting with an RC2014 vintage computer via the rc2014bridge daemon.
---

# RC2014 Bridge Agent Skill & API Reference

Use this skill when interacting with, controlling, or querying an RC2014 Z80 vintage computer running RomWBW, HBIOS, ZSDOS, or CP/M via the `rc2014bridge` daemon.

## Transport & Protocol

`rc2014bridge` exposes:
1. **Model Context Protocol (MCP) HTTP / SSE Endpoint**: `http://<your-host-ip>:8014/sse` (bound to `0.0.0.0:8014` by default for local network LLM agents).
2. **Unix Domain Socket**: `/tmp/rc2014bridge.sock` (newline-delimited JSON protocol).
3. **CLI Client Helper**: `client.py` in workspace root.

---

## MCP Server Resources & Prompts

### MCP Resources
- `rc2014://docs/hardware-overview` : RC2014 Z80 hardware, MMU memory map, RomWBW BIOS, and serial pacing rules.
- `rc2014://docs/cpm-guide` : Quick reference for CP/M 2.2 / ZSDOS syntax and utility programs.
- `rc2014://system/hardware-info` : Live JSON snapshot of connected hardware specs and disk inventory.

### MCP Prompts
- `rc2014_assistant_instructions` : System prompt template for LLM retro-computing assistants.

---

## Command Reference

### 1. Send Text / Commands (`send_text`)
Transmits a text string to the RC2014 serial terminal. Multi-character strings are automatically paced with 1-byte chunking and a `15ms` inter-character delay to prevent Z80 UART buffer overruns.

* **CLI Usage**:
  ```bash
  python client.py send_text 'DIR A:\r'
  ```
* **Socket JSON Request**:
  ```json
  { "cmd": "send_text", "text": "DIR A:\r" }
  ```

---

### 2. Get Screen & Terminal State (`get_screen`)
Retrieves rendered terminal screen lines (80x48 grid), cursor position, baud rate, hardware port, system state (`cpm`, `hbios`, `flash_util`), and active XMODEM progress.

* **Parameters**:
  - `scroll_offset` (int, default `0`): Shift viewport up into scrollback history.
  - `max_lines` (int, default `None`): Max lines to retrieve. Use `0` to fetch **all** scrollback history lines.
* **CLI Usage**:
  ```bash
  # Get current viewport lines
  python client.py get_screen

  # Get all scrollback history lines
  python client.py get_screen --history

  # Get last 100 lines
  python client.py get_screen --max-lines 100
  ```
* **Socket JSON Request**:
  ```json
  { "cmd": "get_screen", "max_lines": 0 }
  ```

---

### 3. Wait for Screen Pattern (`wait_for`)
Blocks execution until a regex pattern (e.g. `A>`, `HBIOS>`, `Boot:`) appears on the terminal screen within a specified timeout.

* **CLI Usage**:
  ```bash
  python client.py wait_for 'A>' --timeout 10.0
  ```
* **Socket JSON Request**:
  ```json
  { "cmd": "wait_for", "pattern": "A>", "timeout": 10.0 }
  ```

---

### 4. Scan Mapped Drives & Disk Purpose (`scan_drives`)
Executes `STAT` to query free space and read/write access mode, scans file catalogs across CP/M drives `A:` through `J:`, and classifies each drive's purpose.

* **CLI Usage**:
  ```bash
  python client.py scan_drives
  ```
* **Socket JSON Request**:
  ```json
  { "cmd": "scan_drives" }
  ```

---

### 5. Hardware Info & Diagnostic Capture (`get_hardware_info`)
Returns persisted RomWBW version, CPU architecture, memory/MMU configuration, wait states, SIO ports, ZSDOS version, CBIOS version, TPA size, drive mappings, and cataloged disk table.

* **CLI Usage**:
  ```bash
  python client.py get_hardware_info
  ```
* **Socket JSON Request**:
  ```json
  { "cmd": "get_hardware_info" }
  ```

---

### 6. XMODEM File Transfer (`xmodem_send`, `xmodem_receive`)
Sends or receives binary files to/from CP/M using the XMODEM CRC protocol.

* **CLI Usage**:
  ```bash
  # Send local file to RC2014
  python client.py xmodem_send /path/to/local/MYPROG.COM

  # Receive file from RC2014
  python client.py xmodem_receive /path/to/save/OUTPUT.BIN
  ```

---

### 7. Reboot RC2014 Hardware (`reboot`)
Issues system-appropriate reboot triggers (`C:REBOOT /C` in CP/M mode, `R` in HBIOS mode) and refreshes hardware parameters in `hardware_info.json`.

* **CLI Usage**:
  ```bash
  python client.py reboot
  ```

---

## Python Socket Client Example

```python
import json
import socket

def call_bridge(req: dict) -> dict:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.connect("/tmp/rc2014bridge.sock")
        s.sendall((json.dumps(req) + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
        return json.loads(buf.decode())

# Example: Check RC2014 hardware info
hw = call_bridge({"cmd": "get_hardware_info"})
print("RomWBW Version:", hw.get("info", {}).get("version"))
```
