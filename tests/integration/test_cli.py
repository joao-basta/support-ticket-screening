"""End-to-end tests for the CLI, driven as a real subprocess.

Everything here runs `python -m ldp` in a child process, exactly the way the
RPA robot and any CI job will call it. Nothing is imported from `ldp` for the
assertions on purpose: the contract under test is the *process* contract -
exit code, stdout, stderr - not the Python API.

The exit code matrix is the automation contract (see `config.EXIT_*`):
0 aprovado, 1 bloqueado, 2 atencao, 3 erro. Renumbering it silently would break
every downstream robot, so it is pinned here first.

Every test spawns an interpreter that imports scikit-learn, so the whole module
is marked `slow`.
"""

import json
import subprocess
import sys
from pathlib import Path
from typing import Sequence

import pytest

from tests.conftest import SIGNATURE_LINES

pytestmark = pytest.mark.slow

#: Repo root: tests/integration/test_cli.py -> tests/integration -> tests -> root.
REPO_ROOT = Path(__file__).resolve().parents[2]

#: Banner printed on stdout in report mode only; --json must stay parseable.
BANNER = '--- 2CX / Caixa Log Diagnostic Pipeline (NLP Engine) ---'

#: A path that is guaranteed never to exist, used to force the error branch.
MISSING_TICKET_PATH = 'tests/integration/_arquivo_que_nao_existe.txt'

# Ticket bodies whose outcome is pinned by the matrix below.
APPROVED_TEXT = 'a ligacao caiu as 14:30, telefone 11987654321'
BLOCKED_TEXT = 'sem audio na ligacao as 14:30, protocolo 1234567890'
ATTENTION_TEXT = 'bom dia, solicito informacao sobre ferias e folha de pagamento'

EXPECTED_JSON_KEYS = {
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
}


def run_cli(*args: str) -> subprocess.CompletedProcess:
    """Invoke `python -m ldp` with `args` from the repo root and capture both streams."""
    return subprocess.run(
        [sys.executable, '-m', 'ldp', *args],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        encoding='utf-8',
        timeout=300,
    )


# ---------------------------------------------------------
# Shared runs: module-scoped so several assertions share one subprocess.
# ---------------------------------------------------------

@pytest.fixture(scope='module')
def approved_report_run() -> subprocess.CompletedProcess:
    """One report-mode run of an approvable ticket."""
    return run_cli('--text', APPROVED_TEXT)


@pytest.fixture(scope='module')
def approved_json_run() -> subprocess.CompletedProcess:
    """The same approvable ticket, in --json mode."""
    return run_cli('--json', '--text', APPROVED_TEXT)


@pytest.fixture(scope='module')
def missing_file_run() -> subprocess.CompletedProcess:
    """A run pointed at a --text-file that does not exist."""
    return run_cli('--text-file', MISSING_TICKET_PATH)


# ---------------------------------------------------------
# Smoke: the package is runnable at all
# ---------------------------------------------------------

def test_module_is_runnable() -> None:
    """`python -m ldp --help` succeeds: the package stays importable as a module.

    Regression: the src-layout package was once not installed/importable, so
    `python -m ldp` died with ModuleNotFoundError before parsing anything.
    """
    completed = run_cli('--help')

    assert completed.returncode == 0
    assert 'usage: ldp' in completed.stdout
    assert 'Traceback' not in completed.stderr


# ---------------------------------------------------------
# Exit code matrix (automation contract)
# ---------------------------------------------------------

@pytest.mark.parametrize(('args', 'expected_code'), [
    pytest.param(('--text', APPROVED_TEXT), 0, id='0-aprovado'),
    pytest.param(('--text', BLOCKED_TEXT), 1, id='1-bloqueado'),
    pytest.param(('--text', ATTENTION_TEXT), 2, id='2-atencao-sintoma-desconhecido'),
    pytest.param(('--text-file', MISSING_TICKET_PATH), 3, id='3-erro-arquivo-ausente'),
])
def test_exit_code_matrix(args: Sequence[str], expected_code: int) -> None:
    """Pins the exit code contract: 0 approved, 1 blocked, 2 attention, 3 error."""
    completed = run_cli(*args)

    assert completed.returncode == expected_code


