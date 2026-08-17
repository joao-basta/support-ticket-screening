"""Ticket ingestion (Port & Adapter).

`TicketSource` is the port: everything downstream is agnostic to whether a
ticket came from disk, from an HTTP upload, or from the Service Desk API. The
adapters differ only in how they obtain `(id, text, attachment_paths)`.

Hardening applied here
----------------------
* Ticket bodies are decoded through an encoding ladder. Legacy GLPI exports are
  routinely cp1252, and `read_text(encoding='utf-8')` with no fallback crashed
  on the first accented character.
* Log extensions cover `.log`, `.csv`, `.json` and `.out` as well as `.txt`.
  Previously anything but `.txt` was classified as "other" and silently skipped
  by the forensic pass, including files extracted from an archive.
* Archive extraction validates containment with `Path.is_relative_to` instead
  of a string prefix comparison (which treats `/tmp/ldp_abcdef` as living
  inside `/tmp/ldp_abc`) and enforces member-count, total-size and compression
  ratio limits against zip bombs.
* Every failure raises a typed `LdpError` with a pt-BR message instead of
  surfacing a raw traceback.
"""

import logging
import random
import shutil
import tempfile
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Protocol, Sequence, Tuple

from ldp import config
from ldp.core.errors import AttachmentError, TicketLoadError

logger = logging.getLogger(__name__)

# Re-exported for backwards compatibility with callers that imported them here.
LOG_EXTENSIONS = config.LOG_EXTENSIONS
VISUAL_EXTENSIONS = config.VISUAL_EXTENSIONS
ARCHIVE_EXTENSIONS = config.ARCHIVE_EXTENSIONS


@dataclass(frozen=True)
class Ticket:
    """A ticket ready for the pipeline, wherever it came from."""

    source_id: str
    raw_text: str
    attachments: Tuple[Path, ...] = ()


@dataclass(frozen=True)
class AttachmentBundle:
    """Attachments split by their role in the pipeline."""

    log_files: Tuple[Path, ...]
    visual_files: Tuple[Path, ...]
    other_files: Tuple[Path, ...]

    @property
    def total(self) -> int:
        return len(self.log_files) + len(self.visual_files) + len(self.other_files)


class TicketSource(Protocol):
    """Port: any ticket origin exposes only `load()`."""

    def load(self) -> Ticket: ...


def read_text_file(path: Path) -> str:
    """Read a text file, trying each encoding in `config.TEXT_ENCODINGS`.

    The last encoding in the ladder is applied with `errors='replace'` so a
    genuinely mixed-encoding file degrades to replacement characters instead of
    aborting the analysis.

    Raises:
        TicketLoadError: if the path is missing, is a directory, or is unreadable.
    """
    if not path.exists():
        raise TicketLoadError(
            f"Arquivo de chamado não encontrado: {path}",
            detail='caminho inexistente',
        )
    if path.is_dir():
        raise TicketLoadError(
            f"O caminho informado é um diretório, não um arquivo: {path}",
        )

    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise TicketLoadError(
            f"Não foi possível ler o arquivo: {path}", detail=str(exc)
        ) from exc

    for encoding in config.TEXT_ENCODINGS[:-1]:
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue

    fallback = config.TEXT_ENCODINGS[-1]
    logger.warning("Encoding não identificado em %s; decodificando como %s com substituição.",
                   path.name, fallback)
    return raw.decode(fallback, errors='replace')


def classify_attachments(paths: Sequence[Path]) -> AttachmentBundle:
    """Split already-resolved attachment paths by role, using the file suffix."""
    logs: List[Path] = []
    visuals: List[Path] = []
    others: List[Path] = []
    for path in paths:
        suffix = path.suffix.lower()
        if suffix in config.LOG_EXTENSIONS:
            logs.append(path)
        elif suffix in config.VISUAL_EXTENSIONS:
            visuals.append(path)
        else:
            others.append(path)
    return AttachmentBundle(tuple(logs), tuple(visuals), tuple(others))


def _extract_zip(zip_path: Path, temp_dirs: List[Path]) -> Path:
    """Extract an archive into a fresh temp directory, safely.

    Rejects path traversal (Zip Slip) and refuses archives that exceed the
    member-count, uncompressed-size or compression-ratio limits in `config`.

    Raises:
        AttachmentError: if the archive is corrupt or trips a safety limit.
    """
    tmp_dir = Path(tempfile.mkdtemp(prefix='ldp_'))
    temp_dirs.append(tmp_dir)
    resolved_root = tmp_dir.resolve()

    try:
        with zipfile.ZipFile(zip_path) as archive:
            members = archive.infolist()
            if len(members) > config.ZIP_MAX_MEMBERS:
                raise AttachmentError(
                    f"Anexo '{zip_path.name}' contém arquivos demais "
                    f"({len(members)} > {config.ZIP_MAX_MEMBERS}).",
                    detail='limite de membros excedido',
                )

            total_uncompressed = sum(member.file_size for member in members)
            if total_uncompressed > config.ZIP_MAX_UNCOMPRESSED_BYTES:
                raise AttachmentError(
                    f"Anexo '{zip_path.name}' excede o tamanho máximo descompactado.",
                    detail=f'{total_uncompressed} bytes',
                )

            total_compressed = sum(member.compress_size for member in members) or 1
            if total_uncompressed / total_compressed > config.ZIP_MAX_COMPRESSION_RATIO:
                raise AttachmentError(
                    f"Anexo '{zip_path.name}' tem taxa de compressão suspeita "
                    "e foi recusado por segurança.",
                    detail='possível zip bomb',
                )

            for member in members:
                if member.is_dir():
                    continue
                destination = (tmp_dir / member.filename).resolve()
                if not destination.is_relative_to(resolved_root):
                    logger.warning("Membro ignorado por path traversal em %s: %s",
                                   zip_path.name, member.filename)
                    continue
                archive.extract(member, tmp_dir)
    except zipfile.BadZipFile as exc:
        raise AttachmentError(
            f"Anexo '{zip_path.name}' não é um arquivo .zip válido.", detail=str(exc)
        ) from exc
    except OSError as exc:
        raise AttachmentError(
            f"Falha ao extrair o anexo '{zip_path.name}'.", detail=str(exc)
        ) from exc

    return tmp_dir


