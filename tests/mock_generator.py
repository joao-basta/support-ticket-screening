import argparse
import os
import random
import string
import sys
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

#: Fixed epoch used for log timestamps when a seed is supplied, so that a
#: seeded corpus is reproducible. None means "anchor to the current time".
_BASE_TIME: Optional[datetime] = None

# ---------------------------------------------------------
# 1. FAKE PII GENERATORS
# Helper functions that synthesize sensitive-looking data.
# ---------------------------------------------------------

def _generate_fake_cpf() -> str:
    """Generate a fake CPF number in the XXX.XXX.XXX-XX format."""
    return f"{random.randint(100, 999)}.{random.randint(100, 999)}.{random.randint(100, 999)}-{random.randint(10, 99)}"

def _generate_fake_cnpj() -> str:
    """Generate a fake CNPJ number in the XX.XXX.XXX/0001-XX format."""
    return f"{random.randint(10, 99)}.{random.randint(100, 999)}.{random.randint(100, 999)}/0001-{random.randint(10, 99)}"

def _generate_fake_phone_formatted() -> str:
    """Generate a fake mobile phone number in the (XX) 9XXXX-XXXX format (with DDD/parens)."""
    return f"({random.randint(11, 99)}) 9{random.randint(1000, 9999)}-{random.randint(1000, 9999)}"

def _generate_fake_phone_raw() -> str:
    """Generate a fake mobile number as a bare digit run, no DDD/parens/dashes
    (mirrors real GLPI tickets where the phone is pasted straight from the PABX log,
    e.g. '987654321')."""
    digit_count = random.choice([9, 10])
    return f"9{''.join(str(random.randint(0, 9)) for _ in range(digit_count - 1))}"

def _generate_fake_phone() -> str:
    """Generate a fake phone number, mostly formatted but occasionally a bare digit
    run, matching the variety seen in real tickets."""
    return _generate_fake_phone_formatted() if random.random() < 0.7 else _generate_fake_phone_raw()

def _generate_fake_agent_id() -> str:
    """Generate a fake agent registration ID in the pXXXXXX format."""
    return f"p{random.randint(100000, 999999)}"

def _generate_fake_ip() -> str:
    """Generate a fake private IPv4 address."""
    return f"192.168.{random.randint(0, 255)}.{random.randint(1, 254)}"

def _generate_fake_name() -> str:
    """Generate a fake Brazilian full name."""
    return f"{random.choice(FIRST_NAMES)} {random.choice(LAST_NAMES)}"

def _generate_fake_request_number() -> str:
    """Generate a fake GLPI-style request number (12 digits)."""
    return "".join(str(random.randint(0, 9)) for _ in range(12))

def _generate_fake_bank_code() -> str:
    """Generate a fake bank/branch code in the digit-letter-digit format (e.g. '1X4'),
    mirroring the 'codigodobanco' field of real tickets."""
    return f"{random.randint(0, 9)}{random.choice(string.ascii_uppercase)}{random.randint(0, 9)}"

def _generate_fake_contract_number() -> str:
    """Generate a fake supplier contract number."""
    return f"CT-{random.randint(100000, 999999)}"

def _random_hex(length: int) -> str:
    """Generate a random hex string.

    Uses `random` rather than `uuid.uuid4()` so that seeding the module
    actually makes the generated logs reproducible - uuid4 draws from the OS
    entropy pool and ignores `random.seed`.
    """
    return "".join(random.choice("0123456789abcdef") for _ in range(length))

def _generate_fake_time() -> str:
    """Generate a fake time of occurrence in one of the formats analysts actually type."""
    hour, minute = random.randint(0, 23), random.randint(0, 59)
    return random.choice([f"{hour:02d}:{minute:02d}", f"{hour:02d}h{minute:02d}", f"{hour:02d}h"])

# ---------------------------------------------------------
# 2. DATA POOLS (pt-BR content)
# All human-facing strings below simulate real legacy GLPI
# tickets written by Brazilian support staff.
# ---------------------------------------------------------

