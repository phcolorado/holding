from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.validators import FileExtensionValidator
from django.db import models
from django.db.models import Q
from simple_history.models import HistoricalRecords

from financeiro.models import ReceitaAluguel, Despesa
from patrimonio.models import Imovel, Pessoa


def upload_extrato_path(instance, filename):
    conta_id = instance.conta_id or 'sem_conta'
    return f'extratos/conta_{conta_id}/{filename}'


class ContaBancaria(models.Model):
    nome = models.CharField('Nome / Apelido', max_length=100, help_text='Ex.: "Itaú PJ Holding".')
    banco = models.CharField('Banco', max_length=100, blank=True)
    bank_id = models.CharField(
        'Código do Banco (OFX)', max_length=20, blank=True,
        help_text='Código BANKID que aparece no arquivo OFX (ex.: 0341). Se preenchido, é conferido na importação.',
    )
    agencia = models.CharField('Agência', max_length=20, blank=True)
    numero_conta = models.CharField(
        'Número da Conta', max_length=30, blank=True,
        help_text='Se preenchido, é conferido (só dígitos) com o ACCTID do arquivo OFX na importação.',
    )
    ativo = models.BooleanField('Ativa', default=True)
    observacoes = models.TextField('Observações', blank=True)
    criado_em = models.DateTimeField('Criado em', auto_now_add=True)
    atualizado_em = models.DateTimeField('Atualizado em', auto_now=True)
    history = HistoricalRecords()

    class Meta:
        verbose_name = 'Conta Bancária'
        verbose_name_plural = 'Contas Bancárias'
        ordering = ['nome']

    def __str__(self):
        return self.nome


class ExtratoImportadoQuerySet(models.QuerySet):
    """
    QuerySet.delete() em massa nunca é permitido — a exclusão segura de um
    extrato exige exclusão explícita das transações pendentes ANTES do
    extrato (ver conciliacao.services.excluir_extrato_sem_movimentacoes),
    algo que uma exclusão em massa/genérica nunca poderia fazer com
    segurança. Não há caminho legítimo que precise disto.
    """
    def delete(self):
        raise ValidationError(
            'Exclusão em massa (QuerySet.delete()) de ExtratoImportado não é permitida — '
            'exclua um extrato de cada vez pelo fluxo seguro (excluir_extrato_sem_movimentacoes).'
        )


class ExtratoImportado(models.Model):
    objects = ExtratoImportadoQuerySet.as_manager()

    conta = models.ForeignKey(
        ContaBancaria, on_delete=models.PROTECT, related_name='extratos', verbose_name='Conta'
    )
    arquivo = models.FileField(
        'Arquivo OFX', upload_to=upload_extrato_path,
        validators=[FileExtensionValidator(allowed_extensions=['ofx', 'qfx'])],
    )
    hash_arquivo = models.CharField(
        'Hash do Arquivo', max_length=64, unique=True,
        help_text='SHA-256 do conteúdo — impede importar o mesmo arquivo duas vezes.',
    )
    periodo_inicio = models.DateField('Início do Período', null=True, blank=True)
    periodo_fim = models.DateField('Fim do Período', null=True, blank=True)
    transacoes_novas = models.PositiveIntegerField('Transações Novas', default=0)
    transacoes_duplicadas = models.PositiveIntegerField('Transações Duplicadas (ignoradas)', default=0)
    importado_em = models.DateTimeField('Importado em', auto_now_add=True)
    importado_por = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, verbose_name='Importado por'
    )
    history = HistoricalRecords()

    class Meta:
        verbose_name = 'Extrato Importado'
        verbose_name_plural = 'Extratos Importados'
        ordering = ['-importado_em']

    def __str__(self):
        return f'Extrato {self.conta} — {self.importado_em:%d/%m/%Y %H:%M}'

    @property
    def pendentes(self):
        return self.transacoes.filter(status='pendente').count()

    def delete(self, *args, **kwargs):
        # Bloqueado, salvo quando chamado pelo service seguro (que já
        # validou extrato_pode_ser_excluido() e excluiu explicitamente as
        # transações pendentes antes de chegar aqui) — o sinalizador é
        # privado e só é setado por excluir_extrato_sem_movimentacoes().
        if not getattr(self, '_exclusao_via_service_seguro', False):
            raise ValidationError(
                'A exclusão direta de ExtratoImportado não é permitida — use o fluxo seguro '
                '(conciliacao.services.excluir_extrato_sem_movimentacoes), que só permite '
                'extratos totalmente pendentes.'
            )
        super().delete(*args, **kwargs)


