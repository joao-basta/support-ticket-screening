"""Integration tests for the HTTP layer (`ldp.api.app`).

These exercise the Flask application through its test client only - never by
calling the view functions directly - so what is pinned here is the wire
contract an integrator actually sees: status codes, media types, and the exact
shape of the JSON body.

The most important test in the file is `test_json_body_equals_screen_ticket`:
the API is supposed to be a thin serialization of `ScreeningResult`, so its
body must be byte-for-byte the dict the CLI renders. That test is what makes it
impossible for the HTTP layer to quietly grow business rules of its own.
"""

import json

import pytest

from ldp import config
from ldp.api.app import create_app
from ldp.core.screening import CONTEXT_RULES
from ldp.core.symptoms import CLASSIFIABLE_SYMPTOMS
from ldp.core.result import ScreeningStatus
from ldp.core.screening import screen_ticket
from ldp.core.ticket_source import Ticket

SCREENINGS_URL = '/api/v1/screenings'
SYMPTOMS_URL = '/api/v1/symptoms'
PROBLEM_JSON = 'application/problem+json'

#: One log line per audio signature, mirroring `SIGNATURE_LINES` in conftest.
#: Duplicated as bytes here because the upload fixture wants a payload, not a path.
AUDIO_LOG_BYTES = (
    b'NETWORK_METRIC: High Jitter detected: 350ms\n'
    b'SIP/408: Request Timeout - No response from client endpoint\n'
)

#: A call-drop report carrying both required evidences (time + phone).
APPROVED_AUDIO_TEXT = (
    'A ligacao caiu no meio do atendimento com o cliente as 14:30. '
    'Telefone 11987654321.'
)

#: The same report with no time and no phone at all.
BLOCKED_AUDIO_TEXT = 'A ligacao caiu no meio do atendimento com o cliente e a voz sumiu.'

#: A perfectly polite ticket that is not one of the three technical failures.
UNKNOWN_TEXT = 'bom dia obrigado abraco'


def _keys_without_id(body: dict) -> dict:
    """Drop the generated ticket id, which is a random uuid per HTTP request."""
    return {key: value for key, value in body.items() if key != 'chamado'}


# ---------------------------------------------------------------------------
# Test page
# ---------------------------------------------------------------------------

def test_index_serves_the_html_page(client):
    """GET / returns a 200 HTML document carrying the page title."""
    response = client.get('/')

    assert response.status_code == 200
    assert response.headers['Content-Type'].startswith('text/html')
    assert '<title>LDP — Triagem de Chamados</title>' in response.get_data(as_text=True)


# ---------------------------------------------------------------------------
# Health probes
# ---------------------------------------------------------------------------

def test_healthz_reports_ok(client):
    """GET /healthz is a pure liveness probe: 200 with {'status': 'ok'}."""
    response = client.get('/healthz')

    assert response.status_code == 200
    assert response.get_json() == {'status': 'ok'}


def test_readyz_reports_a_fitted_model(client):
    """GET /readyz proves the vectorizer is fitted: 200 with a positive anchor count."""
    response = client.get('/readyz')
    body = response.get_json()

    assert response.status_code == 200
    assert body['status'] == 'ok'
    assert body['ancoras'] > 0


# ---------------------------------------------------------------------------
# Taxonomy
# ---------------------------------------------------------------------------

def test_symptoms_lists_every_classifiable_symptom(client):
    """GET /api/v1/symptoms exposes exactly the three classifiable symptoms, in order."""
    response = client.get(SYMPTOMS_URL)
    body = response.get_json()

    assert response.status_code == 200
    assert [item['codigo'] for item in body['sintomas']] == [
        symptom.value for symptom in CLASSIFIABLE_SYMPTOMS
    ]
    assert len(body['sintomas']) == 3


@pytest.mark.parametrize('symptom', CLASSIFIABLE_SYMPTOMS, ids=lambda s: s.value)
def test_symptoms_entry_carries_label_and_evidence_matrix(client, symptom):
    """Each entry mirrors `Symptom.label` and the symptom's rules in CONTEXT_RULES."""
    body = client.get(SYMPTOMS_URL).get_json()
    entry = next(item for item in body['sintomas'] if item['codigo'] == symptom.value)

    assert set(entry) == {'codigo', 'descricao', 'evidencias_obrigatorias'}
    assert entry['descricao'] == symptom.label
    assert entry['evidencias_obrigatorias'] == [
        label for label, _ in CONTEXT_RULES[symptom]
    ]
    assert entry['evidencias_obrigatorias']


