from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Q, Sum
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone

from .models import ReceitaAluguel, Despesa, FechamentoMensal, valor_total_devido_expr
from .dimob import linhas_locacao
from .forms import RegistrarRecebimentoForm, EditarEncargosForm
from .exports import (
    exportar_imoveis_csv, exportar_imoveis_xlsx,
    exportar_contratos_csv, exportar_contratos_xlsx,
    exportar_locatarios_csv, exportar_locatarios_xlsx,
    exportar_dimob_xlsx,
    exportar_receitas_csv, exportar_receitas_xlsx,
    exportar_despesas_csv, exportar_despesas_xlsx,
    exportar_inadimplencia_csv, exportar_inadimplencia_xlsx,
    exportar_relatorio_mensal_xlsx,
    exportar_relatorio_contabilidade_xlsx,
)
from core.utils import anos_para_filtro, int_param, mes_ano_da_request, pk_param
from patrimonio.models import Imovel, Contrato, Pessoa, imobiliarias_queryset

ITENS_POR_PAGINA = 50


def _filtros_periodo(request):
    mes, ano = mes_ano_da_request(request)
    imovel_id = pk_param(request.GET.get('imovel'))
    status = request.GET.get('status', '')
    categoria = request.GET.get('categoria', '')
    return mes, ano, imovel_id, status, categoria


def _paginar(request, queryset):
    paginator = Paginator(queryset, ITENS_POR_PAGINA)
    numero = int_param(request.GET.get('page'), 1, minimo=1)
    return paginator.get_page(numero)


def _query_string_sem_page(request):
    """Query string atual sem o parâmetro page — usada nos links de paginação."""
    params = request.GET.copy()
    params.pop('page', None)
    return params.urlencode()


def _exigir_permissao(request, perm):
    if not request.user.has_perm(perm):
        raise PermissionDenied


@login_required
@permission_required('financeiro.view_receitaaluguel', raise_exception=True)
def receitas_list(request):
    mes, ano, imovel_id, status, _ = _filtros_periodo(request)

    receitas = ReceitaAluguel.objects.select_related('imovel', 'contrato__locatario').filter(
        competencia_mes=mes, competencia_ano=ano
    )
    if imovel_id:
        receitas = receitas.filter(imovel_id=imovel_id)
    if status:
        receitas = receitas.filter(status=status)

    # Valor EXIGÍVEL do período filtrado (itens 7-8): previsto + multa +
    # juros − desconto (nunca só valor_previsto isolado), excluindo
    # canceladas — a cobrança delas foi encerrada, não representam mais
    # valor a exigir. Soma sobre o queryset completo (antes da paginação).
    total_exigivel = receitas.exclude(status='cancelado').aggregate(
        total=Sum(valor_total_devido_expr())
    )['total'] or Decimal('0.00')

    pagina = _paginar(request, receitas)

    context = {
        'receitas': pagina,
        'pagina': pagina,
        'total_exigivel': total_exigivel,
        'query_string': _query_string_sem_page(request),
        'imoveis': Imovel.objects.all(),
        'mes_atual': mes,
        'ano_atual': ano,
        'status_atual': status,
        'imovel_atual': imovel_id,
        'status_choices': ReceitaAluguel.STATUS_CHOICES,
        'meses': ReceitaAluguel.MESES,
        'anos': anos_para_filtro(),
    }
    return render(request, 'financeiro/receitas_list.html', context)


@login_required
@permission_required('financeiro.view_despesa', raise_exception=True)
def despesas_list(request):
    mes, ano, imovel_id, status, categoria = _filtros_periodo(request)

    despesas = Despesa.objects.select_related('imovel', 'fornecedor').filter(
        competencia_mes=mes, competencia_ano=ano
    )
    if imovel_id:
        despesas = despesas.filter(imovel_id=imovel_id)
    if status:
        despesas = despesas.filter(status=status)
    if categoria:
        despesas = despesas.filter(categoria=categoria)

    pagina = _paginar(request, despesas)

    context = {
        'despesas': pagina,
        'pagina': pagina,
        'query_string': _query_string_sem_page(request),
        'imoveis': Imovel.objects.all(),
        'mes_atual': mes,
        'ano_atual': ano,
        'status_atual': status,
        'imovel_atual': imovel_id,
        'categoria_atual': categoria,
        'status_choices': Despesa.STATUS_CHOICES,
        'categoria_choices': Despesa.CATEGORIA_CHOICES,
        'meses': Despesa.MESES,
        'anos': anos_para_filtro(),
    }
    return render(request, 'financeiro/despesas_list.html', context)


