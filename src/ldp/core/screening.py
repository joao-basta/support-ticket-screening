"""The screening pipeline itself - pure, I/O-free, reusable.

`screen_ticket` runs all four steps and returns a `ScreeningResult`. It prints
nothing and touches no terminal, which is what lets the CLI and the HTTP API
share one implementation instead of maintaining two.

Steps
-----
0. Parse the GLPI layout and take only allow-listed content.
1. Detect and mask PII (LGPD).
2. Infer the symptom (TF-IDF + cosine).
3. Validate the evidence the symptom requires.
4. Scan attached logs and compare the technical cause against the text.
"""

import re
from pathlib import Path
from typing import Callable, Dict, Sequence, Tuple

from ldp.core.entities import Confidence, Entity, EntityKind, anonymize, has_entity
from ldp.core.glpi_parser import parse_ticket
from ldp.core.result import (
    AttachmentSummary,
    EvidenceCheck,
    ScreeningResult,
    ScreeningStatus,
)
from ldp.core.symptoms import Symptom
from ldp.core.ticket_source import AttachmentBundle, Ticket, classify_attachments
from ldp.engines.log_analyzer import analyze_logs
from ldp.engines.nlp_engine import infer_symptom

# ---------------------------------------------------------
# EVIDENCE PATTERNS
# Compiled once at module scope.
# ---------------------------------------------------------

#: Time of occurrence: "14:30", "14h30", "14h", "às 14 horas".
#: Bare "N horas" is deliberately excluded so an SLA duration ("4 Horas Úteis")
#: is not mistaken for the moment the incident happened.
TIME_REGEX = re.compile(
    r'\b(?:[01]?\d|2[0-3])\s*(?::|h)\s*[0-5]\d\b'
    r'|\b(?:[01]?\d|2[0-3])\s*h(?:s|rs)?\b'
    r'|\b(?:as|às)\s+(?:[01]?\d|2[0-3])\s*horas?\b',
    re.IGNORECASE,
)

#: Words denoting an actual artifact the requester attached or captured.
#: "tela" was removed: it is the *subject* of nearly every UX complaint
#: ("a tela ficou branca"), so accepting it meant the screenshot requirement
#: was satisfied by the very sentence describing the problem.
EVIDENCE_REGEX = re.compile(
    r'\b(anexo|anexos|anexado|anexada|anexei|print|prints|screenshot|captura|'
    r'imagem|foto|evidencia|evidência|console|log|logs)\b',
    re.IGNORECASE,
)

#: Signature of an evidence check: (masked text, entities, attachments) -> bool.
EvidenceCheckFn = Callable[[str, Sequence[Entity], AttachmentBundle], bool]


def _has_time(text: str, entities: Sequence[Entity], bundle: AttachmentBundle) -> bool:
    """True when the text states when the incident happened."""
    return bool(TIME_REGEX.search(text))


def _has_phone(text: str, entities: Sequence[Entity], bundle: AttachmentBundle) -> bool:
    """True when a phone number was deliberately supplied.

    Requires HIGH confidence, meaning the number was either punctuated as a
    phone or introduced by a cue word. A bare digit run is still masked for
    privacy but does not count as evidence - that distinction is what stops a
    protocol number from satisfying this rule.
    """
    return has_entity(entities, EntityKind.TELEFONE, Confidence.HIGH)


def _has_visual_or_log_evidence(text: str, entities: Sequence[Entity],
                                bundle: AttachmentBundle) -> bool:
    """True when a screenshot/console log was mentioned or actually attached."""
    if bundle.visual_files or bundle.log_files:
        return True
    return bool(EVIDENCE_REGEX.search(text))


# ---------------------------------------------------------
# VALIDATION MATRIX (Passo 2)
# ---------------------------------------------------------
CONTEXT_RULES: Dict[Symptom, Tuple[Tuple[str, EvidenceCheckFn], ...]] = {
    Symptom.CHAMADA_AUDIO: (
        ('Horário da ocorrência', _has_time),
        ('Número do Telefone/Alvo', _has_phone),
    ),
    Symptom.INTERFACE_UX: (
        ('Print da tela, log de console ou anexo (imagem/PDF/log)', _has_visual_or_log_evidence),
    ),
    Symptom.FILA_ROTEAMENTO: (
        ('Horário da ocorrência', _has_time),
    ),
    # Unresolved symptoms carry no rules: there is nothing specific to demand,
    # and the ticket goes to a human instead of back to the requester.
    Symptom.DESCONHECIDO: (),
    Symptom.TEXTO_CURTO: (),
}


def validate_context(symptom: Symptom, text: str, entities: Sequence[Entity],
                     bundle: AttachmentBundle) -> Tuple[EvidenceCheck, ...]:
    """Run the evidence rules for `symptom`.

    An unknown symptom yields no checks. Note this fails *open* by design: the
    pipeline must never block a ticket over a rule it does not have. The
    resulting status is ATENCAO (human review), not APROVADO.
    """
    rules = CONTEXT_RULES.get(symptom, ())
    return tuple(
        EvidenceCheck(item=label, satisfied=check(text, entities, bundle))
        for label, check in rules
    )


def resolve_status(symptom: Symptom, evidences: Sequence[EvidenceCheck]) -> ScreeningStatus:
    """Decide the terminal state.

    Missing evidence outranks an unresolved symptom: if we know what to demand,
    demanding it is more useful than routing to a human.
    """
    if any(not check.satisfied for check in evidences):
        return ScreeningStatus.BLOQUEADO
    if not symptom.is_resolved:
        return ScreeningStatus.ATENCAO
    return ScreeningStatus.APROVADO


def screen_ticket(ticket: Ticket) -> ScreeningResult:
    """Run the full pipeline over a loaded ticket.

    Pure with respect to the terminal: it reads attached log files but writes
    nothing and prints nothing.
    """
    # Passo 0a: parse the legacy layout, keep only allow-listed content.
    document = parse_ticket(ticket.raw_text)
    relevant_text = document.analysis_text

    # Passo 0b: sanitize (LGPD) and capture typed entities for the rules.
    masked_text, entities = anonymize(relevant_text)

    # Passo 1: infer the symptom.
    inference = infer_symptom(masked_text)

    # Passo 2: validate the required evidence.
    bundle = classify_attachments(ticket.attachments)
    evidences = validate_context(inference.symptom, masked_text, entities, bundle)
    status = resolve_status(inference.symptom, evidences)

    # Passo 3: forensic pass over attached logs (informational; never blocks).
    log_analysis = analyze_logs(bundle.log_files, inference.symptom) if bundle.log_files else None

    return ScreeningResult(
        source_id=ticket.source_id,
        analyzed_text=masked_text,
        symptom=inference.symptom,
        confidence=inference.confidence,
        status=status,
        evidences=evidences,
        runner_ups=inference.runner_ups,
        entities=entities,
        attachments=AttachmentSummary(
            logs=len(bundle.log_files),
            visuals=len(bundle.visual_files),
            others=len(bundle.other_files),
        ),
        log_analysis=log_analysis,
        discarded_blocks=document.discarded_blocks,
    )
