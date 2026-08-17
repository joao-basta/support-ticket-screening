"""Object-level tests for the screening pipeline and its two views.

Covers `ldp.core.screening` (the validation matrix and status resolution),
`ldp.core.result` (the serializable outcome) and `ldp.core.reporting` (the
analyst-facing text) end-to-end, driving everything through `screen_ticket`
with in-memory tickets - no subprocess, no CLI, no network.
"""

import json

import pytest

from ldp import config
from ldp.core.entities import Confidence, EntityKind
from ldp.core.reporting import render_report
from ldp.core.result import EvidenceCheck, ScreeningStatus
from ldp.core.screening import (
    CONTEXT_RULES,
    EVIDENCE_REGEX,
    TIME_REGEX,
    resolve_status,
    screen_ticket,
    validate_context,
)
from ldp.core.symptoms import UNRESOLVED_SYMPTOMS, Symptom
from ldp.core.ticket_source import classify_attachments

# ---------------------------------------------------------
# Evidence labels, spelled exactly as the pipeline emits them.
# ---------------------------------------------------------
TIME_ITEM = 'Horário da ocorrência'
PHONE_ITEM = 'Número do Telefone/Alvo'
VISUAL_ITEM = 'Print da tela, log de console ou anexo (imagem/PDF/log)'

#: Narrative that reliably infers FALHA_INTERFACE_UX and mentions no artifact.
UX_TEXT = 'a tela travou e o sistema congelou durante o uso'

#: Narrative that reliably infers FALHA_FILA_ROTEAMENTO.
QUEUE_TEXT = 'agentes disponiveis mas clientes parados na fila de atendimento'

#: The complete set of top-level keys `to_dict()` promises to integrators.
EXPECTED_PAYLOAD_KEYS = frozenset({
    'chamado',
    'status',
    'acao_necessaria',
    'sintoma',
    'confianca',
    'alternativas',
    'texto_analisado',
    'evidencias',
    'pendencias',
    'dados_sensiveis',
    'blocos_descartados',
    'anexos',
    'analise_log',
    'codigo_saida',
})


def items(result):
    """Evidence labels of `result`, in order."""
    return tuple(check.item for check in result.evidences)


# =========================================================
# FALHA_CHAMADA_AUDIO: the two-rule matrix
# =========================================================

@pytest.mark.parametrize('text, status, missing', [
    pytest.param(
        'a ligacao caiu no meio do atendimento as 14:30, telefone (11) 98765-4321',
        ScreeningStatus.APROVADO, (),
        id='time-and-phone',
    ),
    pytest.param(
        'a ligacao caiu no meio do atendimento as 14:30',
        ScreeningStatus.BLOQUEADO, (PHONE_ITEM,),
        id='time-only',
    ),
    pytest.param(
        'a ligacao caiu no meio do atendimento, telefone (11) 98765-4321',
        ScreeningStatus.BLOQUEADO, (TIME_ITEM,),
        id='phone-only',
    ),
    pytest.param(
        'a ligacao caiu no meio do atendimento com o cliente',
        ScreeningStatus.BLOQUEADO, (TIME_ITEM, PHONE_ITEM),
        id='neither',
    ),
])
def test_audio_requires_both_time_and_phone(make_ticket, text, status, missing):
    """Pins that FALHA_CHAMADA_AUDIO is approved only when time AND a phone are supplied."""
    result = screen_ticket(make_ticket(text))

    assert result.symptom is Symptom.CHAMADA_AUDIO
    assert result.status is status
    assert result.missing == missing
    assert items(result) == (TIME_ITEM, PHONE_ITEM)


def test_audio_rule_order_is_time_then_phone(make_ticket):
    """Pins the declared order of the two audio evidence checks."""
    result = screen_ticket(make_ticket('a ligacao caiu no meio do atendimento com o cliente'))

    assert [check.item for check in result.evidences] == [TIME_ITEM, PHONE_ITEM]
    assert [check.satisfied for check in result.evidences] == [False, False]


def test_protocol_number_is_not_phone_evidence(make_ticket):
    """REGRESSION: a protocol number must not satisfy the phone rule.

    `protocolo 1234567890` used to be masked as a phone and approved a ticket
    that carried no phone number at all.
    """
    result = screen_ticket(
        make_ticket('sem audio na ligacao as 14:30, protocolo 1234567890')
    )

    assert result.symptom is Symptom.CHAMADA_AUDIO
    assert result.status is ScreeningStatus.BLOQUEADO
    assert result.missing == (PHONE_ITEM,)
    assert result.entity_counts == {'PROTOCOLO': 1}


