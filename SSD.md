# System Design Document (SDD)
## Caixa Log Diagnostic Pipeline (LDP) - V3 (Fase 4 + Fase 7)

### 1. Visão Geral (Overview)
O LDP é um Assistente de Triagem Forense Nível 1 construído em Python. Ele processa chamados de
suporte em texto livre (layout legado do GLPI ou texto solto) e arquivos de log anexados,
classificando o sintoma relatado e cobrando as evidências técnicas obrigatórias antes de o chamado
avançar para análise profunda.

O núcleo do pipeline é **puro e sem I/O**: `screen_ticket()` devolve um `ScreeningResult`
serializável. Sobre ele existem duas interfaces equivalentes — a **CLI** e a **API HTTP (Flask)** —
que nunca podem divergir no veredito, porque compartilham a mesma implementação de regras.

**Restrição de Segurança:** o sistema roda 100% offline (local), sem qualquer chamada de rede de
saída, e aplica sanitização de PII (LGPD) antes de qualquer análise. A página de teste HTML embute
todo o CSS/JS justamente para não depender de CDN.

### 2. Stack Tecnológica
* **Linguagem:** Python 3.10+ (validado em 3.12), empacotado em src-layout (`src/ldp`), instalável
  via `pip install -e .`.
* **NLP:** `scikit-learn` — TF-IDF sobre **n-gramas de caractere** (`char_wb`, 3–5) + Similaridade
  de Cosseno. O analisador de caractere é o que entrega a tolerância a erro de digitação; um
  analisador de palavra trata `ligacao`/`ligação`/`lgacao` como tokens sem relação.
* **Extração de Dados:** `re` (Expressões Regulares Nativas), sempre pré-compiladas em escopo de
  módulo.
* **API HTTP:** `Flask` 3.x com application factory (`create_app`), servida sob WSGI de produção.
* **Performance:** Generators para leitura de log em O(1) de memória, agregação incremental por
  `Counter`, e `functools.lru_cache` na inferência de sintoma (função determinística de entrada
  hashável — é onde o cache de fato rende).

### 3. Arquitetura do Sistema (Clean Architecture)
Direção de dependência: `cli`/`api` → `core.screening` → `engines` → `core` → stdlib. Sem ciclos.

* `/src/ldp/core/screening.py` -> **Pipeline.** `screen_ticket()` executa os 4 passos e devolve um
  `ScreeningResult`. **Toda** regra de negócio (a matriz de validação) vive aqui — nunca na CLI nem
  na API, sob pena de as duas interfaces divergirem.
* `/src/ldp/core/result.py` -> **Contrato de Saída.** `ScreeningResult` serializável, com chaves
  JSON em pt-BR. Expõe apenas *contagens* de dados sensíveis, nunca o valor detectado.
* `/src/ldp/core/entities.py` -> **Módulo de Segurança.** Detecta PII como *spans tipados com nível
  de confiança* e mascara como etapa separada. A validação de evidência consome entidades, não
  tokens de máscara — máscara é lossy e não distingue um telefone de um CPF.
* `/src/ldp/core/glpi_parser.py` -> **Parser Legado.** Interpreta os blocos do GLPI e os pares
  `chave -> valor`, tolerando variação de caixa, acento e espaçamento. A análise consome uma
  **allow-list** de blocos (`Detalhes:`), não um truncamento posicional.
* `/src/ldp/core/ticket_source.py` -> **Fonte de Chamado (Port & Adapter).** Contrato
  `TicketSource`, adapter local (com escada de encoding e extração segura de `.zip`) e o stub
  `ApiTicketSource` para a Fase 8.
* `/src/ldp/core/symptoms.py` -> **Taxonomia.** `Symptom` como `Enum`, fonte única da verdade.
* `/src/ldp/core/errors.py` -> **Erros.** Hierarquia `LdpError` com mensagens pt-BR prontas para
  exibição; nenhum traceback chega ao usuário.
* `/src/ldp/core/reporting.py` -> **Apresentação.** Renderiza um `ScreeningResult` como relatório
  de texto. Só apresentação.
* `/src/ldp/engines/nlp_engine.py` -> **Cérebro (Intenção).** Classifica o sintoma por similaridade
  contra um corpus de âncoras, incluindo uma **classe de rejeição** explícita.
