"""Unit tests for `ldp.core.glpi_parser`.

These tests pin the contract that replaced the old
``raw_text.split('Info Solicitante:')[0]`` truncation: block parsing plus an
explicit allow-list, so that casing, accents, spacing and block order stop
deciding which text reaches the NLP engine and the evidence rules.
"""

import pytest

from ldp.core.glpi_parser import (
    ANALYSIS_BLOCKS,
    BLOCK_DETALHES,
    BLOCK_INFO_ARQUIVO,
    BLOCK_INFO_SOLICITANTE,
    FIELD_DELIMITER,
    KNOWN_BLOCKS,
    NARRATIVE_FIELD,
    GlpiBlock,
    GlpiDocument,
    GlpiField,
    normalize_header,
    parse_ticket,
)

#: Contact details of the *caller*, which must never reach analysis text.
SOLICITANTE_PHONE = '(21) 3344-5566'

#: A line that literally states there is no attachment, yet contains the word
#: the evidence rule looks for. The allow-list exists to keep it out.
NO_ATTACHMENT_LINE = 'Nenhum anexo informado'


# --------------------------------------------------------------------------
# normalize_header
# --------------------------------------------------------------------------

class TestNormalizeHeader:
    """Header folding: accents, case, whitespace and the trailing colon."""

    @pytest.mark.parametrize(
        'raw, expected',
        [
            ('Info Solicitante:', 'info solicitante'),
            ('INFO SOLICITANTE:', 'info solicitante'),
            ('  INFO   SOLICITANTE :  ', 'info solicitante'),
            ('info  Solicitante', 'info solicitante'),
            ('Categorização:', 'categorizacao'),
            ('Categorizacao', 'categorizacao'),
            ('Info\tArquivo', 'info arquivo'),
            ('ÁÉÍÓÚ Ç', 'aeiou c'),
            ('Detalhes:::', 'detalhes'),
        ],
        ids=[
            'canonical', 'uppercase', 'padded-and-spaced', 'double-space-no-colon',
            'accented', 'unaccented', 'tab-separator', 'accent-folding',
            'repeated-colons',
        ],
    )
    def test_folds_to_canonical_form(self, raw, expected):
        """normalize_header strips accents, lowercases, collapses whitespace, drops colons."""
        assert normalize_header(raw) == expected

    def test_canonical_names_are_already_normalized(self):
        """Every KNOWN_BLOCKS constant is its own canonical form (no drift)."""
        assert {normalize_header(name) for name in KNOWN_BLOCKS} == set(KNOWN_BLOCKS)


# --------------------------------------------------------------------------
# Header tolerance
# --------------------------------------------------------------------------

def _document_with_header(header_line: str) -> GlpiDocument:
    """Build a two-block ticket whose *first* block header is `header_line`."""
    body = (
        f'{header_line}\n'
        'contatonome -> Maria Silva\n'
        f'contatotelefone -> {SOLICITANTE_PHONE}\n'
        '\n'
        'Detalhes:\n'
        'detalhe1 -> A ligacao do agente p125413 caiu as 14:30.\n'
    )
    return parse_ticket(body)


