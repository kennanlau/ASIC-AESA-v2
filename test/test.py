# Tests for AESA Digital Brain v2 with a behavioral ADAR1000 SPI model.
# Signals are sampled on falling clock edges so the same tests work for RTL and gate-level runs.
import hashlib, pathlib
import cocotb
from cocotb.clock import Clock
from cocotb.triggers import FallingEdge, ClockCycles

GOLD = pathlib.Path(__file__).parent / "golden" / "burst_frames.sha256"
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
        cocotb.start_soon(self.run())

    async def run(self):
        ps = pm = 0; pc = 1; prx = ptx = 0; bits = []; rd = None
        while True:
            await FallingEdge(self.dut.clk)
            v = int(self.dut.uo_out.value)
            s, m, c, rx, tx = v & 1, (v >> 1) & 1, (v >> 2) & 1, (v >> 5) & 1, (v >> 6) & 1
            self.rx_loads += rx and not prx; self.tx_loads += tx and not ptx
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
                    hdr, data = f >> 8, f & 0xFF
                    if not (hdr >> 15):
                        chips = range(4) if ((hdr >> 11) & 0xF) == 0b0001 else [(hdr >> 13) & 3]
                        addr = hdr & (0x7FF if ((hdr >> 11) & 0xF) == 0b0001 else 0x1FFF)
                        for ch in chips: self.regs[ch][addr] = data
                bits, rd = [], None; self.pins.miso = 0; self.pins.apply()
            ps, pm, pc, prx, ptx = s, m, c, rx, tx


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
    assert adar.frames == exp, [hex(f) for f in adar.frames]
    for k in range(16):
        assert adar.regs[k >> 2][hdr + (k & 3) * stride] == bank[k]
    assert adar.rx_loads == 1 and adar.tx_loads == 0 and adar.violations == 0
    h = hashlib.sha256("\n".join(f"{f:06x}" for f in adar.frames).encode()).hexdigest()
    if not GOLD.exists(): GOLD.write_text(h + "\n")
    assert GOLD.read_text().strip() == h


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
