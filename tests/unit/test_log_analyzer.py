"""Tests for ldp.engines.log_analyzer (Passo 3).

Two of these pin bugs that shipped silently for a long time: a signature that
could never match any real log line, and a divergence warning raised against a
symptom that was never determined.
"""

import tracemalloc
from pathlib import Path

import pytest

from ldp import config
from ldp.core.errors import AttachmentError
from ldp.core.symptoms import Symptom
from ldp.engines.log_analyzer import (
    ERROR_SIGNATURES,
    analyze_logs,
    scan_log_file,
)

from tests.conftest import SIGNATURE_LINES

# Flatten conftest's mapping into (expected symptom, log line) pairs.
SIGNATURE_CASES = [
    (Symptom(symptom_value), line)
    for symptom_value, lines in SIGNATURE_LINES.items()
    for line in lines
]


# ---------------------------------------------------------------------------
# Signature coverage
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('expected_symptom,line', SIGNATURE_CASES)
def test_every_known_signature_fires(make_log, expected_symptom, line):
    """Each root-cause line the generator emits produces exactly one hit.

    'Jitter Alto' is the reason this test exists. Its pattern was
    `jitter[:\\s]+\\d+\\s*ms`, which cannot match "High Jitter detected: 350ms"
    because the word "detected" sits between the label and the number - so one
    of the nine signatures matched nothing at all, and nothing caught it.
    """
    hits = list(scan_log_file(make_log([line])))

    assert len(hits) == 1
    assert hits[0].symptom is expected_symptom
    assert hits[0].line_number == 1


def test_all_nine_signatures_are_reachable(make_log):
    """Every configured signature label is produced by at least one case."""
    produced = set()
    for _, line in SIGNATURE_CASES:
        produced.update(hit.label for hit in scan_log_file(make_log([line])))

    configured = {label for sigs in ERROR_SIGNATURES.values() for _, label in sigs}
    assert produced == configured


@pytest.mark.parametrize('line', [
    'SERVER_RESPONSE: 500 Internal Server Error on /api/reports',
    'HTTP error: internal server error, code 503',
])
def test_http_5xx_accepts_either_order(make_log, line):
    """The code may precede or follow the keyword.

    The original pattern required the numeric code first and missed the second
    phrasing entirely.
    """
    hits = list(scan_log_file(make_log([line])))

    assert len(hits) == 1
    assert hits[0].symptom is Symptom.INTERFACE_UX


def test_line_matching_two_symptoms_yields_one_hit(make_log):
    """First match wins; a line is never counted twice.

    The signatures used to run as nine independent searches with no
    short-circuit, so a line touching two symptoms inflated both counters.
    """
    line = 'ACD_QUEUE: ERROR overflow while SIP/408 Request Timeout occurred'
    hits = list(scan_log_file(make_log([line])))

    assert len(hits) == 1


def test_lines_without_signatures_produce_nothing(make_log):
    """Ordinary log noise is ignored."""
    noise = [f'INFO: heartbeat {index} ok' for index in range(50)]

    assert list(scan_log_file(make_log(noise))) == []


# ---------------------------------------------------------------------------
# Aggregation and divergence
# ---------------------------------------------------------------------------

def test_empty_log_reports_no_dominant_cause(make_log):
    """No hits means no dominant cause and no comparison."""
    result = analyze_logs([make_log(['INFO: nothing to see'])], Symptom.CHAMADA_AUDIO)

    assert result.total_hits == 0
    assert result.dominant_symptom is None
    assert result.matches_nlp_symptom is None


@pytest.mark.parametrize('unresolved', [Symptom.DESCONHECIDO, Symptom.TEXTO_CURTO])
def test_divergence_is_none_when_the_symptom_is_unresolved(make_log, unresolved):
    """An undetermined symptom cannot diverge from anything.

    `matches_nlp_symptom` was `dominant == nlp_symptom`, which is always False
    for an unresolved symptom, so the report announced a conflict against
    'SINTOMA_DESCONHECIDO' - a warning that never made sense.
    """
    log = make_log(['NETWORK_METRIC: High Jitter detected: 350ms'])
    result = analyze_logs([log], unresolved)

    assert result.dominant_symptom is Symptom.CHAMADA_AUDIO
    assert result.matches_nlp_symptom is None


def test_divergence_true_when_log_confirms_the_text(make_log):
    """Matching signals report confirmation."""
    log = make_log(['NETWORK_METRIC: High Jitter detected: 350ms'])

    assert analyze_logs([log], Symptom.CHAMADA_AUDIO).matches_nlp_symptom is True


def test_divergence_false_when_log_contradicts_the_text(make_log):
    """Conflicting signals report divergence."""
    log = make_log(['NETWORK_METRIC: High Jitter detected: 350ms'])

    assert analyze_logs([log], Symptom.INTERFACE_UX).matches_nlp_symptom is False