@login_required
def paineis(request):
    """
    Painéis gráficos: ocupação, receitas × despesas, inadimplência e
    categorias. Cada painel só é consultado e exibido se o usuário tiver a
    permissão de leitura da área correspondente:
    - ocupação: patrimonio.view_imovel;
    - inadimplência e a parcela de RECEITAS do painel "por imóvel": financeiro.view_receitaaluguel;
    - despesas por categoria e a parcela de DESPESAS do painel "por imóvel": financeiro.view_despesa.
    Acessar a tela exige pelo menos uma dessas permissões; sem nenhuma, 403.
    """
    from datetime import date as date_cls
    from .paineis import (
        serie_ocupacao_mensal, serie_receita_despesa_por_imovel,
        serie_inadimplencia_mensal, serie_despesas_por_categoria,
    )

    pode = request.user.has_perm
    ve_imoveis = pode('patrimonio.view_imovel')
    ve_receitas = pode('financeiro.view_receitaaluguel')
    ve_despesas = pode('financeiro.view_despesa')
    if not (ve_imoveis or ve_receitas or ve_despesas):
        raise PermissionDenied

    mes, ano = mes_ano_da_request(request)
    janela = int_param(request.GET.get('janela'), 12)
    if janela not in (6, 12, 24):
        janela = 12
    referencia = date_cls(ano, mes, 1)

    context = {
        'ocupacao': serie_ocupacao_mensal(janela, referencia) if ve_imoveis else None,
        'por_imovel': (
            serie_receita_despesa_por_imovel(
                janela, referencia, incluir_receitas=ve_receitas, incluir_despesas=ve_despesas,
            ) if (ve_receitas or ve_despesas) else None
        ),
        'inadimplencia': serie_inadimplencia_mensal(janela, referencia) if ve_receitas else None,
        'por_categoria': serie_despesas_por_categoria(janela, referencia) if ve_despesas else None,
        've_imoveis': ve_imoveis,
        've_receitas': ve_receitas,
        've_despesas': ve_despesas,
        'mes_atual': mes,
        'ano_atual': ano,
        'janela_atual': janela,
        'meses': ReceitaAluguel.MESES,
        'anos': anos_para_filtro(),
        'janelas': (6, 12, 24),
    }
    return render(request, 'financeiro/paineis.html', context)


@login_required
def relatorios(request):
    """
    Tela de relatórios/exportações: cada card só é exibido — e seus filtros
    (imóveis/imobiliárias/categorias) só são consultados — quando o usuário
    tem a permissão da área correspondente. O relatório mensal completo
    exige receitas+despesas; o contábil completo exige também documentos
    (mesmas permissões cobradas pelas views de exportação abaixo).
    """
    hoje = timezone.localdate()
    pode = request.user.has_perm
    ve_imoveis = pode('patrimonio.view_imovel')
    ve_contratos = pode('patrimonio.view_contrato')
    ve_receitas = pode('financeiro.view_receitaaluguel')
    ve_despesas = pode('financeiro.view_despesa')
    ve_documentos = pode('documentos.view_documento')

    if not any([ve_imoveis, ve_contratos, ve_receitas, ve_despesas]):
        raise PermissionDenied

    precisa_filtro_imovel = ve_contratos or ve_receitas or ve_despesas

    context = {
        'imoveis': Imovel.objects.all() if precisa_filtro_imovel else Imovel.objects.none(),
        # Item 6.4: imobiliarias_queryset() já executa as queries para
        # montar o conjunto de ids ANTES de retornar o queryset — chamá-la
        # só para descartar o resultado com .none() desperdiçaria essas
        # consultas mesmo sem permissão. Pessoa.objects.none() não consulta nada.
        'imobiliarias': imobiliarias_queryset() if ve_contratos else Pessoa.objects.none(),
        've_imoveis': ve_imoveis,
        've_contratos': ve_contratos,
        've_receitas': ve_receitas,
        've_despesas': ve_despesas,
        've_documentos': ve_documentos,
        # DIMOB cruza contratos/locatários (patrimônio) com valores
        # recebidos (financeiro) — mesmas permissões cobradas por export_dimob.
        've_dimob': ve_contratos and ve_receitas,
        'anos_dimob': anos_para_filtro(),
        'ano_dimob_padrao': hoje.year - 1,
        've_relatorio_mensal': ve_receitas and ve_despesas,
        've_relatorio_contabil': ve_receitas and ve_despesas and ve_documentos,
        'mes_atual': hoje.month,
        'ano_atual': hoje.year,
        'meses': ReceitaAluguel.MESES,
        'anos': anos_para_filtro(),
        'status_receita': ReceitaAluguel.STATUS_CHOICES,
        'status_despesa': Despesa.STATUS_CHOICES,
        'categoria_despesa': Despesa.CATEGORIA_CHOICES,
    }
    return render(request, 'financeiro/relatorios.html', context)


