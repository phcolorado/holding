from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError
from django.test import TestCase, Client
from django.urls import reverse
from django.utils import timezone

from .models import (
    Imovel, Pessoa, Contrato, ContratoParte, EncargoContrato, ReajusteContrato,
)


def _criar_imovel(nome='Imóvel Teste', **kwargs):
    defaults = dict(endereco='Rua A, 100', cidade='São Paulo', estado='SP')
    defaults.update(kwargs)
    return Imovel.objects.create(nome=nome, **defaults)


def _criar_pessoa(nome='Pessoa Teste', tipo='outro'):
    return Pessoa.objects.create(nome=nome, tipo=tipo)


def _criar_contrato(imovel, locatario, data_inicio, data_fim, **kwargs):
    defaults = dict(
        valor_aluguel=Decimal('2000.00'),
        dia_vencimento=10,
        status='ativo',
    )
    defaults.update(kwargs)
    return Contrato.objects.create(
        imovel=imovel, locatario=locatario,
        data_inicio=data_inicio, data_fim=data_fim,
        **defaults,
    )


# ─── Pessoa com múltiplos papéis ──────────────────────────────────────────────

class PessoaMultiplosPapeisTest(TestCase):
    """Uma pessoa pode ser locatária em um contrato e fiadora em outro."""

    def test_pessoa_locataria_em_um_e_fiadora_em_outro(self):
        pessoa = _criar_pessoa('Maria', tipo='locatario')
        imovel1 = _criar_imovel('Imóvel 1')
        imovel2 = _criar_imovel('Imóvel 2')
        outro_locatario = _criar_pessoa('Outro Locatário')

        contrato1 = _criar_contrato(imovel1, pessoa, date(2024, 1, 1), date(2024, 12, 31))
        contrato2 = _criar_contrato(imovel2, outro_locatario, date(2024, 1, 1), date(2024, 12, 31))

        ContratoParte.objects.create(contrato=contrato1, pessoa=pessoa, papel='locatario')
        ContratoParte.objects.create(contrato=contrato2, pessoa=pessoa, papel='fiador')

        self.assertIn(pessoa, contrato1.get_locatarios())
        self.assertIn(pessoa, contrato2.get_fiadores())

    def test_tipo_nao_restringe_uso_em_contratos(self):
        """Uma pessoa cadastrada com tipo='fiador' pode ser usada como locatária."""
        pessoa = _criar_pessoa('João', tipo='fiador')
        imovel = _criar_imovel()
        contrato = _criar_contrato(imovel, pessoa, date(2024, 1, 1), date(2024, 12, 31))
        parte = ContratoParte(contrato=contrato, pessoa=pessoa, papel='locatario')
        parte.full_clean()
        parte.save()
        self.assertIn(pessoa, contrato.get_locatarios())


# ─── ContratoParte ─────────────────────────────────────────────────────────────

