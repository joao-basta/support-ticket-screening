"""Symptom inference (Passo 1): TF-IDF over character n-grams + cosine similarity.

Why character n-grams
---------------------
The design document promises classification that ignores typos. A word-level
analyzer cannot deliver that: ``ligacao``, ``ligação`` and ``ligcao`` are three
unrelated tokens, so a single slip drops the match to zero. ``char_wb`` builds
3-to-5 character n-grams *inside* word boundaries, so those three spellings
share most of their features and score almost identically.

The corpus was also widened from one anchor document per symptom to several.
With three documents the IDF term spanned roughly 1.0-1.69 - statistically
degenerate, effectively plain keyword overlap. More, shorter anchors give the
weighting something real to work with and let a ticket match whichever phrasing
it resembles instead of one averaged blob.

The vectorizer is fitted once at import time; inference only ever transforms.
"""

from dataclasses import dataclass
from functools import lru_cache
from typing import Dict, List, Tuple

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from ldp import config
from ldp.core.symptoms import CLASSIFIABLE_SYMPTOMS, Symptom

# ---------------------------------------------------------
# ANCHOR CORPUS
# Short, distinct phrasings of how each failure is actually reported by
# Brazilian support staff. Written unaccented on purpose: the vectorizer
# strips accents anyway, and it keeps the anchors readable in any terminal.
# ---------------------------------------------------------
ANCHOR_DATA: Dict[Symptom, Tuple[str, ...]] = {
    Symptom.INTERFACE_UX: (
        "botao travou nao responde ao clique",
        "tela branca sistema congelou durante o uso",
        "nao foi possivel realizar a tabulacao do atendimento",
        "relatorio nao carrega pagina fica girando",
        "erro ao mutar o audio pelo botao da interface",
        "sistema lento travando ao abrir a tela de atendimento",
        "aplicacao fechou sozinha apresentou erro na tela",
    ),
    Symptom.CHAMADA_AUDIO: (
        "ligacao caiu no meio do atendimento com o cliente",
        "voz muda cliente nao escuta o agente",
        "audio picotando cortando durante a chamada",
        "chamada robotica com delay e atraso na voz",
        "softphone sem som nao sai audio na ligacao",
        "queda abrupta da chamada telefonica em linha ativa",
        "ruido e mudez repentina no audio do headset",
        "chamada caiu sozinha varias vezes seguidas",
        "ligacao derrubada sem motivo durante a conversa",
    ),
    Symptom.FILA_ROTEAMENTO: (
        "agentes disponiveis mas clientes parados na fila",
        "cliente nao entra na fila de atendimento",
        "fila travada nao distribui chamadas",
        "roteamento nao direciona a chamada para o agente",
        "pausa indevida agente logado nao recebe chamada",
        "skill errada atribuida ao agente na distribuicao",
        "agente ocioso enquanto a fila acumula clientes aguardando",
    ),
}


# ---------------------------------------------------------
# REJECT CORPUS
# Tickets that are perfectly valid requests but are not one of the three
# technical failures: HR, access, purchasing, pleasantries.
#
# These exist because a bare confidence threshold cannot separate them. With
# character n-grams, generic Portuguese ("preciso de acesso ao sistema de RH")
# scores as high against the symptom anchors as a real report does - the word
# "sistema" alone carries it. Modelling "not a symptom" as its own class turns
# an arbitrary cutoff into an ordinary comparison: the text is unknown when it
# resembles these more than it resembles any failure.
# ---------------------------------------------------------
# These are deliberately written as bare subject matter, with none of the
# "prezado suporte / solicito analise / colaborador matricula" framing. Genuine
# tickets are wrapped in exactly that boilerplate, so an anchor containing it
# would pull real failures into the reject class - measured at 8/19 correct on
# FALHA_INTERFACE_UX before the framing was stripped out, 19/19 after.
REJECT_ANCHORS: Tuple[str, ...] = (
    "ferias salario folha de pagamento beneficio adicional",
    "nota fiscal reembolso despesa orcamento financeiro",
    "compra de cadeira mesa mobiliario material de escritorio",
    "troca de senha esquecida reset de credencial bloqueada",
    "criacao de login novo perfil permissao de acesso rh",
    "bom dia boa tarde obrigado agradeco abraco",
    "andamento status previsao do meu pedido protocolo",
    "mudanca de setor departamento e endereco cadastral",
    "instalacao de programa office pacote na maquina nova",
    "duvida sobre politica interna norma e procedimento",
)


