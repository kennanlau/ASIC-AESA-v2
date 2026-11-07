## How it works

AESA Digital Brain v2 is an SPI frame engine for up to four ADAR1000 beamformers on one bus.

Each SPI frame is 24 bits, MSB first: a read/write bit, the 2-bit chip address (AD1, AD0, matching each
chip's ADDR1/ADDR0 pins), a 13-bit register address, and an 8-bit data byte. Header bits [14:11] = 0001
writes all four chips at once. SPI runs in mode 0 by default: SDIO is valid before every SCLK rising edge,
which is where the ADAR1000 samples it in write mode. SCLK is clk/10 (5 MHz at 50 MHz) by default.

Two kinds of transfer:

- **Single frame**: sends {header, data byte}. With header bit 15 set it is a read, and the returned byte
  (from the chip's SDO) can be read back on the uio pins.
- **Burst**: sends 16 write frames from a 16-byte bank. Frame k goes to chip k[3:2], register
  header[12:0] + k[1:0] x stride, with data bank[k]. One burst can set one register type (for example the
  phase I or Q value) for all 16 channels of four chips. An RX_LOAD and/or TX_LOAD pulse can follow
  automatically, so all chips apply the new values at the same moment.

### Load sequencing (v2.1)

The ADAR1000 datasheet requires at least 6 SCLK cycles before a beam-memory load, at least 8 SCLK cycles
between load pulses, and RX_LOAD and TX_LOAD never tied together. Every load request therefore runs as a
sequence: a dummy write-all to the scratchpad register (frame 0x080AAA, 24 SCLK cycles), then the pulse.
A combined request becomes dummy, RX_LOAD, dummy, TX_LOAD, so the two pins are never high together and are
separated by 24 SCLK cycles. Both the manual load command and the automatic load after a burst use this
sequence. `busy` stays high and new commands are ignored until the last pulse has ended; then `done` is set.

Inputs pass through two-flop synchronizers and the control pins act on their rising edge only, so holding a
pin high does not repeat the action. `done` stays high until the next operation starts.

### Registers (write: put the value on uio, select it on ui[4:2], pulse ui[0])

| sel | Register |
|---|---|
| 0 | Data byte for single frames |
| 1 | Header low byte (A7..A0) |
| 2 | Header high byte (R/W, AD1, AD0, A12..A8) |
| 3 | Bank write at the pointer, pointer then increments |
| 4 | Bank pointer (uio[3:0]) |
| 5 | Config: [1:0] stride-1, [2] RX_LOAD after burst, [3] TX_LOAD after burst, [4] CPHA (1 = v1 timing), [6:5] SCLK: 0 clk/10, 1 clk/4, 2 clk/50, 3 clk/100 |
| 6 | Load pulse now: uio[0] RX_LOAD, uio[1] TX_LOAD |

## How to test

1. Clock at 50 MHz, hold rst_n low, then release.
2. Single write to chip 1 register 0x00A (scratchpad): write 0x20 to sel 2, 0x0A to sel 1, the data to sel 0, then pulse ui[1].
3. Read it back: header 0xA00A, pulse ui[1], wait for done, then set ui[7] and read uio[7:0].
4. Burst: set the config, write 0 to sel 4, write 16 bytes to sel 3, write the base address to sel 2 and 1, pulse ui[5].
5. Wait for uo[3] (done) with uo[4] (busy) low.

The cocotb tests include a behavioral ADAR1000 model that checks every frame, the register contents of
four chips, write-all mode, readback, the load pulses, and that SDIO never changes on a sampling edge.
It also checks the v2.1 load sequencing: RX-only, TX-only and combined requests, at least 6 SCLK cycles
before each pulse and at least 8 between pulses, no overlap, no load pulse during an SPI frame, the
busy/done handshake, and that commands are ignored while a sequence runs.
The burst frames (unchanged from v2.0) and the load sequence are locked to SHA-256 golden hashes in
test/golden/. A missing golden file fails the test; it is only written with GOLDEN_UPDATE=1.

## External hardware

ADAR1000 (or the ADAR1000-EVALZ board). The ADAR1000 uses 1.8 V logic, so level shifting is needed.
Readback needs the chip's SDO enabled. Check against the datasheet before connecting: minimum CSB
high time, load pulse width, and the I/Q register addresses for phase.

Multi-chip readback: the ADAR1000 has a silicon erratum where chips that are not being read do not
tristate SDO. The host must enable SDO only on the chip being read and disable it afterwards (ADI's
procedure, with a special case for chip 0), and the board should have a series resistor between each
chip's SDO and the shared line. The cocotb model assumes an ideal bus and does not model this contention.