def test_symptoms_publishes_the_exit_code_contract(client):
    """The automation exit codes travel with the taxonomy, straight from config."""
    body = client.get(SYMPTOMS_URL).get_json()

    assert body['codigos_saida'] == {
        'aprovado': config.EXIT_SUCCESS,
        'bloqueado': config.EXIT_BLOCKED,
        'atencao': config.EXIT_ATTENTION,
        'erro': config.EXIT_ERROR,
    }


# ---------------------------------------------------------------------------
# Screening: happy paths
# ---------------------------------------------------------------------------

def test_approved_ticket_returns_200(client, glpi_sample):
    """A complete GLPI ticket is approved and answered with HTTP 200."""
    response = client.post(SCREENINGS_URL, json={'texto': glpi_sample})
    body = response.get_json()

    assert response.status_code == 200
    assert response.headers['Content-Type'].startswith('application/json')
    assert body['status'] == ScreeningStatus.APROVADO.value
    assert body['pendencias'] == []
    assert body['codigo_saida'] == config.EXIT_SUCCESS


def test_blocked_ticket_returns_422_with_pending_items(client):
    """Missing evidence maps to HTTP 422 and a non-empty 'pendencias' list."""
    response = client.post(SCREENINGS_URL, json={'texto': BLOCKED_AUDIO_TEXT})
    body = response.get_json()

    assert response.status_code == 422
    assert body['status'] == ScreeningStatus.BLOQUEADO.value
    assert body['pendencias']
    assert body['codigo_saida'] == config.EXIT_BLOCKED


def test_unresolved_symptom_returns_200_not_422(client):
    """ATENCAO is a routing decision, not a client error: it stays on HTTP 200."""
    response = client.post(SCREENINGS_URL, json={'texto': UNKNOWN_TEXT})
    body = response.get_json()

    assert response.status_code == 200
    assert body['status'] == ScreeningStatus.ATENCAO.value
    assert body['codigo_saida'] == config.EXIT_ATTENTION


def test_text_alias_field_is_accepted(client, glpi_sample):
    """The English alias 'text' is honoured as well as the pt-BR 'texto'."""
    response = client.post(SCREENINGS_URL, json={'text': glpi_sample})

    assert response.status_code == 200
    assert response.get_json()['status'] == ScreeningStatus.APROVADO.value


# ---------------------------------------------------------------------------
# Screening: regressions, verified through HTTP
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    'evidence, expected_status, expected_verdict',
    [
        pytest.param(
            'telefone 11987654321',
            200,
            ScreeningStatus.APROVADO.value,
            id='cued-phone-counts-as-evidence',
        ),
        pytest.param(
            'protocolo 1234567890',
            422,
            ScreeningStatus.BLOQUEADO.value,
            id='protocol-number-is-not-a-phone',
        ),
    ],
)
def test_phone_versus_protocol_regression_over_http(
    client, evidence, expected_status, expected_verdict
):
    """A cued phone satisfies the phone rule; a same-length protocol never does."""
    text = f'A ligacao caiu no meio do atendimento as 14:30. {evidence.capitalize()}.'
    response = client.post(SCREENINGS_URL, json={'texto': text})
    body = response.get_json()

    assert response.status_code == expected_status
    assert body['status'] == expected_verdict


def test_protocol_ticket_blocks_specifically_on_the_phone_rule(client):
    """The protocol regression blocks on the phone item alone - the time is present."""
    text = 'A ligacao caiu no meio do atendimento as 14:30. Protocolo 1234567890.'
    body = client.post(SCREENINGS_URL, json={'texto': text}).get_json()

    assert body['pendencias'] == ['Número do Telefone/Alvo']
    assert 'PROTOCOLO' in body['dados_sensiveis']