class TransacaoExtratoQuerySet(models.QuerySet):
    """
    Exclusão em massa de TransacaoExtrato NUNCA é permitida (item 4, rodada
    de fechamento estrutural) — nem mesmo quando todas as transações do
    conjunto estão pendentes e sem vínculo algum. Excluir uma transação
    importada, isoladamente, deixaria os metadados do extrato (contadores
    de novas/duplicadas, período detectado) divergentes do arquivo OFX
    original. A única exclusão possível é a do extrato inteiro (quando
    totalmente pendente), que exclui cada transação INDIVIDUALMENTE via
    TransacaoExtrato.delete() dentro de excluir_extrato_sem_movimentacoes()
    — nunca por QuerySet.delete().
    """
    def delete(self):
        raise ValidationError(
            'Exclusão em massa (QuerySet.delete()) de TransacaoExtrato não é permitida — '
            'nenhuma transação importada pode ser excluída isoladamente, nem mesmo pendente '
            'e sem vínculo. Exclua o extrato inteiro (quando totalmente pendente) pela tela '
            'de Conciliação Bancária.'
        )

    # O PAR despesa × despesa_criada_pela_conciliacao é a prova de origem
    # da despesa de um débito — controlado exclusivamente por
    # lancar_despesa() (marca) e desfazer_conciliacao() (limpa). Nenhuma
    # operação em massa pode tocá-lo: trocar a despesa mantendo o booleano
    # True falsificaria a prova (a nova despesa NÃO foi criada pelo
    # service) e o desfazer excluiria uma despesa manual indevidamente.
    # Bloqueio incondicional por NOME de campo — nunca por exists() sobre o
    # estado do conjunto, que criaria janela de corrida entre a checagem e
    # a escrita.
    CAMPOS_PROTEGIDOS_ORIGEM_DESPESA = frozenset({
        'despesa', 'despesa_id', 'despesa_criada_pela_conciliacao',
    })

    def update(self, **kwargs):
        protegidos = self.CAMPOS_PROTEGIDOS_ORIGEM_DESPESA & set(kwargs)
        if protegidos:
            raise ValidationError(
                f'QuerySet.update() não pode alterar {sorted(protegidos)} — o par '
                'despesa/despesa_criada_pela_conciliacao é controlado exclusivamente por '
                'lancar_despesa()/desfazer_conciliacao().'
            )
        return super().update(**kwargs)

    def bulk_update(self, objs, fields, **kwargs):
        # Mesmo que a implementação atual do Django execute bulk_update via
        # update() (que já bloquearia), a proteção é declarada aqui
        # explicitamente — não depende de detalhe interno de versão.
        protegidos = self.CAMPOS_PROTEGIDOS_ORIGEM_DESPESA & set(fields)
        if protegidos:
            raise ValidationError(
                f'QuerySet.bulk_update() não pode alterar {sorted(protegidos)} — o par '
                'despesa/despesa_criada_pela_conciliacao é controlado exclusivamente por '
                'lancar_despesa()/desfazer_conciliacao().'
            )
        return super().bulk_update(objs, fields, **kwargs)

    def bulk_create(self, objs, **kwargs):
        # bulk_create() não passa por Model.save() — sem esta checagem, uma
        # criação em lote poderia declarar despesa_criada_pela_conciliacao=
        # True para uma despesa que lancar_despesa() nunca criou. A
        # CheckConstraint do banco verifica só coerência ESTRUTURAL
        # (débito + despesa preenchida), não consegue comprovar a origem —
        # por isso True em criação em lote é rejeitado mesmo quando a
        # estrutura estaria válida. Importação normal (campo False, o
        # default) continua funcionando.
        objs = list(objs)
        if any(getattr(obj, 'despesa_criada_pela_conciliacao', False) for obj in objs):
            raise ValidationError(
                'bulk_create() não pode criar TransacaoExtrato com '
                'despesa_criada_pela_conciliacao=True — essa marcação declara que '
                'lancar_despesa() criou a despesa vinculada, o que só o próprio service '
                'pode afirmar.'
            )
        return super().bulk_create(objs, **kwargs)


