"""Backwards-compatible facade over `ldp.core.entities`.

The masking API used to live here as a list of sequential `re.sub` calls. That
approach could not tell a phone number from a CPF (both are bare digit runs)
and destroyed its own input as it went, which produced two live triage bugs -
see the module docstring in `entities.py` for the full account.

Detection now lives in `ldp.core.entities`. This module remains so existing
imports keep working, and re-exports the mask tokens other code checks for.
New code should import from `ldp.core.entities` directly and validate against
typed entities rather than mask tokens.
"""

from ldp.core.entities import (  # noqa: F401  (re-exported for compatibility)
    CNPJ_MASK,
    CONTA_MASK,
    CPF_MASK,
    EMAIL_MASK,
    TELEFONE_MASK,
    VALOR_MASK,
    Confidence,
    Entity,
    EntityKind,
    anonymize,
    detect_entities,
    has_entity,
    mask_text,
)

__all__ = [
    'CPF_MASK', 'CNPJ_MASK', 'CONTA_MASK', 'VALOR_MASK', 'TELEFONE_MASK', 'EMAIL_MASK',
    'Confidence', 'Entity', 'EntityKind',
    'anonymize', 'detect_entities', 'has_entity', 'mask_text',
    'mask_sensitive_data',
]


def mask_sensitive_data(raw_text: str) -> str:
    """Mask every PII entity in `raw_text`.

    Deprecated: prefer `ldp.core.entities.anonymize`, which also returns the
    typed entities. Masking alone is lossy, and treating a mask token as proof
    that a specific kind of datum was present is what caused the phone/CPF
    confusion this module used to have.
    """
    return anonymize(raw_text)[0] if raw_text else ''