FIRST_NAMES: List[str] = [
    "Maria", "João", "Ana", "Carlos", "Fernanda", "Paulo", "Juliana", "Rafael",
    "Camila", "Bruno", "Patrícia", "Lucas", "Beatriz", "Gustavo", "Larissa",
]

LAST_NAMES: List[str] = [
    "Fernandes", "Silva", "Oliveira", "Souza", "Costa", "Pereira", "Almeida",
    "Ribeiro", "Carvalho", "Gomes", "Martins", "Barbosa", "Rocha", "Dias", "Nascimento",
]

SERVER_LOCATIONS: List[str] = [
    "Central", "Salvador", "Juiz de Fora", "Recife", "Curitiba", "Belo Horizonte",
    "Fortaleza", "Campinas",
]

INCIDENT_CATEGORIES: List[str] = [
    "Incidente - Telefonia / VOIP",
    "Incidente - Sistema / Aplicativo",
    "Incidente - Rede / Conectividade",
    "Solicitação - Acesso / Permissão",
    "Incidente - Hardware / Periférico",
]

SLA_OPTIONS: List[str] = [
    "4 Horas Úteis", "8 Horas Úteis", "24 Horas Corridas", "2 Horas Úteis (Crítico)",
]

STATUS_OPTIONS: List[str] = [
    "Triagem", "Em Atendimento", "Aguardando Retorno do Cliente", "Escalado Nível 2",
]

OS_OPTIONS: List[str] = [
    "Windows 10 Pro", "Windows 11 Enterprise", "Windows Server 2016", "Windows 10 Enterprise LTSC",
]

HARDWARE_OPTIONS: List[str] = [
    "Roteador_Borda_01", "Headset_Plantronics_HW", "Switch_Andar3_Rack2", "Estação_Dell_OptiPlex",
]

CHANNEL_OPTIONS: List[str] = [
    "Abertura via Portal Web", "Abertura via Telefone - Central de Serviços",
    "Abertura via E-mail", "Abertura via Chatbot Interno",
]

STREET_NAMES: List[str] = [
    "Av. Paulista", "Rua das Flores", "Av. Rio Branco", "Rua XV de Novembro",
    "Av. Brasil", "Rua Sete de Setembro",
]

CITIES: List[str] = [
    "São Paulo, SP", "Rio de Janeiro, RJ", "Salvador, BA", "Belo Horizonte, MG",
    "Curitiba, PR", "Recife, PE",
]

DEPARTMENTS: List[str] = [
    "TI Corporativo", "Atendimento ao Cliente", "Operações Central",
    "Recursos Humanos", "Financeiro Matriz", "Telefonia / VOIP",
]

SUPPLIERS: List[str] = [
    "Alpha Telecom", "Beta Sistemas Ltda", "Gamma Infraestrutura",
    "Delta Soluções TI", "Ômega Redes Corporativas",
]

ANEXO_OPTIONS: List[str] = [
    "print_tela_erro.png", "log_chamada.txt", "evidencia.pdf",
    "Nenhum anexo informado", "captura_console.png",
]

# ---------------------------------------------------------
# 3. FORMAL ISSUE DESCRIPTIONS (Detalhes: / detalhe1)
# Verbose, bureaucratic phrasing that embeds the AFFECTED
# agent ID and AFFECTED phone number, mirroring real tickets.
# ---------------------------------------------------------

