from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone

from .models import ReceitaAluguel, Despesa, FechamentoMensal
from .exports import (
    exportar_imoveis_csv, exportar_imoveis_xlsx,
    exportar_contratos_csv, exportar_contratos_xlsx,
    exportar_receitas_csv, exportar_receitas_xlsx,
    exportar_despesas_csv, exportar_despesas_xlsx,
    exportar_inadimplencia_csv, exportar_inadimplencia_xlsx,
    exportar_relatorio_mensal_xlsx,
    exportar_relatorio_contabilidade_xlsx,
)
from patrimonio.models import Imovel, Contrato


def _filtros_periodo(request):
    hoje = timezone.now().date()
    mes = int(request.GET.get('mes', hoje.month))
    ano = int(request.GET.get('ano', hoje.year))
    imovel_id = request.GET.get('imovel', '')
    status = request.GET.get('status', '')
    categoria = request.GET.get('categoria', '')
    return mes, ano, imovel_id, status, categoria


@login_required
def receitas_list(request):
    mes, ano, imovel_id, status, _ = _filtros_periodo(request)

    receitas = ReceitaAluguel.objects.select_related('imovel', 'contrato__locatario').filter(
        competencia_mes=mes, competencia_ano=ano
    )
    if imovel_id:
        receitas = receitas.filter(imovel_id=imovel_id)
    if status:
        receitas = receitas.filter(status=status)

    imoveis = Imovel.objects.all()
    anos = range(2020, timezone.now().year + 2)
    meses = ReceitaAluguel.MESES

    context = {
        'receitas': receitas,
        'imoveis': imoveis,
        'mes_atual': mes,
        'ano_atual': ano,
        'status_atual': status,
        'imovel_atual': imovel_id,
        'status_choices': ReceitaAluguel.STATUS_CHOICES,
        'meses': meses,
        'anos': anos,
    }
    return render(request, 'financeiro/receitas_list.html', context)


@login_required
def despesas_list(request):
    mes, ano, imovel_id, status, categoria = _filtros_periodo(request)

    despesas = Despesa.objects.select_related('imovel', 'fornecedor').all()
    if mes and ano:
        despesas = despesas.filter(competencia_mes=mes, competencia_ano=ano)
    if imovel_id:
        despesas = despesas.filter(imovel_id=imovel_id)
    if status:
        despesas = despesas.filter(status=status)
    if categoria:
        despesas = despesas.filter(categoria=categoria)

    imoveis = Imovel.objects.all()
    anos = range(2020, timezone.now().year + 2)
    meses = Despesa.MESES

    context = {
        'despesas': despesas,
        'imoveis': imoveis,
        'mes_atual': mes,
        'ano_atual': ano,
        'status_atual': status,
        'imovel_atual': imovel_id,
        'categoria_atual': categoria,
        'status_choices': Despesa.STATUS_CHOICES,
        'categoria_choices': Despesa.CATEGORIA_CHOICES,
        'meses': meses,
        'anos': anos,
    }
    return render(request, 'financeiro/despesas_list.html', context)


@login_required
def relatorios(request):
    hoje = timezone.now().date()
    context = {
        'imoveis': Imovel.objects.all(),
        'mes_atual': hoje.month,
        'ano_atual': hoje.year,
        'meses': ReceitaAluguel.MESES,
        'anos': range(2020, hoje.year + 2),
        'status_receita': ReceitaAluguel.STATUS_CHOICES,
        'status_despesa': Despesa.STATUS_CHOICES,
        'categoria_despesa': Despesa.CATEGORIA_CHOICES,
    }
    return render(request, 'financeiro/relatorios.html', context)


# ─── exportações ───────────────────────────────────────────────────────────────

@login_required
def export_imoveis(request, formato):
    imoveis = Imovel.objects.filter(status=request.GET.get('status')) if request.GET.get('status') else Imovel.objects.all()
    if formato == 'csv':
        return exportar_imoveis_csv(request, imoveis)
    return exportar_imoveis_xlsx(request, imoveis)


@login_required
def export_contratos(request, formato):
    status = request.GET.get('status', '')
    imovel_id = request.GET.get('imovel', '')
    contratos = Contrato.objects.select_related('imovel', 'locatario')
    if status:
        contratos = contratos.filter(status=status)
    else:
        contratos = contratos.filter(status='ativo')
    if imovel_id:
        contratos = contratos.filter(imovel_id=imovel_id)
    if formato == 'csv':
        return exportar_contratos_csv(request, contratos)
    return exportar_contratos_xlsx(request, contratos)


@login_required
def export_receitas(request, formato):
    mes, ano, imovel_id, status, _ = _filtros_periodo(request)
    receitas = ReceitaAluguel.objects.select_related('imovel', 'contrato__locatario').filter(
        competencia_mes=mes, competencia_ano=ano
    )
    if imovel_id:
        receitas = receitas.filter(imovel_id=imovel_id)
    if status:
        receitas = receitas.filter(status=status)
    if formato == 'csv':
        return exportar_receitas_csv(request, receitas)
    return exportar_receitas_xlsx(request, receitas)


@login_required
def export_despesas(request, formato):
    mes, ano, imovel_id, status, categoria = _filtros_periodo(request)
    despesas = Despesa.objects.select_related('imovel', 'fornecedor').filter(
        competencia_mes=mes, competencia_ano=ano
    )
    if imovel_id:
        despesas = despesas.filter(imovel_id=imovel_id)
    if status:
        despesas = despesas.filter(status=status)
    if categoria:
        despesas = despesas.filter(categoria=categoria)
    if formato == 'csv':
        return exportar_despesas_csv(request, despesas)
    return exportar_despesas_xlsx(request, despesas)


