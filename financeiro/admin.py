from django import forms
from django.contrib import admin, messages
from django.core.exceptions import ValidationError
from django.forms.models import BaseInlineFormSet
from simple_history.admin import SimpleHistoryAdmin
from .models import ReceitaAluguel, ReceitaAluguelItem, RecebimentoReceita, Despesa, FechamentoMensal
from .services import atualizar_recebimentos_da_receita


class ReceitaAluguelItemInline(admin.TabularInline):
    model = ReceitaAluguelItem
    extra = 0
    fields = ('tipo', 'descricao', 'valor')


class RecebimentoReceitaInlineForm(forms.ModelForm):
    """
    Pula a checagem de saldo POR LINHA do model (RecebimentoReceita.clean())
    — o inline salva o formset inteiro como uma única operação financeira via
    atualizar_recebimentos_da_receita() (ReceitaAluguelAdmin.save_formset),
    que calcula o total FINAL de todas as inclusões/alterações/exclusões em
    CONJUNTO. Validar cada linha isolada aqui, contra o saldo ATUAL (antes
    das outras mudanças do mesmo envio), rejeitaria ou aceitaria combinações
    incorretamente. A validação valor>0 continua ativa (não é pulada).
    """
    class Meta:
        model = RecebimentoReceita
        fields = '__all__'

    def _post_clean(self):
        self.instance._pular_validacao_saldo = True
        super()._post_clean()


class RecebimentoReceitaInlineFormSet(BaseInlineFormSet):
    """
    Desabilita, por linha, a edição e exclusão de recebimentos originados de
    conciliação bancária (origem='conciliacao' ou transacao_extrato
    preenchida) — para corrigi-los, desfaça a conciliação correspondente na
    tela de Conciliação Bancária. disabled=True é reforço de SERVIDOR (dado
    submetido para um campo disabled é ignorado em favor do initial).
    """
    def add_fields(self, form, index):
        super().add_fields(form, index)
        instance = getattr(form, 'instance', None)
        if instance and instance.pk and (
            instance.origem == 'conciliacao' or instance.transacao_extrato_id
        ):
            if 'DELETE' in form.fields:
                form.fields['DELETE'].disabled = True
            for campo in ('data_recebimento', 'valor', 'observacoes'):
                if campo in form.fields:
                    form.fields[campo].disabled = True


class RecebimentoReceitaInline(admin.TabularInline):
    model = RecebimentoReceita
    form = RecebimentoReceitaInlineForm
    formset = RecebimentoReceitaInlineFormSet
    extra = 0
    fields = (
        'data_recebimento', 'valor', 'origem', 'transacao_extrato',
        'criado_por', 'observacoes', 'nota_edicao',
    )
    readonly_fields = ('origem', 'transacao_extrato', 'criado_por', 'nota_edicao')

    @admin.display(description='Nota')
    def nota_edicao(self, obj):
        if obj and obj.pk and (obj.origem == 'conciliacao' or obj.transacao_extrato_id):
            return 'Origem: conciliação bancária — desfaça a conciliação correspondente para corrigir.'
        return ''


