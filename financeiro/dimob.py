"""
Apuração dos dados da DIMOB — Declaração de Informações sobre Atividades
Imobiliárias (IN RFB 1.115/2010).

ESCOPO DESTE MÓDULO: apurar os números da ficha de LOCAÇÃO (registro R02) e
apontar o que falta no cadastro. NÃO gera o arquivo .txt no leiaute oficial
— isso é uma etapa seguinte, que exige campos que o sistema ainda não tem
(código do município na tabela da RFB, tipo U/R do imóvel, CNPJ do
declarante). O que sai daqui é a planilha de apoio para a contabilidade
preencher o PGD.

Regras da DIMOB que determinam a apuração:

* REGIME DE CAIXA, por mês de recebimento — o rendimento bruto entra no mês
  em que o aluguel foi efetivamente pago, não no mês de competência. Por
  isso a base é RecebimentoReceita.data_recebimento (o registro oficial de
  pagamento no sistema), nunca a competência da ReceitaAluguel.
  EXCEÇÃO NECESSÁRIA: uma receita pode ter valor_recebido consolidado SEM
  nenhum RecebimentoReceita — é o dado legado que o próprio model trata
  como válido (ver ReceitaAluguel.garantir_recebimento_legado e o
  preservar_legado de recalcular_recebimentos). Esse dinheiro entrou de
  verdade e precisa aparecer na DIMOB; ler só RecebimentoReceita deixaria
  a declaração a menos. Ver _rendimentos_legados_por_contrato().
* UM REGISTRO POR CONTRATO (não por imóvel nem por locatário).
* Entram os contratos que TIVERAM operação no ano — inclusive contratos já
  encerrados, se receberam aluguel dentro do ano-calendário.
* A COMISSÃO é informada no mês em que foi PAGA à imobiliária (por isso a
  base é Despesa.data_pagamento das comissões, com status 'paga').
"""
from collections import defaultdict
from decimal import Decimal

from django.db.models import Q, Sum
from django.db.models.functions import Coalesce

from .models import Despesa, ReceitaAluguel, RecebimentoReceita

MESES_ABREV = [
    'Jan', 'Fev', 'Mar', 'Abr', 'Mai', 'Jun',
    'Jul', 'Ago', 'Set', 'Out', 'Nov', 'Dez',
]

ZERO = Decimal('0.00')

# Sinalizações de cadastro incompleto para a DIMOB. O relatório NUNCA omite
# o contrato por causa delas — a contabilidade precisa ver a linha e saber
# o que falta preencher antes de transmitir.
FALTA_CPF_LOCATARIO = 'Locatário sem CPF/CNPJ cadastrado'
FALTA_LOCATARIO = 'Contrato sem locatário cadastrado'
FALTA_CEP = 'Imóvel sem CEP (a DIMOB exige CEP e código do município)'
FALTA_ENDERECO = 'Imóvel sem endereço'


def _doze_meses():
    return [ZERO] * 12


def _rendimentos_por_contrato(ano):
    """
    Soma, por contrato e por mês de RECEBIMENTO, o valor efetivamente pago —
    a base do "rendimento bruto" mensal da DIMOB (regime de caixa).
    """
    totais = defaultdict(_doze_meses)
    recebimentos = (
        RecebimentoReceita.objects
        .filter(data_recebimento__year=ano, receita__contrato__isnull=False)
        .values_list('receita__contrato_id', 'data_recebimento', 'valor')
    )
    for contrato_id, data, valor in recebimentos:
        totais[contrato_id][data.month - 1] += valor
    _somar_rendimentos_legados(ano, totais)
    return totais


def _receitas_legadas(ano):
    """
    Receitas com valor_recebido consolidado e NENHUM RecebimentoReceita, cuja
    data de caixa cai no ano — o dado legado que o model preserva.

    A data de caixa é data_recebimento e, na falta dela, data_vencimento —
    exatamente o critério que garantir_recebimento_legado() usaria ao
    materializar esse valor como recebimento.

    O status NÃO filtra: uma receita cancelada cujo dinheiro entrou continua
    contabilizada (mesma regra de recalcular_recebimentos), e os
    recebimentos normais também não são filtrados por status.
    """
    return (
        ReceitaAluguel.objects
        .filter(valor_recebido__gt=0, recebimentos__isnull=True)
        .annotate(data_caixa=Coalesce('data_recebimento', 'data_vencimento'))
        .filter(data_caixa__year=ano)
    )


