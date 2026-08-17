"""Unit tests for `ldp.core.ticket_source` (ingestion port and its adapters).

These pin the hardening the module claims in its docstring: the encoding
ladder, the widened log-extension set, Zip Slip containment, zip bomb guards,
non-fatal missing attachments and temp-dir cleanup.
"""

import logging
import tempfile
import zipfile
from pathlib import Path

import pytest

from ldp import config
from ldp.core.errors import AttachmentError, TicketLoadError
from ldp.core.ticket_source import (
    ApiTicketSource,
    AttachmentBundle,
    LocalTicketSource,
    Ticket,
    classify_attachments,
    mock_pool_fetcher,
    read_text_file,
    resolve_attachments,
)

MODULE_LOGGER = 'ldp.core.ticket_source'

#: Accented pt-BR body used across the encoding ladder cases. Every accented
#: character below becomes a byte that is invalid UTF-8 once encoded as cp1252,
#: so the ladder really has to fall through instead of guessing right by luck.
ACCENTED_BODY = (
    "Prezado suporte, a ligação caiu às 14:30 durante o atendimento.\n"
    "O ramal não completa chamadas e a gravação está inaudível.\n"
)


# ---------------------------------------------------------------------------
# read_text_file - encoding ladder
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('encoding', ['utf-8', 'utf-8-sig', 'cp1252'])
def test_read_text_file_decodes_every_ladder_encoding(write_file, encoding):
    """REGRESSION: the same accented body reads back intact from utf-8, utf-8-sig
    (BOM) and cp1252. The cp1252 case used to crash with UnicodeDecodeError."""
    path = write_file(f'chamado_{encoding}.txt', ACCENTED_BODY, encoding=encoding)

    assert read_text_file(path) == ACCENTED_BODY


def test_read_text_file_strips_the_utf8_bom(write_file):
    """A utf-8-sig file must not leak the BOM character into the ticket text."""
    path = write_file('com_bom.txt', ACCENTED_BODY, encoding='utf-8-sig')

    assert path.read_bytes().startswith(b'\xef\xbb\xbf')
    assert not read_text_file(path).startswith('﻿')


def test_read_text_file_falls_back_with_replacement_and_warns(tmp_path, caplog):
    """Bytes that no strict ladder encoding accepts degrade to the last encoding
    (with replacement) and log a warning instead of aborting the analysis."""
    path = tmp_path / 'misto.txt'
    # \x81 and \x8d are undefined in cp1252 and invalid as UTF-8 lead bytes.
    path.write_bytes(b'Erro \x81\x8d cr\xedtico no ramal')

    with caplog.at_level(logging.WARNING, logger=MODULE_LOGGER):
        text = read_text_file(path)

    assert 'crítico' in text
    assert any('Encoding' in record.message for record in caplog.records)


def test_read_text_file_missing_path_raises(tmp_path):
    """A missing ticket file raises TicketLoadError, never FileNotFoundError."""
    with pytest.raises(TicketLoadError) as excinfo:
        read_text_file(tmp_path / 'nao_existe.txt')

    assert 'não encontrado' in excinfo.value.message
    assert excinfo.value.detail == 'caminho inexistente'


def test_read_text_file_directory_raises(tmp_path):
    """Pointing the reader at a directory raises TicketLoadError, not IsADirectoryError."""
    directory = tmp_path / 'pasta'
    directory.mkdir()

    with pytest.raises(TicketLoadError) as excinfo:
        read_text_file(directory)

    assert 'diretório' in excinfo.value.message


# ---------------------------------------------------------------------------
# LocalTicketSource
# ---------------------------------------------------------------------------

def test_local_source_loads_inline_text():
    """`--text` produces a Ticket whose body is the string handed in."""
    ticket = LocalTicketSource(text='A ligação caiu às 14:30.').load()

    assert isinstance(ticket, Ticket)
    assert ticket.raw_text == 'A ligação caiu às 14:30.'
    assert ticket.attachments == ()
    assert ticket.source_id  # a random id is generated when there is no file


