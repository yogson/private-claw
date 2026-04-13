"""Tests for vocabulary models."""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from assistant.extensions.language_learning.models import (
    CardDirection,
    CardResult,
    CompactVerbForms,
    CompactWordPayload,
    ExerciseResultPayload,
    Gender,
    PartOfSpeech,
    VerbForms,
    VocabularyEntry,
    VocabularyProgress,
)


class TestVocabularyEntry:
    """Tests for VocabularyEntry model."""

    def test_minimal_valid_entry(self) -> None:
        entry = VocabularyEntry(
            user_id="user-1",
            word="σπίτι",
            transliteration="spíti",
            translation="дом",
            part_of_speech=PartOfSpeech.NOUN,
        )
        assert entry.word == "σπίτι"
        assert entry.easiness_factor == 2.5
        assert entry.interval == 0
        assert entry.repetitions == 0

    def test_full_entry(self) -> None:
        entry = VocabularyEntry(
            user_id="user-1",
            word="σπίτι",
            transliteration="spíti",
            translation="дом",
            part_of_speech=PartOfSpeech.NOUN,
            gender=Gender.NEUTER,
            article="το",
            example_sentence="Το σπίτι είναι μεγάλο.",
            example_translation="Дом большой.",
            tags=["home", "basics"],
        )
        assert entry.gender == Gender.NEUTER
        assert entry.article == "το"
        assert "home" in entry.tags

    def test_id_auto_generated(self) -> None:
        entry1 = VocabularyEntry(
            user_id="user-1",
            word="σπίτι",
            transliteration="spíti",
            translation="дом",
            part_of_speech=PartOfSpeech.NOUN,
        )
        entry2 = VocabularyEntry(
            user_id="user-1",
            word="νερό",
            transliteration="neró",
            translation="вода",
            part_of_speech=PartOfSpeech.NOUN,
        )
        assert entry1.id != entry2.id

    def test_invalid_article(self) -> None:
        with pytest.raises(ValidationError):
            VocabularyEntry(
                user_id="user-1",
                word="σπίτι",
                transliteration="spíti",
                translation="дом",
                part_of_speech=PartOfSpeech.NOUN,
                article="the",  # Invalid - should be ο, η, or το
            )

    def test_valid_articles(self) -> None:
        for article in ["ο", "η", "το"]:
            entry = VocabularyEntry(
                user_id="user-1",
                word="test",
                transliteration="test",
                translation="test",
                part_of_speech=PartOfSpeech.NOUN,
                article=article,
            )
            assert entry.article == article

    def test_easiness_factor_minimum(self) -> None:
        with pytest.raises(ValidationError):
            VocabularyEntry(
                user_id="user-1",
                word="test",
                transliteration="test",
                translation="test",
                part_of_speech=PartOfSpeech.NOUN,
                easiness_factor=1.0,  # Below minimum of 1.3
            )

    def test_is_due_forward(self) -> None:
        now = datetime.now(UTC)
        past = now - timedelta(days=1)
        future = now + timedelta(days=1)

        due_entry = VocabularyEntry(
            user_id="user-1",
            word="test",
            transliteration="test",
            translation="test",
            part_of_speech=PartOfSpeech.NOUN,
            next_review=past,
        )
        assert due_entry.is_due(CardDirection.FORWARD, now) is True

        not_due_entry = VocabularyEntry(
            user_id="user-1",
            word="test",
            transliteration="test",
            translation="test",
            part_of_speech=PartOfSpeech.NOUN,
            next_review=future,
        )
        assert not_due_entry.is_due(CardDirection.FORWARD, now) is False

    def test_is_due_reverse(self) -> None:
        now = datetime.now(UTC)
        past = now - timedelta(days=1)

        entry = VocabularyEntry(
            user_id="user-1",
            word="test",
            transliteration="test",
            translation="test",
            part_of_speech=PartOfSpeech.NOUN,
            reverse_next_review=past,
        )
        assert entry.is_due(CardDirection.REVERSE, now) is True

    def test_get_sm2_fields_forward(self) -> None:
        entry = VocabularyEntry(
            user_id="user-1",
            word="test",
            transliteration="test",
            translation="test",
            part_of_speech=PartOfSpeech.NOUN,
            easiness_factor=2.7,
            interval=6,
            repetitions=2,
        )
        fields = entry.get_sm2_fields(CardDirection.FORWARD)
        assert fields["easiness_factor"] == 2.7
        assert fields["interval"] == 6
        assert fields["repetitions"] == 2

    def test_get_sm2_fields_reverse(self) -> None:
        entry = VocabularyEntry(
            user_id="user-1",
            word="test",
            transliteration="test",
            translation="test",
            part_of_speech=PartOfSpeech.NOUN,
            reverse_easiness_factor=2.3,
            reverse_interval=10,
            reverse_repetitions=3,
        )
        fields = entry.get_sm2_fields(CardDirection.REVERSE)
        assert fields["easiness_factor"] == 2.3
        assert fields["interval"] == 10
        assert fields["repetitions"] == 3

    def test_verb_entry_with_verb_forms(self) -> None:
        verb_forms = VerbForms(
            present="γράφω",
            present_tr="gráfo",
            aorist="έγραψα",
            aorist_tr="égrapsa",
            future="θα γράψω",
            future_tr="tha grápso",
        )
        entry = VocabularyEntry(
            user_id="user-1",
            word="γράφω",
            transliteration="gráfo",
            translation="писать",
            part_of_speech=PartOfSpeech.VERB,
            verb_forms=verb_forms,
        )
        assert entry.verb_forms is not None
        assert entry.verb_forms.present == "γράφω"
        assert entry.verb_forms.aorist == "έγραψα"
        assert entry.verb_forms.future == "θα γράψω"

    def test_noun_entry_without_verb_forms(self) -> None:
        entry = VocabularyEntry(
            user_id="user-1",
            word="σπίτι",
            transliteration="spíti",
            translation="дом",
            part_of_speech=PartOfSpeech.NOUN,
        )
        assert entry.verb_forms is None