def test_eleven_digit_mobile_after_cue_is_phone_evidence(make_ticket):
    """REGRESSION: an 11-digit mobile behind a cue word must approve the ticket.

    `telefone 11987654321` used to be claimed by the CPF rule, so the phone the
    requester actually supplied never counted.
    """
    result = screen_ticket(make_ticket('a ligacao caiu as 14:30, telefone 11987654321'))

    assert result.symptom is Symptom.CHAMADA_AUDIO
    assert result.status is ScreeningStatus.APROVADO
    assert result.missing == ()
    assert result.exit_code == config.EXIT_SUCCESS


def test_bare_digit_run_is_masked_but_is_not_phone_evidence(make_ticket):
    """Pins that a MEDIUM-confidence digit run is hidden yet never counts as evidence."""
    result = screen_ticket(
        make_ticket('a ligacao caiu as 14:30 e o aparelho 11987654321 ficou mudo')
    )

    phones = [e for e in result.entities if e.kind is EntityKind.TELEFONE]
    assert phones and all(e.confidence is Confidence.MEDIUM for e in phones)
    assert '11987654321' not in result.analyzed_text
    assert result.status is ScreeningStatus.BLOQUEADO
    assert result.missing == (PHONE_ITEM,)


# =========================================================
# FALHA_INTERFACE_UX: artifact evidence
# =========================================================

@pytest.mark.parametrize('word', [
    'print', 'prints', 'anexo', 'anexei', 'screenshot', 'captura',
    'imagem', 'foto', 'console', 'log', 'logs',
])
def test_evidence_regex_accepts_artifact_words(word):
    """Pins the vocabulary that denotes an artifact the requester actually produced."""
    assert EVIDENCE_REGEX.search(f'segue {word} do problema')


@pytest.mark.parametrize('text', [
    'tela',
    'a tela travou',
    'a tela ficou branca durante o atendimento',
    'telas do sistema',
])
def test_evidence_regex_rejects_the_bare_word_tela(text):
    """Pins that 'tela' alone is NOT artifact evidence.

    It was removed from EVIDENCE_REGEX because nearly every UX complaint is
    *about* the screen, so accepting it let the problem statement satisfy the
    screenshot requirement.
    """
    assert EVIDENCE_REGEX.search(text) is None


def test_ux_complaint_about_the_screen_is_blocked(make_ticket):
    """Pins that describing a frozen screen does not, by itself, supply evidence."""
    result = screen_ticket(make_ticket(UX_TEXT))

    assert result.symptom is Symptom.INTERFACE_UX
    assert result.status is ScreeningStatus.BLOQUEADO
    assert result.missing == (VISUAL_ITEM,)
    assert result.exit_code == config.EXIT_BLOCKED


@pytest.mark.parametrize('text', [
    'a tela travou, segue o print da tela',
    'a tela travou, envio o anexo com a evidencia',
    'a tela travou, colei o log do console abaixo',
])
def test_ux_satisfied_by_mentioning_an_artifact(make_ticket, text):
    """Pins that naming a captured artifact satisfies the UX evidence rule."""
    result = screen_ticket(make_ticket(text))

    assert result.symptom is Symptom.INTERFACE_UX
    assert result.status is ScreeningStatus.APROVADO
    assert result.missing == ()


@pytest.mark.parametrize('name, payload, expected_role', [
    pytest.param('app.txt', b'linha de log\n', 'logs', id='txt-log'),
    pytest.param('app.log', b'linha de log\n', 'logs', id='log'),
    pytest.param('captura.png', b'\x89PNG\r\n', 'visuals', id='png'),
    pytest.param('evidencia.pdf', b'%PDF-1.4', 'visuals', id='pdf'),
])
def test_ux_satisfied_by_an_attached_file(make_ticket, tmp_path, name, payload, expected_role):
    """Pins that an attached log or visual satisfies the UX rule with no keyword in the text."""
    attachment = tmp_path / name
    attachment.write_bytes(payload)

    result = screen_ticket(make_ticket(UX_TEXT, attachments=[attachment]))

    assert result.symptom is Symptom.INTERFACE_UX
    assert result.status is ScreeningStatus.APROVADO
    assert result.missing == ()
    assert getattr(result.attachments, expected_role) == 1
    assert result.attachments.total == 1


