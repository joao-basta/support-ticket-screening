"""Centralized configuration for the whole pipeline.

Every tunable constant lives here so the same binary runs unchanged across
dev/homolog/prod (12-factor). Values are read from environment variables at
import time and fall back to the defaults validated by the test suite.
"""

import os
from pathlib import Path
from typing import FrozenSet


def _env_float(name: str, default: float) -> float:
    """Read a float from the environment, falling back to `default` when unset or malformed."""
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    """Read an int from the environment, falling back to `default` when unset or malformed."""
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


# ---------------------------------------------------------
# NLP ENGINE
# ---------------------------------------------------------
# Minimum cosine similarity for a symptom to be considered recognized.
# Calibrated against the char_wb n-gram vectorizer; recalibrate whenever the
# analyzer or the anchor corpus changes.
NLP_CONFIDENCE_THRESHOLD: float = _env_float('LDP_NLP_THRESHOLD', 0.12)

# Texts shorter than this (after stripping) carry no usable signal.
NLP_MIN_TEXT_LENGTH: int = _env_int('LDP_NLP_MIN_TEXT_LENGTH', 5)

# Character n-gram window. char_wb keeps n-grams inside word boundaries, which
# is what buys tolerance to typos ("ligacao" vs "ligacao" vs "ligcao").
NLP_NGRAM_MIN: int = _env_int('LDP_NLP_NGRAM_MIN', 3)
NLP_NGRAM_MAX: int = _env_int('LDP_NLP_NGRAM_MAX', 5)

# Inference cache: identical ticket bodies are common in RPA replay scenarios.
NLP_CACHE_SIZE: int = _env_int('LDP_NLP_CACHE_SIZE', 512)

# ---------------------------------------------------------
# LOG ANALYZER
# ---------------------------------------------------------
# Hard cap on retained hits. Aggregation stays streaming regardless: counters
# keep advancing after the cap, only the retained excerpts stop growing. This
# is what keeps memory O(1) on multi-gigabyte logs.
LOG_MAX_RETAINED_HITS: int = _env_int('LDP_LOG_MAX_HITS', 100)

# A dominant technical cause needs at least this many hits before the report
# is willing to contradict the symptom inferred from the ticket text.
LOG_DOMINANCE_MIN_HITS: int = _env_int('LDP_LOG_DOMINANCE_MIN_HITS', 1)

# Truncation for excerpts shown in the report (raw log lines can be enormous).
LOG_EXCERPT_MAX_CHARS: int = _env_int('LDP_LOG_EXCERPT_MAX_CHARS', 240)

# ---------------------------------------------------------
# ATTACHMENTS
# ---------------------------------------------------------
LOG_EXTENSIONS: FrozenSet[str] = frozenset({'.txt', '.log', '.csv', '.json', '.out'})
VISUAL_EXTENSIONS: FrozenSet[str] = frozenset({'.png', '.jpg', '.jpeg', '.gif', '.bmp', '.pdf'})
ARCHIVE_EXTENSIONS: FrozenSet[str] = frozenset({'.zip'})

# Zip bomb guards applied during extraction.
ZIP_MAX_MEMBERS: int = _env_int('LDP_ZIP_MAX_MEMBERS', 512)
ZIP_MAX_UNCOMPRESSED_BYTES: int = _env_int('LDP_ZIP_MAX_BYTES', 512 * 1024 * 1024)
ZIP_MAX_COMPRESSION_RATIO: int = _env_int('LDP_ZIP_MAX_RATIO', 200)

# Encoding ladder for legacy GLPI exports. Brazilian systems routinely emit
# cp1252; utf-8-sig strips the BOM that Excel/Notepad prepend.
TEXT_ENCODINGS: tuple = ('utf-8-sig', 'utf-8', 'cp1252', 'latin-1')

# ---------------------------------------------------------
# TICKET SOURCES
# ---------------------------------------------------------
MOCK_POOL_DIR: Path = Path(os.environ.get('LDP_MOCK_POOL_DIR', 'data/raw'))

# ---------------------------------------------------------
# REPORTING
# ---------------------------------------------------------
REPORT_WIDTH: int = _env_int('LDP_REPORT_WIDTH', 50)

# ---------------------------------------------------------
# HTTP API
# ---------------------------------------------------------
API_MAX_CONTENT_LENGTH: int = _env_int('LDP_API_MAX_CONTENT_LENGTH', 32 * 1024 * 1024)
API_HOST: str = os.environ.get('LDP_API_HOST', '127.0.0.1')
API_PORT: int = _env_int('LDP_API_PORT', 8000)

# ---------------------------------------------------------
# EXIT CODES (automation contract - do not renumber)
# ---------------------------------------------------------
EXIT_SUCCESS: int = 0
EXIT_BLOCKED: int = 1
EXIT_ATTENTION: int = 2
EXIT_ERROR: int = 3
