"""Text rendering of a `ScreeningResult`.

Presentation only: this module decides how the analysis looks on a terminal and
nothing else. Keeping it separate from `screening.py` is what lets the API
serve the same analysis as JSON without duplicating a single rule.

All output here is pt-BR - it is read by support analysts.
"""

from typing import List

from ldp import config
from ldp.core.result import ScreeningResult, ScreeningStatus

_STATUS_DECORATION = {
    ScreeningStatus.APROVADO: '✅ SUCESSO - PRONTO PARA ANÁLISE TÉCNICA',
    ScreeningStatus.BLOQUEADO: '❌ BLOQUEADO - FALTAM DADOS',
    ScreeningStatus.ATENCAO: '⚠️ ATENÇÃO - ANÁLISE HUMANA NECESSÁRIA',
}


def render_report(result: ScreeningResult, width: int = None) -> str:
    """Render the screening as the boxed analyst report.

    Args:
        result: the analysis to display.
        width: separator width; defaults to `config.REPORT_WIDTH`.

    Returns:
        The full report as a single string, ready to print.
    """
    rule_width = width or config.REPORT_WIDTH
    heavy = '=' * rule_width
    light = '-' * rule_width

    lines: List[str] = [heavy]
    lines.append(f"[CHAMADO]          : {result.source_id}")
    lines.append(f"[TEXTO ANALISADO]  : {result.analyzed_text}")
    lines.append(
        f"[SINTOMA INFERIDO] : {result.symptom.value} "
        f"(Confiança: {result.confidence * 100:.1f}%)"
    )

    if result.attachments.total:
        lines.append(
            f"[ANEXOS]           : {result.attachments.logs} log(s), "
            f"{result.attachments.visuals} evidência(s) visual(is), "
            f"{result.attachments.others} outro(s)"
        )

    if result.entity_counts:
        detected = ', '.join(
            f"{kind}={count}" for kind, count in sorted(result.entity_counts.items())
        )
        lines.append(f"[DADOS SENSÍVEIS]  : {detected}")

    lines.append(f"[STATUS]           : {_STATUS_DECORATION[result.status]}")

    if result.status is ScreeningStatus.BLOQUEADO:
        lines.append(
            f"[AÇÃO NECESSÁRIA]  : Devolver chamado ao solicitante cobrando: "
            f"{', '.join(result.missing)}"
        )
    else:
        lines.append(f"[AÇÃO NECESSÁRIA]  : {result.status.action}")

    if result.evidences:
        lines.append(light)
        lines.append("[EVIDÊNCIAS]")
        for check in result.evidences:
            mark = 'OK  ' if check.satisfied else 'FALTA'
            lines.append(f"  [{mark}] {check.item}")

    analysis = result.log_analysis
    if analysis is not None:
        lines.append(light)
        if not analysis.total_hits:
            lines.append("[ANÁLISE DE LOG]   : Nenhuma assinatura de erro conhecida encontrada.")
        else:
            suffix = ' (exibindo os primeiros)' if analysis.truncated else ''
            lines.append(
                f"[ANÁLISE DE LOG]   : {analysis.total_hits} ocorrência(s) "
                f"em {analysis.scanned_files} arquivo(s){suffix}:"
            )
            for hit in analysis.hits:
                lines.append(
                    f"  - {hit.file_name}:{hit.line_number} "
                    f"[{hit.label}] {hit.line_excerpt}"
                )
            if analysis.matches_nlp_symptom is False:
                lines.append(
                    f"[⚠️ DIVERGÊNCIA]    : Log aponta causa técnica de "
                    f"'{analysis.dominant_symptom.value}', mas o texto do chamado indica "
                    f"'{result.symptom.value}'. Revisar antes de prosseguir."
                )
            elif analysis.matches_nlp_symptom is True:
                lines.append("[CONFIRMAÇÃO]      : Causa técnica no log confirma o sintoma relatado.")
            else:
                lines.append(
                    f"[INDÍCIO TÉCNICO]  : O log aponta "
                    f"'{analysis.dominant_symptom.value}'. Como o texto não permitiu "
                    "inferir o sintoma, use o log como ponto de partida."
                )

    lines.append(heavy)
    return '\n'.join(lines)
