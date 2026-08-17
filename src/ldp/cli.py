"""Command-line entry point.

Thin by design: it parses arguments, loads a ticket through a `TicketSource`,
delegates to `screen_ticket`, prints the rendered report and exits with the
status code. Every rule lives in `ldp.core.screening`, so the CLI and the HTTP
API cannot drift apart.
"""

import argparse
import json
import logging
import sys
from pathlib import Path

from ldp import config
from ldp.core.errors import LdpError
from ldp.core.reporting import render_report
from ldp.core.screening import screen_ticket
from ldp.core.ticket_source import ApiTicketSource, LocalTicketSource, Ticket

logger = logging.getLogger(__name__)


def _configure_streams() -> None:
    """Force UTF-8 on stdout/stderr for the report's emoji and accents.

    The Windows console defaults to a legacy code page that raises
    UnicodeEncodeError on '✅'. Guarded with `hasattr` because under pytest
    capture (and some CI wrappers) the streams are replaced by objects that
    have no `reconfigure`.
    """
    for stream in (sys.stdout, sys.stderr):
        encoding = getattr(stream, 'encoding', None)
        if encoding and encoding.lower() != 'utf-8' and hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')


def _configure_logging(verbose: bool, quiet: bool) -> None:
    """Send diagnostics to stderr so stdout carries only the report."""
    level = logging.DEBUG if verbose else logging.ERROR if quiet else logging.WARNING
    logging.basicConfig(level=level, stream=sys.stderr,
                        format='%(levelname)s %(name)s: %(message)s')


def build_arg_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog='ldp',
        description='2CX / Caixa Log Diagnostic Pipeline (LDP)',
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument('--text', type=str,
                        help='Texto do chamado direto na linha de comando')
    source.add_argument('--text-file', type=Path,
                        help='Arquivo com o texto do chamado (aceita UTF-8 e cp1252)')
    source.add_argument('--api-source', action='store_true',
                        help='[STUB] Simula um chamado vindo da API do Service Desk, '
                             'lendo o pool de mocks em data/raw/')
    parser.add_argument('--attachments', nargs='+', type=Path, default=[],
                        help='Anexos: logs .txt/.log/.csv/.json, imagens .png/.jpg/.pdf ou .zip')
    parser.add_argument('--interactive', action='store_true',
                        help='Modo legado: cola o texto do chamado no terminal')
    parser.add_argument('--json', action='store_true',
                        help='Emite o resultado como JSON em vez do relatório de texto')
    parser.add_argument('--verbose', action='store_true', help='Log detalhado em stderr')
    parser.add_argument('--quiet', action='store_true', help='Suprime avisos em stderr')
    return parser


def emit(result, as_json: bool) -> None:
    """Print the screening in the requested format."""
    if as_json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(render_report(result))


def run_interactive_loop(as_json: bool = False) -> None:
    """Legacy paste mode: read lines until FIM, screen, repeat."""
    print('--- 2CX / Caixa Log Diagnostic Pipeline (Modo Interativo) ---')
    print('Cole o texto do chamado abaixo e pressione Enter. Use Ctrl+C para sair.\n')

    try:
        while True:
            print('\n' + '-' * config.REPORT_WIDTH)
            print('Cole o chamado abaixo.')
            print('(Para analisar, digite FIM em uma nova linha e aperte Enter)')
            print('-' * config.REPORT_WIDTH)

            lines = []
            while True:
                line = input()
                if line.strip().upper() == 'FIM':
                    break
                lines.append(line)

            # Newlines are preserved so the GLPI block parser still works on
            # pasted exports; the old version joined with spaces and destroyed
            # the layout.
            raw_text = '\n'.join(lines)
            if not raw_text.strip():
                continue

            ticket = Ticket(source_id='interativo', raw_text=raw_text, attachments=())
            emit(screen_ticket(ticket), as_json)
    except (KeyboardInterrupt, EOFError):
        print('\n\nPipeline encerrado pelo usuário. Bom descanso!')


def main() -> None:
    """Parse arguments, run one screening, exit with the status code."""
    _configure_streams()
    parser = build_arg_parser()
    args = parser.parse_args()
    _configure_logging(args.verbose, args.quiet)

    if args.interactive:
        run_interactive_loop(args.json)
        sys.exit(config.EXIT_SUCCESS)

    if not args.text and not args.text_file and not args.api_source:
        parser.error('informe --text, --text-file, --api-source ou --interactive')

    if not args.json:
        print('--- 2CX / Caixa Log Diagnostic Pipeline (NLP Engine) ---')

    try:
        if args.api_source:
            source = ApiTicketSource()
        else:
            source = LocalTicketSource(
                text=args.text,
                text_file=args.text_file,
                attachment_paths=args.attachments,
            )
        with source:
            result = screen_ticket(source.load())
    except LdpError as exc:
        # Expected failures print a clean message, never a traceback.
        print(f'[ERRO] {exc}', file=sys.stderr)
        sys.exit(config.EXIT_ERROR)

    emit(result, args.json)
    sys.exit(result.exit_code)


if __name__ == '__main__':
    main()
