from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.utils import timezone

from .models import ReceitaAluguel, Despesa, FechamentoMensal
from .exports import (
    exportar_imoveis_csv, exportar_imoveis_xlsx,
    exportar_contratos_csv, exportar_contratos_xlsx,
    exportar_receitas_csv, exportar_receitas_xlsx,
    exportar_despesas_csv, exportar_despesas_xlsx,
    exportar_inadimplencia_csv, exportar_inadimplencia_xlsx,
    exportar_relatorio_mensal_xlsx,
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
    contratos = Contrato.objects.select_related('imovel', 'locatario').filter(status='ativo')
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
    imovel_id = request.GET.get('imovel', '')
    receitas = ReceitaAluguel.objects.select_related('imovel', 'contrato__locatario').filter(status='atrasado')
    if imovel_id:
        receitas = receitas.filter(imovel_id=imovel_id)
    if formato == 'csv':
        return exportar_inadimplencia_csv(request, receitas)
    return exportar_inadimplencia_xlsx(request, receitas)


@login_required
def export_relatorio_mensal(request, formato):
    mes, ano, _, _, _ = _filtros_periodo(request)
    return exportar_relatorio_mensal_xlsx(request, mes, ano)