class ContratoParteTest(TestCase):
    """Testa múltiplos locatários/fiadores e fallback para campos legados."""

    def setUp(self):
        self.imovel = _criar_imovel()
        self.locatario = _criar_pessoa('Locatário Legado')
        self.contrato = _criar_contrato(self.imovel, self.locatario, date(2024, 1, 1), date(2024, 12, 31))

    def test_contrato_aceita_multiplos_locatarios(self):
        pessoa2 = _criar_pessoa('Segundo Locatário')
        ContratoParte.objects.create(contrato=self.contrato, pessoa=self.locatario, papel='locatario')
        ContratoParte.objects.create(contrato=self.contrato, pessoa=pessoa2, papel='locatario')

        locatarios = self.contrato.get_locatarios()
        self.assertEqual(len(locatarios), 2)
        self.assertIn(self.locatario, locatarios)
        self.assertIn(pessoa2, locatarios)

    def test_contrato_aceita_multiplos_fiadores(self):
        fiador1 = _criar_pessoa('Fiador 1')
        fiador2 = _criar_pessoa('Fiador 2')
        ContratoParte.objects.create(contrato=self.contrato, pessoa=fiador1, papel='fiador')
        ContratoParte.objects.create(contrato=self.contrato, pessoa=fiador2, papel='fiador')

        fiadores = self.contrato.get_fiadores()
        self.assertEqual(len(fiadores), 2)
        self.assertIn(fiador1, fiadores)
        self.assertIn(fiador2, fiadores)

    def test_fallback_para_campo_legado_sem_partes(self):
        """Sem ContratoParte cadastradas, get_locatarios() usa o campo legado."""
        self.assertEqual(self.contrato.get_locatarios(), [self.locatario])
        self.assertEqual(self.contrato.locatarios_display, self.locatario.nome)

    def test_locatarios_display_junta_nomes(self):
        pessoa2 = _criar_pessoa('Segundo Locatário')
        ContratoParte.objects.create(contrato=self.contrato, pessoa=self.locatario, papel='locatario')
        ContratoParte.objects.create(contrato=self.contrato, pessoa=pessoa2, papel='locatario')
        self.assertIn(self.locatario.nome, self.contrato.locatarios_display)
        self.assertIn(pessoa2.nome, self.contrato.locatarios_display)

    def test_unique_together_impede_duplicata_exata(self):
        ContratoParte.objects.create(contrato=self.contrato, pessoa=self.locatario, papel='locatario')
        with self.assertRaises(IntegrityError):
            ContratoParte.objects.create(contrato=self.contrato, pessoa=self.locatario, papel='locatario')

    def test_get_imobiliaria_principal_fallback_legado(self):
        imobiliaria = _criar_pessoa('Imob Legado', tipo='imobiliaria')
        self.contrato.imobiliaria = imobiliaria
        self.contrato.save()
        self.assertEqual(self.contrato.get_imobiliaria_principal(), imobiliaria)

    def test_get_imobiliaria_principal_via_parte(self):
        imobiliaria = _criar_pessoa('Imob Nova', tipo='imobiliaria')
        ContratoParte.objects.create(contrato=self.contrato, pessoa=imobiliaria, papel='imobiliaria')
        self.assertEqual(self.contrato.get_imobiliaria_principal(), imobiliaria)


# ─── Data migrations (partes e encargo de aluguel) ────────────────────────────

class DataMigrationPartesTest(TestCase):
    """
    Testa a lógica da data migration 0004_migrar_partes_contrato: cria
    ContratoParte a partir dos campos legados locatario/fiador/imobiliaria.
    """

    def test_migrar_partes_cria_contratoparte_para_campos_legados(self):
        import importlib
        from django.apps import apps as django_apps

        imovel = _criar_imovel()
        locatario = _criar_pessoa('Locatário Legado')
        fiador = _criar_pessoa('Fiador Legado')
        imobiliaria = _criar_pessoa('Imobiliária Legada', tipo='imobiliaria')
        contrato = _criar_contrato(
            imovel, locatario, date(2024, 1, 1), date(2024, 12, 31),
            fiador=fiador, imobiliaria=imobiliaria,
        )
        # Nenhuma ContratoParte deve existir ainda (simula estado pré-migração)
        self.assertEqual(ContratoParte.objects.filter(contrato=contrato).count(), 0)

        mod = importlib.import_module('patrimonio.migrations.0004_migrar_partes_contrato')
        mod.migrar_partes(django_apps, None)

        self.assertTrue(
            ContratoParte.objects.filter(contrato=contrato, pessoa=locatario, papel='locatario').exists()
        )
        self.assertTrue(
            ContratoParte.objects.filter(contrato=contrato, pessoa=fiador, papel='fiador').exists()
        )
        self.assertTrue(
            ContratoParte.objects.filter(contrato=contrato, pessoa=imobiliaria, papel='imobiliaria').exists()
        )

    def test_migrar_partes_idempotente(self):
        import importlib
        from django.apps import apps as django_apps

        imovel = _criar_imovel()
        locatario = _criar_pessoa('Locatário Legado')
        contrato = _criar_contrato(imovel, locatario, date(2024, 1, 1), date(2024, 12, 31))

        mod = importlib.import_module('patrimonio.migrations.0004_migrar_partes_contrato')
        mod.migrar_partes(django_apps, None)
        mod.migrar_partes(django_apps, None)

        self.assertEqual(
            ContratoParte.objects.filter(contrato=contrato, pessoa=locatario, papel='locatario').count(), 1
        )


