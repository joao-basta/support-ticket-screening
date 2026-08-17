"""Unit tests for `ldp.core.entities` (Passo 0: PII detection and masking).

The module exists because masking was once used as entity extraction, which
produced two production incidents. Both are pinned here as explicit regression
suites (`TestRegressionD1`, `TestRegressionD2`) so a future refactor of the rule
table cannot silently reintroduce them.

Everything else covers the rule table itself plus the structural invariants the
rest of the pipeline depends on: entities are ordered and disjoint, masking is
idempotent, non-PII kinds stay readable, and MEDIUM confidence is masked but
never counts as evidence.
"""

import pytest

from ldp.core.entities import (
    CNPJ_MASK,
    CONTA_MASK,
    CPF_MASK,
    EMAIL_MASK,
    EMAIL_MASK as _EMAIL_MASK_ALIAS,  # noqa: F401  (kept import list explicit)
    MASK_TOKENS,
    NON_PII_KINDS,
    TELEFONE_MASK,
    VALID_DDD,
    VALOR_MASK,
    Confidence,
    Entity,
    EntityKind,
    anonymize,
    detect_entities,
    has_entity,
    mask_text,
)

# The DDD validator is a private helper, but the CPF/phone disambiguation it
# implements is the whole point of the module, so it is tested directly.
from ldp.core.entities import _is_brazilian_phone

# ---------------------------------------------------------------------------
# Shared test data
# ---------------------------------------------------------------------------

#: A long, deliberately messy body mixing every supported kind, both cued and
#: bare. Used as the substrate for the ordering / disjointness properties.
MIXED_TEXT = (
    "Prezado suporte, o agente matricula p125413 abriu o protocolo 1234567890 "
    "as 14:30. O telefone afetado e (11) 98765-4321 e o contato alternativo "
    "e +55 21 3344-5566. CPF 123.456.789-00, CNPJ 12.345.678/0001-95, "
    "conta: 12345-6, valor R$ 1.234,56 estornado. Email maria.silva@corp.caixa.gov.br, "
    "IP 192.168.240.90, chamado 987654321, cpf 12345678900, avulso 21987654321 "
    "e o cnpj 12345678000195 do fornecedor. Tel.: 11987654321."
)

#: A body whose every digit belongs to a PII entity, so masking must leave no
#: digit at all behind.
PII_ONLY_TEXT = (
    "Contato do cliente: telefone 11987654321, cpf 12345678900, "
    "outro (21) 3344-5566 e CPF 123.456.789-00."
)


def _single(text: str) -> Entity:
    """Detect over `text` asserting exactly one entity, and return it."""
    entities = detect_entities(text)
    assert len(entities) == 1, f'expected one entity in {text!r}, got {entities!r}'
    return entities[0]


def _kinds(text: str) -> list:
    """The kinds detected in `text`, in positional order."""
    return [entity.kind for entity in detect_entities(text)]


# ---------------------------------------------------------------------------
# REGRESSION D1 - an 11-digit mobile must not be stolen by the CPF rule
# ---------------------------------------------------------------------------


class TestRegressionD1:
    """The `\\b\\d{11}\\b` CPF rule used to claim `telefone 11987654321`.

    The evidence check looked for a TELEFONE entity, never found one, and
    blocked tickets whose phone number was present in the text.
    """

    @pytest.mark.parametrize('text', [
        'telefone 11987654321',
        'Telefone: 11987654321',
        'TELEFONE=11987654321',
        'Tel.: 11987654321',
        'tel: 11987654321',
        'fone: 11987654321',
        'Fone 11987654321',
        'cel: 11987654321',
        'ramal 11987654321',
        'contato 11987654321',
        'whatsapp 11987654321',
        'zap 11987654321',
    ])
    def test_cued_11_digit_mobile_is_a_high_confidence_phone(self, text):
        """Every lexical phone cue yields TELEFONE at HIGH, never CPF."""
        entity = _single(text)
        assert entity.kind is EntityKind.TELEFONE
        assert entity.confidence is Confidence.HIGH
        assert entity.raw == '11987654321'

    def test_cued_mobile_satisfies_the_evidence_check(self):
        """`has_entity` at its HIGH default accepts the cued mobile as evidence."""
        entities = detect_entities('telefone 11987654321')
        assert has_entity(entities, EntityKind.TELEFONE) is True
        assert has_entity(entities, EntityKind.CPF) is False

    def test_cued_mobile_is_masked_as_a_phone_not_a_cpf(self):
        """The rendered mask names the right kind, so downstream text reads true."""
        masked, _ = anonymize('telefone 11987654321')
        assert masked == f'telefone {TELEFONE_MASK}'
        assert CPF_MASK not in masked

    @pytest.mark.parametrize('text, expected_raw', [
        ('11987654321', '11987654321'),   # DDD 11 mobile
        ('21987654321', '21987654321'),   # DDD 21 mobile
        ('85987654321', '85987654321'),   # DDD 85 mobile
    ])
    def test_bare_valid_mobile_is_a_phone_even_without_a_cue(self, text, expected_raw):
        """A bare phone-shaped 11-digit run is TELEFONE (MEDIUM), still not CPF."""
        entity = _single(text)
        assert entity.kind is EntityKind.TELEFONE
        assert entity.confidence is Confidence.MEDIUM
        assert entity.raw == expected_raw