def resolve_attachments(paths: Sequence[Path], temp_dirs: List[Path]) -> Tuple[Path, ...]:
    """Resolve attachment paths, expanding archives into tracked temp dirs.

    A missing attachment is logged and skipped rather than aborting the
    screening: partial evidence is still worth analysing.
    """
    resolved: List[Path] = []
    for path in paths:
        if not path.exists():
            logger.warning("Anexo não encontrado, seguindo sem ele: %s", path)
            continue
        if path.suffix.lower() in config.ARCHIVE_EXTENSIONS:
            extracted_dir = _extract_zip(path, temp_dirs)
            resolved.extend(sorted(p for p in extracted_dir.rglob('*') if p.is_file()))
        else:
            resolved.append(path)
    return tuple(resolved)


class _TempDirTicketSource:
    """Base for adapters that extract archives and must clean up afterwards."""

    def __init__(self) -> None:
        self._temp_dirs: List[Path] = []

    def __enter__(self) -> "_TempDirTicketSource":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def close(self) -> None:
        """Remove every temp directory created during resolution."""
        for tmp_dir in self._temp_dirs:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        self._temp_dirs.clear()


class LocalTicketSource(_TempDirTicketSource):
    """Adapter reading the ticket text and attachments from disk."""

    def __init__(self, text: Optional[str] = None, text_file: Optional[Path] = None,
                 attachment_paths: Sequence[Path] = ()) -> None:
        super().__init__()
        self._text = text
        self._text_file = text_file
        self._attachment_paths = list(attachment_paths)

    def load(self) -> Ticket:
        raw_text = self._resolve_text()
        if not raw_text.strip():
            raise TicketLoadError("O chamado está vazio: não há texto para analisar.")
        attachments = resolve_attachments(self._attachment_paths, self._temp_dirs)
        source_id = self._text_file.stem if self._text_file else uuid.uuid4().hex[:8]
        return Ticket(source_id=source_id, raw_text=raw_text, attachments=attachments)

    def _resolve_text(self) -> str:
        if self._text_file is not None:
            return read_text_file(self._text_file)
        return self._text or ''


TicketPayload = Dict[str, object]
Fetcher = Callable[[], TicketPayload]


def mock_pool_fetcher(pool_dir: Optional[Path] = None) -> TicketPayload:
    """Simulate a Service Desk API response from the local mock pool.

    Reads a random ticket/log pair produced by `tests/mock_generator.py`. A real
    fetcher must return the same payload shape so nothing downstream changes.

    Raises:
        TicketLoadError: if the pool has not been generated yet.
    """
    directory = pool_dir or config.MOCK_POOL_DIR
    ticket_files = sorted(directory.glob('req*.md'))
    if not ticket_files:
        raise TicketLoadError(
            f"Nenhum chamado mock encontrado em '{directory}'.",
            detail='execute `py tests/mock_generator.py` antes de usar --api-source',
        )
    ticket_file = random.choice(ticket_files)
    log_file = ticket_file.with_name(f"{ticket_file.stem}_log.txt")
    return {
        'id': ticket_file.stem,
        'text': read_text_file(ticket_file),
        'attachment_paths': [str(log_file)] if log_file.exists() else [],
    }


class ApiTicketSource(_TempDirTicketSource):
    """Adapter for the 2CX Service Desk REST API (Fase 8).

    Performs no HTTP today. The injected `fetcher` returns the payload; swapping
    in a real HTTP call is the entire remaining work, and requires no change
    anywhere else in the pipeline.
    """

    def __init__(self, fetcher: Fetcher = mock_pool_fetcher) -> None:
        super().__init__()
        self._fetcher = fetcher

    def load(self) -> Ticket:
        payload = self._fetcher()

        source_id = payload.get('id')
        raw_text = payload.get('text')
        if not source_id or not raw_text:
            raise TicketLoadError(
                "Payload do Service Desk incompleto: 'id' e 'text' são obrigatórios.",
                detail=f'campos recebidos: {sorted(payload)}',
            )

        raw_paths = payload.get('attachment_paths') or []
        attachments = resolve_attachments([Path(str(p)) for p in raw_paths], self._temp_dirs)
        return Ticket(
            source_id=str(source_id),
            raw_text=str(raw_text),
            attachments=attachments,
        )