def test_unrelated_attachment_does_not_satisfy_ux(make_ticket, tmp_path):
    """Pins that a file of an unrecognized kind counts as neither log nor visual."""
    attachment = tmp_path / 'planilha.xlsx'
    attachment.write_bytes(b'nao e log nem imagem')

    result = screen_ticket(make_ticket(UX_TEXT, attachments=[attachment]))

    assert result.attachments.others == 1
    assert result.status is ScreeningStatus.BLOQUEADO
    assert result.missing == (VISUAL_ITEM,)


# =========================================================
# FALHA_FILA_ROTEAMENTO: time only
# =========================================================

@pytest.mark.parametrize('text, status, missing', [
    pytest.param(f'{QUEUE_TEXT} desde as 14:30', ScreeningStatus.APROVADO, (), id='with-time'),
    pytest.param(QUEUE_TEXT, ScreeningStatus.BLOQUEADO, (TIME_ITEM,), id='without-time'),
])
def test_queue_requires_time_only(make_ticket, text, status, missing):
    """Pins that FALHA_FILA_ROTEAMENTO demands the time of occurrence and nothing else."""
    result = screen_ticket(make_ticket(text))

    assert result.symptom is Symptom.FILA_ROTEAMENTO
    assert items(result) == (TIME_ITEM,)
    assert result.status is status
    assert result.missing == missing


def test_queue_does_not_require_a_phone(make_ticket):
    """Pins that the phone rule is exclusive to the audio symptom."""
    result = screen_ticket(make_ticket(f'{QUEUE_TEXT} desde as 14:30'))

    assert PHONE_ITEM not in items(result)


# =========================================================
# Unresolved symptoms
# =========================================================

@pytest.mark.parametrize('text, symptom', [
    pytest.param(
        'bom dia preciso de ferias e folha de pagamento beneficio',
        Symptom.DESCONHECIDO,
        id='reject-corpus',
    ),
    pytest.param('oi', Symptom.TEXTO_CURTO, id='too-short'),
])
def test_unresolved_symptom_goes_to_a_human(make_ticket, text, symptom):
    """Pins that an unrecognized symptom yields ATENCAO with no evidence demands."""
    result = screen_ticket(make_ticket(text))

    assert result.symptom is symptom
    assert result.symptom in UNRESOLVED_SYMPTOMS
    assert result.evidences == ()
    assert result.missing == ()
    assert result.status is ScreeningStatus.ATENCAO
    assert result.exit_code == config.EXIT_ATTENTION
    assert result.exit_code == 2


@pytest.mark.parametrize('symptom', sorted(UNRESOLVED_SYMPTOMS, key=lambda s: s.value))
def test_unresolved_symptoms_carry_no_rules(symptom):
    """Pins that the validation matrix demands nothing for an unresolved symptom."""
    bundle = classify_attachments(())

    assert CONTEXT_RULES[symptom] == ()
    assert validate_context(symptom, 'as 14:30 com print em anexo', (), bundle) == ()


def test_every_symptom_has_an_entry_in_the_matrix():
    """Pins that no symptom can reach validation without a declared rule set."""
    assert set(CONTEXT_RULES) == set(Symptom)


# =========================================================
# TIME_REGEX
# =========================================================

@pytest.mark.parametrize('text', [
    '14:30', '14h30', '09h', '9h', 'as 14 horas', 'às 14 horas',
    'o problema comecou as 08:05 de hoje',
    'caiu 23:59 e nao voltou',
])
def test_time_regex_accepts_a_moment_of_occurrence(text):
    """Pins the notations analysts use to state when the incident happened."""
    assert TIME_REGEX.search(text)


@pytest.mark.parametrize('text', [
    pytest.param('4 Horas Úteis', id='sla-duration'),
    pytest.param('sla de 4 horas uteis', id='sla-sentence'),
    pytest.param('2024', id='bare-year'),
    pytest.param('999', id='bare-number'),
    pytest.param('25:00', id='impossible-hour'),
    pytest.param('sem informacao de horario', id='no-digits'),
])
def test_time_regex_rejects_non_moments(text):
    """Pins that a duration, a year or a loose number is not a time of occurrence."""
    assert TIME_REGEX.search(text) is None