def _somar_rendimentos_legados(ano, totais):
    for contrato_id, data, valor in _receitas_legadas(ano).values_list(
        'contrato_id', 'data_caixa', 'valor_recebido'
    ):
        totais[contrato_id][data.month - 1] += valor


def _comissoes_por_contrato(ano):
    """
    Soma, por contrato e por mês de PAGAMENTO, as comissões de administração
    pagas à imobiliária. A despesa pode estar ligada ao contrato diretamente
    ou apenas à receita — as duas formas são consideradas.
    """
    totais = defaultdict(_doze_meses)
    comissoes = (
        Despesa.objects
        .filter(
            categoria='comissao_imobiliaria',
            status='paga',
            data_pagamento__year=ano,
        )
        .filter(Q(contrato__isnull=False) | Q(receita__contrato__isnull=False))
        .values_list('contrato_id', 'receita__contrato_id', 'data_pagamento', 'valor')
    )
    for contrato_id, contrato_da_receita_id, data, valor in comissoes:
        alvo = contrato_id or contrato_da_receita_id
        if alvo is None:
            continue
        totais[alvo][data.month - 1] += valor
    return totais


def _pendencias_do_contrato(contrato, locatarios):
    pendencias = []
    if not locatarios:
        pendencias.append(FALTA_LOCATARIO)
    else:
        sem_doc = [p.nome for p in locatarios if not p.cpf_cnpj]
        if sem_doc:
            pendencias.append(f'{FALTA_CPF_LOCATARIO}: {", ".join(sem_doc)}')
    imovel = contrato.imovel
    if not imovel.endereco:
        pendencias.append(FALTA_ENDERECO)
    if not imovel.cep:
        pendencias.append(FALTA_CEP)
    return pendencias


def linhas_locacao(ano):
    """
    Uma entrada por CONTRATO com operação de locação no ano-calendário.

    Cada entrada é um dict com identificação (contrato, imóvel, locatários),
    as 12 posições mensais de rendimento bruto e de comissão, os totais e a
    lista de pendências de cadastro. Contratos encerrados que receberam
    dentro do ano ENTRAM (a DIMOB olha a operação, não a vigência).

    Ordenado por nome do imóvel para conferência.
    """
    # Import local: patrimonio importa financeiro, então o import de
    # Contrato no topo deste módulo criaria ciclo.
    from patrimonio.models import Contrato

    rendimentos = _rendimentos_por_contrato(ano)
    comissoes = _comissoes_por_contrato(ano)

    contratos_com_operacao = set(rendimentos) | set(comissoes)
    if not contratos_com_operacao:
        return []

    contratos = (
        Contrato.objects
        .filter(pk__in=contratos_com_operacao)
        .select_related('imovel', 'locatario')
        .prefetch_related('partes__pessoa')
        .order_by('imovel__nome', 'pk')
    )

    linhas = []
    for contrato in contratos:
        locatarios = contrato.get_locatarios()
        rend = rendimentos.get(contrato.pk, _doze_meses())
        com = comissoes.get(contrato.pk, _doze_meses())
        linhas.append({
            'contrato': contrato,
            'imovel': contrato.imovel,
            'locatarios': locatarios,
            # A DIMOB aceita no máximo 6 posições no número do contrato; o
            # id do sistema é a identificação estável de cada contrato.
            'numero_contrato': str(contrato.pk)[:6],
            'data_contrato': contrato.data_inicio,
            'rendimento_mensal': rend,
            'comissao_mensal': com,
            'total_rendimento': sum(rend, ZERO),
            'total_comissao': sum(com, ZERO),
            'pendencias': _pendencias_do_contrato(contrato, locatarios),
        })
    return linhas