FORMAL_ISSUE_TEMPLATES: Dict[str, List[str]] = {
    'FALHA_INTERFACE_UX': [
        "Prezado suporte técnico, venho relatar uma anomalia crítica na interface do sistema de atendimento. "
        "O colaborador afetado, matrícula {agent_id}, relatou o congelamento total da tela durante a geração "
        "de relatório, impossibilitando a conclusão da tarefa. Para contato e acompanhamento do chamado, o "
        "telefone informado foi {phone}. Solicito análise urgente da equipe de infraestrutura.",
        "Registro o presente chamado para formalizar uma instabilidade recorrente na camada de apresentação "
        "do sistema corporativo. O usuário impactado, cadastro {agent_id}, não conseguiu prosseguir com o "
        "atendimento devido a uma tela em branco persistente. O contato para retorno é o número {phone}. "
        "Aguardo posicionamento da equipe responsável.",
    ],
    'FALHA_CHAMADA_AUDIO': [
        "Prezado suporte técnico, venho por meio deste relatar uma instabilidade operacional severa ocorrida "
        "durante a tratativa telefônica. O colaborador afetado, identificado pela matrícula {agent_id}, "
        "experienciou uma mudez repentina no áudio do softphone enquanto estava em linha ativa com o cliente "
        "através do número {phone}. Solicito a averiguação imediata do ocorrido para mitigar impactos nos "
        "indicadores de TMA da operação.",
        "Comunico formalmente uma degradação severa na qualidade de áudio durante o atendimento telefônico. "
        "O agente de matrícula {agent_id} relatou picotamento constante e ruídos de robotização durante ligação "
        "ativa com o número {phone}, culminando na queda abrupta da chamada. Peço prioridade máxima na apuração "
        "técnica.",
    ],
    'FALHA_FILA_ROTEAMENTO': [
        "Venho por meio deste formalizar uma falha crítica no motor de roteamento de chamadas. O colaborador "
        "de matrícula {agent_id}, mesmo disponível e logado corretamente, deixou de receber chamadas da fila "
        "ativa, enquanto o número de contato para follow-up permanece sendo {phone}. Solicito verificação "
        "emergencial do skill atribuído.",
        "Relato uma inconsistência grave no direcionamento automático de chamadas. O agente matrícula {agent_id} "
        "permaneceu ocioso por período prolongado apesar da fila com clientes aguardando, sendo o telefone "
        "{phone} o contato válido para esclarecimentos. Peço análise prioritária da equipe de telefonia.",
    ],
}

# ---------------------------------------------------------
# 4. ROOT CAUSE INJECTOR (Technical Logs)
# Maps a symptom to the technical error line that gets buried
# inside the massive log file.
# ---------------------------------------------------------

TECHNICAL_ROOT_CAUSE: Dict[str, List[str]] = {
    'FALHA_INTERFACE_UX': [
        "ERROR: Uncaught TypeError: Cannot read properties of null (reading 'value')",
        "FATAL: JavaScript heap out of memory",
        "SERVER_RESPONSE: 500 Internal Server Error on /api/reports",
    ],
    'FALHA_CHAMADA_AUDIO': [
        "NETWORK_METRIC: High Jitter detected: 350ms",
        "VOIP_STATUS: UDP Packet Loss exceeded 15%",
        "SIP/408: Request Timeout - No response from client endpoint",
    ],
    'FALHA_FILA_ROTEAMENTO': [
        "ACD_QUEUE: ERROR - QueueID 7 overflow. Max capacity reached.",
        "AGENT_STATE: Mismatch - Agent 3012 state is 'Available' but channel is not open.",
        "ROUTING_ENGINE: No available agent found for skill 'Vendas_Premium'",
    ],
}

# ---------------------------------------------------------
# 5. DETALHES NOISE POOL (detalhe2 .. detalhe11)
# Each generator produces one unrelated value meant to trap
# naive regex extraction (locations, decoy phones, IDs, ...).
# ---------------------------------------------------------

def _noise_location() -> str:
    return random.choice(SERVER_LOCATIONS)

def _noise_cnpj() -> str:
    return _generate_fake_cnpj()

def _noise_cpf() -> str:
    return _generate_fake_cpf()

def _noise_name() -> str:
    return _generate_fake_name()

def _noise_category() -> str:
    return random.choice(INCIDENT_CATEGORIES)

def _noise_address() -> str:
    return f"{random.choice(STREET_NAMES)}, {random.randint(1, 9999)} - {random.choice(CITIES)}"

def _noise_sla() -> str:
    return random.choice(SLA_OPTIONS)

def _noise_status() -> str:
    return random.choice(STATUS_OPTIONS)