# ---------------------------------------------------------------------------
# Malformed payloads (RFC 7807)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    'payload',
    [
        pytest.param({}, id='no-texto-field'),
        pytest.param({'texto': ''}, id='empty-texto'),
        pytest.param({'texto': '   '}, id='whitespace-only-texto'),
    ],
)
def test_malformed_payload_returns_problem_json(client, payload):
    """A body with no usable text is a 400 problem+json document, never a traceback."""
    response = client.post(SCREENINGS_URL, json=payload)
    body = response.get_json()

    assert response.status_code == 400
    assert response.headers['Content-Type'] == PROBLEM_JSON
    assert {'type', 'title', 'status'} <= set(body)
    assert body['status'] == 400
    assert body['type'] == '/erros/requisicao-invalida'
    assert body['title']


def test_non_json_content_type_is_also_a_problem_document(client):
    """A body that is not JSON and carries no form field degrades to the same 400."""
    response = client.post(SCREENINGS_URL, data='texto solto', content_type='text/plain')
    body = response.get_json()

    assert response.status_code == 400
    assert response.headers['Content-Type'] == PROBLEM_JSON
    assert body['status'] == 400


def test_unknown_route_returns_problem_json(client):
    """404 is served as problem+json too, so clients only ever parse one error shape."""
    response = client.get('/api/v1/rota-inexistente')
    body = response.get_json()

    assert response.status_code == 404
    assert response.headers['Content-Type'] == PROBLEM_JSON
    assert {'type', 'title', 'status'} <= set(body)
    assert body['status'] == 404
    assert body['type'] == '/erros/erro-http'


# ---------------------------------------------------------------------------
# Uploads
# ---------------------------------------------------------------------------

def test_multipart_upload_runs_the_log_analysis(client, upload):
    """A multipart request with a log attachment is counted and forensically scanned."""
    response = client.post(
        SCREENINGS_URL,
        data={
            'texto': APPROVED_AUDIO_TEXT,
            'anexos': upload(AUDIO_LOG_BYTES, 'app.log'),
        },
        content_type='multipart/form-data',
    )
    body = response.get_json()

    assert response.status_code == 200
    assert body['anexos'] == {'logs': 1, 'visuais': 0, 'outros': 0, 'total': 1}
    assert body['analise_log'] is not None
    assert body['analise_log']['arquivos_lidos'] == 1
    assert body['analise_log']['ocorrencias'] >= 1
    assert body['analise_log']['causa_dominante'] == 'FALHA_CHAMADA_AUDIO'
    assert body['analise_log']['confere_com_texto'] is True


def test_multipart_without_attachments_still_screens(client):
    """The 'texto' form field alone is a valid multipart request."""
    response = client.post(
        SCREENINGS_URL,
        data={'texto': APPROVED_AUDIO_TEXT},
        content_type='multipart/form-data',
    )
    body = response.get_json()

    assert response.status_code == 200
    assert body['status'] == ScreeningStatus.APROVADO.value
    assert body['anexos']['total'] == 0


def test_unsupported_extension_is_skipped_not_fatal(client, upload):
    """A stray .exe is dropped silently; the rest of the screening still succeeds."""
    response = client.post(
        SCREENINGS_URL,
        data={
            'texto': APPROVED_AUDIO_TEXT,
            'anexos': upload(b'MZ\x90\x00', 'malware.exe'),
        },
        content_type='multipart/form-data',
    )
    body = response.get_json()

    assert response.status_code == 200
    assert body['status'] == ScreeningStatus.APROVADO.value
    assert body['anexos']['total'] == 0
    assert body['analise_log'] is None


def test_supported_attachment_survives_an_unsupported_sibling(client, upload):
    """One rejected upload must not take a valid log down with it."""
    response = client.post(
        SCREENINGS_URL,
        data={
            'texto': APPROVED_AUDIO_TEXT,
            'anexos': [
                upload(b'MZ\x90\x00', 'malware.exe'),
                upload(AUDIO_LOG_BYTES, 'app.log'),
            ],
        },
        content_type='multipart/form-data',
    )
    body = response.get_json()

    assert response.status_code == 200
    assert body['anexos']['logs'] == 1
    assert body['analise_log']['ocorrencias'] >= 1


