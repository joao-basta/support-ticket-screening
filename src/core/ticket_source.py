import random
import shutil
import tempfile
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Protocol, Sequence, Tuple

LOG_EXTENSIONS = frozenset({'.txt'})
VISUAL_EXTENSIONS = frozenset({'.png', '.jpg', '.jpeg', '.pdf'})
ARCHIVE_EXTENSIONS = frozenset({'.zip'})


@dataclass(frozen=True)
class Ticket:
    source_id: str
    raw_text: str
    attachments: Tuple[Path, ...] = ()


@dataclass(frozen=True)
class AttachmentBundle:
    log_files: Tuple[Path, ...]
    visual_files: Tuple[Path, ...]
    other_files: Tuple[Path, ...]


class TicketSource(Protocol):
    """Port: qualquer fonte de chamado (arquivo local, API do Service Desk, etc.)
    expõe apenas load() -> Ticket, mantendo o restante do pipeline agnóstico
    a de onde os dados vieram."""

    def load(self) -> Ticket: ...


def classify_attachments(paths: Sequence[Path]) -> AttachmentBundle:
    """Separa anexos já resolvidos (arquivos existentes no disco) por papel no pipeline."""
    logs: List[Path] = []
    visuals: List[Path] = []
    others: List[Path] = []
    for path in paths:
        suffix = path.suffix.lower()
        if suffix in LOG_EXTENSIONS:
            logs.append(path)
        elif suffix in VISUAL_EXTENSIONS:
            visuals.append(path)
        else:
            others.append(path)
    return AttachmentBundle(tuple(logs), tuple(visuals), tuple(others))


def _extract_zip(zip_path: Path, temp_dirs: List[Path]) -> Path:
    tmp_dir = Path(tempfile.mkdtemp(prefix='ldp_'))
    resolved_root = tmp_dir.resolve()
    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.infolist():
            dest = (tmp_dir / member.filename).resolve()
            if not str(dest).startswith(str(resolved_root)):
                continue  # proteção Zip Slip: ignora membro com path traversal
            zf.extract(member, tmp_dir)
    temp_dirs.append(tmp_dir)
    return tmp_dir


def resolve_attachments(paths: Sequence[Path], temp_dirs: List[Path]) -> Tuple[Path, ...]:
    """Resolve uma lista de caminhos de anexo (extraindo .zip em diretórios
    temporários rastreados em temp_dirs) para uso comum por qualquer adapter
    de TicketSource."""
    resolved: List[Path] = []
    for path in paths:
        if not path.exists():
            print(f"[AVISO] Anexo não encontrado, seguindo sem ele: {path}")
            continue
        if path.suffix.lower() in ARCHIVE_EXTENSIONS:
            extracted_dir = _extract_zip(path, temp_dirs)
            resolved.extend(p for p in extracted_dir.rglob('*') if p.is_file())
        else:
            resolved.append(path)
    return tuple(resolved)


class _TempDirTicketSource:
    """Base comum a adapters de TicketSource que extraem anexos (.zip) para
    diretórios temporários e precisam limpá-los ao final do `with`."""

    def __init__(self) -> None:
        self._temp_dirs: List[Path] = []

    def __enter__(self) -> "_TempDirTicketSource":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def close(self) -> None:
        for tmp_dir in self._temp_dirs:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        self._temp_dirs.clear()


class LocalTicketSource(_TempDirTicketSource):
    """Adapter local do port TicketSource: lê texto e anexos do disco.

    Um futuro adapter (ex: API do Service Desk da 2CX) implementa o mesmo
    port sem exigir mudanças no restante do pipeline (anonymizer/nlp_engine/
    log_analyzer não sabem nem precisam saber de onde o Ticket veio).
    """

    def __init__(self, text: Optional[str], text_file: Optional[Path],
                 attachment_paths: Sequence[Path]) -> None:
        super().__init__()
        self._text = text
        self._text_file = text_file
        self._attachment_paths = list(attachment_paths)

    def load(self) -> Ticket:
        raw_text = self._resolve_text()
        attachments = resolve_attachments(self._attachment_paths, self._temp_dirs)
        source_id = self._text_file.stem if self._text_file else uuid.uuid4().hex[:8]
        return Ticket(source_id=source_id, raw_text=raw_text, attachments=attachments)

    def _resolve_text(self) -> str:
        if self._text_file is not None:
            return self._text_file.read_text(encoding='utf-8')
        return self._text or ""


TicketPayload = Dict[str, object]
Fetcher = Callable[[], TicketPayload]


def mock_pool_fetcher(pool_dir: Path = Path('data/raw')) -> TicketPayload:
    """Simula uma resposta da API do Service Desk lendo um par chamado/log
    aleatório já gerado por tests/mock_generator.py.

    É a fixture padrão do ApiTicketSource enquanto o endpoint REST real da
    2CX não existe (ver SSD.md, Fase 3 - RPA). Quando ele existir, um fetcher
    real (ex: `requests.get(...)`) deve devolver o mesmo formato de payload
    (id/text/attachment_paths) para não exigir nenhuma mudança no restante
    do pipeline.
    """
    ticket_files = sorted(pool_dir.glob('req*.md'))
    if not ticket_files:
        raise FileNotFoundError(
            f"Nenhum chamado mock encontrado em '{pool_dir}'. "
            "Rode `python tests/mock_generator.py` antes de usar o ApiTicketSource stub."
        )
    ticket_file = random.choice(ticket_files)
    log_file = ticket_file.with_name(f"{ticket_file.stem}_log.txt")
    attachment_paths = [str(log_file)] if log_file.exists() else []
    return {
        'id': ticket_file.stem,
        'text': ticket_file.read_text(encoding='utf-8'),
        'attachment_paths': attachment_paths,
    }


class ApiTicketSource(_TempDirTicketSource):
    """Adapter (stub) do port TicketSource para a futura API REST do Service
    Desk da 2CX (Fase 3 - RPA, ver SSD.md).

    Não faz nenhuma chamada HTTP hoje. Recebe um `fetcher` que devolve o
    payload do chamado (id/text/attachment_paths); por padrão usa
    `mock_pool_fetcher`, que simula a resposta lendo os pares chamado/log
    sintéticos gerados por tests/mock_generator.py. Quando o endpoint real
    existir, basta injetar um fetcher que faça a chamada HTTP retornando o
    mesmo formato de payload — nenhuma mudança no restante do pipeline é
    necessária.
    """

    def __init__(self, fetcher: Fetcher = mock_pool_fetcher) -> None:
        super().__init__()
        self._fetcher = fetcher

    def load(self) -> Ticket:
        payload = self._fetcher()
        attachment_paths = [Path(str(p)) for p in payload.get('attachment_paths', [])]
        attachments = resolve_attachments(attachment_paths, self._temp_dirs)
        return Ticket(
            source_id=str(payload['id']),
            raw_text=str(payload['text']),
            attachments=attachments,
        )
