from calendar import monthrange
from datetime import date

from .models import ReceitaAluguel


def gerar_receitas_para_contrato(contrato, data_inicio=None, data_fim=None):
    """
    Gera ReceitaAluguel para cada mês da vigência do contrato.

    data_inicio e data_fim são opcionais. Quando informados, limitam o
    intervalo de geração — útil para gerar apenas o mês atual. O período
    efetivo é sempre a interseção desses parâmetros com a vigência real do
    contrato, portanto nunca gera receitas fora do prazo contratual.

    Retorna: (criadas, ja_existiam)
    """
    if contrato.status != 'ativo':
        return 0, 0

    # Normaliza os limites para o primeiro/último dia do mês informado
    if data_inicio:
        limite_inicio = date(data_inicio.year, data_inicio.month, 1)
    else:
        limite_inicio = contrato.data_inicio

    if data_fim:
        ultimo = monthrange(data_fim.year, data_fim.month)[1]
        limite_fim = date(data_fim.year, data_fim.month, ultimo)
    else:
        limite_fim = contrato.data_fim

    # Interseção com o período do próprio contrato
    inicio_efetivo = max(limite_inicio, contrato.data_inicio)
    fim_efetivo = min(limite_fim, contrato.data_fim)

    if inicio_efetivo > fim_efetivo:
        return 0, 0

    criadas = 0
    ja_existiam = 0

    ano, mes = inicio_efetivo.year, inicio_efetivo.month
    fim_ano, fim_mes = fim_efetivo.year, fim_efetivo.month

    while (ano, mes) <= (fim_ano, fim_mes):
        ultimo_dia = monthrange(ano, mes)[1]
        # Ajusta dia de vencimento para meses com menos dias (ex.: 31 em fevereiro)
        dia_venc = min(contrato.dia_vencimento, ultimo_dia)
        data_vencimento = date(ano, mes, dia_venc)

        _, criada = ReceitaAluguel.objects.get_or_create(
            contrato=contrato,
            competencia_mes=mes,
            competencia_ano=ano,
            defaults={
                'imovel': contrato.imovel,
                'data_vencimento': data_vencimento,
                'valor_previsto': contrato.valor_aluguel,
                'status': 'previsto',
            },
        )

        if criada:
            criadas += 1
        else:
            ja_existiam += 1

        # Avança para o próximo mês
        if mes == 12:
            ano += 1
            mes = 1
        else:
            mes += 1

    return criadas, ja_existiam


def gerar_receitas_mes(mes, ano):
    """
    Percorre todos os contratos ativos que cobrem o mês/ano informado
    e gera as ReceitaAluguel esperadas para esse período.

    Retorna: (total_criadas, total_ja_existiam)
    """
    from patrimonio.models import Contrato

    data_inicio_mes = date(ano, mes, 1)
    data_fim_mes = date(ano, mes, monthrange(ano, mes)[1])

    # Contratos ativos que se sobrepõem ao mês solicitado
    contratos = Contrato.objects.filter(
        status='ativo',
        data_inicio__lte=data_fim_mes,
        data_fim__gte=data_inicio_mes,
    )

    total_criadas = 0
    total_existiam = 0

    for contrato in contratos:
        criadas, existiam = gerar_receitas_para_contrato(
            contrato,
            data_inicio=data_inicio_mes,
            data_fim=data_fim_mes,
        )
        total_criadas += criadas
        total_existiam += existiam

    return total_criadas, total_existiam
