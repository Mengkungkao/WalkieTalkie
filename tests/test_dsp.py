"""Getting speech into and out of Codec2 without spoiling it on the way.

The cards run at 48 kHz and Codec2 at 8 kHz. ALSA's default conversion
does not filter, so what is above 4 kHz folds back into the voice band on
the way in, and mirror images of the voice are left above 4 kHz on the
way out. These pin the filtering that replaced it, and the levelling.
"""

from __future__ import annotations

import numpy as np
import pytest

from app.audio import dsp


def tone(freq: float, rate: int, seconds: float = 0.5, amplitude: float = 8000.0) -> bytes:
    t = np.arange(int(rate * seconds)) / rate
    return (amplitude * np.sin(2 * np.pi * freq * t)).astype("<i2").tobytes()


def level_at(pcm: bytes, rate: int, freq: float) -> float:
    """Amplitude of the `freq` component, from the middle of the clip."""
    x = np.frombuffer(pcm, "<i2").astype(float)
    x = x[len(x) // 4: 3 * len(x) // 4]
    spectrum = np.abs(np.fft.rfft(x * np.hanning(len(x)))) / (len(x) / 4)
    bins = np.fft.rfftfreq(len(x), 1 / rate)
    return float(spectrum[np.argmin(np.abs(bins - freq))])


def db(ratio: float) -> float:
    return 20 * np.log10(max(ratio, 1e-12))


def test_speech_band_tones_come_through_at_their_level():
    for freq in (300, 1000, 3000):
        out = dsp.downsample(tone(freq, 48000))
        assert abs(db(level_at(out, 8000, freq) / 8000)) < 0.5, freq


def test_nothing_above_4khz_folds_back_into_the_voice_band():
    """A 6 kHz whistle would land on 2 kHz, right in the voice band."""
    out = dsp.downsample(tone(6000, 48000))
    assert db(level_at(out, 8000, 2000) / 8000) < -50


def test_alsas_unfiltered_conversion_would_have_let_it_through():
    """Why this module exists: plain interpolation folds the whistle in."""
    x = np.frombuffer(tone(6000, 48000), "<i2").astype(float)
    linear = np.interp(np.arange(0, len(x), 6), np.arange(len(x)), x)
    folded = linear.astype("<i2").tobytes()
    assert db(level_at(folded, 8000, 2000) / 8000) > -10


def test_upsampling_leaves_no_mirror_image():
    """1 kHz at 8 kHz has an image at 7 kHz that sounds metallic."""
    out = dsp.upsample(tone(1000, 8000))
    assert abs(db(level_at(out, 48000, 1000) / 8000)) < 0.5
    assert db(level_at(out, 48000, 7000) / 8000) < -50


def test_lengths_are_exact():
    assert len(dsp.downsample(bytes(96000))) == 16000
    assert len(dsp.upsample(bytes(16000))) == 96000
    assert dsp.downsample(b"") == b""


def test_quiet_speech_is_brought_up_to_a_steady_level():
    quiet = tone(500, 8000, amplitude=1500)                # about -27 dBFS
    out = np.frombuffer(dsp.prepare_speech(quiet), "<i2").astype(float)
    assert db(np.abs(out).max() / 32767) == pytest.approx(-9, abs=1)


def test_the_lift_stops_at_20_db():
    very_quiet = tone(500, 8000, amplitude=600)            # about -35 dBFS
    out = np.frombuffer(dsp.prepare_speech(very_quiet), "<i2").astype(float)
    assert db(np.abs(out).max() / 32767) == pytest.approx(-34.7 + 20, abs=1)


def test_loud_speech_is_brought_down_not_clipped():
    loud = tone(500, 8000, amplitude=32000)
    out = np.frombuffer(dsp.prepare_speech(loud), "<i2").astype(float)
    assert db(np.abs(out).max() / 32767) == pytest.approx(-9, abs=1)


def test_near_silence_is_not_turned_up_into_noise():
    hiss = (np.random.default_rng(1).normal(0, 20, 8000)).astype("<i2").tobytes()
    out = np.frombuffer(dsp.prepare_speech(hiss), "<i2").astype(float)
    assert np.abs(out).max() < 20 * 10 * 5                 # at most +20 dB


def test_hum_and_rumble_are_taken_out():
    hum = tone(50, 8000, amplitude=8000)
    voice = tone(1000, 8000, amplitude=8000)
    mixed = (np.frombuffer(hum, "<i2").astype(int)
             + np.frombuffer(voice, "<i2").astype(int)) // 2
    out = dsp.prepare_speech(mixed.astype("<i2").tobytes())
    assert db(level_at(out, 8000, 50) / level_at(out, 8000, 1000)) < -30


def test_the_fast_conversions_equal_plain_filtering():
    """Polyphase only skips work: the output is the filter's, sample for sample."""
    rng = np.random.default_rng(3)
    x48 = rng.normal(0, 4000, 48000).astype("<i2").tobytes()
    x8 = rng.normal(0, 4000, 8000).astype("<i2").tobytes()

    a = np.frombuffer(x48, "<i2").astype(float)
    expected = np.round(dsp._filter(a, dsp._RESAMPLE)[::dsp.FACTOR])
    assert np.array_equal(np.frombuffer(dsp.downsample(x48), "<i2"), expected)

    b = np.frombuffer(x8, "<i2").astype(float)
    stuffed = np.zeros(b.size * dsp.FACTOR)
    stuffed[::dsp.FACTOR] = b
    expected = np.clip(np.round(dsp._filter(stuffed, dsp._RESAMPLE * dsp.FACTOR)),
                       -32768, 32767)
    assert np.array_equal(np.frombuffer(dsp.upsample(x8), "<i2"), expected)
