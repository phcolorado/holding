from django.contrib.auth.decorators import login_required
from django.shortcuts import render, get_object_or_404
from django.db.models import Sum, Q
from django.utils import timezone

from .models import Imovel, Pessoa, Contrato, Manutencao
from financeiro.models import ReceitaAluguel, Despesa
from documentos.models import Documento


@login_required
def imovel_list(request):
    status = request.GET.get('status', '')
    cidade = request.GET.get('cidade', '')

    imoveis = Imovel.objects.all()
    if status:
        imoveis = imoveis.filter(status=status)
    if cidade:
        imoveis = imoveis.filter(cidade__icontains=cidade)

    context = {
        'imoveis': imoveis,
        'status_atual': status,
        'cidade_atual': cidade,
        'status_choices': Imovel.STATUS_CHOICES,
    }
    return render(request, 'patrimonio/imovel_list.html', context)


@login_required
def imovel_detail(request, pk):
    imovel = get_object_or_404(Imovel, pk=pk)
    contrato_ativo = imovel.get_contrato_ativo()

    receitas = ReceitaAluguel.objects.filter(imovel=imovel).order_by('-competencia_ano', '-competencia_mes')[:24]
    despesas = Despesa.objects.filter(imovel=imovel).order_by('-data_vencimento')[:24]
    documentos = Documento.objects.filter(imovel=imovel).order_by('-criado_em')
    manutencoes = Manutencao.objects.filter(imovel=imovel).order_by('-data_solicitacao')

    total_recebido = ReceitaAluguel.objects.filter(
        imovel=imovel, status__in=('recebido', 'parcial')
    ).aggregate(total=Sum('valor_recebido'))['total'] or 0
    total_despesas = Despesa.objects.filter(
        imovel=imovel, status='paga'
    ).aggregate(total=Sum('valor'))['total'] or 0
    resultado = total_recebido - total_despesas

    context = {
        'imovel': imovel,
        'contrato_ativo': contrato_ativo,
        'receitas': receitas,
        'despesas': despesas,
        'documentos': documentos,
        'manutencoes': manutencoes,
        'total_recebido': total_recebido,
        'total_despesas': total_despesas,
        'resultado': resultado,
    }
    return render(request, 'patrimonio/imovel_detail.html', context)


@login_required
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
def contrato_list(request):
    status = request.GET.get('status', '')
    imovel_id = request.GET.get('imovel', '')

    contratos = Contrato.objects.select_related('imovel', 'locatario').all()
    if status:
        contratos = contratos.filter(status=status)
    if imovel_id:
        contratos = contratos.filter(imovel_id=imovel_id)

    hoje = timezone.now().date()
    imoveis = Imovel.objects.all()

    context = {
        'contratos': contratos,
        'status_atual': status,
        'imovel_atual': imovel_id,
        'imoveis': imoveis,
        'status_choices': Contrato.STATUS_CHOICES,
        'hoje': hoje,
    }
    return render(request, 'patrimonio/contrato_list.html', context)