# ─── exportações ───────────────────────────────────────────────────────────────

@login_required
@permission_required('patrimonio.view_imovel', raise_exception=True)
def export_imoveis(request, formato):
    status = request.GET.get('status', '')
    imoveis = Imovel.objects.filter(status=status) if status else Imovel.objects.all()
    if formato == 'csv':
        return exportar_imoveis_csv(request, imoveis)
    return exportar_imoveis_xlsx(request, imoveis)


@login_required
@permission_required('patrimonio.view_contrato', raise_exception=True)
def export_contratos(request, formato):
    status = request.GET.get('status', '')
    imovel_id = pk_param(request.GET.get('imovel'))
    imobiliaria_id = pk_param(request.GET.get('imobiliaria'))
    contratos = Contrato.objects.select_related('imovel', 'locatario', 'fiador', 'imobiliaria')
    if status:
        contratos = contratos.filter(status=status)
    else:
        contratos = contratos.filter(status='ativo')
    if imovel_id:
        contratos = contratos.filter(imovel_id=imovel_id)
    if imobiliaria_id:
        contratos = contratos.filter(
            Q(imobiliaria_id=imobiliaria_id) | Q(partes__papel='imobiliaria', partes__pessoa_id=imobiliaria_id)
        ).distinct()
    if formato == 'csv':
        return exportar_contratos_csv(request, contratos)
    return exportar_contratos_xlsx(request, contratos)


@login_required
@permission_required('patrimonio.view_contrato', raise_exception=True)
def export_locatarios(request, formato):
    """
    Relatório para a contabilidade: cada imóvel ALUGADO com o nome e o
    CPF/CNPJ do(s) locatário(s). Uma linha por locatário.

    "Imóvel alugado" = imóvel com contrato ATIVO — mesma convenção do
    export de contratos (o contrato é a fonte de quem é o locatário; o
    campo Imovel.status é apenas reflexo dele). Exige patrimonio.view_
    contrato: o relatório expõe dados de contrato e documento das partes.
    """
    imovel_id = pk_param(request.GET.get('imovel'))
    contratos = (
        Contrato.objects
        .filter(status='ativo')
        .select_related('imovel', 'locatario')
        .prefetch_related('partes__pessoa')
        .order_by('imovel__nome')
    )
    if imovel_id:
        contratos = contratos.filter(imovel_id=imovel_id)
    if formato == 'csv':
        return exportar_locatarios_csv(request, contratos)
    return exportar_locatarios_xlsx(request, contratos)


@login_required
@permission_required('patrimonio.view_contrato', raise_exception=True)
@permission_required('financeiro.view_receitaaluguel', raise_exception=True)
def export_dimob(request):
    """
    Planilha de apoio da DIMOB (ficha de locação) do ano-calendário.

    Exige as DUAS permissões: o relatório cruza dados de contrato/locatário
    (patrimônio) com os valores recebidos mês a mês (financeiro).
    """
    ano = int_param(request.GET.get('ano'), timezone.localdate().year - 1, 1990, 2200)
    linhas = linhas_locacao(ano)
    return exportar_dimob_xlsx(request, ano, linhas)