# ---------------------------------------------------------------------------
# Contract: the API is a serialization of ScreeningResult, nothing more
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    'text',
    [
        pytest.param(APPROVED_AUDIO_TEXT, id='approved'),
        pytest.param(BLOCKED_AUDIO_TEXT, id='blocked'),
        pytest.param(UNKNOWN_TEXT, id='unresolved'),
    ],
)
def test_json_body_equals_screen_ticket_to_dict(client, text):
    """The HTTP body is exactly `screen_ticket(...).to_dict()`, so API and CLI cannot drift.

    Only 'chamado' is excluded: the API mints a random uuid per request, since
    an HTTP body has no filename to derive an id from.
    """
    response = client.post(SCREENINGS_URL, json={'texto': text})
    expected = screen_ticket(Ticket(source_id='irrelevante', raw_text=text)).to_dict()

    assert _keys_without_id(response.get_json()) == _keys_without_id(expected)


def test_contract_holds_for_the_full_glpi_layout(client, glpi_sample):
    """The same equality holds for a real legacy GLPI body, discarded blocks included."""
    response = client.post(SCREENINGS_URL, json={'texto': glpi_sample})
    body = response.get_json()
    expected = screen_ticket(Ticket(source_id='irrelevante', raw_text=glpi_sample)).to_dict()

    assert _keys_without_id(body) == _keys_without_id(expected)
    assert body['blocos_descartados']


def test_generated_ticket_id_is_a_non_empty_string(client, glpi_sample):
    """The one field the API owns: a short generated id standing in for a filename."""
    body = client.post(SCREENINGS_URL, json={'texto': glpi_sample}).get_json()

    assert isinstance(body['chamado'], str)
    assert body['chamado']


def test_response_body_is_json_serializable_as_sent(client, glpi_sample):
    """Nothing in the body needs a custom encoder - enums arrive as plain strings."""
    body = client.post(SCREENINGS_URL, json={'texto': glpi_sample}).get_json()

    assert json.loads(json.dumps(body)) == body
    assert isinstance(body['sintoma']['codigo'], str)
    assert isinstance(body['status'], str)


# ---------------------------------------------------------------------------
# Payload size limit
# ---------------------------------------------------------------------------

@pytest.fixture
def tiny_limit_client():
    """A client whose app refuses anything over 128 bytes."""
    return create_app({'TESTING': True, 'MAX_CONTENT_LENGTH': 128}).test_client()


@pytest.mark.parametrize(
    'send',
    [
        pytest.param(
            lambda c: c.post(SCREENINGS_URL, json={'texto': 'a' * 5000}),
            id='json-body',
        ),
        pytest.param(
            lambda c: c.post(
                SCREENINGS_URL,
                data={'texto': 'a' * 5000},
                content_type='multipart/form-data',
            ),
            id='multipart-body',
        ),
    ],
)
def test_oversized_body_returns_413_problem_json(tiny_limit_client, send):
    """Exceeding MAX_CONTENT_LENGTH is a 413 problem document, not a raw 500."""
    response = send(tiny_limit_client)
    body = response.get_json()

    assert response.status_code == 413
    assert response.headers['Content-Type'] == PROBLEM_JSON
    assert {'type', 'title', 'status'} <= set(body)
    assert body['status'] == 413
    assert body['type'] == '/erros/envio-muito-grande'


def test_body_within_the_limit_is_accepted(tiny_limit_client):
    """The guard is a size check only: a small body still screens normally."""
    response = tiny_limit_client.post(SCREENINGS_URL, json={'texto': BLOCKED_AUDIO_TEXT})

    assert response.status_code == 422
    assert response.get_json()['status'] == ScreeningStatus.BLOQUEADO.value


def test_413_message_reports_the_configured_limit(tiny_limit_client):
    """The 413 title quotes the limit the app actually enforces.

    The handler used to read the module default, so an app configured with an
    override told the client 32 MB regardless of its real limit.
    """
    body = tiny_limit_client.post(SCREENINGS_URL, json={'texto': 'a' * 5000}).get_json()

    assert '32 MB' not in body['title']
    assert 'bytes' in body['title'] or 'KB' in body['title']
