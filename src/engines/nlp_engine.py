import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
from typing import Tuple

# ---------------------------------------------------------
# 1. CORPUS DE ÂNCORAS (O "Cérebro" do Modelo)
# Aqui você coloca as palavras e variações comuns para cada sintoma.
# O TF-IDF não liga para a ordem das palavras, só para a relevância delas.
# ---------------------------------------------------------
ANCHOR_DATA = {
    'FALHA_INTERFACE_UX': (
        "botao travou erro mutar nao foi possivel realizar tabulacao tela branca "
        "sistema congelou relatorio nao carrega pagina"
    ),
    'FALHA_CHAMADA_AUDIO': (
        "ligacao desligou no meio voz muda picotando chamada robotica audio "
        "cortando delay atraso telefone cliente sem som"
    ),
    'FALHA_FILA_ROTEAMENTO': (
        "agentes disponiveis e clientes na fila cliente nao entra travada "
        "roteamento pausa indevida nao loga skill errada"
    )
}

# Desmembrando o dicionário para manter a ordem dos índices
CATEGORIES = list(ANCHOR_DATA.keys())
ANCHOR_TEXTS = list(ANCHOR_DATA.values())

# ---------------------------------------------------------
# 2. INICIALIZAÇÃO GLOBAL (Alta Performance)
# Treinamos o vetorizador nas nossas âncoras na inicialização do módulo.
# Assim, não recriamos a matriz a cada chamado avaliado.
# ---------------------------------------------------------
vectorizer = TfidfVectorizer(
    lowercase=True, 
    stop_words=['o', 'a', 'e', 'do', 'da', 'no', 'na', 'que', 'com', 'um', 'uma'] # Filtra ruídos básicos
)
anchor_matrix = vectorizer.fit_transform(ANCHOR_TEXTS)

def infer_symptom_nlp(clean_text: str) -> Tuple[str, float]:
    """
    Avalia o texto do chamado utilizando Vetorização TF-IDF e Similaridade de Cosseno.
    Retorna a categoria do sintoma inferido e o score de confiança matemático.
    """
    if not clean_text or len(clean_text.strip()) < 5:
        return "TEXTO_MUITO_CURTO", 0.0

    # 1. Transforma o chamado real em um vetor matemático
    query_vector = vectorizer.transform([clean_text])
    
    # 2. Calcula a similaridade (ângulo) contra todas as nossas categorias âncora
    # Isso retorna um array de scores, ex: [0.12, 0.85, 0.03]
    similarities = cosine_similarity(query_vector, anchor_matrix)[0]
    
    # 3. Descobre qual categoria teve o maior match
    best_match_idx = int(np.argmax(similarities))
    best_score = float(similarities[best_match_idx])
    
    # 4. Limiar de Confiança (Threshold)
    # Se o chamado não tiver NADA a ver com os nossos problemas, o score será muito baixo.
    if best_score < 0.15:
        return "SINTOMA_DESCONHECIDO", round(best_score, 4)
        
    return CATEGORIES[best_match_idx], round(best_score, 4)