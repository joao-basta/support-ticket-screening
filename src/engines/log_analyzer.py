import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Sequence, Tuple

from core.anonymizer import mask_sensitive_data

# ---------------------------------------------------------
# Assinaturas de erro por sintoma (Passo 3 do SSD). Regex genéricas
# (não strings exatas) para tolerar variação entre logs reais.
# ---------------------------------------------------------
ERROR_SIGNATURES: Dict[str, List[Tuple[re.Pattern, str]]] = {
    'FALHA_INTERFACE_UX': [
        (re.compile(r'\b5\d{2}\b.*(internal server error|http)', re.IGNORECASE), 'HTTP 5xx Server Error'),
        (re.compile(r'uncaught \w*error', re.IGNORECASE), 'JS Uncaught Exception'),
        (re.compile(r'heap out of memory', re.IGNORECASE), 'JS Heap Overflow'),
    ],
    'FALHA_CHAMADA_AUDIO': [
        (re.compile(r'jitter[:\s]+\d+\s*ms', re.IGNORECASE), 'Jitter Alto'),
        (re.compile(r'packet loss', re.IGNORECASE), 'Perda de Pacotes (UDP)'),
        (re.compile(r'sip/4\d{2}', re.IGNORECASE), 'SIP Timeout/Erro'),
    ],
    'FALHA_FILA_ROTEAMENTO': [
        (re.compile(r'acd_queue.*(error|overflow)', re.IGNORECASE), 'Fila ACD Overflow'),
        (re.compile(r'agent_state.*mismatch', re.IGNORECASE), 'Divergência de Estado do Agente'),
        (re.compile(r'routing_engine.*no available agent', re.IGNORECASE), 'Sem Agente Disponível'),
    ],
}


@dataclass(frozen=True)
class LogHit:
    file_name: str
    line_number: int
    symptom: str
    label: str
    line_excerpt: str


@dataclass(frozen=True)
class LogAnalysisResult:
    hits: Tuple[LogHit, ...]
    counts_by_symptom: Dict[str, int]
    dominant_symptom: Optional[str]
    matches_nlp_symptom: Optional[bool]


def scan_log_file(path: Path) -> Iterator[LogHit]:
    """Lê o log linha a linha (generator, O(1) de memória em arquivos grandes)
    buscando assinaturas de erro conhecidas por sintoma."""
    with open(path, encoding='utf-8', errors='replace') as log_file:
        for line_number, line in enumerate(log_file, start=1):
            for symptom, signatures in ERROR_SIGNATURES.items():
                for pattern, label in signatures:
                    if pattern.search(line):
                        yield LogHit(
                            file_name=path.name,
                            line_number=line_number,
                            symptom=symptom,
                            label=label,
                            line_excerpt=mask_sensitive_data(line.strip()),
                        )


def analyze_logs(log_paths: Sequence[Path], nlp_symptom: str) -> LogAnalysisResult:
    """Agrega os hits de todos os logs anexados e compara a causa raiz técnica
    dominante encontrada com o sintoma que o NLP inferiu do texto do chamado."""
    hits = [hit for path in log_paths for hit in scan_log_file(path)]
    counts = Counter(hit.symptom for hit in hits)
    dominant = counts.most_common(1)[0][0] if counts else None
    matches = None if dominant is None else (dominant == nlp_symptom)
    return LogAnalysisResult(
        hits=tuple(hits),
        counts_by_symptom=dict(counts),
        dominant_symptom=dominant,
        matches_nlp_symptom=matches,
    )
