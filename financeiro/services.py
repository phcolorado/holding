from calendar import monthrange
from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q

from .models import ReceitaAluguel, ReceitaAluguelItem, RecebimentoReceita, Despesa


def _registrar_recebimento_core(receita, valor, data_recebimento, usuario, origem, transacao_extrato, observacoes):
    """
    Regra central e única de criação de um RecebimentoReceita — usada pelos
    dois pontos de entrada abaixo. `receita` já deve estar com o lock que o
    chamador julgar adequado (ou sem lock nenhum, para leitura fora de uma
    transação de escrita — não é o caso dos dois pontos de entrada).
    """
    if receita.status == 'cancelado':
        raise ValidationError({
            'receita': 'Não é possível registrar recebimento em uma receita cancelada — reabra a receita primeiro.'
        })
    recebimento = RecebimentoReceita(
        receita=receita, valor=valor, data_recebimento=data_recebimento,
        transacao_extrato=transacao_extrato, origem=origem,
        observacoes=observacoes, criado_por=usuario,
    )
    # Autoria explícita no histórico (django-simple-history): criado_por já
    # registrava o autor como CAMPO do model, mas o autor do REGISTRO
    # histórico (history_user) ficava None quando o service roda fora de uma
    # requisição HTTP (script, shell, comando, conciliação chamada
    # diretamente) — o middleware não tem request.user para capturar. Setar
    # _history_user antes do save() (mesmo padrão de atualizar_recebimentos_
    # da_receita) preenche o autor no histórico da CRIAÇÃO. Sem usuário,
    # nada é setado (o middleware, quando houver, continua no controle).
    #
    # _receita_history_user propaga o mesmo autor à reconsolidação da
    # ReceitaAluguel (e a um eventual recebimento legado materializado) que
    # o save() abaixo dispara — ver RecebimentoReceita.save().
    if usuario is not None:
        recebimento._history_user = usuario
        recebimento._receita_history_user = usuario
    # full_clean() reaplica as regras do model (valor>0, valor<=saldo) sem
    # duplicá-las aqui — fonte única em RecebimentoReceita.clean().
    recebimento.full_clean()
    recebimento.save()
    return recebimento


@transaction.atomic
def registrar_recebimento(
    receita_id, valor, data_recebimento, usuario=None, origem='manual',
    transacao_extrato=None, observacoes='',
):
    """
    Service central e atômico para registrar um recebimento (RecebimentoReceita).

    Bloqueia a receita com select_for_update() dentro de uma transação,
    confirma o saldo e o status a partir do registro bloqueado (nunca de uma
    cópia potencialmente desatualizada), rejeita receita cancelada, valor
    zero/negativo e valor acima do saldo em aberto, cria o recebimento
    (que já reconsolida a receita) e retorna o recebimento criado.

    Levanta django.core.exceptions.ValidationError em qualquer rejeição —
    a transação inteira é revertida (nada é gravado) quando isso acontece.

    Use esta função sempre que o CHAMADOR ainda não tiver bloqueado a
    receita. Se a receita já estiver bloqueada (ex.: a conciliação bancária,
    que bloqueia várias receitas de uma vez com uma única query), use
    registrar_recebimento_bloqueada() para reaproveitar o lock existente em
    vez de emitir um SELECT ... FOR UPDATE redundante na mesma transação.
    """
    receita = ReceitaAluguel.objects.select_for_update().get(pk=receita_id)
    return _registrar_recebimento_core(
        receita, valor, data_recebimento, usuario, origem, transacao_extrato, observacoes
    )


def registrar_recebimento_bloqueada(
    receita, valor, data_recebimento, usuario=None, origem='manual',
    transacao_extrato=None, observacoes='',
):
    """
    Mesma regra central de registrar_recebimento(), mas reaproveitando uma
    instância de ReceitaAluguel que o CHAMADOR já bloqueou com
    select_for_update() dentro da própria transaction.atomic() (ex.:
    conciliar_com_receitas(), que bloqueia todas as receitas selecionadas de
    uma vez). Não abre uma nova transação nem um novo lock — o chamador é
    responsável por ambos.
    """
    return _registrar_recebimento_core(
        receita, valor, data_recebimento, usuario, origem, transacao_extrato, observacoes
    )


def _recebimento_e_protegido(rec):
    """Recebimento originado de conciliação bancária — imutável fora do fluxo de desfazer."""
    return rec.origem == 'conciliacao' or rec.transacao_extrato_id is not None


