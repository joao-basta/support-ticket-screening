"""Parser for the legacy GLPI ticket layout.

Format
------
A real export is a sequence of blocks, each opened by a header line and
followed by ``chave -> valor`` pairs (delimiter is space, hyphen,
greater-than, space)::

    Detalhes:
    detalhe1 -> A ligacao do agente p125413 caiu no meio do atendimento
    detalhe2 -> 192.168.240.90
    detalhe3 -> TEL: 987654321

    Info Solicitante:
    contatonome -> Maria Silva
    contatotelefone -> (11) 98765-4321

Why the old approach was replaced
---------------------------------
Analysis text used to be produced by ``raw_text.split('Info Solicitante:')[0]``
- an exact, case-sensitive, whitespace-sensitive literal split. It failed in
several ways that all end in a wrong triage decision:

* ``INFO SOLICITANTE:``, ``Info solicitante:`` or ``Info Solicitante :`` do not
  match, so the split silently does nothing and the *caller's* phone number
  flows into the evidence check - the exact false positive the truncation was
  added to prevent.
* It only worked because ``Info Solicitante:`` happens to be the second block.
  Reorder the export and blocks like ``Info Arquivo:`` survive, where the line
  ``anexo -> Nenhum anexo informado`` satisfies the "attachment required" rule
  using text that explicitly states there is no attachment.
* A body that opens with the header yields an empty string, which the pipeline
  then reports as "text too short" with a blank report and no diagnostic.

This module inverts the rule: instead of denying everything after one known
header, it parses all blocks and takes analysis text from an explicit
**allow-list**. Block order, casing, accents and spacing stop mattering.

Only ``detalhe1`` feeds inference and evidence. ``detalhe2``..``detalhe11`` are
deliberate decoys - fake CPFs, IPs, addresses and phone numbers - so admitting
them would hand the evidence rules exactly the traps they were built to dodge.
"""

import re
import unicodedata
from dataclasses import dataclass
from typing import Dict, FrozenSet, List, Optional, Tuple

#: Delimiter between a field key and its value.
FIELD_DELIMITER = re.compile(r'\s*->\s*')

#: A header is a short line naming a block, optionally followed by a colon.
_HEADER_LINE = re.compile(r'^\s*([A-Za-zÀ-ÿ][A-Za-zÀ-ÿ\s]{2,40}?)\s*:?\s*$')

#: Canonical block names, normalized (lowercase, unaccented, single-spaced).
BLOCK_DETALHES = 'detalhes'
BLOCK_INFO_SOLICITANTE = 'info solicitante'
BLOCK_CATEGORIZACAO = 'categorizacao'
BLOCK_INFO_DEMANDANTE = 'info demandante'
BLOCK_INFO_ARQUIVO = 'info arquivo'
BLOCK_INFO_FORNECEDOR = 'info fornecedor'

KNOWN_BLOCKS: FrozenSet[str] = frozenset({
    BLOCK_DETALHES,
    BLOCK_INFO_SOLICITANTE,
    BLOCK_CATEGORIZACAO,
    BLOCK_INFO_DEMANDANTE,
    BLOCK_INFO_ARQUIVO,
    BLOCK_INFO_FORNECEDOR,
})

#: Blocks whose content may reach the NLP engine and the evidence rules.
#: Everything else describes *who reported* the incident rather than what
#: happened, and would poison both.
ANALYSIS_BLOCKS: FrozenSet[str] = frozenset({BLOCK_DETALHES})

#: Field holding the analyst-written narrative inside the Detalhes block.
NARRATIVE_FIELD = 'detalhe1'


def normalize_header(text: str) -> str:
    """Fold a header to its canonical form: unaccented, lowercase, single-spaced."""
    decomposed = unicodedata.normalize('NFKD', text)
    stripped = ''.join(ch for ch in decomposed if not unicodedata.combining(ch))
    return re.sub(r'\s+', ' ', stripped).strip().lower().rstrip(':').strip()


@dataclass(frozen=True)
class GlpiField:
    """One ``chave -> valor`` pair."""

    key: str
    value: str


@dataclass(frozen=True)
class GlpiBlock:
    """One header plus the fields beneath it."""

    name: str
    raw_header: str
    fields: Tuple[GlpiField, ...]

    def get(self, key: str) -> Optional[str]:
        """Return the value of `key`, case-insensitively, or None."""
        wanted = key.strip().lower()
        for field in self.fields:
            if field.key.strip().lower() == wanted:
                return field.value
        return None

    @property
    def values_text(self) -> str:
        """All field values joined, keys discarded (keys are noise for NLP)."""
        return '\n'.join(field.value for field in self.fields if field.value)


