"""Tests for ldp.core.reporting — the text view of a ScreeningResult.

Presentation only, but it is the analyst's entire interface, so the branches
that explain *why* a ticket was stopped need to actually render.
"""

import pytest

from ldp.core.reporting import render_report
from ldp.core.result import (
    AttachmentSummary,
    EvidenceCheck,
    ScreeningResult,
    ScreeningStatus,
)
from ldp.core.screening import screen_ticket
from ldp.core.symptoms import Symptom
from ldp.engines.log_analyzer import LogAnalysisResult, LogHit


def _result(**overrides) -> ScreeningResult:
    """Build a ScreeningResult with sensible defaults for rendering tests."""
    base = dict(
        source_id='req123',
        analyzed_text='a ligacao caiu as 14:30',
        symptom=Symptom.CHAMADA_AUDIO,
        confidence=0.42,
        status=ScreeningStatus.APROVADO,
        evidences=(),
        attachments=AttachmentSummary(),
    )
    base.update(overrides)
    return ScreeningResult(**base)


def _analysis(**overrides) -> LogAnalysisResult:
    hit = LogHit(file_name='app.txt', line_number=12, symptom=Symptom.CHAMADA_AUDIO,
                 label='Jitter Alto', line_excerpt='High Jitter detected: 350ms')
    base = dict(
        hits=(hit,),
        counts_by_symptom={Symptom.CHAMADA_AUDIO: 1},
        dominant_symptom=Symptom.CHAMADA_AUDIO,
        matches_nlp_symptom=True,
        total_hits=1,
        scanned_files=1,
        truncated=False,
    )
    base.update(overrides)
    return LogAnalysisResult(**base)


@pytest.mark.parametrize('status,marker', [
    (ScreeningStatus.APROVADO, 'SUCESSO'),
    (ScreeningStatus.BLOQUEADO, 'BLOQUEADO'),
    (ScreeningStatus.ATENCAO, 'ATENÇÃO'),
])
def test_every_status_renders_its_decoration(status, marker):
    """Each terminal state shows its own headline."""
    report = render_report(_result(status=status))

    assert marker in report


def test_blocked_report_names_every_missing_item():
    """A blocked ticket must tell the analyst exactly what to demand back."""
    result = _result(
        status=ScreeningStatus.BLOQUEADO,
        evidences=(
            EvidenceCheck('Horário da ocorrência', True),
            EvidenceCheck('Número do Telefone/Alvo', False),
        ),
    )
    report = render_report(result)

    assert 'Número do Telefone/Alvo' in report
    assert 'FALTA' in report
    assert 'Devolver chamado' in report


def test_confirmation_branch_renders():
    """A log agreeing with the text is reported as confirmation."""
    report = render_report(_result(log_analysis=_analysis()))

    assert 'CONFIRMAÇÃO' in report
    assert 'app.txt:12' in report
    assert 'Jitter Alto' in report


def test_divergence_branch_renders():
    """A log contradicting the text raises a visible divergence warning."""
    analysis = _analysis(dominant_symptom=Symptom.INTERFACE_UX,
                         matches_nlp_symptom=False)
    report = render_report(_result(log_analysis=analysis))

    assert 'DIVERGÊNCIA' in report
    assert Symptom.INTERFACE_UX.value in report


def test_unresolved_symptom_renders_the_log_as_a_lead_not_a_conflict():
    """With no symptom from the text, the log is a starting point.

    This is the branch that used to print a nonsensical divergence against
    SINTOMA_DESCONHECIDO.
    """
    analysis = _analysis(matches_nlp_symptom=None)
    report = render_report(_result(symptom=Symptom.DESCONHECIDO, log_analysis=analysis))

    assert 'INDÍCIO TÉCNICO' in report
    assert 'DIVERGÊNCIA' not in report


def test_empty_log_analysis_says_so():
    """Scanning a log and finding nothing is itself a reportable outcome."""
    analysis = _analysis(hits=(), counts_by_symptom={}, dominant_symptom=None,
                         matches_nlp_symptom=None, total_hits=0)
    report = render_report(_result(log_analysis=analysis))

    assert 'Nenhuma assinatura' in report


def test_truncated_analysis_is_flagged():
    """A capped report must admit it is showing only the first hits."""
    analysis = _analysis(total_hits=5000, truncated=True)
    report = render_report(_result(log_analysis=analysis))

    assert 'exibindo os primeiros' in report
    assert '5000' in report


def test_attachment_and_pii_lines_appear_only_when_relevant():
    """Optional sections stay out of the report when they carry no information."""
    bare = render_report(_result())
    assert '[ANEXOS]' not in bare
    assert '[DADOS SENSÍVEIS]' not in bare

    rich = render_report(_result(attachments=AttachmentSummary(logs=2, visuals=1)))
    assert '[ANEXOS]' in rich


def test_report_of_a_real_screening_contains_no_raw_pii(make_ticket):
    """The rendered report is masked, like everything else downstream of Passo 0."""
    ticket = make_ticket('a ligacao caiu as 14:30, telefone (11) 98765-4321')
    report = render_report(screen_ticket(ticket))

    assert '98765-4321' not in report
    assert '[TELEFONE_MASK]' in report
