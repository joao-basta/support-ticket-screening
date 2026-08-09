import sys
import json
import uuid
import re
from datetime import datetime, timezone
from typing import Generator, Dict, Any, TextIO, Tuple, List

from core.anonymizer import mask_sensitive_data
from engines.nlp_engine import infer_symptom_nlp

# ---------------------------------------------------------
# REGEX DE EVIDÊNCIAS (O "Segurança da Balada")
# Compilados globalmente para evitar recálculo em loops (O(1)).
# ---------------------------------------------------------
PHONE_REGEX = re.compile(r'(?:\+?55\s*)?(?:\(?\d{2}\)?\s*)?\d{4,5}[-\s]?\d{4}')
TIME_REGEX = re.compile(r'\b([0-1]?[0-9]|2[0-3])[:hH][0-5][0-9](?:[:][0-5][0-9])?\b')
EVIDENCE_REGEX = re.compile(r'\b(anexo|anexado|print|imagem|tela|console|log)\b', re.IGNORECASE)

# ---------------------------------------------------------
# MATRIZ DE REQUISITOS (Validação Contextual)
# Dicionário O(1) que mapeia o Sintoma -> Evidências Obrigatórias
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
    'SINTOMA_DESCONHECIDO': [], # Se não sabe o que é, não tem como cobrar regra específica
    'TEXTO_MUITO_CURTO': []
}

def validate_context(symptom: str, raw_text: str) -> Tuple[bool, List[str]]:
    """
    Inspeciona o chamado buscando apenas as evidências necessárias para aquele sintoma específico.
    Retorna um booleano de sucesso e a lista do que ficou faltando.
    """
    # Look-up em O(1) na tabela de regras
    rules = CONTEXT_RULES.get(symptom, [])
    missing_items = []
    
    for pattern, error_msg in rules:
        if not pattern.search(raw_text):
            missing_items.append(error_msg)
            
    return len(missing_items) == 0, missing_items

def log_line_generator(file_stream: TextIO) -> Generator[str, None, None]:
    """Gerador eficiente em memória para lidar com o input do terminal."""
    for line in file_stream:
        clean_line = line.strip()
        if clean_line:
            yield clean_line

def save_feedback(log_data: Dict[str, Any], is_correct: bool, correct_category: str = None) -> None:
    """Persiste o feedback para treinar o ML no futuro."""
    log_data["user_validated"] = is_correct
    log_data["final_category"] = correct_category if not is_correct else log_data["inferred_category"]
    
    try:
        with open("data/processed/dataset.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(log_data) + "\n")
    except IOError as e:
        print(f"[ERRO DE IO] Falha ao salvar feedback: {e}", file=sys.stderr)

def main() -> None:
    print("--- 2CX / Caixa Log Diagnostic Pipeline (NLP Engine) ---")
    print("Cole o chamado e pressione Enter. Pressione Ctrl+C para sair.\n")

    try:
        for raw_text in log_line_generator(sys.stdin):
            # Passo 0: Sanitização (LGPD)
            anonymized = mask_sensitive_data(raw_text)

            # Passo 1: Inferência de Sintoma via TF-IDF (Matemática)
            symptom, score = infer_symptom_nlp(anonymized)

            # Passo 2: Validação de Evidências (Fail Fast)
            is_valid, missing = validate_context(symptom, anonymized)

            # --- SAÍDA PARA O USUÁRIO ---
            print("\n" + "="*50)
            print(f"[TEXTO LIDO] : {anonymized}")
            print(f"[SINTOMA]    : {symptom} (Confiança: {score*100:.1f}%)")
            
            if not is_valid:
                print(f"[STATUS]     : ❌ BLOQUEADO - FALTAM DADOS")
                print(f"[AÇÃO]       : Devolver chamado solicitando: {', '.join(missing)}")
            else:
                if symptom in ['SINTOMA_DESCONHECIDO', 'TEXTO_MUITO_CURTO']:
                    print(f"[STATUS]     : ⚠️ NECESSITA ANÁLISE HUMANA TOTAL")
                else:
                    print(f"[STATUS]     : ✅ PRONTO PARA ANÁLISE NO MANAGER")

            print("="*50)

            # Passo 3: Loop de Feedback
            feedback = input("\nO sintoma deduzido está correto? (Y/N): ").strip().upper()
            
            log_data = {
                "id": str(uuid.uuid4()),
                "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "raw_text": anonymized,
                "inferred_category": symptom,
                "confidence_score": score,
                "missing_evidences": missing
            }

            if feedback == 'Y':
                save_feedback(log_data, True)
                print("[+] Arquivado no Dataset de Ouro.\n")
            else:
                correct_cat = input(f"Categorias válidas: {list(CONTEXT_RULES.keys())}\nQual é o sintoma correto?: ").strip().upper()
                save_feedback(log_data, False, correct_cat)
                print("[+] Correção aprendida e arquivada.\n")

    except KeyboardInterrupt:
        print("\n\nEncerrando Pipeline com segurança. Bom descanso!")
        sys.exit(0)

if __name__ == "__main__":
    main()