"""
Component ID: CMP_EXT_LANGUAGE_LEARNING

Add vocabulary tool for the language learning agent.
"""

from typing import Any

import structlog
from pydantic import BaseModel, Field
from pydantic_ai import RunContext

from assistant.agent.tools.deps import TurnDeps
from assistant.extensions.language_learning.models import (
    LearningStatus,
    PartOfSpeech,
    VerbForms,
    VocabularyEntry,
)

logger = structlog.get_logger(__name__)

_MAX_WORDS_PER_CALL = 10


class VerbFormsInput(BaseModel):
    """Verb forms input for add_vocabulary tool."""

    present: str = Field(..., min_length=1)
    present_tr: str = Field(..., min_length=1)
    aorist: str = Field(..., min_length=1)
    aorist_tr: str = Field(..., min_length=1)
    future: str = Field(..., min_length=1)
    future_tr: str = Field(..., min_length=1)


class WordInput(BaseModel):
    """A single word to add to vocabulary."""

    word: str = Field(..., min_length=1, description="Greek word in base/dictionary form")
    transliteration: str = Field(
        ..., min_length=1, description="Latin transliteration with stress marks"
    )
    translation: str = Field(..., min_length=1, description="Russian translation, 1-3 words")
    part_of_speech: PartOfSpeech
    gender: str | None = Field(default=None, description="Noun gender: m, f, or n")
    article: str | None = Field(default=None, description="Greek article: ο, η, or το")
    verb_forms: VerbFormsInput | None = Field(default=None, description="Required for verbs")
    example_sentence: str | None = Field(default=None)
    example_translation: str | None = Field(default=None)
    tags: list[str] = Field(default_factory=list)


async def add_vocabulary(
    ctx: RunContext[TurnDeps],
    words: list[WordInput],
) -> dict[str, Any]:
    """Add one or more Greek vocabulary words (up to 10 per call).

    You MUST provide accurate linguistic metadata:
    - Transliteration: use Latin letters with acute accent on stressed vowel
      (e.g. σπίτι → spíti, not spiti)
    - Nouns: ALWAYS include gender (m/f/n) and article (ο/η/το)
    - Verbs: ALWAYS include verb_forms with all three tenses in 1st person singular:
      present (Ενεστώτας), aorist (Αόριστος), future (Μέλλοντας with θα particle)
    - Examples: provide a natural example sentence when possible

    Duplicates (same word field for same user) are silently skipped and reported in the response.

    Args:
        words: List of word entries to add (max 10).
    """
    user_id = ctx.deps.user_id
    store = ctx.deps.vocabulary_store
    if user_id is None or store is None:
        logger.warning("ext.language_learning.add_vocabulary", status="unavailable")
        return {"status": "unavailable", "reason": "language learning not configured"}

    if not words:
        return {"status": "rejected_invalid", "reason": "words list cannot be empty"}

    bounded_words = words[:_MAX_WORDS_PER_CALL]

    added: list[str] = []
    skipped_duplicates: list[str] = []
    errors: list[str] = []

    for word_input in bounded_words:
        try:
            # Check for duplicate
            existing = await store.find_duplicate(user_id, word_input.word)
            if existing is not None:
                skipped_duplicates.append(word_input.word)
                logger.info(
                    "ext.language_learning.add_vocabulary",
                    action="skip_duplicate",
                    word=word_input.word,
                )
                continue

            # Build VerbForms if provided
            verb_forms = None
            if word_input.verb_forms is not None:
                verb_forms = VerbForms(
                    present=word_input.verb_forms.present,
                    present_tr=word_input.verb_forms.present_tr,
                    aorist=word_input.verb_forms.aorist,
                    aorist_tr=word_input.verb_forms.aorist_tr,
                    future=word_input.verb_forms.future,
                    future_tr=word_input.verb_forms.future_tr,
                )

            # Build gender enum if provided
            from assistant.extensions.language_learning.models import Gender

            gender = None
            if word_input.gender is not None:
                try:
                    gender = Gender(word_input.gender)
                except ValueError:
                    errors.append(f"{word_input.word}: invalid gender '{word_input.gender}'")
                    continue

            entry = VocabularyEntry(
                user_id=user_id,
                word=word_input.word,
                transliteration=word_input.transliteration,
                translation=word_input.translation,
                part_of_speech=word_input.part_of_speech,
                gender=gender,
                article=word_input.article,
                verb_forms=verb_forms,
                example_sentence=word_input.example_sentence,
                example_translation=word_input.example_translation,
                tags=word_input.tags,
                learning_status=LearningStatus.NEW,
            )
            await store.add(entry)
            added.append(word_input.word)
            logger.info(
                "ext.language_learning.add_vocabulary",
                action="added",
                word=word_input.word,
                word_id=entry.id,
            )
        except Exception as exc:
            errors.append(f"{word_input.word}: {exc}")
            logger.warning(
                "ext.language_learning.add_vocabulary",
                action="error",
                word=word_input.word,
                error=str(exc),
            )

    parts: list[str] = []
    if added:
        parts.append(f"Added {len(added)} word(s): {', '.join(added)}.")
    if skipped_duplicates:
        parts.append(f"Duplicates skipped: {', '.join(skipped_duplicates)}.")
    if errors:
        parts.append(f"Errors: {'; '.join(errors)}.")
    if not added and not skipped_duplicates:
        parts.append("No words were added.")

    summary = " ".join(parts)
    logger.info(
        "ext.language_learning.add_vocabulary",
        added_count=len(added),
        skipped_count=len(skipped_duplicates),
        error_count=len(errors),
    )
    return {
        "status": "ok",
        "summary": summary,
        "added": added,
        "skipped_duplicates": skipped_duplicates,
        "errors": errors,
    }
