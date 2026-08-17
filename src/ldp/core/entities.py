"""PII detection and masking (LGPD, Passo 0 of the pipeline).

Why this module replaced a flat list of `re.sub` calls
------------------------------------------------------
The previous implementation masked by running substitutions in sequence over
the text. That is lossy in a way that broke the business rules downstream:

* A Brazilian mobile with area code is *exactly* 11 digits, and the CPF pattern
  (`\\b\\d{11}\\b`) ran first, so `telefone 11987654321` became `[CPF_MASK]`.
  The evidence check looked for `[TELEFONE_MASK]`, never found it, and blocked
  a ticket whose phone number was right there in the text.
* The phone pattern accepted any run of ~7-12 digits, so `protocolo 1234567890`
  became `[TELEFONE_MASK]` and satisfied the "phone required" rule on a ticket
  containing no phone at all.

Both bugs share one root cause: masking was being used as *entity extraction*.
A mask token records that something was hidden, not what it was or how sure we
were. So detection now happens once over the original text, produces typed
spans with a confidence level, and masking is a separate rendering step over
those spans.

Validation consumes entities, never mask tokens. Evidence rules additionally
require HIGH confidence, so an ambiguous bare digit run is still masked (the
privacy guarantee is unchanged) without being accepted as proof that the
requester supplied a phone number.
"""

import re
from dataclasses import dataclass
from enum import Enum
from typing import Callable, FrozenSet, List, Optional, Sequence, Tuple


class EntityKind(str, Enum):
    """Class of a detected span."""

    CPF = 'CPF'
    CNPJ = 'CNPJ'
    TELEFONE = 'TELEFONE'
    CONTA = 'CONTA'
    VALOR = 'VALOR'
    EMAIL = 'EMAIL'
    PROTOCOLO = 'PROTOCOLO'
    MATRICULA = 'MATRICULA'
    IP = 'IP'


class Confidence(str, Enum):
    """How strongly the surrounding context supports the classification.

    HIGH means the span was disambiguated by punctuation (``(11) 98765-4321``)
    or by an adjacent lexical cue (``telefone: ...``). MEDIUM means only the
    digit structure supports it, which is enough to justify masking but not
    enough to accept as evidence.
    """

    HIGH = 'ALTA'
    MEDIUM = 'MEDIA'


@dataclass(frozen=True)
class Entity:
    """A detected span of sensitive or structurally significant text."""

    kind: EntityKind
    start: int
    end: int
    raw: str
    confidence: Confidence = Confidence.HIGH

    @property
    def length(self) -> int:
        return self.end - self.start


# ---------------------------------------------------------
# MASK TOKENS
# Exported so other modules can render or recognize them without
# duplicating the literal.
# ---------------------------------------------------------
MASK_TOKENS = {
    EntityKind.CPF: '[CPF_MASK]',
    EntityKind.CNPJ: '[CNPJ_MASK]',
    EntityKind.TELEFONE: '[TELEFONE_MASK]',
    EntityKind.CONTA: '[CONTA_MASK]',
    EntityKind.VALOR: '[VALOR_MASK]',
    EntityKind.EMAIL: '[EMAIL_MASK]',
}

CPF_MASK = MASK_TOKENS[EntityKind.CPF]
CNPJ_MASK = MASK_TOKENS[EntityKind.CNPJ]
TELEFONE_MASK = MASK_TOKENS[EntityKind.TELEFONE]
CONTA_MASK = MASK_TOKENS[EntityKind.CONTA]
VALOR_MASK = MASK_TOKENS[EntityKind.VALOR]
EMAIL_MASK = MASK_TOKENS[EntityKind.EMAIL]

#: Kinds that are detected for disambiguation/diagnostics but left visible.
#: A protocol number is not personal data, and the affected agent's
#: registration ID is precisely what the technician needs to act on.
NON_PII_KINDS: FrozenSet[EntityKind] = frozenset({
    EntityKind.PROTOCOLO,
    EntityKind.MATRICULA,
    EntityKind.IP,
})

#: Every area code in use in Brazil. Used to tell a mobile number apart from an
#: 11-digit CPF, which is the disambiguation the old code got wrong.
VALID_DDD: FrozenSet[str] = frozenset({
    '11', '12', '13', '14', '15', '16', '17', '18', '19',
    '21', '22', '24', '27', '28',
    '31', '32', '33', '34', '35', '37', '38',
    '41', '42', '43', '44', '45', '46', '47', '48', '49',
    '51', '53', '54', '55',
    '61', '62', '63', '64', '65', '66', '67', '68', '69',
    '71', '73', '74', '75', '77', '79',
    '81', '82', '83', '84', '85', '86', '87', '88', '89',
    '91', '92', '93', '94', '95', '96', '97', '98', '99',
})