class TestHeaderTolerance:
    """The literal split was case/whitespace sensitive; parsing must not be."""

    @pytest.mark.parametrize(
        'header_line',
        [
            'Info Solicitante:',
            'INFO SOLICITANTE:',
            'info solicitante:',
            'Info solicitante :',
            'info  Solicitante',
            'Info Solicitante',
            '   Info Solicitante:   ',
            '﻿Info Solicitante:',
            'Info Solicitante:',
            'INFO  SOLICITANTE',
        ],
        ids=[
            'canonical', 'uppercase', 'lowercase', 'space-before-colon',
            'double-space-no-colon', 'no-colon', 'padded', 'leading-bom',
            'non-breaking-space', 'nbsp-uppercase-no-colon',
        ],
    )
    def test_variant_is_recognised_as_info_solicitante(self, header_line):
        """Every spelling of the Info Solicitante header opens the same block."""
        document = _document_with_header(header_line)

        block = document.block(BLOCK_INFO_SOLICITANTE)
        assert block is not None
        assert block.get('contatotelefone') == SOLICITANTE_PHONE

    @pytest.mark.parametrize(
        'header_line',
        [
            'Info Solicitante:',
            'INFO SOLICITANTE:',
            'Info solicitante :',
            'info  Solicitante',
            '﻿Info Solicitante:',
            'Info Solicitante:',
        ],
        ids=['canonical', 'uppercase', 'space-before-colon', 'double-space-no-colon',
             'leading-bom', 'non-breaking-space'],
    )
    def test_variant_never_leaks_the_caller_phone(self, header_line):
        """Whatever the header spelling, the caller's phone stays out of analysis text."""
        document = _document_with_header(header_line)

        assert SOLICITANTE_PHONE not in document.analysis_text
        assert document.analysis_text == 'A ligacao do agente p125413 caiu as 14:30.'

    @pytest.mark.parametrize(
        'header_line, expected',
        [
            ('Categorização:', 'categorizacao'),
            ('CATEGORIZACAO', 'categorizacao'),
            ('Info Demandante:', 'info demandante'),
            ('INFO ARQUIVO', 'info arquivo'),
            ('info   fornecedor  :', 'info fornecedor'),
            ('DETALHES :', 'detalhes'),
        ],
        ids=['accented', 'uppercase', 'demandante', 'arquivo', 'fornecedor', 'detalhes'],
    )
    def test_every_known_block_tolerates_spelling(self, header_line, expected):
        """Tolerance is not special-cased to Info Solicitante."""
        document = parse_ticket(f'{header_line}\nchave -> valor\n')

        assert [block.name for block in document.blocks] == [expected]

    def test_raw_header_keeps_the_original_spelling(self):
        """The canonical name is normalized, but raw_header preserves the export text."""
        document = parse_ticket('  INFO SOLICITANTE:  \ncontatonome -> Maria Silva\n')

        block = document.block(BLOCK_INFO_SOLICITANTE)
        assert block.raw_header == 'INFO SOLICITANTE:'

    def test_unknown_header_does_not_open_a_block(self):
        """Only KNOWN_BLOCKS headers are recognised; anything else is unstructured."""
        document = parse_ticket('Bloco Desconhecido:\nchave -> valor\n')

        assert document.blocks == ()
        assert document.is_structured is False

    def test_line_containing_the_delimiter_is_never_a_header(self):
        """A value that mentions a block name is a field, not a new block."""
        document = parse_ticket('Detalhes:\ndetalhe1 -> Detalhes -> nao e cabecalho\n')

        assert [block.name for block in document.blocks] == [BLOCK_DETALHES]
        assert document.narrative == 'Detalhes -> nao e cabecalho'


# --------------------------------------------------------------------------
# Block order
# --------------------------------------------------------------------------

class TestBlockOrder:
    """The old split only worked because Info Solicitante came second."""

    def test_info_arquivo_before_detalhes_is_still_excluded(self):
        """With Info Arquivo first, analysis text is still only the detalhe1 narrative."""
        body = (
            'Info Arquivo:\n'
            f'anexo -> {NO_ATTACHMENT_LINE}\n'
            '\n'
            'Info Solicitante:\n'
            f'contatotelefone -> {SOLICITANTE_PHONE}\n'
            '\n'
            'Detalhes:\n'
            'detalhe1 -> A ligacao do agente p125413 caiu as 14:30.\n'
            'detalhe2 -> 192.168.240.90\n'
        )

        document = parse_ticket(body)

        assert document.analysis_text == 'A ligacao do agente p125413 caiu as 14:30.'
        assert NO_ATTACHMENT_LINE not in document.analysis_text
        assert SOLICITANTE_PHONE not in document.analysis_text

    def test_block_order_is_preserved_in_document_order(self):
        """Blocks are reported in the order they appear, not in a canonical order."""
        body = (
            'Info Arquivo:\nanexo -> x\n'
            '\nCategorizacao:\ncategoria -> y\n'
            '\nDetalhes:\ndetalhe1 -> z\n'
        )

        document = parse_ticket(body)

        assert [block.name for block in document.blocks] == [
            'info arquivo', 'categorizacao', 'detalhes',
        ]

    @pytest.mark.parametrize('detalhes_index', [0, 1, 2], ids=['first', 'middle', 'last'])
    def test_narrative_is_independent_of_detalhes_position(self, detalhes_index):
        """Wherever Detalhes sits in the export, the narrative is the same."""
        other_blocks = [
            f'Info Solicitante:\ncontatotelefone -> {SOLICITANTE_PHONE}\n',
            f'Info Arquivo:\nanexo -> {NO_ATTACHMENT_LINE}\n',
        ]
        detalhes = 'Detalhes:\ndetalhe1 -> Fila de atendimento travada as 09:15.\n'
        ordered = list(other_blocks)
        ordered.insert(detalhes_index, detalhes)

        document = parse_ticket('\n'.join(ordered))

        assert document.narrative == 'Fila de atendimento travada as 09:15.'
        assert document.analysis_text == 'Fila de atendimento travada as 09:15.'