@dataclass(frozen=True)
class SymptomInference:
    """Outcome of Passo 1.

    Attributes:
        symptom: the winning symptom, or an unresolved marker.
        confidence: cosine similarity of the winner, rounded.
        runner_ups: every other symptom and its score, best first. Kept so a
            misclassification can be debugged without re-running the engine.
    """

    symptom: Symptom
    confidence: float
    runner_ups: Tuple[Tuple[Symptom, float], ...] = ()

    @property
    def is_resolved(self) -> bool:
        return self.symptom.is_resolved


# ---------------------------------------------------------
# MODEL INITIALIZATION (import time, exactly once)
# ---------------------------------------------------------
_ANCHOR_TEXTS: List[str] = []
_ANCHOR_OWNER: List[Symptom] = []
for _symptom in CLASSIFIABLE_SYMPTOMS:
    for _anchor in ANCHOR_DATA[_symptom]:
        _ANCHOR_TEXTS.append(_anchor)
        _ANCHOR_OWNER.append(_symptom)
for _anchor in REJECT_ANCHORS:
    _ANCHOR_TEXTS.append(_anchor)
    _ANCHOR_OWNER.append(Symptom.DESCONHECIDO)

vectorizer = TfidfVectorizer(
    analyzer='char_wb',
    ngram_range=(config.NLP_NGRAM_MIN, config.NLP_NGRAM_MAX),
    lowercase=True,
    strip_accents='unicode',
    sublinear_tf=True,
)
anchor_matrix = vectorizer.fit_transform(_ANCHOR_TEXTS)


@lru_cache(maxsize=config.NLP_CACHE_SIZE)
def infer_symptom(clean_text: str) -> SymptomInference:
    """Classify already-anonymized ticket text.

    Each symptom scores as the best cosine similarity across *its* anchors, so
    a ticket matching one specific phrasing is not diluted by the others.

    Args:
        clean_text: masked ticket text (Passo 0 output).

    Returns:
        The inference, or an unresolved marker when the text is too short or
        too dissimilar from every anchor.
    """
    if not clean_text or len(clean_text.strip()) < config.NLP_MIN_TEXT_LENGTH:
        return SymptomInference(Symptom.TEXTO_CURTO, 0.0)

    similarities = cosine_similarity(
        vectorizer.transform([clean_text]), anchor_matrix
    )[0]

    best_per_symptom: Dict[Symptom, float] = {}
    for score, owner in zip(similarities, _ANCHOR_OWNER):
        if score > best_per_symptom.get(owner, -1.0):
            best_per_symptom[owner] = float(score)

    # Ties break on the label so the outcome is reproducible. The previous
    # np.argmax broke them by dict insertion order, silently favouring
    # FALHA_INTERFACE_UX.
    ranked = sorted(best_per_symptom.items(), key=lambda item: (-item[1], item[0].value))
    winner, winning_score = ranked[0]
    runner_ups = tuple((symptom, round(score, 4)) for symptom, score in ranked[1:])

    # The reject class won, or nothing resembled anything: hand it to a human
    # rather than guessing.
    if winner is Symptom.DESCONHECIDO or winning_score < config.NLP_CONFIDENCE_THRESHOLD:
        return SymptomInference(Symptom.DESCONHECIDO, round(winning_score, 4), runner_ups)

    return SymptomInference(winner, round(winning_score, 4), runner_ups)


def infer_symptom_nlp(clean_text: str) -> Tuple[str, float]:
    """Backwards-compatible wrapper returning the original ``(label, score)`` tuple."""
    inference = infer_symptom(clean_text)
    return inference.symptom.value, inference.confidence
