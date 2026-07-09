from datetime import timedelta

from django.contrib.auth.decorators import login_required
from django.db.models import Sum
from django.shortcuts import render
from django.utils import timezone

from conciliacao.services import transacoes_pendentes_qs
from patrimonio.models import Imovel, Contrato, Manutencao
from financeiro.models import ReceitaAluguel, Despesa
from financeiro.services import serie_fluxo_caixa_12m
from documentos.models import Documento, DocumentoObrigatorio, documentos_vencendo_qs


@login_required
def dashboard(request):
    hoje = timezone.localdate()
    mes_atual = hoje.month
    ano_atual = hoje.year
    prazo_90 = hoje + timedelta(days=90)

    imoveis_total = Imovel.objects.count()
    imoveis_alugados = Imovel.objects.filter(status='alugado').count()
    imoveis_vagos = Imovel.objects.filter(status='vago').count()

    # Taxa de ocupação considera apenas unidades locáveis não vendidas
    locaveis = Imovel.objects.filter(unidade_locavel=True).exclude(status='vendido')
    total_locaveis = locaveis.count()
    alugados_locaveis = locaveis.filter(status='alugado').count()
    ocupacao_percentual = (
        round(alugados_locaveis / total_locaveis * 100, 1) if total_locaveis else None
    )

    receitas_mes = ReceitaAluguel.objects.filter(competencia_mes=mes_atual, competencia_ano=ano_atual)
    receitas_previstas = receitas_mes.aggregate(total=Sum('valor_previsto'))['total'] or 0
    receitas_recebidas = receitas_mes.filter(
        status__in=('recebido', 'parcial')
    ).aggregate(total=Sum('valor_recebido'))['total'] or 0
    receitas_atrasadas = receitas_mes.filter(
        data_vencimento__lt=hoje
    ).exclude(
        status__in=ReceitaAluguel.STATUS_QUITADOS
    ).count()

    despesas_abertas = Despesa.objects.filter(status__in=('prevista', 'atrasada')).count()

    # Contratos por prazo indeterminado (sem encerramento real) não têm uma
    # data_fim efetiva próxima — não devem aparecer como "vencendo".
    contratos_vencendo = Contrato.objects.filter(
        status='ativo',
        prazo_indeterminado=False,
        data_fim__gte=hoje,
        data_fim__lte=prazo_90,
    ).order_by('data_fim')

    reajustes_proximos = Contrato.objects.filter(
        status='ativo',
        data_proximo_reajuste__gte=hoje,
        data_proximo_reajuste__lte=prazo_90,
    ).order_by('data_proximo_reajuste')

    reajustes_pendentes = Contrato.objects.filter(
        status='ativo',
        data_proximo_reajuste__isnull=False,
        data_proximo_reajuste__lte=hoje,
    ).count()

    manutencoes_abertas = Manutencao.objects.filter(
        status__in=('solicitada', 'orcamento_recebido', 'aprovada', 'em_execucao')
    ).select_related('imovel').order_by('-data_solicitacao')[:10]

    docs_pendentes_contabilidade = Documento.objects.filter(
        tipo__in=Documento.TIPOS_CONTABILIDADE,
        enviado_contabilidade=False,
    ).count()

    docs_obrigatorios_pendentes = DocumentoObrigatorio.objects.filter(
        obrigatorio=True, documento__isnull=True
    ).count()

    docs_vencendo_qs = documentos_vencendo_qs().select_related('imovel')
    docs_vencendo_cnt = docs_vencendo_qs.count()
    docs_vencendo = docs_vencendo_qs[:10]

    fluxo_caixa = serie_fluxo_caixa_12m(hoje)

    extrato_pendentes = transacoes_pendentes_qs().count()

    context = {
        'extrato_pendentes': extrato_pendentes,
        'imoveis_total': imoveis_total,
        'imoveis_alugados': imoveis_alugados,
        'imoveis_vagos': imoveis_vagos,
        'ocupacao_percentual': ocupacao_percentual,
        'total_locaveis': total_locaveis,
        'alugados_locaveis': alugados_locaveis,
        'receitas_previstas': receitas_previstas,
        'receitas_recebidas': receitas_recebidas,
        'receitas_atrasadas': receitas_atrasadas,
        'despesas_abertas': despesas_abertas,
        'contratos_vencendo': contratos_vencendo,
        'reajustes_proximos': reajustes_proximos,
        'reajustes_pendentes': reajustes_pendentes,
        'manutencoes_abertas': manutencoes_abertas,
        'docs_pendentes_contabilidade': docs_pendentes_contabilidade,
        'docs_obrigatorios_pendentes': docs_obrigatorios_pendentes,
        'docs_vencendo': docs_vencendo,
        'docs_vencendo_cnt': docs_vencendo_cnt,
        'fluxo_caixa': fluxo_caixa,
        'mes_atual': mes_atual,
        'ano_atual': ano_atual,
    }
    return render(request, 'core/dashboard.html', context)