# --------------------------------------------------------------------------
# Allow-list
# --------------------------------------------------------------------------

class TestAllowList:
    """`analysis_text` is produced from ANALYSIS_BLOCKS, never by denying one header."""

    def test_only_detalhes_is_allowed(self):
        """The allow-list is exactly the Detalhes block."""
        assert ANALYSIS_BLOCKS == frozenset({BLOCK_DETALHES})
        assert ANALYSIS_BLOCKS < KNOWN_BLOCKS

    def test_analysis_text_is_only_the_narrative(self, glpi_sample):
        """On a full GLPI export, analysis text equals detalhe1 and nothing else."""
        document = parse_ticket(glpi_sample)

        expected = (
            'Prezado suporte, o agente matricula p125413 relatou que a ligacao '
            'caiu no meio do atendimento as 14:30. O telefone afetado e (11) 98765-4321.'
        )
        assert document.analysis_text == expected
        assert document.narrative == expected

    @pytest.mark.parametrize(
        'forbidden',
        [
            SOLICITANTE_PHONE,
            NO_ATTACHMENT_LINE,
            'anexo',
            'Maria Silva',
            'p999888@corp.caixa.gov.br',
            '192.168.240.90',
            '123.456.789-00',
            '987654321',
            '4 Horas Uteis',
        ],
        ids=[
            'caller-phone', 'no-attachment-sentence', 'attachment-keyword',
            'caller-name', 'caller-email', 'decoy-ip', 'decoy-cpf',
            'decoy-phone', 'sla',
        ],
    )
    def test_excluded_content_is_absent_from_analysis_text(self, glpi_sample, forbidden):
        """None of the caller/decoy/metadata content reaches the evidence rules."""
        document = parse_ticket(glpi_sample)

        assert forbidden not in document.analysis_text

    def test_excluded_content_is_still_parsed_and_reachable(self, glpi_sample):
        """Excluded blocks are dropped from analysis, not from the document."""
        document = parse_ticket(glpi_sample)

        solicitante = document.block(BLOCK_INFO_SOLICITANTE)
        assert solicitante.get('contatotelefone') == SOLICITANTE_PHONE
        assert document.block(BLOCK_INFO_ARQUIVO).get('anexo') == NO_ATTACHMENT_LINE

    def test_narrative_field_constant_is_the_one_consulted(self):
        """`narrative` reads NARRATIVE_FIELD, not a hardcoded first field."""
        document = parse_ticket(
            'Detalhes:\n'
            'detalhe0 -> ruido que vem antes\n'
            f'{NARRATIVE_FIELD} -> A narrativa verdadeira.\n'
        )

        assert document.narrative == 'A narrativa verdadeira.'


# --------------------------------------------------------------------------
# discarded_blocks
# --------------------------------------------------------------------------

class TestDiscardedBlocks:
    """Diagnostics: the report must be able to name what was excluded."""

    def test_names_every_excluded_block(self, glpi_sample):
        """discarded_blocks lists the non-analysis blocks in document order."""
        document = parse_ticket(glpi_sample)

        assert document.discarded_blocks == (
            'info solicitante', 'categorizacao', 'info arquivo',
        )

    def test_detalhes_is_never_discarded(self, glpi_sample):
        """The allow-listed block never appears among the discarded ones."""
        document = parse_ticket(glpi_sample)

        assert BLOCK_DETALHES not in document.discarded_blocks

    def test_is_empty_when_only_detalhes_is_present(self):
        """Nothing excluded means an empty tuple, not None."""
        document = parse_ticket('Detalhes:\ndetalhe1 -> texto\n')

        assert document.discarded_blocks == ()

    def test_is_empty_for_unstructured_text(self):
        """Free text has no blocks, so nothing can be discarded."""
        document = parse_ticket('O telefone nao funciona.')

        assert document.discarded_blocks == ()


# --------------------------------------------------------------------------
# Fallbacks: analysis text is never silently empty
# --------------------------------------------------------------------------