class DataMigrationEncargoAluguelTest(TestCase):
    """Testa a lógica da data migration 0005_migrar_encargo_aluguel."""

    def test_migrar_encargo_cria_encargo_aluguel_a_partir_do_valor(self):
        import importlib
        from django.apps import apps as django_apps

        imovel = _criar_imovel()
        locatario = _criar_pessoa('Locatário')
        contrato = _criar_contrato(
            imovel, locatario, date(2024, 1, 1), date(2024, 12, 31), valor_aluguel=Decimal('3500.00')
        )
        self.assertEqual(EncargoContrato.objects.filter(contrato=contrato).count(), 0)

        mod = importlib.import_module('patrimonio.migrations.0005_migrar_encargo_aluguel')
        mod.migrar_encargo_aluguel(django_apps, None)

        encargo = EncargoContrato.objects.get(contrato=contrato, tipo='aluguel')
        self.assertEqual(encargo.valor, Decimal('3500.00'))
        self.assertEqual(encargo.periodicidade, 'mensal')


# ─── Contrato por prazo indeterminado ──────────────────────────────────────────

class ContratoPrazoIndeterminadoTest(TestCase):
    def setUp(self):
        self.imovel = _criar_imovel()
        self.locatario = _criar_pessoa('Locatário')

    def test_ativo_prazo_indeterminado_nao_aparece_vencido(self):
        ontem_ano_passado = date.today().replace(year=date.today().year - 1)
        contrato = _criar_contrato(
            self.imovel, self.locatario,
            data_inicio=date(2020, 1, 1), data_fim=ontem_ano_passado,
            prazo_indeterminado=True,
        )
        self.assertFalse(contrato.esta_vencido)
        self.assertIsNone(contrato.data_fim_efetiva)

    def test_prazo_determinado_vencido_aparece_vencido(self):
        ontem = date.today() - timedelta(days=1)
        contrato = _criar_contrato(
            self.imovel, self.locatario,
            data_inicio=date(2020, 1, 1), data_fim=ontem,
        )
        self.assertTrue(contrato.esta_vencido)

    def test_data_encerramento_real_limita_data_fim_efetiva(self):
        encerramento = date(2024, 6, 15)
        contrato = _criar_contrato(
            self.imovel, self.locatario,
            data_inicio=date(2020, 1, 1), data_fim=date(2030, 12, 31),
            prazo_indeterminado=True, data_encerramento_real=encerramento,
        )
        self.assertEqual(contrato.data_fim_efetiva, encerramento)

    def test_vigencia_display_prazo_indeterminado(self):
        contrato = _criar_contrato(
            self.imovel, self.locatario,
            data_inicio=date(2020, 1, 1), data_fim=date(2021, 1, 1),
            prazo_indeterminado=True,
        )
        self.assertIn('Prazo Indeterminado', contrato.vigencia_display)

    def test_overlap_considera_prazo_indeterminado_como_aberto(self):
        """Um contrato ativo por prazo indeterminado bloqueia sobreposição mesmo após a data_fim original."""
        _criar_contrato(
            self.imovel, self.locatario,
            data_inicio=date(2020, 1, 1), data_fim=date(2021, 1, 1),
            prazo_indeterminado=True,
        )
        outro_locatario = _criar_pessoa('Outro')
        novo = Contrato(
            imovel=self.imovel, locatario=outro_locatario,
            data_inicio=date(2025, 1, 1), data_fim=date(2026, 1, 1),
            valor_aluguel=Decimal('1000.00'), dia_vencimento=5, status='ativo',
        )
        with self.assertRaises(ValidationError):
            novo.clean()

    def test_overlap_respeita_encerramento_real(self):
        """Contrato com data_encerramento_real não bloqueia períodos após o encerramento."""
        _criar_contrato(
            self.imovel, self.locatario,
            data_inicio=date(2020, 1, 1), data_fim=date(2030, 1, 1),
            prazo_indeterminado=True, data_encerramento_real=date(2023, 12, 31),
        )
        outro_locatario = _criar_pessoa('Outro')
        novo = Contrato(
            imovel=self.imovel, locatario=outro_locatario,
            data_inicio=date(2024, 1, 1), data_fim=date(2025, 1, 1),
            valor_aluguel=Decimal('1000.00'), dia_vencimento=5, status='ativo',
        )
        try:
            novo.clean()
        except ValidationError:
            self.fail('Não deveria bloquear período posterior ao encerramento real.')