class TransacaoExtrato(models.Model):
    TIPO_CHOICES = [
        ('credito', 'Crédito'),
        ('debito', 'Débito'),
    ]
    STATUS_CHOICES = [
        ('pendente', 'Pendente'),
        ('conciliada', 'Conciliada'),
        ('ignorada', 'Ignorada'),
    ]

    objects = TransacaoExtratoQuerySet.as_manager()

    extrato = models.ForeignKey(
        # PROTECT (não CASCADE): a exclusão do ExtratoImportado NUNCA deve
        # apagar TransacaoExtrato em cascata — o Collector de exclusão do
        # Django apagaria em lote via SQL direto, contornando totalmente a
        # proteção de TransacaoExtrato.delete()/QuerySet abaixo. O service
        # seguro (excluir_extrato_sem_movimentacoes) exclui explicitamente
        # cada transação pendente ANTES de excluir o extrato.
        ExtratoImportado, on_delete=models.PROTECT, related_name='transacoes', verbose_name='Extrato'
    )
    conta = models.ForeignKey(
        ContaBancaria, on_delete=models.PROTECT, related_name='transacoes', verbose_name='Conta'
    )
    fitid = models.CharField(
        'FITID', max_length=255,
        help_text='Identificador único da transação no OFX — garante que reimportações não dupliquem.',
    )
    data = models.DateField('Data')
    valor = models.DecimalField('Valor (R$)', max_digits=14, decimal_places=2,
                                help_text='Negativo para débitos, como vem no extrato.')
    tipo = models.CharField('Tipo', max_length=10, choices=TIPO_CHOICES)
    descricao = models.CharField('Descrição', max_length=300, blank=True)
    memo = models.CharField('Memo', max_length=300, blank=True)
    status = models.CharField('Status', max_length=12, choices=STATUS_CHOICES, default='pendente')
    comissoes_marcadas = models.BooleanField(
        'Comissões Marcadas como Pagas', default=False,
        help_text='Indica que esta conciliação (repasse líquido) marcou despesas de comissão como pagas — usado ao desfazer.',
    )
    receitas = models.ManyToManyField(
        ReceitaAluguel, through='ConciliacaoReceita', related_name='transacoes_extrato',
        verbose_name='Receitas Conciliadas', blank=True,
    )
    despesa = models.ForeignKey(
        # PROTECT (não SET_NULL): este FK só fica preenchido enquanto a
        # transação está 'conciliada' (lancar_despesa()/desfazer_conciliacao()
        # mantêm essa invariante) — por isso PROTECT nunca bloqueia uma
        # exclusão legítima de despesa, só impede que uma despesa ainda
        # vinculada a um débito conciliado desapareça e deixe a transação
        # 'conciliada' sem despesa (trilha de auditoria quebrada). Para
        # excluir a despesa, desfaça a conciliação primeiro (desvincula).
        Despesa, on_delete=models.PROTECT, null=True, blank=True,
        related_name='transacoes_extrato', verbose_name='Despesa Lançada',
    )
    despesa_criada_pela_conciliacao = models.BooleanField(
        'Despesa Criada pela Conciliação', default=False, editable=False,
        help_text=(
            'True declara que a despesa atualmente vinculada em "despesa" foi criada pelo '
            'próprio lancar_despesa() para esta transação — única prova de origem aceita para '
            'excluí-la automaticamente ao desfazer a conciliação. Nunca inferido por '
            'coincidência de valor/data/status; controlado exclusivamente pelos services de '
            'conciliação (lancar_despesa()/desfazer_conciliacao()).'
        ),
    )
    observacoes = models.TextField('Observações', blank=True)
    criado_em = models.DateTimeField('Criado em', auto_now_add=True)
    atualizado_em = models.DateTimeField('Atualizado em', auto_now=True)
    history = HistoricalRecords()

    class Meta:
        verbose_name = 'Transação do Extrato'
        verbose_name_plural = 'Transações do Extrato'
        ordering = ['data', 'id']
        unique_together = [['conta', 'fitid']]
        constraints = [
            # despesa_criada_pela_conciliacao=True só faz sentido para um
            # débito com despesa vinculada — nunca para crédito, nem sem
            # despesa. Usa os valores reais de TIPO_CHOICES.
            models.CheckConstraint(
                check=(
                    Q(despesa_criada_pela_conciliacao=False)
                    | Q(tipo='debito', despesa__isnull=False)
                ),
                name='transacaoextrato_origem_despesa_exige_debito_vinculado',
            ),
        ]

    def __str__(self):
        return f'{self.data:%d/%m/%Y} — {self.descricao or self.memo} (R$ {self.valor})'

    @property
    def valor_absoluto(self):
        return abs(self.valor)

    def save(self, *args, **kwargs):
        # Integridade do PAR despesa × despesa_criada_pela_conciliacao —
        # decidida sempre pelo estado PERSISTIDO no banco (nunca pelos
        # valores em memória de self, que podem estar desatualizados ou
        # adulterados):
        #
        # 1) Banco com o par MARCADO (True): trocar despesa_id, limpar
        #    despesa_id ou voltar o booleano para False falsificaria a
        #    prova de origem (o booleano passaria a "atestar" uma despesa
        #    que lancar_despesa() nunca criou, ou apagaria a prova) — só
        #    desfazer_conciliacao() pode fazer isso, via o sinalizador
        #    privado _alterando_origem_despesa_via_service. Resalvar sem
        #    tocar no par (ex.: só observações) continua permitido.
        #
        # 2) Banco com o par NÃO marcado (False, ou criação): a transição
        #    para True é exclusiva de lancar_despesa(), via o sinalizador
        #    privado _marcando_origem_despesa.
        #
        # Nenhum dos sinalizadores é argumento de save() — existem apenas
        # na instância, setados imediatamente antes do save() legítimo
        # dentro do respectivo service.
        persistido = (
            TransacaoExtrato.objects.filter(pk=self.pk)
            .values('despesa_id', 'despesa_criada_pela_conciliacao')
            .first()
            if self.pk else None
        )
        if persistido and persistido['despesa_criada_pela_conciliacao']:
            if not getattr(self, '_alterando_origem_despesa_via_service', False):
                if self.despesa_id != persistido['despesa_id']:
                    raise ValidationError({
                        'despesa': (
                            'Esta transação tem a despesa marcada como criada pela '
                            'conciliação — o vínculo não pode ser trocado nem limpo por uma '
                            'alteração comum. Use desfazer_conciliacao().'
                        )
                    })
                if not self.despesa_criada_pela_conciliacao:
                    raise ValidationError({
                        'despesa_criada_pela_conciliacao': (
                            'A prova de origem da despesa não pode ser apagada por uma '
                            'alteração comum — só desfazer_conciliacao() limpa este campo '
                            '(junto com o vínculo e o status, atomicamente).'
                        )
                    })
        elif self.despesa_criada_pela_conciliacao:
            if not getattr(self, '_marcando_origem_despesa', False):
                raise ValidationError({
                    'despesa_criada_pela_conciliacao': (
                        'Este campo só pode ser definido como True pelo service '
                        'lancar_despesa() — não por uma alteração comum (save() direto).'
                    )
                })
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        # Bloqueado, salvo quando chamado pelo service seguro (que já
        # validou extrato_pode_ser_excluido() e bloqueou/reconfirmou cada
        # transação antes de chegar aqui) — sinalizador privado, setado
        # exclusivamente por excluir_extrato_sem_movimentacoes() (item 4,
        # rodada de fechamento estrutural). Nenhuma transação importada pode
        # ser excluída isoladamente, nem mesmo pendente e sem vínculo — isso
        # deixaria os metadados do extrato divergentes do arquivo OFX
        # original. Mesmo padrão de ExtratoImportado.delete().
        if not getattr(self, '_exclusao_via_service_seguro', False):
            raise ValidationError(
                'Transação importada não pode ser excluída diretamente — a única forma de '
                'remover transações é excluir o extrato inteiro (quando totalmente pendente) '
                'pela tela de Conciliação Bancária.'
            )
        super().delete(*args, **kwargs)


