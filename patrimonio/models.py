from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models


class Imovel(models.Model):
    STATUS_CHOICES = [
        ('alugado', 'Alugado'),
        ('vago', 'Vago'),
        ('em_manutencao', 'Em Manutenção'),
        ('vendido', 'Vendido'),
    ]

    nome = models.CharField('Nome / Identificação', max_length=200)
    endereco = models.CharField('Endereço', max_length=300)
    cidade = models.CharField('Cidade', max_length=100)
    estado = models.CharField('Estado', max_length=2)
    cep = models.CharField('CEP', max_length=9, blank=True)
    matricula = models.CharField('Matrícula', max_length=100, blank=True)
    cartorio = models.CharField('Cartório', max_length=200, blank=True)
    inscricao_iptu = models.CharField('Inscrição IPTU', max_length=100, blank=True)
    proprietario = models.CharField('Proprietário(s)', max_length=300, blank=True)
    status = models.CharField('Status', max_length=20, choices=STATUS_CHOICES, default='vago')
    data_aquisicao = models.DateField('Data de Aquisição', null=True, blank=True)
    valor_aquisicao = models.DecimalField('Valor de Aquisição (R$)', max_digits=14, decimal_places=2, null=True, blank=True)
    valor_estimado = models.DecimalField('Valor Estimado Atual (R$)', max_digits=14, decimal_places=2, null=True, blank=True)
    observacoes = models.TextField('Observações', blank=True)
    criado_em = models.DateTimeField('Criado em', auto_now_add=True)
    atualizado_em = models.DateTimeField('Atualizado em', auto_now=True)

    class Meta:
        verbose_name = 'Imóvel'
        verbose_name_plural = 'Imóveis'
        ordering = ['nome']

    def __str__(self):
        return f'{self.nome} — {self.cidade}/{self.estado}'

    def get_contrato_ativo(self):
        return self.contrato_set.filter(status='ativo').first()


class Pessoa(models.Model):
    TIPO_CHOICES = [
        ('locatario', 'Locatário'),
        ('fiador', 'Fiador'),
        ('fornecedor', 'Fornecedor'),
        ('imobiliaria', 'Imobiliária'),
        ('contador', 'Contador'),
        ('advogado', 'Advogado'),
        ('familiar', 'Familiar'),
        ('outro', 'Outro'),
    ]

    nome = models.CharField('Nome / Razão Social', max_length=200)
    tipo = models.CharField('Tipo', max_length=20, choices=TIPO_CHOICES, default='outro')
    cpf_cnpj = models.CharField('CPF / CNPJ', max_length=18, blank=True)
    email = models.EmailField('E-mail', blank=True)
    telefone = models.CharField('Telefone', max_length=20, blank=True)
    endereco = models.CharField('Endereço', max_length=300, blank=True)
    observacoes = models.TextField('Observações', blank=True)
    criado_em = models.DateTimeField('Criado em', auto_now_add=True)
    atualizado_em = models.DateTimeField('Atualizado em', auto_now=True)

    class Meta:
        verbose_name = 'Pessoa'
        verbose_name_plural = 'Pessoas'
        ordering = ['nome']

    def __str__(self):
        return f'{self.nome} ({self.get_tipo_display()})'


