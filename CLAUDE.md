# CLAUDE.md

Guidance for Claude Code when working in this repository.

## Project

**Caixa Log Diagnostic Pipeline (LDP)** — a Level-1 forensic triage assistant for 2CX/Caixa support
tickets. It reads free-text GLPI tickets plus attached logs, infers the reported symptom via TF-IDF,
and demands the mandatory technical evidence before a ticket may advance to deep analysis.

**Hard constraint: the system runs 100% offline.** No outbound network calls, no cloud NLP, no
telemetry, no CDN assets (the HTML test page inlines all its CSS/JS for exactly this reason). PII is
masked (LGPD) before any analysis touches the text. Do not introduce a dependency that phones home.

Design document: `SSD.md` (pt-BR). Its §6 holds the phased roadmap; keep it in sync when you ship a
phase.

---

## 1. Build & Development Commands

### Interpreter

Use **`py`**, not `python`. On this machine `python` resolves to the Microsoft Store stub and fails
with "Python was not found".

```bash
py -m pip install -e ".[dev]"     # canonical install (editable, with test deps)
```

The project is a proper src-layout package (`src/ldp`), installed as `ldp`. Installing editable is
required before anything below will run.

### CLI

```bash
# Inline text
py -m ldp --text "a ligacao caiu no meio da chamada as 14:30, telefone (11) 98765-4321"

# From a file (UTF-8, UTF-8-BOM and cp1252 are all handled)
py -m ldp --text-file data/raw/req123456789012.md

# With attachments (.txt/.log/.csv/.json logs, .png/.jpg/.pdf images, or .zip)
py -m ldp --text-file chamado.md --attachments log1.log evidencia.png anexos.zip

# Machine-readable output (no banner on stdout)
py -m ldp --text "..." --json

# Legacy interactive paste mode (type FIM on its own line)
py -m ldp --interactive

# Fase 8 RPA stub — pulls a random mock ticket from data/raw/
py -m ldp --api-source
```

`--text`, `--text-file` and `--api-source` are mutually exclusive. `--verbose` / `--quiet` control
stderr diagnostics; the report always goes to stdout.

**Exit codes** — the automation contract; preserve it:

| Code | Meaning |
|---|---|
| `0` | Approved — evidence present, proceed to technical analysis |
| `1` | Blocked — mandatory evidence missing, return to the requester |
| `2` | Attention — symptom not recognised, needs human analysis |
| `3` | Error — ticket could not be loaded or parsed |

### Flask API

```bash
py -m ldp.api.app                                       # dev server, 127.0.0.1:8000
py -m waitress --port=8000 "ldp.api:create_app"         # production (Windows)
gunicorn "ldp.api:create_app()" --bind 0.0.0.0:8000     # production (Linux)
```

| Route | Purpose |
|---|---|
| `GET /` | Interactive test page — paste a ticket, attach files, see the verdict |
| `POST /api/v1/screenings` | Screen a ticket (JSON `{"texto": ...}` or multipart with `anexos`) |
| `GET /api/v1/symptoms` | Taxonomy + validation matrix |
| `GET /healthz` / `GET /readyz` | Liveness / readiness (readiness probes the vectorizer) |

HTTP `200` approved or attention, `422` blocked, `400` malformed, `413` too large. Errors are
`application/problem+json` (RFC 7807).

```bash
curl -s localhost:8000/api/v1/screenings \
  -H 'Content-Type: application/json' \
  -d '{"texto":"a ligacao caiu as 14:30, telefone (11) 98765-4321"}'
```

### Tests

```bash
py -m pytest                        # full suite
py -m pytest -m "not slow"          # skip massive-log performance tests
py -m pytest --cov=ldp --cov-report=term-missing
py -m pytest tests/unit/test_entities.py -q
```

### Mock data generator

```bash
py tests/mock_generator.py                          # 50 pairs into data/raw/
py tests/mock_generator.py --count 200 --seed 42    # reproducible corpus
```

Writes `req<12 digits>.md` + `req<12 digits>_log.txt`. `data/` is gitignored — never commit
generated data. **Run this before `--api-source`**, which reads that pool.

`--seed` makes output byte-for-byte reproducible; without it, timestamps and IDs vary.

---

## 2. Architecture & Business Rules

### Module map

