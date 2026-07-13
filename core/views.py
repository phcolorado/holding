from datetime import timedelta

from django.contrib.auth.decorators import login_required
from django.db.models import Sum
from django.shortcuts import render
from django.utils import timezone

from conciliacao.services import transacoes_pendentes_qs
from patrimonio.models import Imovel, Contrato, Manutencao
from financeiro.models import ReceitaAluguel, RecebimentoReceita, Despesa
from financeiro.services import serie_fluxo_caixa_12m
from documentos.models import Documento, DocumentoObrigatorio, documentos_vencendo_qs


@login_required
def dashboard(request):
    """
    Cada bloco só é consultado quando o usuário tem a permissão de leitura
    da área correspondente — mesma lógica defensiva de patrimonio.imovel_
    detail: a view não busca dados que o template esconderia.
    """
    hoje = timezone.localdate()
    mes_atual = hoje.month
    ano_atual = hoje.year
    prazo_90 = hoje + timedelta(days=90)
    pode = request.user.has_perm

    ve_imoveis = pode('patrimonio.view_imovel')
    ve_receitas = pode('financeiro.view_receitaaluguel')
    ve_despesas = pode('financeiro.view_despesa')
    ve_contratos = pode('patrimonio.view_contrato')
    ve_manutencoes = pode('patrimonio.view_manutencao')
    ve_documentos = pode('documentos.view_documento')
    ve_documentos_obrigatorios = pode('documentos.view_documentoobrigatorio')
    ve_conciliacao = pode('conciliacao.view_extratoimportado')

    if ve_imoveis:
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
    else:
        imoveis_total = imoveis_alugados = imoveis_vagos = 0
        total_locaveis = alugados_locaveis = 0
        ocupacao_percentual = None

    if ve_receitas:
        receitas_mes = ReceitaAluguel.objects.filter(competencia_mes=mes_atual, competencia_ano=ano_atual)
        # "Receita Prevista" do mês: soma SÓ valor_previsto (não é o valor
        # exigível, que também somaria multa+juros−desconto — ver
        # valor_total_devido_expr()) — coerente para planejamento, não
        # renomear nem documentar como "Exigível" (item 8, rodada de
        # fechamento estrutural). Exclui canceladas — a cobrança delas foi
        # encerrada, não é mais "receita prevista" a cobrar.
        receitas_previstas = receitas_mes.exclude(status='cancelado').aggregate(
            total=Sum('valor_previsto')
        )['total'] or 0
        # Agrega RecebimentoReceita diretamente (não valor_recebido filtrado
        # por status) — dinheiro recebido continua contando mesmo que a
        # receita tenha sido cancelada depois de receber algo.
        receitas_recebidas = RecebimentoReceita.objects.filter(
            receita__competencia_mes=mes_atual, receita__competencia_ano=ano_atual,
        ).aggregate(total=Sum('valor'))['total'] or 0
        # Regra centralizada: vencidas com saldo em aberto (independe do status)
        receitas_atrasadas = receitas_mes.inadimplentes().count()
        # Fluxo de caixa mistura despesas pagas — só inclui essa série quando
        # o usuário também pode ver despesas (item 9: quando não inclui,
        # 'pago'/'saldo' vêm None na série — o template esconde as colunas
        # e o dataset correspondentes, nunca mostra "0" como se não houvesse
        # despesa nenhuma no mês).
        fluxo_caixa = serie_fluxo_caixa_12m(hoje, incluir_despesas=ve_despesas)
    else:
        receitas_previstas = receitas_recebidas = 0
        receitas_atrasadas = 0
        fluxo_caixa = []

    if ve_despesas:
        despesas_abertas = Despesa.objects.filter(status__in=('prevista', 'atrasada')).count()
    else:
        despesas_abertas = 0

    if ve_contratos:
        # Contratos por prazo indeterminado (sem encerramento real) não têm
        # uma data_fim efetiva próxima — não devem aparecer como "vencendo".
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
    else:
        contratos_vencendo = Contrato.objects.none()
        reajustes_proximos = Contrato.objects.none()
        reajustes_pendentes = 0

    if ve_manutencoes:
        manutencoes_abertas = Manutencao.objects.filter(
            status__in=('solicitada', 'orcamento_recebido', 'aprovada', 'em_execucao')
        ).select_related('imovel').order_by('-data_solicitacao')[:10]
    else:
        manutencoes_abertas = Manutencao.objects.none()

    if ve_documentos:
        docs_pendentes_contabilidade = Documento.objects.filter(
            tipo__in=Documento.TIPOS_CONTABILIDADE,
            enviado_contabilidade=False,
        ).count()
        docs_vencendo_qs = documentos_vencendo_qs().select_related('imovel')
        docs_vencendo_cnt = docs_vencendo_qs.count()
        docs_vencendo = docs_vencendo_qs[:10]
    else:
        docs_pendentes_contabilidade = 0
        docs_vencendo_cnt = 0
        docs_vencendo = Documento.objects.none()

    if ve_documentos_obrigatorios:
        # Item 6.3: DocumentoObrigatorio nunca é consultado com apenas
        # documentos.view_documento — exige a permissão própria do model.
        docs_obrigatorios_pendentes = DocumentoObrigatorio.objects.filter(
            obrigatorio=True, documento__isnull=True
        ).count()
    else:
        docs_obrigatorios_pendentes = 0

    if ve_conciliacao:
        extrato_pendentes = transacoes_pendentes_qs().count()
    else:
        extrato_pendentes = 0

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
        've_documentos_obrigatorios': ve_documentos_obrigatorios,
        'docs_vencendo': docs_vencendo,
        'docs_vencendo_cnt': docs_vencendo_cnt,
        'fluxo_caixa': fluxo_caixa,
        'fluxo_inclui_despesas': ve_despesas,
        'mes_atual': mes_atual,
        'ano_atual': ano_atual,
    }
    return render(request, 'core/dashboard.html', context)
