"""Naming a spoken answer's speaker.

The case that matters most here is the one that looks like a bug and is not: a
name one letter from the host's is left alone. Correcting it would be a guess at
identity dressed up as a spelling fix, and nothing downstream could tell the
difference afterwards.
"""

from __future__ import annotations

from src.workshops.qa_speaker_names import (
    DEFAULT_SPEAKER,
    canonical_speaker,
    speaker_roster,
)

from tests.workshops.test_qa_extraction_service import make_question  # noqa: F401


class TestCanonicalSpeaker:
    def test_a_caption_misspelling_of_the_host_is_corrected(self):
        assert canonical_speaker("Paul Merlin") == "Paul Martin"

    def test_the_host_first_name_alone_is_corrected(self):
        assert canonical_speaker("Paul") == "Paul Martin"

    def test_a_name_one_letter_from_the_host_is_left_alone(self):
        # The whole point of an exact alias table rather than edit distance.
        assert canonical_speaker("Haul Martin") == "Haul Martin"
        assert canonical_speaker("Paula Martin") == "Paula Martin"

    def test_a_roster_spelling_wins_over_the_transcript_spelling(self):
        roster = ["Paul Martin", "Meredith Britt, Sierra Canyon School"]
        assert (
            canonical_speaker("meredith britt - sierra canyon school", roster)
            == "Meredith Britt, Sierra Canyon School"
        )

    def test_a_guest_the_roster_does_not_name_is_stored_as_given(self):
        assert canonical_speaker("Abby Roberts", ["Paul Martin"]) == "Abby Roberts"

    def test_no_speaker_at_all_falls_back_to_the_host(self):
        assert canonical_speaker(None) == DEFAULT_SPEAKER
        assert canonical_speaker("   ") == DEFAULT_SPEAKER


class TestSpeakerRoster:
    def test_the_roster_is_zoom_names_with_the_host_always_present(
        self, qa_db, qa_webinar, make_question
    ):
        make_question(responder_name="Meredith Britt, Sierra Canyon School")
        make_question(responder_name="Talia Pole")

        assert speaker_roster(qa_db, qa_webinar.id) == [
            DEFAULT_SPEAKER,
            "Meredith Britt, Sierra Canyon School",
            "Talia Pole",
        ]

    def test_a_webinar_zoom_named_nobody_on_still_offers_the_host(self, qa_db, qa_webinar):
        assert speaker_roster(qa_db, qa_webinar.id) == [DEFAULT_SPEAKER]

    def test_the_host_is_not_listed_twice(self, qa_db, qa_webinar, make_question):
        make_question(responder_name=DEFAULT_SPEAKER)
        assert speaker_roster(qa_db, qa_webinar.id) == [DEFAULT_SPEAKER]