# ---------------------------------------------------------------------------
# REGRESSION D2 - a protocol number must not masquerade as a phone
# ---------------------------------------------------------------------------


class TestRegressionD2:
    """The old phone rule accepted any 7-12 digit run.

    `protocolo 1234567890` was masked as a phone and satisfied the "phone
    required" evidence rule on a ticket that contained no phone at all.
    """

    @pytest.mark.parametrize('text, expected_raw', [
        ('protocolo 1234567890', '1234567890'),
        ('Protocolo: 1234567890', '1234567890'),
        ('chamado 987654321', '987654321'),
        ('chamados 987654321', '987654321'),
        ('ticket 1234567890', '1234567890'),
        ('requisicao 1234567890', '1234567890'),
        ('req 1234567890', '1234567890'),
        ('incidente 1234567890', '1234567890'),
        ('numero 1234567890', '1234567890'),
        ('ordem de servico 1234567890', '1234567890'),
    ])
    def test_cued_request_number_is_a_protocol(self, text, expected_raw):
        """Protocol cues claim the digit run before any phone rule sees it."""
        entity = _single(text)
        assert entity.kind is EntityKind.PROTOCOLO
        assert entity.confidence is Confidence.HIGH
        assert entity.raw == expected_raw

    @pytest.mark.parametrize('text', [
        'protocolo 1234567890',
        'chamado 987654321',
    ])
    def test_protocol_is_never_accepted_as_phone_evidence(self, text):
        """Neither at HIGH nor at MEDIUM does a protocol count as a phone."""
        entities = detect_entities(text)
        assert has_entity(entities, EntityKind.TELEFONE) is False
        assert has_entity(entities, EntityKind.TELEFONE, Confidence.MEDIUM) is False
        assert has_entity(entities, EntityKind.PROTOCOLO) is True

    @pytest.mark.parametrize('text', [
        'protocolo 1234567890',
        'chamado 987654321',
    ])
    def test_protocol_survives_masking_verbatim(self, text):
        """A protocol is not personal data, so it stays readable for the tech."""
        masked, _ = anonymize(text)
        assert masked == text
        assert TELEFONE_MASK not in masked


# ---------------------------------------------------------------------------
# Phone formats
# ---------------------------------------------------------------------------


class TestPhoneFormats:
    """Punctuation-bearing phone layouts are self-evident and score HIGH."""

    @pytest.mark.parametrize('text, expected_raw', [
        ('Contato (11) 98765-4321 disponivel', '(11) 98765-4321'),
        ('telefone: (11)98765-4321', '(11)98765-4321'),
        ('whatsapp (85) 99999-8888', '(85) 99999-8888'),
        ('+55 11 98765-4321', '+55 11 98765-4321'),
        ('celular 11 98765 4321', '11 98765 4321'),
        ('ligue 3344-5566 hoje', '3344-5566'),
        ('ramal 1234-5678', '1234-5678'),
    ])
    def test_formatted_phone_is_high_confidence(self, text, expected_raw):
        """Parentheses, +55 and the hyphen group each disambiguate on their own."""
        entity = _single(text)
        assert entity.kind is EntityKind.TELEFONE
        assert entity.confidence is Confidence.HIGH
        assert entity.raw == expected_raw

    @pytest.mark.parametrize('text', [
        'Contato (11) 98765-4321 disponivel',
        '+55 11 98765-4321',
        'ligue 3344-5566 hoje',
    ])
    def test_formatted_phone_is_masked(self, text):
        """Formatted phones are PII and disappear from the rendered text."""
        masked, entities = anonymize(text)
        assert TELEFONE_MASK in masked
        assert entities[0].raw not in masked

    def test_nine_digit_number_after_a_cue_is_a_phone(self):
        """The legacy GLPI `TEL: 987654321` layout still reads as a phone."""
        entity = _single('TEL: 987654321')
        assert entity.kind is EntityKind.TELEFONE
        assert entity.confidence is Confidence.HIGH


