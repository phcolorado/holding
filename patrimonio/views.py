from django.contrib.auth.decorators import login_required, permission_required
from django.shortcuts import render, get_object_or_404
from django.db.models import Sum, Q
from django.utils import timezone

from .models import Imovel, Pessoa, Contrato, Manutencao, imobiliarias_queryset
from .indicadores import indicadores_do_imovel
from core.utils import pk_param
from financeiro.models import ReceitaAluguel, Despesa
from documentos.models import Documento, DocumentoObrigatorio


@login_required
@permission_required('patrimonio.view_imovel', raise_exception=True)
def imovel_list(request):
    status = request.GET.get('status', '')
    cidade = request.GET.get('cidade', '')
    tipo_imovel = request.GET.get('tipo_imovel', '')
    uso = request.GET.get('uso', '')

    imoveis = Imovel.objects.select_related('imovel_pai').all()
    if status:
        imoveis = imoveis.filter(status=status)
    if cidade:
        imoveis = imoveis.filter(cidade__icontains=cidade)
    if tipo_imovel:
        imoveis = imoveis.filter(tipo_imovel=tipo_imovel)
    if uso:
        imoveis = imoveis.filter(uso=uso)

    context = {
        'imoveis': imoveis,
        'status_atual': status,
        'cidade_atual': cidade,
        'tipo_imovel_atual': tipo_imovel,
        'uso_atual': uso,
        'status_choices': Imovel.STATUS_CHOICES,
        'tipo_imovel_choices': Imovel.TIPO_IMOVEL_CHOICES,
        'uso_choices': Imovel.USO_CHOICES,
    }
    return render(request, 'patrimonio/imovel_list.html', context)


@login_required
@permission_required('patrimonio.view_imovel', raise_exception=True)
def imovel_detail(request, pk):
    """
    Detalhe do imóvel: cada seção só é consultada e exibida se o usuário tem
    a permissão de leitura da área correspondente — a view não busca dados
    que o template esconderia.
    """
    imovel = get_object_or_404(Imovel, pk=pk)
    pode = request.user.has_perm

    ve_contrato = pode('patrimonio.view_contrato')
    ve_receitas = pode('financeiro.view_receitaaluguel')
    ve_despesas = pode('financeiro.view_despesa')
    ve_documentos = pode('documentos.view_documento')
    ve_obrigatorios = pode('documentos.view_documentoobrigatorio')
    ve_manutencoes = pode('patrimonio.view_manutencao')

    contrato_ativo = imovel.get_contrato_ativo() if ve_contrato else None
    receitas = (
        ReceitaAluguel.objects.filter(imovel=imovel)
        .prefetch_related('contrato__partes__pessoa')
        .order_by('-competencia_ano', '-competencia_mes')[:24]
        if ve_receitas else ReceitaAluguel.objects.none()
    )
    despesas = (
        Despesa.objects.filter(imovel=imovel).order_by('-data_vencimento')[:24]
        if ve_despesas else Despesa.objects.none()
    )
    documentos = (
        Documento.objects.filter(imovel=imovel).order_by('-criado_em')
        if ve_documentos else Documento.objects.none()
    )
    manutencoes = (
        Manutencao.objects.filter(imovel=imovel).order_by('-data_solicitacao')
        if ve_manutencoes else Manutencao.objects.none()
    )
    docs_obrigatorios = (
        DocumentoObrigatorio.objects.filter(imovel=imovel).select_related('documento')
        if ve_obrigatorios else DocumentoObrigatorio.objects.none()
    )
    unidades = imovel.unidades.all()

    # Valores financeiros calculados SOMENTE para as áreas que o usuário pode
    # ver: sem permissão de despesas não há consulta a despesas nem resultado
    # líquido/yield líquido; sem permissão de receitas não há indicadores.
    if ve_receitas:
        total_recebido = ReceitaAluguel.objects.filter(
            imovel=imovel, status__in=('recebido', 'parcial')
        ).aggregate(total=Sum('valor_recebido'))['total'] or 0
        indicadores = indicadores_do_imovel(imovel, incluir_despesas=ve_despesas)
    else:
        total_recebido = None
        indicadores = None
    total_despesas = (
        Despesa.objects.filter(imovel=imovel, status='paga').aggregate(total=Sum('valor'))['total'] or 0
        if ve_despesas else None
    )
    resultado = (
        total_recebido - total_despesas
        if ve_receitas and ve_despesas else None
    )

    context = {
        'indicadores': indicadores,
        'imovel': imovel,
        'contrato_ativo': contrato_ativo,
        'receitas': receitas,
        'despesas': despesas,
        'documentos': documentos,
        'manutencoes': manutencoes,
        'total_recebido': total_recebido,
        'total_despesas': total_despesas,
        'resultado': resultado,
        'docs_obrigatorios': docs_obrigatorios,
        'unidades': unidades,
        've_obrigatorios': ve_obrigatorios,
        've_receitas': ve_receitas,
        've_despesas': ve_despesas,
        've_manutencoes': ve_manutencoes,
        've_documentos': ve_documentos,
        've_contrato': ve_contrato,
    }
    return render(request, 'patrimonio/imovel_detail.html', context)


@login_required
@permission_required('patrimonio.view_pessoa', raise_exception=True)
def pessoa_list(request):
    tipo = request.GET.get('tipo', '')
    q = request.GET.get('q', '')

    pessoas = Pessoa.objects.all()
    if tipo:
        pessoas = pessoas.filter(tipo=tipo)
    if q:
        pessoas = pessoas.filter(Q(nome__icontains=q) | Q(cpf_cnpj__icontains=q) | Q(email__icontains=q))

    context = {
        'pessoas': pessoas,
        'tipo_atual': tipo,
        'q': q,
        'tipo_choices': Pessoa.TIPO_CHOICES,
    }
    return render(request, 'patrimonio/pessoa_list.html', context)


@login_required
@permission_required('patrimonio.view_contrato', raise_exception=True)
def contrato_list(request):
    status = request.GET.get('status', '')
    imovel_id = pk_param(request.GET.get('imovel'))
    imobiliaria_id = pk_param(request.GET.get('imobiliaria'))
    reajuste_pendente = request.GET.get('reajuste_pendente', '')

    hoje = timezone.localdate()

    contratos = (
        Contrato.objects
        .select_related('imovel', 'locatario', 'fiador', 'imobiliaria')
        .prefetch_related('partes__pessoa')
        .all()
    )
    if status:
        contratos = contratos.filter(status=status)
    if imovel_id:
        contratos = contratos.filter(imovel_id=imovel_id)
    if imobiliaria_id:
        contratos = contratos.filter(
            Q(imobiliaria_id=imobiliaria_id) | Q(partes__papel='imobiliaria', partes__pessoa_id=imobiliaria_id)
        ).distinct()
    if reajuste_pendente:
        contratos = contratos.filter(
            status='ativo', data_proximo_reajuste__isnull=False, data_proximo_reajuste__lte=hoje,
        )

    imoveis = Imovel.objects.all()
    imobiliarias = imobiliarias_queryset()

    context = {
        'contratos': contratos,
        'status_atual': status,
        'imovel_atual': imovel_id,
        'imobiliaria_atual': imobiliaria_id,
        'reajuste_pendente_atual': reajuste_pendente,
        'imoveis': imoveis,
        'imobiliarias': imobiliarias,
        'status_choices': Contrato.STATUS_CHOICES,
        'hoje': hoje,
    }
    return render(request, 'patrimonio/contrato_list.html', context)
