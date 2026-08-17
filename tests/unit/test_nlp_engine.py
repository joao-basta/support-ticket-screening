"""Unit tests for `ldp.engines.nlp_engine` (Passo 1: symptom inference).

The headline promise of the design document is classification that survives the
typos real support staff make. These tests pin that promise, the reject class
that keeps administrative requests out of the technical taxonomy, and the
structural contract of `SymptomInference` (confidence range, runner-ups, cache
determinism).
"""

from pathlib import Path
from typing import Dict, List, Tuple

import pytest

from conftest import SIGNATURE_LINES
from ldp import config
from ldp.core.entities import anonymize
from ldp.core.glpi_parser import parse_ticket
from ldp.core.symptoms import CLASSIFIABLE_SYMPTOMS, Symptom
from ldp.engines.nlp_engine import (
    ANCHOR_DATA,
    REJECT_ANCHORS,
    SymptomInference,
    infer_symptom,
    infer_symptom_nlp,
)

# ---------------------------------------------------------
# TEST DATA (pt-BR, as written by real support staff)
# ---------------------------------------------------------

#: The same complaint spelled three ways: unaccented, accented, and mistyped.
#: char_wb n-grams must collapse all three onto the same symptom.
TYPO_VARIANTS: Tuple[str, ...] = (
    'ligacao caiu no meio da chamada',
    'ligação caiu no meio da chamada',
    'lgacao caiu no meio da chmada',
)

#: (text, expected symptom) for every classifiable symptom, three texts each.
#: None of these is a verbatim anchor - they are paraphrases, which is what the
#: engine faces in production.
SYMPTOM_CASES: Tuple[Tuple[str, Symptom], ...] = (
    ('o botao de tabulacao travou e nao responde ao clique', Symptom.INTERFACE_UX),
    ('a tela ficou branca e o sistema congelou', Symptom.INTERFACE_UX),
    ('relatorio nao carrega a pagina fica girando sem fim', Symptom.INTERFACE_UX),
    ('o audio do softphone ficou mudo durante o atendimento', Symptom.CHAMADA_AUDIO),
    ('cliente nao escuta o agente na chamada', Symptom.CHAMADA_AUDIO),
    ('chamada com voz robotica e picotando', Symptom.CHAMADA_AUDIO),
    ('clientes parados na fila e agentes disponiveis sem receber chamada',
     Symptom.FILA_ROTEAMENTO),
    ('o roteamento nao direciona a chamada para o agente logado', Symptom.FILA_ROTEAMENTO),
    ('fila travada nao distribui as chamadas para os atendentes', Symptom.FILA_ROTEAMENTO),
)

#: Perfectly valid tickets that are simply not one of the three technical
#: failures. A bare confidence threshold cannot separate these, which is why the
#: engine models "not a symptom" as its own class.
REJECT_CASES: Tuple[str, ...] = (
    'solicito aumento de salario e ferias',
    'preciso de acesso ao sistema de RH',
    'bom dia obrigado pelo atendimento',
    'pedido de reembolso de despesas',
    'troca de cadeira do escritorio',
)

#: Texts shorter than `config.NLP_MIN_TEXT_LENGTH` once stripped.
SHORT_TEXTS: Tuple[str, ...] = ('', 'abc', '   ', 'oi', 'abcd')

#: Every text the engine is expected to resolve to a technical symptom.
RESOLVED_TEXTS: Tuple[str, ...] = TYPO_VARIANTS + tuple(text for text, _ in SYMPTOM_CASES)

# ---------------------------------------------------------
# MOCK CORPUS (accuracy test, skipped when data/raw is absent)
# ---------------------------------------------------------
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_MOCK_DIR = _PROJECT_ROOT / 'data' / 'raw'
_MOCK_TICKETS: List[Path] = sorted(_MOCK_DIR.glob('req*.md')) if _MOCK_DIR.is_dir() else []

#: Reverse map from an injected root-cause log line to the symptom it encodes.
#: The mock generator writes exactly one of these into each log, which makes the
#: log the ground-truth label for its ticket.
_LINE_TO_SYMPTOM: Dict[str, str] = {
    line: label for label, lines in SIGNATURE_LINES.items() for line in lines
}

#: Minimum share of the seeded corpus the engine must classify correctly.
#: Measured at 60/60 on the committed corpus; the margin absorbs regeneration
#: with a different seed.
MIN_CORPUS_ACCURACY = 0.90


def _ground_truth(ticket_path: Path) -> str:
    """Return the symptom label encoded in a mock ticket's companion log.

    Returns an empty string when the log is missing or carries no unambiguous
    signature, so the caller can skip that pair instead of scoring noise.
    """
    log_path = ticket_path.with_name(f'{ticket_path.stem}_log.txt')
    if not log_path.exists():
        return ''
    content = log_path.read_text(encoding='utf-8')
    labels = {label for line, label in _LINE_TO_SYMPTOM.items() if line in content}
    return labels.pop() if len(labels) == 1 else ''


