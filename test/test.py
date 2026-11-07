# Tests for AESA Digital Brain v2 with a behavioral ADAR1000 SPI model.
# Signals are sampled on falling clock edges so the same tests work for RTL and gate-level runs.
import hashlib, os, pathlib
import cocotb
from cocotb.clock import Clock
from cocotb.triggers import FallingEdge, ClockCycles

GOLD_DIR = pathlib.Path(__file__).parent / "golden"
GOLD = GOLD_DIR / "burst_frames.sha256"
GOLD_LOAD = GOLD_DIR / "load_sequence.sha256"
DUMMY = 0x080AAA     # v2.1 dummy write: write-all, scratchpad 0x00A, data 0xAA


def check_golden(path, text):
    """Compare against a committed golden hash. A missing golden is a failure, never auto-created,
    unless GOLDEN_UPDATE=1 is set deliberately."""
    h = hashlib.sha256(text.encode()).hexdigest()
    if os.environ.get("GOLDEN_UPDATE") == "1":
        path.write_text(h + "\n")
    assert path.exists(), f"missing golden {path.name}: rerun with GOLDEN_UPDATE=1 only after review"
    assert path.read_text().strip() == h, f"golden mismatch for {path.name}"
SEL_DATA, SEL_HLO, SEL_HHI, SEL_BANK, SEL_PTR, SEL_CFG, SEL_LOAD = range(7)


def burst_frames(hdr, bank, stride):
    """Python reference: the 16 frames a burst must send."""
    out = []
    for k in range(16):
        addr = ((hdr & 0x1FFF) + (k & 3) * stride) & 0x1FFF
        out.append(((k >> 2) << 21) | (addr << 8) | bank[k])
    return out


class Adar1000Model:
    """Up to 4 ADAR1000s on one bus. Samples SDIO on SCLK rising edges (datasheet, write mode),
    drives SDO on falling edges during the data phase of a read."""
    def __init__(self, dut, pins, sample_edge="rise"):
        self.dut, self.pins, self.edge = dut, pins, sample_edge
        self.regs = [dict() for _ in range(4)]
        self.frames, self.violations = [], 0
        self.rx_loads = self.tx_loads = 0
        self.events = []          # (kind, value, sclk_rises_so_far)
        self.sclk_rises = 0
        self.overlap = 0          # cycles with RX_LOAD and TX_LOAD both high
        self.load_with_cs_low = 0 # cycles with a load pin high during an SPI frame
        cocotb.start_soon(self.run())

    async def run(self):
        ps = pm = 0; pc = 1; prx = ptx = 0; pdone = 0; bits = []; rd = None
        while True:
            await FallingEdge(self.dut.clk)
            v = int(self.dut.uo_out.value)
            s, m, c, rx, tx = v & 1, (v >> 1) & 1, (v >> 2) & 1, (v >> 5) & 1, (v >> 6) & 1
            done = (v >> 3) & 1
            self.rx_loads += rx and not prx; self.tx_loads += tx and not ptx
            if ps == 0 and s == 1: self.sclk_rises += 1
            if rx and tx: self.overlap += 1
            if (rx or tx) and c == 0: self.load_with_cs_low += 1
            for name, now, prev in (("RX+", rx and not prx, 0), ("RX-", prx and not rx, 0),
                                    ("TX+", tx and not ptx, 0), ("TX-", ptx and not tx, 0),
                                    ("DONE+", done and not pdone, 0)):
                if now: self.events.append((name, None, self.sclk_rises))
            if c == 0:
                rise, fall = (ps == 0 and s == 1), (ps == 1 and s == 0)
                sample = rise if self.edge == "rise" else fall
                if sample:
                    if self.edge == "rise" and m != pm:
                        self.violations += 1          # data changed on the sampling edge
                    bits.append(pm if self.edge == "rise" else m)
                    if len(bits) == 16 and bits[0] == 1:
                        hdr = int("".join(map(str, bits)), 2)
                        rd = self.regs[(hdr >> 13) & 3].get(hdr & 0x1FFF, 0)
                if fall and rd is not None and len(bits) >= 16:
                    n = len(bits) - 16
                    if n < 8:
                        self.pins.miso = (rd >> (7 - n)) & 1; self.pins.apply()
            if pc == 0 and c == 1:                    # CSB rising: frame complete
                if len(bits) == 24:
                    f = int("".join(map(str, bits)), 2); self.frames.append(f)
                    self.events.append(("FRAME", f, self.sclk_rises))
                    hdr, data = f >> 8, f & 0xFF
                    if not (hdr >> 15):
                        chips = range(4) if ((hdr >> 11) & 0xF) == 0b0001 else [(hdr >> 13) & 3]
                        addr = hdr & (0x7FF if ((hdr >> 11) & 0xF) == 0b0001 else 0x1FFF)
                        for ch in chips: self.regs[ch][addr] = data
                bits, rd = [], None; self.pins.miso = 0; self.pins.apply()
            ps, pm, pc, prx, ptx, pdone = s, m, c, rx, tx, done


