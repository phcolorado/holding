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


def periodo_fim_ocupacao(contrato):
    """
    Fim efetivo do contrato PARA FINS DE OCUPAÇÃO (regra defensiva):
    - encerramento real sempre vence;
    - contrato ATIVO por prazo indeterminado é aberto (None);
    - contrato encerrado/rescindido por prazo indeterminado SEM encerramento
      real não pode ocupar indefinidamente — usa a data_fim original.
    """
    if contrato.data_encerramento_real:
        return contrato.data_encerramento_real
    if contrato.prazo_indeterminado and contrato.status == 'ativo':
        return None
    return contrato.data_fim


def contrato_ocupa_mes(contrato, primeiro_dia, ultimo_dia):
    """Se o contrato ocupa (ao menos parte de) o mês — contratos suspensos não ocupam."""
    if contrato.status == 'suspenso':
        return False
    fim = periodo_fim_ocupacao(contrato)
    return contrato.data_inicio <= ultimo_dia and (fim is None or fim >= primeiro_dia)


def receita_locaticia(receitas):
    """
    Parcela LOCATÍCIA (aluguel) do que foi recebido, excluindo reembolsos de
    encargos (IPTU, condomínio etc.). Usa os itens tipo 'aluguel' da receita;
    receitas antigas sem itens usam valor_previsto como fallback (antes dos
    encargos, o previsto era só o aluguel). Considera que o aluguel é quitado
    primeiro em pagamentos parciais. valor_recebido aqui já reflete o
    dinheiro real recebido mesmo em receitas canceladas (recalcular_recebi
    mentos reconsolida esse campo independentemente do status).
    """
    total = Decimal('0.00')
    for r in receitas:
        itens = list(r.itens.all())
        if itens:
            base_aluguel = sum((i.valor for i in itens if i.tipo == 'aluguel'), Decimal('0.00'))
        else:
            base_aluguel = r.valor_previsto
        total += min(r.valor_recebido or Decimal('0.00'), base_aluguel)
    return total


def indicadores_do_imovel(imovel, referencia=None, incluir_despesas=True):
    """
    Calcula, para os últimos 12 meses de competência: receita total de caixa,
    receita locatícia (só aluguel), despesas pagas, resultado, yield
    bruto/líquido anual sobre a receita LOCATÍCIA (valor estimado, com
    fallback para valor de aquisição) e ocupação.

    Com incluir_despesas=False (usuário sem permissão de despesas), os valores
    que dependem de despesas (despesa_12m, resultado_12m, yield_liquido) nem
    são consultados — retornam None.

    Inclui receitas CANCELADAS que tiveram algum recebimento: o dinheiro que
    entrou continua no caixa mesmo com a cobrança encerrada depois — só
    exclui receitas sem nenhum valor_recebido (nada entrou).
    """
    from financeiro.models import ReceitaAluguel, Despesa

    competencias = competencias_ultimos_12_meses(referencia)
    filtro = filtro_competencias(competencias)

    receitas_recebidas = list(
        ReceitaAluguel.objects.filter(filtro, imovel=imovel, valor_recebido__isnull=False)
        .prefetch_related('itens')
    )
    receita_12m = sum((r.valor_recebido or Decimal('0.00') for r in receitas_recebidas), Decimal('0.00'))
    receita_locaticia_12m = receita_locaticia(receitas_recebidas)

    if incluir_despesas:
        despesa_12m = Despesa.objects.filter(
            filtro, imovel=imovel, status='paga'
        ).aggregate(total=Sum('valor'))['total'] or Decimal('0.00')
        resultado_12m = receita_12m - despesa_12m
    else:
        despesa_12m = None
        resultado_12m = None

    valor_base = imovel.valor_estimado or imovel.valor_aquisicao
    # Yield calculado sobre a receita locatícia — IPTU/condomínio repassados
    # são valores transitórios e não remuneram o capital investido.
    yield_bruto = _percentual(receita_locaticia_12m, valor_base)
    yield_liquido = (
        _percentual(receita_locaticia_12m - despesa_12m, valor_base)
        if incluir_despesas else None
    )

    meses_ocupados = _meses_ocupados(imovel, competencias)
    ocupacao = _percentual(meses_ocupados, len(competencias))

    return {
        'receita_12m': receita_12m,
        'receita_locaticia_12m': receita_locaticia_12m,
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
    Conta em quantas das competências houve contrato vigente, usando a regra
    defensiva de periodo_fim_ocupacao (encerrado/rescindido nunca ocupa
    indefinidamente; suspenso não ocupa).
    """
    from datetime import date
    from calendar import monthrange

    contratos = list(imovel.contrato_set.all())
    if not contratos:
        return 0

    ocupados = 0
    for ano, mes in competencias:
        primeiro = date(ano, mes, 1)
        ultimo = date(ano, mes, monthrange(ano, mes)[1])
        if any(contrato_ocupa_mes(c, primeiro, ultimo) for c in contratos):
            ocupados += 1
    return ocupados
