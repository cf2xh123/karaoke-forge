import math
import random

from karaoke_forge.media import match_audio_envelopes


def _distinctive_envelope(length: int, seed: int) -> list[float]:
    randomizer = random.Random(seed)
    return [
        math.sin(index * 0.071)
        + 0.55 * math.sin(index * 0.193)
        + randomizer.uniform(-0.08, 0.08)
        for index in range(length)
    ]


def test_audio_sync_finds_song_after_narrative_intro() -> None:
    reference = _distinctive_envelope(2400, seed=1)
    intro = _distinctive_envelope(150, seed=2)
    outro = _distinctive_envelope(90, seed=3)
    video = [*intro, *reference, *outro]

    result = match_audio_envelopes(reference, video, frame_seconds=0.05)

    assert result.reliable
    assert abs(result.offset - 7.5) <= 0.1
    assert result.confidence > 0.9
    assert result.matched_windows == result.total_windows


def test_audio_sync_rejects_different_song() -> None:
    reference = _distinctive_envelope(2400, seed=4)
    randomizer = random.Random(5)
    video = [randomizer.uniform(-1.0, 1.0) for _ in range(2200)]

    result = match_audio_envelopes(reference, video, frame_seconds=0.05)

    assert not result.reliable
    assert result.matched_windows < 3