@login_required
@permission_required('financeiro.view_receitaaluguel', raise_exception=True)
def export_receitas(request, formato):
    mes, ano, imovel_id, status, _ = _filtros_periodo(request)
    receitas = ReceitaAluguel.objects.select_related('imovel', 'contrato__locatario').filter(
        competencia_mes=mes, competencia_ano=ano
    )
    if imovel_id:
        receitas = receitas.filter(imovel_id=imovel_id)
    if status:
        receitas = receitas.filter(status=status)
    if formato == 'csv':
        return exportar_receitas_csv(request, receitas)
    return exportar_receitas_xlsx(request, receitas)


@login_required
@permission_required('financeiro.view_despesa', raise_exception=True)
def export_despesas(request, formato):
    mes, ano, imovel_id, status, categoria = _filtros_periodo(request)
    despesas = Despesa.objects.select_related('imovel', 'fornecedor').filter(
        competencia_mes=mes, competencia_ano=ano
    )
    if imovel_id:
        despesas = despesas.filter(imovel_id=imovel_id)
    if status:
        despesas = despesas.filter(status=status)
    if categoria:
        despesas = despesas.filter(categoria=categoria)
    if formato == 'csv':
        return exportar_despesas_csv(request, despesas)
    return exportar_despesas_xlsx(request, despesas)


@login_required
@permission_required('financeiro.view_receitaaluguel', raise_exception=True)
def export_inadimplencia(request, formato):
    from .models import receitas_inadimplentes_qs
    imovel_id = pk_param(request.GET.get('imovel'))
    receitas = receitas_inadimplentes_qs().select_related('imovel', 'contrato__locatario')
    if imovel_id:
        receitas = receitas.filter(imovel_id=imovel_id)
    if formato == 'csv':
        return exportar_inadimplencia_csv(request, receitas)
    return exportar_inadimplencia_xlsx(request, receitas)


@login_required
@permission_required(
    ('financeiro.view_receitaaluguel', 'financeiro.view_despesa'), raise_exception=True,
)
def export_relatorio_mensal(request, formato):
    """Relatório mensal completo (receitas + despesas + resumo por imóvel): exige as duas permissões."""
    mes, ano, _, _, _ = _filtros_periodo(request)
    return exportar_relatorio_mensal_xlsx(request, mes, ano)


@login_required
@permission_required(
    ('financeiro.view_receitaaluguel', 'financeiro.view_despesa', 'documentos.view_documento'),
    raise_exception=True,
)
def export_relatorio_contabilidade(request):
    """Relatório contábil completo: exige receitas + despesas + documentos."""
    mes, ano = mes_ano_da_request(request)
    return exportar_relatorio_contabilidade_xlsx(request, mes, ano)