@admin.register(ReceitaAluguel)
class ReceitaAluguelAdmin(SimpleHistoryAdmin):
    list_display = (
        'imovel', 'competencia_mes', 'competencia_ano',
        'valor_previsto', 'valor_recebido', 'data_vencimento', 'status',
    )
    list_filter = ('status', 'competencia_ano', 'competencia_mes', 'imovel')
    search_fields = ('imovel__nome', 'contrato__locatario__nome', 'contrato__partes__pessoa__nome')
    date_hierarchy = 'data_vencimento'
    # valor_recebido/data_recebimento/status são CONSOLIDADOS derivados dos
    # recebimentos (inline abaixo) — nunca editados diretamente. Cancelamento
    # e reabertura passam pelas actions dedicadas.
    readonly_fields = ('valor_recebido', 'data_recebimento', 'status', 'criado_em', 'atualizado_em')
    ordering = ('-competencia_ano', '-competencia_mes')
    raw_id_fields = ('contrato',)
    inlines = [ReceitaAluguelItemInline, RecebimentoReceitaInline]
    actions = ['cancelar_receitas', 'reabrir_receitas']

    def get_readonly_fields(self, request, obj=None):
        return self.readonly_fields + ('imovel',)

    fieldsets = (
        ('Referência', {
            'description': 'O imóvel é preenchido automaticamente pelo contrato selecionado ao salvar.',
            'fields': ('contrato', 'imovel', 'competencia_mes', 'competencia_ano')
        }),
        ('Vencimento e Valores', {
            'fields': ('data_vencimento', 'valor_previsto', 'multa', 'juros', 'desconto')
        }),
        ('Recebimento (consolidado — derivado dos recebimentos abaixo)', {
            'description': (
                'Estes campos são calculados automaticamente a partir dos '
                '"Recebimentos da Receita". Para registrar um pagamento, adicione '
                'um recebimento no inline; para cancelar/reabrir a receita, use as '
                'ações da listagem.'
            ),
            'fields': ('valor_recebido', 'data_recebimento', 'status')
        }),
        ('Observações', {
            'fields': ('observacoes',)
        }),
        ('Controle', {
            'fields': ('criado_em', 'atualizado_em'),
            'classes': ('collapse',),
        }),
    )

    @admin.action(description='Cancelar receitas selecionadas')
    def cancelar_receitas(self, request, queryset):
        total = sum(1 for r in queryset if r.cancelar())
        if total:
            self.message_user(
                request,
                f'{total} receita(s) cancelada(s) — recebimentos registrados foram preservados.',
                messages.SUCCESS,
            )
        else:
            self.message_user(request, 'Nenhuma receita precisou ser cancelada.', messages.WARNING)

    @admin.action(description='Reabrir receitas canceladas')
    def reabrir_receitas(self, request, queryset):
        total = sum(1 for r in queryset if r.reabrir())
        if total:
            self.message_user(
                request,
                f'{total} receita(s) reaberta(s) — status recalculado pelo saldo, vencimento e recebimentos.',
                messages.SUCCESS,
            )
        else:
            self.message_user(request, 'Nenhuma receita selecionada estava cancelada.', messages.WARNING)

    def save_formset(self, request, form, formset, change):
        if formset.model is RecebimentoReceita:
            # formset.save(commit=False) NÃO persiste nada — apenas popula
            # new_objects/changed_objects/deleted_objects (mesma técnica já
            # usada para ReajusteContrato/Documento neste admin). O formset
            # inteiro é então salvo como UMA ÚNICA operação financeira
            # atômica por atualizar_recebimentos_da_receita(), que valida o
            # total final de todas as linhas em conjunto — nunca uma
            # transação independente por linha.
            formset.save(commit=False)
            novos = [
                {
                    'data_recebimento': obj.data_recebimento,
                    'valor': obj.valor,
                    'observacoes': obj.observacoes,
                }
                for obj in formset.new_objects
            ]
            alterados = [
                {
                    'pk': obj.pk,
                    'data_recebimento': obj.data_recebimento,
                    'valor': obj.valor,
                    'observacoes': obj.observacoes,
                }
                for obj, _campos in formset.changed_objects
            ]
            excluidos = [obj.pk for obj in formset.deleted_objects]
            if novos or alterados or excluidos:
                try:
                    atualizar_recebimentos_da_receita(
                        form.instance.pk, novos=novos, alterados=alterados,
                        excluidos=excluidos, usuario=request.user,
                    )
                except ValidationError as exc:
                    mensagens = exc.messages if hasattr(exc, 'messages') else [str(exc)]
                    messages.error(request, '; '.join(mensagens))
            formset.save_m2m()
        else:
            formset.save()

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        # Depois de salvar/alterar/excluir inlines (e possíveis mudanças em
        # multa/juros/desconto no form principal), reconsolida a receita.
        form.instance.refresh_from_db()
        form.instance.recalcular_recebimentos()


@admin.register(Despesa)
class DespesaAdmin(SimpleHistoryAdmin):
    list_display = (
        'descricao', 'imovel', 'categoria', 'valor', 'data_vencimento',
        'status', 'origem_automatica',
    )
    list_filter = ('status', 'categoria', 'imovel', 'competencia_ano', 'origem_automatica')
    search_fields = ('descricao', 'imovel__nome', 'fornecedor__nome')
    date_hierarchy = 'data_vencimento'
    readonly_fields = ('criado_em', 'atualizado_em', 'origem_automatica')
    ordering = ('-data_vencimento',)
    raw_id_fields = ('contrato', 'receita')
    actions = ['reabrir_despesas_action', 'cancelar_despesas_action']

    @admin.action(description='Reabrir despesas selecionadas (limpa data de pagamento)')
    def reabrir_despesas_action(self, request, queryset):
        from .services import reabrir_despesa
        total = 0
        for despesa in queryset.exclude(status__in=('prevista', 'atrasada')):
            reabrir_despesa(despesa.pk)
            total += 1
        if total:
            self.message_user(request, f'{total} despesa(s) reaberta(s).', messages.SUCCESS)
        else:
            self.message_user(request, 'Nenhuma despesa selecionada precisava ser reaberta.', messages.WARNING)

    @admin.action(description='Cancelar despesas selecionadas')
    def cancelar_despesas_action(self, request, queryset):
        from .services import cancelar_despesa
        total = 0
        for despesa in queryset.exclude(status='cancelada'):
            cancelar_despesa(despesa.pk)
            total += 1
        if total:
            self.message_user(request, f'{total} despesa(s) cancelada(s).', messages.SUCCESS)
        else:
            self.message_user(request, 'Nenhuma despesa selecionada precisava ser cancelada.', messages.WARNING)

    fieldsets = (
        ('Identificação', {
            'fields': ('descricao', 'categoria', 'imovel', 'fornecedor')
        }),
        ('Vínculos', {
            'fields': ('contrato', 'receita', 'origem_automatica'),
            'description': 'Preenchidos automaticamente quando a despesa é gerada pelo sistema (ex.: taxa de administração).',
        }),
        ('Competência e Vencimento', {
            'fields': ('competencia_mes', 'competencia_ano', 'data_vencimento')
        }),
        ('Pagamento', {
            'fields': ('valor', 'data_pagamento', 'status')
        }),
        ('Observações', {
            'fields': ('observacoes',)
        }),
        ('Controle', {
            'fields': ('criado_em', 'atualizado_em'),
            'classes': ('collapse',),
        }),
    )


@admin.register(FechamentoMensal)
class FechamentoMensalAdmin(SimpleHistoryAdmin):
    list_display = ('mes', 'ano', 'data_fechamento', 'enviado_contabilidade', 'data_envio_contabilidade')
    list_filter = ('enviado_contabilidade', 'ano')
    readonly_fields = ('criado_em', 'atualizado_em')
    ordering = ('-ano', '-mes')