# ---------------------------------------------------------
# TYPO TOLERANCE
# ---------------------------------------------------------

@pytest.mark.parametrize('text', TYPO_VARIANTS)
def test_typo_tolerance_keeps_the_same_symptom(text):
    """Accented, unaccented and mistyped spellings all reach FALHA_CHAMADA_AUDIO."""
    inference = infer_symptom(text)

    assert inference.symptom is Symptom.CHAMADA_AUDIO
    assert inference.is_resolved is True


def test_typo_costs_confidence_but_not_the_verdict():
    """A typo may lower the score, yet never below the acceptance threshold."""
    clean = infer_symptom('ligacao caiu no meio da chamada')
    typo = infer_symptom('lgacao caiu no meio da chmada')

    assert typo.symptom is clean.symptom
    assert typo.confidence >= config.NLP_CONFIDENCE_THRESHOLD


def test_accent_stripping_makes_spellings_identical():
    """`strip_accents='unicode'` means 'ligacao' and 'ligação' score the same."""
    unaccented = infer_symptom('ligacao caiu no meio da chamada')
    accented = infer_symptom('ligação caiu no meio da chamada')

    assert unaccented == accented


# ---------------------------------------------------------
# CLASSIFICATION TABLE
# ---------------------------------------------------------

@pytest.mark.parametrize('text, expected', SYMPTOM_CASES)
def test_classifies_each_symptom(text, expected):
    """Paraphrased reports of each of the three failures land on the right symptom."""
    assert infer_symptom(text).symptom is expected


@pytest.mark.parametrize(
    'symptom, anchor',
    [(symptom, anchor) for symptom, anchors in ANCHOR_DATA.items() for anchor in anchors],
)
def test_every_anchor_classifies_as_its_own_symptom(symptom, anchor):
    """The corpus is self-consistent: no anchor is closer to a rival class."""
    assert infer_symptom(anchor).symptom is symptom


# ---------------------------------------------------------
# REJECT CLASS
# ---------------------------------------------------------

@pytest.mark.parametrize('text', REJECT_CASES)
def test_administrative_requests_are_unknown(text):
    """HR, purchasing and pleasantries route to a human, not to a technical symptom."""
    inference = infer_symptom(text)

    assert inference.symptom is Symptom.DESCONHECIDO
    assert inference.is_resolved is False


@pytest.mark.parametrize('anchor', REJECT_ANCHORS)
def test_every_reject_anchor_stays_unknown(anchor):
    """The reject corpus never leaks into the technical taxonomy."""
    assert infer_symptom(anchor).symptom is Symptom.DESCONHECIDO


# ---------------------------------------------------------
# SHORT TEXT
# ---------------------------------------------------------

@pytest.mark.parametrize('text', SHORT_TEXTS)
def test_short_text_short_circuits(text):
    """Text shorter than the minimum length returns TEXTO_CURTO with zero confidence."""
    inference = infer_symptom(text)

    assert inference.symptom is Symptom.TEXTO_CURTO
    assert inference.confidence == 0.0
    assert inference.runner_ups == ()
    assert inference.is_resolved is False


def test_min_text_length_boundary():
    """A text of exactly NLP_MIN_TEXT_LENGTH characters is analysed, not short-circuited."""
    text = 'x' * config.NLP_MIN_TEXT_LENGTH

    assert infer_symptom(text).symptom is not Symptom.TEXTO_CURTO


# ---------------------------------------------------------
# RUNNER-UPS
# ---------------------------------------------------------

@pytest.mark.parametrize('text', RESOLVED_TEXTS)
def test_runner_ups_are_populated_ranked_and_exclude_the_winner(text):
    """A resolved result carries every losing class, best first, winner absent."""
    inference = infer_symptom(text)
    losers = [symptom for symptom, _ in inference.runner_ups]
    scores = [score for _, score in inference.runner_ups]

    assert losers, 'runner_ups must not be empty for a resolved inference'
    assert inference.symptom not in losers
    assert len(losers) == len(set(losers))
    assert scores == sorted(scores, reverse=True)
    assert all(score <= inference.confidence for score in scores)


@pytest.mark.parametrize('text', RESOLVED_TEXTS)
def test_runner_ups_cover_every_other_class(text):
    """Runner-ups list all three losing classes: the two rival symptoms plus reject."""
    inference = infer_symptom(text)
    reported = {inference.symptom} | {symptom for symptom, _ in inference.runner_ups}

    assert reported == set(CLASSIFIABLE_SYMPTOMS) | {Symptom.DESCONHECIDO}


# ---------------------------------------------------------
# CONFIDENCE
# ---------------------------------------------------------

