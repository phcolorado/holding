from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from core.test_utils import com_leitura
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError
from django.test import TestCase, TransactionTestCase, Client
from django.urls import reverse
from django.utils import timezone

from .models import (
    Imovel, Pessoa, Contrato, ContratoParte, EncargoContrato, ReajusteContrato,
    Manutencao,
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
        com_leitura(User.objects.create_user('dash_reajuste', password='pass'))
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
        com_leitura(User.objects.create_user('imovel_user', password='pass'))
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
        com_leitura(User.objects.create_user('contrato_user', password='pass'))
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


# ─── garantir_encargo_aluguel ──────────────────────────────────────────────────

class GarantirEncargoAluguelTest(TestCase):
    def setUp(self):
        self.imovel = _criar_imovel()
        self.locatario = _criar_pessoa('Locatário')

    def test_contrato_sem_encargos_cria_encargo_aluguel(self):
        contrato = _criar_contrato(
            self.imovel, self.locatario, date(2024, 1, 1), date(2024, 12, 31),
            valor_aluguel=Decimal('1100.00'),
        )
        encargo = contrato.garantir_encargo_aluguel()
        self.assertIsNotNone(encargo)
        self.assertEqual(encargo.tipo, 'aluguel')
        self.assertEqual(encargo.valor, Decimal('1100.00'))
        self.assertEqual(encargo.periodicidade, 'mensal')
        self.assertEqual(encargo.data_inicio_cobranca, contrato.data_inicio)

    def test_contrato_com_encargo_aluguel_nao_duplica(self):
        contrato = _criar_contrato(self.imovel, self.locatario, date(2024, 1, 1), date(2024, 12, 31))
        contrato.garantir_encargo_aluguel()
        resultado_segunda_chamada = contrato.garantir_encargo_aluguel()
        self.assertIsNone(resultado_segunda_chamada)
        self.assertEqual(EncargoContrato.objects.filter(contrato=contrato, tipo='aluguel').count(), 1)

    def test_encargo_aluguel_manual_nao_e_sobrescrito(self):
        contrato = _criar_contrato(
            self.imovel, self.locatario, date(2024, 1, 1), date(2024, 12, 31),
            valor_aluguel=Decimal('2000.00'),
        )
        encargo_manual = EncargoContrato.objects.create(
            contrato=contrato, tipo='aluguel', descricao='Aluguel negociado',
            valor=Decimal('1850.00'),
        )
        resultado = contrato.garantir_encargo_aluguel()
        self.assertIsNone(resultado)
        encargo_manual.refresh_from_db()
        self.assertEqual(encargo_manual.valor, Decimal('1850.00'))
        self.assertEqual(encargo_manual.descricao, 'Aluguel negociado')

    def test_contrato_encerrado_nao_cria_encargo(self):
        contrato = _criar_contrato(
            self.imovel, self.locatario, date(2024, 1, 1), date(2024, 12, 31),
            status='encerrado',
        )
        resultado = contrato.garantir_encargo_aluguel()
        self.assertIsNone(resultado)
        self.assertFalse(EncargoContrato.objects.filter(contrato=contrato, tipo='aluguel').exists())

    def test_admin_save_model_nao_cria_encargo_sozinho(self):
        """
        save_model() roda antes dos inlines — não deve mais chamar
        garantir_encargo_aluguel() diretamente (isso ficou para
        save_related(), depois que o inline de encargos salva).
        """
        from django.contrib import admin as django_admin
        from django.contrib.auth.models import User
        from django.test import RequestFactory
        from patrimonio.admin import ContratoAdmin

        contrato = _criar_contrato(self.imovel, self.locatario, date(2024, 1, 1), date(2024, 12, 31))
        admin_instance = ContratoAdmin(Contrato, django_admin.site)
        request = RequestFactory().post('/admin/patrimonio/contrato/add/')
        request.user = com_leitura(User.objects.create_user('admin_encargo', password='pass'))
        admin_instance.save_model(request=request, obj=contrato, form=None, change=False)

        self.assertFalse(contrato.encargos.filter(tipo='aluguel').exists())

    def test_admin_action_garante_encargo_aluguel(self):
        from django.contrib import admin as django_admin
        from django.contrib.messages.storage.fallback import FallbackStorage
        from django.test import RequestFactory
        from patrimonio.admin import ContratoAdmin

        contrato = _criar_contrato(self.imovel, self.locatario, date(2024, 1, 1), date(2024, 12, 31))
        admin_instance = ContratoAdmin(Contrato, django_admin.site)
        request = RequestFactory().get('/admin/patrimonio/contrato/')
        request.session = {}
        request._messages = FallbackStorage(request)

        admin_instance.garantir_encargo_aluguel_action(request, Contrato.objects.filter(pk=contrato.pk))

        self.assertTrue(contrato.encargos.filter(tipo='aluguel', ativo=True).exists())


class _FakeFormComM2M:
    """Form simplificado com o .instance e o .save_m2m() no-op exigidos por ModelAdmin.save_related()."""

    def __init__(self, instance):
        self.instance = instance

    def save_m2m(self):
        pass


class _FakeEncargoInlineFormSet:
    """
    Representa o inline de EncargoContrato já preenchido pelo usuário no
    admin. Seu .save() simula o que o formset real faz: persiste as linhas
    do inline no banco — chamado por ContratoAdmin.save_formset() dentro de
    ModelAdmin.save_related(), antes de garantir_encargo_aluguel() rodar.
    """

    def __init__(self, contrato, encargos_kwargs):
        self.model = EncargoContrato
        self.deleted_objects = []
        self._contrato = contrato
        self._encargos_kwargs = encargos_kwargs

    def save(self, commit=True):
        return [
            EncargoContrato.objects.create(contrato=self._contrato, **kwargs)
            for kwargs in self._encargos_kwargs
        ]


class GarantirEncargoAluguelSaveRelatedTest(TestCase):
    """
    Testa a ordem correta no Admin: 1) contrato já salvo (save_model já
    rodou); 2) inline de encargos salva via save_related()/save_formset();
    3) só então garantir_encargo_aluguel() roda — nunca duplicando um
    encargo de aluguel cadastrado manualmente no mesmo formulário.
    """

    def setUp(self):
        self.imovel = _criar_imovel()
        self.locatario = _criar_pessoa('Locatário')

    def _save_related(self, contrato, formset):
        from django.contrib import admin as django_admin
        from patrimonio.admin import ContratoAdmin

        admin_instance = ContratoAdmin(Contrato, django_admin.site)
        form = _FakeFormComM2M(contrato)
        admin_instance.save_related(request=None, form=form, formsets=[formset], change=False)

    def test_save_related_cria_encargo_quando_inline_nao_tem_aluguel(self):
        contrato = _criar_contrato(
            self.imovel, self.locatario, date(2024, 1, 1), date(2024, 12, 31),
            valor_aluguel=Decimal('1100.00'),
        )
        formset_vazio = _FakeEncargoInlineFormSet(contrato, [])
        self._save_related(contrato, formset_vazio)

        self.assertEqual(contrato.encargos.filter(tipo='aluguel', ativo=True).count(), 1)
        self.assertEqual(contrato.encargos.get(tipo='aluguel').valor, Decimal('1100.00'))

    def test_save_related_nao_duplica_quando_inline_ja_tem_encargo_aluguel(self):
        contrato = _criar_contrato(
            self.imovel, self.locatario, date(2024, 1, 1), date(2024, 12, 31),
            valor_aluguel=Decimal('2000.00'),
        )
        formset_com_manual = _FakeEncargoInlineFormSet(contrato, [
            {
                'tipo': 'aluguel', 'descricao': 'Aluguel negociado',
                'valor': Decimal('1850.00'), 'periodicidade': 'mensal', 'ativo': True,
            },
        ])
        self._save_related(contrato, formset_com_manual)

        # O inline salvou o encargo manual ANTES de garantir_encargo_aluguel()
        # rodar — não deve haver um segundo encargo de aluguel criado por cima.
        self.assertEqual(contrato.encargos.filter(tipo='aluguel', ativo=True).count(), 1)
        encargo = contrato.encargos.get(tipo='aluguel', ativo=True)
        self.assertEqual(encargo.valor, Decimal('1850.00'))
        self.assertEqual(encargo.descricao, 'Aluguel negociado')


# ─── ContratoAdmin.save_formset: transição de aplicado em ReajusteContrato ────

class _FakeReajusteFormSet:
    def __init__(self, instances):
        self.model = ReajusteContrato
        self._instances = instances
        self.deleted_objects = []

    def save(self, commit=False):
        return self._instances

    def save_m2m(self):
        pass


class ReajusteAdminTransicaoTest(TestCase):
    """Testa ContratoAdmin.save_formset para ReajusteContrato via formset simplificado."""

    def setUp(self):
        self.imovel = _criar_imovel()
        self.locatario = _criar_pessoa('Locatário')
        self.contrato = _criar_contrato(
            self.imovel, self.locatario, date(2024, 1, 1), date(2025, 12, 31),
            valor_aluguel=Decimal('2000.00'), indice_reajuste='ipca',
            data_proximo_reajuste=date(2025, 1, 1),
        )
        EncargoContrato.objects.create(contrato=self.contrato, tipo='aluguel', valor=Decimal('2000.00'))

    def _save_formset(self, reajuste):
        from django.contrib import admin as django_admin
        from patrimonio.admin import ContratoAdmin

        admin_instance = ContratoAdmin(Contrato, django_admin.site)
        formset = _FakeReajusteFormSet([reajuste])
        form = DocumentoContratoInlineTest._FakeForm(self.contrato)
        admin_instance.save_formset(request=None, form=form, formset=formset, change=True)

    def test_reajuste_novo_aplicado_true_aplica(self):
        reajuste = ReajusteContrato(
            contrato=self.contrato, data_reajuste=date(2025, 1, 1), indice='ipca',
            valor_anterior=Decimal('2000.00'), valor_novo=Decimal('2150.00'), aplicado=True,
        )
        self._save_formset(reajuste)
        self.contrato.refresh_from_db()
        self.assertEqual(self.contrato.valor_aluguel, Decimal('2150.00'))

    def test_reajuste_existente_de_false_para_true_aplica(self):
        reajuste = ReajusteContrato.objects.create(
            contrato=self.contrato, data_reajuste=date(2025, 1, 1), indice='ipca',
            valor_anterior=Decimal('2000.00'), valor_novo=Decimal('2150.00'), aplicado=False,
        )
        reajuste.aplicado = True  # simula edição no admin, ainda não salva
        self._save_formset(reajuste)

        self.contrato.refresh_from_db()
        self.assertEqual(self.contrato.valor_aluguel, Decimal('2150.00'))
        self.assertEqual(self.contrato.data_proximo_reajuste, date(2026, 1, 1))

        encargo = self.contrato.encargos.get(tipo='aluguel')
        self.assertEqual(encargo.valor, Decimal('2150.00'))

    def test_reajuste_ja_aplicado_nao_reaplica(self):
        reajuste = ReajusteContrato.objects.create(
            contrato=self.contrato, data_reajuste=date(2025, 1, 1), indice='ipca',
            valor_anterior=Decimal('2000.00'), valor_novo=Decimal('2150.00'), aplicado=True,
        )
        # Primeira aplicação real (fora do save_formset, simulando estado já processado)
        reajuste.aplicar()
        self.contrato.refresh_from_db()
        self.assertEqual(self.contrato.valor_aluguel, Decimal('2150.00'))

        # Um segundo reajuste é registrado; ao salvar o formset novamente, o
        # primeiro reajuste (já aplicado=True antes e depois) não deve reaplicar.
        reajuste.observacoes = 'apenas edição de observação'
        self._save_formset(reajuste)

        self.contrato.refresh_from_db()
        self.assertEqual(self.contrato.valor_aluguel, Decimal('2150.00'))
        self.assertEqual(self.contrato.data_proximo_reajuste, date(2026, 1, 1))

    def test_reajuste_novo_aplicado_false_nao_aplica(self):
        reajuste = ReajusteContrato(
            contrato=self.contrato, data_reajuste=date(2025, 1, 1), indice='ipca',
            valor_anterior=Decimal('2000.00'), valor_novo=Decimal('2150.00'), aplicado=False,
        )
        self._save_formset(reajuste)
        self.contrato.refresh_from_db()
        self.assertEqual(self.contrato.valor_aluguel, Decimal('2000.00'))

    def test_tentativa_de_exclusao_via_inline_e_bloqueada_e_avisada(self):
        """Item 10: exclusão de reajuste aplicado pelo inline não derruba a página, apenas avisa."""
        from django.contrib import admin as django_admin
        from django.contrib.messages.storage.fallback import FallbackStorage
        from django.test import RequestFactory
        from patrimonio.admin import ContratoAdmin

        reajuste = ReajusteContrato.objects.create(
            contrato=self.contrato, data_reajuste=date(2025, 1, 1), indice='ipca',
            valor_anterior=Decimal('2000.00'), valor_novo=Decimal('2150.00'), aplicado=True,
        )
        reajuste.aplicar()

        admin_instance = ContratoAdmin(Contrato, django_admin.site)
        formset = _FakeReajusteFormSet([])
        formset.deleted_objects = [reajuste]
        form = DocumentoContratoInlineTest._FakeForm(self.contrato)

        request = RequestFactory().get('/')
        request.session = {}
        request._messages = FallbackStorage(request)

        admin_instance.save_formset(request=request, form=form, formset=formset, change=True)

        self.assertTrue(ReajusteContrato.objects.filter(pk=reajuste.pk).exists())
        mensagens = [str(m) for m in request._messages]
        self.assertTrue(any('já foi aplicado' in m for m in mensagens))


# ─── Dashboard: contratos vencendo respeita prazo indeterminado ───────────────

class DashboardContratosVencendoTest(TestCase):
    def setUp(self):
        self.client = Client()
        com_leitura(User.objects.create_user('dash_vencendo_user', password='pass'))
        self.client.login(username='dash_vencendo_user', password='pass')
        self.imovel1 = _criar_imovel('Imóvel Determinado')
        self.imovel2 = _criar_imovel('Imóvel Indeterminado')
        self.locatario = _criar_pessoa('Locatário')

    def test_contrato_determinado_com_data_fim_proxima_aparece(self):
        contrato = _criar_contrato(
            self.imovel1, self.locatario, date(2020, 1, 1),
            date.today() + timedelta(days=30),
        )
        response = self.client.get(reverse('dashboard'))
        self.assertIn(contrato, list(response.context['contratos_vencendo']))

    def test_contrato_prazo_indeterminado_nao_aparece_vencendo(self):
        contrato = _criar_contrato(
            self.imovel2, self.locatario, date(2020, 1, 1),
            date.today() + timedelta(days=30),
            prazo_indeterminado=True,
        )
        response = self.client.get(reverse('dashboard'))
        self.assertNotIn(contrato, list(response.context['contratos_vencendo']))


# ─── Encargo de aluguel ativo único por contrato ──────────────────────────────

class EncargoAluguelUnicoTest(TestCase):
    def setUp(self):
        self.imovel = _criar_imovel()
        self.locatario = _criar_pessoa('Locatário', tipo='locatario')
        self.contrato = _criar_contrato(
            self.imovel, self.locatario, date(2024, 1, 1), date(2024, 12, 31)
        )

    def test_segundo_encargo_aluguel_ativo_rejeitado_no_clean(self):
        self.contrato.garantir_encargo_aluguel()
        duplicado = EncargoContrato(
            contrato=self.contrato, tipo='aluguel', valor=Decimal('1500.00'),
            periodicidade='mensal', ativo=True,
        )
        with self.assertRaises(ValidationError):
            duplicado.full_clean()

    def test_segundo_encargo_aluguel_ativo_rejeitado_no_banco(self):
        self.contrato.garantir_encargo_aluguel()
        with self.assertRaises(IntegrityError):
            EncargoContrato.objects.create(
                contrato=self.contrato, tipo='aluguel', valor=Decimal('1500.00'),
                periodicidade='mensal', ativo=True,
            )

    def test_encargo_aluguel_inativo_adicional_permitido(self):
        self.contrato.garantir_encargo_aluguel()
        historico = EncargoContrato(
            contrato=self.contrato, tipo='aluguel', valor=Decimal('1800.00'),
            periodicidade='mensal', ativo=False,
        )
        historico.full_clean()  # não deve levantar
        historico.save()
        self.assertEqual(self.contrato.encargos.filter(tipo='aluguel').count(), 2)


# ─── Índices do Banco Central (API SGS mockada) ───────────────────────────────

class IndicesBCBTest(TestCase):
    def setUp(self):
        from django.core.cache import cache
        cache.clear()

    def _resposta_sgs(self, valores):
        import io
        import json
        dados = [
            {'data': f'01/{(i % 12) + 1:02d}/2025', 'valor': str(v)}
            for i, v in enumerate(valores)
        ]
        return io.BytesIO(json.dumps(dados).encode('utf-8'))

    def test_acumulado_12m_calculado_por_juros_compostos(self):
        from unittest.mock import patch
        from patrimonio.indices import variacao_acumulada_12m

        # BytesIO é um context manager que retorna a si mesmo — serve como
        # substituto direto da resposta de urlopen().
        with patch('patrimonio.indices.urlopen', return_value=self._resposta_sgs(['1.0'] * 12)):
            resultado = variacao_acumulada_12m('ipca')

        # (1.01^12 - 1) * 100 ≈ 12.6825%
        self.assertAlmostEqual(float(resultado['percentual']), 12.6825, places=3)

    def test_indice_sem_serie_levanta_erro(self):
        from patrimonio.indices import variacao_acumulada_12m, IndiceIndisponivelError
        with self.assertRaises(IndiceIndisponivelError):
            variacao_acumulada_12m('fixo')

    def test_falha_de_rede_levanta_erro_amigavel(self):
        from unittest.mock import patch
        from urllib.error import URLError
        from patrimonio.indices import variacao_acumulada_12m, IndiceIndisponivelError

        with patch('patrimonio.indices.urlopen', side_effect=URLError('offline')):
            with self.assertRaises(IndiceIndisponivelError):
                variacao_acumulada_12m('igpm')

    def test_admin_action_cria_reajuste_pendente(self):
        from unittest.mock import patch
        from django.contrib import admin as django_admin
        from django.contrib.messages.storage.fallback import FallbackStorage
        from django.test import RequestFactory
        from patrimonio.admin import ContratoAdmin

        imovel = _criar_imovel('Imóvel Reajuste')
        locatario = _criar_pessoa('Locatário R', tipo='locatario')
        contrato = _criar_contrato(
            imovel, locatario, date(2024, 1, 1), date(2030, 12, 31),
            indice_reajuste='ipca', data_proximo_reajuste=date(2026, 1, 1),
        )

        admin_instance = ContratoAdmin(Contrato, django_admin.site)
        request = RequestFactory().post('/admin/patrimonio/contrato/')
        request.user = com_leitura(User.objects.create_user('reajuste_admin', password='pass'))
        request.session = {}
        request._messages = FallbackStorage(request)

        resultado_mock = {'percentual': Decimal('5.0000'), 'inicio': '01/2025', 'fim': '12/2025'}
        with patch('patrimonio.indices.variacao_acumulada_12m', return_value=resultado_mock):
            admin_instance.sugerir_reajuste_indice(request, Contrato.objects.filter(pk=contrato.pk))

        reajuste = ReajusteContrato.objects.get(contrato=contrato)
        self.assertFalse(reajuste.aplicado)
        self.assertEqual(reajuste.percentual_aplicado, Decimal('5.0000'))
        self.assertEqual(reajuste.valor_anterior, Decimal('2000.00'))
        self.assertEqual(reajuste.valor_novo, Decimal('2100.00'))
        self.assertEqual(reajuste.data_reajuste, date(2026, 1, 1))
        # Rodar de novo não duplica: contrato já tem reajuste pendente
        with patch('patrimonio.indices.variacao_acumulada_12m', return_value=resultado_mock):
            admin_instance.sugerir_reajuste_indice(request, Contrato.objects.filter(pk=contrato.pk))
        self.assertEqual(ReajusteContrato.objects.filter(contrato=contrato).count(), 1)


# ─── Indicadores patrimoniais ─────────────────────────────────────────────────

class IndicadoresImovelTest(TestCase):
    def setUp(self):
        self.imovel = _criar_imovel('Imóvel Indicadores', valor_estimado=Decimal('240000.00'))
        self.locatario = _criar_pessoa('Locatário I', tipo='locatario')
        hoje = timezone.localdate()
        self.contrato = _criar_contrato(
            self.imovel, self.locatario,
            hoje.replace(day=1) - timedelta(days=400), hoje + timedelta(days=365),
        )

    def test_indicadores_com_receita_recebida(self):
        from financeiro.models import ReceitaAluguel
        from patrimonio.indicadores import indicadores_do_imovel

        hoje = timezone.localdate()
        ReceitaAluguel.objects.create(
            contrato=self.contrato, imovel=self.imovel,
            competencia_mes=hoje.month, competencia_ano=hoje.year,
            data_vencimento=hoje, valor_previsto=Decimal('2000.00'),
            valor_recebido=Decimal('2000.00'), status='recebido',
        )
        ind = indicadores_do_imovel(self.imovel)
        self.assertEqual(ind['receita_12m'], Decimal('2000.00'))
        self.assertEqual(ind['resultado_12m'], Decimal('2000.00'))
        # 2000 / 240000 * 100 = 0.83%
        self.assertEqual(ind['yield_bruto'], Decimal('0.83'))
        self.assertEqual(ind['meses_ocupados'], 12)
        self.assertEqual(ind['ocupacao'], Decimal('100.00'))

    def test_yield_none_sem_valor_de_referencia(self):
        from patrimonio.indicadores import indicadores_do_imovel
        self.imovel.valor_estimado = None
        self.imovel.save()
        ind = indicadores_do_imovel(self.imovel)
        self.assertIsNone(ind['yield_bruto'])
        self.assertIsNone(ind['yield_liquido'])

    def test_imovel_sem_contrato_tem_ocupacao_zero(self):
        from patrimonio.indicadores import indicadores_do_imovel
        vazio = _criar_imovel('Imóvel Vazio')
        ind = indicadores_do_imovel(vazio)
        self.assertEqual(ind['meses_ocupados'], 0)


# ─── Perfis de acesso (Fase 3) ────────────────────────────────────────────────

class PerfisDeAcessoTest(TestCase):
    """Advogado vê contratos/documentos mas não financeiro; contador vê financeiro."""

    @classmethod
    def setUpTestData(cls):
        from django.core.management import call_command
        call_command('criar_grupos', verbosity=0)

    def _usuario_no_grupo(self, username, grupo):
        from django.contrib.auth.models import Group
        user = User.objects.create_user(username, password='pass')
        user.groups.add(Group.objects.get(name=grupo))
        return user

    def test_advogado_acessa_contratos_e_documentos(self):
        self._usuario_no_grupo('adv', 'advogado')
        client = Client()
        client.login(username='adv', password='pass')

        self.assertEqual(client.get(reverse('contrato_list')).status_code, 200)
        self.assertEqual(client.get(reverse('imovel_list')).status_code, 200)
        self.assertEqual(client.get(reverse('documento_list')).status_code, 200)

    def test_advogado_nao_acessa_financeiro(self):
        self._usuario_no_grupo('adv2', 'advogado')
        client = Client()
        client.login(username='adv2', password='pass')

        self.assertEqual(client.get(reverse('receitas_list')).status_code, 403)
        self.assertEqual(client.get(reverse('despesas_list')).status_code, 403)
        self.assertEqual(client.get(reverse('extrato_list')).status_code, 403)
        # Painéis: advogado TEM patrimonio.view_imovel, então acessa a tela
        # (200) mas só vê o painel de ocupação — nenhum dado financeiro
        # (item 3, rodada pós-revisão: painéis degradam por permissão em vez
        # de negar a tela inteira quando há ao menos uma área permitida).
        resposta = client.get(reverse('paineis'))
        self.assertEqual(resposta.status_code, 200)
        self.assertIsNotNone(resposta.context['ocupacao'])
        self.assertIsNone(resposta.context['inadimplencia'])
        self.assertIsNone(resposta.context['por_categoria'])
        self.assertIsNone(resposta.context['por_imovel'])

    def test_contador_acessa_financeiro_e_exports(self):
        self._usuario_no_grupo('cont', 'contador')
        client = Client()
        client.login(username='cont', password='pass')

        self.assertEqual(client.get(reverse('receitas_list')).status_code, 200)
        self.assertEqual(client.get(reverse('checklist_mensal')).status_code, 200)
        self.assertEqual(client.get(reverse('export_contratos', args=['csv'])).status_code, 200)
        self.assertEqual(client.get(reverse('extrato_list')).status_code, 200)

    def test_socio_ve_tudo_mas_nao_altera(self):
        self._usuario_no_grupo('soc', 'socio')
        client = Client()
        client.login(username='soc', password='pass')

        self.assertEqual(client.get(reverse('receitas_list')).status_code, 200)
        self.assertEqual(client.get(reverse('contrato_list')).status_code, 200)
        self.assertEqual(client.get(reverse('paineis')).status_code, 200)
        # POST de escrita continua bloqueado (perfil só de leitura)
        hoje = timezone.localdate()
        resposta = client.post(reverse('gerar_receitas_mes'), {'mes': hoje.month, 'ano': hoje.year})
        self.assertEqual(resposta.status_code, 403)

    def test_dashboard_do_advogado_esconde_financeiro(self):
        self._usuario_no_grupo('adv3', 'advogado')
        client = Client()
        client.login(username='adv3', password='pass')

        resposta = client.get(reverse('dashboard'))
        self.assertEqual(resposta.status_code, 200)
        html = resposta.content.decode()
        self.assertNotIn('Fluxo de Caixa', html)
        self.assertNotIn('Receita Prevista', html)
        self.assertIn('Contratos vencendo', html)

    def test_menu_do_advogado_esconde_financeiro(self):
        self._usuario_no_grupo('adv4', 'advogado')
        client = Client()
        client.login(username='adv4', password='pass')

        html = client.get(reverse('dashboard')).content.decode()
        self.assertNotIn('Baixa de Aluguéis', html)
        self.assertNotIn('Conciliação Bancária', html)
        self.assertIn('Contratos', html)
        self.assertIn('Documentos', html)

    def test_sem_login_view_protegida_redireciona(self):
        client = Client()
        resposta = client.get(reverse('contrato_list'))
        self.assertEqual(resposta.status_code, 302)
        self.assertIn('/login/', resposta['Location'])


# ─── Validações de domínio dos models (rodada de correções) ───────────────────

class ValidacoesModelsTest(TestCase):
    def setUp(self):
        self.imovel = _criar_imovel()
        self.locatario = _criar_pessoa('Locatário V', tipo='locatario')

    def _contrato_nao_salvo(self, **kwargs):
        defaults = dict(
            imovel=self.imovel, locatario=self.locatario,
            data_inicio=date(2024, 1, 1), data_fim=date(2024, 12, 31),
            valor_aluguel=Decimal('2000.00'), dia_vencimento=10, status='ativo',
        )
        defaults.update(kwargs)
        return Contrato(**defaults)

    def test_data_fim_anterior_ao_inicio_rejeitada(self):
        c = self._contrato_nao_salvo(data_fim=date(2023, 12, 31))
        with self.assertRaises(ValidationError):
            c.full_clean()

    def test_encerramento_real_anterior_ao_inicio_rejeitado(self):
        c = self._contrato_nao_salvo(data_encerramento_real=date(2023, 6, 1))
        with self.assertRaises(ValidationError):
            c.full_clean()

    def test_valor_aluguel_zero_rejeitado(self):
        c = self._contrato_nao_salvo(valor_aluguel=Decimal('0.00'))
        with self.assertRaises(ValidationError):
            c.full_clean()

    def test_comissao_acima_de_100_rejeitada(self):
        c = self._contrato_nao_salvo(comissao_imobiliaria_percentual=Decimal('101.00'))
        with self.assertRaises(ValidationError):
            c.full_clean()

    def test_multa_negativa_rejeitada(self):
        c = self._contrato_nao_salvo(multa_atraso_percentual=Decimal('-1.00'))
        with self.assertRaises(ValidationError):
            c.full_clean()

    def test_encerrado_indeterminado_exige_data_real(self):
        c = self._contrato_nao_salvo(status='encerrado', prazo_indeterminado=True)
        with self.assertRaises(ValidationError) as ctx:
            c.full_clean()
        self.assertIn('data_encerramento_real', ctx.exception.message_dict)

    def test_ativo_com_encerramento_no_passado_rejeitado(self):
        c = self._contrato_nao_salvo(
            data_inicio=date(2020, 1, 1), data_fim=date(2030, 12, 31),
            data_encerramento_real=date(2021, 1, 1),
        )
        with self.assertRaises(ValidationError) as ctx:
            c.full_clean()
        self.assertIn('status', ctx.exception.message_dict)

    def test_encargo_fim_cobranca_antes_do_inicio_rejeitado(self):
        contrato = _criar_contrato(self.imovel, self.locatario, date(2024, 1, 1), date(2024, 12, 31))
        encargo = EncargoContrato(
            contrato=contrato, tipo='iptu', valor=Decimal('100.00'),
            data_inicio_cobranca=date(2024, 6, 1), data_fim_cobranca=date(2024, 5, 1),
        )
        with self.assertRaises(ValidationError):
            encargo.full_clean()

    def test_encargo_aluguel_valor_zero_rejeitado(self):
        contrato = _criar_contrato(self.imovel, self.locatario, date(2024, 1, 1), date(2024, 12, 31))
        encargo = EncargoContrato(contrato=contrato, tipo='aluguel', valor=Decimal('0.00'))
        with self.assertRaises(ValidationError):
            encargo.full_clean()

    def test_imovel_pai_igual_a_si_mesmo_rejeitado(self):
        self.imovel.imovel_pai = self.imovel
        with self.assertRaises(ValidationError):
            self.imovel.full_clean()

    def test_hierarquia_circular_rejeitada(self):
        filho = _criar_imovel('Filho', imovel_pai=self.imovel)
        self.imovel.imovel_pai = filho
        with self.assertRaises(ValidationError):
            self.imovel.full_clean()

    def test_cpf_cnpj_duplicado_rejeitado(self):
        _criar_pessoa('Um').__class__.objects.filter(nome='Um').update(cpf_cnpj='123.456.789-09', cpf_cnpj_normalizado='12345678909')
        duplicada = Pessoa(nome='Dois', cpf_cnpj='12345678909')
        with self.assertRaises(ValidationError):
            duplicada.full_clean()

    def test_cpf_cnpj_normalizado_preenchido_no_save(self):
        p = Pessoa.objects.create(nome='Três', cpf_cnpj='987.654.321-00')
        self.assertEqual(p.cpf_cnpj_normalizado, '98765432100')
        # forma formatada preservada
        self.assertEqual(p.cpf_cnpj, '987.654.321-00')

    def test_cpf_cnpj_vazio_nao_obriga_nem_conflita(self):
        Pessoa.objects.create(nome='Sem Doc 1')
        p2 = Pessoa(nome='Sem Doc 2')
        p2.full_clean()  # não deve levantar


# ─── Reajuste seguro e competência histórica do índice ────────────────────────

class ReajusteSeguroTest(TestCase):
    def setUp(self):
        self.imovel = _criar_imovel('Imóvel Reaj')
        self.locatario = _criar_pessoa('Locatário R2', tipo='locatario')
        self.contrato = _criar_contrato(
            self.imovel, self.locatario, date(2024, 1, 1), date(2030, 12, 31),
            valor_aluguel=Decimal('2000.00'),
        )
        self.contrato.garantir_encargo_aluguel()
        self.reajuste = ReajusteContrato.objects.create(
            contrato=self.contrato, data_reajuste=date(2025, 1, 1), indice='ipca',
            valor_anterior=Decimal('2000.00'), valor_novo=Decimal('2100.00'),
        )

    def test_aplicar_atualiza_contrato_encargo_e_marca_aplicado(self):
        self.assertTrue(self.reajuste.aplicar())
        self.contrato.refresh_from_db()
        self.reajuste.refresh_from_db()
        encargo = self.contrato.encargos.get(tipo='aluguel', ativo=True)
        self.assertEqual(self.contrato.valor_aluguel, Decimal('2100.00'))
        self.assertEqual(encargo.valor, Decimal('2100.00'))
        self.assertEqual(self.contrato.data_proximo_reajuste, date(2026, 1, 1))
        self.assertTrue(self.reajuste.aplicado)
        self.assertIsNotNone(self.reajuste.aplicado_em)

    def test_aplicar_e_idempotente(self):
        self.assertTrue(self.reajuste.aplicar())
        self.contrato.refresh_from_db()
        self.reajuste.refresh_from_db()
        # segunda chamada não reaplica nem altera valores
        self.assertFalse(self.reajuste.aplicar())
        self.contrato.refresh_from_db()
        self.assertEqual(self.contrato.valor_aluguel, Decimal('2100.00'))

    def test_valor_novo_zero_rejeitado(self):
        r = ReajusteContrato(
            contrato=self.contrato, data_reajuste=date(2025, 1, 1), indice='ipca',
            valor_anterior=Decimal('2000.00'), valor_novo=Decimal('0.00'),
        )
        with self.assertRaises(ValidationError):
            r.full_clean()


class IndiceCompetenciaHistoricaTest(TestCase):
    def setUp(self):
        from django.core.cache import cache
        cache.clear()

    def test_competencia_final_do_reajuste_e_o_mes_anterior(self):
        from patrimonio.indices import competencia_final_para_reajuste
        self.assertEqual(competencia_final_para_reajuste(date(2024, 6, 15)), (2024, 5))
        self.assertEqual(competencia_final_para_reajuste(date(2024, 1, 1)), (2023, 12))

    def test_consulta_usa_intervalo_historico_correto(self):
        import io, json
        from unittest.mock import patch
        from patrimonio.indices import variacao_acumulada_12m

        urls = []

        def fake_urlopen(url, timeout=0):
            urls.append(url)
            dados = [
                {'data': f'01/{m:02d}/{2023 if m >= 6 else 2024}', 'valor': '0.5'}
                for m in list(range(6, 13)) + list(range(1, 6))
            ]
            return io.BytesIO(json.dumps(dados).encode('utf-8'))

        with patch('patrimonio.indices.urlopen', side_effect=fake_urlopen):
            resultado = variacao_acumulada_12m('ipca', competencia_final=(2024, 5))

        # URL pede exatamente jun/2023 a mai/2024 — não os "últimos 12 de hoje"
        self.assertIn('dataInicial=01%2F06%2F2023', urls[0])
        self.assertIn('dataFinal=31%2F05%2F2024', urls[0])
        self.assertAlmostEqual(float(resultado['percentual']), 6.1678, places=3)

    def test_serie_incompleta_gera_erro_de_defasagem(self):
        import io, json
        from unittest.mock import patch
        from patrimonio.indices import variacao_acumulada_12m, IndiceIndisponivelError

        poucos = io.BytesIO(json.dumps(
            [{'data': '01/01/2024', 'valor': '0.5'}] * 5
        ).encode('utf-8'))
        with patch('patrimonio.indices.urlopen', return_value=poucos):
            with self.assertRaises(IndiceIndisponivelError) as ctx:
                variacao_acumulada_12m('igpm', competencia_final=(2024, 5))
        self.assertIn('defasagem', str(ctx.exception))


# ─── Permissões customizadas no detalhe do imóvel (item 9) ───────────────────

class ImovelDetailPermissoesTest(TestCase):
    def setUp(self):
        from django.contrib.auth.models import Permission
        self.imovel = _criar_imovel('Imóvel Perm')
        self.locatario = _criar_pessoa('Locatário P', tipo='locatario')
        contrato = _criar_contrato(self.imovel, self.locatario, date(2024, 1, 1), date(2030, 12, 31))
        from financeiro.models import ReceitaAluguel
        ReceitaAluguel.objects.create(
            contrato=contrato, imovel=self.imovel,
            competencia_mes=1, competencia_ano=2025,
            data_vencimento=date(2025, 1, 10), valor_previsto=Decimal('2000.00'), status='previsto',
        )
        # usuário APENAS com view_imovel — sem financeiro, documentos ou contrato
        self.user = User.objects.create_user('so_imovel', password='pass')
        self.user.user_permissions.add(Permission.objects.get(codename='view_imovel'))
        self.client = Client()
        self.client.login(username='so_imovel', password='pass')

    def test_detalhe_carrega_sem_dados_restritos(self):
        resposta = self.client.get(reverse('imovel_detail', args=[self.imovel.pk]))
        self.assertEqual(resposta.status_code, 200)
        # a view não busca (nem exibe) dados de áreas sem permissão
        self.assertEqual(len(resposta.context['receitas']), 0)
        self.assertEqual(len(resposta.context['despesas']), 0)
        self.assertEqual(len(resposta.context['documentos']), 0)
        self.assertIsNone(resposta.context['contrato_ativo'])
        self.assertIsNone(resposta.context['indicadores'])
        html = resposta.content.decode()
        self.assertNotIn('Resultado Financeiro', html)
        self.assertNotIn('R$ 2.000', html)

    def test_sem_view_imovel_403(self):
        User.objects.create_user('nada', password='pass')
        client = Client()
        client.login(username='nada', password='pass')
        self.assertEqual(client.get(reverse('imovel_detail', args=[self.imovel.pk])).status_code, 403)


# ─── Yield locatício (item 13) ────────────────────────────────────────────────

class YieldLocaticioTest(TestCase):
    def test_yield_usa_apenas_itens_de_aluguel(self):
        from financeiro.models import ReceitaAluguel, ReceitaAluguelItem
        from patrimonio.indicadores import indicadores_do_imovel

        imovel = _criar_imovel('Imóvel Yield', valor_estimado=Decimal('240000.00'))
        locatario = _criar_pessoa('Locatário Y', tipo='locatario')
        hoje = timezone.localdate()
        contrato = _criar_contrato(
            imovel, locatario, hoje - timedelta(days=400), hoje + timedelta(days=365),
        )
        receita = ReceitaAluguel.objects.create(
            contrato=contrato, imovel=imovel,
            competencia_mes=hoje.month, competencia_ano=hoje.year,
            data_vencimento=hoje, valor_previsto=Decimal('2500.00'),
            valor_recebido=Decimal('2500.00'), status='recebido',
        )
        ReceitaAluguelItem.objects.create(receita=receita, tipo='aluguel', valor=Decimal('2000.00'))
        ReceitaAluguelItem.objects.create(receita=receita, tipo='iptu', valor=Decimal('500.00'))

        ind = indicadores_do_imovel(imovel)
        self.assertEqual(ind['receita_12m'], Decimal('2500.00'))          # caixa total
        self.assertEqual(ind['receita_locaticia_12m'], Decimal('2000.00'))  # só aluguel
        # 2000 / 240000 * 100 = 0.83 — IPTU repassado não infla o yield
        self.assertEqual(ind['yield_bruto'], Decimal('0.83'))


# ─── Rodada 2: reajuste aplicado é imutável ────────────────────────────────────

class ReajusteProtegidoTest(TestCase):
    def setUp(self):
        self.imovel = _criar_imovel('Imóvel Protegido')
        self.locatario = _criar_pessoa('Locatário P')
        self.contrato = _criar_contrato(
            self.imovel, self.locatario, date(2024, 1, 1), date(2026, 12, 31),
            valor_aluguel=Decimal('2000.00'), indice_reajuste='ipca',
        )
        self.reajuste = ReajusteContrato.objects.create(
            contrato=self.contrato, data_reajuste=date(2025, 1, 1), indice='ipca',
            valor_anterior=Decimal('2000.00'), valor_novo=Decimal('2150.00'), aplicado=False,
        )

    def test_nao_e_possivel_desmarcar_reajuste_aplicado(self):
        self.reajuste.aplicar()
        self.reajuste.aplicado = False
        with self.assertRaises(ValidationError):
            self.reajuste.full_clean()
        # mesmo um save() direto via ORM é REJEITADO (não mais silenciosamente
        # corrigido de volta para aplicado=True) — rodada pós-revisão, item 2.
        with self.assertRaises(ValidationError):
            self.reajuste.save()
        self.reajuste.refresh_from_db()
        self.assertTrue(self.reajuste.aplicado)
        self.assertIsNotNone(self.reajuste.aplicado_em)

    def test_campos_financeiros_imutaveis_apos_aplicacao(self):
        self.reajuste.aplicar()
        self.reajuste.refresh_from_db()
        self.reajuste.valor_novo = Decimal('9999.00')
        with self.assertRaises(ValidationError) as ctx:
            self.reajuste.full_clean()
        self.assertIn('valor_novo', ctx.exception.message_dict)

    def test_observacoes_continuam_editaveis_apos_aplicacao(self):
        self.reajuste.aplicar()
        self.reajuste.refresh_from_db()
        self.reajuste.observacoes = 'nota posterior'
        self.reajuste.full_clean()  # não deve levantar
        self.reajuste.save()
        self.reajuste.refresh_from_db()
        self.assertEqual(self.reajuste.observacoes, 'nota posterior')

    def test_aplicar_usa_valores_do_registro_bloqueado_no_banco(self):
        # altera o objeto EM MEMÓRIA sem salvar — aplicar() deve ignorar e usar o banco
        self.reajuste.valor_novo = Decimal('9999.00')
        aplicou = self.reajuste.aplicar()
        self.assertTrue(aplicou)
        self.contrato.refresh_from_db()
        self.assertEqual(self.contrato.valor_aluguel, Decimal('2150.00'))
        # objeto em memória foi sincronizado com o estado persistido
        self.assertTrue(self.reajuste.aplicado)
        self.assertIsNotNone(self.reajuste.aplicado_em)
        self.assertEqual(self.reajuste.valor_novo, Decimal('2150.00'))

    def test_chamada_repetida_nao_reaplica(self):
        self.assertTrue(self.reajuste.aplicar())
        primeiro_aplicado_em = ReajusteContrato.objects.get(pk=self.reajuste.pk).aplicado_em
        self.contrato.refresh_from_db()
        proximo = self.contrato.data_proximo_reajuste
        self.assertFalse(self.reajuste.aplicar())
        self.contrato.refresh_from_db()
        self.assertEqual(self.contrato.valor_aluguel, Decimal('2150.00'))
        self.assertEqual(self.contrato.data_proximo_reajuste, proximo)
        self.assertEqual(
            ReajusteContrato.objects.get(pk=self.reajuste.pk).aplicado_em, primeiro_aplicado_em
        )

    def test_alteracao_direta_de_valor_novo_via_save_e_bloqueada(self):
        """Item 10: save() direto (sem passar por full_clean()) também protege."""
        self.reajuste.aplicar()
        aplicado = ReajusteContrato.objects.get(pk=self.reajuste.pk)
        aplicado.valor_novo = Decimal('9999.00')
        with self.assertRaises(ValidationError):
            aplicado.save()
        self.assertEqual(
            ReajusteContrato.objects.get(pk=self.reajuste.pk).valor_novo, Decimal('2150.00')
        )

    def test_alteracao_de_contrato_via_save_e_bloqueada(self):
        self.reajuste.aplicar()
        outro_contrato = _criar_contrato(
            self.imovel, self.locatario, date(2024, 1, 1), date(2026, 12, 31),
            valor_aluguel=Decimal('1000.00'),
        )
        aplicado = ReajusteContrato.objects.get(pk=self.reajuste.pk)
        aplicado.contrato = outro_contrato
        with self.assertRaises(ValidationError):
            aplicado.save()

    def test_alteracao_de_data_via_save_e_bloqueada(self):
        self.reajuste.aplicar()
        aplicado = ReajusteContrato.objects.get(pk=self.reajuste.pk)
        aplicado.data_reajuste = date(2025, 6, 1)
        with self.assertRaises(ValidationError):
            aplicado.save()

    def test_delete_de_reajuste_aplicado_e_bloqueado(self):
        self.reajuste.aplicar()
        aplicado = ReajusteContrato.objects.get(pk=self.reajuste.pk)
        with self.assertRaises(ValidationError):
            aplicado.delete()
        self.assertTrue(ReajusteContrato.objects.filter(pk=self.reajuste.pk).exists())

    def test_delete_de_reajuste_pendente_funciona_normalmente(self):
        pendente = ReajusteContrato.objects.create(
            contrato=self.contrato, data_reajuste=date(2025, 6, 1), indice='ipca',
            valor_anterior=Decimal('2000.00'), valor_novo=Decimal('2100.00'), aplicado=False,
        )
        pendente.delete()  # não deve levantar
        self.assertFalse(ReajusteContrato.objects.filter(pk=pendente.pk).exists())

    def test_pendencia_e_definida_por_aplicado_em(self):
        # flag aplicado=True marcada manualmente sem aplicar (dado antigo):
        # continua PENDENTE pela regra oficial (aplicado_em IS NULL)
        avulso = ReajusteContrato.objects.create(
            contrato=self.contrato, data_reajuste=date(2025, 6, 1), indice='ipca',
            valor_anterior=Decimal('2000.00'), valor_novo=Decimal('2100.00'), aplicado=True,
        )
        pendentes = self.contrato.reajustes.filter(aplicado_em__isnull=True)
        self.assertIn(avulso, pendentes)
        self.assertIn(self.reajuste, pendentes)

    # ─── Rodada pós-revisão (item 2): robustez contra bypass de aplicado_em ──

    def test_apagar_aplicado_em_via_save_e_rejeitado(self):
        self.reajuste.aplicar()
        aplicado = ReajusteContrato.objects.get(pk=self.reajuste.pk)
        aplicado.aplicado_em = None
        with self.assertRaises(ValidationError):
            aplicado.save()
        persistido = ReajusteContrato.objects.get(pk=self.reajuste.pk)
        self.assertIsNotNone(persistido.aplicado_em)
        self.assertTrue(persistido.aplicado)

    def test_alterar_aplicado_em_para_outra_data_e_rejeitado(self):
        self.reajuste.aplicar()
        aplicado = ReajusteContrato.objects.get(pk=self.reajuste.pk)
        original = aplicado.aplicado_em
        aplicado.aplicado_em = original - timedelta(days=1)
        with self.assertRaises(ValidationError):
            aplicado.save()
        persistido = ReajusteContrato.objects.get(pk=self.reajuste.pk)
        self.assertEqual(persistido.aplicado_em, original)

    def test_instancia_desatualizada_nao_consegue_excluir_registro_aplicado_por_outra(self):
        """
        Duas instâncias Python apontam para o MESMO reajuste, ainda pendente.
        Uma delas (`aplicado_via_outra`) aplica o reajuste — a OUTRA
        (`desatualizada`) continua com aplicado_em=None em memória, mas
        delete() deve consultar o banco e recusar mesmo assim.
        """
        desatualizada = ReajusteContrato.objects.get(pk=self.reajuste.pk)
        aplicado_via_outra = ReajusteContrato.objects.get(pk=self.reajuste.pk)
        aplicado_via_outra.aplicar()

        self.assertIsNone(desatualizada.aplicado_em)  # em memória, ainda "vazio"
        with self.assertRaises(ValidationError):
            desatualizada.delete()
        self.assertTrue(ReajusteContrato.objects.filter(pk=self.reajuste.pk).exists())

    def test_queryset_update_em_massa_e_bloqueado_quando_inclui_aplicado(self):
        self.reajuste.aplicar()
        with self.assertRaises(ValidationError):
            ReajusteContrato.objects.filter(contrato=self.contrato).update(observacoes='alterado em massa')
        self.reajuste.refresh_from_db()
        self.assertNotEqual(self.reajuste.observacoes, 'alterado em massa')

    def test_queryset_delete_em_massa_e_bloqueado_quando_inclui_aplicado(self):
        self.reajuste.aplicar()
        with self.assertRaises(ValidationError):
            ReajusteContrato.objects.filter(contrato=self.contrato).delete()
        self.assertTrue(ReajusteContrato.objects.filter(pk=self.reajuste.pk).exists())

    def test_queryset_update_em_massa_funciona_normalmente_para_pendentes(self):
        pendente = ReajusteContrato.objects.create(
            contrato=self.contrato, data_reajuste=date(2025, 6, 1), indice='ipca',
            valor_anterior=Decimal('2000.00'), valor_novo=Decimal('2100.00'), aplicado=False,
        )
        ReajusteContrato.objects.filter(pk=pendente.pk).update(observacoes='nota em lote')
        pendente.refresh_from_db()
        self.assertEqual(pendente.observacoes, 'nota em lote')


# ─── Rodada 2: unicidade de CPF/CNPJ no banco ─────────────────────────────────

class CpfCnpjUnicidadeTest(TestCase):
    def test_duplicado_via_objects_create_bloqueado_pelo_banco(self):
        from django.db import transaction
        Pessoa.objects.create(nome='Titular', cpf_cnpj='123.456.789-09')
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                # objects.create não passa por clean() — a constraint pega
                Pessoa.objects.create(nome='Cópia', cpf_cnpj='12345678909')

    def test_clean_detecta_cpf_com_formatacao_diferente(self):
        Pessoa.objects.create(nome='Titular', cpf_cnpj='123.456.789-09')
        duplicada = Pessoa(nome='Cópia', cpf_cnpj='12345678909')
        with self.assertRaises(ValidationError):
            duplicada.full_clean()

    def test_clean_detecta_cnpj_com_formatacao_diferente(self):
        Pessoa.objects.create(nome='Empresa', cpf_cnpj='12.345.678/0001-95')
        duplicada = Pessoa(nome='Empresa 2', cpf_cnpj='12345678000195')
        with self.assertRaises(ValidationError):
            duplicada.full_clean()

    def test_cpf_vazio_repetido_permitido(self):
        Pessoa.objects.create(nome='Sem Doc 1')
        Pessoa.objects.create(nome='Sem Doc 2')
        self.assertEqual(Pessoa.objects.filter(cpf_cnpj_normalizado='').count(), 2)

    def test_atualizacao_da_propria_pessoa_sem_falso_positivo(self):
        pessoa = Pessoa.objects.create(nome='Titular', cpf_cnpj='123.456.789-09')
        pessoa.nome = 'Titular Renomeado'
        pessoa.full_clean()  # não deve levantar
        pessoa.save()


class CpfCnpjMigrationCheckTest(TransactionTestCase):
    """
    Testa a checagem de duplicidade da migration 0009 num banco "antigo"
    (sem a constraint). Usa TransactionTestCase porque o schema editor do
    SQLite não pode rodar dentro da transação do TestCase comum.
    """

    def test_migration_interrompe_com_duplicidade_preexistente(self):
        import importlib
        from django.apps import apps as django_apps
        from django.db import connection
        _0009 = importlib.import_module(
            'patrimonio.migrations.0009_pessoa_pessoa_cpf_cnpj_normalizado_unico_quando_preenchido'
        )
        constraint = Pessoa._meta.constraints[0]
        # remove temporariamente a constraint para simular um banco antigo
        with connection.schema_editor() as editor:
            editor.remove_constraint(Pessoa, constraint)
        try:
            Pessoa.objects.create(nome='Dup A', cpf_cnpj='123.456.789-09')
            Pessoa.objects.create(nome='Dup B', cpf_cnpj='12345678909')
            with self.assertRaises(RuntimeError) as ctx:
                _0009.verificar_duplicidades(django_apps, None)
            self.assertIn('Dup A', str(ctx.exception))
            self.assertIn('Dup B', str(ctx.exception))
            # sem duplicidade, a verificação passa
            Pessoa.objects.filter(nome='Dup B').delete()
            _0009.verificar_duplicidades(django_apps, None)
        finally:
            Pessoa.objects.filter(nome__in=['Dup A', 'Dup B']).delete()
            with connection.schema_editor() as editor:
                editor.add_constraint(Pessoa, constraint)


# ─── Rodada 2: combinações de permissão no detalhe do imóvel ──────────────────

class ImovelDetailPermCombinacoesTest(TestCase):
    def setUp(self):
        from django.contrib.auth.models import Permission
        self.Permission = Permission
        self.client = Client()
        self.imovel = _criar_imovel('Imóvel Perm')
        Manutencao.objects.create(
            imovel=self.imovel, descricao='Troca de telhado',
            data_solicitacao=date(2026, 1, 5),
        )

    def _usuario(self, nome, *perms):
        user = User.objects.create_user(nome, password='pass')
        codenames = ('view_imovel',) + perms
        user.user_permissions.add(*self.Permission.objects.filter(codename__in=codenames))
        self.client.login(username=nome, password='pass')
        return user

    def _get(self):
        return self.client.get(reverse('imovel_detail', args=[self.imovel.pk]))

    def test_apenas_imovel_sem_cards_financeiros_e_sem_manutencao(self):
        self._usuario('perm_so_imovel')
        resposta = self._get()
        self.assertEqual(resposta.status_code, 200)
        self.assertIsNone(resposta.context['total_recebido'])
        self.assertIsNone(resposta.context['total_despesas'])
        self.assertIsNone(resposta.context['resultado'])
        self.assertIsNone(resposta.context['indicadores'])
        conteudo = resposta.content.decode()
        self.assertNotIn('Resultado Financeiro', conteudo)
        self.assertNotIn('manutencoes-tab', conteudo)
        self.assertNotIn('Troca de telhado', conteudo)

    def test_somente_receitas_mostra_recebido_sem_liquido(self):
        self._usuario('perm_receitas', 'view_receitaaluguel')
        resposta = self._get()
        conteudo = resposta.content.decode()
        self.assertIsNotNone(resposta.context['total_recebido'])
        self.assertIsNone(resposta.context['resultado'])
        self.assertIn('Total Recebido (histórico)', conteudo)
        self.assertNotIn('Resultado Financeiro', conteudo)
        self.assertNotIn('Yield líquido', conteudo)
        # indicadores não consultaram despesas
        self.assertIsNone(resposta.context['indicadores']['despesa_12m'])
        self.assertIsNone(resposta.context['indicadores']['yield_liquido'])

    def test_somente_despesas_mostra_total_despesas(self):
        self._usuario('perm_despesas', 'view_despesa')
        resposta = self._get()
        conteudo = resposta.content.decode()
        self.assertIsNone(resposta.context['total_recebido'])
        self.assertIsNotNone(resposta.context['total_despesas'])
        self.assertIsNone(resposta.context['resultado'])
        self.assertIn('Total de Despesas Pagas', conteudo)
        self.assertNotIn('Resultado Financeiro', conteudo)

    def test_receitas_e_despesas_mostra_resultado_completo(self):
        self._usuario('perm_completo', 'view_receitaaluguel', 'view_despesa')
        resposta = self._get()
        conteudo = resposta.content.decode()
        self.assertIsNotNone(resposta.context['resultado'])
        self.assertIn('Resultado Financeiro', conteudo)
        self.assertIn('Yield líquido', conteudo)

    # ─── Item 13: aba inicial do detalhe (primeira que o usuário pode ver) ────

    def test_aba_inicial_e_receitas_quando_permitido(self):
        self._usuario('aba_receitas', 'view_receitaaluguel', 'view_despesa')
        resposta = self._get()
        self.assertEqual(resposta.context['aba_inicial'], 'receitas')
        conteudo = resposta.content.decode()
        # a aba de receitas contém "show active"; despesas não
        self.assertIn('tab-pane fade show active" id="receitas-tab"', conteudo)
        self.assertNotIn('tab-pane fade show active" id="despesas-tab"', conteudo)

    def test_aba_inicial_e_despesas_quando_sem_receitas(self):
        self._usuario('aba_despesas', 'view_despesa')
        resposta = self._get()
        self.assertEqual(resposta.context['aba_inicial'], 'despesas')
        conteudo = resposta.content.decode()
        self.assertIn('tab-pane fade show active" id="despesas-tab"', conteudo)

    def test_aba_inicial_e_documentos_quando_sem_receitas_e_despesas(self):
        self._usuario('aba_docs', 'view_documento')
        resposta = self._get()
        self.assertEqual(resposta.context['aba_inicial'], 'documentos')
        conteudo = resposta.content.decode()
        self.assertIn('tab-pane fade show active" id="documentos-tab"', conteudo)

    def test_aba_inicial_e_unidades_quando_sem_nenhuma_area(self):
        self._usuario('aba_unidades')
        resposta = self._get()
        self.assertEqual(resposta.context['aba_inicial'], 'unidades')
        conteudo = resposta.content.decode()
        self.assertIn('tab-pane fade show active" id="unidades-tab"', conteudo)

    # ─── Item 13: botão "Doc. Obrigatório" respeita a permissão certa ─────────

    def test_botao_doc_obrigatorio_oculto_sem_permissao_especifica(self):
        user = self._usuario('sem_perm_obrig', 'add_documento')
        user.is_staff = True
        user.save()
        conteudo = self._get().content.decode()
        self.assertNotIn('Doc. Obrigatório', conteudo)

    def test_botao_doc_obrigatorio_oculto_sem_is_staff(self):
        user = self._usuario('sem_staff_obrig', 'add_documentoobrigatorio')
        user.is_staff = False
        user.save()
        conteudo = self._get().content.decode()
        self.assertNotIn('Doc. Obrigatório', conteudo)

    def test_botao_doc_obrigatorio_visivel_com_permissao_e_staff(self):
        user = self._usuario('com_perm_obrig', 'add_documentoobrigatorio')
        user.is_staff = True
        user.save()
        conteudo = self._get().content.decode()
        self.assertIn('Doc. Obrigatório', conteudo)

    def test_com_permissao_de_manutencao_aba_aparece(self):
        self._usuario('perm_manut', 'view_manutencao')
        resposta = self._get()
        conteudo = resposta.content.decode()
        self.assertIn('manutencoes-tab', conteudo)
        self.assertIn('Troca de telhado', conteudo)


# ─── Rodada 3 (item 6): permissões defensivas no dashboard ────────────────────

class DashboardPermissoesTest(TestCase):
    def setUp(self):
        from django.contrib.auth.models import Permission
        self.Permission = Permission
        self.client = Client()
        self.imovel = _criar_imovel('Imóvel Dash Perm')
        self.locatario = _criar_pessoa('Locatário Dash')
        hoje = timezone.localdate()
        self.contrato = _criar_contrato(
            self.imovel, self.locatario, hoje - timedelta(days=10), hoje + timedelta(days=30),
        )
        Manutencao.objects.create(
            imovel=self.imovel, descricao='Vazamento', data_solicitacao=hoje,
            status='solicitada',
        )

    def _usuario(self, nome, *perms):
        user = User.objects.create_user(nome, password='pass')
        if perms:
            user.user_permissions.add(*self.Permission.objects.filter(codename__in=perms))
        self.client.login(username=nome, password='pass')
        return user

    def _get(self):
        return self.client.get(reverse('dashboard'))

    def test_sem_permissoes_nenhum_dado_e_consultado(self):
        self._usuario('dash_sem_perm')
        resposta = self._get()
        self.assertEqual(resposta.status_code, 200)
        conteudo = resposta.content.decode()
        self.assertNotIn('Total de Imóveis', conteudo)
        self.assertNotIn('Receita Prevista', conteudo)
        self.assertNotIn('Despesas em Aberto', conteudo)
        self.assertNotIn('Contratos vencendo', conteudo)
        self.assertNotIn('Manutenções em aberto', conteudo)
        self.assertEqual(resposta.context['imoveis_total'], 0)
        self.assertEqual(resposta.context['receitas_previstas'], 0)
        self.assertEqual(list(resposta.context['contratos_vencendo']), [])
        self.assertEqual(list(resposta.context['manutencoes_abertas']), [])

    def test_apenas_view_imovel_mostra_so_o_card_de_imoveis(self):
        self._usuario('dash_so_imovel', 'view_imovel')
        resposta = self._get()
        conteudo = resposta.content.decode()
        self.assertIn('Total de Imóveis', conteudo)
        self.assertNotIn('Receita Prevista', conteudo)
        self.assertNotIn('Contratos vencendo', conteudo)
        self.assertEqual(resposta.context['imoveis_total'], 1)

    def test_apenas_receitas_mostra_financeiro_de_receitas(self):
        self._usuario('dash_so_receitas', 'view_receitaaluguel')
        resposta = self._get()
        conteudo = resposta.content.decode()
        self.assertIn('Receita Prevista', conteudo)
        self.assertNotIn('Despesas em Aberto', conteudo)
        self.assertNotIn('Total de Imóveis', conteudo)
        self.assertEqual(resposta.context['despesas_abertas'], 0)

    def test_apenas_receitas_fluxo_de_caixa_esconde_pago_e_resultado(self):
        """Item 9: sem view_despesa, o fluxo de caixa não mostra 'Pago'/'Resultado' como zero."""
        self._usuario('dash_fluxo_so_rec', 'view_receitaaluguel')
        resposta = self._get()
        self.assertFalse(resposta.context['fluxo_inclui_despesas'])
        for ponto in resposta.context['fluxo_caixa']:
            self.assertIsNone(ponto['pago'])
            self.assertIsNone(ponto['saldo'])
        conteudo = resposta.content.decode()
        self.assertIn('Entradas Recebidas', conteudo)
        self.assertNotIn('Fluxo de Caixa — últimos 12 meses', conteudo)
        self.assertNotIn('<th class="text-end">Pago (R$)</th>', conteudo)
        self.assertNotIn('<th class="text-end">Resultado (R$)</th>', conteudo)
        self.assertIn('FLUXO_INCLUI_DESPESAS = false', conteudo)

    def test_receitas_e_despesas_fluxo_de_caixa_mostra_tudo(self):
        self._usuario('dash_fluxo_completo', 'view_receitaaluguel', 'view_despesa')
        resposta = self._get()
        self.assertTrue(resposta.context['fluxo_inclui_despesas'])
        for ponto in resposta.context['fluxo_caixa']:
            self.assertIsNotNone(ponto['pago'])
            self.assertIsNotNone(ponto['saldo'])
        conteudo = resposta.content.decode()
        self.assertIn('Fluxo de Caixa — últimos 12 meses', conteudo)
        self.assertIn('<th class="text-end">Pago (R$)</th>', conteudo)
        self.assertIn('<th class="text-end">Resultado (R$)</th>', conteudo)
        self.assertIn('FLUXO_INCLUI_DESPESAS = true', conteudo)

    def test_apenas_despesas_mostra_card_de_despesas(self):
        from financeiro.models import Despesa
        Despesa.objects.create(
            imovel=self.imovel, descricao='Água', data_vencimento=timezone.localdate(),
            valor=Decimal('80.00'), status='prevista',
        )
        self._usuario('dash_so_despesas', 'view_despesa')
        resposta = self._get()
        conteudo = resposta.content.decode()
        self.assertIn('Despesas em Aberto', conteudo)
        self.assertNotIn('Receita Prevista', conteudo)
        self.assertEqual(resposta.context['despesas_abertas'], 1)
        self.assertEqual(resposta.context['receitas_previstas'], 0)

    def test_apenas_contratos_mostra_contratos_vencendo(self):
        self._usuario('dash_so_contratos', 'view_contrato')
        resposta = self._get()
        conteudo = resposta.content.decode()
        self.assertIn('Contratos vencendo', conteudo)
        self.assertIn(self.contrato, list(resposta.context['contratos_vencendo']))

    def test_apenas_documentos_mostra_bloco_de_documentos(self):
        from documentos.models import Documento
        Documento.objects.create(
            titulo='Contrato assinado', tipo='contrato_aluguel', imovel=self.imovel,
            arquivo=SimpleUploadedFile('c.pdf', b'x', content_type='application/pdf'),
            data_validade=timezone.localdate() - timedelta(days=1),
        )
        self._usuario('dash_so_docs', 'view_documento')
        resposta = self._get()
        conteudo = resposta.content.decode()
        self.assertIn('Documentos vencidos', conteudo)
        self.assertEqual(resposta.context['docs_vencendo_cnt'], 1)

    def test_acesso_financeiro_completo_mostra_receitas_e_despesas(self):
        self._usuario('dash_financeiro', 'view_receitaaluguel', 'view_despesa')
        resposta = self._get()
        conteudo = resposta.content.decode()
        self.assertIn('Receita Prevista', conteudo)
        self.assertIn('Despesas em Aberto', conteudo)

    def test_apenas_manutencao_mostra_bloco_de_manutencoes(self):
        self._usuario('dash_so_manut', 'view_manutencao')
        resposta = self._get()
        conteudo = resposta.content.decode()
        self.assertIn('Manutenções em aberto', conteudo)
        self.assertIn('Vazamento', conteudo)


class AcoesRapidasPermissaoTest(TestCase):
    """
    Item 11 (rodada pós-revisão): botão de consulta -> view; criação -> add;
    edição -> change; link para o Admin -> também exige user.is_staff. Cobre
    o exemplo citado explicitamente ("Gerar Receitas do Mês Atual" não pode
    depender só de view_receitaaluguel) e o mesmo padrão nas demais listagens
    que expõem links diretos para o Django Admin.
    """
    def setUp(self):
        from django.contrib.auth.models import Permission
        self.Permission = Permission
        self.client = Client()
        self.imovel = _criar_imovel('Imóvel Ações Rápidas')

    def _usuario(self, nome, *perms, is_staff=False):
        user = User.objects.create_user(nome, password='pass', is_staff=is_staff)
        if perms:
            user.user_permissions.add(*self.Permission.objects.filter(codename__in=perms))
        self.client.login(username=nome, password='pass')
        return user

    def test_dashboard_so_view_receitaaluguel_nao_mostra_gerar_receitas(self):
        self._usuario('acoes_so_view_receita', 'view_receitaaluguel')
        resposta = self.client.get(reverse('dashboard'))
        conteudo = resposta.content.decode()
        self.assertIn('Conferir Recebimentos', conteudo)
        self.assertNotIn('Gerar Receitas do Mês Atual', conteudo)

    def test_dashboard_com_add_receitaaluguel_mostra_gerar_receitas(self):
        self._usuario('acoes_com_add_receita', 'view_receitaaluguel', 'add_receitaaluguel')
        resposta = self.client.get(reverse('dashboard'))
        conteudo = resposta.content.decode()
        self.assertIn('Gerar Receitas do Mês Atual', conteudo)

    def test_dashboard_links_de_admin_exigem_is_staff(self):
        # add_imovel/add_receitaaluguel/add_despesa/add_documento sem is_staff:
        # nenhum link para o Admin deve aparecer nas Ações Rápidas.
        from django.contrib.auth.models import Permission
        user = User.objects.create_user('acoes_sem_staff', password='pass', is_staff=False)
        user.user_permissions.add(*Permission.objects.filter(
            codename__in=('add_imovel', 'add_receitaaluguel', 'add_despesa', 'add_documento')
        ))
        self.client.login(username='acoes_sem_staff', password='pass')
        resposta = self.client.get(reverse('dashboard'))
        conteudo = resposta.content.decode()
        self.assertNotIn('Cadastrar Imóvel', conteudo)
        self.assertNotIn('Lançar Receita Manualmente', conteudo)
        self.assertNotIn('Lançar Despesa', conteudo)
        self.assertNotIn('Enviar Documento', conteudo)

    def test_nav_gerar_receitas_exige_add_receitaaluguel(self):
        self._usuario('nav_so_view_receita', 'view_receitaaluguel')
        resposta = self.client.get(reverse('dashboard'))
        conteudo = resposta.content.decode()
        self.assertNotIn(reverse('gerar_receitas_mes'), conteudo)

        self._usuario('nav_com_add_receita', 'view_receitaaluguel', 'add_receitaaluguel')
        resposta = self.client.get(reverse('dashboard'))
        conteudo = resposta.content.decode()
        self.assertIn(reverse('gerar_receitas_mes'), conteudo)

    def test_receitas_list_nova_receita_exige_staff_e_add(self):
        user = self._usuario('rec_list_view_only', 'view_receitaaluguel')
        resposta = self.client.get(reverse('receitas_list'))
        conteudo = resposta.content.decode()
        self.assertNotIn(reverse('admin:financeiro_receitaaluguel_add'), conteudo)

        user.is_staff = True
        user.save()
        user.user_permissions.add(
            self.Permission.objects.get(codename='add_receitaaluguel')
        )
        resposta = self.client.get(reverse('receitas_list'))
        conteudo = resposta.content.decode()
        self.assertIn(reverse('admin:financeiro_receitaaluguel_add'), conteudo)

    def test_despesas_list_nova_despesa_exige_staff_e_add(self):
        user = self._usuario('desp_list_view_only', 'view_despesa')
        resposta = self.client.get(reverse('despesas_list'))
        conteudo = resposta.content.decode()
        self.assertNotIn(reverse('admin:financeiro_despesa_add'), conteudo)

        user.is_staff = True
        user.save()
        user.user_permissions.add(self.Permission.objects.get(codename='add_despesa'))
        resposta = self.client.get(reverse('despesas_list'))
        conteudo = resposta.content.decode()
        self.assertIn(reverse('admin:financeiro_despesa_add'), conteudo)

    def test_imovel_list_novo_imovel_exige_staff_e_add(self):
        user = self._usuario('imv_list_view_only', 'view_imovel')
        resposta = self.client.get(reverse('imovel_list'))
        conteudo = resposta.content.decode()
        self.assertNotIn(reverse('admin:patrimonio_imovel_add'), conteudo)

        user.is_staff = True
        user.save()
        user.user_permissions.add(self.Permission.objects.get(codename='add_imovel'))
        resposta = self.client.get(reverse('imovel_list'))
        conteudo = resposta.content.decode()
        self.assertIn(reverse('admin:patrimonio_imovel_add'), conteudo)

    def test_pessoa_list_nova_pessoa_exige_staff_e_add(self):
        user = self._usuario('pes_list_view_only', 'view_pessoa')
        resposta = self.client.get(reverse('pessoa_list'))
        conteudo = resposta.content.decode()
        self.assertNotIn(reverse('admin:patrimonio_pessoa_add'), conteudo)

        user.is_staff = True
        user.save()
        user.user_permissions.add(self.Permission.objects.get(codename='add_pessoa'))
        resposta = self.client.get(reverse('pessoa_list'))
        conteudo = resposta.content.decode()
        self.assertIn(reverse('admin:patrimonio_pessoa_add'), conteudo)

    def test_contrato_list_novo_contrato_exige_staff_e_add(self):
        user = self._usuario('ctr_list_view_only', 'view_contrato')
        resposta = self.client.get(reverse('contrato_list'))
        conteudo = resposta.content.decode()
        self.assertNotIn(reverse('admin:patrimonio_contrato_add'), conteudo)

        user.is_staff = True
        user.save()
        user.user_permissions.add(self.Permission.objects.get(codename='add_contrato'))
        resposta = self.client.get(reverse('contrato_list'))
        conteudo = resposta.content.decode()
        self.assertIn(reverse('admin:patrimonio_contrato_add'), conteudo)
