"""Getting speech into and out of Codec2 cleanly.

Codec2 wants 8 kHz audio. The sound cards run at 48 kHz -- the Orange Pi's
Whisplay card offers nothing else -- so audio has to be converted, and how
matters more than it looks. ALSA's `plughw` converts with its "linear"
plugin, which interpolates without filtering: going down it folds
everything above 4 kHz back into the voice band as hiss, and going up it
leaves mirror images of the voice above 4 kHz that sound metallic.

Measured with STOI (an objective intelligibility score, 0 to 1) on
recorded human speech through the whole path:

    Codec2 mode        ALSA linear    filtered here, levelled
    700C                  0.670           0.73
    1600                  0.736           0.83
    3200                  0.746           0.87

So the conversion is done here, with a proper low-pass filter, and the
cards are opened at 48 kHz directly. Before encoding, speech is also
high-passed (hum, rumble and handling noise cost Codec2 bits and buy
nothing) and brought to a steady level (Codec2 does badly with quiet
input, and people hold a radio at every distance from their mouth).

numpy only: scipy is not installed on the radios. The rate conversions
are polyphase -- only the samples that are kept are computed -- because
filtering every 48 kHz sample and discarding five in six took 1.4 s per
twenty-second clip on a Zero 2 W, each way.
"""

from __future__ import annotations

import numpy as np

CODEC_RATE = 8000
HARDWARE_RATE = 48000
FACTOR = HARDWARE_RATE // CODEC_RATE

# Pass speech up to 3.4 kHz; be well down by 4 kHz, where 8 kHz folds.
_CUTOFF_HZ = 3700.0
# Odd, and its delay (the middle tap) a whole number of 8 kHz samples,
# so the polyphase branches below line up exactly.
_RESAMPLE_TAPS = 6 * 48 + 1
_DELAY_8K = (_RESAMPLE_TAPS - 1) // 2 // FACTOR
# High-pass for speech: telephone speech starts at 300 Hz, but voices
# carry energy lower; 120 Hz loses nothing you would miss.
_HIGHPASS_HZ = 120.0
_HIGHPASS_TAPS = 401

# Level: peaks brought to -9 dBFS, but never lifted by more than 20 dB --
# a clip that is quiet because nobody spoke should stay quiet, not have
# its background noise turned up to speech level. -9 dBFS scored best:
# nearer full scale, 700C lost intelligibility (0.737 -> 0.717), and
# levelling mattered most for quiet speech (700C 0.641 -> 0.736, 3200
# 0.846 -> 0.865, at -30 dBFS in).
_TARGET_PEAK = 10 ** (-9 / 20) * 32767
_MAX_GAIN = 10.0


def _lowpass(taps: int, cutoff: float) -> np.ndarray:
    """Windowed-sinc low-pass; `cutoff` in cycles per sample."""
    n = np.arange(taps) - (taps - 1) / 2
    h = 2 * cutoff * np.sinc(2 * cutoff * n) * np.kaiser(taps, 8.6)
    return h / h.sum()


_RESAMPLE = _lowpass(_RESAMPLE_TAPS, _CUTOFF_HZ / HARDWARE_RATE)
_HIGHPASS = -_lowpass(_HIGHPASS_TAPS, _HIGHPASS_HZ / CODEC_RATE)
_HIGHPASS[(_HIGHPASS_TAPS - 1) // 2] += 1.0     # delta minus low-pass


def _filter(x: np.ndarray, h: np.ndarray) -> np.ndarray:
    """Linear-phase FIR by FFT, with its delay removed: same length out."""
    if not x.size:
        return x
    n = x.size + h.size - 1
    size = 1 << (n - 1).bit_length()
    y = np.fft.irfft(np.fft.rfft(x, size) * np.fft.rfft(h, size), size)[:n]
    delay = (h.size - 1) // 2
    return y[delay:delay + x.size]


def _to_array(pcm: bytes) -> np.ndarray:
    return np.frombuffer(pcm[: len(pcm) & ~1], dtype="<i2").astype(np.float64)


def _to_pcm(x: np.ndarray) -> bytes:
    return np.clip(np.round(x), -32768, 32767).astype("<i2").tobytes()


def _window(full: np.ndarray, start: int, size: int) -> np.ndarray:
    out = full[start:start + size]
    return np.pad(out, (0, size - out.size)) if out.size < size else out


def downsample(pcm48: bytes) -> bytes:
    """48 kHz 16-bit mono -> 8 kHz, filtered so nothing folds back.

    y[m] = sum_k h[k] x[6m + D - k]. Splitting k = 6j + r turns that into
    six short convolutions at the low rate, one per phase of x.
    """
    x = _to_array(pcm48)
    size = x.size // FACTOR
    if not size:
        return b""
    phases = x[: size * FACTOR].reshape(size, FACTOR)
    y = np.zeros(size)
    for r in range(FACTOR):
        h_r = _RESAMPLE[r::FACTOR]
        if r == 0:
            branch, offset = phases[:, 0], _DELAY_8K
        else:
            branch, offset = phases[:, FACTOR - r], _DELAY_8K - 1
        y += _window(np.convolve(branch, h_r), offset, size)
    return _to_pcm(y)


def upsample(pcm8: bytes) -> bytes:
    """8 kHz 16-bit mono -> 48 kHz, filtered so no mirror images remain.

    Output sample 6m + p only ever sees the filter taps 6j + p, so each of
    the six phases is one short convolution of the 8 kHz input.
    """
    x = _to_array(pcm8)
    if not x.size:
        return b""
    out = np.empty((x.size, FACTOR))
    for p in range(FACTOR):
        out[:, p] = FACTOR * _window(np.convolve(x, _RESAMPLE[p::FACTOR]),
                                     _DELAY_8K, x.size)
    return _to_pcm(out.reshape(-1))


def prepare_speech(pcm8: bytes) -> bytes:
    """High-pass and level 8 kHz speech before it is encoded."""
    x = _to_array(pcm8)
    if not x.size:
        return pcm8
    x = _filter(x, _HIGHPASS)
    peak = float(np.abs(x).max())
    if peak > 0:
        x *= min(_MAX_GAIN, _TARGET_PEAK / peak)
    return _to_pcm(x)
