from datetime import timedelta

from django.core.exceptions import ValidationError
from django.core.validators import FileExtensionValidator
from django.db import models
from django.utils import timezone
from simple_history.models import HistoricalRecords
from patrimonio.models import Imovel, Pessoa, Contrato
from financeiro.models import ReceitaAluguel, Despesa

EXTENSOES_PERMITIDAS = [
    'pdf', 'jpg', 'jpeg', 'png', 'webp',
    'doc', 'docx', 'odt', 'xls', 'xlsx', 'ods', 'csv', 'txt', 'zip',
]
TAMANHO_MAXIMO_UPLOAD_MB = 25

# Janela (em dias) usada nos alertas de vencimento de validade de documentos.
DIAS_ALERTA_VALIDADE = 30


def upload_documento_path(instance, filename):
    imovel_id = instance.imovel.pk if instance.imovel else 'geral'
    tipo = instance.tipo or 'outro'
    return f'documentos/imovel_{imovel_id}/{tipo}/{filename}'


def validar_tamanho_arquivo(arquivo):
    limite = TAMANHO_MAXIMO_UPLOAD_MB * 1024 * 1024
    if arquivo.size > limite:
        raise ValidationError(
            f'Arquivo maior que o limite de {TAMANHO_MAXIMO_UPLOAD_MB} MB.'
        )


class DocumentoObrigatorio(models.Model):
    TIPO_CHOICES = [
        ('matricula', 'Matrícula'),
        ('iptu', 'IPTU'),
        ('contrato_atual', 'Contrato Atual'),
        ('laudo_vistoria', 'Laudo de Vistoria'),
        ('seguro', 'Seguro'),
        ('procuracao', 'Procuração'),
        ('documento_contabil', 'Documento Contábil'),
        ('outro', 'Outro'),
    ]

    imovel = models.ForeignKey(
        Imovel, on_delete=models.CASCADE, verbose_name='Imóvel',
        related_name='documentos_obrigatorios',
    )
    tipo = models.CharField('Tipo', max_length=30, choices=TIPO_CHOICES)
    descricao = models.CharField('Descrição', max_length=200)
    obrigatorio = models.BooleanField('Obrigatório', default=True)
    documento = models.ForeignKey(
        'Documento', null=True, blank=True, on_delete=models.SET_NULL,
        verbose_name='Documento Vinculado', related_name='obrigatorios',
    )
    observacoes = models.TextField('Observações', blank=True)
    criado_em = models.DateTimeField('Criado em', auto_now_add=True)
    atualizado_em = models.DateTimeField('Atualizado em', auto_now=True)
    history = HistoricalRecords()

    class Meta:
        verbose_name = 'Documento Obrigatório'
        verbose_name_plural = 'Documentos Obrigatórios'
        ordering = ['imovel__nome', 'tipo']
        unique_together = [['imovel', 'tipo', 'descricao']]

    def __str__(self):
        return f'{self.get_tipo_display()} — {self.imovel.nome}'

    def clean(self):
        if self.documento_id:
            if self.documento.imovel_id is None:
                raise ValidationError(
                    {'documento': 'O documento vinculado deve pertencer a um imóvel.'}
                )
            if self.documento.imovel_id != self.imovel_id:
                raise ValidationError(
                    {'documento': 'O documento vinculado deve pertencer ao mesmo imóvel.'}
                )

    @property
    def pendente(self):
        return self.obrigatorio and self.documento_id is None


class Documento(models.Model):
    TIPO_CHOICES = [
        ('matricula', 'Matrícula'),
        ('contrato_aluguel', 'Contrato de Aluguel'),
        ('iptu', 'IPTU'),
        ('comprovante_pagamento', 'Comprovante de Pagamento'),
        ('nota_fiscal', 'Nota Fiscal'),
        ('declaracao_imobiliaria', 'Declaração de Imóvel'),
        ('extrato_bancario', 'Extrato Bancário'),
        ('seguro', 'Seguro'),
        ('laudo', 'Laudo / Vistoria'),
        ('procuracao', 'Procuração'),
        ('documento_contabil', 'Documento Contábil'),
        ('outro', 'Outro'),
    ]

    TIPOS_CONTABILIDADE = {
        'comprovante_pagamento', 'nota_fiscal', 'extrato_bancario', 'documento_contabil', 'iptu'
    }

    titulo = models.CharField('Título', max_length=200)
    tipo = models.CharField('Tipo', max_length=30, choices=TIPO_CHOICES, default='outro')
    arquivo = models.FileField(
        'Arquivo', upload_to=upload_documento_path,
        validators=[
            FileExtensionValidator(allowed_extensions=EXTENSOES_PERMITIDAS),
            validar_tamanho_arquivo,
        ],
        help_text=f'Extensões aceitas: {", ".join(EXTENSOES_PERMITIDAS)}. Máx. {TAMANHO_MAXIMO_UPLOAD_MB} MB.',
    )
    imovel = models.ForeignKey(Imovel, on_delete=models.SET_NULL, null=True, blank=True, verbose_name='Imóvel')
    contrato = models.ForeignKey(Contrato, on_delete=models.SET_NULL, null=True, blank=True, verbose_name='Contrato')
    pessoa = models.ForeignKey(Pessoa, on_delete=models.SET_NULL, null=True, blank=True, verbose_name='Pessoa')
    receita = models.ForeignKey(ReceitaAluguel, on_delete=models.SET_NULL, null=True, blank=True, verbose_name='Receita de Aluguel')
    despesa = models.ForeignKey(Despesa, on_delete=models.SET_NULL, null=True, blank=True, verbose_name='Despesa')
    data_documento = models.DateField('Data do Documento', null=True, blank=True)
    data_validade = models.DateField('Data de Validade', null=True, blank=True)
    enviado_contabilidade = models.BooleanField('Enviado à Contabilidade', default=False)
    data_envio_contabilidade = models.DateField('Data de Envio à Contabilidade', null=True, blank=True)
    observacoes = models.TextField('Observações', blank=True)
    criado_em = models.DateTimeField('Criado em', auto_now_add=True)
    atualizado_em = models.DateTimeField('Atualizado em', auto_now=True)
    history = HistoricalRecords()

    class Meta:
        verbose_name = 'Documento'
        verbose_name_plural = 'Documentos'
        ordering = ['-criado_em']

    def __str__(self):
        return f'{self.titulo} ({self.get_tipo_display()})'

    @property
    def pendente_contabilidade(self):
        return self.tipo in self.TIPOS_CONTABILIDADE and not self.enviado_contabilidade

    @property
    def vencido(self):
        return self.data_validade is not None and self.data_validade < timezone.localdate()

    @property
    def vence_em_breve(self):
        """True quando a validade está dentro da janela de alerta (30 dias), mas ainda não venceu."""
        if self.data_validade is None:
            return False
        hoje = timezone.localdate()
        return hoje <= self.data_validade <= hoje + timedelta(days=DIAS_ALERTA_VALIDADE)


def documentos_vencendo_qs(dias=DIAS_ALERTA_VALIDADE):
    """Documentos com validade vencida ou vencendo nos próximos `dias`."""
    hoje = timezone.localdate()
    return Documento.objects.filter(
        data_validade__isnull=False,
        data_validade__lte=hoje + timedelta(days=dias),
    ).order_by('data_validade')