def test_sla_duration_does_not_satisfy_the_time_rule(make_ticket):
    """Pins the SLA trap end-to-end: '4 horas uteis' must not pass as a timestamp."""
    result = screen_ticket(make_ticket(
        'a ligacao caiu no meio do atendimento, o sla e de 4 horas uteis, '
        'telefone (11) 98765-4321'
    ))

    assert result.status is ScreeningStatus.BLOQUEADO
    assert result.missing == (TIME_ITEM,)


# =========================================================
# resolve_status
# =========================================================

@pytest.mark.parametrize('symptom, evidences, expected', [
    pytest.param(
        Symptom.CHAMADA_AUDIO,
        (EvidenceCheck(TIME_ITEM, True), EvidenceCheck(PHONE_ITEM, True)),
        ScreeningStatus.APROVADO,
        id='resolved-all-satisfied',
    ),
    pytest.param(
        Symptom.CHAMADA_AUDIO,
        (EvidenceCheck(TIME_ITEM, True), EvidenceCheck(PHONE_ITEM, False)),
        ScreeningStatus.BLOQUEADO,
        id='resolved-one-missing',
    ),
    pytest.param(
        Symptom.DESCONHECIDO, (), ScreeningStatus.ATENCAO,
        id='unresolved-no-rules',
    ),
    pytest.param(
        Symptom.TEXTO_CURTO, (), ScreeningStatus.ATENCAO,
        id='too-short-no-rules',
    ),
])
def test_resolve_status_table(symptom, evidences, expected):
    """Pins the terminal state for each combination of symptom and evidence."""
    assert resolve_status(symptom, evidences) is expected


def test_missing_evidence_outranks_an_unresolved_symptom():
    """Pins the documented precedence: demanding a known item beats human review."""
    evidences = (EvidenceCheck(TIME_ITEM, False),)

    assert resolve_status(Symptom.DESCONHECIDO, evidences) is ScreeningStatus.BLOQUEADO
    assert resolve_status(Symptom.TEXTO_CURTO, evidences) is ScreeningStatus.BLOQUEADO


@pytest.mark.parametrize('status, code', [
    (ScreeningStatus.APROVADO, config.EXIT_SUCCESS),
    (ScreeningStatus.BLOQUEADO, config.EXIT_BLOCKED),
    (ScreeningStatus.ATENCAO, config.EXIT_ATTENTION),
])
def test_status_exit_codes_are_the_automation_contract(status, code):
    """Pins the exit code each status maps to."""
    assert status.exit_code == code


# =========================================================
# ScreeningResult.to_dict
# =========================================================

def test_to_dict_top_level_keys_are_exactly_the_documented_set(make_ticket):
    """Pins the JSON contract: the exact pt-BR top-level key set, nothing more."""
    result = screen_ticket(make_ticket('a ligacao caiu as 14:30, telefone 11987654321'))

    assert set(result.to_dict()) == EXPECTED_PAYLOAD_KEYS


def test_to_dict_mirrors_the_result_object(make_ticket):
    """Pins that the serialized document agrees with the object it came from."""
    result = screen_ticket(make_ticket('a ligacao caiu no meio do atendimento as 14:30'))
    payload = result.to_dict()

    assert payload['chamado'] == result.source_id
    assert payload['status'] == result.status.value == 'BLOQUEADO'
    assert payload['acao_necessaria'] == result.status.action
    assert payload['sintoma'] == {
        'codigo': result.symptom.value,
        'descricao': result.symptom.label,
    }
    assert payload['texto_analisado'] == result.analyzed_text
    assert payload['pendencias'] == list(result.missing) == [PHONE_ITEM]
    assert payload['evidencias'] == [
        {'item': TIME_ITEM, 'atendida': True},
        {'item': PHONE_ITEM, 'atendida': False},
    ]
    assert payload['codigo_saida'] == result.exit_code == config.EXIT_BLOCKED