def validar_ids_e_protecao_recebimentos(receita, existentes, alterados_pks, excluidos_pks, tem_novos):
    """
    Validações de INTEGRIDADE do lote (não de valor) — compartilhadas entre a
    validação do formset do Admin (RecebimentoReceitaInlineFormSet.clean(),
    que barra o salvamento no ciclo correto do Django, antes de qualquer
    persistência) e o service transacional abaixo (que repete TODAS as
    validações dentro da transação — a do formset melhora a experiência e
    evita salvamento parcial, mas nunca a substitui).

    `existentes`: dict {pk: RecebimentoReceita} da receita.
    `alterados_pks`/`excluidos_pks`: sets de pks presentes no lote.
    `tem_novos`: True se o lote inclui alguma inclusão.
    """
    invalidos = (excluidos_pks | alterados_pks) - set(existentes)
    if invalidos:
        raise ValidationError(
            f'Recebimento(s) {sorted(invalidos)} não pertence(m) a esta receita.'
        )
    protegidos = {pk for pk, rec in existentes.items() if _recebimento_e_protegido(rec)}
    if (excluidos_pks | alterados_pks) & protegidos:
        raise ValidationError(
            'Recebimentos originados de conciliação bancária não podem ser editados ou '
            'excluídos por aqui — desfaça a conciliação correspondente para corrigi-los.'
        )
    if tem_novos and receita.status == 'cancelado':
        raise ValidationError(
            'Não é possível registrar recebimento em uma receita cancelada — reabra a receita primeiro.'
        )


def validar_total_final_recebimentos(receita, existentes, alterados_valores, excluidos_pks, novos_valores):
    """
    Calcula o total FINAL de todos os recebimentos da receita considerando
    existentes (menos excluídos, com os alterados já no valor novo) e novos
    EM CONJUNTO — nunca uma validação por linha isolada, que poderia aprovar
    duas novas linhas que, somadas, excedem o saldo mas cada uma
    isoladamente não excede. Levanta ValidationError na primeira violação;
    retorna o total final (Decimal) quando tudo é válido. Compartilhada pelo
    formset do Admin e pelo service (ver validar_ids_e_protecao_recebimentos).
    """
    total_final = Decimal('0.00')
    for pk, rec in existentes.items():
        if pk in excluidos_pks:
            continue
        valor = alterados_valores.get(pk, rec.valor)
        if valor is None or valor <= 0:
            raise ValidationError('O valor de cada recebimento deve ser maior que zero.')
        total_final += valor
    for valor in novos_valores:
        if valor is None or valor <= 0:
            raise ValidationError('O valor de cada recebimento deve ser maior que zero.')
        total_final += valor

    if total_final > receita.valor_total_devido:
        raise ValidationError(
            f'A soma dos recebimentos (R$ {total_final}) excederia o valor total devido '
            f'da receita (R$ {receita.valor_total_devido}) — ajuste os valores antes de salvar.'
        )
    return total_final