* `/src/ldp/engines/log_analyzer.py` -> **Perícia Técnica.** Varre os logs anexados em streaming
  buscando assinaturas de erro e compara a causa técnica dominante com o sintoma inferido.
* `/src/ldp/api/` -> **Camada HTTP (Fase 7).** Factory Flask, endpoints REST e a página de teste
  interativa. Não contém regra de negócio.
* `/src/ldp/config.py` -> **Configuração.** Todas as constantes ajustáveis, sobrescrevíveis por
  variável de ambiente (12-factor).
* `/tests/mock_generator.py` -> **Fábrica de Caos.** Gera massa sintética; com `--seed` a saída é
  reproduzível byte a byte.

### 4. Fluxo de Execução (Pipeline Flow)
1. **Parsing (Passo 0a):** `glpi_parser.py` separa os blocos e devolve apenas o conteúdo
   permitido — na prática o campo `detalhe1` do bloco `Detalhes:`. Os campos `detalhe2..11` são
   **chamarizes deliberados** (CPF, IP, endereço e telefone falsos) e ficam de fora.
2. **Sanitização (Passo 0b):** `entities.py` detecta as entidades sensíveis e mascara o texto. A
   detecção roda sobre o texto original, então nenhuma regra destrói a entrada de outra.
3. **Classificação de Sintoma (Passo 1):** `nlp_engine.py` compara o texto com o corpus de âncoras.
   O sintoma com maior similaridade vence; se a **classe de rejeição** vencer, ou se o escore ficar
   abaixo do limiar de `config.NLP_CONFIDENCE_THRESHOLD`, o resultado é `SINTOMA_DESCONHECIDO`.
   Empates são resolvidos pelo rótulo, de forma reproduzível.
4. **Validação Contextual (Passo 2):** `screening.py` cobra as evidências do sintoma inferido,
   consultando as **entidades tipadas** e os anexos resolvidos. A evidência de telefone exige
   confiança ALTA: um número pontuado como telefone ou introduzido por uma palavra-pista. Uma
   corrida de dígitos ambígua continua sendo mascarada, mas não vale como prova.
5. **Análise de Arquivo (Passo 3):** `log_analyzer.py` lê os logs anexados linha a linha buscando
   assinaturas de erro. Sinaliza divergência **apenas** quando o sintoma do texto foi resolvido —
   um sintoma desconhecido não diverge de nada. Nunca bloqueia o chamado.
6. **Output:** a CLI renderiza o relatório de texto (ou `--json`); a API serializa o mesmo
   `ScreeningResult`. Código de saída: `0` aprovado, `1` bloqueado, `2` análise humana, `3` erro.

### 5. Matriz de Validação (Sintoma x Evidência)
* **FALHA_CHAMADA_AUDIO:** exige "Número do Telefone/Alvo" (confiança ALTA) **E** "Horário da
  Ocorrência".
* **FALHA_INTERFACE_UX:** exige "Print da tela, log de console ou anexo" — menção textual a um
  artefato **ou** um anexo visual/log de fato.
* **FALHA_FILA_ROTEAMENTO:** exige "Horário da Ocorrência".
* **SINTOMA_DESCONHECIDO / TEXTO_MUITO_CURTO:** sem exigências. Não há o que cobrar, então o
  chamado vai para análise humana (saída `2`) em vez de voltar ao solicitante.

### 6. Roadmap & Próximos Passos

Estado atual: **V3** — Fases 4 e 7 implementadas; Fase 5 (suíte de testes) em andamento.

| Fase | Escopo | Situação |
|---|---|---|
| 4 | Fundação testável & contrato de dados | ✅ Concluída |
| 5 | Automação de testes | 🔄 Em andamento |
| 6 | Prontidão para produção | ⬜ Pendente (resta CI, logging estruturado, LGPD formal) |
| 7 | API HTTP (Flask) | ✅ Concluída |
| 8 | RPA / ingestão autônoma | ⬜ Pendente |

Itens da Fase 6 já antecipados na Fase 4: `char_wb` n-gramas no NLP, agregação incremental no
`analyze_logs`, `logging` no lugar de `print`, `config.py` por variável de ambiente, e remoção de
`nltk`/`spacy` do `requirements.txt`.