@pytest.mark.parametrize(('text', 'expected_code'), [
    pytest.param(APPROVED_TEXT, 0, id='tempo+telefone-alta-confianca-aprova'),
    pytest.param(BLOCKED_TEXT, 1, id='protocolo-nao-vale-como-telefone-bloqueia'),
])
def test_evidence_regressions(text: str, expected_code: int) -> None:
    """Regression: a HIGH-confidence phone approves, a bare protocol number does not.

    The masked protocol used to satisfy the "Número do Telefone/Alvo" rule,
    turning a ticket with no callable number into an APROVADO.
    """
    completed = run_cli('--text', text)

    assert completed.returncode == expected_code


# ---------------------------------------------------------
# Report mode
# ---------------------------------------------------------

def test_report_mode_prints_banner(approved_report_run: subprocess.CompletedProcess) -> None:
    """Report mode opens with the human-facing banner on stdout."""
    assert approved_report_run.stdout.startswith(BANNER)


def test_report_is_pt_br_and_shows_status_decoration(
    approved_report_run: subprocess.CompletedProcess,
) -> None:
    """The analyst report is pt-BR and carries the decorated status line."""
    stdout = approved_report_run.stdout

    assert '[STATUS]           : ✅ SUCESSO - PRONTO PARA ANÁLISE TÉCNICA' in stdout
    for label in ('[CHAMADO]', '[TEXTO ANALISADO]', '[SINTOMA INFERIDO]',
                  '[AÇÃO NECESSÁRIA]', '[EVIDÊNCIAS]'):
        assert label in stdout
    assert 'Horário da ocorrência' in stdout
    assert 'Número do Telefone/Alvo' in stdout


def test_blocked_report_shows_blocked_decoration_and_pending_items() -> None:
    """A blocked ticket shows the ❌ decoration and names what is missing."""
    completed = run_cli('--text', BLOCKED_TEXT)

    assert completed.returncode == 1
    assert '❌ BLOQUEADO - FALTAM DADOS' in completed.stdout
    assert 'Devolver chamado ao solicitante cobrando: Número do Telefone/Alvo' in completed.stdout
    assert '[FALTA] Número do Telefone/Alvo' in completed.stdout


# ---------------------------------------------------------
# JSON mode
# ---------------------------------------------------------

def test_json_mode_stdout_is_parseable(approved_json_run: subprocess.CompletedProcess) -> None:
    """--json writes a single parseable JSON document with the pt-BR keys."""
    payload = json.loads(approved_json_run.stdout)

    assert set(payload) == EXPECTED_JSON_KEYS
    assert payload['status'] == 'APROVADO'
    assert payload['sintoma']['codigo'] == 'FALHA_CHAMADA_AUDIO'
    assert payload['dados_sensiveis'] == {'TELEFONE': 1}
    assert payload['pendencias'] == []


def test_json_mode_omits_banner(approved_json_run: subprocess.CompletedProcess) -> None:
    """--json suppresses the banner so stdout stays machine-readable."""
    assert BANNER not in approved_json_run.stdout
    assert approved_json_run.stdout.lstrip().startswith('{')


def test_json_codigo_saida_matches_process_exit_code(
    approved_json_run: subprocess.CompletedProcess,
) -> None:
    """`codigo_saida` in the payload equals the real process exit code."""
    payload = json.loads(approved_json_run.stdout)

    assert payload['codigo_saida'] == approved_json_run.returncode == 0


def test_json_codigo_saida_matches_exit_code_when_blocked() -> None:
    """The payload/exit-code agreement also holds on the blocked path."""
    completed = run_cli('--json', '--text', BLOCKED_TEXT)
    payload = json.loads(completed.stdout)

    assert completed.returncode == 1
    assert payload['codigo_saida'] == completed.returncode
    assert payload['status'] == 'BLOQUEADO'
    assert payload['pendencias'] == ['Número do Telefone/Alvo']


