"""
Component ID: CMP_EXT_LANGUAGE_LEARNING

Pydantic models for vocabulary entries and exercise payloads.
"""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator


class PartOfSpeech(StrEnum):
    """Parts of speech for vocabulary entries."""

    NOUN = "noun"
    VERB = "verb"
    ADJECTIVE = "adjective"
    ADVERB = "adverb"
    PHRASE = "phrase"
    OTHER = "other"


class LearningStatus(StrEnum):
    """Learning status for vocabulary entries."""

    NEW = "new"  # Just added, never reviewed
    LEARNING = "learning"  # Active repetitions in progress
    KNOWN = "known"  # Confident (interval >= 21d AND EF >= 2.5)
    SUSPENDED = "suspended"  # Manually excluded by user


class Gender(StrEnum):
    """Grammatical gender for nouns."""

    MASCULINE = "m"
    FEMININE = "f"
    NEUTER = "n"


class CardDirection(StrEnum):
    """Direction for flashcard exercises."""

    FORWARD = "forward"  # Source language -> Target language (Greek -> Russian)
    REVERSE = "reverse"  # Target language -> Source language (Russian -> Greek)


class ExerciseType(StrEnum):
    """Types of learning exercises."""

    FLASHCARDS = "flashcards"


class VerbForms(BaseModel):
    """Greek verb conjugation forms (1st person singular)."""

    present: str = Field(..., min_length=1, description="Present tense (Ενεστώτας): γράφω")
    present_tr: str = Field(..., min_length=1, description="Present transliteration: gráfo")
    aorist: str = Field(..., min_length=1, description="Aorist/Past tense (Αόριστος): έγραψα")
    aorist_tr: str = Field(..., min_length=1, description="Aorist transliteration: égrapsa")
    future: str = Field(..., min_length=1, description="Future tense (Μέλλοντας): θα γράψω")
    future_tr: str = Field(..., min_length=1, description="Future transliteration: tha grápso")


class VocabularyEntry(BaseModel):
    """A vocabulary entry with SM-2 spaced repetition metadata."""

    # Identity
    id: str = Field(default_factory=lambda: str(uuid4()))
    user_id: str

    # Word data (Greek)
    word: str = Field(..., min_length=1, description="Greek word")
    transliteration: str = Field(..., min_length=1, description="Latin transliteration")
    translation: str = Field(..., min_length=1, description="Russian translation")

    # Linguistic metadata
    part_of_speech: PartOfSpeech
    gender: Gender | None = Field(default=None, description="Noun gender (m/f/n)")
    article: Annotated[str | None, Field(pattern=r"^(ο|η|το)$")] = Field(
        default=None, description="Greek article for nouns"
    )
    verb_forms: VerbForms | None = Field(
        default=None, description="Verb conjugation forms (present/aorist/future)"
    )
    example_sentence: str | None = Field(default=None, description="Example in Greek")
    example_translation: str | None = Field(default=None, description="Example translation")
    tags: list[str] = Field(default_factory=list, description="Topical tags")

    # Learning status
    learning_status: LearningStatus = Field(
        default=LearningStatus.NEW, description="Current learning status"
    )

    # SM-2 Spaced Repetition fields (forward direction: Greek -> Russian)
    easiness_factor: float = Field(default=2.5, ge=1.3, description="SM-2 easiness factor")
    interval: int = Field(default=0, ge=0, description="Days until next review")
    repetitions: int = Field(default=0, ge=0, description="Consecutive correct recalls")
    next_review: datetime = Field(default_factory=lambda: datetime.now(UTC))

    # SM-2 fields for reverse direction (Russian -> Greek)
    reverse_easiness_factor: float = Field(default=2.5, ge=1.3)
    reverse_interval: int = Field(default=0, ge=0)
    reverse_repetitions: int = Field(default=0, ge=0)
    reverse_next_review: datetime = Field(default_factory=lambda: datetime.now(UTC))

    # Stats
    total_reviews: int = Field(default=0, ge=0)
    correct_reviews: int = Field(default=0, ge=0)
    reverse_total_reviews: int = Field(default=0, ge=0)
    reverse_correct_reviews: int = Field(default=0, ge=0)

    # Timestamps
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def validate_verb_forms_consistency(self) -> "VocabularyEntry":
        """Ensure verb_forms is required for verbs and forbidden for non-verbs."""
        if self.part_of_speech == PartOfSpeech.VERB:
            if self.verb_forms is None:
                raise ValueError("verb_forms is required when part_of_speech is 'verb'")
        else:
            if self.verb_forms is not None:
                raise ValueError("verb_forms must be None when part_of_speech is not 'verb'")
        return self

    def get_sm2_fields(self, direction: CardDirection) -> dict[str, float | int | datetime]:
        """Get SM-2 fields for a specific direction."""
        if direction == CardDirection.FORWARD:
            return {
                "easiness_factor": self.easiness_factor,
                "interval": self.interval,
                "repetitions": self.repetitions,
                "next_review": self.next_review,
                "total_reviews": self.total_reviews,
                "correct_reviews": self.correct_reviews,
            }
        return {
            "easiness_factor": self.reverse_easiness_factor,
            "interval": self.reverse_interval,
            "repetitions": self.reverse_repetitions,
            "next_review": self.reverse_next_review,
            "total_reviews": self.reverse_total_reviews,
            "correct_reviews": self.reverse_correct_reviews,
        }

    def is_due(self, direction: CardDirection, as_of: datetime | None = None) -> bool:
        """Check if word is due for review in the specified direction."""
        check_time = as_of or datetime.now(UTC)
        if direction == CardDirection.FORWARD:
            return self.next_review <= check_time
        return self.reverse_next_review <= check_time