@login_required
@permission_required('financeiro.view_receitaaluguel', raise_exception=True)
def baixa_receitas_mes_view(request):
    """
    GET: lista as receitas do mês para conferência.
    POST: atualiza individualmente (marcar_recebida ou editar).
    """
    hoje = timezone.localdate()
    mes, ano = mes_ano_da_request(request)
    imovel_id = pk_param(request.GET.get('imovel') or request.POST.get('imovel'))

    if request.method == 'POST':
        action = request.POST.get('action', '')
        receita_id = pk_param(request.POST.get('receita_id'))

        if action == 'gerar_receitas':
            _exigir_permissao(request, 'financeiro.add_receitaaluguel')
            from .services import gerar_receitas_mes
            criadas, existiam = gerar_receitas_mes(mes, ano)
            if criadas:
                messages.success(request, f'{criadas} receita(s) gerada(s) para {mes:02d}/{ano}.')
            if existiam:
                messages.info(request, f'{existiam} receita(s) já existiam.')
            if not criadas and not existiam:
                messages.warning(request, f'Nenhum contrato ativo para {mes:02d}/{ano}.')

        elif receita_id:
            _exigir_permissao(request, 'financeiro.change_receitaaluguel')
            receita = get_object_or_404(ReceitaAluguel, pk=receita_id)

            if action == 'marcar_recebida':
                from django.core.exceptions import ValidationError
                from .services import registrar_recebimento
                saldo = receita.saldo_em_aberto
                if saldo > 0:
                    # Registra o recebimento do saldo — soma-se aos anteriores
                    # (parciais) e reconsolida valor_recebido/status. Serviço
                    # atômico: bloqueia a receita e reconfirma o saldo dentro
                    # da transação antes de gravar.
                    try:
                        registrar_recebimento(
                            receita.pk, saldo, hoje, usuario=request.user, origem='manual',
                        )
                        messages.success(request, f'{receita.imovel.nome} marcado como recebido.')
                    except ValidationError as exc:
                        erros = '; '.join(e for lista in exc.message_dict.values() for e in lista)
                        messages.error(request, f'{receita.imovel.nome}: {erros}')
                else:
                    messages.info(request, f'{receita.imovel.nome} já está sem saldo em aberto.')

            elif action == 'registrar_recebimento':
                from django.core.exceptions import ValidationError
                from .services import registrar_recebimento
                form = RegistrarRecebimentoForm(request.POST)
                if form.is_valid():
                    try:
                        recebimento = registrar_recebimento(
                            receita.pk,
                            form.cleaned_data['valor'],
                            form.cleaned_data['data_recebimento'],
                            usuario=request.user,
                            origem='manual',
                            observacoes=form.cleaned_data['observacoes'],
                        )
                        messages.success(
                            request,
                            f'{receita.imovel.nome}: recebimento de R$ {recebimento.valor} registrado.'
                        )
                    except ValidationError as exc:
                        erros = '; '.join(e for lista in exc.message_dict.values() for e in lista)
                        messages.error(request, f'{receita.imovel.nome}: {erros}')
                else:
                    erros = '; '.join(e for lista in form.errors.values() for e in lista)
                    messages.error(request, f'{receita.imovel.nome}: {erros}')

            elif action == 'editar_encargos':
                from django.core.exceptions import ValidationError
                form = EditarEncargosForm(request.POST)
                if form.is_valid():
                    form.aplicar(receita)
                    try:
                        receita.full_clean()  # impede desconto que torne o total devido negativo
                        receita.save()
                        # multa/juros/desconto mudam o saldo total → reconsolida o status
                        receita.recalcular_recebimentos()
                        messages.success(request, f'{receita.imovel.nome} atualizado.')
                    except ValidationError as exc:
                        erros = '; '.join(e for lista in exc.message_dict.values() for e in lista)
                        messages.error(request, f'{receita.imovel.nome} não foi atualizado: {erros}')
                else:
                    erros = '; '.join(e for lista in form.errors.values() for e in lista)
                    messages.error(request, f'{receita.imovel.nome} não foi atualizado: {erros}')

            elif action == 'cancelar':
                if receita.cancelar():
                    messages.success(
                        request,
                        f'{receita.imovel.nome}: receita cancelada (cobrança encerrada — '
                        'os recebimentos já registrados foram preservados).'
                    )
                else:
                    messages.info(request, f'{receita.imovel.nome} já estava cancelada.')

            elif action == 'reabrir':
                if receita.reabrir():
                    messages.success(
                        request,
                        f'{receita.imovel.nome}: receita reaberta — status recalculado '
                        f'({receita.get_status_display()}).'
                    )
                else:
                    messages.info(request, f'{receita.imovel.nome} não estava cancelada.')

        url = f"{reverse('baixa_receitas_mes')}?mes={mes}&ano={ano}"
        if imovel_id:
            url += f'&imovel={imovel_id}'
        return HttpResponseRedirect(url)

    receitas = (
        ReceitaAluguel.objects
        .filter(competencia_mes=mes, competencia_ano=ano)
        .select_related('imovel', 'contrato__locatario')
        .prefetch_related('itens')
        .order_by('imovel__nome')
    )
    if imovel_id:
        receitas = receitas.filter(imovel_id=imovel_id)

    receitas = list(receitas)
    for r in receitas:
        r.sugestao = r.calcular_multa_juros(hoje) if r.esta_atrasada else None

    context = {
        'receitas': receitas,
        'mes_atual': mes,
        'ano_atual': ano,
        'imovel_atual': imovel_id,
        'imoveis': Imovel.objects.all(),
        'meses': ReceitaAluguel.MESES,
        'anos': anos_para_filtro(),
        'hoje': hoje,
        'status_choices': ReceitaAluguel.STATUS_CHOICES,
    }
    return render(request, 'financeiro/baixa_receitas.html', context)