def test_local_source_loads_text_file_and_derives_source_id(write_file):
    """`--text-file` reads through the encoding ladder and names the ticket
    after the file stem."""
    path = write_file('req0000123.txt', ACCENTED_BODY, encoding='cp1252')

    ticket = LocalTicketSource(text_file=path).load()

    assert ticket.raw_text == ACCENTED_BODY
    assert ticket.source_id == 'req0000123'


def test_local_source_inline_id_is_hex_and_unique():
    """Without a file the source_id is a fresh short hex token per load."""
    first = LocalTicketSource(text='texto').load().source_id
    second = LocalTicketSource(text='texto').load().source_id

    assert first != second
    assert len(first) == 8
    assert all(char in '0123456789abcdef' for char in first)


@pytest.mark.parametrize('text', ['', '   ', '\n\t  \n'])
def test_local_source_empty_text_raises(text):
    """A blank body is refused up front: there is nothing to screen."""
    with pytest.raises(TicketLoadError) as excinfo:
        LocalTicketSource(text=text).load()

    assert 'vazio' in excinfo.value.message


def test_local_source_empty_text_file_raises(write_file):
    """A whitespace-only ticket file is as empty as an empty `--text`."""
    path = write_file('vazio.txt', '   \n\n')

    with pytest.raises(TicketLoadError):
        LocalTicketSource(text_file=path).load()


def test_local_source_text_file_wins_over_inline_text(write_file):
    """When both are supplied the file is authoritative."""
    path = write_file('req42.txt', 'conteudo do arquivo')

    ticket = LocalTicketSource(text='inline', text_file=path).load()

    assert ticket.raw_text == 'conteudo do arquivo'


# ---------------------------------------------------------------------------
# classify_attachments
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('name, bucket', [
    ('app.txt', 'log_files'),
    ('app.log', 'log_files'),      # REGRESSION: .log used to fall into "others"
    ('metricas.csv', 'log_files'),
    ('trace.json', 'log_files'),
    ('console.out', 'log_files'),
    ('tela.png', 'visual_files'),
    ('tela.jpg', 'visual_files'),
    ('tela.jpeg', 'visual_files'),
    ('evidencia.pdf', 'visual_files'),
    ('malware.exe', 'other_files'),
    ('relatorio.docx', 'other_files'),
    ('sem_extensao', 'other_files'),
])
def test_classify_attachments_routes_by_extension(name, bucket):
    """Each suffix lands in exactly one bucket of the AttachmentBundle."""
    path = Path(name)
    bundle = classify_attachments([path])

    assert getattr(bundle, bucket) == (path,)
    assert bundle.total == 1


@pytest.mark.parametrize('name', ['APP.LOG', 'Tela.PNG'])
def test_classify_attachments_is_case_insensitive(name):
    """Suffix matching lowercases first, so upper-case exports still classify."""
    bundle = classify_attachments([Path(name)])

    assert bundle.other_files == ()
    assert bundle.total == 1


def test_classify_attachments_preserves_order_and_totals():
    """Ordering inside each bucket follows the input order; total sums buckets."""
    paths = [Path('a.log'), Path('b.png'), Path('c.exe'), Path('d.txt')]

    bundle = classify_attachments(paths)

    assert bundle.log_files == (Path('a.log'), Path('d.txt'))
    assert bundle.visual_files == (Path('b.png'),)
    assert bundle.other_files == (Path('c.exe'),)
    assert bundle.total == 4


def test_classify_attachments_empty_bundle():
    """No attachments yields an empty bundle whose total is zero."""
    bundle = classify_attachments([])

    assert bundle == AttachmentBundle((), (), ())
    assert bundle.total == 0


# ---------------------------------------------------------------------------
# resolve_attachments - plain files
# ---------------------------------------------------------------------------

def test_resolve_attachments_passes_plain_files_through(make_log):
    """Non-archive attachments are returned untouched and create no temp dir."""
    log = make_log(['ERROR: falha'], name='app.log')
    temp_dirs = []

    resolved = resolve_attachments([log], temp_dirs)

    assert resolved == (log,)
    assert temp_dirs == []


