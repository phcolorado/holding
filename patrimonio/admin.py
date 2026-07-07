from decimal import Decimal

from django.contrib import admin, messages
from django.utils import timezone
from simple_history.admin import SimpleHistoryAdmin

from .models import (
    Imovel, Pessoa, Contrato, Manutencao,
    ContratoParte, EncargoContrato, ReajusteContrato,
)
from documentos.models import Documento


@admin.register(Imovel)
class ImovelAdmin(SimpleHistoryAdmin):
    list_display = (
        'nome', 'cidade', 'estado', 'tipo_imovel', 'uso', 'status',
        'imovel_pai', 'proprietario', 'valor_estimado', 'criado_em',
    )
    list_filter = ('status', 'tipo_imovel', 'uso', 'estado', 'cidade')
    search_fields = ('nome', 'endereco', 'cidade', 'matricula', 'inscricao_iptu', 'proprietario')
    date_hierarchy = 'data_aquisicao'
    readonly_fields = ('criado_em', 'atualizado_em')
    ordering = ('nome',)
    raw_id_fields = ('imovel_pai',)
    fieldsets = (
        ('Identificação', {
            'fields': ('nome', 'status', 'proprietario', 'tipo_imovel', 'uso')
        }),
        ('Prédio / Unidades', {
            'fields': ('imovel_pai', 'unidade_locavel'),
            'description': 'Preencha "Imóvel Pai" quando este imóvel for uma unidade de um prédio maior.',
        }),
        ('Localização', {
            'fields': ('endereco', 'cidade', 'estado', 'cep')
        }),
        ('Registro', {
            'fields': ('matricula', 'cartorio', 'inscricao_iptu')
        }),
        ('Valores', {
            'fields': ('data_aquisicao', 'valor_aquisicao', 'valor_estimado')
        }),
        ('Observações', {
            'fields': ('observacoes',)
        }),
        ('Controle', {
            'fields': ('criado_em', 'atualizado_em'),
            'classes': ('collapse',),
        }),
    )


@admin.register(Pessoa)
class PessoaAdmin(SimpleHistoryAdmin):
    list_display = ('nome', 'tipo', 'cpf_cnpj', 'email', 'telefone', 'criado_em')
    list_filter = ('tipo',)
    search_fields = ('nome', 'cpf_cnpj', 'email', 'telefone')
    readonly_fields = ('criado_em', 'atualizado_em')
    ordering = ('nome',)
    fieldsets = (
        ('Dados Principais', {
            'fields': ('nome', 'tipo', 'cpf_cnpj'),
            'description': (
                'A categoria é apenas uma referência de busca — a mesma pessoa pode '
                'assumir papéis diferentes (locatário, fiador, imobiliária etc.) em '
                'contratos diferentes, cadastrados em "Partes do Contrato".'
            ),
        }),
        ('Contato', {
            'fields': ('email', 'telefone', 'endereco')
        }),
        ('Observações', {
            'fields': ('observacoes',)
        }),
        ('Controle', {
            'fields': ('criado_em', 'atualizado_em'),
            'classes': ('collapse',),
        }),
    )


class ContratoParteInline(admin.TabularInline):
    model = ContratoParte
    extra = 1
    fields = ('pessoa', 'papel', 'principal', 'observacoes')
    raw_id_fields = ('pessoa',)


class EncargoContratoInline(admin.TabularInline):
    model = EncargoContrato
    extra = 0
    fields = ('tipo', 'descricao', 'valor', 'periodicidade', 'data_inicio_cobranca', 'data_fim_cobranca', 'ativo')


class ReajusteContratoInline(admin.TabularInline):
    model = ReajusteContrato
    extra = 0
    fields = ('data_reajuste', 'indice', 'percentual_aplicado', 'valor_anterior', 'valor_novo', 'aplicado', 'observacoes')


class DocumentoContratoInline(admin.TabularInline):
    """Permite anexar documentos (contrato assinado, aditivos, vistoria) direto no cadastro do contrato."""
    model = Documento
    fk_name = 'contrato'
    fields = ('titulo', 'tipo', 'arquivo', 'data_documento')
    extra = 0
    verbose_name = 'Documento do Contrato'
    verbose_name_plural = 'Documentos do Contrato'