# Every cue group is anchored with a LEADING \b as well as a trailing one.
# Without the leading boundary the alternations matched word *tails*, which
# quietly recreated the bugs this module exists to fix:
#   "hotel 11987654321"      -> 'tel' matched, promoting a random number to
#                               HIGH phone evidence
#   "contatos 11987654321"   -> 'os' matched, typing a real phone as PROTOCOLO
#                               so it stopped counting as evidence
#   "documentos 12345678900" -> 'os' matched, typing a real CPF as PROTOCOLO -
#                               a non-PII kind - so it was never masked (LGPD)
# Note also `celular(?:es)?`, not `celulares?`: the latter expands to
# "celulare" + optional "s" and therefore never matches the singular form that
# people actually write.
_PHONE_CUES = (r'\b(?:telefones?|tels?|fones?|celular(?:es)?|cels?|ramal|ramais'
               r'|contatos?|whatsapp|zap)\b')
_CPF_CUES = r'\b(?:cpf|documentos?|docs?)\b'
# 'os' was removed: even correctly anchored it collides with the Portuguese
# article "os". 'ordem de servico' covers the intended meaning unambiguously.
_PROTOCOL_CUES = (r'\b(?:protocolos?|chamados?|requisi[cç][aã]o|req|ticket'
                  r'|incidente|ordem de servi[cç]o|n[uú]mero)\b')

# Characters allowed between a cue word and the value it introduces. Deliberately
# excludes '(' so an opening parenthesis stays inside the captured span and gets
# masked along with the number, instead of being stranded outside it.
_CUE_GAP = r'[\s:.=-]{0,3}'


def _digits(text: str) -> str:
    """Strip every non-digit character."""
    return re.sub(r'\D', '', text)


def _is_brazilian_phone(raw: str) -> bool:
    """True when a bare digit run has the structure of a Brazilian phone number.

    A mobile is DDD + 9 + 8 digits (11 total); a landline is DDD + 8 digits
    where the subscriber part starts 2-5 (10 total). Anything else - notably an
    11-digit CPF whose first two digits are not a valid area code, or whose
    third digit is not 9 - is rejected so the CPF rule can claim the span.
    """
    digits = _digits(raw)
    if len(digits) not in (10, 11):
        return False
    if digits[:2] not in VALID_DDD:
        return False
    if len(digits) == 11:
        return digits[2] == '9'
    return digits[2] in '2345'


def _is_not_phone_shaped(raw: str) -> bool:
    """True when an 11-digit run does NOT look like a phone, so it may be a CPF."""
    return not _is_brazilian_phone(raw)


@dataclass(frozen=True)
class _Rule:
    """One detection rule.

    Attributes:
        pattern: compiled regex applied to the whole text.
        kind: entity kind produced on a match.
        confidence: how much the match's context supports the classification.
        priority: lower wins when two rules claim overlapping spans.
        group: which capture group carries the entity span (0 = whole match).
        validator: optional extra check on the matched text.
    """

    pattern: re.Pattern
    kind: EntityKind
    confidence: Confidence
    priority: int
    group: int = 0
    validator: Optional[Callable[[str], bool]] = None


