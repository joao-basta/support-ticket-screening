import shutil
import tempfile
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Protocol, Sequence, Tuple

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


class LocalTicketSource:
    """Adapter local do port TicketSource: lê texto e anexos do disco.

    Um futuro adapter (ex: API do Service Desk da 2CX) implementa o mesmo
    port sem exigir mudanças no restante do pipeline (anonymizer/nlp_engine/
    log_analyzer não sabem nem precisam saber de onde o Ticket veio).
    """

    def __init__(self, text: Optional[str], text_file: Optional[Path],
                 attachment_paths: Sequence[Path]) -> None:
        self._text = text
        self._text_file = text_file
        self._attachment_paths = list(attachment_paths)
        self._temp_dirs: List[Path] = []

    def __enter__(self) -> "LocalTicketSource":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def close(self) -> None:
        for tmp_dir in self._temp_dirs:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        self._temp_dirs.clear()

    def load(self) -> Ticket:
        raw_text = self._resolve_text()
        attachments = self._resolve_attachments()
        source_id = self._text_file.stem if self._text_file else uuid.uuid4().hex[:8]
        return Ticket(source_id=source_id, raw_text=raw_text, attachments=attachments)

    def _resolve_text(self) -> str:
        if self._text_file is not None:
            return self._text_file.read_text(encoding='utf-8')
        return self._text or ""

    def _resolve_attachments(self) -> Tuple[Path, ...]:
        resolved: List[Path] = []
        for path in self._attachment_paths:
            if not path.exists():
                print(f"[AVISO] Anexo não encontrado, seguindo sem ele: {path}")
                continue
            if path.suffix.lower() in ARCHIVE_EXTENSIONS:
                extracted_dir = self._extract_zip(path)
                resolved.extend(p for p in extracted_dir.rglob('*') if p.is_file())
            else:
                resolved.append(path)
        return tuple(resolved)

    def _extract_zip(self, zip_path: Path) -> Path:
        tmp_dir = Path(tempfile.mkdtemp(prefix='ldp_'))
        resolved_root = tmp_dir.resolve()
        with zipfile.ZipFile(zip_path) as zf:
            for member in zf.infolist():
                dest = (tmp_dir / member.filename).resolve()
                if not str(dest).startswith(str(resolved_root)):
                    continue  # proteção Zip Slip: ignora membro com path traversal
                zf.extract(member, tmp_dir)
        self._temp_dirs.append(tmp_dir)
        return tmp_dir
