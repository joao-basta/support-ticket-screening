import re
from functools import lru_cache

# ---------------------------------------------------------
# OTIMIZAÇÃO: Expressões Regulares pré-compiladas no escopo 
# global para evitar custo de recompilação (O(1)).
# ---------------------------------------------------------
PATTERNS = {
    # Máscara para CPFs (com pontuação ou 11 dígitos limpos)
    r'\b\d{3}\.\d{3}\.\d{3}-\d{2}\b|\b\d{11}\b': '[CPF_MASK]',
    
    # Máscara para CNPJs
    r'\b\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}\b|\b\d{14}\b': '[CNPJ_MASK]',
    
    # Máscara genérica para Contas/Agências (CC, Ag, Conta)
    r'\b(?:agencia|ag|cc|conta)\s*:?\s*\d{3,5}[-.]?\d{1,2}\b': '[CONTA_MASK]',
    
    # Máscara para Valores Financeiros (R$ XX.XXX,XX)
    r'(?i)R\$\s*\d{1,3}(?:\.\d{3})*,\d{2}\b': '[VALOR_MASK]'
}

# Lista de tuplas (RegEx Compilado, Token de Substituição)
COMPILED_PATTERNS = [
    (re.compile(p, re.IGNORECASE), repl) 
    for p, repl in PATTERNS.items()
]

@lru_cache(maxsize=4096)
def mask_sensitive_data(raw_text: str) -> str:
    """
    Substitui CPFs, CNPJs, números de contas e valores financeiros por tokens (ex: [CPF_MASK])[cite: 2].
    
    Programação Dinâmica: Utiliza lru_cache para guardar em memória (hash table) 
    os logs já limpos. Em cenários de spam de logs idênticos, a complexidade 
    cai de O(N) para O(1).
    """
    if not raw_text:
        return ""

    sanitized_text = raw_text
    for pattern, replacement in COMPILED_PATTERNS:
        sanitized_text = pattern.sub(replacement, sanitized_text)

    return sanitized_text