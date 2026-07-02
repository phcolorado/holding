from calendar import monthrange
from datetime import date
from decimal import Decimal

from django.db.models import Q

from .models import ReceitaAluguel, ReceitaAluguelItem, Despesa


def _itens_para_competencia(contrato, ano, mes):
    """
    Retorna a lista de itens (tipo/descrição/valor) com base nos encargos
    ativos do contrato que se aplicam à competência informada.

    Retorna None quando o contrato não tem nenhum encargo cadastrado —
    nesse caso o chamador deve usar o fallback para contrato.valor_aluguel.
    """
    encargos = list(contrato.encargos.all())
    if not encargos:
        return None

    itens = []
    for encargo in encargos:
        if encargo.aplica_em(ano, mes):
            itens.append({
                'tipo': encargo.tipo,
                'descricao': encargo.descricao or encargo.get_tipo_display(),
                'valor': encargo.valor,
            })
    return itens


def _criar_despesa_administracao(contrato, receita, ano, mes):
    """
    Cria (se ainda não existir) a despesa automática de taxa de administração
    da imobiliária, calculada como percentual sobre o encargo de aluguel
    (ou sobre valor_aluguel, se não houver encargos configurados).
    """
    if not contrato.comissao_imobiliaria_percentual:
        return None

    encargo_aluguel = contrato.encargos.filter(tipo='aluguel', ativo=True).first()
    if encargo_aluguel and encargo_aluguel.aplica_em(ano, mes):
        base = encargo_aluguel.valor
    else:
        base = contrato.valor_aluguel

    valor_taxa = (base * contrato.comissao_imobiliaria_percentual / Decimal('100')).quantize(Decimal('0.01'))

    despesa, _criada = Despesa.objects.get_or_create(
        contrato=contrato,
        receita=receita,
        origem_automatica=True,
        categoria='comissao_imobiliaria',
        defaults={
            'imovel': contrato.imovel,
            'descricao': f'Taxa de administração — {contrato.imovel.nome} ({mes:02d}/{ano})',
            'competencia_mes': mes,
            'competencia_ano': ano,
            'data_vencimento': receita.data_vencimento,
            'valor': valor_taxa,
            'status': 'prevista',
        },
    )
    return despesa


def gerar_receitas_para_contrato(contrato, data_inicio=None, data_fim=None):
    """
    Gera ReceitaAluguel para cada mês da vigência do contrato.

    data_inicio e data_fim são opcionais. Quando informados, limitam o
    intervalo de geração — útil para gerar apenas o mês atual. O período
    efetivo é sempre a interseção desses parâmetros com a vigência real do
    contrato (considerando prazo indeterminado e encerramento real),
    portanto nunca gera receitas fora do prazo contratual.

    Para cada receita nova, cria os itens correspondentes aos encargos
    ativos do contrato (ou usa valor_aluguel como fallback quando o
    contrato não tem encargos cadastrados) e, se configurada, a despesa
    automática de taxa de administração. Receitas já existentes nunca são
    alteradas.

    Retorna: (criadas, ja_existiam)
    """
    if contrato.status != 'ativo':
        return 0, 0

    contrato.garantir_encargo_aluguel()

    # Normaliza os limites para o primeiro/último dia do mês informado
    if data_inicio:
        limite_inicio = date(data_inicio.year, data_inicio.month, 1)
    else:
        limite_inicio = contrato.data_inicio

    if data_fim:
        ultimo = monthrange(data_fim.year, data_fim.month)[1]
        limite_fim = date(data_fim.year, data_fim.month, ultimo)
    else:
        # Sem intervalo explícito: quando o contrato é por prazo
        # indeterminado e sem encerramento real, usa a data_fim original
        # como limite conservador (evita geração indefinida em ações em lote).
        limite_fim = contrato.data_fim_efetiva or contrato.data_fim

    contrato_fim = contrato.data_fim_efetiva  # None quando indeterminado e sem encerramento real
    inicio_efetivo = max(limite_inicio, contrato.data_inicio)
    fim_efetivo = min(limite_fim, contrato_fim) if contrato_fim is not None else limite_fim

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

        itens_dados = _itens_para_competencia(contrato, ano, mes)
        if itens_dados is not None:
            valor_previsto_calc = sum((item['valor'] for item in itens_dados), Decimal('0.00'))
        else:
            valor_previsto_calc = contrato.valor_aluguel

        receita, criada = ReceitaAluguel.objects.get_or_create(
            contrato=contrato,
            competencia_mes=mes,
            competencia_ano=ano,
            defaults={
                'imovel': contrato.imovel,
                'data_vencimento': data_vencimento,
                'valor_previsto': valor_previsto_calc,
                'status': 'previsto',
            },
        )

        if criada:
            if itens_dados:
                ReceitaAluguelItem.objects.bulk_create([
                    ReceitaAluguelItem(receita=receita, tipo=i['tipo'], descricao=i['descricao'], valor=i['valor'])
                    for i in itens_dados
                ])
            _criar_despesa_administracao(contrato, receita, ano, mes)
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


def contratos_para_geracao_mes(mes, ano):
    """
    Retorna o queryset de contratos aptos a gerar receita no mês/ano
    informado: ativos, iniciados até o fim do mês, e com vigência cobrindo
    o mês — seja por data_fim (contrato determinado) ou por prazo
    indeterminado (considerado aberto). Contratos com data_encerramento_real
    anterior ao mês são excluídos, independentemente de prazo_indeterminado.

    Usada tanto para gerar as receitas quanto para exibir a contagem de
    contratos aptos na tela "Gerar Receitas".
    """
    from patrimonio.models import Contrato

    data_inicio_mes = date(ano, mes, 1)
    data_fim_mes = date(ano, mes, monthrange(ano, mes)[1])

    return Contrato.objects.filter(
        status='ativo',
        data_inicio__lte=data_fim_mes,
    ).filter(
        Q(data_fim__gte=data_inicio_mes) | Q(prazo_indeterminado=True)
    ).filter(
        Q(data_encerramento_real__isnull=True) | Q(data_encerramento_real__gte=data_inicio_mes)
    )


def gerar_receitas_mes(mes, ano):
    """
    Percorre todos os contratos ativos que cobrem o mês/ano informado
    e gera as ReceitaAluguel esperadas para esse período.

    Contratos por prazo indeterminado continuam sendo considerados mesmo
    após a data_fim original, desde que não tenham data_encerramento_real
    anterior ao mês solicitado. Contratos encerrados/rescindidos (status
    diferente de 'ativo') nunca geram receitas.

    Retorna: (total_criadas, total_ja_existiam)
    """
    data_inicio_mes = date(ano, mes, 1)
    data_fim_mes = date(ano, mes, monthrange(ano, mes)[1])

    contratos = contratos_para_geracao_mes(mes, ano)

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
