from calendar import monthrange
from datetime import date

from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone


def _avancar_12_meses(d):
    """Avança uma data em 12 meses, tratando o caso de 29/fev."""
    try:
        return d.replace(year=d.year + 1)
    except ValueError:
        return d.replace(year=d.year + 1, day=28)


class Imovel(models.Model):
    STATUS_CHOICES = [
        ('alugado', 'Alugado'),
        ('vago', 'Vago'),
        ('em_manutencao', 'Em Manutenção'),
        ('vendido', 'Vendido'),
    ]
    TIPO_IMOVEL_CHOICES = [
        ('apartamento', 'Apartamento'),
        ('casa', 'Casa'),
        ('sala_comercial', 'Sala Comercial'),
        ('loja', 'Loja'),
        ('predio', 'Prédio'),
        ('andar', 'Andar'),
        ('terreno', 'Terreno'),
        ('galpao', 'Galpão'),
        ('garagem', 'Garagem'),
        ('coworking', 'Coworking'),
        ('outro', 'Outro'),
    ]
    USO_CHOICES = [
        ('residencial', 'Residencial'),
        ('comercial', 'Comercial'),
        ('misto', 'Misto'),
        ('outro', 'Outro'),
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
    tipo_imovel = models.CharField('Tipo de Imóvel', max_length=30, choices=TIPO_IMOVEL_CHOICES, default='outro')
    uso = models.CharField('Uso', max_length=20, choices=USO_CHOICES, default='outro')
    imovel_pai = models.ForeignKey(
        'self', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='unidades', verbose_name='Imóvel Pai',
        help_text='Preencha quando este imóvel for uma unidade vinculada a um prédio/imóvel maior (ex.: andar ou sala de um prédio).',
    )
    unidade_locavel = models.BooleanField(
        'Unidade Locável', default=True,
        help_text='Desmarque para imóveis que apenas agrupam unidades (ex.: o prédio em si, não alugado diretamente).',
    )
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
    tipo = models.CharField(
        'Categoria Preferencial', max_length=20, choices=TIPO_CHOICES, default='outro',
        help_text=(
            'Classificação principal, usada apenas para busca e organização. '
            'Não restringe o papel da pessoa em contratos — a mesma pessoa pode ser '
            'locatária em um contrato e fiadora em outro (ver "Partes do Contrato").'
        ),
    )
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
        return self.nome


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
    # Campos legados de partes — mantidos por compatibilidade. Para múltiplos
    # locatários/fiadores/representantes, use o model ContratoParte (seção
    # "Partes do Contrato" no admin). get_locatarios()/get_fiadores()/
    # get_imobiliaria_principal() usam ContratoParte quando disponível e caem
    # de volta para estes campos legados quando não há partes cadastradas.
    locatario = models.ForeignKey(
        Pessoa, on_delete=models.PROTECT,
        related_name='contratos_como_locatario',
        verbose_name='Locatário (legado)',
        help_text='Campo legado. Para múltiplos locatários, cadastre em "Partes do Contrato".',
    )
    fiador = models.ForeignKey(
        Pessoa, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='contratos_como_fiador',
        verbose_name='Fiador (legado)',
        help_text='Campo legado. Para múltiplos fiadores, cadastre em "Partes do Contrato".',
    )
    imobiliaria = models.ForeignKey(
        Pessoa, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='contratos_como_imobiliaria',
        verbose_name='Imobiliária (legado)',
        help_text='Campo legado. Pode também ser cadastrada em "Partes do Contrato".',
    )
    data_inicio = models.DateField('Data de Início')
    data_fim = models.DateField(
        'Data de Término Original',
        help_text='Data de término do prazo determinado original do contrato.',
    )
    prazo_indeterminado = models.BooleanField(
        'Prazo Indeterminado', default=False,
        help_text='Marque quando o contrato foi prorrogado por prazo indeterminado após o término original.',
    )
    data_encerramento_real = models.DateField(
        'Data de Encerramento Real', null=True, blank=True,
        help_text='Preencha apenas quando o contrato foi efetivamente encerrado (rescisão ou fim real da locação).',
    )
    valor_aluguel = models.DecimalField('Valor do Aluguel (R$)', max_digits=12, decimal_places=2)
    dia_vencimento = models.PositiveSmallIntegerField(
        'Dia de Vencimento',
        validators=[MinValueValidator(1), MaxValueValidator(31)],
    )
    indice_reajuste = models.CharField('Índice de Reajuste', max_length=10, choices=INDICE_CHOICES, default='ipca')
    data_proximo_reajuste = models.DateField('Data do Próximo Reajuste', null=True, blank=True)
    tipo_garantia = models.CharField('Tipo de Garantia', max_length=30, choices=GARANTIA_CHOICES, default='sem_garantia')
    comissao_imobiliaria_percentual = models.DecimalField(
        'Taxa de Administração Imobiliária (%)', max_digits=5, decimal_places=2, null=True, blank=True,
        help_text='Percentual cobrado pela imobiliária sobre o encargo de aluguel. Gera despesa automática ao gerar a receita mensal.',
    )
    multa_atraso_percentual = models.DecimalField(
        'Multa por Atraso (%)', max_digits=6, decimal_places=2, default=0,
        help_text='Percentual de multa sobre o valor previsto, aplicado após os dias de carência.',
    )
    juros_mora_percentual_mes = models.DecimalField(
        'Juros de Mora (% ao mês)', max_digits=6, decimal_places=2, default=0,
        help_text='Percentual de juros ao mês, calculado proporcionalmente aos dias de atraso (base 30 dias).',
    )
    dias_carencia_multa = models.PositiveSmallIntegerField(
        'Dias de Carência para Multa', default=0,
        help_text='Número de dias após o vencimento antes de multa/juros começarem a incidir.',
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
        # Impede contratos ativos sobrepostos para o mesmo imóvel.
        # Contratos por prazo indeterminado (sem encerramento real) são
        # tratados como abertos até uma data futura indefinida (date.max).
        if self.status == 'ativo' and self.imovel_id and self.data_inicio:
            outros = Contrato.objects.filter(imovel_id=self.imovel_id, status='ativo')
            if self.pk:
                outros = outros.exclude(pk=self.pk)

            self_fim = self.data_encerramento_real or (None if self.prazo_indeterminado else self.data_fim)

            for outro in outros:
                outro_fim = outro.data_encerramento_real or (None if outro.prazo_indeterminado else outro.data_fim)
                inicio_ok = outro.data_inicio <= (self_fim or date.max)
                fim_ok = self.data_inicio <= (outro_fim or date.max)
                if inicio_ok and fim_ok:
                    raise ValidationError(
                        'Já existe um contrato ativo para este imóvel com período sobreposto.'
                    )

    @property
    def data_fim_efetiva(self):
        """Data que efetivamente limita a vigência: encerramento real > prazo indeterminado (aberto) > data_fim original."""
        if self.data_encerramento_real:
            return self.data_encerramento_real
        if self.prazo_indeterminado:
            return None
        return self.data_fim

    @property
    def esta_vencido(self):
        if self.status != 'ativo':
            return False
        if self.prazo_indeterminado and not self.data_encerramento_real:
            return False
        fim = self.data_fim_efetiva
        return fim is not None and fim < timezone.now().date()

    @property
    def vigencia_display(self):
        inicio = self.data_inicio.strftime('%d/%m/%Y') if self.data_inicio else '—'
        if self.data_encerramento_real:
            return f'{inicio} a {self.data_encerramento_real.strftime("%d/%m/%Y")} (encerrado)'
        if self.prazo_indeterminado:
            return f'{inicio} — Prazo Indeterminado'
        fim = self.data_fim.strftime('%d/%m/%Y') if self.data_fim else '—'
        return f'{inicio} a {fim}'

    def get_locatarios(self):
        """Lista de Pessoa com papel locatario via ContratoParte, com fallback para o campo legado."""
        partes = list(self.partes.filter(papel='locatario').select_related('pessoa'))
        if partes:
            return [p.pessoa for p in partes]
        return [self.locatario] if self.locatario_id else []

    def get_fiadores(self):
        """Lista de Pessoa com papel fiador via ContratoParte, com fallback para o campo legado."""
        partes = list(self.partes.filter(papel='fiador').select_related('pessoa'))
        if partes:
            return [p.pessoa for p in partes]
        return [self.fiador] if self.fiador_id else []

    def get_imobiliaria_principal(self):
        """Pessoa com papel imobiliaria via ContratoParte, com fallback para o campo legado."""
        parte = self.partes.filter(papel='imobiliaria').select_related('pessoa').first()
        if parte:
            return parte.pessoa
        return self.imobiliaria

    @property
    def locatarios_display(self):
        nomes = [p.nome for p in self.get_locatarios()]
        return ', '.join(nomes) if nomes else '—'

    @property
    def fiadores_display(self):
        nomes = [p.nome for p in self.get_fiadores()]
        return ', '.join(nomes) if nomes else '—'


class ContratoParte(models.Model):
    PAPEL_CHOICES = [
        ('locatario', 'Locatário'),
        ('fiador', 'Fiador'),
        ('representante', 'Representante'),
        ('conjuge', 'Cônjuge'),
        ('imobiliaria', 'Imobiliária'),
        ('outro', 'Outro'),
    ]

    contrato = models.ForeignKey(Contrato, on_delete=models.CASCADE, related_name='partes', verbose_name='Contrato')
    pessoa = models.ForeignKey(
        Pessoa, on_delete=models.PROTECT, related_name='participacoes_contratuais', verbose_name='Pessoa'
    )
    papel = models.CharField('Papel', max_length=30, choices=PAPEL_CHOICES)
    principal = models.BooleanField('Principal', default=False)
    observacoes = models.TextField('Observações', blank=True)
    criado_em = models.DateTimeField('Criado em', auto_now_add=True)
    atualizado_em = models.DateTimeField('Atualizado em', auto_now=True)

    class Meta:
        verbose_name = 'Parte do Contrato'
        verbose_name_plural = 'Partes do Contrato'
        ordering = ['contrato', 'papel', 'pessoa__nome']
        unique_together = [['contrato', 'pessoa', 'papel']]

    def __str__(self):
        return f'{self.pessoa.nome} — {self.get_papel_display()} ({self.contrato})'


class EncargoContrato(models.Model):
    TIPO_CHOICES = [
        ('aluguel', 'Aluguel'),
        ('iptu', 'IPTU'),
        ('condominio', 'Condomínio'),
        ('taxa_manutencao', 'Taxa de Manutenção'),
        ('seguro', 'Seguro'),
        ('agua_luz_comum', 'Água/Luz Comum'),
        ('outro', 'Outro'),
    ]
    PERIODICIDADE_CHOICES = [
        ('mensal', 'Mensal'),
        ('anual', 'Anual'),
        ('unico', 'Único'),
    ]

    contrato = models.ForeignKey(Contrato, on_delete=models.CASCADE, related_name='encargos', verbose_name='Contrato')
    tipo = models.CharField('Tipo', max_length=30, choices=TIPO_CHOICES)
    descricao = models.CharField('Descrição', max_length=200, blank=True)
    valor = models.DecimalField('Valor (R$)', max_digits=12, decimal_places=2)
    periodicidade = models.CharField('Periodicidade', max_length=20, choices=PERIODICIDADE_CHOICES, default='mensal')
    data_inicio_cobranca = models.DateField(
        'Início da Cobrança', null=True, blank=True,
        help_text='Se preenchida e posterior à competência, o encargo não é cobrado antes dessa data (útil para carência).',
    )
    data_fim_cobranca = models.DateField(
        'Fim da Cobrança', null=True, blank=True,
        help_text='Se preenchida e anterior à competência, o encargo deixa de ser cobrado a partir dessa data.',
    )
    ativo = models.BooleanField('Ativo', default=True)
    observacoes = models.TextField('Observações', blank=True)
    criado_em = models.DateTimeField('Criado em', auto_now_add=True)
    atualizado_em = models.DateTimeField('Atualizado em', auto_now=True)

    class Meta:
        verbose_name = 'Encargo do Contrato'
        verbose_name_plural = 'Encargos do Contrato'
        ordering = ['contrato', 'tipo']

    def __str__(self):
        return f'{self.get_tipo_display()} — {self.contrato} (R$ {self.valor})'

    def clean(self):
        if self.tipo == 'aluguel' and self.periodicidade != 'mensal':
            raise ValidationError({'periodicidade': 'O encargo de aluguel deve ser mensal.'})

    def aplica_em(self, ano, mes):
        """Indica se este encargo deve ser cobrado na competência informada."""
        if not self.ativo:
            return False

        primeiro_dia = date(ano, mes, 1)
        ultimo_dia = date(ano, mes, monthrange(ano, mes)[1])

        if self.data_inicio_cobranca and self.data_inicio_cobranca > ultimo_dia:
            return False
        if self.data_fim_cobranca and self.data_fim_cobranca < primeiro_dia:
            return False

        if self.periodicidade == 'mensal':
            return True

        if self.periodicidade == 'anual':
            mes_referencia = (
                self.data_inicio_cobranca.month if self.data_inicio_cobranca else self.contrato.data_inicio.month
            )
            return mes == mes_referencia

        if self.periodicidade == 'unico':
            if self.data_inicio_cobranca:
                ano_ref, mes_ref = self.data_inicio_cobranca.year, self.data_inicio_cobranca.month
            else:
                ano_ref, mes_ref = self.contrato.data_inicio.year, self.contrato.data_inicio.month
            return (ano, mes) == (ano_ref, mes_ref)

        return False


class ReajusteContrato(models.Model):
    contrato = models.ForeignKey(Contrato, on_delete=models.CASCADE, related_name='reajustes', verbose_name='Contrato')
    data_reajuste = models.DateField('Data do Reajuste')
    indice = models.CharField('Índice', max_length=10, choices=Contrato.INDICE_CHOICES)
    percentual_aplicado = models.DecimalField(
        'Percentual Aplicado (%)', max_digits=8, decimal_places=4, null=True, blank=True
    )
    valor_anterior = models.DecimalField('Valor Anterior (R$)', max_digits=12, decimal_places=2)
    valor_novo = models.DecimalField('Valor Novo (R$)', max_digits=12, decimal_places=2)
    aplicado = models.BooleanField(
        'Aplicado', default=False,
        help_text='Ao marcar e salvar um reajuste novo como aplicado, o sistema atualiza o valor vigente do contrato e do encargo de aluguel automaticamente.',
    )
    observacoes = models.TextField('Observações', blank=True)
    criado_em = models.DateTimeField('Criado em', auto_now_add=True)
    atualizado_em = models.DateTimeField('Atualizado em', auto_now=True)

    class Meta:
        verbose_name = 'Reajuste de Contrato'
        verbose_name_plural = 'Reajustes de Contrato'
        ordering = ['-data_reajuste']

    def __str__(self):
        return f'Reajuste {self.contrato} em {self.data_reajuste:%d/%m/%Y}'

    def aplicar(self):
        """
        Atualiza o valor vigente do contrato e do encargo de aluguel ativo a
        partir deste reajuste, e avança data_proximo_reajuste em 12 meses
        (exceto para índice fixo). Nunca altera receitas já geradas.
        """
        contrato = self.contrato
        contrato.valor_aluguel = self.valor_novo
        update_fields = ['valor_aluguel']
        if self.indice != 'fixo':
            contrato.data_proximo_reajuste = _avancar_12_meses(self.data_reajuste)
            update_fields.append('data_proximo_reajuste')
        contrato.save(update_fields=update_fields)

        encargo_aluguel = contrato.encargos.filter(tipo='aluguel', ativo=True).first()
        if encargo_aluguel:
            encargo_aluguel.valor = self.valor_novo
            encargo_aluguel.save(update_fields=['valor'])


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
