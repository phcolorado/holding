from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.core.exceptions import PermissionDenied
from django.db.models import Count
from django.http import FileResponse, Http404, HttpResponseRedirect
from django.shortcuts import get_object_or_404, render
from django.urls import reverse

from core.utils import pk_param
from financeiro.models import ReceitaAluguel
from .forms import UploadExtratoForm, DespesaExtratoForm
from .models import ExtratoImportado, TransacaoExtrato, ContaBancaria
from .services import (
    ConciliacaoInvalidaError, ExtratoJaImportadoError, OFXInvalidoError,
    importar_ofx, receitas_candidatas, sugerir_receitas, sugerir_classificacao,
    conciliar_com_receitas, desfazer_conciliacao, lancar_despesa,
    excluir_extrato_sem_movimentacoes,
)


def _exigir_permissao(request, perm):
    if not request.user.has_perm(perm):
        raise PermissionDenied


@login_required
@permission_required('conciliacao.view_extratoimportado', raise_exception=True)
def extrato_list(request):
    """
    GET: lista extratos importados.
    POST action=excluir: exclui um extrato TOTALMENTE pendente (nenhuma
    transação tratada) — única forma de exclusão oferecida pelo sistema
    (o Admin bloqueia a exclusão para preservar a trilha de auditoria).
    POST (sem action): importa um novo arquivo OFX.
    """
    if request.method == 'POST' and request.POST.get('action') == 'excluir':
        _exigir_permissao(request, 'conciliacao.delete_extratoimportado')
        extrato_id = pk_param(request.POST.get('extrato_id'))
        extrato = get_object_or_404(ExtratoImportado, pk=extrato_id) if extrato_id else None
        if extrato is None:
            messages.error(request, 'Extrato inválido.')
        else:
            try:
                excluir_extrato_sem_movimentacoes(extrato.pk, usuario=request.user)
            except ConciliacaoInvalidaError as exc:
                messages.error(request, str(exc))
            else:
                messages.success(request, 'Extrato excluído.')
        return HttpResponseRedirect(reverse('extrato_list'))

    if request.method == 'POST':
        _exigir_permissao(request, 'conciliacao.add_extratoimportado')
        form = UploadExtratoForm(request.POST, request.FILES)
        if form.is_valid():
            try:
                extrato = importar_ofx(
                    form.cleaned_data['arquivo'], form.cleaned_data['conta'], request.user
                )
            except (ExtratoJaImportadoError, OFXInvalidoError) as exc:
                messages.error(request, str(exc))
            else:
                messages.success(
                    request,
                    f'Extrato importado: {extrato.transacoes_novas} transação(ões) nova(s), '
                    f'{extrato.transacoes_duplicadas} já existiam.',
                )
                return HttpResponseRedirect(reverse('conciliar_extrato', args=[extrato.pk]))
        else:
            for erros in form.errors.values():
                for erro in erros:
                    messages.error(request, erro)
        return HttpResponseRedirect(reverse('extrato_list'))

    extratos = (
        ExtratoImportado.objects.select_related('conta', 'importado_por')
        .annotate(total_transacoes=Count('transacoes'))
    )
    # Item 6.1: usuário só com view_extratoimportado vê a lista, mas não o
    # formulário de upload, a lista de contas para upload nem o aviso para
    # cadastrar conta — nada disso é construído/consultado sem add_
    # extratoimportado. O POST continua exigindo a permissão no servidor
    # (checagem acima), independentemente do que o template mostra.
    pode_importar_extrato = request.user.has_perm('conciliacao.add_extratoimportado')
    context = {
        'extratos': extratos,
        'pode_importar_extrato': pode_importar_extrato,
        'form': UploadExtratoForm() if pode_importar_extrato else None,
        'tem_conta': ContaBancaria.objects.filter(ativo=True).exists() if pode_importar_extrato else None,
        'total_pendentes': TransacaoExtrato.objects.filter(status='pendente').count(),
    }
    return render(request, 'conciliacao/extrato_list.html', context)