@login_required
def export_inadimplencia(request, formato):
    from .models import receitas_inadimplentes_qs
    imovel_id = request.GET.get('imovel', '')
    receitas = receitas_inadimplentes_qs().select_related('imovel', 'contrato__locatario')
    if imovel_id:
        receitas = receitas.filter(imovel_id=imovel_id)
    if formato == 'csv':
        return exportar_inadimplencia_csv(request, receitas)
    return exportar_inadimplencia_xlsx(request, receitas)


@login_required
def export_relatorio_mensal(request, formato):
    mes, ano, _, _, _ = _filtros_periodo(request)
    return exportar_relatorio_mensal_xlsx(request, mes, ano)


@login_required
def export_relatorio_contabilidade(request):
    hoje = timezone.now().date()
    mes = int(request.GET.get('mes', hoje.month))
    ano = int(request.GET.get('ano', hoje.year))
    return exportar_relatorio_contabilidade_xlsx(request, mes, ano)


@login_required
def baixa_receitas_mes_view(request):
    """
    GET: lista as receitas do mês para conferência.
    POST: atualiza individualmente (marcar_recebida ou editar).
    """
    hoje = timezone.now().date()
    try:
        mes = int(request.GET.get('mes') or request.POST.get('mes') or hoje.month)
        ano = int(request.GET.get('ano') or request.POST.get('ano') or hoje.year)
    except (ValueError, TypeError):
        mes, ano = hoje.month, hoje.year

    if request.method == 'POST':
        action = request.POST.get('action', '')
        receita_id = request.POST.get('receita_id')

        if action == 'gerar_receitas':
            from .services import gerar_receitas_mes
            criadas, existiam = gerar_receitas_mes(mes, ano)
            if criadas:
                messages.success(request, f'{criadas} receita(s) gerada(s) para {mes:02d}/{ano}.')
            if existiam:
                messages.info(request, f'{existiam} receita(s) já existiam.')
            if not criadas and not existiam:
                messages.warning(request, f'Nenhum contrato ativo para {mes:02d}/{ano}.')

        elif receita_id:
            receita = get_object_or_404(ReceitaAluguel, pk=receita_id)

            if action == 'marcar_recebida':
                if not receita.valor_recebido:
                    receita.valor_recebido = receita.valor_previsto
                if not receita.data_recebimento:
                    receita.data_recebimento = hoje
                receita.status = 'recebido'
                receita.save()
                messages.success(request, f'{receita.imovel.nome} marcado como recebido.')

            elif action == 'editar':
                valor = request.POST.get('valor_recebido', '').strip()
                data = request.POST.get('data_recebimento', '').strip()
                receita.valor_recebido = valor if valor else None
                receita.data_recebimento = data if data else None
                receita.status = request.POST.get('status', receita.status)
                receita.observacoes = request.POST.get('observacoes', '')
                receita.save()
                messages.success(request, f'{receita.imovel.nome} atualizado.')

        return HttpResponseRedirect(f"{reverse('baixa_receitas_mes')}?mes={mes}&ano={ano}")

    receitas = (
        ReceitaAluguel.objects
        .filter(competencia_mes=mes, competencia_ano=ano)
        .select_related('imovel', 'contrato__locatario')
        .order_by('imovel__nome')
    )

    context = {
        'receitas': receitas,
        'mes_atual': mes,
        'ano_atual': ano,
        'meses': ReceitaAluguel.MESES,
        'anos': range(2020, hoje.year + 2),
        'hoje': hoje,
        'status_choices': ReceitaAluguel.STATUS_CHOICES,
    }
    return render(request, 'financeiro/baixa_receitas.html', context)


@login_required
def gerar_receitas_mes_view(request):
    """
    GET: exibe formulário de confirmação com seleção de mês/ano.
    POST: executa a geração e redireciona para a lista de receitas do período.
    """
    from .services import gerar_receitas_mes
    from calendar import monthrange

    hoje = timezone.now().date()

    try:
        mes = int(request.POST.get('mes') or request.GET.get('mes') or hoje.month)
        ano = int(request.POST.get('ano') or request.GET.get('ano') or hoje.year)
    except (ValueError, TypeError):
        mes, ano = hoje.month, hoje.year

    if request.method == 'POST':
        criadas, existiam = gerar_receitas_mes(mes, ano)

        if criadas:
            messages.success(request, f'{criadas} receita(s) gerada(s) para {mes:02d}/{ano}.')
        if existiam:
            messages.warning(request, f'{existiam} receita(s) já existiam e foram ignoradas.')
        if not criadas and not existiam:
            messages.info(request, f'Nenhum contrato ativo encontrado para {mes:02d}/{ano}.')

        return HttpResponseRedirect(f"{reverse('receitas_list')}?mes={mes}&ano={ano}")

    # GET — monta contexto para o formulário de confirmação
    data_inicio_mes = hoje.replace(year=ano, month=mes, day=1)
    data_fim_mes = hoje.replace(year=ano, month=mes, day=monthrange(ano, mes)[1])
    contratos_ativos = Contrato.objects.filter(
        status='ativo',
        data_inicio__lte=data_fim_mes,
        data_fim__gte=data_inicio_mes,
    ).count()

    context = {
        'mes_atual': mes,
        'ano_atual': ano,
        'contratos_ativos': contratos_ativos,
        'meses': ReceitaAluguel.MESES,
        'anos': range(2020, hoje.year + 2),
    }
    return render(request, 'financeiro/gerar_receitas.html', context)
