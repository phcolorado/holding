from calendar import monthrange
from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone
from simple_history.models import HistoricalRecords


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
    history = HistoricalRecords()

    class Meta:
        verbose_name = 'Imóvel'
        verbose_name_plural = 'Imóveis'
        ordering = ['nome']

    def __str__(self):
        return f'{self.nome} — {self.cidade}/{self.estado}'

    def clean(self):
        if self.imovel_pai_id:
            if self.pk and self.imovel_pai_id == self.pk:
                raise ValidationError({'imovel_pai': 'Um imóvel não pode ser pai de si mesmo.'})
            # Impede ciclos na hierarquia (A → B → A)
            visitados = {self.pk} if self.pk else set()
            atual = self.imovel_pai
            while atual is not None:
                if atual.pk in visitados:
                    raise ValidationError({'imovel_pai': 'Hierarquia circular de imóveis não é permitida.'})
                visitados.add(atual.pk)
                atual = atual.imovel_pai

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
    cpf_cnpj_normalizado = models.CharField(
        'CPF/CNPJ (somente dígitos)', max_length=14, blank=True, editable=False, db_index=True,
        help_text='Preenchido automaticamente — usado para impedir cadastros duplicados.',
    )
    email = models.EmailField('E-mail', blank=True)
    telefone = models.CharField('Telefone', max_length=20, blank=True)
    endereco = models.CharField('Endereço', max_length=300, blank=True)
    observacoes = models.TextField('Observações', blank=True)
    criado_em = models.DateTimeField('Criado em', auto_now_add=True)
    atualizado_em = models.DateTimeField('Atualizado em', auto_now=True)
    history = HistoricalRecords()

    class Meta:
        verbose_name = 'Pessoa'
        verbose_name_plural = 'Pessoas'
        ordering = ['nome']
        constraints = [
            # Unicidade garantida no BANCO (a validação em clean() não protege
            # contra concorrência nem contra criação direta via ORM). Vazio
            # continua permitido em qualquer quantidade — CPF/CNPJ é opcional.
            models.UniqueConstraint(
                fields=['cpf_cnpj_normalizado'],
                condition=~Q(cpf_cnpj_normalizado=''),
                name='pessoa_cpf_cnpj_normalizado_unico_quando_preenchido',
            ),
        ]

    def __str__(self):
        return self.nome

    @staticmethod
    def normalizar_cpf_cnpj(valor):
        return ''.join(c for c in (valor or '') if c.isdigit())

    def clean(self):
        normalizado = self.normalizar_cpf_cnpj(self.cpf_cnpj)
        if normalizado:
            duplicadas = Pessoa.objects.filter(cpf_cnpj_normalizado=normalizado)
            if self.pk:
                duplicadas = duplicadas.exclude(pk=self.pk)
            if duplicadas.exists():
                raise ValidationError({
                    'cpf_cnpj': f'Já existe uma pessoa cadastrada com este CPF/CNPJ ({duplicadas.first().nome}).'
                })

    def save(self, *args, **kwargs):
        # A forma formatada é preservada em cpf_cnpj; a normalizada serve para comparação.
        self.cpf_cnpj_normalizado = self.normalizar_cpf_cnpj(self.cpf_cnpj)
        super().save(*args, **kwargs)


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
    valor_aluguel = models.DecimalField(
        'Valor do Aluguel (R$)', max_digits=12, decimal_places=2,
        validators=[MinValueValidator(Decimal('0.01'))],
    )
    dia_vencimento = models.PositiveSmallIntegerField(
        'Dia de Vencimento',
        validators=[MinValueValidator(1), MaxValueValidator(31)],
    )
    indice_reajuste = models.CharField('Índice de Reajuste', max_length=10, choices=INDICE_CHOICES, default='ipca')
    data_proximo_reajuste = models.DateField('Data do Próximo Reajuste', null=True, blank=True)
    tipo_garantia = models.CharField('Tipo de Garantia', max_length=30, choices=GARANTIA_CHOICES, default='sem_garantia')
    comissao_imobiliaria_percentual = models.DecimalField(
        'Taxa de Administração Imobiliária (%)', max_digits=5, decimal_places=2, null=True, blank=True,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
        help_text='Percentual cobrado pela imobiliária sobre o encargo de aluguel. Gera despesa automática ao gerar a receita mensal.',
    )
    multa_atraso_percentual = models.DecimalField(
        'Multa por Atraso (%)', max_digits=6, decimal_places=2, default=0,
        validators=[MinValueValidator(0)],
        help_text='Percentual de multa sobre o valor previsto, aplicado após os dias de carência.',
    )
    juros_mora_percentual_mes = models.DecimalField(
        'Juros de Mora (% ao mês)', max_digits=6, decimal_places=2, default=0,
        validators=[MinValueValidator(0)],
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
    history = HistoricalRecords()

    class Meta:
        verbose_name = 'Contrato'
        verbose_name_plural = 'Contratos'
        ordering = ['-data_inicio']

    def __str__(self):
        return f'Contrato {self.imovel} — {self.locatario.nome} ({self.get_status_display()})'

    def clean(self):
        erros = {}
        if self.data_inicio and self.data_fim and self.data_fim < self.data_inicio:
            erros['data_fim'] = 'A data de término não pode ser anterior à data de início.'
        if self.data_inicio and self.data_encerramento_real and self.data_encerramento_real < self.data_inicio:
            erros['data_encerramento_real'] = 'O encerramento real não pode ser anterior ao início do contrato.'
        if (
            self.status in ('encerrado', 'rescindido')
            and self.prazo_indeterminado
            and not self.data_encerramento_real
        ):
            erros['data_encerramento_real'] = (
                'Contrato por prazo indeterminado encerrado/rescindido precisa da data de '
                'encerramento real — sem ela a vigência ficaria indefinida.'
            )
        if (
            self.status == 'ativo'
            and self.data_encerramento_real
            and self.data_encerramento_real < timezone.localdate()
        ):
            erros['status'] = (
                'O contrato tem encerramento real no passado — altere o status para '
                'Encerrado ou Rescindido, ou remova a data de encerramento.'
            )
        if erros:
            raise ValidationError(erros)

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
        return fim is not None and fim < timezone.localdate()

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

    def garantir_encargo_aluguel(self):
        """
        Garante que o contrato ativo tenha um EncargoContrato ativo do tipo
        aluguel coerente com valor_aluguel. Idempotente — nunca duplica nem
        sobrescreve um encargo de aluguel já existente (manual ou automático).
        Retorna o encargo criado, ou None se não criou nenhum.
        """
        if self.status != 'ativo':
            return None
        if self.encargos.filter(tipo='aluguel', ativo=True).exists():
            return None
        return EncargoContrato.objects.create(
            contrato=self,
            tipo='aluguel',
            descricao='Aluguel',
            valor=self.valor_aluguel,
            periodicidade='mensal',
            ativo=True,
            data_inicio_cobranca=self.data_inicio,
        )


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
    history = HistoricalRecords()

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
    history = HistoricalRecords()

    class Meta:
        verbose_name = 'Encargo do Contrato'
        verbose_name_plural = 'Encargos do Contrato'
        ordering = ['contrato', 'tipo']
        constraints = [
            models.UniqueConstraint(
                fields=['contrato'],
                condition=Q(tipo='aluguel', ativo=True),
                name='unico_encargo_aluguel_ativo_por_contrato',
            ),
        ]

    def __str__(self):
        return f'{self.get_tipo_display()} — {self.contrato} (R$ {self.valor})'

    def clean(self):
        if self.valor is not None:
            if self.tipo == 'aluguel' and self.valor <= 0:
                raise ValidationError({'valor': 'O encargo de aluguel deve ter valor maior que zero.'})
            if self.valor < 0:
                raise ValidationError({'valor': 'O valor do encargo não pode ser negativo.'})
        if (
            self.data_inicio_cobranca and self.data_fim_cobranca
            and self.data_fim_cobranca < self.data_inicio_cobranca
        ):
            raise ValidationError({'data_fim_cobranca': 'O fim da cobrança não pode ser anterior ao início.'})
        if self.tipo == 'aluguel' and self.periodicidade != 'mensal':
            raise ValidationError({'periodicidade': 'O encargo de aluguel deve ser mensal.'})
        if self.tipo == 'aluguel' and self.ativo and self.contrato_id:
            duplicados = EncargoContrato.objects.filter(
                contrato_id=self.contrato_id, tipo='aluguel', ativo=True
            )
            if self.pk:
                duplicados = duplicados.exclude(pk=self.pk)
            if duplicados.exists():
                raise ValidationError(
                    {'tipo': 'Já existe um encargo de aluguel ativo para este contrato.'}
                )

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


class ReajusteContratoQuerySet(models.QuerySet):
    """
    QuerySet.update()/QuerySet.delete() operam direto no banco e NUNCA
    chamam ReajusteContrato.save()/delete() nem clean() — por isso, sem esta
    camada, um `ReajusteContrato.objects.filter(...).update(...)` ou
    `.delete()` em massa contornaria completamente a imutabilidade dos
    reajustes já aplicados.

    update(): bloqueia pelo NOME dos campos, não pelo estado do conjunto —
    evita a janela de corrida de um exists()+update() (um `.update(aplicado=
    True, aplicado_em=...)` seria inseguro mesmo sobre um reajuste AINDA
    pendente no momento da checagem, porque QuerySet.update() nunca executa
    aplicar() — não atualiza Contrato.valor_aluguel, o encargo de aluguel
    nem avança data_proximo_reajuste). Campos financeiros e de aplicação são
    SEMPRE bloqueados, aplicado ou pendente; apenas observacoes (e
    atualizado_em, que .update() não seta sozinho por não passar por
    auto_now) podem ser alterados em massa — para o restante, use
    instance.save() (reforça a imutabilidade) ou o método aplicar().

    delete(): BLOQUEADO COMPLETAMENTE (item 6, rodada de fechamento
    estrutural) — nunca permite exclusão em massa, mesmo quando TODO o
    conjunto é pendente. A checagem anterior (exists() de aplicados, seguida
    de super().delete()) tinha uma janela de corrida entre as duas operações
    — um reajuste podia ser aplicado por outro processo exatamente nesse
    intervalo. Exclusão de reajustes pendentes é sempre INDIVIDUAL, via
    reajuste.delete() (que reconsulta o estado persistido antes de excluir).
    """
    CAMPOS_SEGUROS_PARA_UPDATE_EM_MASSA = frozenset({'observacoes', 'atualizado_em'})

    def update(self, **kwargs):
        campos_nao_seguros = set(kwargs) - self.CAMPOS_SEGUROS_PARA_UPDATE_EM_MASSA
        if campos_nao_seguros:
            raise ValidationError(
                'QuerySet.update() de ReajusteContrato só permite alterar '
                f'{sorted(self.CAMPOS_SEGUROS_PARA_UPDATE_EM_MASSA)} em massa — campos '
                'financeiros ou de aplicação (ex.: aplicado, aplicado_em, valor_novo, '
                'contrato, data_reajuste) exigem instance.save() ou o método aplicar(), '
                'que reforçam a imutabilidade e executam as regras de negócio completas.'
            )
        if 'observacoes' in kwargs:
            kwargs.setdefault('atualizado_em', timezone.now())
        return super().update(**kwargs)

    def delete(self):
        raise ValidationError(
            'Exclusão em massa (QuerySet.delete()) de ReajusteContrato não é permitida — '
            'exclua reajustes pendentes individualmente (reajuste.delete()). Reajustes '
            'já aplicados nunca podem ser excluídos, mesmo individualmente.'
        )


class ReajusteContrato(models.Model):
    objects = ReajusteContratoQuerySet.as_manager()

    contrato = models.ForeignKey(
        # PROTECT (não CASCADE): a exclusão do Contrato NÃO chama
        # ReajusteContrato.delete() nem o QuerySet customizado (o Collector
        # de exclusão do Django apaga em lote via SQL direto) — CASCADE
        # apagaria silenciosamente reajustes já aplicados, contornando toda
        # a proteção acima. Um contrato com reajuste aplicado exige
        # tratamento explícito (nunca é apagado automaticamente).
        Contrato, on_delete=models.PROTECT, related_name='reajustes', verbose_name='Contrato',
    )
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
    aplicado_em = models.DateTimeField(
        'Aplicado em', null=True, blank=True, editable=False,
        help_text='Preenchido pelo sistema no momento da aplicação — impede reaplicação.',
    )
    periodo_indice = models.CharField(
        'Período do Índice', max_length=40, blank=True,
        help_text='Competências usadas na sugestão automática (ex.: "06/2023 a 05/2024").',
    )
    observacoes = models.TextField('Observações', blank=True)
    criado_em = models.DateTimeField('Criado em', auto_now_add=True)
    atualizado_em = models.DateTimeField('Atualizado em', auto_now=True)
    history = HistoricalRecords()

    class Meta:
        verbose_name = 'Reajuste de Contrato'
        verbose_name_plural = 'Reajustes de Contrato'
        ordering = ['-data_reajuste']
        constraints = [
            # Únicos estados válidos: (aplicado=False, aplicado_em=NULL) ou
            # (aplicado=True, aplicado_em preenchido) — nunca uma combinação
            # intermediária, mesmo em criação direta via ORM que contorne
            # clean()/save() (item 5, rodada de fechamento estrutural).
            models.CheckConstraint(
                check=(
                    Q(aplicado=False, aplicado_em__isnull=True)
                    | Q(aplicado=True, aplicado_em__isnull=False)
                ),
                name='reajustecontrato_aplicado_aplicado_em_coerentes',
            ),
        ]

    def __str__(self):
        return f'Reajuste {self.contrato} em {self.data_reajuste:%d/%m/%Y}'

    # Campos financeiros que ficam imutáveis depois da aplicação do reajuste.
    CAMPOS_PROTEGIDOS_APOS_APLICACAO = (
        'contrato_id', 'data_reajuste', 'indice', 'percentual_aplicado',
        'valor_anterior', 'valor_novo', 'periodo_indice',
    )

    def clean(self):
        erros = {}
        if self.valor_novo is not None and self.valor_novo <= 0:
            erros['valor_novo'] = 'O valor novo deve ser maior que zero.'
        if self.valor_anterior is not None and self.valor_anterior <= 0:
            erros['valor_anterior'] = 'O valor anterior deve ser maior que zero.'

        # Item 5 (rodada de fechamento estrutural): únicos estados válidos
        # são (aplicado=False, aplicado_em=None) ou (aplicado=True,
        # aplicado_em preenchido) — nunca uma combinação intermediária,
        # mesmo em criação. A aplicação inicial só pode ocorrer por
        # aplicar(); nenhuma data é preenchida automaticamente aqui.
        if self.aplicado and self.aplicado_em is None:
            erros['aplicado_em'] = (
                'Reajuste marcado como aplicado exige "aplicado em" preenchido — use o '
                'método aplicar() para aplicar um reajuste.'
            )
        if not self.aplicado and self.aplicado_em is not None:
            erros['aplicado'] = (
                'Reajuste com "aplicado em" preenchido deve estar marcado como aplicado '
                '— estado inconsistente.'
            )

        if self.pk:
            persistido = ReajusteContrato.objects.filter(pk=self.pk).first()
            if persistido and persistido.aplicado_em is not None:
                if not self.aplicado:
                    erros['aplicado'] = (
                        'Este reajuste já foi aplicado — não é possível desmarcá-lo. '
                        'Para corrigir um reajuste aplicado, registre um novo reajuste.'
                    )
                for campo in self.CAMPOS_PROTEGIDOS_APOS_APLICACAO:
                    if getattr(self, campo) != getattr(persistido, campo):
                        nome = campo[:-3] if campo.endswith('_id') else campo
                        erros[nome] = (
                            'Este reajuste já foi aplicado — os campos financeiros são '
                            'imutáveis. Apenas as observações podem ser alteradas.'
                        )
        if erros:
            raise ValidationError(erros)

    def save(self, *args, **kwargs):
        # Proteção em duas camadas: clean() cobre o fluxo de formulários/Admin
        # (chamado por full_clean() na validação), mas um save() direto via
        # ORM (script, shell, código) nunca passa por clean() — por isso a
        # mesma checagem roda AQUI, incondicionalmente, antes de persistir.
        # Usa .values() (não .first()) para reconsultar o banco em vez de
        # confiar em self — um save() de uma instância Python desatualizada
        # não pode contornar a proteção.
        if self.pk:
            persistido = ReajusteContrato.objects.filter(pk=self.pk).values(
                'aplicado_em', *self.CAMPOS_PROTEGIDOS_APOS_APLICACAO
            ).first()
            if persistido and persistido['aplicado_em'] is not None:
                # aplicado_em em si é protegido explicitamente: nunca pode ser
                # apagado (None) nem substituído por outra data/hora — sem
                # isso, um save() direto poderia "desaplicar" o reajuste
                # (aplicado_em=None) sem passar pela checagem de campos
                # financeiros abaixo, já que aplicado_em não fazia parte de
                # CAMPOS_PROTEGIDOS_APOS_APLICACAO.
                if self.aplicado_em != persistido['aplicado_em']:
                    raise ValidationError(
                        'Este reajuste já foi aplicado — "aplicado em" não pode ser '
                        'apagado nem alterado (mesmo por save() direto).'
                    )
                if not self.aplicado:
                    raise ValidationError(
                        'Este reajuste já foi aplicado — não é possível desmarcá-lo '
                        '(mesmo por save() direto). Para corrigir, registre um novo reajuste.'
                    )
                for campo in self.CAMPOS_PROTEGIDOS_APOS_APLICACAO:
                    if getattr(self, campo) != persistido[campo]:
                        raise ValidationError(
                            'Este reajuste já foi aplicado — os campos financeiros são '
                            'imutáveis (mesmo por save() direto). Apenas as observações '
                            'podem ser alteradas. Para corrigir, registre um novo reajuste.'
                        )
        # Estados válidos de aplicado × aplicado_em (item 5, rodada de
        # fechamento estrutural): (False, None) ou (True, <data>) — nunca uma
        # combinação intermediária, mesmo por save() direto via ORM que
        # contorne clean(). Diferente de uma versão anterior desta proteção,
        # que CORRIGIA silenciosamente aplicado=False→True quando aplicado_em
        # vinha preenchido: agora REJEITA — preencher aplicado_em sem marcar
        # aplicado=True é sempre um estado inválido a ser corrigido pelo
        # chamador, nunca "consertado" nos bastidores. A aplicação inicial
        # legítima (aplicar()) já define os dois campos juntos ANTES deste
        # save(), então nunca cai nestas duas checagens.
        if self.aplicado and self.aplicado_em is None:
            raise ValidationError({
                'aplicado_em': 'Reajuste marcado como aplicado exige "aplicado em" '
                'preenchido — use o método aplicar() para aplicar um reajuste (mesmo '
                'por save() direto).'
            })
        if not self.aplicado and self.aplicado_em is not None:
            raise ValidationError({
                'aplicado': 'Reajuste com "aplicado em" preenchido deve estar marcado '
                'como aplicado — estado inconsistente (mesmo por save() direto).'
            })
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        # ATENÇÃO (manutenção): esta proteção só vale para .delete()/.save() de
        # INSTÂNCIA. QuerySet.delete() em massa e QuerySet.update() operam
        # direto no banco e NÃO chamam este método nem clean() — o manager
        # customizado (ReajusteContratoQuerySet, usado por `objects`) bloqueia
        # essas duas operações quando o conjunto inclui reajuste(s) aplicado(s);
        # mesmo assim, um acesso direto ao SQL por um DBA não é impedido por
        # nenhuma dessas camadas — fora do escopo desta proteção.
        #
        # NUNCA confia em self.aplicado_em: uma instância Python obtida ANTES
        # de o reajuste ser aplicado por outro código/processo continuaria
        # com aplicado_em=None em memória mesmo depois de aplicado no banco —
        # reconsulta o estado REAL antes de excluir.
        persistido = ReajusteContrato.objects.filter(pk=self.pk).values('aplicado_em').first()
        if persistido and persistido['aplicado_em'] is not None:
            raise ValidationError(
                'Este reajuste já foi aplicado e não pode ser excluído — o histórico '
                'financeiro do contrato depende dele. Para corrigir, registre um novo '
                'reajuste compensatório.'
            )
        super().delete(*args, **kwargs)

    def aplicar(self):
        """
        Aplica o reajuste: atualiza o valor vigente do contrato e do encargo
        de aluguel ativo, avança data_proximo_reajuste em 12 meses (exceto
        índice fixo) e marca aplicado/aplicado_em. Nunca altera receitas já
        geradas.

        Idempotente e atômico: se o reajuste já foi aplicado (aplicado_em
        preenchido no banco), retorna False sem reaplicar. Usa
        select_for_update e opera sobre os valores do REGISTRO BLOQUEADO no
        banco (não sobre self, que pode estar desatualizado); ao final, self
        é sincronizado com o estado persistido.
        Retorna True quando aplicou nesta chamada.
        """
        from django.db import transaction

        with transaction.atomic():
            atual = ReajusteContrato.objects.select_for_update().get(pk=self.pk)
            if atual.aplicado_em is not None:
                # sincroniza o objeto em memória com o estado real
                self.aplicado = True
                self.aplicado_em = atual.aplicado_em
                return False

            contrato = Contrato.objects.select_for_update().get(pk=atual.contrato_id)
            contrato.valor_aluguel = atual.valor_novo
            update_fields = ['valor_aluguel']
            if atual.indice != 'fixo':
                contrato.data_proximo_reajuste = _avancar_12_meses(atual.data_reajuste)
                update_fields.append('data_proximo_reajuste')
            contrato.save(update_fields=update_fields)

            encargo_aluguel = contrato.encargos.filter(tipo='aluguel', ativo=True).first()
            if encargo_aluguel:
                encargo_aluguel.valor = atual.valor_novo
                encargo_aluguel.save(update_fields=['valor'])

            atual.aplicado = True
            atual.aplicado_em = timezone.now()
            atual.save(update_fields=['aplicado', 'aplicado_em'])
        self.refresh_from_db()
        return True


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
    history = HistoricalRecords()

    class Meta:
        verbose_name = 'Manutenção'
        verbose_name_plural = 'Manutenções'
        ordering = ['-data_solicitacao']

    def __str__(self):
        return f'{self.descricao} — {self.imovel.nome} ({self.get_status_display()})'


def imobiliarias_queryset():
    """
    Pessoas que atuam como imobiliária: categoria preferencial 'imobiliaria',
    vínculo legado em Contrato.imobiliaria ou papel imobiliária em ContratoParte.
    Compartilhada pelas telas de contratos e relatórios.
    """
    ids = set(
        Contrato.objects.exclude(imobiliaria__isnull=True).values_list('imobiliaria_id', flat=True)
    )
    ids |= set(
        ContratoParte.objects.filter(papel='imobiliaria').values_list('pessoa_id', flat=True)
    )
    return Pessoa.objects.filter(Q(pk__in=ids) | Q(tipo='imobiliaria')).distinct().order_by('nome')