@pytest.mark.parametrize('text', RESOLVED_TEXTS + REJECT_CASES)
def test_confidence_is_a_cosine_similarity(text):
    """Confidence stays inside the [0, 1] range a cosine similarity can produce."""
    assert 0.0 <= infer_symptom(text).confidence <= 1.0


@pytest.mark.parametrize('text', RESOLVED_TEXTS)
def test_resolved_confidence_clears_the_threshold(text):
    """A symptom is only reported when it beats config.NLP_CONFIDENCE_THRESHOLD."""
    assert infer_symptom(text).confidence >= config.NLP_CONFIDENCE_THRESHOLD


# ---------------------------------------------------------
# DETERMINISM AND CACHE
# ---------------------------------------------------------

def test_inference_is_deterministic():
    """Two calls on the same text produce equal results (and hit the lru_cache)."""
    text = 'o audio picotou e a ligacao caiu durante o atendimento ao cliente'

    first = infer_symptom(text)
    second = infer_symptom(text)

    assert first == second
    assert first is second, 'the lru_cache should return the very same object'


def test_cache_records_a_hit_on_repeat():
    """The lru_cache is actually wired: a repeated text counts as a hit."""
    text = 'chamada derrubada sozinha varias vezes no mesmo turno de atendimento'
    infer_symptom(text)
    before = infer_symptom.cache_info().hits

    infer_symptom(text)

    assert infer_symptom.cache_info().hits == before + 1


def test_result_is_an_immutable_value_object():
    """SymptomInference is frozen, so a cached result cannot be mutated in place."""
    inference = infer_symptom('a tela ficou branca e o sistema congelou')

    assert isinstance(inference, SymptomInference)
    with pytest.raises(Exception):
        inference.symptom = Symptom.DESCONHECIDO  # type: ignore[misc]


# ---------------------------------------------------------
# CORPUS INTEGRITY
# ---------------------------------------------------------

def test_anchor_data_covers_exactly_the_classifiable_symptoms():
    """Every classifiable symptom has anchors, and nothing else does."""
    assert set(ANCHOR_DATA) == set(CLASSIFIABLE_SYMPTOMS)


@pytest.mark.parametrize('symptom', CLASSIFIABLE_SYMPTOMS)
def test_every_anchor_is_a_non_empty_string(symptom):
    """Anchors are plain non-empty text; an empty one would poison the IDF weights."""
    anchors = ANCHOR_DATA[symptom]

    assert len(anchors) >= 2
    assert all(isinstance(anchor, str) and anchor.strip() for anchor in anchors)


def test_reject_anchors_are_non_empty_strings():
    """The reject corpus follows the same rule as the symptom anchors."""
    assert REJECT_ANCHORS
    assert all(isinstance(anchor, str) and anchor.strip() for anchor in REJECT_ANCHORS)


# ---------------------------------------------------------
# BACKWARDS-COMPATIBLE WRAPPER
# ---------------------------------------------------------

@pytest.mark.parametrize('text, expected', SYMPTOM_CASES)
def test_legacy_wrapper_returns_label_and_score(text, expected):
    """`infer_symptom_nlp` still yields the original (label, score) tuple."""
    label, score = infer_symptom_nlp(text)

    assert label == expected.value
    assert score == infer_symptom(text).confidence


# ---------------------------------------------------------
# ACCURACY OVER THE SEEDED MOCK CORPUS
# ---------------------------------------------------------

@pytest.mark.skipif(
    not _MOCK_TICKETS,
    reason=f"mock corpus absent: run `py tests/mock_generator.py` to populate {_MOCK_DIR}",
)
def test_accuracy_over_mock_corpus():
    """End-to-end accuracy on the generated corpus stays above MIN_CORPUS_ACCURACY.

    Ground truth is the root-cause line the generator buried in each companion
    log, so the label is independent of the ticket wording the engine reads.
    """
    scored = 0
    correct = 0
    misses = []

    for ticket_path in _MOCK_TICKETS:
        expected = _ground_truth(ticket_path)
        if not expected:
            continue
        document = parse_ticket(ticket_path.read_text(encoding='utf-8'))
        masked_text, _ = anonymize(document.analysis_text)
        inference = infer_symptom(masked_text)
        scored += 1
        if inference.symptom.value == expected:
            correct += 1
        else:
            misses.append((ticket_path.name, expected, inference.symptom.value,
                           inference.confidence))

    assert scored >= 10, f'corpus too small to be meaningful: {scored} labelled tickets'
    accuracy = correct / scored
    assert accuracy >= MIN_CORPUS_ACCURACY, (
        f'accuracy {accuracy:.2%} ({correct}/{scored}) below '
        f'{MIN_CORPUS_ACCURACY:.0%}; misses: {misses[:5]}'
    )
