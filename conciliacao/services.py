"""
Importação de extratos OFX e conciliação com receitas/despesas.

Regras de matching de créditos (repasse de aluguel), sempre sobre o
SALDO EM ABERTO de cada receita (não sobre o valor previsto):
1. Exata — uma única receita em aberto cujo saldo é igual ao crédito.
2. Repasse por imobiliária (bruto) — a soma dos saldos das receitas em
   aberto de uma mesma imobiliária bate com o crédito.
3. Repasse por imobiliária (líquido) — idem, mas descontando as despesas de
   comissão automáticas ainda não pagas vinculadas a essas receitas.
"""
import hashlib
from datetime import timedelta
from decimal import Decimal

from django.db import transaction

from financeiro.models import ReceitaAluguel, RecebimentoReceita, Despesa
from financeiro.services import registrar_recebimento_bloqueada
from .models import (
    ExtratoImportado, TransacaoExtrato, ConciliacaoReceita, ConciliacaoComissao,
    RegraClassificacao,
)

JANELA_DIAS = 15
# Usada SOMENTE para sugerir/pré-selecionar candidatas em sugerir_receitas()
# (heurística de matching, ajuda o usuário a escolher). O COMMIT em
# conciliar_com_receitas() nunca usa tolerância — exige igualdade exata entre
# a soma atribuída e o crédito, já que valores monetários (Decimal, 2 casas)
# não têm imprecisão de ponto flutuante que justifique folga (item 8).
TOLERANCIA = Decimal('0.05')


class ExtratoJaImportadoError(Exception):
    """O mesmo arquivo (hash idêntico) já foi importado."""


class OFXInvalidoError(Exception):
    """O arquivo não pôde ser interpretado como OFX ou não corresponde à conta."""


class ConciliacaoInvalidaError(Exception):
    """A conciliação solicitada viola alguma regra de validação."""


def _hash_conteudo(conteudo):
    return hashlib.sha256(conteudo).hexdigest()


def _somente_digitos(valor):
    return ''.join(c for c in str(valor or '') if c.isdigit())


# Valores de FITID considerados ausentes/inválidos (após strip/lower)
_FITIDS_INVALIDOS = frozenset({'', 'none', 'null'})


def _campo_ofx(bloco, tag):
    """Valor textual de uma tag SGML/XML dentro de um bloco OFX ('' se ausente)."""
    import re
    m = re.search(rf'<{tag}>\s*([^<\r\n]*)', bloco, flags=re.I)
    return m.group(1).strip() if m else ''


def _fitid_gerado(assinatura_base, contadores):
    """
    Identificador determinístico para transação sem FITID:
    gerado-<sha256(assinatura)[:32]>-<nº de ocorrência da mesma assinatura>.

    A assinatura usa só os DADOS da transação (data, valor, descrição, memo,
    tipo) — nunca a posição global no arquivo, para que a mesma transação
    gere o MESMO id em extratos sobrepostos com ordenação diferente. O
    contador de ocorrência distingue duas transações realmente idênticas no
    mesmo arquivo (mantém o mesmo id quando a ordem relativa entre idênticas
    se preserva).
    """
    assinatura = hashlib.sha256(assinatura_base.encode('utf-8')).hexdigest()[:32]
    contadores[assinatura] = contadores.get(assinatura, 0) + 1
    return f'gerado-{assinatura}-{contadores[assinatura]}'