class CardResult(BaseModel):
    """Result of a single card review from Mini App."""

    word_id: str = Field(..., description="ID of the reviewed word")
    rating: int = Field(..., ge=0, le=3, description="0=Again, 1=Hard, 2=Good, 3=Easy")
    time_ms: int | None = Field(default=None, ge=0, description="Response time in milliseconds")
    direction: CardDirection = Field(
        default=CardDirection.FORWARD, description="Card direction reviewed"
    )


class ExerciseResultPayload(BaseModel):
    """Payload received from Mini App via sendData()."""

    type: str = Field(default="exercise_results")
    results: list[CardResult]


class CompactVerbForms(BaseModel):
    """Compact verb forms for Mini App URL encoding."""

    present: str = Field(..., alias="p", description="Present tense")
    present_tr: str = Field(..., alias="pt", description="Present transliteration")
    aorist: str = Field(..., alias="ao", description="Aorist tense")
    aorist_tr: str = Field(..., alias="aot", description="Aorist transliteration")
    future: str = Field(..., alias="f", description="Future tense")
    future_tr: str = Field(..., alias="ft", description="Future transliteration")

    model_config = {"populate_by_name": True}

    @classmethod
    def from_verb_forms(cls, verb_forms: VerbForms) -> "CompactVerbForms":
        """Create compact verb forms from VerbForms."""
        return cls(
            p=verb_forms.present,
            pt=verb_forms.present_tr,
            ao=verb_forms.aorist,
            aot=verb_forms.aorist_tr,
            f=verb_forms.future,
            ft=verb_forms.future_tr,
        )


class CompactWordPayload(BaseModel):
    """Compact word payload for Mini App URL encoding."""

    id: str = Field(..., alias="id", description="Word ID")
    word: str = Field(..., alias="w", description="Greek word")
    transliteration: str = Field(..., alias="t", description="Latin transliteration")
    translation: str = Field(..., alias="tr", description="Russian translation")
    article: str | None = Field(default=None, alias="a", description="Greek article")
    verb_forms: CompactVerbForms | None = Field(
        default=None, alias="vf", description="Verb conjugation forms"
    )
    example_sentence: str | None = Field(default=None, alias="ex", description="Example")
    example_translation: str | None = Field(default=None, alias="et", description="Example trans")

    model_config = {"populate_by_name": True}

    @classmethod
    def from_entry(cls, entry: VocabularyEntry) -> "CompactWordPayload":
        """Create a compact payload from a vocabulary entry."""
        compact_vf = None
        if entry.verb_forms is not None:
            compact_vf = CompactVerbForms.from_verb_forms(entry.verb_forms)

        return cls(
            id=entry.id,
            w=entry.word,
            t=entry.transliteration,
            tr=entry.translation,
            a=entry.article,
            vf=compact_vf,
            ex=entry.example_sentence,
            et=entry.example_translation,
        )


class VocabularyProgress(BaseModel):
    """Learning progress statistics for a user."""

    user_id: str
    total_words: int = 0
    words_learned: int = 0  # Words with interval >= 21 days
    words_learning: int = 0  # Words with 0 < interval < 21
    words_new: int = 0  # Words with interval = 0
    total_reviews: int = 0
    correct_reviews: int = 0
    accuracy_percent: float = 0.0
    due_today: int = 0
    due_today_reverse: int = 0
    streak_days: int = 0
    last_review_date: datetime | None = None
