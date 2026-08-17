"""Flask application exposing the screening pipeline over HTTP (Fase 7).

This layer owns no business rules. It turns a request into a `Ticket`, calls
`screen_ticket`, and serializes the `ScreeningResult` - so the API and the CLI
are guaranteed to reach the same verdict for the same input.

Endpoints
---------
``GET  /``                     interactive test page (HTML)
``POST /api/v1/screenings``    screen a ticket (JSON or multipart)
``GET  /api/v1/symptoms``      taxonomy and validation matrix
``GET  /healthz``              liveness
``GET  /readyz``               readiness (confirms the vectorizer is loaded)

Errors are returned as ``application/problem+json`` (RFC 7807).
"""

import logging
import shutil
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from flask import Flask, jsonify, render_template, request
from werkzeug.exceptions import HTTPException, RequestEntityTooLarge
from werkzeug.utils import secure_filename

from ldp import config
from ldp.core.errors import LdpError, PayloadError
from ldp.core.screening import CONTEXT_RULES, screen_ticket
from ldp.core.symptoms import CLASSIFIABLE_SYMPTOMS, Symptom
from ldp.core.ticket_source import LocalTicketSource

logger = logging.getLogger(__name__)

PROBLEM_JSON = 'application/problem+json'

#: Upload types the pipeline knows how to consume.
ALLOWED_UPLOAD_EXTENSIONS = (
    config.LOG_EXTENSIONS | config.VISUAL_EXTENSIONS | config.ARCHIVE_EXTENSIONS
)


def _problem(status: int, title: str, slug: str, detail: str = '') -> Tuple[Any, int, Dict[str, str]]:
    """Build an RFC 7807 problem document."""
    body: Dict[str, Any] = {
        'type': f'/erros/{slug}',
        'title': title,
        'status': status,
    }
    if detail:
        body['detail'] = detail
    return jsonify(body), status, {'Content-Type': PROBLEM_JSON}


def _extract_text(payload: Optional[Dict[str, Any]]) -> str:
    """Pull the ticket text out of a JSON body or a form field.

    Raises:
        PayloadError: when no usable text was supplied.
    """
    if payload is not None:
        text = payload.get('texto') or payload.get('text') or ''
    else:
        text = request.form.get('texto') or request.form.get('text') or ''

    if not isinstance(text, str) or not text.strip():
        raise PayloadError(
            "Campo 'texto' é obrigatório e não pode estar vazio.",
            detail="envie JSON {'texto': '...'} ou um campo de formulário 'texto'",
        )
    return text


def _save_uploads(destination: Path) -> List[Path]:
    """Persist uploaded attachments into `destination`.

    Filenames are sanitized and unsupported extensions are skipped rather than
    rejected, so one stray file does not fail an otherwise valid screening.
    """
    saved: List[Path] = []
    for index, storage in enumerate(request.files.getlist('anexos') + request.files.getlist('attachments')):
        if not storage or not storage.filename:
            continue
        safe_name = secure_filename(storage.filename) or f'anexo_{index}'
        if Path(safe_name).suffix.lower() not in ALLOWED_UPLOAD_EXTENSIONS:
            logger.warning('Anexo ignorado por extensão não suportada: %s', safe_name)
            continue
        target = destination / f'{index:02d}_{safe_name}'
        storage.save(target)
        saved.append(target)
    return saved