@admin.register(Contrato)
class ContratoAdmin(SimpleHistoryAdmin):
    list_display = (
        'imovel', 'locatarios_display', 'vigencia_display', 'valor_aluguel',
        'status', 'indice_reajuste',
    )
    list_filter = ('status', 'indice_reajuste', 'tipo_garantia', 'prazo_indeterminado')
    search_fields = (
        'imovel__nome', 'locatario__nome', 'fiador__nome',
        'partes__pessoa__nome',
    )
    date_hierarchy = 'data_inicio'
    readonly_fields = ('criado_em', 'atualizado_em')
    ordering = ('-data_inicio',)
    raw_id_fields = ('imovel', 'locatario', 'fiador', 'imobiliaria')
    actions = ['gerar_receitas_esperadas', 'garantir_encargo_aluguel_action', 'sugerir_reajuste_indice']
    inlines = [ContratoParteInline, EncargoContratoInline, ReajusteContratoInline, DocumentoContratoInline]

    @admin.action(description='Garantir encargo de aluguel')
    def garantir_encargo_aluguel_action(self, request, queryset):
        total = 0
        for contrato in queryset:
            if contrato.garantir_encargo_aluguel() is not None:
                total += 1
        if total:
            self.message_user(request, f'{total} encargo(s) de aluguel criado(s).', messages.SUCCESS)
        else:
            self.message_user(request, 'Nenhum encargo de aluguel precisou ser criado.', messages.WARNING)

    @admin.action(description='Sugerir reajuste pelo índice acumulado 12m (Banco Central)')
    def sugerir_reajuste_indice(self, request, queryset):
        """
        Consulta o acumulado de 12 meses do índice do contrato na API do
        Banco Central e cria um ReajusteContrato pendente (aplicado=False)
        para revisão. Não altera valores vigentes.
        """
        from .indices import variacao_acumulada_12m, IndiceIndisponivelError, SERIES_SGS

        hoje = timezone.localdate()
        criados = 0
        sem_indice = 0
        ja_pendentes = 0
        inativos = 0

        for contrato in queryset:
            if contrato.status != 'ativo':
                inativos += 1
                continue
            if contrato.indice_reajuste not in SERIES_SGS:
                sem_indice += 1
                continue
            if contrato.reajustes.filter(aplicado=False).exists():
                ja_pendentes += 1
                continue

            try:
                resultado = variacao_acumulada_12m(contrato.indice_reajuste)
            except IndiceIndisponivelError as exc:
                self.message_user(request, str(exc), messages.ERROR)
                return

            percentual = resultado['percentual']
            valor_novo = (
                contrato.valor_aluguel * (Decimal('1') + percentual / Decimal('100'))
            ).quantize(Decimal('0.01'))

            ReajusteContrato.objects.create(
                contrato=contrato,
                data_reajuste=contrato.data_proximo_reajuste or hoje,
                indice=contrato.indice_reajuste,
                percentual_aplicado=percentual,
                valor_anterior=contrato.valor_aluguel,
                valor_novo=valor_novo,
                aplicado=False,
                observacoes=(
                    f'Sugestão automática — {contrato.get_indice_reajuste_display()} acumulado '
                    f'{resultado["inicio"]} a {resultado["fim"]}: {percentual}% (fonte: Banco Central/SGS). '
                    'Revise e marque "Aplicado" para efetivar.'
                ),
            )
            criados += 1

        if criados:
            self.message_user(
                request,
                f'{criados} sugestão(ões) de reajuste criada(s) — revise na seção '
                '"Reajustes de Contrato" e marque "Aplicado" para efetivar.',
                messages.SUCCESS,
            )
        if ja_pendentes:
            self.message_user(
                request,
                f'{ja_pendentes} contrato(s) ignorado(s): já possuem reajuste pendente de aplicação.',
                messages.WARNING,
            )
        if sem_indice:
            self.message_user(
                request,
                f'{sem_indice} contrato(s) ignorado(s): índice sem série no Banco Central (fixo/outro).',
                messages.WARNING,
            )
        if inativos:
            self.message_user(request, f'{inativos} contrato(s) ignorado(s) por não estarem ativos.', messages.WARNING)

    @admin.action(description='Gerar receitas esperadas')
    def gerar_receitas_esperadas(self, request, queryset):
        from financeiro.services import gerar_receitas_para_contrato

        total_criadas = 0
        total_existiam = 0
        ignorados = 0

        for contrato in queryset:
            if contrato.status != 'ativo':
                ignorados += 1
                continue
            criadas, existiam = gerar_receitas_para_contrato(contrato)
            total_criadas += criadas
            total_existiam += existiam

        if total_criadas:
            self.message_user(request, f'{total_criadas} receita(s) gerada(s) com sucesso.', messages.SUCCESS)
        if total_existiam:
            self.message_user(request, f'{total_existiam} receita(s) já existiam e foram ignoradas.', messages.WARNING)
        if ignorados:
            self.message_user(
                request,
                f'{ignorados} contrato(s) ignorado(s) por não estar com status ativo.',
                messages.ERROR,
            )
        if not total_criadas and not total_existiam and not ignorados:
            self.message_user(request, 'Nenhuma receita para gerar.', messages.WARNING)

    fieldsets = (
        ('Imóvel e Vigência', {
            'fields': (
                'imovel', 'data_inicio', 'data_fim', 'prazo_indeterminado',
                'data_encerramento_real', 'status',
            )
        }),
        ('Partes (campos legados)', {
            'fields': ('locatario', 'fiador', 'imobiliaria'),
            'description': (
                'Mantidos por compatibilidade com contratos já cadastrados. '
                'Para múltiplos locatários, fiadores ou representantes, use a '
                'seção "Partes do Contrato" logo abaixo.'
            ),
        }),
        ('Valores', {
            'fields': ('valor_aluguel', 'dia_vencimento')
        }),
        ('Reajuste e Garantia', {
            'fields': ('indice_reajuste', 'data_proximo_reajuste', 'tipo_garantia', 'comissao_imobiliaria_percentual')
        }),
        ('Multa e Juros por Atraso', {
            'fields': ('multa_atraso_percentual', 'juros_mora_percentual_mes', 'dias_carencia_multa')
        }),
        ('Observações', {
            'fields': ('observacoes',)
        }),
        ('Controle', {
            'fields': ('criado_em', 'atualizado_em'),
            'classes': ('collapse',),
        }),
    )

    def save_formset(self, request, form, formset, change):
        if formset.model is ReajusteContrato:
            instances = formset.save(commit=False)
            for obj in instances:
                aplicado_anterior = False
                if obj.pk is not None:
                    aplicado_anterior = ReajusteContrato.objects.filter(pk=obj.pk).values_list(
                        'aplicado', flat=True
                    ).first() or False
                obj.save()
                # Aplica quando o reajuste é novo com aplicado=True, ou quando
                # um reajuste existente muda de aplicado=False para True.
                # Um reajuste já aplicado nunca é reaplicado.
                if obj.aplicado and not aplicado_anterior:
                    obj.aplicar()
            for obj in formset.deleted_objects:
                obj.delete()
            formset.save_m2m()
        elif formset.model is Documento:
            instances = formset.save(commit=False)
            for obj in instances:
                if not obj.imovel_id:
                    obj.imovel_id = form.instance.imovel_id
                obj.save()
            for obj in formset.deleted_objects:
                obj.delete()
            formset.save_m2m()
        else:
            formset.save()

    def save_related(self, request, form, formsets, change):
        # Os inlines (incluindo Encargos do Contrato) só terminam de ser
        # salvos aqui. Chamar garantir_encargo_aluguel() em save_model()
        # rodaria antes do inline salvar um encargo de aluguel manual,
        # podendo duplicá-lo — por isso a garantia roda só depois.
        super().save_related(request, form, formsets, change)
        form.instance.garantir_encargo_aluguel()


@admin.register(Manutencao)
class ManutencaoAdmin(SimpleHistoryAdmin):
    list_display = ('descricao', 'imovel', 'categoria', 'fornecedor', 'status', 'data_solicitacao', 'valor_final')
    list_filter = ('status', 'categoria', 'imovel')
    search_fields = ('descricao', 'imovel__nome', 'fornecedor__nome')
    date_hierarchy = 'data_solicitacao'
    readonly_fields = ('criado_em', 'atualizado_em')
    ordering = ('-data_solicitacao',)
    fieldsets = (
        ('Identificação', {
            'fields': ('imovel', 'descricao', 'categoria', 'status')
        }),
        ('Fornecedor', {
            'fields': ('fornecedor',)
        }),
        ('Datas e Valores', {
            'fields': ('data_solicitacao', 'data_conclusao', 'valor_estimado', 'valor_final')
        }),
        ('Observações', {
            'fields': ('observacoes',)
        }),
        ('Controle', {
            'fields': ('criado_em', 'atualizado_em'),
            'classes': ('collapse',),
        }),
    )