@login_required
def checklist_mensal_view(request):
    """
    GET: exibe checklist de fechamento mensal com indicadores de status.
    POST action=marcar_enviado: registra envio à contabilidade no FechamentoMensal.

    Cada etapa só é montada — e a consulta correspondente só é feita — quando
    o usuário tem a permissão de leitura da área envolvida:
    receitas/recebimentos/inadimplência → financeiro.view_receitaaluguel;
    despesas pagas → financeiro.view_despesa;
    documentos p/ contabilidade e validade → documentos.view_documento;
    documentos obrigatórios → documentos.view_documentoobrigatorio;
    reajustes → patrimonio.view_contrato;
    conciliação → conciliacao.view_extratoimportado;
    fechamento → financeiro.view_fechamentomensal.
    Acessar a tela exige pelo menos uma dessas permissões; sem nenhuma, 403.
    """
    from .models import receitas_inadimplentes_qs

    hoje = timezone.localdate()
    mes, ano = mes_ano_da_request(request)

    pode = request.user.has_perm
    ve_receitas = pode('financeiro.view_receitaaluguel')
    ve_despesas = pode('financeiro.view_despesa')
    ve_documentos = pode('documentos.view_documento')
    ve_doc_obrigatorios = pode('documentos.view_documentoobrigatorio')
    ve_contratos = pode('patrimonio.view_contrato')
    ve_conciliacao = pode('conciliacao.view_extratoimportado')
    ve_fechamento = pode('financeiro.view_fechamentomensal')

    if not any([
        ve_receitas, ve_despesas, ve_documentos, ve_doc_obrigatorios,
        ve_contratos, ve_conciliacao, ve_fechamento,
    ]):
        raise PermissionDenied

    if request.method == 'POST':
        action = request.POST.get('action', '')
        if action == 'marcar_enviado':
            _exigir_permissao(request, 'financeiro.change_fechamentomensal')
            fechamento, _ = FechamentoMensal.objects.get_or_create(mes=mes, ano=ano)
            fechamento.enviado_contabilidade = True
            fechamento.data_envio_contabilidade = hoje
            if not fechamento.data_fechamento:
                fechamento.data_fechamento = hoje
            fechamento.save()
            messages.success(request, f'Mês {mes:02d}/{ano} marcado como enviado à contabilidade.')
        return HttpResponseRedirect(f"{reverse('checklist_mensal')}?mes={mes}&ano={ano}")

    checklist = []

    if ve_receitas:
        receitas_mes = ReceitaAluguel.objects.filter(competencia_mes=mes, competencia_ano=ano)
        total_receitas = receitas_mes.count()
        # Regra centralizada de SALDO (não de status): pagamento parcial NÃO
        # conta como confirmado — permanece pendente até o saldo zerar.
        receitas_recebidas = receitas_mes.quitadas().exclude(status='cancelado').count()
        receitas_pendentes = receitas_mes.em_aberto().count()
        receitas_canceladas = receitas_mes.filter(status='cancelado').count()
        inadimplentes = receitas_inadimplentes_qs().count()

        checklist.append({
            'item': 'Receitas geradas',
            'ok': total_receitas > 0,
            'detalhe': f'{total_receitas} receita(s) cadastrada(s)' if total_receitas else 'Nenhuma receita gerada ainda',
            'link': reverse('gerar_receitas_mes') + f'?mes={mes}&ano={ano}',
        })
        checklist.append({
            'item': 'Recebimentos confirmados',
            'ok': total_receitas > 0 and receitas_pendentes == 0,
            # Discrimina recebidas/canceladas/em aberto em vez de "0/1 recebida" —
            # uma única receita CANCELADA sem pendência não deve soar como
            # "0 de 1 recebidas" (não há nada pendente a receber dela).
            'detalhe': (
                f'{receitas_recebidas} recebida(s), {receitas_canceladas} cancelada(s), '
                f'{receitas_pendentes} em aberto'
            ) if total_receitas else '—',
            'link': reverse('baixa_receitas_mes') + f'?mes={mes}&ano={ano}',
        })
        checklist.append({
            'item': 'Inadimplência em dia',
            'ok': inadimplentes == 0,
            'detalhe': f'{inadimplentes} receita(s) inadimplente(s) em aberto' if inadimplentes else 'Sem inadimplência em aberto',
            'link': None,
        })
    else:
        total_receitas = receitas_recebidas = receitas_pendentes = receitas_canceladas = None
        inadimplentes = None

    if ve_contratos:
        reajustes_pendentes = Contrato.objects.filter(
            status='ativo', data_proximo_reajuste__isnull=False, data_proximo_reajuste__lte=hoje,
        ).count()
        checklist.append({
            'item': 'Reajustes em dia',
            'ok': reajustes_pendentes == 0,
            'detalhe': (
                f'{reajustes_pendentes} contrato(s) com reajuste pendente'
                if reajustes_pendentes else 'Nenhum reajuste pendente'
            ),
            'link': reverse('contrato_list') + '?reajuste_pendente=1',
        })
    else:
        reajustes_pendentes = None

    if ve_despesas:
        despesas_mes = Despesa.objects.filter(competencia_mes=mes, competencia_ano=ano)
        total_despesas = despesas_mes.count()
        despesas_pagas = despesas_mes.filter(status='paga').count()
        checklist.append({
            'item': 'Despesas pagas',
            'ok': total_despesas == 0 or despesas_pagas == total_despesas,
            'detalhe': f'{despesas_pagas}/{total_despesas} paga(s)' if total_despesas else 'Sem despesas no período',
            'link': reverse('despesas_list') + f'?mes={mes}&ano={ano}',
        })
    else:
        total_despesas = despesas_pagas = None

    if ve_documentos:
        from documentos.models import Documento, documentos_vencendo_qs
        docs_pendentes_cnt = Documento.objects.filter(
            tipo__in=Documento.TIPOS_CONTABILIDADE, enviado_contabilidade=False
        ).count()
        docs_validade_cnt = documentos_vencendo_qs().count()
        checklist.append({
            'item': 'Documentos enviados à contabilidade',
            'ok': docs_pendentes_cnt == 0,
            'detalhe': f'{docs_pendentes_cnt} documento(s) pendente(s)' if docs_pendentes_cnt else 'Todos enviados',
            'link': reverse('documento_list') + '?pendente=1',
        })
        checklist.append({
            'item': 'Validade dos documentos em dia',
            'ok': docs_validade_cnt == 0,
            'detalhe': (
                f'{docs_validade_cnt} documento(s) vencido(s) ou vencendo em 30 dias'
                if docs_validade_cnt else 'Nenhum documento com validade vencendo'
            ),
            'link': reverse('documento_list') + '?vencendo=1',
        })
    else:
        docs_pendentes_cnt = docs_validade_cnt = None

    if ve_doc_obrigatorios:
        from documentos.models import DocumentoObrigatorio
        docs_obrigatorios_pendentes = DocumentoObrigatorio.objects.filter(
            obrigatorio=True, documento__isnull=True
        ).count()
        # Link para o Admin: só faz sentido oferecer para quem pode acessá-lo.
        link_obrigatorios = (
            reverse('admin:documentos_documentoobrigatorio_changelist')
            if request.user.is_staff else None
        )
        checklist.append({
            'item': 'Documentos obrigatórios revisados',
            'ok': docs_obrigatorios_pendentes == 0,
            'detalhe': (
                f'{docs_obrigatorios_pendentes} documento(s) obrigatório(s) pendente(s)'
                if docs_obrigatorios_pendentes
                else 'Todos os documentos obrigatórios vinculados'
            ),
            'link': link_obrigatorios,
        })
    else:
        docs_obrigatorios_pendentes = None

    if ve_conciliacao:
        from conciliacao.services import transacoes_pendentes_qs
        extrato_pendentes = transacoes_pendentes_qs().filter(
            data__year=ano, data__month=mes
        ).count()
        checklist.append({
            'item': 'Extrato bancário conciliado',
            'ok': extrato_pendentes == 0,
            'detalhe': (
                f'{extrato_pendentes} transação(ões) do extrato pendente(s) no mês'
                if extrato_pendentes else 'Nenhuma transação de extrato pendente no mês'
            ),
            'link': reverse('extrato_list'),
        })
    else:
        extrato_pendentes = None

    # Relatório contábil completo exige receitas + despesas + documentos
    # (mesma exigência da view de exportação) — sem as três, não oferece o link.
    if ve_receitas and ve_despesas and ve_documentos:
        checklist.append({
            'item': 'Relatório contábil exportado',
            'ok': False,
            'informativa': True,
            'detalhe': 'Baixe o relatório contábil após revisar receitas, despesas e documentos',
            'link': reverse('export_relatorio_contabilidade') + f'?mes={mes}&ano={ano}',
        })

    if ve_fechamento:
        try:
            fechamento = FechamentoMensal.objects.get(mes=mes, ano=ano)
        except FechamentoMensal.DoesNotExist:
            fechamento = None
        checklist.append({
            'item': 'Fechamento registrado e enviado',
            'ok': fechamento is not None and fechamento.enviado_contabilidade,
            'detalhe': (
                f'Enviado em {fechamento.data_envio_contabilidade:%d/%m/%Y}'
                if fechamento and fechamento.enviado_contabilidade
                else 'Pendente'
            ),
            'link': None,
        })
    else:
        fechamento = None

    nome_mes = dict(ReceitaAluguel.MESES).get(mes, str(mes))

    context = {
        'mes_atual': mes,
        'ano_atual': ano,
        'nome_mes': nome_mes,
        'meses': ReceitaAluguel.MESES,
        'anos': anos_para_filtro(),
        'checklist': checklist,
        'fechamento': fechamento,
        've_receitas': ve_receitas,
        've_despesas': ve_despesas,
        've_documentos': ve_documentos,
        've_doc_obrigatorios': ve_doc_obrigatorios,
        've_contratos': ve_contratos,
        've_conciliacao': ve_conciliacao,
        've_fechamento': ve_fechamento,
        'total_receitas': total_receitas,
        'receitas_recebidas': receitas_recebidas,
        'receitas_pendentes': receitas_pendentes,
        'receitas_canceladas': receitas_canceladas,
        'total_despesas': total_despesas,
        'despesas_pagas': despesas_pagas,
        'inadimplentes': inadimplentes,
        'docs_pendentes_cnt': docs_pendentes_cnt,
        'docs_obrigatorios_pendentes': docs_obrigatorios_pendentes,
        'docs_validade_cnt': docs_validade_cnt,
        'extrato_pendentes': extrato_pendentes,
        'reajustes_pendentes': reajustes_pendentes,
    }
    return render(request, 'financeiro/checklist_mensal.html', context)