@login_required
@permission_required('conciliacao.view_extratoimportado', raise_exception=True)
def extrato_download(request, pk):
    """
    Serve o arquivo OFX original exigindo login e permissão — mesma proteção
    de documento_download. Extratos bancários são potencialmente sensíveis
    (revelam movimentações financeiras completas da conta).
    """
    extrato = get_object_or_404(ExtratoImportado, pk=pk)
    if not extrato.arquivo:
        raise Http404('Extrato sem arquivo anexado.')
    try:
        return FileResponse(
            extrato.arquivo.open('rb'), as_attachment=True,
            filename=extrato.arquivo.name.rsplit('/', 1)[-1],
        )
    except FileNotFoundError:
        raise Http404('Arquivo não encontrado no armazenamento.')


def _contexto_transacao(transacao, pode_conciliar_receitas, pode_lancar_despesa):
    """
    Pré-calcula sugestões e candidatas para exibição na tela de conciliação
    — item 6.2: nunca consulta receitas candidatas se o usuário não puder
    conciliar receitas, nem sugere classificação de débito (que exigiria
    consultar fornecedores/imóveis do form) se não puder lançar despesa.
    """
    dados = {'transacao': transacao, 'sugestao': None, 'candidatas': [], 'regra': None}
    if transacao.status != 'pendente':
        return dados
    if transacao.tipo == 'credito':
        if not pode_conciliar_receitas:
            return dados
        sugestao = sugerir_receitas(transacao)
        dados['sugestao'] = sugestao
        sugeridas_pks = {r.pk for r in sugestao['receitas']} if sugestao else set()
        dados['candidatas'] = [
            {'receita': r, 'sugerida': r.pk in sugeridas_pks}
            for r in receitas_candidatas(transacao)
        ]
    else:
        if not pode_lancar_despesa:
            return dados
        dados['regra'] = sugerir_classificacao(transacao)
    return dados