class TestVerbForms:
    """Tests for VerbForms model."""

    def test_valid_verb_forms(self) -> None:
        verb_forms = VerbForms(
            present="γράφω",
            present_tr="gráfo",
            aorist="έγραψα",
            aorist_tr="égrapsa",
            future="θα γράψω",
            future_tr="tha grápso",
        )
        assert verb_forms.present == "γράφω"
        assert verb_forms.present_tr == "gráfo"
        assert verb_forms.aorist == "έγραψα"
        assert verb_forms.aorist_tr == "égrapsa"
        assert verb_forms.future == "θα γράψω"
        assert verb_forms.future_tr == "tha grápso"

    def test_verb_forms_min_length_validation(self) -> None:
        with pytest.raises(ValidationError):
            VerbForms(
                present="",  # Empty string - should fail
                present_tr="gráfo",
                aorist="έγραψα",
                aorist_tr="égrapsa",
                future="θα γράψω",
                future_tr="tha grápso",
            )

    def test_verb_forms_required_fields(self) -> None:
        with pytest.raises(ValidationError):
            VerbForms(
                present="γράφω",
                present_tr="gráfo",
                # Missing aorist fields
            )


class TestCardResult:
    """Tests for CardResult model."""

    def test_valid_result(self) -> None:
        result = CardResult(word_id="word-123", rating=2, time_ms=1500)
        assert result.word_id == "word-123"
        assert result.rating == 2
        assert result.time_ms == 1500
        assert result.direction == CardDirection.FORWARD

    def test_rating_range(self) -> None:
        for rating in range(4):
            result = CardResult(word_id="word-123", rating=rating)
            assert result.rating == rating

        with pytest.raises(ValidationError):
            CardResult(word_id="word-123", rating=-1)

        with pytest.raises(ValidationError):
            CardResult(word_id="word-123", rating=4)

    def test_reverse_direction(self) -> None:
        result = CardResult(
            word_id="word-123",
            rating=2,
            direction=CardDirection.REVERSE,
        )
        assert result.direction == CardDirection.REVERSE


class TestExerciseResultPayload:
    """Tests for ExerciseResultPayload model."""

    def test_valid_payload(self) -> None:
        payload = ExerciseResultPayload(
            results=[
                CardResult(word_id="w1", rating=2),
                CardResult(word_id="w2", rating=3),
            ]
        )
        assert len(payload.results) == 2
        assert payload.type == "exercise_results"

    def test_empty_results(self) -> None:
        payload = ExerciseResultPayload(results=[])
        assert len(payload.results) == 0