# ---------------------------------------------------------------------------
# CPF
# ---------------------------------------------------------------------------


class TestCpf:
    """CPF in its three shapes: formatted, lexically cued, and bare."""

    @pytest.mark.parametrize('text, expected_raw, expected_confidence', [
        ('CPF 123.456.789-00', '123.456.789-00', Confidence.HIGH),
        ('cpf: 123.456.789-00', '123.456.789-00', Confidence.HIGH),
        ('o documento 123.456.789-00 anexado', '123.456.789-00', Confidence.HIGH),
        ('cpf 12345678900', '12345678900', Confidence.HIGH),
        ('documento: 12345678900', '12345678900', Confidence.HIGH),
        ('doc 12345678900', '12345678900', Confidence.HIGH),
        ('12345678900', '12345678900', Confidence.MEDIUM),
    ])
    def test_cpf_detection(self, text, expected_raw, expected_confidence):
        """The formatted and cued forms are HIGH; a bare 11-digit run is MEDIUM."""
        entity = _single(text)
        assert entity.kind is EntityKind.CPF
        assert entity.raw == expected_raw
        assert entity.confidence is expected_confidence

    @pytest.mark.parametrize('text', [
        'CPF 123.456.789-00',
        'cpf 12345678900',
        '12345678900',
    ])
    def test_cpf_is_always_masked(self, text):
        """Whatever the confidence, a CPF never reaches the rendered output."""
        masked, entities = anonymize(text)
        assert CPF_MASK in masked
        assert entities[0].raw not in masked

    @pytest.mark.parametrize('text', [
        '11887654321',   # valid DDD 11, third digit 8 -> not a mobile
        '21787654321',   # valid DDD 21, third digit 7 -> not a mobile
        '85087654321',   # valid DDD 85, third digit 0 -> not a mobile
        '00987654321',   # invalid DDD 00
        '10987654321',   # invalid DDD 10
        '20987654321',   # invalid DDD 20
    ])
    def test_non_phone_shaped_11_digit_run_falls_through_to_cpf(self, text):
        """A run that fails the phone structure test is claimed by the CPF rule."""
        entity = _single(text)
        assert entity.kind is EntityKind.CPF
        assert entity.confidence is Confidence.MEDIUM


# ---------------------------------------------------------------------------
# CNPJ, VALOR, EMAIL, IP, MATRICULA, CONTA
# ---------------------------------------------------------------------------


class TestCnpj:
    """CNPJ in the punctuated and the bare 14-digit form."""

    @pytest.mark.parametrize('text, expected_raw, expected_confidence', [
        ('CNPJ 12.345.678/0001-95', '12.345.678/0001-95', Confidence.HIGH),
        ('12345678000195', '12345678000195', Confidence.MEDIUM),
    ])
    def test_cnpj_detection(self, text, expected_raw, expected_confidence):
        """Punctuation gives HIGH; a bare 14-digit run gives MEDIUM."""
        entity = _single(text)
        assert entity.kind is EntityKind.CNPJ
        assert entity.raw == expected_raw
        assert entity.confidence is expected_confidence

    @pytest.mark.parametrize('text', ['CNPJ 12.345.678/0001-95', '12345678000195'])
    def test_cnpj_is_masked(self, text):
        """Both forms are PII and get replaced by the CNPJ token."""
        masked, _ = anonymize(text)
        assert CNPJ_MASK in masked