class ConciliacaoReceita(models.Model):
    """
    Liga uma transação do extrato às receitas que ela quitou (repasse
    consolidado = várias).

    Semântica de valor_atribuido: é a parcela do CRÉDITO BANCÁRIO (o dinheiro
    que efetivamente entrou na conta) atribuída a esta receita. No repasse
    líquido de imobiliária, portanto, é o valor LÍQUIDO (saldo bruto da
    receita − comissão retida); o RecebimentoReceita correspondente registra
    o BRUTO, porque o inquilino pagou o aluguel integral — a diferença é a
    despesa de comissão, vinculada via ConciliacaoComissao.
    Invariante: soma dos valor_atribuido de uma transação == valor do crédito.
    """
    transacao = models.ForeignKey(
        TransacaoExtrato, on_delete=models.CASCADE, related_name='itens_receita', verbose_name='Transação'
    )
    receita = models.ForeignKey(
        ReceitaAluguel, on_delete=models.PROTECT, related_name='conciliacoes', verbose_name='Receita'
    )
    valor_atribuido = models.DecimalField(
        'Valor Atribuído do Crédito (R$)', max_digits=12, decimal_places=2,
        help_text='Parcela do crédito bancário atribuída a esta receita (líquida de comissão no repasse líquido).',
    )
    criado_em = models.DateTimeField('Criado em', auto_now_add=True)

    class Meta:
        verbose_name = 'Receita Conciliada'
        verbose_name_plural = 'Receitas Conciliadas'
        unique_together = [['transacao', 'receita']]

    def __str__(self):
        return f'{self.receita} ← R$ {self.valor_atribuido}'