@login_required
@permission_required('conciliacao.view_extratoimportado', raise_exception=True)
def conciliar_extrato(request, pk):
    """Tela de conciliação de um extrato: créditos ↔ receitas, débitos → despesas."""
    extrato = get_object_or_404(ExtratoImportado.objects.select_related('conta'), pk=pk)
    pode_conciliar_receitas = request.user.has_perm('financeiro.change_receitaaluguel')
    pode_lancar_despesa = request.user.has_perm('financeiro.add_despesa')
    pode_alterar_transacao = request.user.has_perm('conciliacao.change_transacaoextrato')
    # Item 3 (rodada de fechamento estrutural): ver a lista de extratos NÃO
    # concede acesso aos detalhes internos das transações já TRATADAS —
    # descrição/categoria/fornecedor/imóvel de uma despesa e imóvel/
    # locatário/valores de receitas vinculadas só aparecem para quem também
    # tem a permissão de VER aquela área (nunca a de escrita, que é outra
    # coisa). Nomes de imóvel dependem também de patrimonio.view_imovel.
    ve_imoveis = request.user.has_perm('patrimonio.view_imovel')
    pode_ver_detalhes_despesas = request.user.has_perm('financeiro.view_despesa') and ve_imoveis
    pode_ver_detalhes_receitas = request.user.has_perm('financeiro.view_receitaaluguel') and ve_imoveis

    if request.method == 'POST':
        transacao = get_object_or_404(
            TransacaoExtrato, pk=pk_param(request.POST.get('transacao_id')) or 0, extrato=extrato
        )
        action = request.POST.get('action', '')

        if action == 'conciliar':
            _exigir_permissao(request, 'financeiro.change_receitaaluguel')
            receita_ids = [pk_param(v) for v in request.POST.getlist('receita_ids') if pk_param(v)]
            receitas = list(ReceitaAluguel.objects.filter(pk__in=receita_ids))
            marcar_comissoes = request.POST.get('marcar_comissoes') == '1'
            try:
                conciliar_com_receitas(
                    transacao, receitas,
                    marcar_comissoes=marcar_comissoes, usuario=request.user,
                )
            except ConciliacaoInvalidaError as exc:
                messages.error(request, str(exc))
            else:
                nomes = ', '.join(r.imovel.nome for r in receitas)
                messages.success(request, f'Crédito de R$ {transacao.valor_absoluto} conciliado com: {nomes}.')

        elif action == 'lancar_despesa':
            _exigir_permissao(request, 'financeiro.add_despesa')
            form = DespesaExtratoForm(request.POST)
            if form.is_valid():
                try:
                    despesa = lancar_despesa(
                        transacao,
                        categoria=form.cleaned_data['categoria'],
                        descricao=form.cleaned_data['descricao'],
                        fornecedor=form.cleaned_data['fornecedor'],
                        imovel=form.cleaned_data['imovel'],
                    )
                except ConciliacaoInvalidaError as exc:
                    messages.error(request, str(exc))
                else:
                    messages.success(request, f'Despesa lançada: {despesa.descricao} (R$ {despesa.valor}).')
            else:
                erros = '; '.join(e for lista in form.errors.values() for e in lista)
                messages.error(request, f'Despesa não lançada: {erros}')

        elif action == 'desfazer':
            _exigir_permissao(request, 'conciliacao.change_transacaoextrato')
            try:
                resultado = desfazer_conciliacao(transacao)
            except ConciliacaoInvalidaError as exc:
                messages.error(request, str(exc))
            else:
                if resultado.tipo == 'debito':
                    messages.success(
                        request,
                        'Conciliação do débito desfeita e despesa vinculada removida.',
                    )
                else:
                    messages.success(
                        request,
                        'Conciliação desfeita: recebimentos removidos, receitas e comissões restauradas.',
                    )

        elif action == 'ignorar' and transacao.status == 'pendente':
            _exigir_permissao(request, 'conciliacao.change_transacaoextrato')
            transacao.status = 'ignorada'
            transacao.save(update_fields=['status'])
            messages.info(request, 'Transação marcada como ignorada.')

        elif action == 'reabrir' and transacao.status == 'ignorada':
            _exigir_permissao(request, 'conciliacao.change_transacaoextrato')
            transacao.status = 'pendente'
            transacao.save(update_fields=['status'])
            messages.info(request, 'Transação reaberta.')

        return HttpResponseRedirect(reverse('conciliar_extrato', args=[extrato.pk]))

    transacoes_qs = extrato.transacoes.order_by('data', 'id')
    # Item 3: só faz select_related/prefetch_related dos objetos protegidos
    # quando o usuário efetivamente pode vê-los — nunca materializa despesa/
    # receita vinculadas no contexto sem a permissão correspondente.
    if pode_ver_detalhes_despesas:
        transacoes_qs = transacoes_qs.select_related('despesa', 'despesa__imovel')
    if pode_ver_detalhes_receitas:
        transacoes_qs = transacoes_qs.prefetch_related('itens_receita__receita__imovel')
    transacoes = list(transacoes_qs)

    pendentes = [
        _contexto_transacao(t, pode_conciliar_receitas, pode_lancar_despesa)
        for t in transacoes if t.status == 'pendente'
    ]
    tratadas = [t for t in transacoes if t.status != 'pendente']

    context = {
        'extrato': extrato,
        'pendentes': pendentes,
        'tratadas': tratadas,
        'total': len(pendentes) + len(tratadas),
        # DespesaExtratoForm() constrói ModelChoiceField de fornecedor/imóvel
        # (consulta o banco) — só é criado quando o usuário pode lançar
        # despesa (item 6.2).
        'despesa_form': DespesaExtratoForm() if pode_lancar_despesa else None,
        'pode_conciliar_receitas': pode_conciliar_receitas,
        'pode_lancar_despesa': pode_lancar_despesa,
        'pode_alterar_transacao': pode_alterar_transacao,
        'pode_ver_detalhes_despesas': pode_ver_detalhes_despesas,
        'pode_ver_detalhes_receitas': pode_ver_detalhes_receitas,
    }
    return render(request, 'conciliacao/conciliar.html', context)