def _preencher_fitids_vazios(conteudo):
    """
    Bancos às vezes exportam <FITID> vazio (ou omitem a tag) — o ofxparse
    DESCARTA silenciosamente essas transações (fail_fast=False) ou lança erro
    (fail_fast=True), por isso o texto é corrigido ANTES do parse. Injeta um
    identificador determinístico baseado na assinatura da transação
    (data+valor+descrição+memo+tipo) + contador de ocorrência.
    """
    import re

    for codec in ('cp1252', 'utf-8', 'latin-1'):
        try:
            texto = conteudo.decode(codec)
            break
        except UnicodeDecodeError:
            continue

    contadores = {}
    alterou = [False]

    def substituir(m):
        bloco = m.group(0)
        fitid_atual = _campo_ofx(bloco, 'FITID')
        if fitid_atual.lower() not in _FITIDS_INVALIDOS:
            return bloco
        assinatura_base = '|'.join((
            _campo_ofx(bloco, 'DTPOSTED'),
            _campo_ofx(bloco, 'TRNAMT'),
            _campo_ofx(bloco, 'NAME') or _campo_ofx(bloco, 'PAYEE'),
            _campo_ofx(bloco, 'MEMO'),
            _campo_ofx(bloco, 'TRNTYPE'),
        ))
        gerado = _fitid_gerado(assinatura_base, contadores)
        alterou[0] = True
        if re.search(r'<FITID>', bloco, flags=re.I):
            return re.sub(r'<FITID>\s*(?=<|$)', f'<FITID>{gerado}', bloco, count=1, flags=re.I)
        # tag totalmente ausente — injeta logo após a abertura do bloco
        return bloco.replace('<STMTTRN>', f'<STMTTRN>\n<FITID>{gerado}', 1)

    texto = re.sub(r'<STMTTRN>.*?</STMTTRN>', substituir, texto, flags=re.S | re.I)
    if not alterou[0]:
        return conteudo
    return texto.encode(codec)


def _validar_conta_ofx(contas_ofx, conta):
    """
    Rejeita arquivos com múltiplas contas e confere o arquivo com a
    ContaBancaria cadastrada:
    - cadastro tem numero_conta e o OFX não traz ACCTID → erro;
    - cadastro tem bank_id e o OFX não traz BANKID → erro;
    - valores presentes dos dois lados e diferentes → erro;
    - campo não preenchido no cadastro → não é exigido/comparado.

    A comparação usa somente os dígitos (remove pontuação/hífen do DV), mas
    NUNCA converte para inteiro — zeros à esquerda são significativos e devem
    coincidir com o OFX.
    """
    if len(contas_ofx) > 1:
        raise OFXInvalidoError(
            f'O arquivo contém {len(contas_ofx)} contas bancárias. Exporte o extrato '
            'de uma única conta por arquivo e importe separadamente.'
        )
    conta_ofx = contas_ofx[0]
    acctid = _somente_digitos(getattr(conta_ofx, 'account_id', '') or getattr(conta_ofx, 'number', ''))
    bankid = _somente_digitos(getattr(conta_ofx, 'routing_number', '') or getattr(conta_ofx, 'bank_id', ''))

    numero_cadastrado = _somente_digitos(conta.numero_conta)
    if numero_cadastrado:
        if not acctid:
            raise OFXInvalidoError(
                f'O arquivo OFX não informa o número da conta (ACCTID), mas a conta '
                f'cadastrada "{conta.nome}" exige conferência ({conta.numero_conta}). '
                'Confirme se o arquivo é da conta correta ou limpe o número no cadastro '
                'para importar sem conferência.'
            )
        if numero_cadastrado != acctid:
            raise OFXInvalidoError(
                f'A conta do arquivo OFX ({acctid}) não corresponde à conta cadastrada '
                f'"{conta.nome}" ({numero_cadastrado}). Selecione a conta correta.'
            )
    bank_cadastrado = _somente_digitos(conta.bank_id)
    if bank_cadastrado:
        if not bankid:
            raise OFXInvalidoError(
                f'O arquivo OFX não informa o código do banco (BANKID), mas a conta '
                f'cadastrada "{conta.nome}" exige conferência (código {conta.bank_id}). '
                'Confirme o arquivo ou limpe o código do banco no cadastro.'
            )
        if bank_cadastrado != bankid:
            raise OFXInvalidoError(
                f'O banco do arquivo OFX (código {bankid}) não corresponde ao banco da conta '
                f'"{conta.nome}" (código {bank_cadastrado}).'
            )