def _noise_os() -> str:
    return random.choice(OS_OPTIONS)

def _noise_ip() -> str:
    return _generate_fake_ip()

def _noise_hardware() -> str:
    return random.choice(HARDWARE_OPTIONS)

def _noise_channel() -> str:
    return random.choice(CHANNEL_OPTIONS)

def _noise_decoy_phone() -> str:
    return _generate_fake_phone_formatted()

def _noise_decoy_tel_raw() -> str:
    return f"TEL: {_generate_fake_phone_raw()}"

DETALHES_NOISE_GENERATORS = [
    _noise_cnpj, _noise_cpf, _noise_name, _noise_category, _noise_address,
    _noise_sla, _noise_status, _noise_os, _noise_ip, _noise_hardware,
    _noise_channel, _noise_decoy_phone, _noise_decoy_tel_raw,
]

# ---------------------------------------------------------
# 6. TICKET BLOCK BUILDERS (real GLPI layout)
# Headers + 'chave -> valor' lines, delimiter is ' -> ' (space,
# single dash, greater-than). Blocks are joined with a blank line.
# ---------------------------------------------------------

#: Sentence appended to the narrative carrying the time of occurrence.
#: Without it every FALHA_CHAMADA_AUDIO and FALHA_FILA_ROTEAMENTO mock was
#: blocked on "Horário da ocorrência", so the generated corpus could never
#: exercise the approved path at all.
OCCURRENCE_TIME_TEMPLATES: List[str] = [
    "O ocorrido foi registrado as {time}.",
    "O incidente teve inicio as {time}.",
    "Horario aproximado da falha: {time}.",
    "Conforme apurado, o problema comecou as {time}.",
]

def _generate_detalhes_block(symptom: str) -> Tuple[str, str, str]:
    """Build the 'Detalhes:' block (detalhe1..detalhe11) and return it along
    with the AFFECTED agent ID/phone (Ctrl+F anchor for the log)."""
    affected_agent_id = _generate_fake_agent_id()
    affected_phone = _generate_fake_phone()

    issue_description = random.choice(FORMAL_ISSUE_TEMPLATES[symptom]).format(
        agent_id=affected_agent_id, phone=affected_phone
    )
    issue_description += " " + random.choice(OCCURRENCE_TIME_TEMPLATES).format(
        time=_generate_fake_time()
    )

    # detalhe2..detalhe11: at least one location (e.g. "Juiz de Fora"), the rest
    # sampled from the wider noise pool so decoy IDs/phones don't always land
    # in the same slot.
    noise_values = [_noise_location()] + [
        gen() for gen in random.choices(DETALHES_NOISE_GENERATORS, k=9)
    ]
    random.shuffle(noise_values)

    lines = [f"detalhe1 -> {issue_description}"]
    lines.extend(f"detalhe{i} -> {value}" for i, value in enumerate(noise_values, start=2))

    return "Detalhes:\n" + "\n".join(lines), affected_agent_id, affected_phone

def _generate_info_solicitante_block() -> str:
    """Build the 'Info Solicitante:' block: the CALLER's own contact info.
    Everything from this header down is bureaucratic noise that the
    orchestrator must discard before handing the text to NLP/Regex."""
    lines = [
        f"contatonome -> {_generate_fake_name()}",
        f"contatotelefone -> {_generate_fake_phone()}",
        f"contatoemail -> {_generate_fake_agent_id()}@corp.caixa.gov.br",
    ]
    return "Info Solicitante:\n" + "\n".join(lines)

def _generate_categorizacao_block() -> str:
    lines = [
        f"categoria -> {random.choice(INCIDENT_CATEGORIES)}",
        f"sla -> {random.choice(SLA_OPTIONS)}",
        f"canalabertura -> {random.choice(CHANNEL_OPTIONS)}",
        f"codigodobanco -> {_generate_fake_bank_code()}",
    ]
    return "Categorizacao:\n" + "\n".join(lines)