class Pins:
    def __init__(self, dut): self.dut, self.ui, self.miso = dut, 0, 0
    def apply(self): self.dut.ui_in.value = (self.ui & 0xBF) | (self.miso << 6)


async def setup(dut, edge="rise"):
    cocotb.start_soon(Clock(dut.clk, 20, unit="ns").start())
    pins = Pins(dut); dut.ena.value = 1; dut.uio_in.value = 0; pins.apply()
    dut.rst_n.value = 0; await ClockCycles(dut.clk, 5); dut.rst_n.value = 1
    await ClockCycles(dut.clk, 5)
    return pins, Adar1000Model(dut, pins, edge)


async def pulse(dut, pins, bit, cycles=6):
    await FallingEdge(dut.clk); pins.ui |= (1 << bit); pins.apply()
    await ClockCycles(dut.clk, cycles); await FallingEdge(dut.clk)
    pins.ui &= ~(1 << bit); pins.apply(); await ClockCycles(dut.clk, cycles)


async def wr(dut, pins, sel, val):
    await FallingEdge(dut.clk)
    pins.ui = (pins.ui & ~0x1C) | (sel << 2); pins.apply(); dut.uio_in.value = val
    await pulse(dut, pins, 0)


async def wait_done(dut, limit=200000):
    for _ in range(limit):
        await FallingEdge(dut.clk)
        v = int(dut.uo_out.value)
        if (v >> 3) & 1 and not (v >> 4) & 1: return
    assert False, "timeout waiting for done"


async def single(dut, pins, hdr, data=0):
    await wr(dut, pins, SEL_HHI, hdr >> 8); await wr(dut, pins, SEL_HLO, hdr & 0xFF)
    await wr(dut, pins, SEL_DATA, data); await pulse(dut, pins, 1); await wait_done(dut)


@cocotb.test()
async def single_write_mode0(dut):
    pins, adar = await setup(dut)
    await single(dut, pins, 0x200A, 0x5A)                 # chip 1, scratchpad 0x00A (ADI forum example)
    assert adar.frames == [0x200A5A], [hex(f) for f in adar.frames]
    assert adar.regs[1][0x00A] == 0x5A and adar.violations == 0
    assert adar.rx_loads == 0 and adar.tx_loads == 0                 # no unintended load pulses


@cocotb.test()
async def write_all_broadcast(dut):
    pins, adar = await setup(dut)
    await single(dut, pins, 0x080A, 0x33)                 # header bits[14:11] = 0001
    assert all(adar.regs[c][0x00A] == 0x33 for c in range(4))


@cocotb.test()
async def readback(dut):
    pins, adar = await setup(dut)
    await single(dut, pins, 0x600A, 0xC3)                 # write chip 3
    await single(dut, pins, 0xE00A)                       # read chip 3 (bit 15 = 1)
    await FallingEdge(dut.clk); pins.ui |= 0x80; pins.apply()
    await ClockCycles(dut.clk, 4); await FallingEdge(dut.clk)
    assert int(dut.uio_oe.value) == 0xFF and int(dut.uio_out.value) == 0xC3


