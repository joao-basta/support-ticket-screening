import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
from typing import Tuple, List

# ---------------------------------------------------------
# 1. ANCHOR CORPUS (The "Brain" of the Model)
# This dictionary maps core symptoms to anchor texts containing common
# keywords and variations for each problem in Brazilian Portuguese.
# TF-IDF focuses on word relevance, not order.
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

# Deconstruct the dictionary to maintain index order for mapping results.
CATEGORIES: List[str] = list(ANCHOR_DATA.keys())
ANCHOR_TEXTS: List[str] = list(ANCHOR_DATA.values())

# ---------------------------------------------------------
# 2. GLOBAL INITIALIZATION (High Performance)
# The vectorizer is trained on our anchor texts when the module is initialized.
# This pre-computation ensures the inference function runs in O(1) time
# by avoiding the re-creation of the matrix for every evaluated ticket.
# ---------------------------------------------------------
vectorizer = TfidfVectorizer(
    lowercase=True, 
    # Basic Portuguese stop words to filter out noise.
    stop_words=['o', 'a', 'e', 'do', 'da', 'no', 'na', 'que', 'com', 'um', 'uma']
)
anchor_matrix = vectorizer.fit_transform(ANCHOR_TEXTS)

def infer_symptom_nlp(clean_text: str) -> Tuple[str, float]:
    """
    Evaluates the ticket text using TF-IDF Vectorization and Cosine Similarity.
    Returns the inferred symptom category and its mathematical confidence score.
    """
    if not clean_text or len(clean_text.strip()) < 5:
        return "TEXTO_MUITO_CURTO", 0.0

    # 1. Transform the input ticket into a mathematical vector.
    query_vector = vectorizer.transform([clean_text])
    
    # 2. Calculate the similarity (angle) against all our anchor categories.
    # This returns an array of scores, e.g., [0.12, 0.85, 0.03].
    similarities = cosine_similarity(query_vector, anchor_matrix)[0]
    
    # 3. Find out which category had the highest match.
    best_match_idx = int(np.argmax(similarities))
    best_score = float(similarities[best_match_idx])
    
    # 4. Confidence Threshold.
    # If the ticket has very little in common with our known issues, the score
    # will be very low, and we should classify it as an unknown symptom.
    if best_score < 0.15:
        return "SINTOMA_DESCONHECIDO", round(best_score, 4)
        
    return CATEGORIES[best_match_idx], round(best_score, 4)