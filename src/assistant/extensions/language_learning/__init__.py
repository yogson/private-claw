"""
Component ID: CMP_EXT_LANGUAGE_LEARNING

Language learning extension for vocabulary management and spaced repetition.
"""

from assistant.extensions.language_learning.fsrs_engine import FSRSEngine
from assistant.extensions.language_learning.models import (
    CardDirection,
    CardResult,
    ExerciseResultPayload,
    ExerciseType,
    LearningStatus,
    PartOfSpeech,
    VocabularyEntry,
    VocabularyProgress,
)
from assistant.extensions.language_learning.store import VocabularyStore

__all__ = [
    "CardDirection",
    "CardResult",
    "ExerciseResultPayload",
    "ExerciseType",
    "FSRSEngine",
    "LearningStatus",
    "PartOfSpeech",
    "VocabularyEntry",
    "VocabularyProgress",
    "VocabularyStore",
]