| Path | Role |
|---|---|
| `src/ldp/cli.py` | Thin CLI: parse args → load ticket → `screen_ticket` → render → exit code |
| `src/ldp/config.py` | **Every** tunable constant, env-var overridable |
| `src/ldp/core/screening.py` | The pipeline + the validation matrix. All business rules live here |
| `src/ldp/core/entities.py` | PII detection & masking (typed spans + confidence) |
| `src/ldp/core/glpi_parser.py` | Legacy GLPI block/field parser |
| `src/ldp/core/result.py` | `ScreeningResult` — the serializable verdict |
| `src/ldp/core/reporting.py` | Text rendering of a result (presentation only) |
| `src/ldp/core/ticket_source.py` | Port & Adapter: local disk, API stub, archive handling |
| `src/ldp/core/symptoms.py` | `Symptom` enum — the single source of truth for the taxonomy |
| `src/ldp/core/errors.py` | `LdpError` hierarchy with pt-BR messages |
| `src/ldp/engines/nlp_engine.py` | TF-IDF (char n-grams) + cosine similarity |
| `src/ldp/engines/log_analyzer.py` | Regex error signatures over attached logs |
| `src/ldp/api/` | Flask factory + the self-contained HTML test page |
| `tests/mock_generator.py` | Synthetic ticket/log factory |

Dependency direction: `cli`/`api` → `core.screening` → `engines` → `core` → stdlib. No cycles.

### The central seam

`screen_ticket(ticket) -> ScreeningResult` (`core/screening.py`) is **pure** — it prints nothing and
touches no terminal. The CLI renders it as text; the API serializes it as JSON. Both are views over
one implementation.

**Never put a business rule in `cli.py` or `api/app.py`.** If the CLI and the API can disagree about
a ticket, the refactor that made this phase possible has been undone.

### Rule: O(1) memory on massive logs

- `scan_log_file()` is a generator reading line by line. Never `.read()`, `.readlines()`, or
  materialize a log into a list.
- `analyze_logs()` counts incrementally and retains at most `config.LOG_MAX_RETAINED_HITS`
  excerpts, so peak memory is independent of file size *and* of match count.
- Open logs with `encoding='utf-8', errors='replace'` — production logs contain invalid bytes.
- The nine signatures are compiled into **one** alternation with named groups: one `re.search` per
  line, first match wins. Do not reintroduce a per-signature loop.

### Rule: GLPI parsing and the ` -> ` delimiter

Real exports are six blocks of `chave -> valor` pairs (delimiter: space, hyphen, greater-than,
space):

```
Detalhes:
detalhe1 -> A ligacao do agente p125413 caiu no meio do atendimento as 14:30
detalhe2 -> 192.168.0.14
detalhe3 -> TEL: 987654321
```

- Analysis text comes from an **allow-list** (`ANALYSIS_BLOCKS = {'detalhes'}`), not from
  truncating at a header. Block order, casing, accents and spacing therefore do not matter.
- Only `detalhe1` (the narrative) feeds inference and evidence. `detalhe2`..`detalhe11` are
  **deliberate decoys** — fake CPFs, IPs, addresses and phone numbers built to trap naive
  extraction. Never widen `ANALYSIS_BLOCKS` without understanding this.
- The `Info Solicitante:` block holds the **caller's** contact details, not the affected line.
  Letting it reach the evidence rules is the original false positive this design exists to prevent.

### Rule: evidence comes from typed entities, never from mask tokens

`core/entities.py` detects PII as typed spans with a confidence level, then masks as a separate
step. Evidence rules call `has_entity(entities, EntityKind.TELEFONE, Confidence.HIGH)`.

**Do not validate by searching for `[TELEFONE_MASK]` in text.** That is what produced the two live
bugs this phase fixed (see Known Pitfalls). HIGH confidence means the value was punctuated as a
phone or introduced by a cue word; a bare digit run is MEDIUM — still masked for privacy, but not
accepted as proof the requester supplied a number.

### Validation matrix

| Symptom | Required evidence |
|---|---|
| `FALHA_CHAMADA_AUDIO` | HIGH-confidence phone **AND** time of occurrence |
| `FALHA_INTERFACE_UX` | Screenshot/console log mentioned, or a visual/log attachment |
| `FALHA_FILA_ROTEAMENTO` | Time of occurrence |
| `SINTOMA_DESCONHECIDO` / `TEXTO_MUITO_CURTO` | None — routed to human analysis (exit 2) |

Rules live in `CONTEXT_RULES` (`core/screening.py`). Symptom labels come from the `Symptom` enum —
never re-type them as bare strings.

### Extension point

