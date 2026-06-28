from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone
from patrimonio.models import Imovel, Pessoa, Contrato


class ReceitaAluguel(models.Model):
    STATUS_CHOICES = [
        ('previsto', 'Previsto'),
        ('recebido', 'Recebido'),
        ('atrasado', 'Atrasado'),
        ('parcial', 'Parcial'),
        ('cancelado', 'Cancelado'),
    ]
    # Statuses that indicate the receipt is settled (not considered overdue)
    STATUS_QUITADOS = frozenset({'recebido', 'parcial', 'cancelado'})

    MESES = [
        (1, 'Janeiro'), (2, 'Fevereiro'), (3, 'Março'), (4, 'Abril'),
        (5, 'Maio'), (6, 'Junho'), (7, 'Julho'), (8, 'Agosto'),
        (9, 'Setembro'), (10, 'Outubro'), (11, 'Novembro'), (12, 'Dezembro'),
    ]

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

    class Meta:
        verbose_name = 'Receita de Aluguel'
        verbose_name_plural = 'Receitas de Aluguel'
        ordering = ['-competencia_ano', '-competencia_mes']
        unique_together = [['contrato', 'competencia_mes', 'competencia_ano']]

    def __str__(self):
        return f'{self.imovel.nome} — {self.get_competencia_mes_display()}/{self.competencia_ano} ({self.get_status_display()})'

    @property
    def esta_atrasada(self):
        """
        Retorna True se a receita está vencida e não foi quitada/cancelada.
        Independe do valor do campo status — calcula pela data de vencimento.
        """
        return (
            self.status not in self.STATUS_QUITADOS
            and self.data_vencimento < timezone.now().date()
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

    MESES = [
        (1, 'Janeiro'), (2, 'Fevereiro'), (3, 'Março'), (4, 'Abril'),
        (5, 'Maio'), (6, 'Junho'), (7, 'Julho'), (8, 'Agosto'),
        (9, 'Setembro'), (10, 'Outubro'), (11, 'Novembro'), (12, 'Dezembro'),
    ]

    imovel = models.ForeignKey(Imovel, on_delete=models.PROTECT, null=True, blank=True, verbose_name='Imóvel')
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
            and self.data_vencimento < timezone.now().date()
        )

    def verificar_atraso(self):
        """Atualiza o campo status para 'atrasada' se vencida e não paga."""
        if self.esta_atrasada and self.status == 'prevista':
            self.status = 'atrasada'
            self.save(update_fields=['status'])


class FechamentoMensal(models.Model):
    MESES = [
        (1, 'Janeiro'), (2, 'Fevereiro'), (3, 'Março'), (4, 'Abril'),
        (5, 'Maio'), (6, 'Junho'), (7, 'Julho'), (8, 'Agosto'),
        (9, 'Setembro'), (10, 'Outubro'), (11, 'Novembro'), (12, 'Dezembro'),
    ]

    mes = models.PositiveSmallIntegerField('Mês', choices=MESES)
    ano = models.PositiveSmallIntegerField('Ano')
    data_fechamento = models.DateField('Data do Fechamento', null=True, blank=True)
    observacoes = models.TextField('Observações', blank=True)
    enviado_contabilidade = models.BooleanField('Enviado à Contabilidade', default=False)
    data_envio_contabilidade = models.DateField('Data de Envio à Contabilidade', null=True, blank=True)
    criado_em = models.DateTimeField('Criado em', auto_now_add=True)
    atualizado_em = models.DateTimeField('Atualizado em', auto_now=True)

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
        data_vencimento__lt=timezone.now().date()
    ).exclude(
        status__in=ReceitaAluguel.STATUS_QUITADOS
    )
