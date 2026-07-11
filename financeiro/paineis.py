"""
Séries de dados para a página de Painéis (/financeiro/paineis/).

Cada função retorna estruturas prontas para serialização em JSON
(floats/ints/strings), consumidas pelos gráficos Chart.js e pelas
tabelas acessíveis do template.
"""
from calendar import monthrange
from collections import defaultdict
from datetime import date
from decimal import Decimal

from django.db.models import Count, Sum

from patrimonio.indicadores import competencias_ultimas, contrato_ocupa_mes, filtro_competencias
from patrimonio.models import Contrato, Imovel
from .models import ReceitaAluguel, Despesa, saldo_em_aberto_expr


def serie_ocupacao_mensal(n_meses=12, referencia=None):
    """
    Percentual de unidades locáveis com contrato vigente em cada competência.
    Considera contratos de qualquer status exceto suspenso, respeitando
    encerramento real e prazo indeterminado (data_fim_efetiva).
    """
    competencias = competencias_ultimas(n_meses, referencia)

    imoveis_ids = list(
        Imovel.objects.filter(unidade_locavel=True).exclude(status='vendido')
        .values_list('pk', flat=True)
    )
    total = len(imoveis_ids)

    contratos_por_imovel = defaultdict(list)
    for contrato in Contrato.objects.filter(imovel_id__in=imoveis_ids):
        contratos_por_imovel[contrato.imovel_id].append(contrato)

    serie = []
    for ano, mes in competencias:
        primeiro = date(ano, mes, 1)
        ultimo = date(ano, mes, monthrange(ano, mes)[1])
        ocupados = sum(
            1 for pk in imoveis_ids
            if any(contrato_ocupa_mes(c, primeiro, ultimo) for c in contratos_por_imovel.get(pk, ()))
        )
        percentual = round(ocupados / total * 100, 1) if total else 0.0
        serie.append({
            'label': f'{mes:02d}/{ano}',
            'ocupados': ocupados,
            'total': total,
            'percentual': percentual,
            'vacancia': round(100 - percentual, 1) if total else 0.0,
        })
    return serie


def serie_receita_despesa_por_imovel(n_meses=12, referencia=None):
    """Receitas recebidas × despesas pagas por imóvel no período (ordenado por receita)."""
    competencias = competencias_ultimas(n_meses, referencia)
    filtro = filtro_competencias(competencias)

    dados = defaultdict(lambda: {'recebido': Decimal('0'), 'pago': Decimal('0')})

    recebidos = (
        ReceitaAluguel.objects.filter(filtro, status__in=('recebido', 'parcial'))
        .values('imovel__nome').annotate(total=Sum('valor_recebido'))
    )
    for linha in recebidos:
        dados[linha['imovel__nome']]['recebido'] = linha['total'] or Decimal('0')

    pagos = (
        Despesa.objects.filter(filtro, status='paga', imovel__isnull=False)
        .values('imovel__nome').annotate(total=Sum('valor'))
    )
    for linha in pagos:
        dados[linha['imovel__nome']]['pago'] = linha['total'] or Decimal('0')

    serie = [
        {
            'imovel': nome,
            'recebido': float(valores['recebido']),
            'pago': float(valores['pago']),
            'resultado': float(valores['recebido'] - valores['pago']),
        }
        for nome, valores in dados.items()
    ]
    serie.sort(key=lambda item: item['recebido'], reverse=True)
    return serie


def serie_inadimplencia_mensal(n_meses=12, referencia=None):
    """Valor e quantidade de receitas vencidas e não quitadas, por competência."""
    competencias = competencias_ultimas(n_meses, referencia)
    filtro = filtro_competencias(competencias)

    # Regra centralizada: ReceitaAluguel.objects.inadimplentes() (vencidas com
    # saldo em aberto — independe do campo status).
    em_aberto = {
        (linha['competencia_ano'], linha['competencia_mes']): linha
        for linha in (
            ReceitaAluguel.objects.inadimplentes().filter(filtro)
            .values('competencia_ano', 'competencia_mes')
            .annotate(total=Sum(saldo_em_aberto_expr()), quantidade=Count('id'))
        )
    }

    serie = []
    for ano, mes in competencias:
        linha = em_aberto.get((ano, mes))
        serie.append({
            'label': f'{mes:02d}/{ano}',
            'valor': float(linha['total']) if linha else 0.0,
            'quantidade': linha['quantidade'] if linha else 0,
        })
    return serie


def serie_despesas_por_categoria(n_meses=12, referencia=None):
    """Total de despesas (exceto canceladas) por categoria no período, ordenado desc."""
    competencias = competencias_ultimas(n_meses, referencia)
    filtro = filtro_competencias(competencias)
    nomes = dict(Despesa.CATEGORIA_CHOICES)

    linhas = (
        Despesa.objects.filter(filtro).exclude(status='cancelada')
        .values('categoria').annotate(total=Sum('valor'), quantidade=Count('id'))
    )
    serie = [
        {
            'categoria': nomes.get(linha['categoria'], linha['categoria']),
            'valor': float(linha['total'] or 0),
            'quantidade': linha['quantidade'],
        }
        for linha in linhas
    ]
    serie.sort(key=lambda item: item['valor'], reverse=True)
    return serie
