# Workspace Behavioral Rules & Technical Invariants

## Hardware Communication & Serial Links
- **Vintage Hardware Serial Transmission Pacing**: When transmitting multi-character command strings over serial to 8-bit retro processors (e.g. Z80 @ 7.372 MHz) running at high baud rates (e.g. 115,200 baud), always pace character transmission using 1-byte chunking with inter-character delays (`~15ms`). Unpaced burst writes will cause UART buffer overruns while the target processor handles video/display interrupts.

## Terminal Parsing & History Management
- **Terminal History Isolation & Command Response Parsing**: When parsing command responses from terminal screen buffers that maintain scrollback history, extract exact text blocks bounded by the command prompt header (`A>DIR <drive>:`) and the trailing prompt (`A>`). Avoid matching regex against full screen views without isolating the specific command block.

## Graphical User Interface & Layout Rendering
- **Dynamic UI Element Offset Math**: Never hardcode static X pixel offsets for adjacent UI text labels with variable lengths (e.g. file counts, capacity strings). Calculate positions dynamically using rendered element widths: `x = box_x + margin + primary_label.get_width() + padding`.
