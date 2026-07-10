from django.contrib.auth.decorators import login_required, permission_required
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, render

from .models import Documento, documentos_vencendo_qs
from core.utils import pk_param
from patrimonio.models import Imovel


@login_required
@permission_required('documentos.view_documento', raise_exception=True)
def documento_list(request):
    tipo = request.GET.get('tipo', '')
    imovel_id = pk_param(request.GET.get('imovel'))
    pendente = request.GET.get('pendente', '')
    vencendo = request.GET.get('vencendo', '')

    docs = Documento.objects.select_related('imovel', 'pessoa', 'contrato').all()
    if tipo:
        docs = docs.filter(tipo=tipo)
    if imovel_id:
        docs = docs.filter(imovel_id=imovel_id)
    if pendente == '1':
        docs = docs.filter(tipo__in=Documento.TIPOS_CONTABILIDADE, enviado_contabilidade=False)
    if vencendo == '1':
        docs = docs.filter(pk__in=documentos_vencendo_qs().values('pk')).order_by('data_validade')

    context = {
        'documentos': docs,
        'imoveis': Imovel.objects.all(),
        'tipo_choices': Documento.TIPO_CHOICES,
        'tipo_atual': tipo,
        'imovel_atual': imovel_id,
        'pendente_atual': pendente,
        'vencendo_atual': vencendo,
    }
    return render(request, 'documentos/documento_list.html', context)


@login_required
@permission_required('documentos.view_documento', raise_exception=True)
def documento_download(request, pk):
    """
    Serve o arquivo do documento exigindo login — evita expor /media/
    diretamente para quem tiver a URL.
    """
    documento = get_object_or_404(Documento, pk=pk)
    if not documento.arquivo:
        raise Http404('Documento sem arquivo anexado.')
    try:
        return FileResponse(documento.arquivo.open('rb'), as_attachment=False,
                            filename=documento.arquivo.name.rsplit('/', 1)[-1])
    except FileNotFoundError:
        raise Http404('Arquivo não encontrado no armazenamento.')