# ─── EncargoContrato ────────────────────────────────────────────────────────────

class EncargoContratoTest(TestCase):
    def setUp(self):
        self.imovel = _criar_imovel()
        self.locatario = _criar_pessoa('Locatário')
        self.contrato = _criar_contrato(self.imovel, self.locatario, date(2024, 1, 1), date(2024, 12, 31))

    def test_aluguel_deve_ser_mensal(self):
        encargo = EncargoContrato(
            contrato=self.contrato, tipo='aluguel', valor=Decimal('2000.00'), periodicidade='anual',
        )
        with self.assertRaises(ValidationError):
            encargo.clean()

    def test_encargo_mensal_ativo_aplica_em_qualquer_mes(self):
        encargo = EncargoContrato.objects.create(
            contrato=self.contrato, tipo='condominio', valor=Decimal('300.00'), periodicidade='mensal',
        )
        self.assertTrue(encargo.aplica_em(2024, 3))
        self.assertTrue(encargo.aplica_em(2024, 11))

    def test_encargo_com_inicio_futuro_nao_aplica_antes(self):
        encargo = EncargoContrato.objects.create(
            contrato=self.contrato, tipo='iptu', valor=Decimal('150.00'), periodicidade='mensal',
            data_inicio_cobranca=date(2024, 6, 1),
        )
        self.assertFalse(encargo.aplica_em(2024, 3))
        self.assertTrue(encargo.aplica_em(2024, 6))
        self.assertTrue(encargo.aplica_em(2024, 7))

    def test_encargo_encerrado_nao_aplica_depois(self):
        encargo = EncargoContrato.objects.create(
            contrato=self.contrato, tipo='seguro', valor=Decimal('80.00'), periodicidade='mensal',
            data_fim_cobranca=date(2024, 6, 30),
        )
        self.assertTrue(encargo.aplica_em(2024, 6))
        self.assertFalse(encargo.aplica_em(2024, 7))

    def test_encargo_anual_aplica_apenas_no_mes_de_referencia(self):
        encargo = EncargoContrato.objects.create(
            contrato=self.contrato, tipo='iptu', valor=Decimal('1200.00'), periodicidade='anual',
            data_inicio_cobranca=date(2024, 3, 1),
        )
        self.assertTrue(encargo.aplica_em(2024, 3))
        self.assertFalse(encargo.aplica_em(2024, 4))

    def test_encargo_inativo_nunca_aplica(self):
        encargo = EncargoContrato.objects.create(
            contrato=self.contrato, tipo='outro', valor=Decimal('50.00'), ativo=False,
        )
        self.assertFalse(encargo.aplica_em(2024, 6))


# ─── ReajusteContrato ───────────────────────────────────────────────────────────

