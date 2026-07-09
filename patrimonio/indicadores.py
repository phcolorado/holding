"""
Indicadores patrimoniais: rentabilidade (yield), ocupação e totais de 12 meses.
Usados na página de detalhe do imóvel e no dashboard.
"""
from decimal import Decimal

from django.db.models import Q, Sum
from django.utils import timezone


def competencias_ultimas(n_meses, referencia=None):
    """Lista [(ano, mes), ...] das últimas n competências até a referência (inclusive), em ordem cronológica."""
    ref = referencia or timezone.localdate()
    ano, mes = ref.year, ref.month
    competencias = []
    for _ in range(n_meses):
        competencias.append((ano, mes))
        mes -= 1
        if mes == 0:
            mes, ano = 12, ano - 1
    return competencias[::-1]


def competencias_ultimos_12_meses(referencia=None):
    return competencias_ultimas(12, referencia)


def filtro_competencias(competencias, campo_ano='competencia_ano', campo_mes='competencia_mes'):
    """Q object que casa qualquer uma das competências informadas."""
    filtro = Q()
    for ano, mes in competencias:
        filtro |= Q(**{campo_ano: ano, campo_mes: mes})
    return filtro


def _percentual(parte, todo):
    if not todo:
        return None
    return (Decimal(parte) / Decimal(todo) * 100).quantize(Decimal('0.01'))


def indicadores_do_imovel(imovel, referencia=None):
    """
    Calcula, para os últimos 12 meses de competência:
    receita recebida, despesas pagas, resultado, yield bruto/líquido anual
    (sobre valor estimado, com fallback para valor de aquisição) e ocupação.
    """
    from financeiro.models import ReceitaAluguel, Despesa

    competencias = competencias_ultimos_12_meses(referencia)
    filtro = filtro_competencias(competencias)

    receita_12m = ReceitaAluguel.objects.filter(
        filtro, imovel=imovel, status__in=('recebido', 'parcial')
    ).aggregate(total=Sum('valor_recebido'))['total'] or Decimal('0.00')

    despesa_12m = Despesa.objects.filter(
        filtro, imovel=imovel, status='paga'
    ).aggregate(total=Sum('valor'))['total'] or Decimal('0.00')

    resultado_12m = receita_12m - despesa_12m

    valor_base = imovel.valor_estimado or imovel.valor_aquisicao
    yield_bruto = _percentual(receita_12m, valor_base)
    yield_liquido = _percentual(resultado_12m, valor_base)

    meses_ocupados = _meses_ocupados(imovel, competencias)
    ocupacao = _percentual(meses_ocupados, len(competencias))

    return {
        'receita_12m': receita_12m,
        'despesa_12m': despesa_12m,
        'resultado_12m': resultado_12m,
        'valor_base': valor_base,
        'yield_bruto': yield_bruto,
        'yield_liquido': yield_liquido,
        'meses_ocupados': meses_ocupados,
        'meses_periodo': len(competencias),
        'ocupacao': ocupacao,
    }


def _meses_ocupados(imovel, competencias):
    """
    Conta em quantas das competências houve contrato vigente (qualquer status
    exceto suspenso), considerando encerramento real e prazo indeterminado.
    """
    from datetime import date
    from calendar import monthrange

    contratos = list(imovel.contrato_set.exclude(status='suspenso'))
    if not contratos:
        return 0

    ocupados = 0
    for ano, mes in competencias:
        primeiro = date(ano, mes, 1)
        ultimo = date(ano, mes, monthrange(ano, mes)[1])
        for contrato in contratos:
            fim = contrato.data_fim_efetiva  # None = aberto (prazo indeterminado)
            if contrato.data_inicio <= ultimo and (fim is None or fim >= primeiro):
                ocupados += 1
                break
    return ocupados
