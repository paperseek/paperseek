"""Research-core package boundary for the v1.0.0b1 cutover."""

from paperseek_core.models import (
    PaperAuthor,
    PaperCitation,
    PaperIdentifiers,
    PaperKeywords,
    PaperLinks,
    PaperNames,
    PaperRecord,
    PaperSource,
)

__version__ = "1.0.0b3"

__all__ = [
    "PaperAuthor",
    "PaperCitation",
    "PaperIdentifiers",
    "PaperKeywords",
    "PaperLinks",
    "PaperNames",
    "PaperRecord",
    "PaperSource",
    "__version__",
]