class TestAnalysisTextFallbacks:
    """The old split produced '' for several real bodies; this must not."""

    def test_header_at_position_zero_without_detalhes(self):
        """A body opening with Info Solicitante still yields analysis text."""
        body = (
            'Info Solicitante:\n'
            'contatonome -> Maria Silva\n'
            f'contatotelefone -> {SOLICITANTE_PHONE}\n'
        )

        document = parse_ticket(body)

        assert document.is_structured is True
        assert document.narrative == ''
        assert document.analysis_text != ''
        assert document.analysis_text == body.strip()

    def test_detalhes_without_detalhe1_falls_back_to_the_block(self):
        """An export using other field names still produces analysis text."""
        document = parse_ticket(
            'Detalhes:\n'
            'descricao -> A fila de atendimento travou.\n'
            'observacao -> Sem retorno do supervisor.\n'
        )

        assert document.narrative == (
            'A fila de atendimento travou.\nSem retorno do supervisor.'
        )
        assert document.analysis_text == document.narrative

    def test_blank_detalhe1_yields_empty_analysis_text(self):
        """A blank narrative analyses as nothing, and that is deliberate.

        The sibling detalhe fields are decoys, so falling back to them would be
        worse than admitting there is nothing to analyse: an empty analysis
        text classifies as TEXTO_MUITO_CURTO and routes the ticket to a human,
        which is the safe outcome. The "never silently empty" guarantee applies
        to tickets with no Detalhes block at all - see the test below.
        """
        document = parse_ticket(
            'Detalhes:\n'
            'detalhe1 -> \n'
            'detalhe4 -> Aguardando Retorno do Cliente\n'
        )

        assert document.analysis_text == ''
        assert 'Aguardando Retorno' not in document.analysis_text

    def test_ticket_without_a_detalhes_block_is_never_empty(self):
        """An unfamiliar layout still gets analysed, rather than vanishing."""
        document = parse_ticket('Info Solicitante:\ncontatonome -> Maria Silva\n')

        assert document.analysis_text != ''

    def test_empty_body(self):
        """An empty body parses into an empty, unstructured document."""
        document = parse_ticket('')

        assert document == GlpiDocument(raw_text='', blocks=(), is_structured=False)
        assert document.analysis_text == ''
        assert document.narrative == ''
        assert document.as_dict() == {}

    @pytest.mark.parametrize(
        'body',
        ['', '   ', '\n\n\t\n'],
        ids=['empty', 'spaces', 'newlines'],
    )
    def test_blank_bodies_are_unstructured(self, body):
        """Whitespace-only bodies never claim to be structured."""
        document = parse_ticket(body)

        assert document.is_structured is False
        assert document.analysis_text == ''

    def test_narrative_is_empty_without_a_detalhes_block(self):
        """`narrative` is '' when Detalhes is absent, even though analysis text is not."""
        document = parse_ticket(f'Info Arquivo:\nanexo -> {NO_ATTACHMENT_LINE}\n')

        assert document.narrative == ''
        assert document.block(BLOCK_DETALHES) is None


class TestUnstructuredText:
    """Free-form text pasted into --text is analysed whole."""

    @pytest.mark.parametrize(
        'text',
        [
            'O sistema de telefonia caiu as 14:30 e nao consigo atender.',
            'Nao consigo logar no discador.\nJa reiniciei a maquina.',
            'chamado 12345 - fila travada',
        ],
        ids=['one-line', 'two-lines', 'with-hyphen'],
    )
    def test_plain_text_round_trips(self, text):
        """Unstructured input keeps its full text and reports is_structured False."""
        document = parse_ticket(text)

        assert document.is_structured is False
        assert document.blocks == ()
        assert document.analysis_text == text
        assert document.raw_text == text

    def test_surrounding_whitespace_is_trimmed_from_analysis_text(self):
        """analysis_text is stripped; raw_text keeps the original body untouched."""
        text = '\n  O telefone esta mudo.  \n\n'

        document = parse_ticket(text)

        assert document.analysis_text == 'O telefone esta mudo.'
        assert document.raw_text == text

    def test_lines_before_the_first_header_are_not_analysed(self):
        """A preamble is dropped once the export is recognised as structured."""
        document = parse_ticket(
            'Chamado 12345\n'
            'Detalhes:\n'
            'detalhe1 -> A ligacao caiu.\n'
        )

        assert document.is_structured is True
        assert document.analysis_text == 'A ligacao caiu.'


# --------------------------------------------------------------------------
# Field parsing
# --------------------------------------------------------------------------

