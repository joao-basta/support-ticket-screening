import argparse
import re
import sys
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from core.anonymizer import mask_sensitive_data, TELEFONE_MASK
from core.ticket_source import ApiTicketSource, AttachmentBundle, LocalTicketSource, Ticket, classify_attachments
from engines.log_analyzer import LogAnalysisResult, analyze_logs
from engines.nlp_engine import infer_symptom_nlp

# ---------------------------------------------------------
# REGEX DE EVIDÊNCIAS (O "Segurança da Balada")
# Pré-compiladas no escopo global para evitar custo de recálculo
# em loops, garantindo performance O(1) na validação.
# ---------------------------------------------------------
TIME_REGEX = re.compile(r'\b([0-1]?[0-9]|2[0-3])[:hH][0-5][0-9](?:[:][0-5][0-9])?\b')
EVIDENCE_REGEX = re.compile(r'\b(anexo|anexado|print|imagem|tela|console|log)\b', re.IGNORECASE)
# O texto já chega mascarado pelo Passo 0 (Sanitização): a evidência de
# telefone é confirmada pela presença do token de máscara, não do número cru.
TELEFONE_MASK_REGEX = re.compile(re.escape(TELEFONE_MASK))

TextCheck = Callable[[str, Sequence[Path]], bool]

UNRESOLVED_SYMPTOMS = {'SINTOMA_DESCONHECIDO', 'TEXTO_MUITO_CURTO'}

EXIT_SUCCESS = 0
EXIT_BLOCKED = 1
EXIT_ATTENTION = 2


def _text_pattern_present(pattern: re.Pattern) -> TextCheck:
    def check(text: str, attachments: Sequence[Path]) -> bool:
        return bool(pattern.search(text))
    return check


def _has_visual_or_log_evidence(text: str, attachments: Sequence[Path]) -> bool:
    if EVIDENCE_REGEX.search(text):
        return True
    bundle = classify_attachments(attachments)
    return bool(bundle.visual_files or bundle.log_files)


# ---------------------------------------------------------
# MATRIZ DE REQUISITOS (Validação Contextual)
# Cada sintoma mapeia para uma lista de (mensagem, checagem), onde a
# checagem recebe o texto anonimizado e os anexos resolvidos do chamado.
# ---------------------------------------------------------
CONTEXT_RULES: Dict[str, List[Tuple[str, TextCheck]]] = {
    'FALHA_CHAMADA_AUDIO': [
        ("Horário da ocorrência", _text_pattern_present(TIME_REGEX)),
        ("Número do Telefone/Alvo", _text_pattern_present(TELEFONE_MASK_REGEX)),
    ],
    'FALHA_INTERFACE_UX': [
        ("Print da tela, log de console ou anexo (imagem/PDF/log)", _has_visual_or_log_evidence),
    ],
    'FALHA_FILA_ROTEAMENTO': [
        ("Horário da ocorrência", _text_pattern_present(TIME_REGEX)),
    ],
    # Se o sintoma não for reconhecido, não há como cobrar regras específicas.
    'SINTOMA_DESCONHECIDO': [],
    'TEXTO_MUITO_CURTO': [],
}


def validate_context(symptom: str, raw_text: str, attachments: Sequence[Path] = ()) -> Tuple[bool, List[str]]:
    """
    Inspeciona o texto do chamado e os anexos resolvidos buscando apenas as
    evidências necessárias para o sintoma específico inferido pelo motor NLP.

    Returns:
        Uma tupla contendo um booleano de sucesso (True se válido) e a
        lista de evidências que ficaram faltando.
    """
    rules = CONTEXT_RULES.get(symptom, [])
    missing_items = [message for message, check in rules if not check(raw_text, attachments)]
    return len(missing_items) == 0, missing_items


def resolve_exit_code(is_valid: bool, symptom: str) -> int:
    if not is_valid:
        return EXIT_BLOCKED
    if symptom in UNRESOLVED_SYMPTOMS:
        return EXIT_ATTENTION
    return EXIT_SUCCESS


def print_report(ticket: Ticket, anonymized: str, symptom: str, score: float,
                  is_valid: bool, missing: List[str], bundle: AttachmentBundle,
                  log_result: Optional[LogAnalysisResult]) -> None:
    print("\n" + "=" * 50)
    print(f"[CHAMADO]          : {ticket.source_id}")
    print(f"[TEXTO ANALISADO]  : {anonymized}")
    print(f"[SINTOMA INFERIDO] : {symptom} (Confiança: {score * 100:.1f}%)")

    if ticket.attachments:
        print(f"[ANEXOS]           : {len(bundle.log_files)} log(s), "
              f"{len(bundle.visual_files)} evidência(s) visual(is), "
              f"{len(bundle.other_files)} outro(s)")

    if not is_valid:
        print("[STATUS]           : ❌ BLOQUEADO - FALTAM DADOS")
        print(f"[AÇÃO NECESSÁRIA]  : Devolver chamado ao solicitante cobrando: {', '.join(missing)}")
    elif symptom in UNRESOLVED_SYMPTOMS:
        print("[STATUS]           : ⚠️ ATENÇÃO - ANÁLISE HUMANA NECESSÁRIA")
        print("[AÇÃO NECESSÁRIA]  : Investigar manualmente. O sintoma não foi reconhecido.")
    else:
        print("[STATUS]           : ✅ SUCESSO - PRONTO PARA ANÁLISE TÉCNICA")
        print("[AÇÃO NECESSÁRIA]  : Prosseguir com a investigação do problema no Manager.")

    if log_result is not None:
        print("-" * 50)
        if not log_result.hits:
            print("[ANÁLISE DE LOG]   : Nenhuma assinatura de erro conhecida encontrada nos anexos.")
        else:
            print(f"[ANÁLISE DE LOG]   : {len(log_result.hits)} ocorrência(s) encontrada(s):")
            for hit in log_result.hits:
                print(f"  - {hit.file_name}:{hit.line_number} [{hit.label}] {hit.line_excerpt}")
            if log_result.matches_nlp_symptom is False:
                print(f"[⚠️ DIVERGÊNCIA]    : Log aponta causa técnica de '{log_result.dominant_symptom}', "
                      f"mas o texto do chamado indica '{symptom}'. Revisar antes de prosseguir.")
            elif log_result.matches_nlp_symptom is True:
                print("[CONFIRMAÇÃO]      : Causa técnica no log confirma o sintoma relatado.")

    print("=" * 50 + "\n")


