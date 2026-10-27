"""TranscriptJoiner: mending sentences that a pause cut split across segments.

The examples are real segment pairs from dictation logs: Whisper marks each
side of a mid-sentence cut with an ellipsis.
"""

from talkat.session import SegmentResult, SessionOutcome, TranscriptJoiner


def _join(*pieces: str) -> str:
    joiner = TranscriptJoiner()
    return "".join(joiner.add(p) for p in pieces) + joiner.finish()


def test_ellipses_on_both_sides_of_a_cut_are_dropped() -> None:
    assert (
        _join(
            "I expect co-work to act somewhat of a...",
            "...guide. Clicking on the browser...",
            "...and doing things that it can easily do.",
        )
        == "I expect co-work to act somewhat of a guide. Clicking on the browser"
        " and doing things that it can easily do."
    )


def test_a_lowercase_start_continues_the_sentence() -> None:
    assert _join("As well as one of the...", "pieces of our...", "...work.") == (
        "As well as one of the pieces of our work."
    )


def test_a_new_sentence_after_trailing_off_keeps_the_ellipsis() -> None:
    assert _join("Maybe this is not...", "Anyway, the dashboard.") == (
        "Maybe this is not... Anyway, the dashboard."
    )


def test_an_ellipsis_at_the_very_end_is_kept() -> None:
    assert _join("First.", "and then...") == "First. and then..."


def test_a_leading_ellipsis_is_dropped_even_without_a_trailing_one() -> None:
    assert _join("...the operator.", "...Done.") == "the operator. Done."


def test_the_unicode_ellipsis_counts_too() -> None:
    assert _join("somewhat of a…", "…guide.") == "somewhat of a guide."


def test_ellipses_inside_a_segment_are_left_alone() -> None:
    assert _join("try to get... most of this") == "try to get... most of this"


def test_empty_and_ellipsis_only_pieces_add_nothing() -> None:
    joiner = TranscriptJoiner()
    assert joiner.add("Hello...") == "Hello"
    assert joiner.add("") == ""
    assert joiner.add(" ... ") == ""
    assert joiner.add("...world.") == " world."
    assert joiner.finish() == ""


def test_each_piece_carries_its_own_separator() -> None:
    joiner = TranscriptJoiner()
    assert joiner.add("One.") == "One."
    assert joiner.add("Two...") == " Two"
    assert joiner.add("Three.") == "... Three."


def test_session_outcome_text_is_joined_the_same_way() -> None:
    results = [
        SegmentResult(index=i, text=t, seconds=1.0, cut="pause")
        for i, t in enumerate(["handing off to...", "", "...the operator."])
    ]
    assert SessionOutcome(results, "stop_requested").text == "handing off to the operator."