def test_resolve_attachments_skips_missing_path_with_warning(tmp_path, make_log, caplog):
    """A missing attachment is a warning, not a fatal error: partial evidence
    is still worth analysing."""
    log = make_log(['ERROR: falha'], name='app.log')
    missing = tmp_path / 'sumiu.log'
    temp_dirs = []

    with caplog.at_level(logging.WARNING, logger=MODULE_LOGGER):
        resolved = resolve_attachments([missing, log], temp_dirs)

    assert resolved == (log,)
    assert any('Anexo não encontrado' in record.message for record in caplog.records)


# ---------------------------------------------------------------------------
# resolve_attachments - archives
# ---------------------------------------------------------------------------

def test_resolve_attachments_extracts_and_classifies_zip_members(make_zip):
    """A .zip is expanded into a tracked temp dir and its members classify by
    their own extensions."""
    archive = make_zip({
        'app.log': 'ERROR: Uncaught TypeError\n',
        'tela.png': 'binario-falso',
        'notas.docx': 'texto',
        'sub/metricas.csv': 'jitter,350\n',
    })
    temp_dirs = []

    resolved = resolve_attachments([archive], temp_dirs)

    assert len(temp_dirs) == 1
    names = sorted(path.name for path in resolved)
    assert names == ['app.log', 'metricas.csv', 'notas.docx', 'tela.png']

    bundle = classify_attachments(resolved)
    assert sorted(p.name for p in bundle.log_files) == ['app.log', 'metricas.csv']
    assert [p.name for p in bundle.visual_files] == ['tela.png']
    assert [p.name for p in bundle.other_files] == ['notas.docx']
    assert (temp_dirs[0] / 'app.log').read_text(encoding='utf-8').startswith('ERROR')


def test_resolve_attachments_mixes_archive_and_plain_files(make_zip, make_log):
    """Archives and loose files can be supplied together in one call."""
    archive = make_zip({'dentro.log': 'ERROR\n'})
    loose = make_log(['ERROR: solto'], name='fora.log')
    temp_dirs = []

    resolved = resolve_attachments([archive, loose], temp_dirs)

    assert sorted(path.name for path in resolved) == ['dentro.log', 'fora.log']


def test_zip_slip_member_is_skipped_and_writes_nothing_outside(tmp_path, monkeypatch, caplog):
    """SECURITY: a member named '../evil.txt' must not escape the extraction
    directory; it is skipped with a warning and the safe member still lands."""
    sandbox = tmp_path / 'tmproot'
    sandbox.mkdir()
    monkeypatch.setattr(tempfile, 'tempdir', str(sandbox))

    archive = tmp_path / 'malicioso.zip'
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as handle:
        handle.writestr('../evil.txt', 'conteudo malicioso')
        handle.writestr('ok.log', 'ERROR: legitimo\n')

    temp_dirs = []
    with caplog.at_level(logging.WARNING, logger=MODULE_LOGGER):
        resolved = resolve_attachments([archive], temp_dirs)

    extraction_dir = temp_dirs[0]
    assert extraction_dir.parent == sandbox
    # Nothing was written above the extraction dir...
    assert not (sandbox / 'evil.txt').exists()
    assert not (tmp_path / 'evil.txt').exists()
    # ...and the traversal member never reaches the pipeline.
    assert [path.name for path in resolved] == ['ok.log']
    assert not any(path.name == 'evil.txt' for path in resolved)
    assert any('path traversal' in record.message for record in caplog.records)


def test_zip_member_limit_raises_attachment_error(make_zip, monkeypatch):
    """ZIP BOMB GUARD: too many members is refused before anything is extracted."""
    archive = make_zip({f'log{index}.log': 'ERROR\n' for index in range(4)})
    monkeypatch.setattr(config, 'ZIP_MAX_MEMBERS', 2)
    temp_dirs = []

    with pytest.raises(AttachmentError) as excinfo:
        resolve_attachments([archive], temp_dirs)

    assert excinfo.value.detail == 'limite de membros excedido'
    assert 'arquivos demais' in excinfo.value.message