def test_dominant_cause_is_the_most_frequent(make_log):
    """The tally, not the first hit, decides the dominant cause."""
    lines = ['FATAL: JavaScript heap out of memory'] * 5
    lines += ['VOIP_STATUS: UDP Packet Loss exceeded 15%']
    result = analyze_logs([make_log(lines)], Symptom.DESCONHECIDO)

    assert result.dominant_symptom is Symptom.INTERFACE_UX
    assert result.counts_by_symptom[Symptom.INTERFACE_UX] == 5


def test_tie_breaking_is_deterministic(make_log):
    """Equal counts resolve the same way on every run."""
    lines = ['FATAL: JavaScript heap out of memory',
             'VOIP_STATUS: UDP Packet Loss exceeded 15%']
    log = make_log(lines)
    outcomes = {analyze_logs([log], Symptom.DESCONHECIDO).dominant_symptom
                for _ in range(5)}

    assert len(outcomes) == 1


def test_multiple_files_are_aggregated(make_log):
    """Counts and file totals span every attached log."""
    first = make_log(['FATAL: JavaScript heap out of memory'], name='a.txt')
    second = make_log(['VOIP_STATUS: UDP Packet Loss exceeded 15%'], name='b.txt')
    result = analyze_logs([first, second], Symptom.DESCONHECIDO)

    assert result.total_hits == 2
    assert result.scanned_files == 2


# ---------------------------------------------------------------------------
# Retention cap and streaming behaviour
# ---------------------------------------------------------------------------

def test_retention_cap_bounds_hits_but_not_counts(make_log, monkeypatch):
    """Excerpts are capped while counting stays complete.

    This is the property that keeps memory flat: the analyzer stops *storing*
    hits past the cap but keeps *counting* them, so the tally is still accurate
    on a log where a signature matches millions of times.
    """
    monkeypatch.setattr(config, 'LOG_MAX_RETAINED_HITS', 10)
    lines = ['FATAL: JavaScript heap out of memory'] * 250
    result = analyze_logs([make_log(lines)], Symptom.INTERFACE_UX)

    assert len(result.hits) == 10
    assert result.truncated is True
    assert result.total_hits == 250
    assert result.counts_by_symptom[Symptom.INTERFACE_UX] == 250


def test_not_truncated_below_the_cap(make_log):
    """A small log keeps every excerpt and is not flagged truncated."""
    result = analyze_logs([make_log(['FATAL: JavaScript heap out of memory'])],
                          Symptom.INTERFACE_UX)

    assert result.truncated is False
    assert len(result.hits) == result.total_hits == 1


def test_excerpts_are_truncated(make_log, monkeypatch):
    """A pathologically long line cannot blow up the report."""
    monkeypatch.setattr(config, 'LOG_EXCERPT_MAX_CHARS', 40)
    line = 'FATAL: JavaScript heap out of memory ' + ('x' * 5000)
    hits = list(scan_log_file(make_log([line])))

    assert len(hits[0].line_excerpt) <= 40


def test_pii_in_a_log_line_is_masked(make_log):
    """Sanitization applies to forensic excerpts too, not just ticket text."""
    line = 'SIP/408: Request Timeout | CallerID: (11) 98765-4321'
    hits = list(scan_log_file(make_log([line])))

    assert '98765-4321' not in hits[0].line_excerpt
    assert '[TELEFONE_MASK]' in hits[0].line_excerpt


@pytest.mark.slow
def test_memory_stays_flat_on_a_large_log(tmp_path, monkeypatch):
    """Peak memory is independent of how many lines match.

    `analyze_logs` used to collect every hit into a list before counting, so
    memory grew with the match count. Here nearly every line matches; if the
    aggregation regressed, this allocates hundreds of MB and fails.
    """
    monkeypatch.setattr(config, 'LOG_MAX_RETAINED_HITS', 50)
    path = tmp_path / 'huge.log'
    with open(path, 'w', encoding='utf-8') as handle:
        for index in range(300_000):
            handle.write(f'{index} FATAL: JavaScript heap out of memory\n')

    tracemalloc.start()
    try:
        result = analyze_logs([path], Symptom.INTERFACE_UX)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert result.total_hits == 300_000
    assert len(result.hits) == 50
    # Generous ceiling: the point is that it is bounded, not that it is tiny.
    assert peak < 8 * 1024 * 1024, f'peak was {peak / 1024 / 1024:.1f} MB'


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------

def test_missing_log_raises_attachment_error(tmp_path):
    """An unreadable log surfaces as a typed error, not an OSError."""
    with pytest.raises(AttachmentError):
        list(scan_log_file(tmp_path / 'does_not_exist.txt'))


def test_invalid_bytes_do_not_crash_the_scan(tmp_path):
    """Production logs contain bytes that are not valid UTF-8."""
    path = tmp_path / 'binary.txt'
    path.write_bytes(b'\xff\xfe FATAL: JavaScript heap out of memory\n')

    assert len(list(scan_log_file(path))) == 1


def test_signature_keys_match_the_symptom_taxonomy():
    """Every signature group is keyed by a real classifiable Symptom."""
    assert set(ERROR_SIGNATURES) == {
        Symptom.INTERFACE_UX, Symptom.CHAMADA_AUDIO, Symptom.FILA_ROTEAMENTO,
    }