# ---------------------------------------------------------
# DETECTION RULES
# Ordered by priority: the most structurally distinctive patterns claim their
# spans first, and ambiguous bare digit runs only get what is left over.
# ---------------------------------------------------------
_RULES: Tuple[_Rule, ...] = (
    # -- Unambiguous, punctuation-bearing formats -------------------------
    _Rule(re.compile(r'\b[\w.+-]+@[\w-]+\.[\w.]{2,}\b'),
          EntityKind.EMAIL, Confidence.HIGH, priority=10),
    _Rule(re.compile(r'\b\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}\b'),
          EntityKind.CNPJ, Confidence.HIGH, priority=11),
    _Rule(re.compile(r'\b\d{3}\.\d{3}\.\d{3}-\d{2}\b'),
          EntityKind.CPF, Confidence.HIGH, priority=12),
    _Rule(re.compile(r'R\$\s*\d{1,3}(?:\.\d{3})*,\d{2}\b', re.IGNORECASE),
          EntityKind.VALOR, Confidence.HIGH, priority=13),
    _Rule(re.compile(r'\b(?:\d{1,3}\.){3}\d{1,3}\b'),
          EntityKind.IP, Confidence.HIGH, priority=14),

    # -- Lexically cued: the surrounding word settles the ambiguity -------
    # Protocol numbers must be claimed before phones, otherwise a 10-12 digit
    # request number is masked as a phone and satisfies the audio evidence rule.
    _Rule(re.compile(rf'{_PROTOCOL_CUES}{_CUE_GAP}(\d{{6,14}})\b', re.IGNORECASE),
          EntityKind.PROTOCOLO, Confidence.HIGH, priority=20, group=1),
    _Rule(re.compile(rf'{_CPF_CUES}{_CUE_GAP}(\d{{11}}|\d{{3}}\.\d{{3}}\.\d{{3}}-\d{{2}})\b', re.IGNORECASE),
          EntityKind.CPF, Confidence.HIGH, priority=21, group=1),
    _Rule(re.compile(
        rf'{_PHONE_CUES}{_CUE_GAP}((?:\+?55[\s-]?)?(?:\(?\d{{2}}\)?[\s-]?)?\d{{4,5}}[\s-]?\d{{4}})\b',
        re.IGNORECASE),
          EntityKind.TELEFONE, Confidence.HIGH, priority=22, group=1),
    _Rule(re.compile(r'\b(?:ag[eê]ncia|ag|cc|conta)\s*:?\s*\d{3,5}[-.]?\d{1,2}\b', re.IGNORECASE),
          EntityKind.CONTA, Confidence.HIGH, priority=23),

    # -- Phone formats whose punctuation is self-evident ------------------
    _Rule(re.compile(r'(?:\+55\s*)?\(\d{2}\)\s*9?\d{4}[-\s]?\d{4}\b'),
          EntityKind.TELEFONE, Confidence.HIGH, priority=30),
    _Rule(re.compile(r'(?:\+55\s*)?\b\d{2}\s+9?\d{4}[-\s]\d{4}\b'),
          EntityKind.TELEFONE, Confidence.HIGH, priority=31),
    _Rule(re.compile(r'\b9?\d{4}-\d{4}\b'),
          EntityKind.TELEFONE, Confidence.HIGH, priority=32),

    # -- Internal identifiers --------------------------------------------
    _Rule(re.compile(r'\bp\d{6}\b', re.IGNORECASE),
          EntityKind.MATRICULA, Confidence.HIGH, priority=40),

    # -- Bare digit runs: structure only, hence MEDIUM --------------------
    # Masked for privacy, but never accepted as evidence.
    _Rule(re.compile(r'\b\d{10,11}\b'),
          EntityKind.TELEFONE, Confidence.MEDIUM, priority=50,
          validator=_is_brazilian_phone),
    _Rule(re.compile(r'\b\d{11}\b'),
          EntityKind.CPF, Confidence.MEDIUM, priority=51,
          validator=_is_not_phone_shaped),
    _Rule(re.compile(r'\b\d{14}\b'),
          EntityKind.CNPJ, Confidence.MEDIUM, priority=52),
)


def detect_entities(text: str) -> Tuple[Entity, ...]:
    """Find every sensitive or structurally significant span in `text`.

    All rules run against the *original* text, so no rule can destroy another
    rule's input. Overlapping candidates are then resolved by priority (and by
    length as a tiebreak), which is what lets a lexical cue override a
    structurally ambiguous digit run.

    Returns:
        Entities sorted by position, with no two overlapping.
    """
    if not text:
        return ()

    candidates: List[Tuple[int, int, Entity]] = []
    for rule in _RULES:
        for match in rule.pattern.finditer(text):
            raw = match.group(rule.group)
            if not raw:
                continue
            if rule.validator is not None and not rule.validator(raw):
                continue
            start, end = match.span(rule.group)
            candidates.append((
                rule.priority,
                -(end - start),
                Entity(kind=rule.kind, start=start, end=end, raw=raw,
                       confidence=rule.confidence),
            ))

    # Resolve overlaps: best priority first, longest span as tiebreak.
    candidates.sort(key=lambda item: (item[0], item[1]))

    accepted: List[Entity] = []
    for _, _, entity in candidates:
        if any(entity.start < kept.end and kept.start < entity.end for kept in accepted):
            continue
        accepted.append(entity)

    accepted.sort(key=lambda entity: entity.start)
    return tuple(accepted)


def mask_text(text: str, entities: Sequence[Entity]) -> str:
    """Replace every PII entity in `text` with its mask token.

    Spans are rewritten back-to-front so earlier offsets stay valid. Entities
    in `NON_PII_KINDS` are left untouched: they were detected to disambiguate
    the digit runs around them, not to be hidden.
    """
    if not text:
        return ''

    masked = text
    for entity in sorted(entities, key=lambda item: item.start, reverse=True):
        if entity.kind in NON_PII_KINDS:
            continue
        token = MASK_TOKENS.get(entity.kind)
        if token is None:
            continue
        masked = masked[:entity.start] + token + masked[entity.end:]
    return masked


def anonymize(text: str) -> Tuple[str, Tuple[Entity, ...]]:
    """Detect and mask in one pass.

    Returns:
        The masked text and the entities that were found. Callers that need to
        reason about *what* was present (the evidence rules) use the entities;
        callers that only display text use the masked string.
    """
    entities = detect_entities(text)
    return mask_text(text, entities), entities


def has_entity(entities: Sequence[Entity], kind: EntityKind,
               minimum_confidence: Confidence = Confidence.HIGH) -> bool:
    """True when `entities` contains a `kind` at or above `minimum_confidence`.

    Evidence rules call this with the default HIGH so that an ambiguous bare
    digit run cannot masquerade as a deliberately supplied phone number.
    """
    if minimum_confidence is Confidence.HIGH:
        return any(e.kind is kind and e.confidence is Confidence.HIGH for e in entities)
    return any(e.kind is kind for e in entities)