class TestFieldParsing:
    """`chave -> valor` splitting, whitespace tolerance and wrapped lines."""

    @pytest.mark.parametrize(
        'line, key, value',
        [
            ('detalhe1 -> valor', 'detalhe1', 'valor'),
            ('detalhe1->valor', 'detalhe1', 'valor'),
            ('detalhe1  ->valor', 'detalhe1', 'valor'),
            ('detalhe1->  valor', 'detalhe1', 'valor'),
            ('detalhe1     ->     valor', 'detalhe1', 'valor'),
            ('  detalhe1 -> valor  ', 'detalhe1', 'valor'),
            ('detalhe1 -> ', 'detalhe1', ''),
            ('detalhe1 -> a -> b', 'detalhe1', 'a -> b'),
        ],
        ids=[
            'canonical', 'no-spaces', 'left-padded', 'right-padded', 'wide',
            'line-padded', 'empty-value', 'value-contains-delimiter',
        ],
    )
    def test_delimiter_tolerance(self, line, key, value):
        """The delimiter is ' -> ' with any surrounding whitespace, split once."""
        document = parse_ticket(f'Detalhes:\n{line}\n')

        assert document.block(BLOCK_DETALHES).fields == (GlpiField(key=key, value=value),)

    def test_field_delimiter_regex_is_the_one_used(self):
        """FIELD_DELIMITER is the exported regex driving the split."""
        assert FIELD_DELIMITER.split('chave  ->  valor', 1) == ['chave', 'valor']

    @pytest.mark.parametrize(
        'lookup',
        ['detalhe1', 'DETALHE1', 'Detalhe1', '  detalhe1  ', 'dEtAlHe1'],
        ids=['exact', 'upper', 'title', 'padded', 'mixed'],
    )
    def test_get_is_case_insensitive_and_trims(self, lookup):
        """GlpiBlock.get matches keys case-insensitively after stripping."""
        block = parse_ticket('Detalhes:\nDetalhe1 -> Texto\n').block(BLOCK_DETALHES)

        assert block.get(lookup) == 'Texto'

    def test_get_returns_none_for_unknown_key(self):
        """A missing key yields None rather than raising."""
        block = parse_ticket('Detalhes:\ndetalhe1 -> Texto\n').block(BLOCK_DETALHES)

        assert block.get('detalhe99') is None

    def test_wrapped_line_appends_to_the_previous_field(self):
        """A continuation line without a delimiter is joined to the previous value."""
        document = parse_ticket(
            'Detalhes:\n'
            'detalhe1 -> A ligacao do agente caiu\n'
            'no meio do atendimento as 14:30\n'
            'detalhe2 -> 192.168.240.90\n'
        )

        block = document.block(BLOCK_DETALHES)
        assert block.get('detalhe1') == (
            'A ligacao do agente caiu no meio do atendimento as 14:30'
        )
        assert block.get('detalhe2') == '192.168.240.90'

    def test_multiple_wrapped_lines_are_joined_with_single_spaces(self):
        """Several wrapped lines collapse into one single-spaced value."""
        document = parse_ticket(
            'Detalhes:\n'
            'detalhe1 -> primeira\n'
            '   segunda   \n'
            'terceira\n'
        )

        assert document.narrative == 'primeira segunda terceira'

    def test_blank_lines_inside_a_block_are_ignored(self):
        """Empty lines are separators, not continuations."""
        document = parse_ticket(
            'Detalhes:\n'
            'detalhe1 -> texto\n'
            '\n'
            '   \n'
            'detalhe2 -> outro\n'
        )

        block = document.block(BLOCK_DETALHES)
        assert block.get('detalhe1') == 'texto'
        assert len(block.fields) == 2

    def test_continuation_before_any_field_is_dropped(self):
        """A stray line right after the header has no field to attach to."""
        document = parse_ticket('Detalhes:\ntexto solto sem delimitador\n')

        assert document.block(BLOCK_DETALHES).fields == ()

    def test_values_text_joins_values_and_drops_keys(self):
        """values_text feeds NLP, so keys (pure noise) are discarded."""
        block = parse_ticket(
            'Info Solicitante:\n'
            'contatonome -> Maria Silva\n'
            'contatoramal -> \n'
            f'contatotelefone -> {SOLICITANTE_PHONE}\n'
        ).block(BLOCK_INFO_SOLICITANTE)

        assert block.values_text == f'Maria Silva\n{SOLICITANTE_PHONE}'


# --------------------------------------------------------------------------
# Document shape helpers
# --------------------------------------------------------------------------