#### Fase 4 - Fundação Testável & Contrato de Dados ✅
* **Empacotamento:** `pyproject.toml` e imports absolutos (`ldp.*`), tornando o projeto
  instalável (`pip install -e .`) e importável por suítes de teste. Hoje os imports são
  achatados (`from core.anonymizer import ...`), então `python -m src.cli` falha e nenhuma
  suíte consegue importar os motores.
* **Extração de Entidades:** separar *detecção* de *mascaramento* no `anonymizer`. Hoje a
  validação de evidência checa a presença do token `[TELEFONE_MASK]`, o que produz falso
  negativo (celular de 11 dígitos é capturado antes pela regex de CPF) e falso positivo
  (número de protocolo de 12 dígitos é lido como telefone). A validação passará a consumir
  entidades tipadas, não texto mascarado.
* **Parser GLPI:** substituir o `split()` literal em `Info Solicitante:` por um parser de
  blocos tolerante a variação de caixa, acento e espaçamento, com extração dos pares
  `chave -> valor` e isolamento do campo de narrativa (`detalhe1`) do ruído burocrático.
* **Resultado Estruturado:** extrair `screen_ticket() -> ScreeningResult` (serializável) do
  atual `process_ticket()`, deixando a impressão do relatório como camada de apresentação.
  É o pré-requisito da API HTTP (Fase 7).
* **Tratamento de Erros:** hierarquia `LdpError`, detecção de encoding (utf-8 → cp1252) para
  exports legados, e proteção contra zip bomb além do guard de Zip Slip já existente.

#### Fase 5 - Automação de Testes
* **Unitários:** motor NLP (matriz sintoma×texto, limiar de confiança, tolerância a typo),
  anonimizador (regressão dos falsos positivos/negativos de PII, idempotência) e parser GLPI
  (variantes de cabeçalho, blocos fora de ordem).
* **Integração:** matriz de exit codes da CLI (0/1/2) via `subprocess`, ingestão de anexos
  `.zip`, e uma fixture por assinatura de erro garantindo que todas as 9 disparam.
* **Performance:** teste marcado como `slow` sobre log sintético de ~5 milhões de linhas,
  assertando teto de memória via `tracemalloc` — prova objetiva do streaming O(1).
* **Meta de cobertura:** 80% em `core/` e `engines/`.

#### Fase 6 - Prontidão para Produção
* **NLP:** migrar para `analyzer='char_wb'` com n-gramas, entregando de fato a tolerância a
  erro de digitação prometida na §1, e ampliar o corpus de âncoras (hoje 3 documentos, o que
  torna o IDF estatisticamente degenerado).
* **Performance:** agregação incremental no `analyze_logs` (a leitura por linha já é
  generator, mas os hits são materializados em lista), assinatura única por alternação
  nomeada, e limite configurável de ocorrências.
* **Observabilidade:** substituir `print` por `logging` com níveis, diagnósticos em stderr e
  `correlation_id` por chamado.
* **Entrega:** CI com lint, type-check e cobertura; dependências pinadas (removendo `nltk` e
  `spacy`, hoje declarados mas não utilizados).

#### Fase 7 - API HTTP (Flask)
Exposição do pipeline como serviço, consumindo o `ScreeningResult` da Fase 4:
* `POST /api/v1/screenings` - triagem de chamado com anexos (`multipart/form-data`).
* `GET /healthz` / `GET /readyz` - liveness e readiness.
* `GET /api/v1/symptoms` - taxonomia e matriz de validação vigentes.

Requisitos transversais: limite de tamanho e allow-list de upload, erros em
`application/problem+json` (RFC 7807), execução sob WSGI de produção (gunicorn/waitress) e
carga única do vetorizador por worker.

#### Fase 8 - RPA / Ingestão Autônoma
Ingestão autônoma via API REST do Service Desk da 2CX, sem input manual (Human-in-the-loop).
O ponto de extensão já existe: `ApiTicketSource` recebe um `fetcher` injetável, bastando
implementar a chamada HTTP real (com retry/backoff, token em variável de ambiente e controle
de idempotência) mantendo o contrato de payload `{id, text, attachment_paths}`.