def _generate_info_demandante_block() -> str:
    lines = [
        f"areasolicitante -> {random.choice(DEPARTMENTS)}",
        f"matriculademandante -> {_generate_fake_agent_id()}",
    ]
    return "Info Demandante:\n" + "\n".join(lines)

def _generate_info_arquivo_block() -> str:
    lines = [
        f"anexo -> {random.choice(ANEXO_OPTIONS)}",
    ]
    return "Info Arquivo:\n" + "\n".join(lines)

def _generate_info_fornecedor_block() -> str:
    lines = [
        f"fornecedor -> {random.choice(SUPPLIERS)}",
        f"contrato -> {_generate_fake_contract_number()}",
    ]
    return "Info Fornecedor:\n" + "\n".join(lines)

# ---------------------------------------------------------
# 7. TICKET & LOG ASSEMBLY
# ---------------------------------------------------------

def _build_ticket(symptom: str) -> Tuple[str, str, str]:
    """Assemble the full ticket body (all blocks, real header order) and
    return it along with the AFFECTED agent ID/phone."""
    detalhes_block, affected_agent_id, affected_phone = _generate_detalhes_block(symptom)

    blocks = [
        detalhes_block,
        _generate_info_solicitante_block(),
        _generate_categorizacao_block(),
        _generate_info_demandante_block(),
        _generate_info_arquivo_block(),
        _generate_info_fornecedor_block(),
    ]

    ticket_text = "\n\n".join(blocks)
    return ticket_text, affected_agent_id, affected_phone

LOG_SKILLS: List[str] = ["Vendas_Premium", "Suporte_N1", "Retenção", "Financeiro", "Cobranca_Ativa", "SAC_Geral"]
LOG_AGENT_STATES: List[str] = ["Available", "Busy", "Wrap-Up", "Paused", "Offline", "Ready"]
LOG_NODE_NAMES: List[str] = ["PBX-NODE-01", "PBX-NODE-02", "GATEWAY-SIP-03", "MEDIA-SERVER-01", "ACD-CORE-02"]
LOG_CODECS: List[str] = ["G.711", "G.729", "OPUS", "G.722"]
LOG_HOSTS: List[str] = ["sip-trunk.internal", "media.gateway.local", "acd-core.internal", "auth.service.local"]

def _log_event_routing() -> str:
    return f"INFO: ACD routing engine dispatched call to agent {_generate_fake_agent_id()} on skill '{random.choice(LOG_SKILLS)}'"

def _log_event_sip() -> str:
    return f"DEBUG: SIP INVITE received from trunk gateway {_generate_fake_ip()}"

def _log_event_session() -> str:
    return f"INFO: Session {_random_hex(12)} established on channel {random.randint(1, 48)}"

def _log_event_queue() -> str:
    return f"WARN: Queue {random.choice(LOG_SKILLS)} depth increased to {random.randint(1, 40)}"

def _log_event_agent_state() -> str:
    return f"INFO: Agent {_generate_fake_agent_id()} state changed to '{random.choice(LOG_AGENT_STATES)}'"

def _log_event_heartbeat() -> str:
    return f"DEBUG: Heartbeat OK for node {random.choice(LOG_NODE_NAMES)}"

def _log_event_call_bridge() -> str:
    return f"INFO: Call {_random_hex(10)} bridged successfully"

def _log_event_codec() -> str:
    return f"TRACE: Codec negotiation completed: {random.choice(LOG_CODECS)}"

def _log_event_dns() -> str:
    return f"DEBUG: DNS resolution for {random.choice(LOG_HOSTS)} took {random.randint(1, 250)}ms"

def _log_event_cache() -> str:
    return f"DEBUG: Cache hit ratio at {random.randint(50, 99)}%"

LOG_EVENT_GENERATORS = [
    _log_event_routing, _log_event_sip, _log_event_session, _log_event_queue,
    _log_event_agent_state, _log_event_heartbeat, _log_event_call_bridge,
    _log_event_codec, _log_event_dns, _log_event_cache,
]

