# System Design Document (SDD)
## Caixa Log Diagnostic Pipeline (LDP) - MVP V2

### 1. Visão Geral (Overview)
O LDP é uma ferramenta CLI (Command Line Interface) construída em Python para atuar como um Assistente de Triagem Forense Nível 1. Ele processa chamados de suporte em texto livre (Linguagem Natural) e arquivos de log anexados, classificando sintomas e cobrando evidências técnicas obrigatórias.
**Restrição de Segurança:** O sistema roda 100% offline (local) e aplica sanitização de PII (LGPD) antes de qualquer análise.

### 2. Stack Tecnológica
* **Linguagem:** Python 3.10+
* **Processamento de Linguagem Natural (NLP):** `scikit-learn` (TF-IDF Vectorizer + Similaridade de Cosseno).
* **Extração de Dados/Linter:** `re` (Expressões Regulares Nativas).
* **Performance:** `functools.lru_cache` e Generators para processamento de arquivos grandes em O(1) sem memory leak.

### 3. Arquitetura do Sistema (Clean Architecture)
O projeto é modularizado para separar responsabilidades:
* `/src/cli.py` -> **Orquestrador.** Gerencia o fluxo de entrada/saída, lê os arquivos e aplica as regras de validação (Fail Fast).
* `/src/core/anonymizer.py` -> **Módulo de Segurança.** Intercepta o texto bruto e mascara CPFs, CNPJs, Telefones e Dados Bancários.
* `/src/engines/nlp_engine.py` -> **Cérebro (Intenção).** Transforma o texto do chamado em vetores matemáticos para classificar o Sintoma relatado ignorando erros de digitação.
* `/tests/mock_generator.py` -> **Fábrica de Caos.** Gera massa de dados sintética (chamados e logs falsos) para testes unitários isolados.

### 4. Fluxo de Execução (Pipeline Flow)
O sistema processa a informação na seguinte ordem cronológica:
1. **Sanitização (Passo 0):** O texto do chamado é mascarado.
2. **Classificação de Sintoma (Passo 1):** O `nlp_engine.py` lê o texto e deduz o problema com um *Score de Confiança* (> 85%).
3. **Validação Contextual (Passo 2):** Baseado no sintoma inferido, o `cli.py` usa Regex para buscar as evidências vitais (Ex: se for erro de áudio, cobra Horário e Telefone obrigatoriamente).
4. **Análise de Arquivo (Passo 3):** Lê o log `.txt` anexado buscando Códigos de Erro reais (Ex: HTTP 500, Jitter).
5. **Output / Feedback:** Retorna a ação sugerida ao analista (Bloquear chamado por falta de dados ou Avançar para análise profunda).

### 5. Matriz de Validação (Sintoma x Evidência)
O núcleo das regras de negócio atua sob a seguinte matriz de cobrança:
* **FALHA_INTERFACE_UX:** Exige presença de "Print/Log de Console".
* **FALHA_CHAMADA_AUDIO:** Exige "Número de Telefone" E "Horário da Ocorrência".
* **FALHA_FILA_ROTEAMENTO:** Exige "Horário da Ocorrência".

### 6. Próximos Passos (Fase 3 - RPA)
No futuro, o sistema fará a ingestão autônoma de chamados via API REST do Service Desk da 2CX, sem necessidade de input manual (Human-in-the-loop).