class TestValor:
    """Monetary amounts are only recognised in full `R$ n,nn` form."""

    @pytest.mark.parametrize('text, expected_raw', [
        ('valor R$ 1.234,56 estornado', 'R$ 1.234,56'),
        ('R$ 45,00', 'R$ 45,00'),
        ('R$1.000,00', 'R$1.000,00'),
        ('r$ 2,00', 'r$ 2,00'),
    ])
    def test_valor_detection(self, text, expected_raw):
        """The `R$` prefix plus two decimal places is required, case-insensitive."""
        entity = _single(text)
        assert entity.kind is EntityKind.VALOR
        assert entity.confidence is Confidence.HIGH
        assert entity.raw == expected_raw

    @pytest.mark.parametrize('text', ['R$ 10,5', 'R$ 1000', '1.234,56'])
    def test_incomplete_amount_is_not_a_valor(self, text):
        """A malformed amount is left alone rather than half-masked."""
        assert EntityKind.VALOR not in _kinds(text)

    def test_valor_is_masked(self):
        """Amounts are hidden behind the VALOR token."""
        masked, _ = anonymize('valor R$ 1.234,56 estornado')
        assert masked == f'valor {VALOR_MASK} estornado'


class TestEmail:
    """Addresses are claimed at the highest priority so no digit rule splits them."""

    @pytest.mark.parametrize('text, expected_raw', [
        ('email maria.silva@corp.caixa.gov.br ok', 'maria.silva@corp.caixa.gov.br'),
        ('contatoemail -> p999888@corp.caixa.gov.br', 'p999888@corp.caixa.gov.br'),
        ('a@b.co', 'a@b.co'),
    ])
    def test_email_detection(self, text, expected_raw):
        """A dotted domain of at least two characters is required."""
        entity = _single(text)
        assert entity.kind is EntityKind.EMAIL
        assert entity.confidence is Confidence.HIGH
        assert entity.raw == expected_raw

    def test_address_without_a_dotted_domain_is_ignored(self):
        """`user@dominio` has no TLD, so it is not treated as an address."""
        assert EntityKind.EMAIL not in _kinds('user@dominio')

    def test_email_local_part_digits_do_not_leak(self):
        """The whole address is masked, including a matricula-like local part."""
        masked, _ = anonymize('contatoemail -> p999888@corp.caixa.gov.br')
        assert masked == f'contatoemail -> {EMAIL_MASK}'
        assert 'p999888' not in masked


class TestIp:
    """IP addresses are detected for diagnostics and left visible."""

    @pytest.mark.parametrize('text, expected_raw', [
        ('IP 192.168.240.90 caiu', '192.168.240.90'),
        ('1.2.3.4', '1.2.3.4'),
    ])
    def test_ip_detection(self, text, expected_raw):
        """Four dot-separated groups of one to three digits."""
        entity = _single(text)
        assert entity.kind is EntityKind.IP
        assert entity.confidence is Confidence.HIGH
        assert entity.raw == expected_raw

    def test_ip_is_not_masked(self):
        """An IP is infrastructure data the technician needs, not PII."""
        masked, _ = anonymize('IP 192.168.240.90 caiu')
        assert masked == 'IP 192.168.240.90 caiu'


class TestMatricula:
    """The agent registration ID `pNNNNNN` is the technician's handle."""

    @pytest.mark.parametrize('text, expected_raw', [
        ('matricula p125413 relatou', 'p125413'),
        ('P125413', 'P125413'),
    ])
    def test_matricula_detection(self, text, expected_raw):
        """Exactly six digits after `p`, case-insensitive."""
        entity = _single(text)
        assert entity.kind is EntityKind.MATRICULA
        assert entity.confidence is Confidence.HIGH
        assert entity.raw == expected_raw

    @pytest.mark.parametrize('text', ['p12345', 'p1254133'])
    def test_wrong_digit_count_is_not_a_matricula(self, text):
        """Five or seven digits do not form a registration ID."""
        assert EntityKind.MATRICULA not in _kinds(text)

    def test_matricula_is_not_masked(self):
        """Masking a matricula would erase the one field N2 acts on."""
        masked, _ = anonymize('matricula p125413 relatou')
        assert masked == 'matricula p125413 relatou'


