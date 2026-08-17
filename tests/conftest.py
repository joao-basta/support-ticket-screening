"""Shared pytest fixtures for the LDP test suite."""

import io
import zipfile
from pathlib import Path
from typing import Callable, Sequence

import pytest

from ldp.api.app import create_app
from ldp.core.ticket_source import Ticket

#: A realistic legacy GLPI body: the narrative in detalhe1 carries the affected
#: phone and the time, while later blocks carry the *caller's* contact details
#: plus decoy values that must never reach the evidence rules.
GLPI_SAMPLE = """Detalhes:
detalhe1 -> Prezado suporte, o agente matricula p125413 relatou que a ligacao caiu no meio do atendimento as 14:30. O telefone afetado e (11) 98765-4321.
detalhe2 -> 192.168.240.90
detalhe3 -> TEL: 987654321
detalhe4 -> Aguardando Retorno do Cliente
detalhe5 -> 123.456.789-00

Info Solicitante:
contatonome -> Maria Silva
contatotelefone -> (21) 3344-5566
contatoemail -> p999888@corp.caixa.gov.br

Categorizacao:
categoria -> Incidente - Telefonia / VOIP
sla -> 4 Horas Uteis

Info Arquivo:
anexo -> Nenhum anexo informado
"""

#: One log line per known error signature, keyed by the symptom it implies.
SIGNATURE_LINES = {
    'FALHA_INTERFACE_UX': [
        "ERROR: Uncaught TypeError: Cannot read properties of null (reading 'value')",
        "FATAL: JavaScript heap out of memory",
        "SERVER_RESPONSE: 500 Internal Server Error on /api/reports",
    ],
    'FALHA_CHAMADA_AUDIO': [
        "NETWORK_METRIC: High Jitter detected: 350ms",
        "VOIP_STATUS: UDP Packet Loss exceeded 15%",
        "SIP/408: Request Timeout - No response from client endpoint",
    ],
    'FALHA_FILA_ROTEAMENTO': [
        "ACD_QUEUE: ERROR - QueueID 7 overflow. Max capacity reached.",
        "AGENT_STATE: Mismatch - Agent 3012 state is 'Available' but channel is not open.",
        "ROUTING_ENGINE: No available agent found for skill 'Vendas_Premium'",
    ],
}


@pytest.fixture
def glpi_sample() -> str:
    """A full legacy GLPI ticket body."""
    return GLPI_SAMPLE


@pytest.fixture
def make_ticket() -> Callable[..., Ticket]:
    """Factory building a `Ticket` without touching disk."""
    def _make(text: str, source_id: str = 'teste', attachments: Sequence[Path] = ()) -> Ticket:
        return Ticket(source_id=source_id, raw_text=text, attachments=tuple(attachments))
    return _make


@pytest.fixture
def write_file(tmp_path: Path) -> Callable[..., Path]:
    """Factory writing a file into the test's tmp dir with a chosen encoding."""
    def _write(name: str, content: str, encoding: str = 'utf-8') -> Path:
        path = tmp_path / name
        path.write_bytes(content.encode(encoding))
        return path
    return _write


@pytest.fixture
def make_log(tmp_path: Path) -> Callable[..., Path]:
    """Factory writing a log file from a sequence of lines."""
    def _make(lines: Sequence[str], name: str = 'app.txt') -> Path:
        path = tmp_path / name
        path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
        return path
    return _make


@pytest.fixture
def make_zip(tmp_path: Path) -> Callable[..., Path]:
    """Factory building a .zip archive from a {name: content} mapping."""
    def _make(members: dict, name: str = 'anexos.zip') -> Path:
        path = tmp_path / name
        with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as archive:
            for member_name, content in members.items():
                archive.writestr(member_name, content)
        return path
    return _make


@pytest.fixture
def app():
    """Flask app configured for testing."""
    return create_app({'TESTING': True})


@pytest.fixture
def client(app):
    """Flask test client."""
    return app.test_client()


@pytest.fixture
def upload() -> Callable[..., tuple]:
    """Factory producing a multipart file tuple for the test client."""
    def _upload(content: bytes, filename: str) -> tuple:
        return (io.BytesIO(content), filename)
    return _upload
