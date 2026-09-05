"""Each station sounds like itself.

Two identical Pis on one desk raise a real question every time something
beeps: was that mine or theirs? Pitch answers it without looking at a
screen, and pattern still carries the meaning.
"""

from __future__ import annotations

import pytest

from app.audio.playback import VOICES, CueSet, cues_for, voice_for


def test_different_addresses_get_different_pitches():
    assert voice_for(90) != voice_for(51)


def test_the_same_address_always_sounds_the_same():
    assert voice_for(90) == voice_for(90)
    assert cues_for(90).pitch == cues_for(90).pitch


def test_every_pitch_is_a_real_audible_tone():
    for pitch in VOICES:
        assert 200 <= pitch <= 4000, f"{pitch} Hz is not useful on a small speaker"


def test_the_pitches_are_all_distinct():
    assert len(set(VOICES)) == len(VOICES)


def test_pitches_are_far_enough_apart_to_tell_by_ear():
    """Adjacent voices need a clear interval, not a few hertz."""
    ordered = sorted(VOICES)
    for lower, higher in zip(ordered, ordered[1:]):
        assert higher / lower >= 1.08, f"{lower} and {higher} are too close"


def test_any_address_maps_to_a_voice():
    for address in (0, 1, 51, 90, 255, 65534):
        assert voice_for(address) in VOICES


@pytest.mark.parametrize("cue", ["tx_start", "tx_done", "rx", "error"])
def test_every_cue_has_audio(cue):
    assert len(getattr(cues_for(90), cue)) > 0


def test_transmit_and_receive_cues_differ():
    """Rising means you are sending; falling means someone is calling."""
    cues = cues_for(90)
    assert cues.rx != cues.tx_done
    assert cues.tx_start != cues.rx


def test_two_stations_produce_different_receive_cues():
    """The point: an incoming call identifies its sender."""
    assert cues_for(90).rx != cues_for(51).rx


def test_the_error_cue_is_the_same_for_everyone():
    """An error is an error; pitching it would only confuse the meaning."""
    assert cues_for(90).error == cues_for(51).error


def test_cues_are_cached_rather_than_rebuilt():
    assert cues_for(90) is cues_for(90)


def test_a_cue_set_reports_itself_usefully():
    assert "MengPi" in repr(CueSet(90, "MengPi"))
