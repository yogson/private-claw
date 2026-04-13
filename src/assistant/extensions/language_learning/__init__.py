"""
Component ID: CMP_EXT_LANGUAGE_LEARNING

Language learning extension for vocabulary management and spaced repetition.
"""

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
from assistant.extensions.language_learning.sm2 import SM2Engine
from assistant.extensions.language_learning.store import VocabularyStore

__all__ = [
    "CardDirection",
    "CardResult",
    "ExerciseResultPayload",
    "ExerciseType",
    "LearningStatus",
    "PartOfSpeech",
    "SM2Engine",
    "VocabularyEntry",
    "VocabularyProgress",
    "VocabularyStore",
]
