import math
import random
import struct
import wave

import pytest

from tests.dtmf import COLS, KEYS, ROWS, detect_tones


def write_wav(path, samples):
    with wave.open(str(path), "wb") as wav:
        wav.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
        wav.writeframes(struct.pack(f"<{len(samples)}h", *samples))
    return path


def tone(frequencies, seconds=0.15, noise=0):
    rng = random.Random(42)
    return [
        int(6000 * sum(math.sin(2 * math.pi * f * i / 8000) for f in frequencies)
            + rng.uniform(-noise, noise))
        for i in range(round(8000 * seconds))
    ]


@pytest.mark.parametrize("row", range(4))
@pytest.mark.parametrize("col", range(4))
def test_detects_each_key_with_noise(tmp_path, row, col):
    path = write_wav(tmp_path / "tone.wav", tone([ROWS[row], COLS[col]], noise=1000))
    assert [t.digit for t in detect_tones(path)] == [KEYS[row][col]]


def test_keeps_repeated_digits_separated_by_silence(tmp_path):
    samples = tone([852, 1477]) + [0] * 800 + tone([852, 1477])
    result = detect_tones(write_wav(tmp_path / "tones.wav", samples))
    assert [t.digit for t in result] == ["9", "9"]
    assert result[0].end < result[1].start


@pytest.mark.parametrize("samples", [
    [0] * 1600,
    tone([852]),
    tone([1477]),
    tone([852, 1477], seconds=0.02),
    tone([300, 600, 1000]),
    [random.Random(i).randint(-12000, 12000) for i in range(1600)],
])
def test_rejects_silence_single_frequencies_short_bursts_and_noise(tmp_path, samples):
    assert detect_tones(write_wav(tmp_path / "not-dtmf.wav", samples)) == []
