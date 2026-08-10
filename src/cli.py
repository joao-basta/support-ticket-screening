import sys
import json
import uuid
import re
from datetime import datetime, timezone
from typing import Generator, Dict, Any, TextIO, Tuple, List

# 1. Importa os módulos de negócio (Clean Architecture)
from core.anonymizer import mask_sensitive_data
from engines.nlp_engine import infer_symptom_nlp

# ---------------------------------------------------------
# 2. REGEX DE EVIDÊNCIAS (O "Segurança da Balada")
# As expressões são pré-compiladas no escopo global para evitar o custo
# de recálculo em loops, garantindo performance O(1) na validação.
# ---------------------------------------------------------
PHONE_REGEX = re.compile(r'(?:\+?55\s*)?(?:\(?\d{2}\)?\s*)?\d{4,5}[-\s]?\d{4}')
TIME_REGEX = re.compile(r'\b([0-1]?[0-9]|2[0-3])[:hH][0-5][0-9](?:[:][0-5][0-9])?\b')
EVIDENCE_REGEX = re.compile(r'\b(anexo|anexado|print|imagem|tela|console|log)\b', re.IGNORECASE)

# ---------------------------------------------------------
# 3. MATRIZ DE REQUISITOS (Validação Contextual)
# Dicionário que mapeia o Sintoma (str) para uma lista de tuplas contendo
# (Regex Compilado, Mensagem de Erro). A estrutura de dicionário (hash map)
# garante um look-up de O(1) para as regras.
# ---------------------------------------------------------
CONTEXT_RULES = {
    'FALHA_CHAMADA_AUDIO': [
        (TIME_REGEX, "Horário da ocorrência"),
        (PHONE_REGEX, "Número do Telefone/Alvo")
    ],
    'FALHA_INTERFACE_UX': [
        (EVIDENCE_REGEX, "Print da tela ou Log de console")
    ],
    'FALHA_FILA_ROTEAMENTO': [
        (TIME_REGEX, "Horário da ocorrência")
    ],
    # Se o sintoma não for reconhecido, não há como cobrar regras específicas.
    'SINTOMA_DESCONHECIDO': [],
    'TEXTO_MUITO_CURTO': []
}

def validate_context(symptom: str, raw_text: str) -> Tuple[bool, List[str]]:
    """
    Inspeciona o texto do chamado buscando apenas as evidências necessárias
    para o sintoma específico inferido pelo motor NLP.

    Returns:
        Uma tupla contendo um booleano de sucesso (True se válido) e a
        lista de evidências que ficaram faltando.
    """
    rules = CONTEXT_RULES.get(symptom, [])
    missing_items = []
    
    for pattern, error_msg in rules:
        if not pattern.search(raw_text):
            missing_items.append(error_msg)
            
    # A validação é bem-sucedida se a lista de itens faltantes estiver vazia.
    return len(missing_items) == 0, missing_items

def main() -> None:
    """Função principal que orquestra o pipeline da CLI."""
    print("--- 2CX / Caixa Log Diagnostic Pipeline (NLP Engine) ---")
    print("Cole o texto do chamado abaixo e pressione Enter. Use Ctrl+C para sair.\n")

    try:
            while True:
                print("\n" + "-"*50)
                print("Cole o chamado abaixo.")
                print("(Para analisar, digite FIM em uma nova linha e aperte Enter)")
                print("-" * 50)
                
                linhas = []
                while True:
                    linha = input()
                    if linha.strip().upper() == 'FIM':
                        break
                    linhas.append(linha)
                    
                raw_text = " ".join(linhas) # Junta tudo em um bloco único
                
                if not raw_text.strip():
                    continue

                # Passo 0: Sanitização (LGPD)
                anonymized = mask_sensitive_data(raw_text)

                # Passo 1: Inferência de Sintoma via TF-IDF (Matemática)
                symptom, score = infer_symptom_nlp(anonymized)

                # Passo 2: Validação de Evidências (Fail Fast)
                is_valid, missing = validate_context(symptom, anonymized)
                
                # ... (Aqui continua os seus prints normais de [TEXTO LIDO], [SINTOMA], etc)
                print("\n" + "="*50)
                print(f"[TEXTO ANALISADO] : {anonymized}")
                print(f"[SINTOMA INFERIDO]: {symptom} (Confiança: {score*100:.1f}%)")
                
                if not is_valid:
                    print(f"[STATUS]     : ❌ BLOQUEADO - FALTAM DADOS")
                    # Concatena os itens faltantes em uma string clara para o usuário.
                    print(f"[AÇÃO NECESSÁRIA] : Devolver chamado ao solicitante cobrando: {', '.join(missing)}")
                else:
                    if symptom in ['SINTOMA_DESCONHECIDO', 'TEXTO_MUITO_CURTO']:
                        print(f"[STATUS]     : ⚠️ ATENÇÃO - ANÁLISE HUMANA NECESSÁRIA")
                        print(f"[AÇÃO NECESSÁRIA] : Investigar manualmente. O sintoma não foi reconhecido.")
                    else:
                        print(f"[STATUS]     : ✅ SUCESSO - PRONTO PARA ANÁLISE TÉCNICA")
                        print(f"[AÇÃO NECESSÁRIA] : Prosseguir com a investigação do problema no Manager.")

                print("="*50 + "\n")

    except KeyboardInterrupt:
            print("\n\nPipeline encerrado pelo usuário. Bom descanso!")
            sys.exit(0)

if __name__ == "__main__":
    main()