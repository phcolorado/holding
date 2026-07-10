from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone
from simple_history.models import HistoricalRecords
from patrimonio.models import Imovel, Pessoa, Contrato

MESES = [
    (1, 'Janeiro'), (2, 'Fevereiro'), (3, 'Março'), (4, 'Abril'),
    (5, 'Maio'), (6, 'Junho'), (7, 'Julho'), (8, 'Agosto'),
    (9, 'Setembro'), (10, 'Outubro'), (11, 'Novembro'), (12, 'Dezembro'),
]


class ReceitaAluguel(models.Model):
    STATUS_CHOICES = [
        ('previsto', 'Previsto'),
        ('recebido', 'Recebido'),
        ('atrasado', 'Atrasado'),
        ('parcial', 'Parcial'),
        ('cancelado', 'Cancelado'),
    ]
    # Status que encerram a cobrança. Receita 'parcial' NÃO é quitada:
    # continua em aberto (inadimplência, checklist, conciliação) até que
    # valor_recebido cubra o valor total devido.
    STATUS_QUITADOS = frozenset({'recebido', 'cancelado'})

    MESES = MESES

    contrato = models.ForeignKey(Contrato, on_delete=models.PROTECT, verbose_name='Contrato')
    imovel = models.ForeignKey(
        Imovel, on_delete=models.PROTECT, verbose_name='Imóvel',
        help_text='Preenchido automaticamente pelo contrato. Não altere manualmente.',
    )
    competencia_mes = models.PositiveSmallIntegerField('Mês de Competência', choices=MESES)
    competencia_ano = models.PositiveSmallIntegerField('Ano de Competência')
    data_vencimento = models.DateField('Data de Vencimento')
    valor_previsto = models.DecimalField('Valor Previsto (R$)', max_digits=12, decimal_places=2)
    valor_recebido = models.DecimalField('Valor Recebido (R$)', max_digits=12, decimal_places=2, null=True, blank=True)
    data_recebimento = models.DateField('Data de Recebimento', null=True, blank=True)
    status = models.CharField('Status', max_length=15, choices=STATUS_CHOICES, default='previsto')
    multa = models.DecimalField('Multa (R$)', max_digits=10, decimal_places=2, default=0)
    juros = models.DecimalField('Juros (R$)', max_digits=10, decimal_places=2, default=0)
    desconto = models.DecimalField('Desconto (R$)', max_digits=10, decimal_places=2, default=0)
    observacoes = models.TextField('Observações', blank=True)
    criado_em = models.DateTimeField('Criado em', auto_now_add=True)
    atualizado_em = models.DateTimeField('Atualizado em', auto_now=True)
    history = HistoricalRecords()

    class Meta:
        verbose_name = 'Receita de Aluguel'
        verbose_name_plural = 'Receitas de Aluguel'
        ordering = ['-competencia_ano', '-competencia_mes']
        unique_together = [['contrato', 'competencia_mes', 'competencia_ano']]

    def __str__(self):
        return f'{self.imovel.nome} — {self.get_competencia_mes_display()}/{self.competencia_ano} ({self.get_status_display()})'

    @property
    def valor_total_devido(self):
        """Valor exigível: previsto + multa + juros − desconto."""
        return (
            self.valor_previsto
            + (self.multa or Decimal('0'))
            + (self.juros or Decimal('0'))
            - (self.desconto or Decimal('0'))
        )

    @property
    def saldo_em_aberto(self):
        """Quanto ainda falta receber. Cancelada não tem saldo exigível."""
        if self.status == 'cancelado':
            return Decimal('0.00')
        saldo = self.valor_total_devido - (self.valor_recebido or Decimal('0'))
        return saldo if saldo > 0 else Decimal('0.00')

    @property
    def esta_quitada(self):
        """Quitada quando cancelada ou quando o recebido cobre o total devido."""
        if self.status == 'cancelado':
            return True
        return (self.valor_recebido or Decimal('0')) >= self.valor_total_devido

    @property
    def esta_atrasada(self):
        """
        Retorna True se a receita está vencida e não foi quitada/cancelada.
        Receita parcial vencida continua atrasada (tem saldo em aberto).
        """
        return (
            self.status not in self.STATUS_QUITADOS
            and not self.esta_quitada
            and self.data_vencimento < timezone.localdate()
        )

    def clean(self):
        if self.contrato_id and self.imovel_id:
            if self.imovel_id != self.contrato.imovel_id:
                raise ValidationError({'imovel': 'O imóvel deve ser o mesmo imóvel do contrato.'})

    def save(self, *args, **kwargs):
        # Garante que imovel é sempre o imóvel do contrato — nunca permite inconsistência
        if self.contrato_id:
            self.imovel_id = self.contrato.imovel_id
        super().save(*args, **kwargs)

    def verificar_atraso(self):
        """Atualiza o campo status para 'atrasado' se vencido e não quitado."""
        if self.esta_atrasada and self.status == 'previsto':
            self.status = 'atrasado'
            self.save(update_fields=['status'])

    def garantir_recebimento_legado(self):
        """
        Materializa como RecebimentoReceita um valor_recebido consolidado que
        exista sem recebimentos (dado legado ou editado diretamente) — evita
        que o recálculo apague o histórico ao registrar um novo recebimento.
        """
        if (self.valor_recebido or Decimal('0')) > 0 and not self.recebimentos.exists():
            legado = RecebimentoReceita(
                receita=self,
                data_recebimento=self.data_recebimento or self.data_vencimento,
                valor=self.valor_recebido,
                origem='migracao',
                observacoes='Recebimento consolidado preexistente, materializado automaticamente.',
            )
            legado._eh_legado = True
            legado.save()

    def recalcular_recebimentos(self):
        """
        Reconsolida valor_recebido/data_recebimento/status a partir dos
        RecebimentoReceita vinculados. Chamada sempre que um recebimento é
        criado, alterado ou excluído. Nunca mexe em receita cancelada.
        """
        if self.status == 'cancelado':
            return
        agregados = self.recebimentos.aggregate(
            total=models.Sum('valor'), ultima=models.Max('data_recebimento')
        )
        total = agregados['total'] or Decimal('0.00')
        self.valor_recebido = total if total > 0 else None
        self.data_recebimento = agregados['ultima']
        if total >= self.valor_total_devido and total > 0:
            self.status = 'recebido'
        elif total > 0:
            self.status = 'parcial'
        else:
            self.status = 'atrasado' if self.data_vencimento < timezone.localdate() else 'previsto'
        self.save(update_fields=['valor_recebido', 'data_recebimento', 'status'])

    def calcular_multa_juros(self, data_recebimento=None):
        """
        Sugestão de multa/juros por atraso com base nas regras do contrato.
        Apenas calcula e retorna — nunca altera o registro automaticamente.
        """
        data_ref = data_recebimento or timezone.localdate()
        contrato = self.contrato
        dias_atraso = (data_ref - self.data_vencimento).days

        if dias_atraso <= contrato.dias_carencia_multa:
            return {'multa': Decimal('0.00'), 'juros': Decimal('0.00'), 'dias_atraso': 0}

        base = self.valor_previsto
        multa = (base * contrato.multa_atraso_percentual / Decimal('100')).quantize(Decimal('0.01'))
        juros = (
            base * contrato.juros_mora_percentual_mes / Decimal('100') / Decimal('30') * dias_atraso
        ).quantize(Decimal('0.01'))
        return {'multa': multa, 'juros': juros, 'dias_atraso': dias_atraso}