@dataclass(frozen=True)
class GlpiDocument:
    """A parsed ticket body.

    Attributes:
        raw_text: the original, untouched body.
        blocks: every block found, in document order.
        is_structured: whether the GLPI layout was recognized at all. Free-form
            text pasted into `--text` parses as unstructured and is analysed
            whole.
    """

    raw_text: str
    blocks: Tuple[GlpiBlock, ...]
    is_structured: bool

    def block(self, name: str) -> Optional[GlpiBlock]:
        """Return the block named `name` (canonical form), or None."""
        for candidate in self.blocks:
            if candidate.name == name:
                return candidate
        return None

    @property
    def narrative(self) -> str:
        """The analyst's own account of the incident.

        Uses ``detalhe1`` whenever that field exists, **even if it is empty**.
        The fallback to the whole Detalhes block applies only when the export
        does not carry a ``detalhe1`` field at all.

        The distinction matters: ``detalhe2``..``detalhe11`` are deliberate
        decoys (fake CPFs, IPs, phone numbers). Falling back on a *blank*
        detalhe1 would hand exactly those decoys to the NLP engine and the
        evidence rules - the failure mode this allow-list exists to prevent.
        """
        detalhes = self.block(BLOCK_DETALHES)
        if detalhes is None:
            return ''
        explicit = detalhes.get(NARRATIVE_FIELD)
        if explicit is not None:
            return explicit
        return detalhes.values_text

    @property
    def analysis_text(self) -> str:
        """The only text the NLP engine and the evidence rules may see.

        Resolution order:

        1. A Detalhes block with a ``detalhe1`` field -> that field, verbatim.
           If it is blank the result is an empty string, and the ticket is
           routed to a human as "texto muito curto". That is deliberate: the
           sibling fields are decoys, so falling back to them would be worse
           than admitting there is nothing to analyse.
        2. A Detalhes block without ``detalhe1`` -> the whole block, for
           exports that name their fields differently.
        3. No Detalhes block at all -> the raw body, so a ticket is never
           silently reduced to nothing just because its layout was unfamiliar.
        """
        detalhes = self.block(BLOCK_DETALHES)
        if detalhes is not None:
            if detalhes.get(NARRATIVE_FIELD) is not None:
                return self.narrative.strip()
            allowed = detalhes.values_text.strip()
            if allowed:
                return allowed
        return self.raw_text.strip()

    @property
    def discarded_blocks(self) -> Tuple[str, ...]:
        """Names of blocks excluded from analysis, for reporting/diagnostics."""
        return tuple(
            block.name for block in self.blocks
            if block.name not in ANALYSIS_BLOCKS
        )

    def as_dict(self) -> Dict[str, Dict[str, str]]:
        """Blocks and fields as plain data, for the API and for debugging."""
        return {
            block.name: {field.key: field.value for field in block.fields}
            for block in self.blocks
        }


def _looks_like_header(line: str) -> Optional[str]:
    """Return the canonical block name if `line` opens a known block."""
    if FIELD_DELIMITER.search(line):
        return None
    match = _HEADER_LINE.match(line)
    if not match:
        return None
    name = normalize_header(match.group(1))
    return name if name in KNOWN_BLOCKS else None


def parse_ticket(raw_text: str) -> GlpiDocument:
    """Parse a ticket body into blocks and fields.

    Tolerates casing, accents, extra whitespace, a missing colon after the
    header, a UTF-8 BOM, and any block ordering. Text that matches none of the
    known headers comes back as an unstructured document, which the pipeline
    analyses in full.
    """
    if not raw_text:
        return GlpiDocument(raw_text='', blocks=(), is_structured=False)

    text = raw_text.lstrip('﻿').replace(' ', ' ')

    blocks: List[GlpiBlock] = []
    current_name: Optional[str] = None
    current_header = ''
    current_fields: List[GlpiField] = []

    def flush() -> None:
        if current_name is not None:
            blocks.append(GlpiBlock(
                name=current_name,
                raw_header=current_header,
                fields=tuple(current_fields),
            ))

    for line in text.splitlines():
        header = _looks_like_header(line)
        if header is not None:
            flush()
            current_name = header
            current_header = line.strip()
            current_fields = []
            continue

        if current_name is None:
            continue

        if FIELD_DELIMITER.search(line):
            key, value = FIELD_DELIMITER.split(line, 1)
            current_fields.append(GlpiField(key=key.strip(), value=value.strip()))
        elif line.strip():
            # Continuation of the previous field (wrapped line in the export).
            if current_fields:
                previous = current_fields[-1]
                current_fields[-1] = GlpiField(
                    key=previous.key,
                    value=f"{previous.value} {line.strip()}".strip(),
                )

    flush()

    return GlpiDocument(
        raw_text=raw_text,
        blocks=tuple(blocks),
        is_structured=bool(blocks),
    )
