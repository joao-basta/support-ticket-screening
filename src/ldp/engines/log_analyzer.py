"""Forensic log analysis (Passo 3).

Scans attached logs for known error signatures and compares the dominant
technical cause against the symptom inferred from the ticket text.

Memory behaviour
----------------
`scan_log_file` streams line by line and always did. The leak was one level up:
`analyze_logs` collected every hit into a list before counting, so peak memory
grew with the number of matches, not with the file. On a real log where a
signature matches broadly that is unbounded. Counting is now incremental and
only the first `LOG_MAX_RETAINED_HITS` excerpts are kept, so a 5 GB log costs
the same as a 5 KB one.

Matching cost
-------------
The signatures used to be applied as nine independent `re.search` calls per
line, with no short-circuit, so a line matching two symptoms produced two hits
and double-counted. They are now compiled into one alternation with named
groups: one search per line, first match wins.
"""

import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Sequence, Tuple

from ldp import config
from ldp.core.entities import anonymize
from ldp.core.errors import AttachmentError
from ldp.core.symptoms import Symptom

# ---------------------------------------------------------
# ERROR SIGNATURES
# Deliberately loose regexes rather than exact strings, so they survive the
# formatting drift between PABX/ACD versions.
# ---------------------------------------------------------
ERROR_SIGNATURES: Dict[Symptom, Tuple[Tuple[str, str], ...]] = {
    Symptom.INTERFACE_UX: (
        # Accepts either order: "500 Internal Server Error" and
        # "HTTP error: internal server error, code 503" both match. The old
        # pattern required the numeric code to come first and missed the latter.
        (r'(?:\b5\d{2}\b[^\n]{0,80}?(?:internal server error|http)'
         r'|(?:internal server error|http)[^\n]{0,80}?\b5\d{2}\b)', 'HTTP 5xx Server Error'),
        (r'uncaught \w*error', 'JS Uncaught Exception'),
        (r'heap out of memory', 'JS Heap Overflow'),
    ),
    Symptom.CHAMADA_AUDIO: (
        # `jitter\D*\d+\s*ms`, not `jitter[:\s]+\d+\s*ms`: real lines read
        # "High Jitter detected: 350ms", and the word between the label and the
        # number meant this signature never fired at all.
        (r'jitter\D{0,40}\d+\s*ms', 'Jitter Alto'),
        (r'packet loss', 'Perda de Pacotes (UDP)'),
        (r'sip/4\d{2}', 'SIP Timeout/Erro'),
    ),
    Symptom.FILA_ROTEAMENTO: (
        (r'acd_queue[^\n]{0,80}(?:error|overflow)', 'Fila ACD Overflow'),
        (r'agent_state[^\n]{0,80}mismatch', 'Divergência de Estado do Agente'),
        (r'routing_engine[^\n]{0,80}no available agent', 'Sem Agente Disponível'),
    ),
}


def _build_combined_pattern() -> Tuple[re.Pattern, Dict[str, Tuple[Symptom, str]]]:
    """Compile every signature into a single alternation with named groups.

    Returns:
        The compiled pattern and a map from group name to (symptom, label).
    """
    parts: List[str] = []
    registry: Dict[str, Tuple[Symptom, str]] = {}
    for index, (symptom, signatures) in enumerate(ERROR_SIGNATURES.items()):
        for offset, (pattern, label) in enumerate(signatures):
            name = f'sig{index}_{offset}'
            registry[name] = (symptom, label)
            parts.append(f'(?P<{name}>{pattern})')
    return re.compile('|'.join(parts), re.IGNORECASE), registry


_COMBINED_PATTERN, _GROUP_REGISTRY = _build_combined_pattern()


@dataclass(frozen=True)
class LogHit:
    """One matched signature occurrence."""

    file_name: str
    line_number: int
    symptom: Symptom
    label: str
    line_excerpt: str