class RecebimentoReceita(models.Model):
    """
    Um recebimento individual de uma receita — permite vários pagamentos por
    receita (parciais, repasses em datas diferentes). Os campos consolidados
    de ReceitaAluguel (valor_recebido/data_recebimento/status) permanecem por
    compatibilidade e são recalculados a cada mudança aqui.
    """
    ORIGEM_CHOICES = [
        ('manual', 'Manual'),
        ('conciliacao', 'Conciliação Bancária'),
        ('migracao', 'Migração (histórico)'),
    ]

    receita = models.ForeignKey(
        ReceitaAluguel, on_delete=models.CASCADE, related_name='recebimentos', verbose_name='Receita'
    )
    data_recebimento = models.DateField('Data do Recebimento')
    valor = models.DecimalField('Valor (R$)', max_digits=12, decimal_places=2)
    transacao_extrato = models.ForeignKey(
        'conciliacao.TransacaoExtrato', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='recebimentos', verbose_name='Transação do Extrato',
    )
    origem = models.CharField('Origem', max_length=15, choices=ORIGEM_CHOICES, default='manual')
    observacoes = models.TextField('Observações', blank=True)
    criado_em = models.DateTimeField('Criado em', auto_now_add=True)
    criado_por = models.ForeignKey(
        'auth.User', on_delete=models.SET_NULL, null=True, blank=True, verbose_name='Criado por'
    )
    history = HistoricalRecords()

    class Meta:
        verbose_name = 'Recebimento da Receita'
        verbose_name_plural = 'Recebimentos da Receita'
        ordering = ['data_recebimento', 'id']

    def __str__(self):
        return f'R$ {self.valor} em {self.data_recebimento:%d/%m/%Y} — {self.receita}'

    def clean(self):
        if self.valor is not None and self.valor <= 0:
            raise ValidationError({'valor': 'O valor do recebimento deve ser maior que zero.'})
        if self.valor is not None and self.receita_id:
            saldo = self.receita.saldo_em_aberto
            if self.pk:
                anterior = RecebimentoReceita.objects.filter(pk=self.pk).values_list('valor', flat=True).first()
                saldo += anterior or Decimal('0')
            if self.valor > saldo:
                raise ValidationError({
                    'valor': f'O valor excede o saldo em aberto da receita (R$ {saldo}).'
                })

    def save(self, *args, **kwargs):
        # Antes do primeiro recebimento novo, preserva o consolidado legado
        # (valor_recebido sem recebimentos) como recebimento histórico.
        if self._state.adding and self.receita_id and not getattr(self, '_eh_legado', False):
            self.receita.garantir_recebimento_legado()
        super().save(*args, **kwargs)
        self.receita.recalcular_recebimentos()

    def delete(self, *args, **kwargs):
        receita = self.receita
        super().delete(*args, **kwargs)
        receita.recalcular_recebimentos()