def test_to_dict_carries_entity_counts_but_never_raw_pii(make_ticket):
    """Pins the LGPD guarantee: 'dados_sensiveis' holds counts, and no PII leaks anywhere."""
    result = screen_ticket(make_ticket(
        'a ligacao caiu as 14:30, telefone 11987654321, email joao@corp.com.br'
    ))
    payload = result.to_dict()

    assert payload['dados_sensiveis'] == result.entity_counts
    assert payload['dados_sensiveis']['TELEFONE'] == 1
    assert all(isinstance(count, int) for count in payload['dados_sensiveis'].values())

    serialized = json.dumps(payload, ensure_ascii=False)
    assert '11987654321' not in serialized
    assert 'joao@corp.com.br' not in serialized


def test_to_dict_has_no_log_analysis_without_logs(make_ticket):
    """Pins that 'analise_log' stays None when no log file was attached."""
    result = screen_ticket(make_ticket('a ligacao caiu as 14:30, telefone 11987654321'))
    payload = result.to_dict()

    assert result.log_analysis is None
    assert payload['analise_log'] is None
    assert payload['anexos'] == {'logs': 0, 'visuais': 0, 'outros': 0, 'total': 0}


def test_to_dict_reports_log_analysis_when_a_log_is_attached(make_ticket, make_log):
    """Pins the shape of 'analise_log' when the forensic pass actually ran."""
    log_path = make_log(SIGNATURE_LINES_UX)
    result = screen_ticket(make_ticket(UX_TEXT, attachments=[log_path]))
    analysis = result.to_dict()['analise_log']

    assert analysis is not None
    assert analysis['arquivos_lidos'] == 1
    assert analysis['ocorrencias'] == len(SIGNATURE_LINES_UX)
    assert analysis['causa_dominante'] == Symptom.INTERFACE_UX.value
    assert analysis['confere_com_texto'] is True
    assert result.has_divergence is False


def test_to_dict_is_json_serializable(make_ticket, glpi_sample):
    """Pins that the payload survives `json.dumps` with no custom encoder."""
    payload = screen_ticket(make_ticket(glpi_sample)).to_dict()

    assert json.loads(json.dumps(payload, ensure_ascii=False)) == payload


# =========================================================
# render_report
# =========================================================

@pytest.mark.parametrize('text, decoration', [
    pytest.param(
        'a ligacao caiu as 14:30, telefone 11987654321',
        '✅ SUCESSO - PRONTO PARA ANÁLISE TÉCNICA',
        id='aprovado',
    ),
    pytest.param(
        'a ligacao caiu no meio do atendimento as 14:30',
        '❌ BLOQUEADO - FALTAM DADOS',
        id='bloqueado',
    ),
    pytest.param(
        'bom dia preciso de ferias e folha de pagamento beneficio',
        '⚠️ ATENÇÃO - ANÁLISE HUMANA NECESSÁRIA',
        id='atencao',
    ),
])
def test_render_report_shows_the_status_decoration(make_ticket, text, decoration):
    """Pins the decorated status line rendered for each terminal state."""
    report = render_report(screen_ticket(make_ticket(text)))

    assert isinstance(report, str)
    assert decoration in report


def test_render_report_lists_every_missing_item(make_ticket):
    """Pins that a blocked report names each pending item, in the evidence block too."""
    result = screen_ticket(make_ticket('a ligacao caiu no meio do atendimento com o cliente'))
    report = render_report(result)

    assert result.missing == (TIME_ITEM, PHONE_ITEM)
    for item in result.missing:
        assert item in report
        assert f'[FALTA] {item}' in report
    assert 'Devolver chamado ao solicitante cobrando' in report


def test_render_report_marks_satisfied_items_as_ok(make_ticket):
    """Pins the OK/FALTA marker rendered per evidence check."""
    report = render_report(screen_ticket(
        make_ticket('a ligacao caiu no meio do atendimento, telefone (11) 98765-4321')
    ))

    assert f'[OK  ] {PHONE_ITEM}' in report
    assert f'[FALTA] {TIME_ITEM}' in report


def test_render_report_omits_the_evidence_block_for_unresolved_symptoms(make_ticket):
    """Pins that a ticket with no rules renders no evidence section."""
    report = render_report(screen_ticket(
        make_ticket('bom dia preciso de ferias e folha de pagamento beneficio')
    ))

    assert '[EVIDÊNCIAS]' not in report
    assert 'Investigar manualmente' in report