class ConciliacaoComissao(models.Model):
    """
    Vínculo explícito entre uma conciliação de repasse líquido e as despesas
    de comissão que ELA marcou como pagas. Ao desfazer a conciliação, apenas
    estas despesas são reabertas — comissões pagas manualmente (ou por outra
    transação) na mesma data nunca são afetadas.
    """
    transacao = models.ForeignKey(
        TransacaoExtrato, on_delete=models.CASCADE, related_name='itens_comissao', verbose_name='Transação'
    )
    despesa = models.ForeignKey(
        # PROTECT (não CASCADE): apagar a despesa não pode levar junto o
        # vínculo de auditoria em silêncio — para editar/excluir uma despesa
        # de comissão reconciliada, desfaça a conciliação primeiro (isso
        # remove o vínculo pelo fluxo normal, não por efeito colateral do FK).
        Despesa, on_delete=models.PROTECT, related_name='conciliacoes_comissao', verbose_name='Despesa de Comissão'
    )
    valor = models.DecimalField('Valor da Comissão (R$)', max_digits=12, decimal_places=2)
    criado_em = models.DateTimeField('Criado em', auto_now_add=True)

    class Meta:
        verbose_name = 'Comissão Conciliada'
        verbose_name_plural = 'Comissões Conciliadas'
        constraints = [
            models.UniqueConstraint(
                fields=['transacao', 'despesa'],
                name='conciliacaocomissao_transacao_despesa_unica',
            ),
        ]

    def __str__(self):
        return f'{self.despesa} ← transação {self.transacao_id} (R$ {self.valor})'


class RegraClassificacao(models.Model):
    """Regra para sugerir a classificação de débitos do extrato como despesas."""
    texto_contem = models.CharField(
        'Descrição Contém', max_length=100,
        help_text='Trecho procurado (sem diferenciar maiúsculas) na descrição/memo da transação. Ex.: "CEMIG".',
    )
    categoria = models.CharField('Categoria da Despesa', max_length=30, choices=Despesa.CATEGORIA_CHOICES)
    fornecedor = models.ForeignKey(
        Pessoa, on_delete=models.SET_NULL, null=True, blank=True, verbose_name='Fornecedor'
    )
    imovel = models.ForeignKey(
        Imovel, on_delete=models.SET_NULL, null=True, blank=True, verbose_name='Imóvel'
    )
    prioridade = models.PositiveIntegerField(
        'Prioridade', default=100, help_text='Menor número = avaliada primeiro.'
    )
    ativo = models.BooleanField('Ativa', default=True)
    criado_em = models.DateTimeField('Criado em', auto_now_add=True)
    atualizado_em = models.DateTimeField('Atualizado em', auto_now=True)
    history = HistoricalRecords()

    class Meta:
        verbose_name = 'Regra de Classificação'
        verbose_name_plural = 'Regras de Classificação'
        ordering = ['prioridade', 'texto_contem']

    def __str__(self):
        return f'"{self.texto_contem}" → {self.get_categoria_display()}'

    def aplica_a(self, transacao):
        texto = f'{transacao.descricao} {transacao.memo}'.lower()
        return self.texto_contem.lower() in texto