class TestConta:
    """Bank account / branch numbers, whose cue word is part of the span."""

    @pytest.mark.parametrize('text, expected_raw', [
        ('agencia 1234-5 conta', 'agencia 1234-5'),
        ('conta: 12345-6', 'conta: 12345-6'),
        ('cc 1234-5', 'cc 1234-5'),
    ])
    def test_conta_detection(self, text, expected_raw):
        """The cue is captured inside the span so the mask swallows it too."""
        entity = _single(text)
        assert entity.kind is EntityKind.CONTA
        assert entity.confidence is Confidence.HIGH
        assert entity.raw == expected_raw

    def test_conta_is_masked_together_with_its_cue(self):
        """`conta: 12345-6` collapses entirely into the CONTA token."""
        masked, _ = anonymize('conta: 12345-6')
        assert masked == CONTA_MASK


# ---------------------------------------------------------------------------
# _is_brazilian_phone - the CPF/phone disambiguation
# ---------------------------------------------------------------------------


class TestBrazilianPhoneValidator:
    """Structural test that decides whether a bare digit run is a phone."""

    @pytest.mark.parametrize('raw, expected', [
        # Mobiles: valid DDD + '9' + 8 digits.
        ('11987654321', True),
        ('21987654321', True),
        ('85987654321', True),
        ('99987654321', True),
        # Invalid area codes are rejected outright.
        ('00987654321', False),
        ('10987654321', False),
        ('20987654321', False),
        ('23987654321', False),
        # Valid DDD but the mobile marker digit is not 9.
        ('11887654321', False),
        ('21087654321', False),
        # Landlines: valid DDD + subscriber starting 2-5.
        ('1132345678', True),
        ('1142345678', True),
        ('1152345678', True),
        ('1122345678', True),
        # Landline subscriber outside 2-5.
        ('1112345678', False),
        ('1162345678', False),
        ('8598765432', False),
        # Wrong length.
        ('119876543', False),
        ('119876543210', False),
        ('', False),
        # Non-digit characters are stripped before the structural test.
        ('(11) 98765-4321', True),
        ('11 98765-4321', True),
        ('abc', False),
    ])
    def test_phone_structure(self, raw, expected):
        """Length, area code and the mobile/landline marker digit all matter."""
        assert _is_brazilian_phone(raw) is expected

    @pytest.mark.parametrize('ddd', ['11', '21', '85', '41', '61', '99'])
    def test_valid_ddd_is_listed(self, ddd):
        """Area codes actually in use are present in the table."""
        assert ddd in VALID_DDD

    @pytest.mark.parametrize('ddd', ['00', '10', '20', '23', '25', '26', '29', '30',
                                     '36', '39', '40', '50', '52', '56', '57', '58',
                                     '59', '60', '70', '72', '76', '78', '80', '90'])
    def test_unassigned_ddd_is_absent(self, ddd):
        """Unassigned area codes are absent, which is what frees them for CPF."""
        assert ddd not in VALID_DDD


# ---------------------------------------------------------------------------
# Confidence semantics
# ---------------------------------------------------------------------------


class TestConfidenceSemantics:
    """MEDIUM protects privacy without ever standing in as evidence."""

    @pytest.mark.parametrize('text, kind, token', [
        ('21987654321', EntityKind.TELEFONE, TELEFONE_MASK),
        ('12345678900', EntityKind.CPF, CPF_MASK),
        ('12345678000195', EntityKind.CNPJ, CNPJ_MASK),
    ])
    def test_medium_confidence_runs_are_still_masked(self, text, kind, token):
        """An ambiguous bare run is hidden: the privacy guarantee is unconditional."""
        masked, entities = anonymize(text)
        assert entities[0].kind is kind
        assert entities[0].confidence is Confidence.MEDIUM
        assert masked == token

    @pytest.mark.parametrize('text, kind', [
        ('21987654321', EntityKind.TELEFONE),
        ('12345678900', EntityKind.CPF),
        ('12345678000195', EntityKind.CNPJ),
    ])
    def test_medium_confidence_runs_are_not_evidence(self, text, kind):
        """`has_entity` defaults to HIGH, so a bare run cannot prove anything."""
        entities = detect_entities(text)
        assert has_entity(entities, kind) is False
        assert has_entity(entities, kind, Confidence.MEDIUM) is True

    def test_confidence_enum_carries_the_portuguese_labels(self):
        """The enum is a str enum whose values are the report-facing labels."""
        assert Confidence.HIGH == 'ALTA'
        assert Confidence.MEDIUM == 'MEDIA'
        assert Confidence.HIGH.value == 'ALTA'
        assert Confidence.MEDIUM.value == 'MEDIA'


