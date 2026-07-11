from django.contrib import admin, messages
from simple_history.admin import SimpleHistoryAdmin
from .models import ReceitaAluguel, ReceitaAluguelItem, RecebimentoReceita, Despesa, FechamentoMensal


class ReceitaAluguelItemInline(admin.TabularInline):
    model = ReceitaAluguelItem
    extra = 0
    fields = ('tipo', 'descricao', 'valor')


class RecebimentoReceitaInline(admin.TabularInline):
    model = RecebimentoReceita
    extra = 0
    fields = ('data_recebimento', 'valor', 'origem', 'transacao_extrato', 'criado_por', 'observacoes')
    readonly_fields = ('origem', 'transacao_extrato', 'criado_por')


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
            instances = formset.save(commit=False)
            for obj in instances:
                if obj.pk is None and obj.criado_por_id is None:
                    obj.criado_por = request.user
                obj.save()  # dispara garantir_recebimento_legado + recálculo
            for obj in formset.deleted_objects:
                obj.delete()  # delete() individual também reconsolida
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