def _generate_log_content(symptom: str, affected_phone: str) -> str:
    """
    Generate a massive fake log (1500-2500 lines) of routing/system noise, injecting the
    technical root cause on the exact same line as the AFFECTED phone number, simulating
    a real-world Ctrl+F(phone) troubleshooting scenario.
    """
    total_lines = random.randint(1500, 2500)
    root_cause_index = random.randint(0, total_lines - 1)
    technical_error = random.choice(TECHNICAL_ROOT_CAUSE[symptom])

    # A seeded run uses a fixed epoch so the corpus is byte-for-byte
    # reproducible; an unseeded run stays anchored to now. Timestamps are UTC
    # because they are emitted with a 'Z' suffix - the previous version
    # stamped naive local time and labelled it as UTC.
    if _BASE_TIME is not None:
        base_time = _BASE_TIME
    else:
        base_time = datetime.now(timezone.utc).replace(tzinfo=None)
    base_time -= timedelta(seconds=total_lines)

    lines = []
    for i in range(total_lines):
        ts = base_time + timedelta(seconds=i)
        if i == root_cause_index:
            lines.append(f"{ts.isoformat()}Z {technical_error} | CallerID: {affected_phone}")
        else:
            lines.append(f"{ts.isoformat()}Z {random.choice(LOG_EVENT_GENERATORS)()}")

    return "\n".join(lines)

def generate_mock_batch(output_dir: str, count: int, seed: Optional[int] = None) -> None:
    """
    Orchestrate the bulk generation of test tickets and their matching logs.

    Args:
        output_dir: The directory where the files will be saved.
        count: The number of ticket/log pairs to generate.
        seed: Optional RNG seed. Passing one makes the whole corpus
            reproducible, which is what lets the test suite assert against
            generated data instead of only smoke-testing it.
    """
    global _BASE_TIME
    if seed is not None:
        random.seed(seed)
        _BASE_TIME = datetime(2026, 1, 1, 12, 0, 0)
    print(f"--- Starting Test Mass Generation ({count} records) ---")
    os.makedirs(output_dir, exist_ok=True)

    symptoms = list(FORMAL_ISSUE_TEMPLATES.keys())
    written = 0

    for _ in range(count):
        chosen_symptom = random.choice(symptoms)

        ticket_text, _, affected_phone = _build_ticket(chosen_symptom)
        log_content = _generate_log_content(chosen_symptom, affected_phone)

        base_filename = f"req{_generate_fake_request_number()}"
        ticket_filename = os.path.join(output_dir, f"{base_filename}.md")
        log_filename = os.path.join(output_dir, f"{base_filename}_log.txt")

        try:
            with open(ticket_filename, 'w', encoding='utf-8') as f:
                f.write(ticket_text)
            with open(log_filename, 'w', encoding='utf-8') as f:
                f.write(log_content)
        except IOError as e:
            print(f"[ERROR] Failed to write files for {base_filename}: {e}", file=sys.stderr)
            continue
        written += 1

    # Report what was actually written. The previous message announced the
    # requested count even when every single write had failed.
    if written != count:
        print(f"\n[WARNING] {count - written} pair(s) failed to write.", file=sys.stderr)
    print(f"\n[SUCCESS] {written} ticket/log pairs were generated in '{output_dir}'.")

if __name__ == "__main__":
    # Usage: `py tests/mock_generator.py [--count N] [--seed N] [--out DIR]`
    # Defaults to 50 pairs under 'data/raw' at the project root.
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    parser = argparse.ArgumentParser(description="Gerador de massa de teste do LDP")
    parser.add_argument('--count', type=int, default=50, help="Quantidade de pares chamado/log")
    parser.add_argument('--seed', type=int, default=None, help="Semente para geração reproduzível")
    parser.add_argument('--out', type=str, default=os.path.join(project_root, 'data', 'raw'),
                        help="Diretório de saída")
    cli_args = parser.parse_args()

    generate_mock_batch(output_dir=cli_args.out, count=cli_args.count, seed=cli_args.seed)