@login_required
@permission_required('financeiro.view_receitaaluguel', raise_exception=True)
def gerar_receitas_mes_view(request):
    """
    GET: exibe formulário de confirmação com seleção de mês/ano.
    POST: executa a geração e redireciona para a lista de receitas do período.
    """
    from .services import gerar_receitas_mes, contratos_para_geracao_mes

    mes, ano = mes_ano_da_request(request)

    if request.method == 'POST':
        _exigir_permissao(request, 'financeiro.add_receitaaluguel')
        criadas, existiam = gerar_receitas_mes(mes, ano)

        if criadas:
            messages.success(request, f'{criadas} receita(s) gerada(s) para {mes:02d}/{ano}.')
        if existiam:
            messages.warning(request, f'{existiam} receita(s) já existiam e foram ignoradas.')
        if not criadas and not existiam:
            messages.info(request, f'Nenhum contrato ativo encontrado para {mes:02d}/{ano}.')

        return HttpResponseRedirect(f"{reverse('receitas_list')}?mes={mes}&ano={ano}")

    # GET — monta contexto para o formulário de confirmação
    contratos_ativos = contratos_para_geracao_mes(mes, ano).count()

    context = {
        'mes_atual': mes,
        'ano_atual': ano,
        'contratos_ativos': contratos_ativos,
        'meses': ReceitaAluguel.MESES,
        'anos': anos_para_filtro(),
    }
    return render(request, 'financeiro/gerar_receitas.html', context)