class TestHasEntity:
    """The evidence predicate itself."""

    def test_empty_sequence_is_never_evidence(self):
        """No entities means no evidence, for every kind."""
        assert has_entity((), EntityKind.TELEFONE) is False
        assert has_entity((), EntityKind.CPF, Confidence.MEDIUM) is False

    def test_high_entities_also_satisfy_a_medium_threshold(self):
        """The threshold is a floor, so HIGH clears a MEDIUM requirement."""
        entities = detect_entities('telefone 11987654321')
        assert has_entity(entities, EntityKind.TELEFONE, Confidence.MEDIUM) is True

    def test_other_kinds_do_not_satisfy_the_requested_kind(self):
        """A CPF in the text does not prove a phone was supplied."""
        entities = detect_entities('CPF 123.456.789-00')
        assert has_entity(entities, EntityKind.CPF) is True
        assert has_entity(entities, EntityKind.TELEFONE) is False

    def test_mixed_confidence_uses_the_high_one(self):
        """One HIGH phone is enough even when a MEDIUM run is also present."""
        entities = detect_entities('telefone 11987654321 e o avulso 21987654321')
        confidences = {e.confidence for e in entities if e.kind is EntityKind.TELEFONE}
        assert confidences == {Confidence.HIGH, Confidence.MEDIUM}
        assert has_entity(entities, EntityKind.TELEFONE) is True


# ---------------------------------------------------------------------------
# Structural invariants
# ---------------------------------------------------------------------------


class TestInvariants:
    """Properties the rest of the pipeline relies on, over realistic bodies."""

    @pytest.mark.parametrize('text_name', ['MIXED_TEXT', 'PII_ONLY_TEXT', 'glpi'])
    def test_entities_are_sorted_by_start(self, text_name, glpi_sample):
        """Detection returns positional order, which masking and reporting assume."""
        text = {'MIXED_TEXT': MIXED_TEXT, 'PII_ONLY_TEXT': PII_ONLY_TEXT,
                'glpi': glpi_sample}[text_name]
        starts = [entity.start for entity in detect_entities(text)]
        assert starts == sorted(starts)

    @pytest.mark.parametrize('text_name', ['MIXED_TEXT', 'PII_ONLY_TEXT', 'glpi'])
    def test_entities_never_overlap(self, text_name, glpi_sample):
        """Overlapping spans would corrupt the back-to-front mask rewrite."""
        text = {'MIXED_TEXT': MIXED_TEXT, 'PII_ONLY_TEXT': PII_ONLY_TEXT,
                'glpi': glpi_sample}[text_name]
        entities = detect_entities(text)
        for previous, current in zip(entities, entities[1:]):
            assert previous.end <= current.start, f'{previous!r} overlaps {current!r}'

    def test_spans_match_the_source_text(self):
        """`raw` is exactly the slice `[start:end]` of the original text."""
        for entity in detect_entities(MIXED_TEXT):
            assert MIXED_TEXT[entity.start:entity.end] == entity.raw
            assert entity.length == entity.end - entity.start

    def test_detect_returns_a_tuple(self):
        """The return type is an immutable tuple, safe to cache and share."""
        assert isinstance(detect_entities(MIXED_TEXT), tuple)
        assert isinstance(detect_entities(''), tuple)

    def test_mixed_text_finds_every_expected_kind(self):
        """A body containing all nine kinds yields all nine kinds."""
        found = {entity.kind for entity in detect_entities(MIXED_TEXT)}
        assert found == set(EntityKind)

    def test_masking_is_idempotent(self):
        """Re-masking already-masked text is a no-op: tokens carry no digits."""
        once, _ = anonymize(MIXED_TEXT)
        twice, _ = anonymize(once)
        assert twice == once

    @pytest.mark.parametrize('text_name', ['MIXED_TEXT', 'PII_ONLY_TEXT', 'glpi'])
    def test_masking_is_idempotent_over_realistic_bodies(self, text_name, glpi_sample):
        """mask(mask(x)) == mask(x) for every body in the corpus."""
        text = {'MIXED_TEXT': MIXED_TEXT, 'PII_ONLY_TEXT': PII_ONLY_TEXT,
                'glpi': glpi_sample}[text_name]
        once, _ = anonymize(text)
        assert anonymize(once)[0] == once

    def test_no_pii_raw_value_survives_masking(self):
        """Every masked entity's literal text is absent from the output."""
        masked, entities = anonymize(MIXED_TEXT)
        for entity in entities:
            if entity.kind in NON_PII_KINDS:
                continue
            assert entity.raw not in masked, f'{entity.kind} leaked: {entity.raw!r}'

    def test_no_digit_survives_when_every_number_is_pii(self):
        """In a body of pure PII, masking removes every single digit."""
        masked, _ = anonymize(PII_ONLY_TEXT)
        assert not any(char.isdigit() for char in masked), masked

    @pytest.mark.parametrize('kind', sorted(NON_PII_KINDS, key=lambda k: k.value))
    def test_non_pii_kinds_have_no_mask_token(self, kind):
        """PROTOCOLO, MATRICULA and IP are detected but deliberately unmasked."""
        assert kind not in MASK_TOKENS

    def test_mask_tokens_cover_exactly_the_pii_kinds(self):
        """Every kind is either maskable or explicitly declared non-PII."""
        assert set(MASK_TOKENS) | NON_PII_KINDS == set(EntityKind)
        assert set(MASK_TOKENS) & NON_PII_KINDS == set()

    def test_non_pii_entities_are_left_verbatim(self):
        """A body of only non-PII values comes back byte-identical."""
        text = 'chamado 987654321 do agente p125413 na maquina 192.168.240.90'
        masked, entities = anonymize(text)
        assert {e.kind for e in entities} == {
            EntityKind.PROTOCOLO, EntityKind.MATRICULA, EntityKind.IP}
        assert masked == text

    def test_mask_text_accepts_entities_in_any_order(self):
        """`mask_text` sorts internally, so caller ordering cannot corrupt offsets."""
        text = 'telefone 11987654321 e cpf 12345678900'
        entities = detect_entities(text)
        expected = mask_text(text, entities)
        assert mask_text(text, tuple(reversed(entities))) == expected
        assert expected == f'telefone {TELEFONE_MASK} e cpf {CPF_MASK}'

    def test_mask_text_with_no_entities_returns_the_input(self):
        """An empty entity sequence leaves the text untouched."""
        text = 'telefone 11987654321 e cpf 12345678900'
        assert mask_text(text, ()) == text

    def test_anonymize_is_detect_plus_mask(self):
        """`anonymize` is exactly the composition of the two public helpers."""
        entities = detect_entities(MIXED_TEXT)
        assert anonymize(MIXED_TEXT) == (mask_text(MIXED_TEXT, entities), entities)