# ---------------------------------------------------------
# Input handling
# ---------------------------------------------------------

def test_text_file_accepts_cp1252(tmp_path: Path) -> None:
    """A cp1252-encoded accented ticket file is decoded, not fatal.

    Regression: `read_text(encoding='utf-8')` crashed on the first accented
    byte of a legacy GLPI export, which is routinely cp1252.
    """
    ticket = tmp_path / 'req0042.txt'
    ticket.write_bytes(
        'A ligação caiu às 14:30 durante o atendimento. Não há áudio. '
        'Telefone afetado: (11) 98765-4321.'.encode('cp1252')
    )

    completed = run_cli('--json', '--text-file', str(ticket))
    payload = json.loads(completed.stdout)

    assert completed.returncode == 0
    assert 'Traceback' not in completed.stderr
    # The accents survived the decode and the file stem became the ticket id.
    assert payload['chamado'] == 'req0042'
    assert 'ligação caiu às 14:30' in payload['texto_analisado']
    assert '[TELEFONE_MASK]' in payload['texto_analisado']


def test_attachments_log_is_analyzed(make_log) -> None:
    """--attachments with a log file adds the forensic section to the report."""
    log_path = make_log(SIGNATURE_LINES['FALHA_CHAMADA_AUDIO'], name='app.txt')

    completed = run_cli('--text', APPROVED_TEXT, '--attachments', str(log_path))

    assert completed.returncode == 0
    assert '[ANEXOS]           : 1 log(s)' in completed.stdout
    assert '[ANÁLISE DE LOG]   : 3 ocorrência(s) em 1 arquivo(s):' in completed.stdout
    assert 'app.txt:1' in completed.stdout
    assert '[CONFIRMAÇÃO]      : Causa técnica no log confirma o sintoma relatado.' \
        in completed.stdout


# ---------------------------------------------------------
# Failure paths
# ---------------------------------------------------------

def test_missing_text_file_exits_with_error_code(
    missing_file_run: subprocess.CompletedProcess,
) -> None:
    """A --text-file that does not exist exits 3 (EXIT_ERROR)."""
    assert missing_file_run.returncode == 3


def test_missing_text_file_reports_on_stderr(
    missing_file_run: subprocess.CompletedProcess,
) -> None:
    """The '[ERRO]' line goes to stderr, never to stdout."""
    assert '[ERRO]' in missing_file_run.stderr
    assert 'Arquivo de chamado não encontrado' in missing_file_run.stderr
    assert '[ERRO]' not in missing_file_run.stdout


def test_missing_text_file_has_no_traceback(
    missing_file_run: subprocess.CompletedProcess,
) -> None:
    """Expected failures surface as a clean message, not a Python traceback."""
    assert 'Traceback' not in missing_file_run.stderr
    assert 'TicketLoadError' not in missing_file_run.stderr


def test_no_source_argument_is_an_argparse_error() -> None:
    """Calling with no source at all fails through argparse with usage text."""
    completed = run_cli()

    assert completed.returncode != 0
    # argparse's own error exit code.
    assert completed.returncode == 2
    assert 'usage: ldp' in completed.stderr
    assert 'informe --text, --text-file, --api-source ou --interactive' in completed.stderr
    assert completed.stdout == ''


def test_text_and_text_file_are_mutually_exclusive(tmp_path: Path) -> None:
    """--text and --text-file cannot be combined; argparse rejects the pair."""
    ticket = tmp_path / 'chamado.txt'
    ticket.write_text(APPROVED_TEXT, encoding='utf-8')

    completed = run_cli('--text', APPROVED_TEXT, '--text-file', str(ticket))

    assert completed.returncode == 2
    assert 'not allowed with argument --text' in completed.stderr
    assert completed.stdout == ''
