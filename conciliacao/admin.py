from django.contrib import admin
from simple_history.admin import SimpleHistoryAdmin

from .models import ContaBancaria, ExtratoImportado, TransacaoExtrato, RegraClassificacao


@admin.register(ContaBancaria)
class ContaBancariaAdmin(SimpleHistoryAdmin):
    list_display = ('nome', 'banco', 'agencia', 'numero_conta', 'ativo')
    list_filter = ('ativo', 'banco')
    search_fields = ('nome', 'banco', 'numero_conta')
    readonly_fields = ('criado_em', 'atualizado_em')


@admin.register(RegraClassificacao)
class RegraClassificacaoAdmin(SimpleHistoryAdmin):
    list_display = ('texto_contem', 'categoria', 'fornecedor', 'imovel', 'prioridade', 'ativo')
    list_filter = ('ativo', 'categoria')
    search_fields = ('texto_contem',)
    ordering = ('prioridade',)
    raw_id_fields = ('fornecedor', 'imovel')


class TransacaoExtratoInline(admin.TabularInline):
    model = TransacaoExtrato
    extra = 0
    can_delete = False
    fields = ('data', 'tipo', 'valor', 'descricao', 'status')
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(ExtratoImportado)
class ExtratoImportadoAdmin(SimpleHistoryAdmin):
    list_display = (
        'conta', 'periodo_inicio', 'periodo_fim',
        'transacoes_novas', 'transacoes_duplicadas', 'importado_em', 'importado_por',
    )
    list_filter = ('conta',)
    readonly_fields = (
        'conta', 'arquivo', 'hash_arquivo', 'periodo_inicio', 'periodo_fim',
        'transacoes_novas', 'transacoes_duplicadas', 'importado_em', 'importado_por',
    )
    inlines = [TransacaoExtratoInline]

    def has_add_permission(self, request):
        # Importação só pela tela de conciliação, que valida e parseia o OFX.
        return False
