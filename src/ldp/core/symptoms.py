"""Single source of truth for the symptom taxonomy.

The labels used to be re-typed as bare strings in the NLP engine, the log
analyzer, the CLI validation matrix and the mock generator. A typo in any one
of them broke the text-vs-log divergence check silently, because that check is
a plain string comparison across module boundaries. Everything now imports
from here.
"""

from enum import Enum
from typing import FrozenSet


class Symptom(str, Enum):
    """A symptom the pipeline can act on.

    Inherits from `str` so the members serialize transparently to JSON and
    compare equal to their wire representation.
    """

    INTERFACE_UX = 'FALHA_INTERFACE_UX'
    CHAMADA_AUDIO = 'FALHA_CHAMADA_AUDIO'
    FILA_ROTEAMENTO = 'FALHA_FILA_ROTEAMENTO'

    # Terminal states: the text carried no usable signal. These are not
    # failures of the ticket, they are failures of inference, and they route
    # the ticket to a human instead of blocking the requester.
    DESCONHECIDO = 'SINTOMA_DESCONHECIDO'
    TEXTO_CURTO = 'TEXTO_MUITO_CURTO'

    @property
    def is_resolved(self) -> bool:
        """True when inference actually identified a technical symptom."""
        return self not in UNRESOLVED_SYMPTOMS

    @property
    def label(self) -> str:
        """Human-facing pt-BR description, used by the report and the API."""
        return SYMPTOM_LABELS[self]


UNRESOLVED_SYMPTOMS: FrozenSet[Symptom] = frozenset({
    Symptom.DESCONHECIDO,
    Symptom.TEXTO_CURTO,
})

#: Symptoms the NLP engine is able to infer (i.e. those with an anchor corpus).
CLASSIFIABLE_SYMPTOMS: tuple = (
    Symptom.INTERFACE_UX,
    Symptom.CHAMADA_AUDIO,
    Symptom.FILA_ROTEAMENTO,
)

#: pt-BR descriptions shown to analysts and returned by the API.
SYMPTOM_LABELS = {
    Symptom.INTERFACE_UX: 'Falha de interface / experiência do usuário',
    Symptom.CHAMADA_AUDIO: 'Falha de áudio em chamada',
    Symptom.FILA_ROTEAMENTO: 'Falha de fila / roteamento',
    Symptom.DESCONHECIDO: 'Sintoma não reconhecido',
    Symptom.TEXTO_CURTO: 'Texto insuficiente para análise',
}


def parse_symptom(value: str) -> Symptom:
    """Coerce a wire string into a `Symptom`, defaulting to DESCONHECIDO.

    Used at the boundaries (API payloads, mock pools) where the value may not
    come from our own code.
    """
    try:
        return Symptom(value)
    except ValueError:
        return Symptom.DESCONHECIDO