def test_render_report_carries_the_headline_fields(make_ticket, tmp_path):
    """Pins the identifying header lines and the attachment/PII summaries."""
    attachment = tmp_path / 'captura.png'
    attachment.write_bytes(b'\x89PNG\r\n')
    result = screen_ticket(make_ticket(
        'a tela travou e o sistema congelou durante o uso do telefone (11) 98765-4321',
        source_id='req000123',
        attachments=[attachment],
    ))
    report = render_report(result)

    assert '[CHAMADO]          : req000123' in report
    assert result.analyzed_text in report
    assert result.symptom.value in report
    assert '[ANEXOS]' in report
    assert '1 evidência(s) visual(is)' in report
    assert '[DADOS SENSÍVEIS]  : TELEFONE=1' in report
    assert '11) 98765-4321' not in report


def test_render_report_width_is_configurable(make_ticket):
    """Pins that the separator rules honour the requested width."""
    result = screen_ticket(make_ticket('a ligacao caiu as 14:30, telefone 11987654321'))

    assert render_report(result, width=20).splitlines()[0] == '=' * 20
    assert render_report(result).splitlines()[0] == '=' * config.REPORT_WIDTH


# =========================================================
# Full legacy GLPI body
# =========================================================

def test_glpi_sample_screens_from_the_narrative_only(make_ticket, glpi_sample):
    """Pins the end-to-end run over a real GLPI layout: only detalhe1 is analysed."""
    result = screen_ticket(make_ticket(glpi_sample, source_id='req000199'))

    assert result.symptom is Symptom.CHAMADA_AUDIO
    assert result.status is ScreeningStatus.APROVADO
    assert result.missing == ()
    assert result.discarded_blocks == ('info solicitante', 'categorizacao', 'info arquivo')
    # The narrative is the only text that reached inference and the rules.
    assert result.analyzed_text.startswith('Prezado suporte')
    assert '192.168.240.90' not in result.analyzed_text
    assert 'Nenhum anexo informado' not in result.analyzed_text
    assert '4 Horas Uteis' not in result.analyzed_text


def test_glpi_sample_never_exposes_the_requester_contact_details(make_ticket, glpi_sample):
    """Pins that the Info Solicitante block reaches neither the text nor the payload."""
    result = screen_ticket(make_ticket(glpi_sample))
    serialized = json.dumps(result.to_dict(), ensure_ascii=False)

    for secret in ('Maria Silva', '3344-5566', 'p999888@corp.caixa.gov.br', '123.456.789-00'):
        assert secret not in result.analyzed_text
        assert secret not in serialized


def test_requester_phone_alone_does_not_satisfy_the_phone_rule(make_ticket):
    """REGRESSION: the caller's own number is not evidence about the affected line.

    Same layout as the sample, but with the phone removed from the narrative:
    only Info Solicitante carries one, and the ticket must still be blocked.
    """
    body = (
        'Detalhes:\n'
        'detalhe1 -> Prezado suporte, a ligacao do agente caiu no meio do '
        'atendimento as 14:30.\n'
        'detalhe2 -> 192.168.240.90\n'
        '\n'
        'Info Solicitante:\n'
        'contatonome -> Maria Silva\n'
        'contatotelefone -> (21) 3344-5566\n'
    )

    result = screen_ticket(make_ticket(body))

    assert result.symptom is Symptom.CHAMADA_AUDIO
    assert result.status is ScreeningStatus.BLOQUEADO
    assert result.missing == (PHONE_ITEM,)
    assert result.entity_counts == {}


def test_glpi_sample_report_renders(make_ticket, glpi_sample):
    """Pins that a full GLPI body renders an approved analyst report."""
    result = screen_ticket(make_ticket(glpi_sample, source_id='req000199'))
    report = render_report(result)

    assert '[CHAMADO]          : req000199' in report
    assert '✅ SUCESSO - PRONTO PARA ANÁLISE TÉCNICA' in report
    assert f'[OK  ] {TIME_ITEM}' in report
    assert f'[OK  ] {PHONE_ITEM}' in report
    assert '(11) 98765-4321' not in report
    assert '(21) 3344-5566' not in report


# Imported late so the module-level constants above stay grouped with the
# labels they belong to.
from tests.conftest import SIGNATURE_LINES  # noqa: E402

SIGNATURE_LINES_UX = SIGNATURE_LINES['FALHA_INTERFACE_UX']