def test_zip_uncompressed_size_limit_raises_attachment_error(make_zip, monkeypatch):
    """ZIP BOMB GUARD: the total uncompressed size cap is enforced."""
    archive = make_zip({'grande.log': 'A' * 4096})
    monkeypatch.setattr(config, 'ZIP_MAX_UNCOMPRESSED_BYTES', 16)
    temp_dirs = []

    with pytest.raises(AttachmentError) as excinfo:
        resolve_attachments([archive], temp_dirs)

    assert 'tamanho máximo descompactado' in excinfo.value.message


def test_zip_compression_ratio_limit_raises_attachment_error(make_zip, monkeypatch):
    """ZIP BOMB GUARD: a highly compressible payload trips the ratio guard."""
    archive = make_zip({'bomba.log': 'A' * 200_000})
    monkeypatch.setattr(config, 'ZIP_MAX_COMPRESSION_RATIO', 5)
    temp_dirs = []

    with pytest.raises(AttachmentError) as excinfo:
        resolve_attachments([archive], temp_dirs)

    assert excinfo.value.detail == 'possível zip bomb'


def test_corrupt_zip_raises_attachment_error(tmp_path):
    """A renamed/corrupt .zip fails as a typed AttachmentError, not BadZipFile."""
    fake = tmp_path / 'anexos.zip'
    fake.write_text('isto nao e um zip, e apenas texto', encoding='utf-8')
    temp_dirs = []

    with pytest.raises(AttachmentError) as excinfo:
        resolve_attachments([fake], temp_dirs)

    assert "não é um arquivo .zip válido" in excinfo.value.message


def test_failed_extraction_still_tracks_temp_dir_for_cleanup(tmp_path):
    """Even when extraction aborts, the temp dir is registered so the caller's
    cleanup removes it."""
    fake = tmp_path / 'anexos.zip'
    fake.write_bytes(b'nao-zip')
    temp_dirs = []

    with pytest.raises(AttachmentError):
        resolve_attachments([fake], temp_dirs)

    assert len(temp_dirs) == 1


# ---------------------------------------------------------------------------
# Temp dir lifecycle
# ---------------------------------------------------------------------------

def test_local_source_context_manager_removes_temp_dirs(make_zip):
    """Leaving the `with` block deletes every temp dir created for archives."""
    archive = make_zip({'app.log': 'ERROR\n'})

    with LocalTicketSource(text='a ligação caiu', attachment_paths=[archive]) as source:
        ticket = source.load()
        extracted = Path(ticket.attachments[0])
        assert extracted.exists()
        created = list(source._temp_dirs)

    assert created
    assert not extracted.exists()
    assert all(not directory.exists() for directory in created)


def test_close_is_idempotent(make_zip):
    """Calling close() twice is safe and leaves no tracked temp dirs behind."""
    archive = make_zip({'app.log': 'ERROR\n'})
    source = LocalTicketSource(text='texto', attachment_paths=[archive])
    source.load()

    source.close()
    source.close()

    assert source._temp_dirs == []


def test_context_manager_cleans_up_even_when_load_raises():
    """An exception inside the block does not leak the temp-dir bookkeeping."""
    source = LocalTicketSource(text='')
    with pytest.raises(TicketLoadError):
        with source:
            source.load()

    assert source._temp_dirs == []


# ---------------------------------------------------------------------------
# ApiTicketSource
# ---------------------------------------------------------------------------

def test_api_source_builds_ticket_from_injected_fetcher(make_log):
    """Happy path: the injected fetcher's payload becomes a Ticket unchanged."""
    log = make_log(['NETWORK_METRIC: High Jitter detected: 350ms'], name='req99_log.txt')
    payload = {
        'id': 'req0099',
        'text': 'A ligação caiu às 14:30.',
        'attachment_paths': [str(log)],
    }

    with ApiTicketSource(fetcher=lambda: payload) as source:
        ticket = source.load()

    assert ticket.source_id == 'req0099'
    assert ticket.raw_text == 'A ligação caiu às 14:30.'
    assert ticket.attachments == (log,)