class ReajusteContratoTest(TestCase):
    def setUp(self):
        self.imovel = _criar_imovel()
        self.locatario = _criar_pessoa('Locatário')
        self.contrato = _criar_contrato(
            self.imovel, self.locatario, date(2024, 1, 1), date(2025, 12, 31),
            valor_aluguel=Decimal('2000.00'), indice_reajuste='ipca',
            data_proximo_reajuste=date(2025, 1, 1),
        )
        self.encargo_aluguel = EncargoContrato.objects.create(
            contrato=self.contrato, tipo='aluguel', valor=Decimal('2000.00'),
        )

    def test_aplicar_atualiza_valor_vigente_do_contrato(self):
        reajuste = ReajusteContrato.objects.create(
            contrato=self.contrato, data_reajuste=date(2025, 1, 1), indice='ipca',
            valor_anterior=Decimal('2000.00'), valor_novo=Decimal('2150.00'), aplicado=True,
        )
        reajuste.aplicar()
        self.contrato.refresh_from_db()
        self.assertEqual(self.contrato.valor_aluguel, Decimal('2150.00'))

    def test_aplicar_atualiza_encargo_aluguel(self):
        reajuste = ReajusteContrato.objects.create(
            contrato=self.contrato, data_reajuste=date(2025, 1, 1), indice='ipca',
            valor_anterior=Decimal('2000.00'), valor_novo=Decimal('2150.00'), aplicado=True,
        )
        reajuste.aplicar()
        self.encargo_aluguel.refresh_from_db()
        self.assertEqual(self.encargo_aluguel.valor, Decimal('2150.00'))

    def test_aplicar_avanca_data_proximo_reajuste_12_meses(self):
        reajuste = ReajusteContrato.objects.create(
            contrato=self.contrato, data_reajuste=date(2025, 1, 1), indice='ipca',
            valor_anterior=Decimal('2000.00'), valor_novo=Decimal('2150.00'), aplicado=True,
        )
        reajuste.aplicar()
        self.contrato.refresh_from_db()
        self.assertEqual(self.contrato.data_proximo_reajuste, date(2026, 1, 1))

    def test_aplicar_indice_fixo_nao_avanca_data(self):
        self.contrato.indice_reajuste = 'fixo'
        self.contrato.save()
        reajuste = ReajusteContrato.objects.create(
            contrato=self.contrato, data_reajuste=date(2025, 1, 1), indice='fixo',
            valor_anterior=Decimal('2000.00'), valor_novo=Decimal('2100.00'), aplicado=True,
        )
        reajuste.aplicar()
        self.contrato.refresh_from_db()
        self.assertEqual(self.contrato.data_proximo_reajuste, date(2025, 1, 1))

    def test_aplicar_cria_historico(self):
        ReajusteContrato.objects.create(
            contrato=self.contrato, data_reajuste=date(2025, 1, 1), indice='ipca',
            valor_anterior=Decimal('2000.00'), valor_novo=Decimal('2150.00'), aplicado=True,
        )
        self.assertEqual(ReajusteContrato.objects.filter(contrato=self.contrato).count(), 1)

    def test_aplicar_nao_altera_receitas_ja_recebidas(self):
        from financeiro.models import ReceitaAluguel
        receita = ReceitaAluguel.objects.create(
            contrato=self.contrato, imovel=self.imovel,
            competencia_mes=1, competencia_ano=2025,
            data_vencimento=date(2025, 1, 10),
            valor_previsto=Decimal('2000.00'), valor_recebido=Decimal('2000.00'),
            status='recebido',
        )
        reajuste = ReajusteContrato.objects.create(
            contrato=self.contrato, data_reajuste=date(2025, 1, 1), indice='ipca',
            valor_anterior=Decimal('2000.00'), valor_novo=Decimal('2150.00'), aplicado=True,
        )
        reajuste.aplicar()
        receita.refresh_from_db()
        self.assertEqual(receita.valor_previsto, Decimal('2000.00'))
        self.assertEqual(receita.status, 'recebido')

    def test_reajuste_pendente_aparece_no_dashboard(self):
        self.contrato.data_proximo_reajuste = date.today() - timedelta(days=1)
        self.contrato.save()
        client = Client()
        User.objects.create_user('dash_reajuste', password='pass')
        client.login(username='dash_reajuste', password='pass')
        response = client.get(reverse('dashboard'))
        self.assertEqual(response.status_code, 200)
        self.assertGreaterEqual(response.context['reajustes_pendentes'], 1)