class ReceitaAluguelItem(models.Model):
    receita = models.ForeignKey(ReceitaAluguel, on_delete=models.CASCADE, related_name='itens', verbose_name='Receita')
    tipo = models.CharField('Tipo', max_length=30)
    descricao = models.CharField('Descrição', max_length=200, blank=True)
    valor = models.DecimalField('Valor (R$)', max_digits=12, decimal_places=2)

    class Meta:
        verbose_name = 'Item da Receita'
        verbose_name_plural = 'Itens da Receita'
        ordering = ['id']

    def __str__(self):
        return f'{self.descricao or self.tipo} — R$ {self.valor}'


class Despesa(models.Model):
    CATEGORIA_CHOICES = [
        ('iptu', 'IPTU'),
        ('condominio', 'Condomínio'),
        ('manutencao', 'Manutenção'),
        ('seguro', 'Seguro'),
        ('taxa_bancaria', 'Taxa Bancária'),
        ('contabilidade', 'Contabilidade'),
        ('advocacia', 'Advocacia'),
        ('comissao_imobiliaria', 'Comissão Imobiliária'),
        ('obra_reforma', 'Obra / Reforma'),
        ('outro', 'Outro'),
    ]
    STATUS_CHOICES = [
        ('prevista', 'Prevista'),
        ('paga', 'Paga'),
        ('atrasada', 'Atrasada'),
        ('cancelada', 'Cancelada'),
    ]
    # Statuses that indicate the expense is settled
    STATUS_ENCERRADOS = frozenset({'paga', 'cancelada'})

    MESES = MESES

    imovel = models.ForeignKey(Imovel, on_delete=models.PROTECT, null=True, blank=True, verbose_name='Imóvel')
    contrato = models.ForeignKey(
        Contrato, on_delete=models.SET_NULL, null=True, blank=True, verbose_name='Contrato',
        related_name='despesas',
    )
    receita = models.ForeignKey(
        ReceitaAluguel, on_delete=models.SET_NULL, null=True, blank=True, verbose_name='Receita de Aluguel Vinculada',
        related_name='despesas_vinculadas',
    )
    origem_automatica = models.BooleanField(
        'Gerada Automaticamente', default=False,
        help_text='Marcado pelo sistema quando a despesa é criada automaticamente (ex.: taxa de administração).',
    )
    categoria = models.CharField('Categoria', max_length=30, choices=CATEGORIA_CHOICES, default='outro')
    fornecedor = models.ForeignKey(
        Pessoa, on_delete=models.SET_NULL, null=True, blank=True, verbose_name='Fornecedor / Credor'
    )
    descricao = models.CharField('Descrição', max_length=300)
    competencia_mes = models.PositiveSmallIntegerField('Mês de Competência', choices=MESES, null=True, blank=True)
    competencia_ano = models.PositiveSmallIntegerField('Ano de Competência', null=True, blank=True)
    data_vencimento = models.DateField('Data de Vencimento')
    valor = models.DecimalField('Valor (R$)', max_digits=12, decimal_places=2)
    data_pagamento = models.DateField('Data de Pagamento', null=True, blank=True)
    status = models.CharField('Status', max_length=15, choices=STATUS_CHOICES, default='prevista')
    observacoes = models.TextField('Observações', blank=True)
    criado_em = models.DateTimeField('Criado em', auto_now_add=True)
    atualizado_em = models.DateTimeField('Atualizado em', auto_now=True)
    history = HistoricalRecords()

    class Meta:
        verbose_name = 'Despesa'
        verbose_name_plural = 'Despesas'
        ordering = ['-data_vencimento']

    def __str__(self):
        imovel_str = self.imovel.nome if self.imovel else 'Geral'
        return f'{self.get_categoria_display()} — {imovel_str} — R$ {self.valor}'

    @property
    def esta_atrasada(self):
        """
        Retorna True se a despesa está vencida e não foi paga/cancelada.
        Independe do valor do campo status — calcula pela data de vencimento.
        """
        return (
            self.status not in self.STATUS_ENCERRADOS
            and self.data_vencimento < timezone.localdate()
        )

    def verificar_atraso(self):
        """Atualiza o campo status para 'atrasada' se vencida e não paga."""
        if self.esta_atrasada and self.status == 'prevista':
            self.status = 'atrasada'
            self.save(update_fields=['status'])