@transaction.atomic
def atualizar_recebimentos_da_receita(receita_id, novos=None, alterados=None, excluidos=None, usuario=None):
    """
    Aplica, como uma ÚNICA operação financeira atômica, inclusões, alterações
    e exclusões de RecebimentoReceita de uma mesma receita — usada pelo
    inline de "Recebimentos da Receita" no Admin de ReceitaAluguel.

    Bloqueia a receita com select_for_update() e repete as mesmas validações
    de validar_ids_e_protecao_recebimentos()/validar_total_final_
    recebimentos() já aplicadas por RecebimentoReceitaInlineFormSet.clean()
    (que barra o salvamento no ciclo do Django ANTES de qualquer
    persistência) — a validação do formset nunca substitui esta, que é quem
    realmente garante a consistência sob concorrência.

    novos: lista de dicts {'data_recebimento', 'valor', 'observacoes'}.
    alterados: lista de dicts {'pk', 'data_recebimento', 'valor', 'observacoes'}.
    excluidos: lista de pks (int) de recebimentos a excluir.

    A receita é reconsolidada (recalcular_recebimentos) UMA ÚNICA VEZ, ao
    final — cada gravação individual usa o flag _pular_recalculo_receita
    (RecebimentoReceita.save()/delete() continuam disparando os sinais do
    django-simple-history normalmente, só pulam a reconsolidação por linha).

    `usuario`, quando informado, é registrado como autor de CADA operação
    (criação, alteração, exclusão e eventual materialização de legado) no
    histórico (django-simple-history) via _history_user — necessário porque
    esta função roda tanto dentro de uma requisição HTTP (onde o middleware
    de histórico já capturaria request.user automaticamente) quanto fora
    dela (scripts, comandos, chamadas diretas), caso em que o middleware não
    tem como capturar o usuário via thread-local. Chamadas sem usuário
    (migrations, processos internos) continuam aceitas normalmente — nesse
    caso o histórico simplesmente não tem history_user, como sempre.
    """
    novos = novos or []
    alterados = alterados or []
    excluidos = list(excluidos or [])

    if len(excluidos) != len(set(excluidos)):
        raise ValidationError('Recebimento duplicado na lista de exclusões.')
    pks_alterados = [dados['pk'] for dados in alterados]
    if len(pks_alterados) != len(set(pks_alterados)):
        raise ValidationError('Recebimento duplicado na lista de alterações.')

    receita = ReceitaAluguel.objects.select_for_update().get(pk=receita_id)

    if novos:
        # Idempotente: só materializa se houver valor_recebido legado sem
        # recebimentos — evita perder um consolidado preexistente ao somar
        # o primeiro recebimento novo do lote.
        receita.garantir_recebimento_legado(usuario=usuario)

    existentes = {r.pk: r for r in receita.recebimentos.all()}
    excluidos_set = set(excluidos)
    alterados_por_pk = {dados['pk']: dados for dados in alterados}

    validar_ids_e_protecao_recebimentos(
        receita, existentes, set(alterados_por_pk), excluidos_set, bool(novos),
    )
    alterados_valores = {pk: dados['valor'] for pk, dados in alterados_por_pk.items()}
    novos_valores = [dados.get('valor') for dados in novos]
    validar_total_final_recebimentos(receita, existentes, alterados_valores, excluidos_set, novos_valores)

    # Validado — persiste. save()/delete() continuam disparando os sinais do
    # histórico (django-simple-history) normalmente; _pular_recalculo_receita
    # evita reconsolidar a receita a cada linha — a reconsolidação acontece
    # uma única vez, ao final.
    for pk in excluidos_set:
        rec = existentes[pk]
        rec._pular_recalculo_receita = True
        if usuario is not None:
            rec._history_user = usuario
        rec.delete()
    for dados in alterados:
        rec = existentes[dados['pk']]
        rec.data_recebimento = dados['data_recebimento']
        rec.valor = dados['valor']
        rec.observacoes = dados.get('observacoes', '')
        rec._pular_recalculo_receita = True
        if usuario is not None:
            rec._history_user = usuario
        rec.save(update_fields=['data_recebimento', 'valor', 'observacoes'])
    for dados in novos:
        novo = RecebimentoReceita(
            receita=receita,
            data_recebimento=dados['data_recebimento'],
            valor=dados['valor'],
            observacoes=dados.get('observacoes', ''),
            origem='manual',
            criado_por=usuario,
        )
        novo._pular_recalculo_receita = True
        if usuario is not None:
            novo._history_user = usuario
        novo.save()

    receita.recalcular_recebimentos()


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
    if encargo_aluguel:
        # Quando o encargo de aluguel existe mas não se aplica à competência
        # (ex.: carência via data_inicio_cobranca), nenhum aluguel é cobrado
        # no mês — logo não há base para a taxa de administração.
        if not encargo_aluguel.aplica_em(ano, mes):
            return None
        base = encargo_aluguel.valor
    else:
        base = contrato.valor_aluguel

    valor_taxa = (base * contrato.comissao_imobiliaria_percentual / Decimal('100')).quantize(Decimal('0.01'))
    imobiliaria = contrato.get_imobiliaria_principal()

    despesa, _criada = Despesa.objects.get_or_create(
        contrato=contrato,
        receita=receita,
        origem_automatica=True,
        categoria='comissao_imobiliaria',
        defaults={
            'imovel': contrato.imovel,
            'fornecedor': imobiliaria,
            'descricao': f'Taxa de administração — {contrato.imovel.nome} ({mes:02d}/{ano})',
            'competencia_mes': mes,
            'competencia_ano': ano,
            'data_vencimento': receita.data_vencimento,
            'valor': valor_taxa,
            'status': 'prevista',
        },
    )
    return despesa


@transaction.atomic
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


