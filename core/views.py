from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.utils import timezone
from datetime import timedelta

from patrimonio.models import Imovel, Contrato, Manutencao
from financeiro.models import ReceitaAluguel, Despesa
from documentos.models import Documento, DocumentoObrigatorio


@login_required
def dashboard(request):
    hoje = timezone.now().date()
    mes_atual = hoje.month
    ano_atual = hoje.year
    prazo_90 = hoje + timedelta(days=90)

    imoveis_total = Imovel.objects.count()
    imoveis_alugados = Imovel.objects.filter(status='alugado').count()
    imoveis_vagos = Imovel.objects.filter(status='vago').count()

    receitas_mes = ReceitaAluguel.objects.filter(competencia_mes=mes_atual, competencia_ano=ano_atual)
    receitas_previstas = sum(r.valor_previsto for r in receitas_mes)
    receitas_recebidas = sum(r.valor_recebido or 0 for r in receitas_mes.filter(status__in=('recebido', 'parcial')))
    receitas_atrasadas = receitas_mes.filter(
        data_vencimento__lt=hoje
    ).exclude(
        status__in=ReceitaAluguel.STATUS_QUITADOS
    ).count()

    despesas_abertas = Despesa.objects.filter(status__in=('prevista', 'atrasada')).count()

    contratos_vencendo = Contrato.objects.filter(
        status='ativo',
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

    context = {
        'imoveis_total': imoveis_total,
        'imoveis_alugados': imoveis_alugados,
        'imoveis_vagos': imoveis_vagos,
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
        'mes_atual': mes_atual,
        'ano_atual': ano_atual,
    }
    return render(request, 'core/dashboard.html', context)
