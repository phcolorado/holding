from django.contrib.auth.decorators import login_required
from django.shortcuts import render

from .models import Documento
from patrimonio.models import Imovel


@login_required
def documento_list(request):
    tipo = request.GET.get('tipo', '')
    imovel_id = request.GET.get('imovel', '')
    pendente = request.GET.get('pendente', '')

    docs = Documento.objects.select_related('imovel', 'pessoa', 'contrato').all()
    if tipo:
        docs = docs.filter(tipo=tipo)
    if imovel_id:
        docs = docs.filter(imovel_id=imovel_id)
    if pendente == '1':
        docs = docs.filter(tipo__in=Documento.TIPOS_CONTABILIDADE, enviado_contabilidade=False)

    context = {
        'documentos': docs,
        'imoveis': Imovel.objects.all(),
        'tipo_choices': Documento.TIPO_CHOICES,
        'tipo_atual': tipo,
        'imovel_atual': imovel_id,
        'pendente_atual': pendente,
    }
    return render(request, 'documentos/documento_list.html', context)