@dataclass(frozen=True)
class LogAnalysisResult:
    """Aggregate outcome of Passo 3.

    Attributes:
        hits: retained occurrences (capped; see `truncated`).
        counts_by_symptom: full tally, unaffected by the retention cap.
        dominant_symptom: most frequent technical cause, if any cleared the
            minimum-hits floor.
        matches_nlp_symptom: True/False when both signals are comparable,
            None when the text produced no resolvable symptom. Left as None
            rather than False so the report does not announce a divergence
            against a symptom that was never determined.
        total_hits: total matches seen, including those not retained.
        scanned_files: how many files were read.
        truncated: whether the retention cap dropped excerpts.
    """

    hits: Tuple[LogHit, ...]
    counts_by_symptom: Dict[Symptom, int]
    dominant_symptom: Optional[Symptom]
    matches_nlp_symptom: Optional[bool]
    total_hits: int = 0
    scanned_files: int = 0
    truncated: bool = False


def scan_log_file(path: Path) -> Iterator[LogHit]:
    """Yield every signature match in `path`, one line at a time.

    Uses `errors='replace'` because production logs routinely contain bytes
    that are not valid UTF-8, and a decoding crash mid-forensics is worse than
    a replacement character.

    Raises:
        AttachmentError: if the file cannot be opened or read.
    """
    try:
        with open(path, encoding='utf-8', errors='replace') as log_file:
            for line_number, line in enumerate(log_file, start=1):
                match = _COMBINED_PATTERN.search(line)
                if match is None:
                    continue
                name = match.lastgroup
                if name is None or name not in _GROUP_REGISTRY:
                    continue
                symptom, label = _GROUP_REGISTRY[name]
                excerpt = line.strip()[:config.LOG_EXCERPT_MAX_CHARS]
                yield LogHit(
                    file_name=path.name,
                    line_number=line_number,
                    symptom=symptom,
                    label=label,
                    line_excerpt=anonymize(excerpt)[0],
                )
    except OSError as exc:
        raise AttachmentError(
            f"Não foi possível ler o log anexado: {path.name}",
            detail=str(exc),
        ) from exc


def analyze_logs(log_paths: Sequence[Path], nlp_symptom: Symptom) -> LogAnalysisResult:
    """Aggregate signature hits across every attached log.

    Counting is incremental, so memory stays flat no matter how many lines
    match; only the first `LOG_MAX_RETAINED_HITS` excerpts are kept for the
    report.

    Args:
        log_paths: resolved log files to scan.
        nlp_symptom: the symptom inferred from the ticket text, used for the
            divergence comparison.
    """
    counts: Counter = Counter()
    retained: List[LogHit] = []
    total = 0
    truncated = False

    for path in log_paths:
        for hit in scan_log_file(path):
            total += 1
            counts[hit.symptom] += 1
            if len(retained) < config.LOG_MAX_RETAINED_HITS:
                retained.append(hit)
            else:
                truncated = True

    dominant: Optional[Symptom] = None
    if counts:
        # Sort by count then label so ties resolve deterministically instead of
        # by whichever symptom happened to be inserted first.
        ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0].value))
        candidate, occurrences = ordered[0]
        if occurrences >= config.LOG_DOMINANCE_MIN_HITS:
            dominant = candidate

    # Only compare when both signals exist. An unresolved symptom can never
    # equal the dominant cause, which used to render as a bogus
    # "DIVERGÊNCIA ... mas o texto indica SINTOMA_DESCONHECIDO".
    matches: Optional[bool] = None
    if dominant is not None and nlp_symptom.is_resolved:
        matches = dominant is nlp_symptom

    return LogAnalysisResult(
        hits=tuple(retained),
        counts_by_symptom=dict(counts),
        dominant_symptom=dominant,
        matches_nlp_symptom=matches,
        total_hits=total,
        scanned_files=len(log_paths),
        truncated=truncated,
    )