class TestDocumentShape:
    """`block()`, `as_dict()` and raw text preservation."""

    def test_as_dict_shape(self, glpi_sample):
        """as_dict maps canonical block name -> {field key: value}."""
        document = parse_ticket(glpi_sample)

        as_dict = document.as_dict()
        assert list(as_dict) == [
            'detalhes', 'info solicitante', 'categorizacao', 'info arquivo',
        ]
        assert as_dict['info solicitante'] == {
            'contatonome': 'Maria Silva',
            'contatotelefone': SOLICITANTE_PHONE,
            'contatoemail': 'p999888@corp.caixa.gov.br',
        }
        assert as_dict['info arquivo'] == {'anexo': NO_ATTACHMENT_LINE}
        assert as_dict['detalhes']['detalhe1'].startswith('Prezado suporte')

    def test_as_dict_values_are_plain_strings(self, glpi_sample):
        """The mapping is JSON-serialisable plain data for the API."""
        as_dict = parse_ticket(glpi_sample).as_dict()

        assert all(
            isinstance(key, str) and isinstance(value, str)
            for fields in as_dict.values()
            for key, value in fields.items()
        )

    def test_block_returns_none_for_absent_block(self):
        """block() is a lookup, not a raise."""
        document = parse_ticket('Detalhes:\ndetalhe1 -> texto\n')

        assert document.block(BLOCK_INFO_ARQUIVO) is None

    def test_raw_text_is_never_rewritten(self):
        """BOM and non-breaking spaces are normalized for parsing only."""
        body = '﻿Detalhes:\ndetalhe1 -> ok\n'

        document = parse_ticket(body)

        assert document.raw_text == body
        assert document.narrative == 'ok'

    def test_document_is_hashable_and_frozen(self):
        """The dataclasses are frozen, so a parsed document is safe to share."""
        document = parse_ticket('Detalhes:\ndetalhe1 -> texto\n')

        with pytest.raises(Exception):
            document.raw_text = 'outro'
        with pytest.raises(Exception):
            document.blocks[0].name = 'outro'

    def test_repeated_block_keeps_both_but_reads_the_first(self):
        """A duplicated header yields two blocks; block() returns the first."""
        document = parse_ticket(
            'Detalhes:\ndetalhe1 -> primeiro\n'
            '\nDetalhes:\ndetalhe1 -> segundo\n'
        )

        assert [block.name for block in document.blocks] == ['detalhes', 'detalhes']
        assert document.narrative == 'primeiro'

    def test_manually_built_block_behaves_like_a_parsed_one(self):
        """GlpiBlock is usable standalone (API/debug construction)."""
        block = GlpiBlock(
            name=BLOCK_DETALHES,
            raw_header='Detalhes:',
            fields=(GlpiField(key='detalhe1', value='texto'),),
        )

        assert block.get('DETALHE1') == 'texto'
        assert block.values_text == 'texto'


# --------------------------------------------------------------------------
# Narrative resolution order
# --------------------------------------------------------------------------

def test_blank_detalhe1_does_not_admit_the_decoy_fields():
    """Decoy fields stay out of analysis text even when detalhe1 is blank.

    A present-but-empty detalhe1 means "there is nothing to analyse", which
    routes the ticket to a human. Falling back to detalhe2..detalhe11 would
    instead feed the NLP engine and the evidence rules the fake IPs, CPFs and
    phone numbers those fields exist to be.
    """
    document = parse_ticket(
        'Detalhes:\n'
        'detalhe1 -> \n'
        'detalhe2 -> 192.168.240.90\n'
        'detalhe3 -> TEL: 987654321\n'
        'detalhe5 -> 123.456.789-00\n'
    )

    assert document.analysis_text == ''
    assert '192.168.240.90' not in document.analysis_text
    assert '123.456.789-00' not in document.analysis_text


def test_missing_detalhe1_field_falls_back_to_the_whole_block():
    """An export naming its fields differently still gets analysed.

    The fallback applies only when there is no detalhe1 key at all - which is
    what distinguishes it from the blank-detalhe1 case above.
    """
    document = parse_ticket('Detalhes:\noutrocampo -> a ligacao caiu as 14:30\n')

    assert document.analysis_text == 'a ligacao caiu as 14:30'


def test_no_detalhes_block_falls_back_to_the_raw_body():
    """A ticket is never silently reduced to nothing by an unfamiliar layout."""
    document = parse_ticket('Info Solicitante:\ncontatotelefone -> (21) 3344-5566\n')

    assert document.analysis_text != ''
