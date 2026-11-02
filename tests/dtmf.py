"""Detect sustained DTMF pairs in a mono, 16-bit PCM WAV recording."""

import math
import struct
import wave
from dataclasses import dataclass
from pathlib import Path

ROWS = (697, 770, 852, 941)
COLS = (1209, 1336, 1477, 1633)
KEYS = ("123A", "456B", "789C", "*0#D")


@dataclass(frozen=True)
class Tone:
    digit: str
    start: float
    end: float


def _power(samples: list[float], frequency: int, rate: int) -> float:
    coefficient = 2 * math.cos(2 * math.pi * frequency / rate)
    previous = older = 0.0
    for sample in samples:
        current = sample + coefficient * previous - older
        older, previous = previous, current
    return previous**2 + older**2 - coefficient * previous * older


def detect_tones(path: Path) -> list[Tone]:
    with wave.open(str(path), "rb") as wav:
        if wav.getnchannels() != 1 or wav.getsampwidth() != 2:
            raise ValueError("Expected mono, 16-bit PCM WAV")
        rate = wav.getframerate()
        if rate < 8000:
            raise ValueError("Expected a sample rate of at least 8000 Hz")
        raw = wav.readframes(wav.getnframes())
    samples = struct.unpack(f"<{len(raw) // 2}h", raw)
    size, hop = round(rate * 0.04), round(rate * 0.01)
    window = [0.5 - 0.5 * math.cos(2 * math.pi * i / (size - 1)) for i in range(size)]
    tones: list[Tone] = []
    active = None
    start = last = count = 0

    def finish():
        # Three overlapping frames require about 60 ms of stable evidence.
        if active is not None and count >= 3:
            tones.append(Tone(active, start / rate, (last + size) / rate))

    for offset in range(0, len(samples) - size + 1, hop):
        frame = [samples[offset + i] / 32768 * window[i] for i in range(size)]
        energy = sum(x * x for x in frame)
        digit = None
        if energy / size > 1e-6:
            row = [_power(frame, f, rate) for f in ROWS]
            col = [_power(frame, f, rate) for f in COLS]
            r, c = max(range(4), key=row.__getitem__), max(range(4), key=col.__getitem__)
            low, high = row[r], col[c]
            # Require two dominant frequencies, plausible amplitude balance,
            # and substantial energy in both tones rather than speech/noise.
            if (
                low > 4 * max(p for i, p in enumerate(row) if i != r)
                and high > 4 * max(p for i, p in enumerate(col) if i != c)
                and 0.1 < low / max(high, 1e-20) < 10
                and 2 * min(low, high) / (size * energy) > 0.10
                and 2 * (low + high) / (size * energy) > 0.55
            ):
                digit = KEYS[r][c]
        if digit != active:
            finish()
            active, start, count = digit, offset, 0
        count += 1
        last = offset
    finish()
    return tones