class FechamentoMensal(models.Model):
    MESES = MESES

    mes = models.PositiveSmallIntegerField('Mês', choices=MESES)
    ano = models.PositiveSmallIntegerField('Ano')
    data_fechamento = models.DateField('Data do Fechamento', null=True, blank=True)
    observacoes = models.TextField('Observações', blank=True)
    enviado_contabilidade = models.BooleanField('Enviado à Contabilidade', default=False)
    data_envio_contabilidade = models.DateField('Data de Envio à Contabilidade', null=True, blank=True)
    criado_em = models.DateTimeField('Criado em', auto_now_add=True)
    atualizado_em = models.DateTimeField('Atualizado em', auto_now=True)
    history = HistoricalRecords()

    class Meta:
        verbose_name = 'Fechamento Mensal'
        verbose_name_plural = 'Fechamentos Mensais'
        ordering = ['-ano', '-mes']
        unique_together = [['mes', 'ano']]

    def __str__(self):
        return f'Fechamento {self.get_mes_display()}/{self.ano}'


def receitas_inadimplentes_qs():
    """
    Retorna QuerySet de receitas vencidas e não quitadas.
    Usa regra de vencimento — independe do campo status.
    """
    return ReceitaAluguel.objects.filter(
        data_vencimento__lt=timezone.localdate()
    ).exclude(
        status__in=ReceitaAluguel.STATUS_QUITADOS
    )
