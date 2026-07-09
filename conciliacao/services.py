"""
Importação de extratos OFX e conciliação com receitas/despesas.

Regras de matching de créditos (repasse de aluguel):
1. Exata — uma única receita em aberto com valor_previsto igual ao crédito.
2. Repasse por imobiliária (bruto) — a soma das receitas em aberto de uma
   mesma imobiliária bate com o crédito (repasse consolidado de vários aluguéis).
3. Repasse por imobiliária (líquido) — idem, mas descontando as despesas de
   comissão automáticas vinculadas (imobiliária que repassa já sem a taxa).
"""
import hashlib
from datetime import timedelta
from decimal import Decimal

from django.db import transaction

from financeiro.models import ReceitaAluguel, Despesa
from .models import ExtratoImportado, TransacaoExtrato, ConciliacaoReceita, RegraClassificacao

JANELA_DIAS = 15
TOLERANCIA = Decimal('0.05')


class ExtratoJaImportadoError(Exception):
    """O mesmo arquivo (hash idêntico) já foi importado."""


class OFXInvalidoError(Exception):
    """O arquivo não pôde ser interpretado como OFX."""


def _hash_conteudo(conteudo):
    return hashlib.sha256(conteudo).hexdigest()


@transaction.atomic
def importar_ofx(arquivo, conta, usuario=None):
    """
    Importa um arquivo OFX para a conta informada.

    Idempotente em dois níveis: o hash do arquivo bloqueia reimportação do
    mesmo arquivo, e o FITID único por conta impede duplicar transações
    presentes em extratos de períodos sobrepostos.

    Retorna o ExtratoImportado criado (com contadores preenchidos).
    """
    import io

    from ofxparse import OfxParser

    conteudo = arquivo.read()
    arquivo.seek(0)

    hash_arquivo = _hash_conteudo(conteudo)
    if ExtratoImportado.objects.filter(hash_arquivo=hash_arquivo).exists():
        raise ExtratoJaImportadoError('Este arquivo de extrato já foi importado anteriormente.')

    try:
        ofx = OfxParser.parse(io.BytesIO(conteudo))
    except Exception as exc:
        raise OFXInvalidoError(f'Não foi possível ler o arquivo OFX: {exc}') from exc

    contas_ofx = getattr(ofx, 'accounts', None) or ([ofx.account] if getattr(ofx, 'account', None) else [])
    if not contas_ofx:
        raise OFXInvalidoError('O arquivo OFX não contém nenhuma conta com transações.')

    extrato = ExtratoImportado.objects.create(
        conta=conta,
        arquivo=arquivo,
        hash_arquivo=hash_arquivo,
        importado_por=usuario,
    )

    novas = 0
    duplicadas = 0
    datas = []

    for conta_ofx in contas_ofx:
        statement = getattr(conta_ofx, 'statement', None)
        if statement is None:
            continue
        for t in statement.transactions:
            data = t.date.date() if hasattr(t.date, 'date') else t.date
            fitid = str(t.id)
            if TransacaoExtrato.objects.filter(conta=conta, fitid=fitid).exists():
                duplicadas += 1
                continue
            valor = Decimal(str(t.amount))
            TransacaoExtrato.objects.create(
                extrato=extrato,
                conta=conta,
                fitid=fitid,
                data=data,
                valor=valor,
                tipo='credito' if valor >= 0 else 'debito',
                descricao=(t.payee or t.memo or '').strip()[:300],
                memo=(t.memo or '').strip()[:300],
            )
            novas += 1
            datas.append(data)

    extrato.transacoes_novas = novas
    extrato.transacoes_duplicadas = duplicadas
    if datas:
        extrato.periodo_inicio = min(datas)
        extrato.periodo_fim = max(datas)
    extrato.save(update_fields=['transacoes_novas', 'transacoes_duplicadas', 'periodo_inicio', 'periodo_fim'])
    return extrato


def receitas_candidatas(transacao):
    """Receitas em aberto com vencimento na janela de ±JANELA_DIAS da transação."""
    inicio = transacao.data - timedelta(days=JANELA_DIAS)
    fim = transacao.data + timedelta(days=JANELA_DIAS)
    return (
        ReceitaAluguel.objects
        .exclude(status__in=ReceitaAluguel.STATUS_QUITADOS)
        .filter(data_vencimento__gte=inicio, data_vencimento__lte=fim)
        .select_related('imovel', 'contrato')
        .prefetch_related('contrato__partes__pessoa')
        .order_by('data_vencimento', 'imovel__nome')
    )


def _comissoes_em_aberto(receitas):
    """Despesas de comissão automáticas, ainda não pagas, vinculadas às receitas."""
    return Despesa.objects.filter(
        receita__in=[r.pk for r in receitas],
        origem_automatica=True,
        categoria='comissao_imobiliaria',
    ).exclude(status__in=Despesa.STATUS_ENCERRADOS)