# ---------------------------------------------------------------------------
# Degenerate inputs
# ---------------------------------------------------------------------------


class TestEmptyAndFalsyInputs:
    """Falsy inputs short-circuit instead of raising, so callers need no guards."""

    @pytest.mark.parametrize('text', ['', None])
    def test_detect_on_falsy_input_returns_empty_tuple(self, text):
        """Empty string and None both yield no entities rather than a TypeError."""
        assert detect_entities(text) == ()

    @pytest.mark.parametrize('text', ['', None])
    def test_mask_on_falsy_input_returns_empty_string(self, text):
        """Masking normalises a falsy body to the empty string."""
        assert mask_text(text, ()) == ''

    @pytest.mark.parametrize('text', ['', None])
    def test_anonymize_on_falsy_input(self, text):
        """`anonymize` returns the normalised pair without raising."""
        assert anonymize(text) == ('', ())

    @pytest.mark.parametrize('text', ['   ', '\n\n', 'sem numeros aqui', 'ola tudo bem'])
    def test_text_without_sensitive_data_is_unchanged(self, text):
        """Bodies with nothing to hide pass through untouched."""
        masked, entities = anonymize(text)
        assert entities == ()
        assert masked == text


# ---------------------------------------------------------------------------
# Entity value object
# ---------------------------------------------------------------------------


class TestEntity:
    """The immutable span record."""

    def test_defaults_to_high_confidence(self):
        """Confidence is optional and defaults to HIGH."""
        entity = Entity(EntityKind.CPF, 0, 11, '12345678900')
        assert entity.confidence is Confidence.HIGH

    def test_length_is_the_span_width(self):
        """`length` is derived, never stored."""
        assert Entity(EntityKind.CPF, 4, 15, '12345678900').length == 11

    def test_is_frozen(self):
        """Entities are hashable value objects and cannot be mutated in place."""
        entity = Entity(EntityKind.CPF, 0, 11, '12345678900')
        with pytest.raises(Exception):
            entity.start = 5

    def test_kind_enum_is_a_string_enum(self):
        """`EntityKind` compares equal to its own name, for JSON round-tripping."""
        assert EntityKind.CPF == 'CPF'
        assert EntityKind.TELEFONE.value == 'TELEFONE'


