import os
import random
import uuid
from datetime import datetime, timedelta
from typing import Dict, List, Tuple

# ---------------------------------------------------------
# 1. GERADORES DE DADOS PESSOAIS (PII Falsos)
# Funções auxiliares para criar dados sensíveis sintéticos.
# ---------------------------------------------------------

def _generate_fake_cpf() -> str:
    """Gera um número de CPF falso no formato XXX.XXX.XXX-XX."""
    return f"{random.randint(100, 999)}.{random.randint(100, 999)}.{random.randint(100, 999)}-{random.randint(10, 99)}"

def _generate_fake_phone() -> str:
    """Gera um número de telefone celular falso no formato (XX) 9XXXX-XXXX."""
    return f"({random.randint(11, 99)}) 9{random.randint(1000, 9999)}-{random.randint(1000, 9999)}"

# ---------------------------------------------------------
# 2. SIMULADOR DE SINTOMAS (Linguagem Natural)
# Dicionários que armazenam fragmentos de frases para montar os chamados.
# ---------------------------------------------------------

TICKET_FRAGMENTS: Dict[str, Dict[str, List[str]]] = {
    'FALHA_INTERFACE_UX': {
        "problema": ["O botão de salvar não funciona", "A tela ficou toda branca", "O sistema travou e congelou tudo", "Não consigo gerar o relatório de vendas"],
        "acao": ["Tentei recarregar a página várias vezes", "Precisei fechar e abrir o programa de novo", "Cliquei em todo lugar mas nada acontece"],
        "evidencia": ["Estou enviando um print da tela em anexo", "O log do console mostra um erro estranho", "Segue a imagem do que aconteceu"]
    },
    'FALHA_CHAMADA_AUDIO': {
        "problema": ["A ligação com o cliente caiu no meio", "O áudio estava picotando muito, com voz de robô", "O cliente disse que minha voz estava muda"],
        "acao": ["Pedi para o cliente retornar a ligação", "Tive que desligar e tentar de novo", "Reiniciei meu headset mas não adiantou"],
        "evidencia": ["Aconteceu por volta das 14h30", f"O número do cliente é {_generate_fake_phone()}", f"Ocorreu às 10h15 com o cliente {_generate_fake_phone()}"]
    },
    'FALHA_FILA_ROTEAMENTO': {
        "problema": ["Estou disponível mas não recebo chamadas", "Tem 5 clientes na fila e ninguém é atendido", "Fui colocado em pausa sem eu pedir"],
        "acao": ["Tentei deslogar e logar novamente no sistema", "Meu supervisor reiniciou meu estado", "Verifiquei e estou no skill correto"],
        "evidencia": ["Isso começou a acontecer depois das 15h00", "O problema foi registrado às 09h45 da manhã", "Notei o erro por volta das 11h20"]
    }
}

# ---------------------------------------------------------
# 3. INJETOR DE CAUSA RAIZ (Logs Técnicos)
# Mapeia o sintoma a um erro técnico que será inserido no arquivo de log.
# ---------------------------------------------------------

TECHNICAL_ROOT_CAUSE: Dict[str, List[str]] = {
    'FALHA_INTERFACE_UX': [
        "ERROR: Uncaught TypeError: Cannot read properties of null (reading 'value')",
        "FATAL: JavaScript heap out of memory",
        "SERVER_RESPONSE: 500 Internal Server Error on /api/reports"
    ],
    'FALHA_CHAMADA_AUDIO': [
        "NETWORK_METRIC: High Jitter detected: 350ms",
        "VOIP_STATUS: UDP Packet Loss exceeded 15%",
        "SIP/408: Request Timeout - No response from client endpoint"
    ],
    'FALHA_FILA_ROTEAMENTO': [
        "ACD_QUEUE: ERROR - QueueID 7 overflow. Max capacity reached.",
        "AGENT_STATE: Mismatch - Agent 3012 state is 'Available' but channel is not open.",
        "ROUTING_ENGINE: No available agent found for skill 'Vendas_Premium'"
    ]
}

def _generate_log_content(symptom: str) -> str:
    """
    Gera o conteúdo de um arquivo de log falso, incluindo uma causa raiz técnica.
    """
    now = datetime.now()
    technical_error = random.choice(TECHNICAL_ROOT_CAUSE[symptom])
    
    log_lines = [
        f"{(now - timedelta(seconds=30)).isoformat()}Z INFO: System check initiated.",
        f"{(now - timedelta(seconds=25)).isoformat()}Z INFO: Database connection OK.",
        f"{(now - timedelta(seconds=20)).isoformat()}Z INFO: Auth module OK.",
        f"{(now - timedelta(seconds=15)).isoformat()}Z DEBUG: User session validated.",
        f"{(now - timedelta(seconds=10)).isoformat()}Z {technical_error}",
        f"{(now - timedelta(seconds=5)).isoformat()}Z WARN: High resource usage detected.",
        f"{now.isoformat()}Z INFO: Log session terminated."
    ]
    return "\n".join(log_lines)

def _generate_ticket_and_log(symptom: str) -> Tuple[str, str]:
    """
    Cria o texto de um chamado e o conteúdo do log associado.
    """
    fragments = TICKET_FRAGMENTS[symptom]
    
    # Monta o texto do chamado
    ticket_text_parts = [
        random.choice(fragments["problema"]),
        random.choice(fragments["acao"]),
        random.choice(fragments["evidencia"]),
        f"Meu CPF para registro é {_generate_fake_cpf()}."
    ]
    random.shuffle(ticket_text_parts)
    ticket_text = ". ".join(ticket_text_parts)
    
    # Gera o conteúdo do log
    log_content = _generate_log_content(symptom)
    
    return ticket_text, log_content

def generate_mock_batch(output_dir: str, count: int) -> None:
    """
    Orquestra a geração em massa de chamados e logs de teste.

    Args:
        output_dir: O diretório onde os arquivos serão salvos.
        count: O número de pares (chamado + log) a serem gerados.
    """
    print(f"--- Iniciando Geração de Massa de Teste ({count} registros) ---")
    os.makedirs(output_dir, exist_ok=True)
    
    symptoms = list(TICKET_FRAGMENTS.keys())
    
    for i in range(count):
        # Escolhe um sintoma aleatório para o chamado
        chosen_symptom = random.choice(symptoms)
        
        # Gera o conteúdo
        ticket_content, log_content = _generate_ticket_and_log(chosen_symptom)
        
        # Define os nomes dos arquivos
        base_filename = f"{chosen_symptom.lower()}_{uuid.uuid4().hex[:8]}"
        ticket_filename = os.path.join(output_dir, f"{base_filename}_ticket.txt")
        log_filename = os.path.join(output_dir, f"{base_filename}_log.txt")
        
        # Salva os arquivos
        try:
            with open(ticket_filename, 'w', encoding='utf-8') as f:
                f.write(ticket_content)
            with open(log_filename, 'w', encoding='utf-8') as f:
                f.write(log_content)
        except IOError as e:
            print(f"[ERRO] Falha ao escrever arquivos para {base_filename}: {e}")
            continue

    print(f"\n[SUCESSO] {count} pares de chamado/log foram gerados em '{output_dir}'.")

if __name__ == "__main__":
    # Exemplo de uso: `python tests/mock_generator.py`
    # Isso irá gerar 50 arquivos de teste no diretório 'data/raw'
    
    # Garante que o caminho seja relativo à raiz do projeto
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    output_path = os.path.join(project_root, 'data', 'raw')
    
    generate_mock_batch(output_dir=output_path, count=50)