def sugerir_receitas(transacao):
    """
    Sugere o conjunto de receitas que o crédito quita.

    Retorna dict {'receitas': [...], 'tipo': 'exata'|'repasse'|'repasse_liquido',
    'comissoes': [...], 'total': Decimal} ou None quando não há match automático.
    """
    if transacao.tipo != 'credito':
        return None

    valor = transacao.valor_absoluto
    candidatas = list(receitas_candidatas(transacao))
    if not candidatas:
        return None

    # Passe 1 — receita única com valor exato
    exatas = [r for r in candidatas if abs(r.valor_previsto - valor) <= TOLERANCIA]
    if len(exatas) == 1:
        return {'receitas': exatas, 'tipo': 'exata', 'comissoes': [], 'total': exatas[0].valor_previsto}

    # Passes 2 e 3 — repasse consolidado por imobiliária (bruto e líquido de comissão)
    grupos = {}
    for r in candidatas:
        imobiliaria = r.contrato.get_imobiliaria_principal()
        if imobiliaria is not None:
            grupos.setdefault(imobiliaria.pk, []).append(r)

    for receitas in grupos.values():
        if len(receitas) < 2:
            continue
        total_bruto = sum((r.valor_previsto for r in receitas), Decimal('0.00'))
        if abs(total_bruto - valor) <= TOLERANCIA:
            return {'receitas': receitas, 'tipo': 'repasse', 'comissoes': [], 'total': total_bruto}

        comissoes = list(_comissoes_em_aberto(receitas))
        if comissoes:
            total_liquido = total_bruto - sum((c.valor for c in comissoes), Decimal('0.00'))
            if abs(total_liquido - valor) <= TOLERANCIA:
                return {
                    'receitas': receitas, 'tipo': 'repasse_liquido',
                    'comissoes': comissoes, 'total': total_liquido,
                }

    return None


def sugerir_classificacao(transacao):
    """Primeira RegraClassificacao ativa (por prioridade) cujo texto casa com a transação."""
    if transacao.tipo != 'debito':
        return None
    for regra in RegraClassificacao.objects.filter(ativo=True):
        if regra.aplica_a(transacao):
            return regra
    return None


@transaction.atomic
def conciliar_com_receitas(transacao, receitas, marcar_comissoes=False):
    """
    Vincula a transação (crédito) às receitas e dá baixa nelas.

    - Com marcar_comissoes=True (repasse líquido): cada receita é considerada
      integralmente paga pelo inquilino (valor_recebido = valor_previsto) e as
      despesas de comissão automáticas vinculadas são marcadas como pagas.
    - Sem marcar_comissoes: o valor do crédito é distribuído na ordem de
      vencimento; receita coberta parcialmente fica com status 'parcial'.
    """
    receitas = sorted(receitas, key=lambda r: (r.data_vencimento, r.pk))
    restante = transacao.valor_absoluto

    for receita in receitas:
        if marcar_comissoes:
            atribuido = receita.valor_previsto
            recebido = receita.valor_previsto
        else:
            atribuido = min(restante, receita.valor_previsto)
            recebido = atribuido
            restante -= atribuido

        ConciliacaoReceita.objects.create(
            transacao=transacao, receita=receita, valor_atribuido=atribuido,
        )
        receita.valor_recebido = recebido
        receita.data_recebimento = transacao.data
        receita.status = 'recebido' if recebido >= receita.valor_previsto - TOLERANCIA else 'parcial'
        receita.save()

    if marcar_comissoes:
        for comissao in _comissoes_em_aberto(receitas):
            comissao.status = 'paga'
            comissao.data_pagamento = transacao.data
            comissao.save(update_fields=['status', 'data_pagamento'])

    transacao.status = 'conciliada'
    transacao.save(update_fields=['status'])
    return transacao


@transaction.atomic
def lancar_despesa(transacao, categoria, descricao, fornecedor=None, imovel=None):
    """Cria uma Despesa paga a partir de um débito do extrato e vincula à transação."""
    despesa = Despesa.objects.create(
        imovel=imovel,
        fornecedor=fornecedor,
        categoria=categoria,
        descricao=descricao,
        competencia_mes=transacao.data.month,
        competencia_ano=transacao.data.year,
        data_vencimento=transacao.data,
        data_pagamento=transacao.data,
        valor=transacao.valor_absoluto,
        status='paga',
    )
    transacao.despesa = despesa
    transacao.status = 'conciliada'
    transacao.save(update_fields=['despesa', 'status'])
    return despesa


def transacoes_pendentes_qs():
    """Transações de extrato ainda não tratadas — usada em dashboard e checklist."""
    return TransacaoExtrato.objects.filter(status='pendente')