class Contrato(models.Model):
    INDICE_CHOICES = [
        ('ipca', 'IPCA'),
        ('igpm', 'IGP-M'),
        ('inpc', 'INPC'),
        ('fixo', 'Fixo'),
        ('outro', 'Outro'),
    ]
    GARANTIA_CHOICES = [
        ('caucao', 'Caução'),
        ('fiador', 'Fiador'),
        ('seguro_fianca', 'Seguro Fiança'),
        ('titulo_capitalizacao', 'Título de Capitalização'),
        ('sem_garantia', 'Sem Garantia'),
        ('outro', 'Outro'),
    ]
    STATUS_CHOICES = [
        ('ativo', 'Ativo'),
        ('encerrado', 'Encerrado'),
        ('rescindido', 'Rescindido'),
        ('suspenso', 'Suspenso'),
    ]

    imovel = models.ForeignKey(Imovel, on_delete=models.PROTECT, verbose_name='Imóvel')
    locatario = models.ForeignKey(
        Pessoa, on_delete=models.PROTECT,
        related_name='contratos_como_locatario',
        verbose_name='Locatário',
        limit_choices_to={'tipo__in': ['locatario', 'outro']},
    )
    fiador = models.ForeignKey(
        Pessoa, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='contratos_como_fiador',
        verbose_name='Fiador',
    )
    imobiliaria = models.ForeignKey(
        Pessoa, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='contratos_como_imobiliaria',
        verbose_name='Imobiliária',
        limit_choices_to={'tipo': 'imobiliaria'},
    )
    data_inicio = models.DateField('Data de Início')
    data_fim = models.DateField('Data de Término')
    valor_aluguel = models.DecimalField('Valor do Aluguel (R$)', max_digits=12, decimal_places=2)
    dia_vencimento = models.PositiveSmallIntegerField(
        'Dia de Vencimento',
        validators=[MinValueValidator(1), MaxValueValidator(31)],
    )
    indice_reajuste = models.CharField('Índice de Reajuste', max_length=10, choices=INDICE_CHOICES, default='ipca')
    data_proximo_reajuste = models.DateField('Data do Próximo Reajuste', null=True, blank=True)
    tipo_garantia = models.CharField('Tipo de Garantia', max_length=30, choices=GARANTIA_CHOICES, default='sem_garantia')
    comissao_imobiliaria_percentual = models.DecimalField(
        'Comissão Imobiliária (%)', max_digits=5, decimal_places=2, null=True, blank=True
    )
    status = models.CharField('Status', max_length=15, choices=STATUS_CHOICES, default='ativo')
    observacoes = models.TextField('Observações', blank=True)
    criado_em = models.DateTimeField('Criado em', auto_now_add=True)
    atualizado_em = models.DateTimeField('Atualizado em', auto_now=True)

    class Meta:
        verbose_name = 'Contrato'
        verbose_name_plural = 'Contratos'
        ordering = ['-data_inicio']

    def __str__(self):
        return f'Contrato {self.imovel} — {self.locatario.nome} ({self.get_status_display()})'

    def clean(self):
        # Impede contratos ativos sobrepostos para o mesmo imóvel
        if (
            self.status == 'ativo'
            and self.imovel_id
            and self.data_inicio
            and self.data_fim
        ):
            qs = Contrato.objects.filter(
                imovel_id=self.imovel_id,
                status='ativo',
                data_inicio__lte=self.data_fim,
                data_fim__gte=self.data_inicio,
            )
            if self.pk:
                qs = qs.exclude(pk=self.pk)
            if qs.exists():
                raise ValidationError(
                    'Já existe um contrato ativo para este imóvel com período sobreposto.'
                )


class Manutencao(models.Model):
    CATEGORIA_CHOICES = [
        ('eletrica', 'Elétrica'),
        ('hidraulica', 'Hidráulica'),
        ('pintura', 'Pintura'),
        ('reforma', 'Reforma'),
        ('limpeza', 'Limpeza'),
        ('telhado', 'Telhado'),
        ('estrutural', 'Estrutural'),
        ('outro', 'Outro'),
    ]
    STATUS_CHOICES = [
        ('solicitada', 'Solicitada'),
        ('orcamento_recebido', 'Orçamento Recebido'),
        ('aprovada', 'Aprovada'),
        ('em_execucao', 'Em Execução'),
        ('concluida', 'Concluída'),
        ('cancelada', 'Cancelada'),
    ]

    imovel = models.ForeignKey(Imovel, on_delete=models.PROTECT, verbose_name='Imóvel')
    descricao = models.CharField('Descrição', max_length=300)
    categoria = models.CharField('Categoria', max_length=20, choices=CATEGORIA_CHOICES, default='outro')
    fornecedor = models.ForeignKey(
        Pessoa, on_delete=models.SET_NULL, null=True, blank=True,
        verbose_name='Fornecedor / Prestador',
    )
    data_solicitacao = models.DateField('Data da Solicitação')
    data_conclusao = models.DateField('Data de Conclusão', null=True, blank=True)
    valor_estimado = models.DecimalField('Valor Estimado (R$)', max_digits=12, decimal_places=2, null=True, blank=True)
    valor_final = models.DecimalField('Valor Final (R$)', max_digits=12, decimal_places=2, null=True, blank=True)
    status = models.CharField('Status', max_length=20, choices=STATUS_CHOICES, default='solicitada')
    observacoes = models.TextField('Observações', blank=True)
    criado_em = models.DateTimeField('Criado em', auto_now_add=True)
    atualizado_em = models.DateTimeField('Atualizado em', auto_now=True)

    class Meta:
        verbose_name = 'Manutenção'
        verbose_name_plural = 'Manutenções'
        ordering = ['-data_solicitacao']

    def __str__(self):
        return f'{self.descricao} — {self.imovel.nome} ({self.get_status_display()})'