@transaction.atomic
def importar_ofx(arquivo, conta, usuario=None):
    """
    Importa um arquivo OFX para a conta informada.

    Idempotente em dois níveis: o hash do arquivo bloqueia reimportação do
    mesmo arquivo, e o FITID único por conta (ou identificador determinístico,
    quando o banco não envia FITID) impede duplicar transações presentes em
    extratos de períodos sobrepostos.
    """
    import io

    from ofxparse import OfxParser

    conteudo = arquivo.read()
    arquivo.seek(0)

    hash_arquivo = _hash_conteudo(conteudo)
    if ExtratoImportado.objects.filter(hash_arquivo=hash_arquivo).exists():
        raise ExtratoJaImportadoError('Este arquivo de extrato já foi importado anteriormente.')

    conteudo_parse = _preencher_fitids_vazios(conteudo)

    try:
        ofx = OfxParser.parse(io.BytesIO(conteudo_parse))
    except Exception as exc:
        raise OFXInvalidoError(f'Não foi possível ler o arquivo OFX: {exc}') from exc

    contas_ofx = getattr(ofx, 'accounts', None) or ([ofx.account] if getattr(ofx, 'account', None) else [])
    contas_ofx = [c for c in contas_ofx if getattr(c, 'statement', None) is not None]
    if not contas_ofx:
        raise OFXInvalidoError('O arquivo OFX não contém nenhuma conta com transações.')

    _validar_conta_ofx(contas_ofx, conta)

    extrato = ExtratoImportado.objects.create(
        conta=conta,
        arquivo=arquivo,
        hash_arquivo=hash_arquivo,
        importado_por=usuario,
    )

    novas = 0
    duplicadas = 0
    datas = []
    contadores_fallback = {}

    for t in contas_ofx[0].statement.transactions:
        data = t.date.date() if hasattr(t.date, 'date') else t.date
        valor = Decimal(str(t.amount))
        descricao = (t.payee or t.memo or '').strip()
        # Fallback obrigatório pós-parse: se mesmo após o pré-processamento o
        # FITID vier ausente (None/''/'None'/'null'), gera o identificador
        # determinístico pela assinatura da transação (inclui a conta).
        fitid_original = t.id
        fitid = '' if fitid_original is None else str(fitid_original).strip()
        if fitid.lower() in _FITIDS_INVALIDOS:
            assinatura_base = '|'.join((
                str(conta.pk), str(data), str(valor), descricao,
                (t.memo or '').strip(), str(getattr(t, 'type', '') or ''),
            ))
            fitid = _fitid_gerado(assinatura_base, contadores_fallback)
        if TransacaoExtrato.objects.filter(conta=conta, fitid=fitid).exists():
            duplicadas += 1
            continue
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
    """Receitas com saldo em aberto e vencimento na janela de ±JANELA_DIAS da transação."""
    inicio = transacao.data - timedelta(days=JANELA_DIAS)
    fim = transacao.data + timedelta(days=JANELA_DIAS)
    # Regra centralizada em ReceitaAluguel.objects.em_aberto(): não canceladas
    # com saldo calculado no banco maior que zero — independe do campo status.
    return list(
        ReceitaAluguel.objects.em_aberto()
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
    Sugere o conjunto de receitas que o crédito quita, com base no SALDO.

    Retorna dict {'receitas': [...], 'tipo': 'exata'|'repasse'|'repasse_liquido',
    'comissoes': [...], 'total': Decimal} ou None quando não há match automático.
    """
    if transacao.tipo != 'credito':
        return None

    valor = transacao.valor_absoluto
    candidatas = receitas_candidatas(transacao)
    if not candidatas:
        return None

    # Passe 1 — receita única com saldo exato
    exatas = [r for r in candidatas if abs(r.saldo_em_aberto - valor) <= TOLERANCIA]
    if len(exatas) == 1:
        return {'receitas': exatas, 'tipo': 'exata', 'comissoes': [], 'total': exatas[0].saldo_em_aberto}

    # Passes 2 e 3 — repasse consolidado por imobiliária (bruto e líquido de comissão)
    grupos = {}
    for r in candidatas:
        imobiliaria = r.contrato.get_imobiliaria_principal()
        if imobiliaria is not None:
            grupos.setdefault(imobiliaria.pk, []).append(r)

    for receitas in grupos.values():
        if len(receitas) < 2:
            continue
        total_bruto = sum((r.saldo_em_aberto for r in receitas), Decimal('0.00'))
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


def _validar_conciliacao(transacao, receitas):
    if transacao.status != 'pendente':
        raise ConciliacaoInvalidaError('Esta transação já foi tratada (não está pendente).')
    if transacao.tipo != 'credito':
        raise ConciliacaoInvalidaError('Apenas créditos podem ser conciliados com receitas.')
    if not receitas:
        raise ConciliacaoInvalidaError('Selecione ao menos uma receita em aberto para conciliar.')

    inicio = transacao.data - timedelta(days=JANELA_DIAS)
    fim = transacao.data + timedelta(days=JANELA_DIAS)
    for r in receitas:
        # saldo_em_aberto já é 0 para canceladas — regra de saldo, não de status
        if r.saldo_em_aberto <= 0:
            raise ConciliacaoInvalidaError(f'A receita de {r.imovel.nome} não tem mais saldo em aberto.')
        if not (inicio <= r.data_vencimento <= fim):
            raise ConciliacaoInvalidaError(
                f'A receita de {r.imovel.nome} (venc. {r.data_vencimento:%d/%m/%Y}) está fora da '
                f'janela de ±{JANELA_DIAS} dias da transação.'
            )


@transaction.atomic
def conciliar_com_receitas(transacao, receitas, marcar_comissoes=False, usuario=None):
    """
    Vincula a transação (crédito) às receitas e dá baixa via RecebimentoReceita.

    Todas as validações rodam no servidor. A distribuição usa o SALDO de cada
    receita, em ordem de vencimento; recebimentos anteriores nunca são
    sobrescritos. A soma dos valores atribuídos deve corresponder exatamente
    ao crédito (repasse líquido: bruto − comissões em aberto vinculadas).
    """
    transacao = TransacaoExtrato.objects.select_for_update().get(pk=transacao.pk)
    receitas = list(
        ReceitaAluguel.objects.select_for_update()
        .filter(pk__in=[r.pk for r in receitas])
        .order_by('data_vencimento', 'pk')
    )
    _validar_conciliacao(transacao, receitas)

    credito = transacao.valor_absoluto

    if marcar_comissoes:
        # Repasse líquido: inquilinos pagaram o bruto; a imobiliária reteve as
        # comissões. Regras estritas:
        # 1) todas as receitas devem ser da MESMA imobiliária principal (mesmo
        #    que a soma matemática coincidisse com outra combinação);
        # 2) receita sem imobiliária não participa de repasse líquido;
        # 3) só comissões automáticas, em aberto, vinculadas a cada receita;
        # 4) comissão >= saldo da própria receita é rejeitada (líquido <= 0);
        # 5) crédito = soma(saldos brutos) − soma(comissões), exato.
        imobiliarias = set()
        for r in receitas:
            imobiliaria = r.contrato.get_imobiliaria_principal() if r.contrato_id else None
            if imobiliaria is None:
                raise ConciliacaoInvalidaError(
                    f'Repasse líquido indisponível: a receita de {r.imovel.nome} não tem '
                    'imobiliária vinculada ao contrato.'
                )
            imobiliarias.add(imobiliaria.pk)
        if len(imobiliarias) > 1:
            raise ConciliacaoInvalidaError(
                'Repasse líquido indisponível: as receitas selecionadas pertencem a '
                'imobiliárias diferentes — um repasse é sempre de uma única imobiliária.'
            )
        imobiliaria_pk = next(iter(imobiliarias))

        comissoes = list(_comissoes_em_aberto(receitas).select_for_update())
        if not comissoes:
            raise ConciliacaoInvalidaError(
                'Repasse líquido indisponível: não há despesas de comissão em aberto '
                'vinculadas às receitas selecionadas.'
            )
        # Comissões antigas podem não ter fornecedor preenchido (dado legado) —
        # aceitas por compatibilidade. Quando o fornecedor ESTÁ preenchido, ele
        # precisa ser exatamente a imobiliária do contrato: uma comissão de
        # outra imobiliária nunca pode compor este repasse.
        for c in comissoes:
            if c.fornecedor_id is not None and c.fornecedor_id != imobiliaria_pk:
                raise ConciliacaoInvalidaError(
                    f'A comissão "{c.descricao}" tem fornecedor diferente da imobiliária '
                    'do repasse — verifique se ela pertence a este conjunto de receitas.'
                )

        comissao_por_receita_chk = {}
        for c in comissoes:
            comissao_por_receita_chk[c.receita_id] = (
                comissao_por_receita_chk.get(c.receita_id, Decimal('0')) + c.valor
            )
        for r in receitas:
            comissao_r = comissao_por_receita_chk.get(r.pk, Decimal('0'))
            if comissao_r >= r.saldo_em_aberto:
                raise ConciliacaoInvalidaError(
                    f'A comissão vinculada à receita de {r.imovel.nome} (R$ {comissao_r}) é '
                    f'maior ou igual ao saldo em aberto (R$ {r.saldo_em_aberto}) — o valor '
                    'líquido seria zero ou negativo. Revise a despesa de comissão.'
                )

        total_bruto = sum((r.saldo_em_aberto for r in receitas), Decimal('0.00'))
        total_comissoes = sum((c.valor for c in comissoes), Decimal('0.00'))
        esperado = total_bruto - total_comissoes
        # Igualdade EXATA — valores monetários já têm 2 casas decimais e Decimal
        # é exato, então não há razão legítima para tolerância aqui: aceitar
        # diferença silenciosamente deixaria centavos do crédito sem destino
        # (item 8). Se a imobiliária arredondou diferente, ajuste manualmente.
        if esperado != credito:
            raise ConciliacaoInvalidaError(
                f'Repasse líquido não confere: saldo bruto R$ {total_bruto} − comissões '
                f'R$ {total_comissoes} = R$ {esperado}, mas o crédito é R$ {credito}. '
                'A conciliação exige igualdade exata — ajuste a seleção ou trate manualmente.'
            )
        alocacoes = [(r, r.saldo_em_aberto) for r in receitas]
    else:
        restante = credito
        alocacoes = []
        for r in receitas:
            aloc = min(restante, r.saldo_em_aberto)
            if aloc <= 0:
                raise ConciliacaoInvalidaError(
                    f'O crédito não cobre nenhuma parte da receita de {r.imovel.nome} — '
                    'remova-a da seleção.'
                )
            alocacoes.append((r, aloc))
            restante -= aloc
        # Igualdade EXATA (item 8): nenhum centavo do crédito pode ficar sem
        # destino — a soma dos saldos selecionados deve fechar exatamente com
        # o crédito, não apenas "próximo o bastante".
        if restante != Decimal('0.00'):
            raise ConciliacaoInvalidaError(
                f'O crédito (R$ {credito}) é maior que o saldo total selecionado '
                f'(R$ {credito - restante}). Sobrariam R$ {restante} sem destino — '
                'a conciliação exige igualdade exata: inclua mais receitas ou trate manualmente.'
            )
        comissoes = []

    comissao_por_receita = {}
    for c in comissoes:
        comissao_por_receita[c.receita_id] = comissao_por_receita.get(c.receita_id, Decimal('0')) + c.valor

    for receita, valor_bruto in alocacoes:
        liquido = valor_bruto - comissao_por_receita.get(receita.pk, Decimal('0'))
        ConciliacaoReceita.objects.create(
            transacao=transacao, receita=receita,
            valor_atribuido=liquido if marcar_comissoes else valor_bruto,
        )
        # `receita` já está bloqueada por select_for_update() acima — reusa o
        # lock existente em vez de um segundo SELECT ... FOR UPDATE redundante.
        registrar_recebimento_bloqueada(
            receita,
            valor_bruto,
            transacao.data,
            usuario=usuario,
            origem='conciliacao',
            transacao_extrato=transacao,
        )

    if marcar_comissoes:
        for comissao in comissoes:
            comissao.status = 'paga'
            comissao.data_pagamento = transacao.data
            comissao.save(update_fields=['status', 'data_pagamento'])
            # Vínculo explícito: ao desfazer, SOMENTE estas despesas reabrem.
            ConciliacaoComissao.objects.create(
                transacao=transacao, despesa=comissao, valor=comissao.valor,
            )
        transacao.comissoes_marcadas = True

    transacao.status = 'conciliada'
    transacao.save(update_fields=['status', 'comissoes_marcadas'])
    return transacao


@transaction.atomic
def desfazer_conciliacao(transacao):
    """
    Desfaz a conciliação de um crédito: remove os recebimentos criados por
    ela (recalculando saldo/status das receitas), reabre as comissões que a
    conciliação marcou como pagas, remove os vínculos e devolve a transação
    para 'pendente'. Registrada no histórico da transação (simple-history).
    """
    transacao = TransacaoExtrato.objects.select_for_update().get(pk=transacao.pk)
    if transacao.status != 'conciliada':
        raise ConciliacaoInvalidaError('Só é possível desfazer transações conciliadas.')
    if transacao.despesa_id:
        raise ConciliacaoInvalidaError(
            'Desfazer automático está disponível apenas para créditos conciliados com '
            'receitas. Para débitos, exclua a despesa vinculada pelo Admin.'
        )

    if transacao.comissoes_marcadas:
        # Reabre EXCLUSIVAMENTE as comissões vinculadas por ConciliacaoComissao
        # a esta transação — nunca por heurística de receita/categoria/data, que
        # poderia reabrir uma comissão paga manualmente no mesmo dia.
        vinculos = list(transacao.itens_comissao.select_related('despesa').select_for_update())
        if vinculos:
            for vinculo in vinculos:
                despesa = vinculo.despesa
                despesa.status = 'prevista'
                despesa.data_pagamento = None
                despesa.save(update_fields=['status', 'data_pagamento'])
            transacao.itens_comissao.all().delete()
        else:
            # Conciliação antiga, anterior ao vínculo explícito: não é possível
            # identificar com segurança quais comissões ELA pagou — exige revisão
            # manual (não inventamos vínculos). Se as comissões suspeitas já
            # foram reabertas manualmente, o desfazer prossegue sem tocar nelas.
            receitas_ids_chk = list(transacao.itens_receita.values_list('receita_id', flat=True))
            suspeitas = Despesa.objects.filter(
                receita_id__in=receitas_ids_chk,
                origem_automatica=True,
                categoria='comissao_imobiliaria',
                status='paga',
                data_pagamento=transacao.data,
            )
            if suspeitas.exists():
                raise ConciliacaoInvalidaError(
                    'Esta conciliação (feita antes do vínculo explícito de comissões) marcou '
                    'comissões como pagas, e não é possível identificar com segurança quais '
                    'foram. Revisão manual necessária: reabra no Admin de Despesas as comissões '
                    'que pertencem a este repasse (status "Prevista", sem data de pagamento) e '
                    'então desfaça a conciliação novamente.'
                )

    # delete() individual para disparar o recálculo consolidado de cada receita
    for recebimento in list(RecebimentoReceita.objects.filter(transacao_extrato=transacao)):
        recebimento.delete()

    transacao.itens_receita.all().delete()
    transacao.comissoes_marcadas = False
    transacao.status = 'pendente'
    transacao.save(update_fields=['status', 'comissoes_marcadas'])
    return transacao


@transaction.atomic
def lancar_despesa(transacao, categoria, descricao, fornecedor=None, imovel=None):
    """Cria uma Despesa paga a partir de um débito do extrato e vincula à transação."""
    transacao = TransacaoExtrato.objects.select_for_update().get(pk=transacao.pk)
    if transacao.status != 'pendente':
        raise ConciliacaoInvalidaError('Esta transação já foi tratada (não está pendente).')
    if transacao.tipo != 'debito':
        raise ConciliacaoInvalidaError('Apenas débitos podem virar despesas.')

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
