"""The serializable outcome of a screening.

`process_ticket` used to print its report and return a bare exit code, so the
analysis existed only as text on a terminal. Nothing else could consume it -
which is precisely why an HTTP layer was impossible without this refactor.

`ScreeningResult` is that analysis as data. The CLI renders it as a text
report, the API serializes it as JSON, and both are views over the same object.

JSON keys are pt-BR because integrators are the audience; the code that builds
them is English, per the project's language policy.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional, Tuple

from ldp import config
from ldp.core.entities import Entity, EntityKind
from ldp.core.symptoms import Symptom
from ldp.engines.log_analyzer import LogAnalysisResult


class ScreeningStatus(str, Enum):
    """Terminal state of a screening."""

    APROVADO = 'APROVADO'
    BLOQUEADO = 'BLOQUEADO'
    ATENCAO = 'ATENCAO'

    @property
    def exit_code(self) -> int:
        return _STATUS_EXIT_CODES[self]

    @property
    def action(self) -> str:
        """pt-BR instruction telling the analyst what to do next."""
        return _STATUS_ACTIONS[self]


_STATUS_EXIT_CODES = {
    ScreeningStatus.APROVADO: config.EXIT_SUCCESS,
    ScreeningStatus.BLOQUEADO: config.EXIT_BLOCKED,
    ScreeningStatus.ATENCAO: config.EXIT_ATTENTION,
}

_STATUS_ACTIONS = {
    ScreeningStatus.APROVADO: 'Prosseguir com a investigação do problema no Manager.',
    ScreeningStatus.BLOQUEADO: 'Devolver o chamado ao solicitante cobrando os itens pendentes.',
    ScreeningStatus.ATENCAO: 'Investigar manualmente: o sintoma não foi reconhecido.',
}


@dataclass(frozen=True)
class EvidenceCheck:
    """One required piece of evidence and whether the ticket supplied it."""

    item: str
    satisfied: bool


@dataclass(frozen=True)
class AttachmentSummary:
    """Counts of attachments by role."""

    logs: int = 0
    visuals: int = 0
    others: int = 0

    @property
    def total(self) -> int:
        return self.logs + self.visuals + self.others


@dataclass(frozen=True)
class ScreeningResult:
    """Everything the pipeline concluded about one ticket."""

    source_id: str
    analyzed_text: str
    symptom: Symptom
    confidence: float
    status: ScreeningStatus
    evidences: Tuple[EvidenceCheck, ...] = ()
    runner_ups: Tuple[Tuple[Symptom, float], ...] = ()
    entities: Tuple[Entity, ...] = ()
    attachments: AttachmentSummary = field(default_factory=AttachmentSummary)
    log_analysis: Optional[LogAnalysisResult] = None
    discarded_blocks: Tuple[str, ...] = ()

    @property
    def missing(self) -> Tuple[str, ...]:
        """Evidence items the ticket failed to provide."""
        return tuple(check.item for check in self.evidences if not check.satisfied)

    @property
    def exit_code(self) -> int:
        return self.status.exit_code

    @property
    def entity_counts(self) -> Dict[str, int]:
        """Tally of detected entities by kind.

        Only kinds and counts - never the matched text. The whole point of
        Passo 0 is that raw PII does not travel any further than detection.
        """
        counts: Dict[str, int] = {}
        for entity in self.entities:
            counts[entity.kind.value] = counts.get(entity.kind.value, 0) + 1
        return counts

    @property
    def has_divergence(self) -> bool:
        """True when the log's dominant cause contradicts the inferred symptom."""
        return bool(self.log_analysis and self.log_analysis.matches_nlp_symptom is False)

    def to_dict(self) -> Dict[str, Any]:
        """Render as a JSON-serializable document with pt-BR keys."""
        payload: Dict[str, Any] = {
            'chamado': self.source_id,
            'status': self.status.value,
            'acao_necessaria': self.status.action,
            'sintoma': {
                'codigo': self.symptom.value,
                'descricao': self.symptom.label,
            },
            'confianca': round(self.confidence, 4),
            'alternativas': [
                {'codigo': symptom.value, 'confianca': round(score, 4)}
                for symptom, score in self.runner_ups
            ],
            'texto_analisado': self.analyzed_text,
            'evidencias': [
                {'item': check.item, 'atendida': check.satisfied}
                for check in self.evidences
            ],
            'pendencias': list(self.missing),
            'dados_sensiveis': self.entity_counts,
            'blocos_descartados': list(self.discarded_blocks),
            'anexos': {
                'logs': self.attachments.logs,
                'visuais': self.attachments.visuals,
                'outros': self.attachments.others,
                'total': self.attachments.total,
            },
            'analise_log': None,
            'codigo_saida': self.exit_code,
        }

        if self.log_analysis is not None:
            analysis = self.log_analysis
            payload['analise_log'] = {
                'arquivos_lidos': analysis.scanned_files,
                'ocorrencias': analysis.total_hits,
                'truncado': analysis.truncated,
                'causa_dominante': (
                    analysis.dominant_symptom.value if analysis.dominant_symptom else None
                ),
                'confere_com_texto': analysis.matches_nlp_symptom,
                'contagem_por_sintoma': {
                    symptom.value: count
                    for symptom, count in analysis.counts_by_symptom.items()
                },
                'ocorrencias_detalhadas': [
                    {
                        'arquivo': hit.file_name,
                        'linha': hit.line_number,
                        'tipo': hit.label,
                        'sintoma': hit.symptom.value,
                        'trecho': hit.line_excerpt,
                    }
                    for hit in analysis.hits
                ],
            }

        return payload