# ─── Imóvel: tipo, uso, imóvel pai e unidades ──────────────────────────────────

class ImovelClassificacaoTest(TestCase):
    def test_imovel_pode_ter_tipo_e_uso(self):
        imovel = _criar_imovel(tipo_imovel='sala_comercial', uso='comercial')
        self.assertEqual(imovel.tipo_imovel, 'sala_comercial')
        self.assertEqual(imovel.uso, 'comercial')

    def test_imovel_pode_ter_imovel_pai(self):
        predio = _criar_imovel('Prédio Central', tipo_imovel='predio', unidade_locavel=False)
        unidade = _criar_imovel('Prédio Central — 1º Andar', tipo_imovel='andar', imovel_pai=predio)
        self.assertEqual(unidade.imovel_pai, predio)

    def test_predio_pode_ter_unidades(self):
        predio = _criar_imovel('Prédio Central', tipo_imovel='predio', unidade_locavel=False)
        _criar_imovel('Sala 101', tipo_imovel='sala_comercial', imovel_pai=predio)
        _criar_imovel('Sala 102', tipo_imovel='sala_comercial', imovel_pai=predio)
        self.assertEqual(predio.unidades.count(), 2)


class ImovelListFiltrosTest(TestCase):
    def setUp(self):
        self.client = Client()
        User.objects.create_user('imovel_user', password='pass')
        self.client.login(username='imovel_user', password='pass')
        _criar_imovel('Apto Residencial', tipo_imovel='apartamento', uso='residencial')
        _criar_imovel('Loja Comercial', tipo_imovel='loja', uso='comercial')

    def test_filtro_por_tipo_imovel(self):
        response = self.client.get(reverse('imovel_list'), {'tipo_imovel': 'loja'})
        nomes = [i.nome for i in response.context['imoveis']]
        self.assertIn('Loja Comercial', nomes)
        self.assertNotIn('Apto Residencial', nomes)

    def test_filtro_por_uso(self):
        response = self.client.get(reverse('imovel_list'), {'uso': 'residencial'})
        nomes = [i.nome for i in response.context['imoveis']]
        self.assertIn('Apto Residencial', nomes)
        self.assertNotIn('Loja Comercial', nomes)


# ─── Listagem de contratos: múltiplos locatários e filtro por imobiliária ─────

