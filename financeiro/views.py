from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone

from .models import ReceitaAluguel, Despesa, FechamentoMensal
from .forms import BaixaReceitaForm
from .exports import (
    exportar_imoveis_csv, exportar_imoveis_xlsx,
    exportar_contratos_csv, exportar_contratos_xlsx,
    exportar_receitas_csv, exportar_receitas_xlsx,
    exportar_despesas_csv, exportar_despesas_xlsx,
    exportar_inadimplencia_csv, exportar_inadimplencia_xlsx,
    exportar_relatorio_mensal_xlsx,
    exportar_relatorio_contabilidade_xlsx,
)
from core.utils import anos_para_filtro, int_param, mes_ano_da_request, pk_param
from patrimonio.models import Imovel, Contrato, imobiliarias_queryset

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
def receitas_list(request):
    mes, ano, imovel_id, status, _ = _filtros_periodo(request)

    receitas = ReceitaAluguel.objects.select_related('imovel', 'contrato__locatario').filter(
        competencia_mes=mes, competencia_ano=ano
    )
    if imovel_id:
        receitas = receitas.filter(imovel_id=imovel_id)
    if status:
        receitas = receitas.filter(status=status)

    pagina = _paginar(request, receitas)

    context = {
        'receitas': pagina,
        'pagina': pagina,
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
def relatorios(request):
    hoje = timezone.localdate()

    context = {
        'imoveis': Imovel.objects.all(),
        'imobiliarias': imobiliarias_queryset(),
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
def export_imoveis(request, formato):
    status = request.GET.get('status', '')
    imoveis = Imovel.objects.filter(status=status) if status else Imovel.objects.all()
    if formato == 'csv':
        return exportar_imoveis_csv(request, imoveis)
    return exportar_imoveis_xlsx(request, imoveis)


@login_required
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
def export_relatorio_mensal(request, formato):
    mes, ano, _, _, _ = _filtros_periodo(request)
    return exportar_relatorio_mensal_xlsx(request, mes, ano)


@login_required
def export_relatorio_contabilidade(request):
    mes, ano = mes_ano_da_request(request)
    return exportar_relatorio_contabilidade_xlsx(request, mes, ano)


@login_required
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
                if receita.valor_recebido is None:
                    receita.valor_recebido = receita.valor_previsto
                if not receita.data_recebimento:
                    receita.data_recebimento = hoje
                receita.status = 'recebido'
                receita.save()
                messages.success(request, f'{receita.imovel.nome} marcado como recebido.')

            elif action == 'editar':
                form = BaixaReceitaForm(request.POST)
                if form.is_valid():
                    form.aplicar(receita)
                    receita.save()
                    messages.success(request, f'{receita.imovel.nome} atualizado.')
                else:
                    erros = '; '.join(
                        erro for lista in form.errors.values() for erro in lista
                    )
                    messages.error(request, f'{receita.imovel.nome} não foi atualizado: {erros}')

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
    """
    from .models import receitas_inadimplentes_qs
    from documentos.models import Documento, DocumentoObrigatorio, documentos_vencendo_qs

    hoje = timezone.localdate()
    mes, ano = mes_ano_da_request(request)

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

    receitas_mes = ReceitaAluguel.objects.filter(competencia_mes=mes, competencia_ano=ano)
    total_receitas = receitas_mes.count()
    receitas_recebidas = receitas_mes.filter(status__in=['recebido', 'parcial']).count()
    receitas_pendentes = receitas_mes.exclude(status__in=['recebido', 'parcial', 'cancelado']).count()

    despesas_mes = Despesa.objects.filter(competencia_mes=mes, competencia_ano=ano)
    total_despesas = despesas_mes.count()
    despesas_pagas = despesas_mes.filter(status='paga').count()

    inadimplentes = receitas_inadimplentes_qs().count()

    docs_pendentes_cnt = Documento.objects.filter(
        tipo__in=Documento.TIPOS_CONTABILIDADE, enviado_contabilidade=False
    ).count()

    docs_obrigatorios_pendentes = DocumentoObrigatorio.objects.filter(
        obrigatorio=True, documento__isnull=True
    ).count()

    docs_validade_cnt = documentos_vencendo_qs().count()

    reajustes_pendentes = Contrato.objects.filter(
        status='ativo', data_proximo_reajuste__isnull=False, data_proximo_reajuste__lte=hoje,
    ).count()

    try:
        fechamento = FechamentoMensal.objects.get(mes=mes, ano=ano)
    except FechamentoMensal.DoesNotExist:
        fechamento = None

    nome_mes = dict(ReceitaAluguel.MESES).get(mes, str(mes))

    checklist = [
        {
            'item': 'Receitas geradas',
            'ok': total_receitas > 0,
            'detalhe': f'{total_receitas} receita(s) cadastrada(s)' if total_receitas else 'Nenhuma receita gerada ainda',
            'link': reverse('gerar_receitas_mes') + f'?mes={mes}&ano={ano}',
        },
        {
            'item': 'Recebimentos confirmados',
            'ok': total_receitas > 0 and receitas_pendentes == 0,
            'detalhe': f'{receitas_recebidas}/{total_receitas} recebida(s)' if total_receitas else '—',
            'link': reverse('baixa_receitas_mes') + f'?mes={mes}&ano={ano}',
        },
        {
            'item': 'Inadimplência em dia',
            'ok': inadimplentes == 0,
            'detalhe': f'{inadimplentes} receita(s) inadimplente(s) em aberto' if inadimplentes else 'Sem inadimplência em aberto',
            'link': None,
        },
        {
            'item': 'Reajustes em dia',
            'ok': reajustes_pendentes == 0,
            'detalhe': (
                f'{reajustes_pendentes} contrato(s) com reajuste pendente'
                if reajustes_pendentes else 'Nenhum reajuste pendente'
            ),
            'link': reverse('contrato_list') + '?reajuste_pendente=1',
        },
        {
            'item': 'Despesas pagas',
            'ok': total_despesas == 0 or despesas_pagas == total_despesas,
            'detalhe': f'{despesas_pagas}/{total_despesas} paga(s)' if total_despesas else 'Sem despesas no período',
            'link': reverse('despesas_list') + f'?mes={mes}&ano={ano}',
        },
        {
            'item': 'Documentos enviados à contabilidade',
            'ok': docs_pendentes_cnt == 0,
            'detalhe': f'{docs_pendentes_cnt} documento(s) pendente(s)' if docs_pendentes_cnt else 'Todos enviados',
            'link': reverse('documento_list') + '?pendente=1',
        },
        {
            'item': 'Documentos obrigatórios revisados',
            'ok': docs_obrigatorios_pendentes == 0,
            'detalhe': (
                f'{docs_obrigatorios_pendentes} documento(s) obrigatório(s) pendente(s)'
                if docs_obrigatorios_pendentes
                else 'Todos os documentos obrigatórios vinculados'
            ),
            'link': reverse('admin:documentos_documentoobrigatorio_changelist'),
        },
        {
            'item': 'Validade dos documentos em dia',
            'ok': docs_validade_cnt == 0,
            'detalhe': (
                f'{docs_validade_cnt} documento(s) vencido(s) ou vencendo em 30 dias'
                if docs_validade_cnt else 'Nenhum documento com validade vencendo'
            ),
            'link': reverse('documento_list') + '?vencendo=1',
        },
        {
            'item': 'Relatório contábil exportado',
            'ok': False,
            'informativa': True,
            'detalhe': 'Baixe o relatório contábil após revisar receitas, despesas e documentos',
            'link': reverse('export_relatorio_contabilidade') + f'?mes={mes}&ano={ano}',
        },
        {
            'item': 'Fechamento registrado e enviado',
            'ok': fechamento is not None and fechamento.enviado_contabilidade,
            'detalhe': (
                f'Enviado em {fechamento.data_envio_contabilidade:%d/%m/%Y}'
                if fechamento and fechamento.enviado_contabilidade
                else 'Pendente'
            ),
            'link': None,
        },
    ]

    context = {
        'mes_atual': mes,
        'ano_atual': ano,
        'nome_mes': nome_mes,
        'meses': ReceitaAluguel.MESES,
        'anos': anos_para_filtro(),
        'checklist': checklist,
        'fechamento': fechamento,
        'total_receitas': total_receitas,
        'receitas_recebidas': receitas_recebidas,
        'receitas_pendentes': receitas_pendentes,
        'total_despesas': total_despesas,
        'despesas_pagas': despesas_pagas,
        'inadimplentes': inadimplentes,
        'docs_pendentes_cnt': docs_pendentes_cnt,
        'docs_obrigatorios_pendentes': docs_obrigatorios_pendentes,
        'docs_validade_cnt': docs_validade_cnt,
        'reajustes_pendentes': reajustes_pendentes,
    }
    return render(request, 'financeiro/checklist_mensal.html', context)


@login_required
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