@cocotb.test()
async def burst_16_frames_golden(dut):
    pins, adar = await setup(dut)
    bank = [(0x11 * k + 7) & 0xFF for k in range(16)]
    hdr, stride = 0x0014, 2
    await wr(dut, pins, SEL_CFG, (stride - 1) | (0b01 << 2))   # stride 2, RX_LOAD after burst
    await wr(dut, pins, SEL_PTR, 0)
    for b in bank: await wr(dut, pins, SEL_BANK, b)
    await wr(dut, pins, SEL_HHI, hdr >> 8); await wr(dut, pins, SEL_HLO, hdr & 0xFF)
    await pulse(dut, pins, 5); await wait_done(dut)
    exp = burst_frames(hdr, bank, stride)
    assert adar.frames[:16] == exp, [hex(f) for f in adar.frames]
    assert adar.frames[16:] == [DUMMY], [hex(f) for f in adar.frames[16:]]   # validated separately
    for k in range(16):
        assert adar.regs[k >> 2][hdr + (k & 3) * stride] == bank[k]
    assert adar.rx_loads == 1 and adar.tx_loads == 0 and adar.violations == 0
    check_golden(GOLD, "\n".join(f"{f:06x}" for f in adar.frames[:16]))


@cocotb.test()
async def start_held_high_sends_once(dut):
    pins, adar = await setup(dut)
    await FallingEdge(dut.clk); pins.ui |= 1 << 5; pins.apply()
    await ClockCycles(dut.clk, 20000)                     # 400 us with start held high
    assert len(adar.frames) == 16, len(adar.frames)


@cocotb.test()
async def manual_tx_load_pulse(dut):
    pins, adar = await setup(dut)
    await wr(dut, pins, SEL_LOAD, 0b10); await wait_done(dut)
    assert adar.tx_loads == 1 and adar.rx_loads == 0


@cocotb.test()
async def fast_sclk_clk_div4(dut):
    pins, adar = await setup(dut)
    await wr(dut, pins, SEL_CFG, 1 << 5)                  # SCLK = clk/4 = 12.5 MHz
    await single(dut, pins, 0x0012, 0xA5)
    assert adar.frames == [0x0012A5] and adar.violations == 0


@cocotb.test()
async def cpha1_matches_v1_timing(dut):
    pins, adar = await setup(dut, edge="fall")            # receiver sampling on falling edges
    await wr(dut, pins, SEL_CFG, 1 << 4)
    await single(dut, pins, 0x0012, 0xA5)
    assert adar.frames == [0x0012A5], [hex(f) for f in adar.frames]


def seq(adar):
    """Event kinds/values without SCLK counts, for order checks."""
    return [(k, v) for k, v, _ in adar.events]


def clocks_between(adar, a, b):
    """SCLK rising edges between the first event a and the next event b after it."""
    ia = next(i for i, e in enumerate(adar.events) if e[0] == a)
    ib = next(i for i, e in enumerate(adar.events) if i > ia and e[0] == b)
    return adar.events[ib][2] - adar.events[ia][2]


async def manual_load(dut, pins, bits):
    await wr(dut, pins, SEL_LOAD, bits); await wait_done(dut)


@cocotb.test()
async def load_rx_only(dut):
    pins, adar = await setup(dut)
    await manual_load(dut, pins, 0b01)
    assert seq(adar) == [("FRAME", DUMMY), ("RX+", None), ("RX-", None), ("DONE+", None)], seq(adar)
    assert adar.events[1][2] >= 6 and adar.tx_loads == 0 and adar.load_with_cs_low == 0


@cocotb.test()
async def load_tx_only(dut):
    pins, adar = await setup(dut)
    await manual_load(dut, pins, 0b10)
    assert seq(adar) == [("FRAME", DUMMY), ("TX+", None), ("TX-", None), ("DONE+", None)], seq(adar)
    assert adar.events[1][2] >= 6 and adar.rx_loads == 0 and adar.load_with_cs_low == 0