def create_app(test_config: Optional[Dict[str, Any]] = None) -> Flask:
    """Application factory.

    The NLP vectorizer is fitted when `ldp.engines.nlp_engine` is imported,
    which happens once per worker process at import time - never per request.
    """
    app = Flask(__name__)
    app.config['MAX_CONTENT_LENGTH'] = config.API_MAX_CONTENT_LENGTH
    app.config['JSON_SORT_KEYS'] = False
    if test_config:
        app.config.update(test_config)

    # -----------------------------------------------------------------
    # Test page
    # -----------------------------------------------------------------
    @app.get('/')
    def index() -> str:
        """Serve the interactive screening page."""
        return render_template(
            'index.html',
            symptoms=[
                {
                    'codigo': symptom.value,
                    'descricao': symptom.label,
                    'evidencias': [label for label, _ in CONTEXT_RULES.get(symptom, ())],
                }
                for symptom in CLASSIFIABLE_SYMPTOMS
            ],
        )

    # -----------------------------------------------------------------
    # Screening
    # -----------------------------------------------------------------
    @app.post('/api/v1/screenings')
    def create_screening():
        """Screen one ticket.

        Accepts ``application/json`` (``{"texto": "..."}``) or
        ``multipart/form-data`` with a ``texto`` field and optional ``anexos``
        file parts.

        Returns 200 when the ticket is approved or routed to a human, and 422
        when it is blocked for missing evidence - the body is the full
        screening either way.
        """
        payload = request.get_json(silent=True) if request.is_json else None
        text = _extract_text(payload)

        upload_dir = Path(tempfile.mkdtemp(prefix='ldp_api_'))
        try:
            attachments = _save_uploads(upload_dir) if request.files else []
            with LocalTicketSource(text=text, attachment_paths=attachments) as source:
                result = screen_ticket(source.load())
        finally:
            shutil.rmtree(upload_dir, ignore_errors=True)

        body = result.to_dict()
        status = 422 if result.exit_code == config.EXIT_BLOCKED else 200
        return jsonify(body), status

    # -----------------------------------------------------------------
    # Metadata & health
    # -----------------------------------------------------------------
    @app.get('/api/v1/symptoms')
    def list_symptoms():
        """Expose the taxonomy and the evidence each symptom requires."""
        return jsonify({
            'sintomas': [
                {
                    'codigo': symptom.value,
                    'descricao': symptom.label,
                    'evidencias_obrigatorias': [
                        label for label, _ in CONTEXT_RULES.get(symptom, ())
                    ],
                }
                for symptom in CLASSIFIABLE_SYMPTOMS
            ],
            'codigos_saida': {
                'aprovado': config.EXIT_SUCCESS,
                'bloqueado': config.EXIT_BLOCKED,
                'atencao': config.EXIT_ATTENTION,
                'erro': config.EXIT_ERROR,
            },
        })

    @app.get('/healthz')
    def healthz():
        """Liveness: the process is up."""
        return jsonify({'status': 'ok'})

    @app.get('/readyz')
    def readyz():
        """Readiness: the model is fitted and able to classify."""
        from ldp.engines.nlp_engine import anchor_matrix, infer_symptom

        probe = infer_symptom('a ligacao caiu no meio da chamada')
        ready = anchor_matrix.shape[0] > 0 and probe.symptom is Symptom.CHAMADA_AUDIO
        return jsonify({
            'status': 'ok' if ready else 'degradado',
            'ancoras': int(anchor_matrix.shape[0]),
        }), (200 if ready else 503)

    # -----------------------------------------------------------------
    # Error handling
    # -----------------------------------------------------------------
    @app.errorhandler(LdpError)
    def handle_ldp_error(exc: LdpError):
        logger.info('Falha tratada (%s): %s', exc.slug, exc)
        return _problem(exc.http_status, exc.message, exc.slug, exc.detail)

    @app.errorhandler(RequestEntityTooLarge)
    def handle_too_large(exc: RequestEntityTooLarge):
        # Read the limit from the app, not from the module default: an app
        # configured with an override would otherwise quote the wrong number.
        limit = app.config.get('MAX_CONTENT_LENGTH') or config.API_MAX_CONTENT_LENGTH
        if limit >= 1024 * 1024:
            readable = f'{limit // (1024 * 1024)} MB'
        elif limit >= 1024:
            readable = f'{limit // 1024} KB'
        else:
            readable = f'{limit} bytes'
        return _problem(413, f'Envio excede o limite de {readable}.', 'envio-muito-grande')

    @app.errorhandler(HTTPException)
    def handle_http_exception(exc: HTTPException):
        return _problem(exc.code or 500, exc.description or 'Erro HTTP', 'erro-http')

    @app.errorhandler(Exception)
    def handle_unexpected(exc: Exception):
        logger.exception('Erro não tratado durante a triagem')
        return _problem(500, 'Erro interno ao processar o chamado.', 'erro-interno')

    return app


def main() -> None:
    """Run the development server. Use a WSGI server in production."""
    logging.basicConfig(level=logging.INFO)
    create_app().run(host=config.API_HOST, port=config.API_PORT, debug=False)


if __name__ == '__main__':
    main()