# ---------------------------------------------------------------------------
# End-to-end over the legacy GLPI body
# ---------------------------------------------------------------------------


class TestGlpiBody:
    """The realistic ticket body used across the suite."""

    def test_expected_kinds_are_found(self, glpi_sample):
        """The sample carries a matricula, phones, an IP, a CPF and an email."""
        found = {entity.kind for entity in detect_entities(glpi_sample)}
        assert found == {
            EntityKind.MATRICULA, EntityKind.TELEFONE, EntityKind.IP,
            EntityKind.CPF, EntityKind.EMAIL,
        }

    def test_affected_phone_is_high_confidence_evidence(self, glpi_sample):
        """`(11) 98765-4321` in the narrative satisfies the phone evidence rule."""
        entities = detect_entities(glpi_sample)
        assert has_entity(entities, EntityKind.TELEFONE) is True

    def test_pii_is_removed_but_diagnostics_remain(self, glpi_sample):
        """Phones, CPF and email go; matricula and IP stay for the technician."""
        masked, _ = anonymize(glpi_sample)
        assert '(11) 98765-4321' not in masked
        assert '(21) 3344-5566' not in masked
        assert '123.456.789-00' not in masked
        assert 'p999888@corp.caixa.gov.br' not in masked
        assert 'p125413' in masked
        assert '192.168.240.90' in masked


# ---------------------------------------------------------------------------
# Known implementation defects
# ---------------------------------------------------------------------------


class TestCueWordBoundaries:
    """Regressions for cue matching that used to fire on word *tails*.

    Every cue alternation is anchored with a leading \b. Without it these four
    inputs each broke a core guarantee - two of them by silently recreating the
    phone/CPF confusion the module exists to prevent, one by leaving real PII
    unmasked.
    """

    def test_singular_celular_cue_is_high_confidence(self):
        """`celular 11987654321` is HIGH phone evidence, like `telefone`.

        The cue used to be spelled `celulares?`, i.e. "celulare" + optional "s",
        which never matched the singular form people actually write.
        """
        entities = detect_entities('celular 11987654321')
        assert has_entity(entities, EntityKind.TELEFONE) is True

    def test_word_ending_in_os_is_not_a_protocol_cue(self):
        """`contatos 11987654321` is a phone list, not a protocol number.

        'os' matching the tail of any -os word stole the span at higher
        priority than the phone rule, so a genuinely cued phone stopped
        counting as evidence.
        """
        entities = detect_entities('contatos 11987654321')
        assert has_entity(entities, EntityKind.TELEFONE) is True

    def test_cpf_after_a_word_ending_in_os_is_still_masked(self):
        """No surrounding prose may leave a raw CPF in the output (LGPD).

        `documentos 12345678900` typed the CPF as PROTOCOLO - a non-PII kind -
        so masking skipped it entirely.
        """
        masked, _ = anonymize('documentos 12345678900')
        assert '12345678900' not in masked

    def test_word_ending_in_tel_is_not_a_phone_cue(self):
        """`hotel 11987654321` must not fabricate HIGH-confidence phone evidence."""
        entities = detect_entities('hotel 11987654321')
        assert has_entity(entities, EntityKind.TELEFONE) is False

    def test_bare_article_os_is_not_a_protocol_cue(self):
        """The Portuguese article "os" must not swallow a following number.

        'os' was dropped from the protocol cues for exactly this collision.
        """
        entities = detect_entities('os 1234567890 registros')
        assert not any(e.kind is EntityKind.PROTOCOLO for e in entities)

    def test_explicit_protocol_cues_still_work(self):
        """The real cues must keep claiming their spans after the fix."""
        for text in ('protocolo 1234567890', 'chamado 987654321', 'ticket 55512345'):
            entities = detect_entities(text)
            assert any(e.kind is EntityKind.PROTOCOLO for e in entities), text
            assert has_entity(entities, EntityKind.TELEFONE) is False, text