def anos_com_movimento_de_caixa():
    """
    Anos-calendário em que existe QUALQUER entrada de aluguel — recebimentos
    registrados ou valor consolidado legado. É a resposta direta para
    "pedi 2025 e veio vazio: então em que ano estão os meus dados?".
    """
    anos = {
        data.year
        for data in RecebimentoReceita.objects
        .values_list('data_recebimento', flat=True)
        if data is not None
    }
    anos.update(
        data.year
        for data in ReceitaAluguel.objects
        .filter(valor_recebido__gt=0, recebimentos__isnull=True)
        .annotate(data_caixa=Coalesce('data_recebimento', 'data_vencimento'))
        .values_list('data_caixa', flat=True)
        if data is not None
    )
    return sorted(anos)


def diagnostico(ano):
    """
    Explica por que a ficha de locação de um ano saiu vazia (ou conferida).

    Não altera nada — só conta o que existe no banco. Devolve um dict com os
    números e uma lista `conclusoes` em linguagem direta, para o comando
    `manage.py diagnostico_dimob`.
    """
    from patrimonio.models import Contrato

    linhas = linhas_locacao(ano)

    recebimentos = RecebimentoReceita.objects.filter(
        data_recebimento__year=ano, receita__contrato__isnull=False,
    )
    legadas = _receitas_legadas(ano)
    comissoes = Despesa.objects.filter(
        categoria='comissao_imobiliaria', status='paga', data_pagamento__year=ano,
    )

    dados = {
        'ano': ano,
        'contratos_total': Contrato.objects.count(),
        'contratos_ativos': Contrato.objects.filter(status='ativo').count(),
        'receitas_total': ReceitaAluguel.objects.count(),
        'receitas_competencia_no_ano': ReceitaAluguel.objects.filter(competencia_ano=ano).count(),
        'recebimentos_total': RecebimentoReceita.objects.count(),
        'recebimentos_no_ano': recebimentos.count(),
        'valor_recebimentos_no_ano': recebimentos.aggregate(t=Sum('valor'))['t'] or ZERO,
        'legadas_no_ano': legadas.count(),
        'valor_legadas_no_ano': legadas.aggregate(t=Sum('valor_recebido'))['t'] or ZERO,
        'comissoes_no_ano': comissoes.count(),
        'valor_comissoes_no_ano': comissoes.aggregate(t=Sum('valor'))['t'] or ZERO,
        'linhas_geradas': len(linhas),
        'anos_com_movimento': anos_com_movimento_de_caixa(),
    }

    conclusoes = []
    if dados['linhas_geradas']:
        conclusoes.append(
            f"A ficha de {ano} tem {dados['linhas_geradas']} contrato(s) — o relatório NÃO está vazio."
        )
    elif dados['contratos_total'] == 0:
        conclusoes.append('Não há NENHUM contrato cadastrado — não há o que declarar.')
    elif dados['recebimentos_total'] == 0 and not dados['anos_com_movimento']:
        conclusoes.append(
            'Não há nenhum recebimento de aluguel registrado em ano nenhum. '
            'A DIMOB é por regime de caixa: enquanto os aluguéis não forem '
            'baixados (tela "Baixa de receitas" ou recebimento no Admin), a '
            'ficha sai vazia mesmo com contratos e receitas previstas.'
        )
    elif dados['anos_com_movimento']:
        anos = ', '.join(str(a) for a in dados['anos_com_movimento'])
        conclusoes.append(
            f'Não houve entrada de aluguel em {ano}. Os anos com recebimento '
            f'registrado são: {anos}. Gere a ficha para um desses anos.'
        )
    if (
        dados['receitas_competencia_no_ano']
        and not dados['recebimentos_no_ano']
        and not dados['legadas_no_ano']
    ):
        conclusoes.append(
            f"Existem {dados['receitas_competencia_no_ano']} receita(s) com competência em "
            f'{ano}, mas nenhuma foi baixada com data de recebimento em {ano} — '
            'a DIMOB conta o mês do PAGAMENTO, não o da competência.'
        )
    dados['conclusoes'] = conclusoes
    return dados