class ContratoListViewTest(TestCase):
    def setUp(self):
        self.client = Client()
        User.objects.create_user('contrato_user', password='pass')
        self.client.login(username='contrato_user', password='pass')
        self.imovel = _criar_imovel()
        self.locatario1 = _criar_pessoa('Ana Locatária')
        self.locatario2 = _criar_pessoa('Bruno Locatário')
        self.contrato = _criar_contrato(self.imovel, self.locatario1, date(2024, 1, 1), date(2025, 12, 31))
        ContratoParte.objects.create(contrato=self.contrato, pessoa=self.locatario1, papel='locatario')
        ContratoParte.objects.create(contrato=self.contrato, pessoa=self.locatario2, papel='locatario')

    def test_listagem_mostra_multiplos_locatarios(self):
        response = self.client.get(reverse('contrato_list'))
        content = response.content.decode()
        self.assertIn('Ana Locatária', content)
        self.assertIn('Bruno Locatário', content)

    def test_filtro_por_imobiliaria(self):
        imobiliaria = _criar_pessoa('Imob X', tipo='imobiliaria')
        imovel2 = _criar_imovel('Imóvel 2')
        outro_locatario = _criar_pessoa('Outro Locatário')
        contrato2 = _criar_contrato(imovel2, outro_locatario, date(2024, 1, 1), date(2025, 12, 31))
        ContratoParte.objects.create(contrato=contrato2, pessoa=imobiliaria, papel='imobiliaria')

        response = self.client.get(reverse('contrato_list'), {'imobiliaria': imobiliaria.pk})
        contratos = list(response.context['contratos'])
        self.assertIn(contrato2, contratos)
        self.assertNotIn(self.contrato, contratos)

    def test_sem_filtro_imobiliaria_mostra_todos(self):
        response = self.client.get(reverse('contrato_list'))
        contratos = list(response.context['contratos'])
        self.assertIn(self.contrato, contratos)

    def test_filtro_reajuste_pendente(self):
        self.contrato.data_proximo_reajuste = date.today() - timedelta(days=1)
        self.contrato.save()
        imovel2 = _criar_imovel('Imóvel Sem Reajuste')
        outro_locatario = _criar_pessoa('Outro')
        contrato_em_dia = _criar_contrato(
            imovel2, outro_locatario, date(2024, 1, 1), date(2025, 12, 31),
            data_proximo_reajuste=date.today() + timedelta(days=30),
        )
        response = self.client.get(reverse('contrato_list'), {'reajuste_pendente': '1'})
        contratos = list(response.context['contratos'])
        self.assertIn(self.contrato, contratos)
        self.assertNotIn(contrato_em_dia, contratos)


# ─── Documento vinculado ao contrato (inline do admin) ────────────────────────

class DocumentoContratoInlineTest(TestCase):
    """
    Testa a lógica de preenchimento automático de imovel no inline de
    Documento em ContratoAdmin.save_formset, usando um formset simplificado
    (evita a complexidade de simular o POST completo do admin do Django).
    """

    class _FakeFormSet:
        def __init__(self, model, instances):
            self.model = model
            self._instances = instances
            self.deleted_objects = []

        def save(self, commit=False):
            return self._instances

        def save_m2m(self):
            pass

    class _FakeForm:
        def __init__(self, instance):
            self.instance = instance

    def test_documento_inline_preenche_imovel_automaticamente(self):
        from django.contrib import admin as django_admin
        from documentos.models import Documento
        from patrimonio.admin import ContratoAdmin

        imovel = _criar_imovel()
        locatario = _criar_pessoa('Locatário')
        contrato = _criar_contrato(imovel, locatario, date(2024, 1, 1), date(2024, 12, 31))

        doc = Documento(
            contrato=contrato, titulo='Contrato Assinado', tipo='contrato_aluguel',
            arquivo=SimpleUploadedFile('contrato.pdf', b'conteudo'),
        )

        admin_instance = ContratoAdmin(Contrato, django_admin.site)
        formset = self._FakeFormSet(Documento, [doc])
        form = self._FakeForm(contrato)
        admin_instance.save_formset(request=None, form=form, formset=formset, change=True)

        doc.refresh_from_db()
        self.assertEqual(doc.imovel_id, contrato.imovel_id)
        self.assertEqual(doc.contrato_id, contrato.pk)

    def test_documento_do_contrato_aparece_vinculado(self):
        from documentos.models import Documento

        imovel = _criar_imovel()
        locatario = _criar_pessoa('Locatário')
        contrato = _criar_contrato(imovel, locatario, date(2024, 1, 1), date(2024, 12, 31))
        Documento.objects.create(
            contrato=contrato, imovel=imovel, titulo='Aditivo', tipo='outro',
            arquivo=SimpleUploadedFile('aditivo.pdf', b'x'),
        )
        self.assertEqual(contrato.documento_set.count(), 1)