@cocotb.test()
async def load_both_is_sequential(dut):
    pins, adar = await setup(dut)
    await manual_load(dut, pins, 0b11)
    assert seq(adar) == [("FRAME", DUMMY), ("RX+", None), ("RX-", None),
                         ("FRAME", DUMMY), ("TX+", None), ("TX-", None), ("DONE+", None)], seq(adar)
    assert adar.overlap == 0, "RX_LOAD and TX_LOAD were high together"
    assert clocks_between(adar, "RX-", "TX+") >= 8                   # separation between pulses
    assert adar.events[1][2] >= 6 and clocks_between(adar, "RX-", "TX+") >= 6   # clocks before each pulse
    assert adar.load_with_cs_low == 0


@cocotb.test()
async def auto_load_both_after_burst_golden(dut):
    pins, adar = await setup(dut)
    bank = [(0x11 * k + 7) & 0xFF for k in range(16)]
    hdr, stride = 0x0014, 2
    await wr(dut, pins, SEL_CFG, (stride - 1) | (0b11 << 2))       # stride 2, RX and TX load after burst
    await wr(dut, pins, SEL_PTR, 0)
    for b in bank: await wr(dut, pins, SEL_BANK, b)
    await wr(dut, pins, SEL_HHI, hdr >> 8); await wr(dut, pins, SEL_HLO, hdr & 0xFF)
    await pulse(dut, pins, 5); await wait_done(dut)
    assert adar.frames[:16] == burst_frames(hdr, bank, stride)
    check_golden(GOLD, "\n".join(f"{f:06x}" for f in adar.frames[:16]))   # same v2.0 burst baseline
    tail = seq(adar)[16:]
    assert tail == [("FRAME", DUMMY), ("RX+", None), ("RX-", None),
                    ("FRAME", DUMMY), ("TX+", None), ("TX-", None), ("DONE+", None)], tail
    assert adar.overlap == 0 and clocks_between(adar, "RX-", "TX+") >= 8
    for e in adar.events[16:]: dut._log.info(f"load sequence event {e[0]:6} {'' if e[1] is None else hex(e[1])} sclk={e[2]}")
    check_golden(GOLD_LOAD, "\n".join(f"{k}:{'' if v is None else f'{v:06x}'}:{n}" for k, v, n in adar.events[16:]))


@cocotb.test()
async def busy_done_handshake(dut):
    pins, adar = await setup(dut)
    await wr(dut, pins, SEL_LOAD, 0b11)
    saw_busy = False
    for _ in range(4000):                                           # watch the sequence run
        await FallingEdge(dut.clk)
        v = int(dut.uo_out.value)
        busy, done = (v >> 4) & 1, (v >> 3) & 1
        if busy: saw_busy = True; assert not done, "done high while busy"
        if saw_busy and not busy: break
    assert saw_busy and (int(dut.uo_out.value) >> 3) & 1
    kinds = [k for k, _, _ in adar.events]
    assert kinds.index("DONE+") > kinds.index("TX-"), "done before the final load pulse ended"


@cocotb.test()
async def commands_ignored_during_load_sequence(dut):
    pins, adar = await setup(dut)
    await wr(dut, pins, SEL_DATA, 0x11)
    await FallingEdge(dut.clk); pins.ui = (pins.ui & ~0x1C) | (SEL_LOAD << 2); pins.apply()
    dut.uio_in.value = 0b11; await pulse(dut, pins, 0)               # start RX+TX sequence
    await pulse(dut, pins, 5)                                        # try a burst while busy
    await wr(dut, pins, SEL_DATA, 0x99)                              # try a register write while busy
    await wait_done(dut)
    assert adar.frames == [DUMMY, DUMMY], [hex(f) for f in adar.frames]   # no burst started
    await pulse(dut, pins, 1); await wait_done(dut)                  # data byte must still be 0x11
    assert adar.frames[-1] & 0xFF == 0x11, hex(adar.frames[-1])
