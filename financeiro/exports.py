import csv
import io
from datetime import date

from django.http import HttpResponse

try:
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter
    OPENPYXL_AVAILABLE = True
except ImportError:
    OPENPYXL_AVAILABLE = False

from .models import ReceitaAluguel, Despesa
from patrimonio.models import Imovel, Contrato


# ─── helpers ───────────────────────────────────────────────────────────────────

def _estilizar_cabecalho(ws, num_colunas):
    """Aplica estilo azul escuro nas células do cabeçalho (linha 1)."""
    fill = PatternFill(start_color='1F3864', end_color='1F3864', fill_type='solid')
    font = Font(color='FFFFFF', bold=True)
    for col in range(1, num_colunas + 1):
        cell = ws.cell(row=1, column=col)
        cell.fill = fill
        cell.font = font
        cell.alignment = Alignment(horizontal='center')
    for col in range(1, num_colunas + 1):
        ws.column_dimensions[get_column_letter(col)].width = 20


def _response_xlsx(nome_arquivo):
    response = HttpResponse(
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
    response['Content-Disposition'] = f'attachment; filename="{nome_arquivo}"'
    return response


def _response_csv(nome_arquivo):
    response = HttpResponse(content_type='text/csv; charset=utf-8-sig')
    response['Content-Disposition'] = f'attachment; filename="{nome_arquivo}"'
    return response


# ─── imóveis ───────────────────────────────────────────────────────────────────

CABECALHO_IMOVEIS = [
    'ID', 'Nome', 'Endereço', 'Cidade', 'Estado', 'CEP',
    'Matrícula', 'Inscrição IPTU', 'Proprietário', 'Status',
    'Data Aquisição', 'Valor Aquisição', 'Valor Estimado',
]


def _linhas_imoveis(imoveis):
    for i in imoveis:
        yield [
            i.pk, i.nome, i.endereco, i.cidade, i.estado, i.cep,
            i.matricula, i.inscricao_iptu, i.proprietario,
            i.get_status_display(),
            i.data_aquisicao, i.valor_aquisicao, i.valor_estimado,
        ]


def exportar_imoveis_csv(request, imoveis):
    response = _response_csv('imoveis.csv')
    writer = csv.writer(response)
    writer.writerow(CABECALHO_IMOVEIS)
    for linha in _linhas_imoveis(imoveis):
        writer.writerow(linha)
    return response


def exportar_imoveis_xlsx(request, imoveis):
    if not OPENPYXL_AVAILABLE:
        return exportar_imoveis_csv(request, imoveis)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = 'Imóveis'
    ws.append(CABECALHO_IMOVEIS)
    for linha in _linhas_imoveis(imoveis):
        ws.append(linha)
    _estilizar_cabecalho(ws, len(CABECALHO_IMOVEIS))
    response = _response_xlsx('imoveis.xlsx')
    wb.save(response)
    return response


# ─── contratos ─────────────────────────────────────────────────────────────────

CABECALHO_CONTRATOS = [
    'ID', 'Imóvel', 'Locatário', 'Data Início', 'Data Fim',
    'Valor Aluguel', 'Dia Vencimento', 'Índice', 'Próximo Reajuste',
    'Garantia', 'Status',
]


def _linhas_contratos(contratos):
    for c in contratos:
        yield [
            c.pk, c.imovel.nome, c.locatario.nome,
            c.data_inicio, c.data_fim, c.valor_aluguel,
            c.dia_vencimento, c.get_indice_reajuste_display(),
            c.data_proximo_reajuste, c.get_tipo_garantia_display(),
            c.get_status_display(),
        ]


def exportar_contratos_csv(request, contratos):
    response = _response_csv('contratos.csv')
    writer = csv.writer(response)
    writer.writerow(CABECALHO_CONTRATOS)
    for linha in _linhas_contratos(contratos):
        writer.writerow(linha)
    return response


def exportar_contratos_xlsx(request, contratos):
    if not OPENPYXL_AVAILABLE:
        return exportar_contratos_csv(request, contratos)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = 'Contratos'
    ws.append(CABECALHO_CONTRATOS)
    for linha in _linhas_contratos(contratos):
        ws.append(linha)
    _estilizar_cabecalho(ws, len(CABECALHO_CONTRATOS))
    response = _response_xlsx('contratos.xlsx')
    wb.save(response)
    return response


# ─── receitas ──────────────────────────────────────────────────────────────────

CABECALHO_RECEITAS = [
    'ID', 'Imóvel', 'Locatário', 'Mês', 'Ano',
    'Vencimento', 'Valor Previsto', 'Valor Recebido',
    'Data Recebimento', 'Multa', 'Juros', 'Desconto', 'Status',
]


def _linhas_receitas(receitas):
    for r in receitas:
        locatario = r.contrato.locatario.nome if r.contrato else ''
        yield [
            r.pk, r.imovel.nome, locatario,
            r.get_competencia_mes_display(), r.competencia_ano,
            r.data_vencimento, r.valor_previsto, r.valor_recebido,
            r.data_recebimento, r.multa, r.juros, r.desconto,
            r.get_status_display(),
        ]


def exportar_receitas_csv(request, receitas):
    response = _response_csv('receitas.csv')
    writer = csv.writer(response)
    writer.writerow(CABECALHO_RECEITAS)
    for linha in _linhas_receitas(receitas):
        writer.writerow(linha)
    return response


def exportar_receitas_xlsx(request, receitas):
    if not OPENPYXL_AVAILABLE:
        return exportar_receitas_csv(request, receitas)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = 'Receitas'
    ws.append(CABECALHO_RECEITAS)
    for linha in _linhas_receitas(receitas):
        ws.append(linha)
    _estilizar_cabecalho(ws, len(CABECALHO_RECEITAS))
    response = _response_xlsx('receitas.xlsx')
    wb.save(response)
    return response


# ─── despesas ──────────────────────────────────────────────────────────────────

CABECALHO_DESPESAS = [
    'ID', 'Imóvel', 'Categoria', 'Fornecedor', 'Descrição',
    'Mês', 'Ano', 'Vencimento', 'Valor', 'Data Pagamento', 'Status',
]


def _linhas_despesas(despesas):
    for d in despesas:
        yield [
            d.pk,
            d.imovel.nome if d.imovel else '',
            d.get_categoria_display(),
            d.fornecedor.nome if d.fornecedor else '',
            d.descricao,
            d.get_competencia_mes_display() if d.competencia_mes else '',
            d.competencia_ano or '',
            d.data_vencimento, d.valor, d.data_pagamento,
            d.get_status_display(),
        ]


def exportar_despesas_csv(request, despesas):
    response = _response_csv('despesas.csv')
    writer = csv.writer(response)
    writer.writerow(CABECALHO_DESPESAS)
    for linha in _linhas_despesas(despesas):
        writer.writerow(linha)
    return response


def exportar_despesas_xlsx(request, despesas):
    if not OPENPYXL_AVAILABLE:
        return exportar_despesas_csv(request, despesas)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = 'Despesas'
    ws.append(CABECALHO_DESPESAS)
    for linha in _linhas_despesas(despesas):
        ws.append(linha)
    _estilizar_cabecalho(ws, len(CABECALHO_DESPESAS))
    response = _response_xlsx('despesas.xlsx')
    wb.save(response)
    return response


# ─── inadimplência ─────────────────────────────────────────────────────────────

CABECALHO_INADIMPLENCIA = [
    'Imóvel', 'Locatário', 'Mês', 'Ano', 'Vencimento', 'Valor Previsto', 'Dias Atraso',
]


def _linhas_inadimplencia(receitas_atrasadas):
    hoje = date.today()
    for r in receitas_atrasadas:
        dias = (hoje - r.data_vencimento).days
        locatario = r.contrato.locatario.nome if r.contrato else ''
        yield [
            r.imovel.nome, locatario,
            r.get_competencia_mes_display(), r.competencia_ano,
            r.data_vencimento, r.valor_previsto, dias,
        ]


def exportar_inadimplencia_csv(request, receitas_atrasadas):
    response = _response_csv('inadimplencia.csv')
    writer = csv.writer(response)
    writer.writerow(CABECALHO_INADIMPLENCIA)
    for linha in _linhas_inadimplencia(receitas_atrasadas):
        writer.writerow(linha)
    return response


def exportar_inadimplencia_xlsx(request, receitas_atrasadas):
    if not OPENPYXL_AVAILABLE:
        return exportar_inadimplencia_csv(request, receitas_atrasadas)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = 'Inadimplência'
    ws.append(CABECALHO_INADIMPLENCIA)
    for linha in _linhas_inadimplencia(receitas_atrasadas):
        ws.append(linha)
    _estilizar_cabecalho(ws, len(CABECALHO_INADIMPLENCIA))
    response = _response_xlsx('inadimplencia.xlsx')
    wb.save(response)
    return response


# ─── relatório mensal ──────────────────────────────────────────────────────────

def exportar_relatorio_mensal_xlsx(request, mes, ano):
    if not OPENPYXL_AVAILABLE:
        return HttpResponse('openpyxl não instalado.', status=500)

    wb = openpyxl.Workbook()

    # Aba receitas
    ws_rec = wb.active
    ws_rec.title = 'Receitas'
    ws_rec.append(CABECALHO_RECEITAS)
    receitas = ReceitaAluguel.objects.filter(
        competencia_mes=mes, competencia_ano=ano
    ).select_related('imovel', 'contrato__locatario')
    for linha in _linhas_receitas(receitas):
        ws_rec.append(linha)
    _estilizar_cabecalho(ws_rec, len(CABECALHO_RECEITAS))

    # Aba despesas
    ws_desp = wb.create_sheet('Despesas')
    ws_desp.append(CABECALHO_DESPESAS)
    despesas = Despesa.objects.filter(
        competencia_mes=mes, competencia_ano=ano
    ).select_related('imovel', 'fornecedor')
    for linha in _linhas_despesas(despesas):
        ws_desp.append(linha)
    _estilizar_cabecalho(ws_desp, len(CABECALHO_DESPESAS))

    # Aba resumo por imóvel
    ws_res = wb.create_sheet('Resumo por Imóvel')
    ws_res.append(['Imóvel', 'Receita Prevista', 'Receita Recebida', 'Despesas Pagas', 'Resultado'])
    _estilizar_cabecalho(ws_res, 5)
    for imovel in Imovel.objects.all():
        rec_prev = sum(r.valor_previsto for r in receitas if r.imovel_id == imovel.pk)
        rec_rec = sum(r.valor_recebido or 0 for r in receitas if r.imovel_id == imovel.pk and r.status in ('recebido', 'parcial'))
        desp_pagas = sum(d.valor for d in despesas if d.imovel_id == imovel.pk and d.status == 'paga')
        if rec_prev or rec_rec or desp_pagas:
            ws_res.append([imovel.nome, rec_prev, rec_rec, desp_pagas, rec_rec - desp_pagas])

    response = _response_xlsx(f'relatorio_{mes:02d}_{ano}.xlsx')
    wb.save(response)
    return response