def serie_fluxo_caixa_12m(referencia=None, n_meses=12, incluir_despesas=True):
    """
    Série mensal de FLUXO DE CAIXA real, para o gráfico do dashboard —
    agrupada pela DATA em que o dinheiro efetivamente entrou ou saiu, não
    pela competência da cobrança:
    - receitas: RecebimentoReceita.data_recebimento (soma de valor).
      Inclui recebimentos de receitas canceladas DEPOIS de terem recebido
      algo — o dinheiro que entrou no caixa não desaparece com o
      cancelamento (só a cobrança do saldo restante é encerrada);
    - despesas: Despesa.data_pagamento, apenas status='paga' (efetivamente
      pagas — não conta despesa prevista/atrasada/cancelada).

    Distinta dos relatórios "por competência" (painéis, resumo por imóvel,
    relatório mensal/contábil), que continuam agrupando pelo mês de
    competência da cobrança — aqui o critério é sempre a data real do
    movimento de caixa (ex.: aluguel de janeiro pago em março aparece em
    março, não em janeiro).

    incluir_despesas=False omite a consulta de despesas pagas E retorna
    'pago'/'saldo' como None (não 0.0) — usado quando o chamador não tem
    permissão para ver despesas. None sinaliza ao template que a informação
    não deve ser exibida, nunca que despesas somaram zero: mostrar "0" ou
    calcular saldo=recebido induziria o leitor a acreditar que não houve
    gasto no mês, quando na verdade a área simplesmente não foi consultada.

    Retorna lista cronológica de dicts:
    {'label': 'mm/aaaa', 'recebido': float, 'pago': float|None, 'saldo': float|None}.
    """
    from django.db.models import Sum
    from patrimonio.indicadores import competencias_ultimas, filtro_competencias

    competencias = competencias_ultimas(n_meses, referencia)

    filtro_recebido = filtro_competencias(
        competencias, campo_ano='data_recebimento__year', campo_mes='data_recebimento__month'
    )
    recebidos = {
        (r['data_recebimento__year'], r['data_recebimento__month']): r['total']
        for r in RecebimentoReceita.objects.filter(filtro_recebido)
        .values('data_recebimento__year', 'data_recebimento__month')
        .annotate(total=Sum('valor'))
    }

    pagos = {}
    if incluir_despesas:
        filtro_pago = filtro_competencias(
            competencias, campo_ano='data_pagamento__year', campo_mes='data_pagamento__month'
        )
        pagos = {
            (d['data_pagamento__year'], d['data_pagamento__month']): d['total']
            for d in Despesa.objects.filter(filtro_pago, status='paga')
            .values('data_pagamento__year', 'data_pagamento__month')
            .annotate(total=Sum('valor'))
        }

    serie = []
    for ano, mes in competencias:
        recebido = float(recebidos.get((ano, mes)) or 0)
        pago = float(pagos.get((ano, mes)) or 0) if incluir_despesas else None
        saldo = (recebido - pago) if incluir_despesas else None
        serie.append({
            'label': f'{mes:02d}/{ano}',
            'recebido': recebido,
            'pago': pago,
            'saldo': saldo,
        })
    return serie


@transaction.atomic
def marcar_despesa_como_paga(despesa_id, data_pagamento, usuario=None):
    """
    Marca uma despesa como paga, mantendo status e data_pagamento SEMPRE
    sincronizados (item 10) — nunca defina um sem o outro diretamente.
    """
    despesa = Despesa.objects.select_for_update().get(pk=despesa_id)
    if not data_pagamento:
        raise ValidationError({'data_pagamento': 'Informe a data de pagamento.'})
    despesa.status = 'paga'
    despesa.data_pagamento = data_pagamento
    despesa.full_clean()
    despesa.save(update_fields=['status', 'data_pagamento'])
    return despesa


@transaction.atomic
def reabrir_despesa(despesa_id):
    """
    Reabre uma despesa (paga ou cancelada) — limpa data_pagamento e
    recalcula o status pela data de vencimento (prevista/atrasada), nunca
    deixando um status não-pago com data de pagamento preenchida.
    """
    from django.utils import timezone

    despesa = Despesa.objects.select_for_update().get(pk=despesa_id)
    hoje = timezone.localdate()
    despesa.data_pagamento = None
    despesa.status = 'atrasada' if despesa.data_vencimento < hoje else 'prevista'
    despesa.full_clean()
    despesa.save(update_fields=['status', 'data_pagamento'])
    return despesa


@transaction.atomic
def cancelar_despesa(despesa_id):
    """Cancela uma despesa — limpa data_pagamento (cancelada nunca tem data)."""
    despesa = Despesa.objects.select_for_update().get(pk=despesa_id)
    despesa.status = 'cancelada'
    despesa.data_pagamento = None
    despesa.full_clean()
    despesa.save(update_fields=['status', 'data_pagamento'])
    return despesa


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