def test_api_source_without_attachments_key(make_log):
    """A payload with no 'attachment_paths' yields an attachment-less Ticket."""
    ticket = ApiTicketSource(fetcher=lambda: {'id': '1', 'text': 'texto'}).load()

    assert ticket.attachments == ()


def test_api_source_coerces_non_string_fields():
    """Numeric ids and bodies are coerced to str so downstream stays typed."""
    ticket = ApiTicketSource(fetcher=lambda: {'id': 42, 'text': 12345}).load()

    assert ticket.source_id == '42'
    assert ticket.raw_text == '12345'


def test_api_source_expands_zip_attachment(make_zip):
    """Archives referenced by the API payload are extracted like local ones."""
    archive = make_zip({'api.log': 'ERROR\n'})

    with ApiTicketSource(fetcher=lambda: {
        'id': 'req1', 'text': 'texto', 'attachment_paths': [str(archive)],
    }) as source:
        ticket = source.load()
        assert [path.name for path in ticket.attachments] == ['api.log']


@pytest.mark.parametrize('payload', [
    {'text': 'A ligação caiu.'},                 # no id
    {'id': 'req0001'},                           # no text
    {},                                          # nothing at all
    {'id': '', 'text': 'A ligação caiu.'},       # blank id
    {'id': 'req0001', 'text': ''},               # blank text
])
def test_api_source_incomplete_payload_raises(payload):
    """'id' and 'text' are mandatory; anything falsy is refused as incomplete."""
    with pytest.raises(TicketLoadError) as excinfo:
        ApiTicketSource(fetcher=lambda: payload).load()

    assert 'incompleto' in excinfo.value.message
    assert 'campos recebidos' in excinfo.value.detail


# ---------------------------------------------------------------------------
# mock_pool_fetcher
# ---------------------------------------------------------------------------

def test_mock_pool_fetcher_empty_pool_raises(tmp_path):
    """An ungenerated mock pool gives an actionable TicketLoadError."""
    empty = tmp_path / 'pool'
    empty.mkdir()

    with pytest.raises(TicketLoadError) as excinfo:
        mock_pool_fetcher(empty)

    assert 'Nenhum chamado mock encontrado' in excinfo.value.message
    assert 'mock_generator.py' in excinfo.value.detail


def test_mock_pool_fetcher_missing_pool_dir_raises(tmp_path):
    """A pool directory that does not exist behaves like an empty one."""
    with pytest.raises(TicketLoadError):
        mock_pool_fetcher(tmp_path / 'inexistente')


def test_mock_pool_fetcher_builds_payload_with_log(tmp_path):
    """A req*.md file plus its `_log.txt` sibling becomes a complete payload."""
    pool = tmp_path / 'pool'
    pool.mkdir()
    # write_bytes, not write_text: the latter would rewrite '\n' as '\r\n' on Windows.
    (pool / 'req0001.md').write_bytes(ACCENTED_BODY.encode('cp1252'))
    log = pool / 'req0001_log.txt'
    log.write_bytes(b'ERROR: falha\n')

    payload = mock_pool_fetcher(pool)

    assert payload['id'] == 'req0001'
    assert payload['text'] == ACCENTED_BODY
    assert payload['attachment_paths'] == [str(log)]


def test_mock_pool_fetcher_without_sibling_log(tmp_path):
    """A ticket with no `_log.txt` sibling yields an empty attachment list."""
    pool = tmp_path / 'pool'
    pool.mkdir()
    (pool / 'req0002.md').write_text('texto do chamado', encoding='utf-8')

    payload = mock_pool_fetcher(pool)

    assert payload['id'] == 'req0002'
    assert payload['attachment_paths'] == []


def test_mock_pool_fetcher_feeds_api_source(tmp_path):
    """The default fetcher shape is exactly what ApiTicketSource consumes."""
    pool = tmp_path / 'pool'
    pool.mkdir()
    (pool / 'req0003.md').write_text('A ligação caiu às 14:30.', encoding='utf-8')

    with ApiTicketSource(fetcher=lambda: mock_pool_fetcher(pool)) as source:
        ticket = source.load()

    assert ticket.source_id == 'req0003'
    assert ticket.raw_text == 'A ligação caiu às 14:30.'