`TicketSource` (`core/ticket_source.py`) is the port. `ApiTicketSource` accepts an injectable
`fetcher` returning `{'id', 'text', 'attachment_paths'}`. To wire the real Service Desk API,
implement that fetcher — **do not modify the pipeline**.

---

## 3. Code Style & Guidelines

### Language policy — this is strict

**English** — everything a developer reads:
- Variable, function, class, and module names
- Docstrings and inline comments
- Commit messages
- Log messages emitted for operators
- Exception class names

**Brazilian Portuguese (pt-BR)** — everything a user or integrator reads:
- CLI report labels and status lines (`[SINTOMA INFERIDO]`, `BLOQUEADO - FALTAM DADOS`)
- Missing-evidence messages in `CONTEXT_RULES` (`"Horário da ocorrência"`)
- **API JSON keys and values** (`chamado`, `sintoma`, `pendencias`, `codigo_saida`)
- `LdpError` messages — they are shown directly to the user
- The HTML test page
- Mock data strings in `tests/mock_generator.py`
- `SSD.md` and design documentation

Symptom identifiers (`FALHA_CHAMADA_AUDIO`) are pt-BR domain terms and stay as they are — business
vocabulary, not code style.

```python
def validate_context(symptom: Symptom, text: str, ...) -> Tuple[EvidenceCheck, ...]:
    """Run the evidence rules for `symptom`."""      # English docstring
    # English comment explaining the why...
    return (EvidenceCheck('Horário da ocorrência', True),)   # pt-BR user-facing string
```

### Conventions

- **Type hints on every function signature.**
- **Pre-compile regexes at module scope**, never inside a loop or function body.
- **`@dataclass(frozen=True)` for pipeline data.** It is immutable end to end.
- **Return tuples, not lists,** from functions producing pipeline data.
- **Constants go in `config.py`**, read from env with a default. No magic numbers in logic.
- Expensive module-level work (the TF-IDF fit in `nlp_engine.py`) runs once at import, never per
  request or per ticket.
- Raise `LdpError` subclasses for anything the user can cause; let the CLI/API translate them. Never
  let a traceback reach the user.
- Use `logging` (stderr), not `print`, everywhere except the report renderer.
- Commit messages follow Conventional Commits: `feat(engines):`, `fix(cli):`, `refactor(core):`.

---

## 4. Known Pitfalls

**Fixed in Fase 4 — do not reintroduce.** Each has a regression test; if you are tempted to
"simplify" one of these, read the test first.

- **11-digit mobile masked as CPF.** A Brazilian mobile with area code is exactly 11 digits, and the
  old CPF pattern `\b\d{11}\b` claimed it first, so `telefone 11987654321` became `[CPF_MASK]` and
  the ticket was blocked for a phone number that was present. Disambiguation now uses lexical cues
  plus DDD/9th-digit structure (`entities._is_brazilian_phone`).
- **Protocol number accepted as a phone.** The old phone pattern matched any 7-12 digit run, so
  `protocolo 1234567890` satisfied the audio evidence rule on a ticket with no phone at all.
  `PROTOCOLO` is now detected at higher priority and claims the span.
- **`Jitter Alto` signature never fired.** `jitter[:\s]+\d+\s*ms` cannot match
  `High Jitter detected: 350ms` — the word `detected` sits between the label and the number.
- **False `DIVERGÊNCIA`.** `matches_nlp_symptom` was `dominant == nlp_symptom`, which is always
  False for an unresolved symptom, so the report announced a conflict against
  `SINTOMA_DESCONHECIDO`. It is `None` when the symptom is unresolved.
- **`.log` attachments ignored.** `LOG_EXTENSIONS` was `{'.txt'}` only.
- **cp1252 tickets crashed.** `read_text_file` now walks an encoding ladder.
- **Package was not importable.** Flat imports (`from core.x import ...`) meant no test suite could
  import the engines. Everything is `ldp.*` absolute now.
- **The word "tela" satisfied the screenshot rule.** It was in `EVIDENCE_REGEX`, and nearly every UX
  complaint contains it ("a tela ficou branca"), so the requirement was met by the sentence
  describing the problem. Removed.

**Still open / by design:**

- `ldp.config` reads env vars at **import time**; changing them mid-process has no effect. Tests
  must monkeypatch the module attribute, not the environment.
- Nested archives are not recursively extracted — a `.zip` inside a `.zip` lands in `other_files`.
- A ticket consisting solely of non-`Detalhes` blocks falls back to analysing the raw text. It
  classifies as `SINTOMA_DESCONHECIDO` and routes to a human, which is the intended safe outcome.