def process_ticket(ticket: Ticket) -> int:
    """Orquestra os 4 passos do pipeline (Sanitização, NLP, Validação Contextual
    e Análise de Log) sobre um Ticket já carregado, e imprime o relatório."""
    # Passo 0: Sanitização (LGPD)
    anonymized = mask_sensitive_data(ticket.raw_text)

    # Passo 1: Inferência de Sintoma via TF-IDF (Matemática)
    symptom, score = infer_symptom_nlp(anonymized)

    # Passo 2: Validação de Evidências (Fail Fast) — texto + anexos
    is_valid, missing = validate_context(symptom, anonymized, ticket.attachments)

    # Passo 3: Análise de arquivo de log anexado (informativo, não bloqueia o chamado)
    bundle = classify_attachments(ticket.attachments)
    log_result = analyze_logs(bundle.log_files, symptom) if bundle.log_files else None

    print_report(ticket, anonymized, symptom, score, is_valid, missing, bundle, log_result)
    return resolve_exit_code(is_valid, symptom)


def run_interactive_loop() -> None:
    """Modo legado: cola texto no terminal até digitar FIM. Sem suporte a anexos."""
    print("--- 2CX / Caixa Log Diagnostic Pipeline (Modo Interativo) ---")
    print("Cole o texto do chamado abaixo e pressione Enter. Use Ctrl+C para sair.\n")

    try:
        while True:
            print("\n" + "-" * 50)
            print("Cole o chamado abaixo.")
            print("(Para analisar, digite FIM em uma nova linha e aperte Enter)")
            print("-" * 50)

            linhas = []
            while True:
                linha = input()
                if linha.strip().upper() == 'FIM':
                    break
                linhas.append(linha)

            raw_text = " ".join(linhas)  # Junta tudo em um bloco único
            if not raw_text.strip():
                continue

            ticket = Ticket(source_id="interativo", raw_text=raw_text, attachments=())
            process_ticket(ticket)
    except (KeyboardInterrupt, EOFError):
        print("\n\nPipeline encerrado pelo usuário. Bom descanso!")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="2CX / Caixa Log Diagnostic Pipeline (LDP)")
    text_source = parser.add_mutually_exclusive_group()
    text_source.add_argument('--text', type=str, help="Texto do chamado direto na linha de comando")
    text_source.add_argument('--text-file', type=Path, help="Arquivo .txt com o texto do chamado")
    parser.add_argument('--attachments', nargs='+', type=Path, default=[],
                         help="Anexos: logs .txt, screenshots .png/.jpg/.pdf, ou .zip")
    parser.add_argument('--interactive', action='store_true',
                         help="Modo legado: cola o texto do chamado interativamente no terminal")
    text_source.add_argument('--api-source', action='store_true',
                              help="[STUB] Usa o adapter ApiTicketSource: simula um chamado vindo da "
                                   "API do Service Desk (Fase 3 - RPA) a partir do pool de mocks em "
                                   "data/raw/, gerado por tests/mock_generator.py")
    return parser


def main() -> None:
    # No Windows, o console usa por padrão a codepage legada (cp1252), que não
    # suporta os emojis do relatório (✅/❌/⚠️) nem acentos em alguns terminais.
    # Força stdout/stderr para UTF-8 para evitar UnicodeEncodeError.
    if sys.stdout.encoding is not None and sys.stdout.encoding.lower() != 'utf-8':
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')

    parser = build_arg_parser()
    args = parser.parse_args()

    if args.interactive:
        run_interactive_loop()
        sys.exit(EXIT_SUCCESS)

    if args.api_source:
        print("--- 2CX / Caixa Log Diagnostic Pipeline (NLP Engine) ---")
        with ApiTicketSource() as source:
            ticket = source.load()
            exit_code = process_ticket(ticket)
        sys.exit(exit_code)

    if not args.text and not args.text_file:
        parser.error("informe --text, --text-file, --api-source ou --interactive")

    print("--- 2CX / Caixa Log Diagnostic Pipeline (NLP Engine) ---")
    with LocalTicketSource(text=args.text, text_file=args.text_file,
                            attachment_paths=args.attachments) as source:
        ticket = source.load()
        exit_code = process_ticket(ticket)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
