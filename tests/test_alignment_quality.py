import json

from karaoke_forge.alignment_quality import annotate_alignment_review
from karaoke_forge.models import KaraokeToken, LyricLine, LyricsDocument


def test_short_high_confidence_tokens_still_require_review_and_survive_reload():
    document = LyricsDocument(lines=[LyricLine(
        "空", 1.0, 2.0, [KaraokeToken("空", 1.0, 1.01, 0.99)],
    )])
    annotate_alignment_review(document, estimated_source=True, new_alignment=True)
    serialized = json.loads(json.dumps(document.to_dict(), ensure_ascii=False))
    assert serialized["metadata"]["alignment_review_lines"] == "1"
    assert "20 毫秒" in serialized["metadata"]["alignment_review_reasons"]


def test_failed_repeat_refinement_keeps_previous_uncertainty():
    document = LyricsDocument(
        lines=[LyricLine("空", 1.0, 2.0, [KaraokeToken("空", 1.0, 2.0)])],
        metadata={
            "word_timing": "audio-refined",
            "alignment_review_lines": "1",
            "alignment_review_reasons": '{"1":["部分逐字时间由插值估算"]}',
        },
    )
    annotate_alignment_review(document, estimated_source=False)
    assert "插值" in document.metadata["alignment_review_reasons"]
    document.metadata["audio_refined_line_indices"] = "1"
    document.lines[0].tokens[0].confidence = 0.95
    annotate_alignment_review(document, estimated_source=False)
    assert document.metadata["alignment_review_lines"] == ""


def test_quality_replaces_stale_reviews_and_ignores_punctuation_and_hidden_lines():
    document = LyricsDocument(
        lines=[
            LyricLine("Hello", 1, 2, [KaraokeToken("Hello", 1, 2, 0.9)]),
            LyricLine("...", 2, 3),
            LyricLine("Hidden", 3, 4, hidden=True),
        ],
        metadata={"alignment_review_reasons": '{"1":["old"]}'},
    )
    annotate_alignment_review(document, estimated_source=True, new_alignment=True)
    assert document.metadata["alignment_review_lines"] == ""


def test_repeat_refinement_with_new_interpolation_still_requires_review():
    document = LyricsDocument(
        lines=[LyricLine("hello world", 1, 3, [
            KaraokeToken("hello ", 1, 2, 0.95), KaraokeToken("world", 2, 3),
        ])],
        metadata={"word_timing": "audio-refined", "audio_refined_line_indices": "1"},
    )
    annotate_alignment_review(document, estimated_source=False)
    assert document.metadata["alignment_review_lines"] == "1"
    assert "插值" in document.metadata["alignment_review_reasons"]


def test_legacy_review_survives_failed_refinement():
    document = LyricsDocument(
        lines=[LyricLine("hello", 1, 3, [KaraokeToken("hello", 1, 3)])],
        metadata={"alignment_status": "low_coverage_recovery", "unmatched_lyric_lines": "1"},
    )
    annotate_alignment_review(document, estimated_source=False)
    assert document.metadata["alignment_review_lines"] == "1"