class TestCompactWordPayload:
    """Tests for CompactWordPayload model."""

    def test_from_entry(self) -> None:
        entry = VocabularyEntry(
            id="word-123",
            user_id="user-1",
            word="σπίτι",
            transliteration="spíti",
            translation="дом",
            part_of_speech=PartOfSpeech.NOUN,
            article="το",
            example_sentence="Το σπίτι είναι μεγάλο.",
            example_translation="Дом большой.",
        )
        compact = CompactWordPayload.from_entry(entry)
        assert compact.id == "word-123"
        assert compact.word == "σπίτι"
        assert compact.transliteration == "spíti"
        assert compact.translation == "дом"
        assert compact.article == "το"
        assert compact.example_sentence == "Το σπίτι είναι μεγάλο."
        assert compact.verb_forms is None

    def test_from_entry_with_verb_forms(self) -> None:
        verb_forms = VerbForms(
            present="γράφω",
            present_tr="gráfo",
            aorist="έγραψα",
            aorist_tr="égrapsa",
            future="θα γράψω",
            future_tr="tha grápso",
        )
        entry = VocabularyEntry(
            id="verb-123",
            user_id="user-1",
            word="γράφω",
            transliteration="gráfo",
            translation="писать",
            part_of_speech=PartOfSpeech.VERB,
            verb_forms=verb_forms,
        )
        compact = CompactWordPayload.from_entry(entry)
        assert compact.id == "verb-123"
        assert compact.word == "γράφω"
        assert compact.verb_forms is not None
        assert compact.verb_forms.present == "γράφω"
        assert compact.verb_forms.aorist == "έγραψα"
        assert compact.verb_forms.future == "θα γράψω"

    def test_compact_aliases(self) -> None:
        compact = CompactWordPayload(
            id="word-123",
            w="σπίτι",
            t="spíti",
            tr="дом",
        )
        # Access via alias
        data = compact.model_dump(by_alias=True)
        assert data["id"] == "word-123"
        assert data["w"] == "σπίτι"
        assert data["t"] == "spíti"
        assert data["tr"] == "дом"

    def test_compact_verb_forms_aliases(self) -> None:
        verb_forms = VerbForms(
            present="γράφω",
            present_tr="gráfo",
            aorist="έγραψα",
            aorist_tr="égrapsa",
            future="θα γράψω",
            future_tr="tha grápso",
        )
        compact_vf = CompactVerbForms.from_verb_forms(verb_forms)
        data = compact_vf.model_dump(by_alias=True)
        assert data["p"] == "γράφω"
        assert data["pt"] == "gráfo"
        assert data["a"] == "έγραψα"
        assert data["at"] == "égrapsa"
        assert data["f"] == "θα γράψω"
        assert data["ft"] == "tha grápso"

    def test_compact_word_payload_with_verb_forms_serialization(self) -> None:
        verb_forms = VerbForms(
            present="διαβάζω",
            present_tr="diavázo",
            aorist="διάβασα",
            aorist_tr="diávasa",
            future="θα διαβάσω",
            future_tr="tha diaváso",
        )
        entry = VocabularyEntry(
            id="verb-456",
            user_id="user-1",
            word="διαβάζω",
            transliteration="diavázo",
            translation="читать",
            part_of_speech=PartOfSpeech.VERB,
            verb_forms=verb_forms,
        )
        compact = CompactWordPayload.from_entry(entry)
        data = compact.model_dump(by_alias=True, exclude_none=True)
        assert data["vf"]["p"] == "διαβάζω"
        assert data["vf"]["a"] == "διάβασα"
        assert data["vf"]["f"] == "θα διαβάσω"


class TestVocabularyProgress:
    """Tests for VocabularyProgress model."""

    def test_default_values(self) -> None:
        progress = VocabularyProgress(user_id="user-1")
        assert progress.total_words == 0
        assert progress.accuracy_percent == 0.0
        assert progress.streak_days == 0

    def test_with_stats(self) -> None:
        progress = VocabularyProgress(
            user_id="user-1",
            total_words=100,
            words_learned=50,
            words_learning=30,
            words_new=20,
            total_reviews=500,
            correct_reviews=450,
            accuracy_percent=90.0,
        )
        assert progress.total_words == 100
        assert progress.accuracy_percent == 90.0


class TestEnums:
    """Tests for enum values."""

    def test_part_of_speech_values(self) -> None:
        assert PartOfSpeech.NOUN == "noun"
        assert PartOfSpeech.VERB == "verb"
        assert PartOfSpeech.PHRASE == "phrase"

    def test_gender_values(self) -> None:
        assert Gender.MASCULINE == "m"
        assert Gender.FEMININE == "f"
        assert Gender.NEUTER == "n"

    def test_card_direction_values(self) -> None:
        assert CardDirection.FORWARD == "forward"
        assert CardDirection.REVERSE == "reverse"
