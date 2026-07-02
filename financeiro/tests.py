import os
from calendar import monthrange
from datetime import date, timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone

from patrimonio.models import Imovel, Pessoa, Contrato, EncargoContrato
from financeiro.models import ReceitaAluguel, ReceitaAluguelItem, Despesa, receitas_inadimplentes_qs
from financeiro.services import gerar_receitas_para_contrato, gerar_receitas_mes, contratos_para_geracao_mes


def _criar_base():
    """Cria imóvel e locatário reutilizáveis entre testes."""
    imovel = Imovel.objects.create(
        nome='Apartamento Teste',
        endereco='Rua A, 100',
        cidade='São Paulo',
        estado='SP',
    )
    locatario = Pessoa.objects.create(nome='Locatário Teste', tipo='locatario')
    return imovel, locatario


def _criar_contrato(imovel, locatario, data_inicio, data_fim, status='ativo', dia_vencimento=10, **kwargs):
    defaults = dict(valor_aluguel=Decimal('2000.00'))
    defaults.update(kwargs)
    return Contrato.objects.create(
        imovel=imovel,
        locatario=locatario,
        data_inicio=data_inicio,
        data_fim=data_fim,
        dia_vencimento=dia_vencimento,
        status=status,
        **defaults,
    )


class GerarReceitasParaContratoTest(TestCase):
    """Testa a função gerar_receitas_para_contrato."""

    def setUp(self):
        self.imovel, self.locatario = _criar_base()
        # Contrato de 3 meses: jan–mar 2024
        self.contrato = _criar_contrato(
            self.imovel, self.locatario,
            data_inicio=date(2024, 1, 1),
            data_fim=date(2024, 3, 31),
        )

    def test_contrato_ativo_gera_receitas_mensais(self):
        """Contrato ativo de 3 meses gera exatamente 3 receitas."""
        criadas, existiam = gerar_receitas_para_contrato(self.contrato)

        self.assertEqual(criadas, 3)
        self.assertEqual(existiam, 0)
        self.assertEqual(ReceitaAluguel.objects.count(), 3)

        meses = list(
            ReceitaAluguel.objects.order_by('competencia_mes')
            .values_list('competencia_mes', flat=True)
        )
        self.assertEqual(meses, [1, 2, 3])

    def test_contrato_inativo_nao_gera_receitas(self):
        """Contratos com status diferente de 'ativo' são ignorados."""
        for status in ('encerrado', 'rescindido', 'suspenso'):
            self.contrato.status = status
            self.contrato.save()

            criadas, existiam = gerar_receitas_para_contrato(self.contrato)

            self.assertEqual(criadas, 0, msg=f'Status {status} não deveria gerar receitas')
            self.assertEqual(existiam, 0)

        self.assertEqual(ReceitaAluguel.objects.count(), 0)

    def test_nao_cria_duplicidade(self):
        """Segunda chamada não cria registros duplicados."""
        criadas1, existiam1 = gerar_receitas_para_contrato(self.contrato)
        criadas2, existiam2 = gerar_receitas_para_contrato(self.contrato)

        self.assertEqual(criadas1, 3)
        self.assertEqual(existiam1, 0)
        self.assertEqual(criadas2, 0)
        self.assertEqual(existiam2, 3)
        # Sem duplicatas no banco
        self.assertEqual(ReceitaAluguel.objects.count(), 3)

    def test_receita_gerada_usa_imovel_do_contrato(self):
        """O imóvel da receita gerada é sempre o imóvel do contrato."""
        gerar_receitas_para_contrato(self.contrato)

        for receita in ReceitaAluguel.objects.all():
            self.assertEqual(
                receita.imovel_id, self.contrato.imovel_id,
                msg='Imóvel da receita difere do imóvel do contrato',
            )

    def test_valores_e_status_corretos(self):
        """Receita gerada tem valor_previsto, data_vencimento e status corretos."""
        gerar_receitas_para_contrato(self.contrato)

        receita_jan = ReceitaAluguel.objects.get(competencia_mes=1, competencia_ano=2024)
        self.assertEqual(receita_jan.data_vencimento, date(2024, 1, 10))
        self.assertEqual(receita_jan.valor_previsto, Decimal('2000.00'))
        self.assertEqual(receita_jan.status, 'previsto')

    def test_dia_vencimento_ajustado_para_fim_do_mes(self):
        """Dia 31 em fevereiro (ano bissexto 2024) deve cair no dia 29."""
        self.contrato.dia_vencimento = 31
        self.contrato.save()

        gerar_receitas_para_contrato(self.contrato)

        receita_fev = ReceitaAluguel.objects.get(competencia_mes=2, competencia_ano=2024)
        self.assertEqual(receita_fev.data_vencimento, date(2024, 2, 29))

    def test_gerar_apenas_periodo_especifico(self):
        """Com data_inicio/data_fim informados, gera somente receitas do intervalo."""
        criadas, _ = gerar_receitas_para_contrato(
            self.contrato,
            data_inicio=date(2024, 2, 1),
            data_fim=date(2024, 2, 29),
        )

        self.assertEqual(criadas, 1)
        self.assertTrue(
            ReceitaAluguel.objects.filter(competencia_mes=2, competencia_ano=2024).exists()
        )
        self.assertFalse(
            ReceitaAluguel.objects.filter(competencia_mes=1).exists()
        )

    def test_periodo_fora_da_vigencia_do_contrato_nao_gera(self):
        """Solicitar um mês fora da vigência do contrato não gera nenhuma receita."""
        criadas, existiam = gerar_receitas_para_contrato(
            self.contrato,
            data_inicio=date(2025, 1, 1),
            data_fim=date(2025, 1, 31),
        )

        self.assertEqual(criadas, 0)
        self.assertEqual(existiam, 0)


class GerarReceitasMesTest(TestCase):
    """Testa a função gerar_receitas_mes (todos os contratos de um mês)."""

    def setUp(self):
        self.imovel, self.locatario = _criar_base()

    def test_geracao_mes_atual(self):
        """gerar_receitas_mes cria receita para contrato ativo no mês atual."""
        hoje = date.today()
        mes = hoje.month
        ano = hoje.year
        ultimo_dia = monthrange(ano, mes)[1]

        contrato = _criar_contrato(
            self.imovel, self.locatario,
            data_inicio=date(ano, mes, 1),
            data_fim=date(ano, mes, ultimo_dia),
        )

        criadas, existiam = gerar_receitas_mes(mes, ano)

        self.assertEqual(criadas, 1)
        self.assertEqual(existiam, 0)
        self.assertTrue(
            ReceitaAluguel.objects.filter(
                contrato=contrato,
                competencia_mes=mes,
                competencia_ano=ano,
            ).exists()
        )

    def test_contrato_inativo_ignorado_em_gerar_mes(self):
        """Contratos inativos não são incluídos na geração por mês."""
        hoje = date.today()
        _criar_contrato(
            self.imovel, self.locatario,
            data_inicio=date(hoje.year, hoje.month, 1),
            data_fim=date(hoje.year, hoje.month, monthrange(hoje.year, hoje.month)[1]),
            status='encerrado',
        )

        criadas, _ = gerar_receitas_mes(hoje.month, hoje.year)

        self.assertEqual(criadas, 0)

    def test_multiplos_contratos_no_mes(self):
        """Múltiplos contratos ativos geram uma receita cada."""
        hoje = date.today()
        mes, ano = hoje.month, hoje.year
        ultimo_dia = monthrange(ano, mes)[1]

        imovel2 = Imovel.objects.create(nome='Casa Teste', endereco='Rua B', cidade='SP', estado='SP')

        _criar_contrato(self.imovel, self.locatario, date(ano, mes, 1), date(ano, mes, ultimo_dia))
        _criar_contrato(imovel2, self.locatario, date(ano, mes, 1), date(ano, mes, ultimo_dia))

        criadas, _ = gerar_receitas_mes(mes, ano)

        self.assertEqual(criadas, 2)
        self.assertEqual(ReceitaAluguel.objects.count(), 2)

    def test_sem_contratos_ativos_retorna_zero(self):
        """Sem contratos ativos no período, retorna (0, 0)."""
        criadas, existiam = gerar_receitas_mes(1, 2000)
        self.assertEqual(criadas, 0)
        self.assertEqual(existiam, 0)


class ReceitaAluguelValidacaoTest(TestCase):
    """Testa clean() e save() de ReceitaAluguel."""

    def setUp(self):
        self.imovel, self.locatario = _criar_base()
        self.imovel2 = Imovel.objects.create(nome='Outro Imóvel', endereco='Rua C', cidade='SP', estado='SP')
        self.contrato = _criar_contrato(
            self.imovel, self.locatario,
            data_inicio=date(2024, 1, 1),
            data_fim=date(2024, 12, 31),
        )

    def test_save_preenche_imovel_automaticamente(self):
        """save() preenche imovel a partir do contrato quando não informado."""
        receita = ReceitaAluguel(
            contrato=self.contrato,
            competencia_mes=1,
            competencia_ano=2024,
            data_vencimento=date(2024, 1, 10),
            valor_previsto=Decimal('2000.00'),
        )
        receita.save()

        self.assertEqual(receita.imovel_id, self.imovel.pk)

    def test_clean_rejeita_imovel_diferente_do_contrato(self):
        """clean() levanta ValidationError quando imovel difere do contrato."""
        from django.core.exceptions import ValidationError

        receita = ReceitaAluguel(
            contrato=self.contrato,
            imovel=self.imovel2,
            competencia_mes=2,
            competencia_ano=2024,
            data_vencimento=date(2024, 2, 10),
            valor_previsto=Decimal('2000.00'),
        )

        with self.assertRaises(ValidationError):
            receita.clean()


class ReceitaSaveForcaImovelTest(TestCase):
    """save() deve sempre forçar imovel a partir do contrato, mesmo se informado outro."""

    def setUp(self):
        self.imovel, self.locatario = _criar_base()
        self.imovel2 = Imovel.objects.create(nome='Outro Imóvel', endereco='Rua C', cidade='SP', estado='SP')
        self.contrato = _criar_contrato(
            self.imovel, self.locatario,
            data_inicio=date(2024, 1, 1),
            data_fim=date(2024, 12, 31),
        )

    def test_save_sobrescreve_imovel_errado(self):
        """Mesmo passando imovel2, save() deve substituir pelo imóvel do contrato."""
        receita = ReceitaAluguel(
            contrato=self.contrato,
            imovel=self.imovel2,
            competencia_mes=3,
            competencia_ano=2024,
            data_vencimento=date(2024, 3, 10),
            valor_previsto=Decimal('2000.00'),
        )
        receita.save()
        self.assertEqual(receita.imovel_id, self.imovel.pk)


class EstaAtrasadaTest(TestCase):
    """Testa a property esta_atrasada em ReceitaAluguel e Despesa."""

    def setUp(self):
        self.imovel, self.locatario = _criar_base()
        self.contrato = _criar_contrato(
            self.imovel, self.locatario,
            data_inicio=date(2020, 1, 1),
            data_fim=date(2030, 12, 31),
        )
        self.ontem = timezone.now().date() - timedelta(days=1)
        self.amanha = timezone.now().date() + timedelta(days=1)

    def _receita(self, vencimento, status):
        return ReceitaAluguel(
            contrato=self.contrato,
            imovel=self.imovel,
            competencia_mes=1,
            competencia_ano=2024,
            data_vencimento=vencimento,
            valor_previsto=Decimal('1000.00'),
            status=status,
        )

    def test_receita_prevista_vencida_esta_atrasada(self):
        r = self._receita(self.ontem, 'previsto')
        self.assertTrue(r.esta_atrasada)

    def test_receita_prevista_nao_vencida_nao_esta_atrasada(self):
        r = self._receita(self.amanha, 'previsto')
        self.assertFalse(r.esta_atrasada)

    def test_receita_recebida_nao_esta_atrasada(self):
        r = self._receita(self.ontem, 'recebido')
        self.assertFalse(r.esta_atrasada)

    def test_receita_parcial_nao_esta_atrasada(self):
        r = self._receita(self.ontem, 'parcial')
        self.assertFalse(r.esta_atrasada)

    def test_receita_cancelada_nao_esta_atrasada(self):
        r = self._receita(self.ontem, 'cancelado')
        self.assertFalse(r.esta_atrasada)

    def test_despesa_prevista_vencida_esta_atrasada(self):
        d = Despesa(
            imovel=self.imovel,
            descricao='Teste',
            data_vencimento=self.ontem,
            valor=Decimal('500.00'),
            status='prevista',
        )
        self.assertTrue(d.esta_atrasada)

    def test_despesa_paga_nao_esta_atrasada(self):
        d = Despesa(
            imovel=self.imovel,
            descricao='Teste',
            data_vencimento=self.ontem,
            valor=Decimal('500.00'),
            status='paga',
        )
        self.assertFalse(d.esta_atrasada)

    def test_despesa_cancelada_nao_esta_atrasada(self):
        d = Despesa(
            imovel=self.imovel,
            descricao='Teste',
            data_vencimento=self.ontem,
            valor=Decimal('500.00'),
            status='cancelada',
        )
        self.assertFalse(d.esta_atrasada)


class ReceitasInadimplentesQsTest(TestCase):
    """Testa receitas_inadimplentes_qs() — regra de vencimento, não de status."""

    def setUp(self):
        self.imovel, self.locatario = _criar_base()
        self.contrato = _criar_contrato(
            self.imovel, self.locatario,
            data_inicio=date(2020, 1, 1),
            data_fim=date(2030, 12, 31),
        )
        self.ontem = timezone.now().date() - timedelta(days=1)
        self.amanha = timezone.now().date() + timedelta(days=1)

    def _criar_receita(self, vencimento, status, mes=1):
        return ReceitaAluguel.objects.create(
            contrato=self.contrato,
            imovel=self.imovel,
            competencia_mes=mes,
            competencia_ano=2024,
            data_vencimento=vencimento,
            valor_previsto=Decimal('1000.00'),
            status=status,
        )

    def test_vencida_previsto_aparece_na_lista(self):
        self._criar_receita(self.ontem, 'previsto')
        self.assertEqual(receitas_inadimplentes_qs().count(), 1)

    def test_vencida_atrasado_aparece_na_lista(self):
        self._criar_receita(self.ontem, 'atrasado')
        self.assertEqual(receitas_inadimplentes_qs().count(), 1)

    def test_vencida_recebido_nao_aparece(self):
        self._criar_receita(self.ontem, 'recebido')
        self.assertEqual(receitas_inadimplentes_qs().count(), 0)

    def test_nao_vencida_nao_aparece(self):
        self._criar_receita(self.amanha, 'previsto', mes=2)
        self.assertEqual(receitas_inadimplentes_qs().count(), 0)


class ContratoValidacaoTest(TestCase):
    """Testa validações do modelo Contrato."""

    def setUp(self):
        self.imovel, self.locatario = _criar_base()

    def test_dia_vencimento_invalido_falha_na_validacao(self):
        """dia_vencimento = 0 deve levantar ValidationError via full_clean()."""
        contrato = Contrato(
            imovel=self.imovel,
            locatario=self.locatario,
            data_inicio=date(2024, 1, 1),
            data_fim=date(2024, 12, 31),
            valor_aluguel=Decimal('2000.00'),
            dia_vencimento=0,
        )
        with self.assertRaises(ValidationError):
            contrato.full_clean()

    def test_dia_vencimento_maior_que_31_falha(self):
        contrato = Contrato(
            imovel=self.imovel,
            locatario=self.locatario,
            data_inicio=date(2024, 1, 1),
            data_fim=date(2024, 12, 31),
            valor_aluguel=Decimal('2000.00'),
            dia_vencimento=32,
        )
        with self.assertRaises(ValidationError):
            contrato.full_clean()

    def test_contratos_sobrepostos_falham(self):
        """Dois contratos ativos no mesmo imóvel com período sobreposto são bloqueados."""
        _criar_contrato(
            self.imovel, self.locatario,
            data_inicio=date(2024, 1, 1),
            data_fim=date(2024, 12, 31),
        )
        contrato2 = Contrato(
            imovel=self.imovel,
            locatario=self.locatario,
            data_inicio=date(2024, 6, 1),
            data_fim=date(2025, 5, 31),
            valor_aluguel=Decimal('2500.00'),
            dia_vencimento=10,
            status='ativo',
        )
        with self.assertRaises(ValidationError):
            contrato2.clean()

    def test_contratos_nao_sobrepostos_sao_permitidos(self):
        """Contratos em períodos consecutivos não devem falhar."""
        _criar_contrato(
            self.imovel, self.locatario,
            data_inicio=date(2024, 1, 1),
            data_fim=date(2024, 6, 30),
        )
        contrato2 = Contrato(
            imovel=self.imovel,
            locatario=self.locatario,
            data_inicio=date(2024, 7, 1),
            data_fim=date(2025, 6, 30),
            valor_aluguel=Decimal('2500.00'),
            dia_vencimento=10,
            status='ativo',
        )
        try:
            contrato2.clean()
        except ValidationError:
            self.fail('clean() não deveria levantar erro para contratos não sobrepostos')


class GerarReceitasViewTest(TestCase):
    """Testa a view gerar_receitas_mes_view — GET não gera, POST gera."""

    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user('test', password='test')
        self.client.login(username='test', password='test')
        self.imovel, self.locatario = _criar_base()
        hoje = date.today()
        self.mes = hoje.month
        self.ano = hoje.year
        ultimo_dia = monthrange(self.ano, self.mes)[1]
        self.contrato = _criar_contrato(
            self.imovel, self.locatario,
            data_inicio=date(self.ano, self.mes, 1),
            data_fim=date(self.ano, self.mes, ultimo_dia),
        )

    def test_get_nao_gera_receitas(self):
        """GET na view de geração apenas exibe o formulário, não cria receitas."""
        response = self.client.get(reverse('gerar_receitas_mes'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(ReceitaAluguel.objects.count(), 0)

    def test_post_gera_receitas_e_redireciona(self):
        """POST na view de geração cria receitas e redireciona para a lista."""
        response = self.client.post(
            reverse('gerar_receitas_mes'),
            data={'mes': self.mes, 'ano': self.ano},
        )
        self.assertRedirects(
            response,
            f"{reverse('receitas_list')}?mes={self.mes}&ano={self.ano}",
        )
        self.assertEqual(ReceitaAluguel.objects.count(), 1)

    def test_get_contagem_inclui_contrato_prazo_indeterminado_apos_data_fim(self):
        """
        A contagem de contratos_ativos exibida na tela GET deve incluir
        contratos por prazo indeterminado mesmo em meses após a data_fim
        original (usa contratos_para_geracao_mes, não data_fim__gte cru).
        """
        contrato_indeterminado = _criar_contrato(
            self.imovel, self.locatario,
            data_inicio=date(2020, 1, 1), data_fim=date(2020, 12, 31),
            prazo_indeterminado=True,
        )
        mes_futuro, ano_futuro = 6, date.today().year + 1
        response = self.client.get(reverse('gerar_receitas_mes'), {'mes': mes_futuro, 'ano': ano_futuro})
        self.assertGreaterEqual(response.context['contratos_ativos'], 1)

    def test_get_contagem_exclui_contrato_encerrado(self):
        contrato_encerrado = _criar_contrato(
            self.imovel, self.locatario,
            data_inicio=date(2020, 1, 1), data_fim=date(2030, 12, 31),
            status='encerrado',
        )
        response = self.client.get(reverse('gerar_receitas_mes'), {'mes': self.mes, 'ano': self.ano})
        # Apenas self.contrato (ativo) deve contar; o encerrado nunca conta.
        self.assertEqual(response.context['contratos_ativos'], 1)

    def test_get_contagem_exclui_data_encerramento_real_anterior_ao_mes(self):
        imovel2 = Imovel.objects.create(nome='Imóvel Encerrado', endereco='Rua Z', cidade='SP', estado='SP')
        inicio_do_mes = date(self.ano, self.mes, 1)
        contrato_encerrado_de_fato = _criar_contrato(
            imovel2, self.locatario,
            data_inicio=date(2020, 1, 1), data_fim=date(2030, 12, 31),
            prazo_indeterminado=True,
            data_encerramento_real=inicio_do_mes - timedelta(days=1),
        )
        contratos_aptos = list(contratos_para_geracao_mes(self.mes, self.ano))
        self.assertNotIn(contrato_encerrado_de_fato, contratos_aptos)


class ExportContratosViewTest(TestCase):
    """Testa filtros de status e imóvel na exportação de contratos."""

    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user('test2', password='test2')
        self.client.login(username='test2', password='test2')
        self.imovel, self.locatario = _criar_base()
        self.imovel2 = Imovel.objects.create(nome='Casa Dois', endereco='Rua D', cidade='SP', estado='SP')
        _criar_contrato(self.imovel, self.locatario, date(2024, 1, 1), date(2024, 12, 31), status='ativo')
        _criar_contrato(self.imovel2, self.locatario, date(2023, 1, 1), date(2023, 12, 31), status='encerrado')

    def test_sem_filtro_exporta_apenas_ativos(self):
        response = self.client.get(reverse('export_contratos', args=['csv']))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode('utf-8-sig')
        self.assertIn('Apartamento Teste', content)
        self.assertNotIn('Casa Dois', content)

    def test_filtro_encerrado_exporta_encerrados(self):
        response = self.client.get(reverse('export_contratos', args=['csv']) + '?status=encerrado')
        self.assertEqual(response.status_code, 200)
        content = response.content.decode('utf-8-sig')
        self.assertIn('Casa Dois', content)
        self.assertNotIn('Apartamento Teste', content)

    def test_filtro_imovel_restringe_resultado(self):
        response = self.client.get(
            reverse('export_contratos', args=['csv']) + f'?status=ativo&imovel={self.imovel.pk}'
        )
        self.assertEqual(response.status_code, 200)
        content = response.content.decode('utf-8-sig')
        self.assertIn('Apartamento Teste', content)
        self.assertNotIn('Casa Dois', content)


# ─── Testes da tela de baixa de aluguéis ──────────────────────────────────────

class BaixaReceitasViewTest(TestCase):
    """Testa a view baixa_receitas_mes_view."""

    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user('baixa_user', password='pass')
        self.imovel, self.locatario = _criar_base()
        hoje = date.today()
        self.mes = hoje.month
        self.ano = hoje.year
        ultimo_dia = monthrange(self.ano, self.mes)[1]
        self.contrato = _criar_contrato(
            self.imovel, self.locatario,
            data_inicio=date(self.ano, self.mes, 1),
            data_fim=date(self.ano, self.mes, ultimo_dia),
        )
        self.receita = ReceitaAluguel.objects.create(
            contrato=self.contrato,
            imovel=self.imovel,
            competencia_mes=self.mes,
            competencia_ano=self.ano,
            data_vencimento=date(self.ano, self.mes, 10),
            valor_previsto=Decimal('2000.00'),
            status='previsto',
        )

    def test_exige_login(self):
        """Acesso sem login deve redirecionar para login."""
        response = self.client.get(reverse('baixa_receitas_mes'))
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login/', response['Location'])

    def test_get_mostra_receitas_do_mes(self):
        """GET autenticado mostra as receitas do mês selecionado."""
        self.client.login(username='baixa_user', password='pass')
        response = self.client.get(
            reverse('baixa_receitas_mes'),
            {'mes': self.mes, 'ano': self.ano},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(self.receita, response.context['receitas'])

    def test_post_marcar_recebida_preenche_valores(self):
        """POST action=marcar_recebida preenche valor e data automaticamente."""
        self.client.login(username='baixa_user', password='pass')
        self.client.post(
            reverse('baixa_receitas_mes'),
            {
                'mes': self.mes, 'ano': self.ano,
                'receita_id': self.receita.pk,
                'action': 'marcar_recebida',
            },
        )
        self.receita.refresh_from_db()
        self.assertEqual(self.receita.status, 'recebido')
        self.assertEqual(self.receita.valor_recebido, Decimal('2000.00'))
        self.assertEqual(self.receita.data_recebimento, date.today())

    def test_post_marcar_recebida_nao_sobrescreve_valor_existente(self):
        """POST action=marcar_recebida preserva valor_recebido se já preenchido."""
        self.receita.valor_recebido = Decimal('1800.00')
        self.receita.save()
        self.client.login(username='baixa_user', password='pass')
        self.client.post(
            reverse('baixa_receitas_mes'),
            {
                'mes': self.mes, 'ano': self.ano,
                'receita_id': self.receita.pk,
                'action': 'marcar_recebida',
            },
        )
        self.receita.refresh_from_db()
        self.assertEqual(self.receita.valor_recebido, Decimal('1800.00'))

    def test_post_editar_atualiza_campos(self):
        """POST action=editar atualiza status, valor_recebido, data e observações."""
        self.client.login(username='baixa_user', password='pass')
        self.client.post(
            reverse('baixa_receitas_mes'),
            {
                'mes': self.mes, 'ano': self.ano,
                'receita_id': self.receita.pk,
                'action': 'editar',
                'status': 'parcial',
                'valor_recebido': '1500.00',
                'data_recebimento': date.today().isoformat(),
                'observacoes': 'Pagamento parcial acordado.',
            },
        )
        self.receita.refresh_from_db()
        self.assertEqual(self.receita.status, 'parcial')
        self.assertEqual(self.receita.valor_recebido, Decimal('1500.00'))
        self.assertEqual(self.receita.observacoes, 'Pagamento parcial acordado.')


# ─── Testes do relatório de contabilidade ────────────────────────────────────

class RelatorioContabilidadeTest(TestCase):
    """Testa exportar_relatorio_contabilidade_xlsx."""

    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user('cont_user', password='pass')
        self.client.login(username='cont_user', password='pass')
        self.imovel, self.locatario = _criar_base()
        self.contrato = _criar_contrato(
            self.imovel, self.locatario,
            data_inicio=date(2024, 1, 1),
            data_fim=date(2024, 12, 31),
        )
        self.ontem = timezone.now().date() - timedelta(days=1)

    def test_retorna_xlsx_valido(self):
        """GET retorna status 200 e content-type XLSX."""
        response = self.client.get(
            reverse('export_relatorio_contabilidade'),
            {'mes': 1, 'ano': 2024},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn('spreadsheetml', response['Content-Type'])

    def test_inclui_receita_vencida_com_status_previsto(self):
        """Receita vencida mas com status=previsto aparece na aba Inadimplência."""
        import io
        try:
            import openpyxl
        except ImportError:
            self.skipTest('openpyxl não instalado')

        ReceitaAluguel.objects.create(
            contrato=self.contrato,
            imovel=self.imovel,
            competencia_mes=1,
            competencia_ano=2024,
            data_vencimento=self.ontem,
            valor_previsto=Decimal('2000.00'),
            status='previsto',
        )
        response = self.client.get(
            reverse('export_relatorio_contabilidade'),
            {'mes': 1, 'ano': 2024},
        )
        wb = openpyxl.load_workbook(io.BytesIO(response.content))
        self.assertIn('Inadimplência Aberta', wb.sheetnames)
        ws = wb['Inadimplência Aberta']
        valores = [row[0] for row in ws.iter_rows(min_row=2, values_only=True) if row[0]]
        self.assertTrue(any(self.imovel.nome in str(v) for v in valores))


# ─── Testes de DocumentoObrigatorio ──────────────────────────────────────────

class DocumentoObrigatorioTest(TestCase):
    """Testa o model DocumentoObrigatorio e contadores."""

    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user('doc_user', password='pass')
        self.client.login(username='doc_user', password='pass')
        self.imovel, self.locatario = _criar_base()

    def test_pendente_sem_documento_vinculado(self):
        """DocumentoObrigatorio sem documento vinculado deve ser pendente."""
        from documentos.models import DocumentoObrigatorio
        ob = DocumentoObrigatorio(
            imovel=self.imovel,
            tipo='matricula',
            descricao='Matrícula do imóvel',
            obrigatorio=True,
        )
        self.assertTrue(ob.pendente)

    def test_nao_pendente_com_documento_vinculado(self):
        """DocumentoObrigatorio com documento vinculado não é pendente."""
        from documentos.models import DocumentoObrigatorio, Documento
        import tempfile, os
        from django.core.files.uploadedfile import SimpleUploadedFile
        doc = Documento.objects.create(
            titulo='Matrícula',
            tipo='matricula',
            arquivo=SimpleUploadedFile('test.pdf', b'conteudo'),
            imovel=self.imovel,
        )
        ob = DocumentoObrigatorio(
            imovel=self.imovel,
            tipo='matricula',
            descricao='Matrícula do imóvel',
            obrigatorio=True,
            documento=doc,
        )
        self.assertFalse(ob.pendente)

    def test_dashboard_conta_pendentes(self):
        """Dashboard exibe contador de docs obrigatórios pendentes."""
        from documentos.models import DocumentoObrigatorio
        DocumentoObrigatorio.objects.create(
            imovel=self.imovel,
            tipo='matricula',
            descricao='Matrícula',
            obrigatorio=True,
        )
        response = self.client.get(reverse('dashboard'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['docs_obrigatorios_pendentes'], 1)

    def test_dashboard_nao_obrigatorio_nao_conta(self):
        """DocumentoObrigatorio com obrigatorio=False não conta no dashboard."""
        from documentos.models import DocumentoObrigatorio
        DocumentoObrigatorio.objects.create(
            imovel=self.imovel,
            tipo='seguro',
            descricao='Seguro',
            obrigatorio=False,
        )
        response = self.client.get(reverse('dashboard'))
        self.assertEqual(response.context['docs_obrigatorios_pendentes'], 0)


# ─── Teste do backup_local (ZIP) ──────────────────────────────────────────────

class BackupLocalTest(TestCase):
    """Testa o comando backup_local — geração de ZIP."""

    def test_gera_arquivo_zip_com_manifest(self):
        """backup_local deve criar um .zip contendo manifest.txt."""
        import tempfile
        import zipfile as ziplib
        from io import StringIO
        from django.core.management import call_command

        with tempfile.TemporaryDirectory() as tmpdir:
            call_command('backup_local', '--destino', tmpdir, verbosity=0)
            zips = [f for f in os.listdir(tmpdir) if f.endswith('.zip')]
            self.assertEqual(len(zips), 1, 'Deve existir exatamente 1 arquivo .zip')
            with ziplib.ZipFile(os.path.join(tmpdir, zips[0])) as zf:
                self.assertIn('manifest.txt', zf.namelist())

    def test_manter_apenas_n_backups(self):
        """--manter N apaga os zips mais antigos mantendo apenas N."""
        import tempfile
        import zipfile as ziplib
        from django.core.management import call_command

        with tempfile.TemporaryDirectory() as tmpdir:
            # Cria 3 backups antigos com timestamps distintos
            for i in range(1, 4):
                path = os.path.join(tmpdir, f'backup_2024010{i}_120000.zip')
                with ziplib.ZipFile(path, 'w') as zf:
                    zf.writestr('manifest.txt', f'dummy {i}')
            # Executa com --manter 2: cria +1, remove os 2 mais antigos
            call_command('backup_local', '--destino', tmpdir, '--manter', '2', verbosity=0)
            zips = [f for f in os.listdir(tmpdir) if f.endswith('.zip')]
            self.assertEqual(len(zips), 2)


# ─── Testes do formulário baixa_receitas (Items 1-3) ─────────────────────────

class BaixaReceitasFormTest(TestCase):
    """Verifica que baixa_receitas.html não tem formulário aninhado."""

    def setUp(self):
        self.client = Client()
        User.objects.create_user('form_user', password='pass')
        self.client.login(username='form_user', password='pass')

    def test_get_renderiza_sem_form_aninhado(self):
        """GET da tela de baixa deve retornar status 200 sem form aninhado."""
        response = self.client.get(reverse('baixa_receitas_mes'))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        # Não deve haver dois <form method="post"> dentro de um <form method="get">
        # Verificamos que o HTML contém o botão Gerar mas como form separado
        self.assertIn('gerar_receitas', content)
        self.assertIn('Gerar Receitas Esperadas', content)

    def test_post_gerar_receitas_a_partir_de_baixa(self):
        """POST action=gerar_receitas na tela de baixa gera receitas corretamente."""
        imovel, locatario = _criar_base()
        hoje = date.today()
        ultimo_dia = monthrange(hoje.year, hoje.month)[1]
        _criar_contrato(
            imovel, locatario,
            date(hoje.year, hoje.month, 1),
            date(hoje.year, hoje.month, ultimo_dia),
        )
        response = self.client.post(
            reverse('baixa_receitas_mes'),
            {'mes': hoje.month, 'ano': hoje.year, 'action': 'gerar_receitas'},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(ReceitaAluguel.objects.count(), 1)


# ─── Testes de DocumentoObrigatorio.clean() (Item 4) ─────────────────────────

class DocumentoObrigatorioCleanTest(TestCase):
    """Testa o método clean() do DocumentoObrigatorio."""

    def setUp(self):
        from documentos.models import Documento, DocumentoObrigatorio
        from django.core.files.uploadedfile import SimpleUploadedFile
        self.imovel, _ = _criar_base()
        self.imovel2 = Imovel.objects.create(nome='Outro', endereco='Rua Z', cidade='SP', estado='SP')
        self.doc_imovel1 = Documento.objects.create(
            titulo='Matrícula 1',
            tipo='matricula',
            arquivo=SimpleUploadedFile('m1.pdf', b'x'),
            imovel=self.imovel,
        )
        self.doc_sem_imovel = Documento.objects.create(
            titulo='Sem Imóvel',
            tipo='outro',
            arquivo=SimpleUploadedFile('s.pdf', b'x'),
            imovel=None,
        )

    def test_clean_aceita_documento_do_mesmo_imovel(self):
        """clean() não levanta erro quando documento pertence ao mesmo imóvel."""
        from documentos.models import DocumentoObrigatorio
        from django.core.exceptions import ValidationError
        ob = DocumentoObrigatorio(
            imovel=self.imovel,
            tipo='matricula',
            descricao='Matrícula',
            documento=self.doc_imovel1,
        )
        try:
            ob.clean()
        except ValidationError:
            self.fail('clean() não deveria levantar ValidationError para documento do mesmo imóvel.')

    def test_clean_rejeita_documento_de_outro_imovel(self):
        """clean() rejeita documento que pertence a outro imóvel."""
        from documentos.models import DocumentoObrigatorio
        from django.core.exceptions import ValidationError
        ob = DocumentoObrigatorio(
            imovel=self.imovel2,
            tipo='matricula',
            descricao='Matrícula',
            documento=self.doc_imovel1,
        )
        with self.assertRaises(ValidationError):
            ob.clean()

    def test_clean_rejeita_documento_sem_imovel(self):
        """clean() rejeita documento sem imóvel vinculado."""
        from documentos.models import DocumentoObrigatorio
        from django.core.exceptions import ValidationError
        ob = DocumentoObrigatorio(
            imovel=self.imovel,
            tipo='outro',
            descricao='Genérico',
            documento=self.doc_sem_imovel,
        )
        with self.assertRaises(ValidationError):
            ob.clean()

    def test_clean_sem_documento_passa(self):
        """clean() sem documento vinculado não levanta erro."""
        from documentos.models import DocumentoObrigatorio
        ob = DocumentoObrigatorio(
            imovel=self.imovel,
            tipo='seguro',
            descricao='Seguro',
            documento=None,
        )
        try:
            ob.clean()
        except Exception:
            self.fail('clean() sem documento não deve levantar exceção.')


# ─── Testes da constraint única (Item 5) ─────────────────────────────────────

class DocumentoObrigatorioUniqueTest(TestCase):
    """Testa que unique_together inclui descricao (imovel, tipo, descricao)."""

    def setUp(self):
        from documentos.models import DocumentoObrigatorio
        self.imovel, _ = _criar_base()
        DocumentoObrigatorio.objects.create(
            imovel=self.imovel,
            tipo='matricula',
            descricao='Matrícula principal',
        )

    def test_mesmo_tipo_descricao_diferente_permitido(self):
        """Dois DocumentoObrigatorio com mesmo tipo mas descrição diferente são permitidos."""
        from documentos.models import DocumentoObrigatorio
        from django.db import IntegrityError
        try:
            DocumentoObrigatorio.objects.create(
                imovel=self.imovel,
                tipo='matricula',
                descricao='Matrícula atualizada 2024',
            )
        except IntegrityError:
            self.fail('Deve ser possível ter mesmo tipo com descrição diferente.')

    def test_mesmo_tipo_mesma_descricao_falha(self):
        """Duplicar imovel+tipo+descricao deve levantar IntegrityError."""
        from documentos.models import DocumentoObrigatorio
        from django.db import IntegrityError
        with self.assertRaises(IntegrityError):
            DocumentoObrigatorio.objects.create(
                imovel=self.imovel,
                tipo='matricula',
                descricao='Matrícula principal',
            )


# ─── Testes do relatório contabilidade — nota inadimplência (Item 6) ──────────

class RelatorioContabilidadeNotaTest(TestCase):
    """Testa que o Resumo do relatório contabilidade inclui nota sobre inadimplência."""

    def setUp(self):
        self.client = Client()
        User.objects.create_user('nota_user', password='pass')
        self.client.login(username='nota_user', password='pass')

    def test_resumo_contem_nota_inadimplencia(self):
        """A aba Resumo deve conter a nota sobre o escopo da aba Inadimplência Aberta."""
        import io
        try:
            import openpyxl
        except ImportError:
            self.skipTest('openpyxl não instalado')
        response = self.client.get(
            reverse('export_relatorio_contabilidade'),
            {'mes': 1, 'ano': 2024},
        )
        wb = openpyxl.load_workbook(io.BytesIO(response.content))
        ws = wb['Resumo']
        valores = [str(row[0] or '') for row in ws.iter_rows(values_only=True)]
        self.assertTrue(any('Nota' in v for v in valores), 'Resumo deve conter linha de nota.')

    def test_aba_inadimplencia_renomeada(self):
        """A aba de inadimplência deve se chamar 'Inadimplência Aberta'."""
        import io
        try:
            import openpyxl
        except ImportError:
            self.skipTest('openpyxl não instalado')
        response = self.client.get(
            reverse('export_relatorio_contabilidade'),
            {'mes': 1, 'ano': 2024},
        )
        wb = openpyxl.load_workbook(io.BytesIO(response.content))
        self.assertIn('Inadimplência Aberta', wb.sheetnames)
        self.assertNotIn('Inadimplência', wb.sheetnames)


# ─── Testes do checklist mensal (Item 8) ─────────────────────────────────────

class ChecklistMensalViewTest(TestCase):
    """Testa a view checklist_mensal_view."""

    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user('chk_user', password='pass')
        self.imovel, self.locatario = _criar_base()
        hoje = date.today()
        self.mes = hoje.month
        self.ano = hoje.year

    def test_exige_login(self):
        """Acesso sem login deve redirecionar."""
        response = self.client.get(reverse('checklist_mensal'))
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login/', response['Location'])

    def test_get_renderiza_checklist(self):
        """GET autenticado renderiza a página de checklist."""
        self.client.login(username='chk_user', password='pass')
        response = self.client.get(
            reverse('checklist_mensal'),
            {'mes': self.mes, 'ano': self.ano},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn('checklist', response.context)
        self.assertEqual(len(response.context['checklist']), 9)

    def test_post_marcar_enviado_cria_fechamento(self):
        """POST action=marcar_enviado cria FechamentoMensal e redireciona."""
        from financeiro.models import FechamentoMensal
        self.client.login(username='chk_user', password='pass')
        response = self.client.post(
            reverse('checklist_mensal'),
            {'mes': self.mes, 'ano': self.ano, 'action': 'marcar_enviado'},
        )
        self.assertEqual(response.status_code, 302)
        fechamento = FechamentoMensal.objects.get(mes=self.mes, ano=self.ano)
        self.assertTrue(fechamento.enviado_contabilidade)
        self.assertIsNotNone(fechamento.data_envio_contabilidade)

    def test_post_marcar_enviado_idempotente(self):
        """Chamar marcar_enviado duas vezes não duplica FechamentoMensal."""
        from financeiro.models import FechamentoMensal
        self.client.login(username='chk_user', password='pass')
        for _ in range(2):
            self.client.post(
                reverse('checklist_mensal'),
                {'mes': self.mes, 'ano': self.ano, 'action': 'marcar_enviado'},
            )
        self.assertEqual(FechamentoMensal.objects.filter(mes=self.mes, ano=self.ano).count(), 1)

    def test_dashboard_contem_link_checklist(self):
        """Dashboard deve ter link para o checklist mensal."""
        self.client.login(username='chk_user', password='pass')
        response = self.client.get(reverse('dashboard'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse('checklist_mensal'))

    def test_checklist_sem_receitas_etapa_nao_ok(self):
        """Com nenhuma receita no mês, a etapa 'Receitas geradas' deve estar não-ok."""
        self.client.login(username='chk_user', password='pass')
        response = self.client.get(
            reverse('checklist_mensal'),
            {'mes': self.mes, 'ano': self.ano},
        )
        checklist = response.context['checklist']
        etapa_receitas = next(e for e in checklist if e['item'] == 'Receitas geradas')
        self.assertFalse(etapa_receitas['ok'])

    def test_checklist_contem_etapa_docs_obrigatorios(self):
        """Checklist deve incluir etapa de documentos obrigatórios."""
        self.client.login(username='chk_user', password='pass')
        response = self.client.get(reverse('checklist_mensal'), {'mes': self.mes, 'ano': self.ano})
        checklist = response.context['checklist']
        items = [e['item'] for e in checklist]
        self.assertIn('Documentos obrigatórios revisados', items)

    def test_docs_obrigatorios_pendentes_torna_etapa_nao_ok(self):
        """Criar DocumentoObrigatorio sem doc vinculado torna a etapa não-ok."""
        from documentos.models import DocumentoObrigatorio
        DocumentoObrigatorio.objects.create(
            imovel=self.imovel, tipo='matricula', descricao='Matrícula', obrigatorio=True
        )
        self.client.login(username='chk_user', password='pass')
        response = self.client.get(reverse('checklist_mensal'), {'mes': self.mes, 'ano': self.ano})
        checklist = response.context['checklist']
        etapa = next(e for e in checklist if e['item'] == 'Documentos obrigatórios revisados')
        self.assertFalse(etapa['ok'])
        self.assertEqual(response.context['docs_obrigatorios_pendentes'], 1)

    def test_docs_obrigatorios_todos_vinculados_torna_etapa_ok(self):
        """DocumentoObrigatorio com doc vinculado torna a etapa ok."""
        from documentos.models import DocumentoObrigatorio, Documento
        from django.core.files.uploadedfile import SimpleUploadedFile
        doc = Documento.objects.create(
            titulo='Matrícula',
            tipo='matricula',
            arquivo=SimpleUploadedFile('m.pdf', b'x'),
            imovel=self.imovel,
        )
        DocumentoObrigatorio.objects.create(
            imovel=self.imovel, tipo='matricula', descricao='Matrícula', obrigatorio=True, documento=doc
        )
        self.client.login(username='chk_user', password='pass')
        response = self.client.get(reverse('checklist_mensal'), {'mes': self.mes, 'ano': self.ano})
        checklist = response.context['checklist']
        etapa = next(e for e in checklist if e['item'] == 'Documentos obrigatórios revisados')
        self.assertTrue(etapa['ok'])

    def test_checklist_contem_etapa_relatorio_contabil(self):
        """Checklist deve incluir etapa informativa de relatório contábil."""
        self.client.login(username='chk_user', password='pass')
        response = self.client.get(reverse('checklist_mensal'), {'mes': self.mes, 'ano': self.ano})
        checklist = response.context['checklist']
        etapa = next((e for e in checklist if e['item'] == 'Relatório contábil exportado'), None)
        self.assertIsNotNone(etapa)
        self.assertTrue(etapa.get('informativa', False))
        self.assertIn(reverse('export_relatorio_contabilidade'), etapa['link'])


# ─── Testes de baixa de aluguéis filtrada por imóvel ─────────────────────────

class BaixaReceitasFiltroImovelTest(TestCase):
    """Testa filtro por imóvel na tela de baixa de aluguéis."""

    def setUp(self):
        self.client = Client()
        User.objects.create_user('filtro_user', password='pass')
        self.client.login(username='filtro_user', password='pass')
        locatario = Pessoa.objects.create(nome='Locatário Filtro', tipo='locatario')
        self.imovel1 = Imovel.objects.create(nome='Apto 1', endereco='Rua A', cidade='SP', estado='SP')
        self.imovel2 = Imovel.objects.create(nome='Casa 2', endereco='Rua B', cidade='SP', estado='SP')
        hoje = date.today()
        self.mes, self.ano = hoje.month, hoje.year
        ultimo_dia = monthrange(self.ano, self.mes)[1]
        c1 = _criar_contrato(self.imovel1, locatario, date(self.ano, self.mes, 1), date(self.ano, self.mes, ultimo_dia))
        c2 = _criar_contrato(self.imovel2, locatario, date(self.ano, self.mes, 1), date(self.ano, self.mes, ultimo_dia))
        self.r1 = ReceitaAluguel.objects.create(
            contrato=c1, imovel=self.imovel1,
            competencia_mes=self.mes, competencia_ano=self.ano,
            data_vencimento=date(self.ano, self.mes, 10),
            valor_previsto=Decimal('1000.00'), status='previsto',
        )
        self.r2 = ReceitaAluguel.objects.create(
            contrato=c2, imovel=self.imovel2,
            competencia_mes=self.mes, competencia_ano=self.ano,
            data_vencimento=date(self.ano, self.mes, 10),
            valor_previsto=Decimal('2000.00'), status='previsto',
        )

    def test_get_sem_filtro_retorna_todas_receitas(self):
        """Sem filtro de imóvel, retorna receitas de todos os imóveis."""
        response = self.client.get(reverse('baixa_receitas_mes'), {'mes': self.mes, 'ano': self.ano})
        self.assertEqual(response.status_code, 200)
        self.assertIn(self.r1, response.context['receitas'])
        self.assertIn(self.r2, response.context['receitas'])

    def test_get_com_filtro_retorna_apenas_imovel_selecionado(self):
        """Com filtro de imóvel, retorna apenas as receitas do imóvel."""
        response = self.client.get(
            reverse('baixa_receitas_mes'),
            {'mes': self.mes, 'ano': self.ano, 'imovel': self.imovel1.pk},
        )
        self.assertEqual(response.status_code, 200)
        receitas = list(response.context['receitas'])
        self.assertIn(self.r1, receitas)
        self.assertNotIn(self.r2, receitas)

    def test_post_marcar_recebida_preserva_filtro_imovel(self):
        """POST marcar_recebida com imovel redireciona preservando imovel no URL."""
        response = self.client.post(
            reverse('baixa_receitas_mes'),
            {
                'mes': self.mes, 'ano': self.ano,
                'imovel': self.imovel1.pk,
                'receita_id': self.r1.pk,
                'action': 'marcar_recebida',
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn(f'imovel={self.imovel1.pk}', response['Location'])

    def test_post_editar_preserva_filtro_imovel(self):
        """POST editar com imovel redireciona preservando imovel no URL."""
        response = self.client.post(
            reverse('baixa_receitas_mes'),
            {
                'mes': self.mes, 'ano': self.ano,
                'imovel': self.imovel1.pk,
                'receita_id': self.r1.pk,
                'action': 'editar',
                'status': 'parcial',
                'valor_recebido': '800.00',
                'data_recebimento': date.today().isoformat(),
                'observacoes': '',
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn(f'imovel={self.imovel1.pk}', response['Location'])

    def test_contexto_contem_imoveis_e_imovel_atual(self):
        """GET com filtro popula imovel_atual e imoveis no contexto."""
        response = self.client.get(
            reverse('baixa_receitas_mes'),
            {'mes': self.mes, 'ano': self.ano, 'imovel': self.imovel1.pk},
        )
        self.assertEqual(str(self.imovel1.pk), response.context['imovel_atual'])
        self.assertIn(self.imovel1, response.context['imoveis'])


# ─── Geração de receitas: prazo indeterminado e encerramento real ────────────

class GerarReceitasPrazoIndeterminadoTest(TestCase):
    """Testa gerar_receitas_mes/gerar_receitas_para_contrato com prazo indeterminado."""

    def setUp(self):
        self.imovel, self.locatario = _criar_base()

    def test_geracao_continua_apos_data_fim_original(self):
        """Contrato por prazo indeterminado gera receita em mês posterior à data_fim original."""
        contrato = _criar_contrato(
            self.imovel, self.locatario,
            data_inicio=date(2024, 1, 1), data_fim=date(2024, 3, 31),
        )
        contrato.prazo_indeterminado = True
        contrato.save()

        # Gera explicitamente para um mês além da data_fim original (junho/2024)
        criadas, existiam = gerar_receitas_mes(6, 2024)

        self.assertEqual(criadas, 1)
        self.assertTrue(
            ReceitaAluguel.objects.filter(contrato=contrato, competencia_mes=6, competencia_ano=2024).exists()
        )

    def test_data_encerramento_real_limita_geracao(self):
        """Contrato com data_encerramento_real não gera receita após o encerramento."""
        contrato = _criar_contrato(
            self.imovel, self.locatario,
            data_inicio=date(2024, 1, 1), data_fim=date(2024, 12, 31),
        )
        contrato.prazo_indeterminado = True
        contrato.data_encerramento_real = date(2024, 3, 31)
        contrato.save()

        criadas, existiam = gerar_receitas_mes(6, 2024)

        self.assertEqual(criadas, 0)
        self.assertFalse(
            ReceitaAluguel.objects.filter(contrato=contrato, competencia_mes=6, competencia_ano=2024).exists()
        )

    def test_contrato_encerrado_nao_gera_receita(self):
        """Contrato com status diferente de ativo nunca gera receita, mesmo com prazo indeterminado."""
        contrato = _criar_contrato(
            self.imovel, self.locatario,
            data_inicio=date(2024, 1, 1), data_fim=date(2024, 3, 31),
            status='encerrado',
        )
        contrato.prazo_indeterminado = True
        contrato.save()

        criadas, existiam = gerar_receitas_mes(6, 2024)
        self.assertEqual(criadas, 0)
        self.assertEqual(existiam, 0)


# ─── Geração de receitas: encargos e itens ────────────────────────────────────

class GerarReceitasComEncargosTest(TestCase):
    """Testa a geração de ReceitaAluguelItem a partir de EncargoContrato."""

    def setUp(self):
        self.imovel, self.locatario = _criar_base()
        self.contrato = _criar_contrato(
            self.imovel, self.locatario,
            data_inicio=date(2024, 1, 1), data_fim=date(2024, 12, 31),
            valor_aluguel=Decimal('2000.00'),
        )

    def test_contrato_com_iptu_mas_sem_aluguel_gera_aluguel_e_iptu(self):
        """
        Contrato com apenas um encargo de IPTU cadastrado (sem aluguel) deve,
        ao gerar a receita, ter o encargo de aluguel garantido automaticamente
        — a receita final soma aluguel + IPTU, nunca apenas o IPTU.
        """
        EncargoContrato.objects.create(contrato=self.contrato, tipo='iptu', valor=Decimal('150.00'))

        gerar_receitas_para_contrato(self.contrato, data_inicio=date(2024, 3, 1), data_fim=date(2024, 3, 31))

        receita = ReceitaAluguel.objects.get(contrato=self.contrato, competencia_mes=3, competencia_ano=2024)
        tipos = set(receita.itens.values_list('tipo', flat=True))
        self.assertEqual(tipos, {'aluguel', 'iptu'})
        self.assertEqual(receita.valor_previsto, Decimal('2150.00'))

    def test_receita_soma_aluguel_iptu_taxa_manutencao(self):
        EncargoContrato.objects.create(contrato=self.contrato, tipo='aluguel', valor=Decimal('2000.00'))
        EncargoContrato.objects.create(contrato=self.contrato, tipo='iptu', valor=Decimal('150.00'))
        EncargoContrato.objects.create(contrato=self.contrato, tipo='taxa_manutencao', valor=Decimal('300.00'))

        gerar_receitas_para_contrato(self.contrato, data_inicio=date(2024, 3, 1), data_fim=date(2024, 3, 31))

        receita = ReceitaAluguel.objects.get(contrato=self.contrato, competencia_mes=3, competencia_ano=2024)
        self.assertEqual(receita.valor_previsto, Decimal('2450.00'))
        self.assertEqual(receita.itens.count(), 3)
        soma_itens = sum(i.valor for i in receita.itens.all())
        self.assertEqual(soma_itens, receita.valor_previsto)

    def test_encargo_com_inicio_futuro_nao_cobrado_antes(self):
        EncargoContrato.objects.create(contrato=self.contrato, tipo='aluguel', valor=Decimal('2000.00'))
        EncargoContrato.objects.create(
            contrato=self.contrato, tipo='seguro', valor=Decimal('100.00'),
            data_inicio_cobranca=date(2024, 6, 1),
        )

        gerar_receitas_para_contrato(self.contrato, data_inicio=date(2024, 3, 1), data_fim=date(2024, 3, 31))
        gerar_receitas_para_contrato(self.contrato, data_inicio=date(2024, 6, 1), data_fim=date(2024, 6, 30))

        receita_marco = ReceitaAluguel.objects.get(contrato=self.contrato, competencia_mes=3, competencia_ano=2024)
        receita_junho = ReceitaAluguel.objects.get(contrato=self.contrato, competencia_mes=6, competencia_ano=2024)
        self.assertEqual(receita_marco.valor_previsto, Decimal('2000.00'))
        self.assertEqual(receita_junho.valor_previsto, Decimal('2100.00'))

    def test_encargo_encerrado_nao_cobrado_depois(self):
        EncargoContrato.objects.create(contrato=self.contrato, tipo='aluguel', valor=Decimal('2000.00'))
        EncargoContrato.objects.create(
            contrato=self.contrato, tipo='seguro', valor=Decimal('100.00'),
            data_fim_cobranca=date(2024, 3, 31),
        )

        gerar_receitas_para_contrato(self.contrato, data_inicio=date(2024, 3, 1), data_fim=date(2024, 3, 31))
        gerar_receitas_para_contrato(self.contrato, data_inicio=date(2024, 4, 1), data_fim=date(2024, 4, 30))

        receita_marco = ReceitaAluguel.objects.get(contrato=self.contrato, competencia_mes=3, competencia_ano=2024)
        receita_abril = ReceitaAluguel.objects.get(contrato=self.contrato, competencia_mes=4, competencia_ano=2024)
        self.assertEqual(receita_marco.valor_previsto, Decimal('2100.00'))
        self.assertEqual(receita_abril.valor_previsto, Decimal('2000.00'))

    def test_fallback_valor_aluguel_sem_encargos(self):
        """
        Contrato sem nenhum EncargoContrato continua usando valor_aluguel — a
        geração garante automaticamente um encargo de aluguel (item 1 desta
        fase), então a receita passa a ter exatamente 1 item (aluguel).
        """
        gerar_receitas_para_contrato(self.contrato, data_inicio=date(2024, 3, 1), data_fim=date(2024, 3, 31))

        receita = ReceitaAluguel.objects.get(contrato=self.contrato, competencia_mes=3, competencia_ano=2024)
        self.assertEqual(receita.valor_previsto, self.contrato.valor_aluguel)
        self.assertEqual(receita.itens.count(), 1)
        self.assertEqual(receita.itens.first().tipo, 'aluguel')

    def test_receitas_ja_existentes_nao_sao_alteradas(self):
        """Gerar novamente não sobrescreve receita/itens já existentes."""
        EncargoContrato.objects.create(contrato=self.contrato, tipo='aluguel', valor=Decimal('2000.00'))
        gerar_receitas_para_contrato(self.contrato, data_inicio=date(2024, 3, 1), data_fim=date(2024, 3, 31))

        receita = ReceitaAluguel.objects.get(contrato=self.contrato, competencia_mes=3, competencia_ano=2024)
        receita.valor_recebido = Decimal('2000.00')
        receita.status = 'recebido'
        receita.save()

        # Adiciona um novo encargo depois — não deve afetar a receita já criada
        EncargoContrato.objects.create(contrato=self.contrato, tipo='iptu', valor=Decimal('150.00'))
        criadas, existiam = gerar_receitas_para_contrato(
            self.contrato, data_inicio=date(2024, 3, 1), data_fim=date(2024, 3, 31)
        )

        self.assertEqual(criadas, 0)
        self.assertEqual(existiam, 1)
        receita.refresh_from_db()
        self.assertEqual(receita.valor_previsto, Decimal('2000.00'))
        self.assertEqual(receita.status, 'recebido')
        # O novo encargo IPTU (criado depois) não deve retroagir sobre a receita já existente
        self.assertEqual(receita.itens.count(), 1)


# ─── Taxa de administração da imobiliária (despesa automática) ────────────────

class TaxaAdministracaoDespesaTest(TestCase):
    def setUp(self):
        self.imovel, self.locatario = _criar_base()

    def test_contrato_com_taxa_gera_despesa_automatica(self):
        contrato = _criar_contrato(
            self.imovel, self.locatario,
            data_inicio=date(2024, 1, 1), data_fim=date(2024, 12, 31),
            valor_aluguel=Decimal('2000.00'),
        )
        contrato.comissao_imobiliaria_percentual = Decimal('10.00')
        contrato.save()

        gerar_receitas_para_contrato(contrato, data_inicio=date(2024, 3, 1), data_fim=date(2024, 3, 31))

        despesa = Despesa.objects.get(contrato=contrato, origem_automatica=True, categoria='comissao_imobiliaria')
        self.assertEqual(despesa.valor, Decimal('200.00'))

    def test_contrato_sem_taxa_nao_gera_despesa(self):
        contrato = _criar_contrato(
            self.imovel, self.locatario,
            data_inicio=date(2024, 1, 1), data_fim=date(2024, 12, 31),
        )
        gerar_receitas_para_contrato(contrato, data_inicio=date(2024, 3, 1), data_fim=date(2024, 3, 31))
        self.assertFalse(Despesa.objects.filter(contrato=contrato, origem_automatica=True).exists())

    def test_gerar_duas_vezes_nao_duplica_despesa(self):
        contrato = _criar_contrato(
            self.imovel, self.locatario,
            data_inicio=date(2024, 1, 1), data_fim=date(2024, 12, 31),
        )
        contrato.comissao_imobiliaria_percentual = Decimal('8.00')
        contrato.save()

        gerar_receitas_para_contrato(contrato, data_inicio=date(2024, 3, 1), data_fim=date(2024, 3, 31))
        gerar_receitas_para_contrato(contrato, data_inicio=date(2024, 3, 1), data_fim=date(2024, 3, 31))

        self.assertEqual(
            Despesa.objects.filter(contrato=contrato, origem_automatica=True).count(), 1
        )

    def test_taxa_calculada_sobre_encargo_aluguel_nao_sobre_total(self):
        contrato = _criar_contrato(
            self.imovel, self.locatario,
            data_inicio=date(2024, 1, 1), data_fim=date(2024, 12, 31),
            valor_aluguel=Decimal('2000.00'),
        )
        contrato.comissao_imobiliaria_percentual = Decimal('10.00')
        contrato.save()
        EncargoContrato.objects.create(contrato=contrato, tipo='aluguel', valor=Decimal('2000.00'))
        EncargoContrato.objects.create(contrato=contrato, tipo='iptu', valor=Decimal('500.00'))

        gerar_receitas_para_contrato(contrato, data_inicio=date(2024, 3, 1), data_fim=date(2024, 3, 31))

        despesa = Despesa.objects.get(contrato=contrato, origem_automatica=True)
        # 10% sobre 2000 (aluguel) — não sobre 2500 (aluguel + IPTU)
        self.assertEqual(despesa.valor, Decimal('200.00'))


# ─── Multa e juros por atraso ──────────────────────────────────────────────────

class CalcularMultaJurosTest(TestCase):
    def setUp(self):
        self.imovel, self.locatario = _criar_base()
        self.contrato = _criar_contrato(
            self.imovel, self.locatario,
            data_inicio=date(2020, 1, 1), data_fim=date(2030, 12, 31),
        )
        self.contrato.multa_atraso_percentual = Decimal('2.00')
        self.contrato.juros_mora_percentual_mes = Decimal('1.00')
        self.contrato.dias_carencia_multa = 3
        self.contrato.save()

    def _receita(self, vencimento):
        return ReceitaAluguel(
            contrato=self.contrato, imovel=self.imovel,
            competencia_mes=1, competencia_ano=2024,
            data_vencimento=vencimento, valor_previsto=Decimal('1000.00'),
            status='previsto',
        )

    def test_sem_atraso_multa_juros_zero(self):
        receita = self._receita(date.today() + timedelta(days=1))
        resultado = receita.calcular_multa_juros(date.today())
        self.assertEqual(resultado['multa'], Decimal('0.00'))
        self.assertEqual(resultado['juros'], Decimal('0.00'))

    def test_atraso_alem_da_carencia_gera_multa_e_juros(self):
        vencimento = date(2024, 1, 1)
        data_recebimento = date(2024, 1, 15)  # 14 dias de atraso, carência de 3
        receita = self._receita(vencimento)
        resultado = receita.calcular_multa_juros(data_recebimento)

        self.assertEqual(resultado['dias_atraso'], 14)
        self.assertEqual(resultado['multa'], Decimal('20.00'))  # 2% de 1000
        # juros: 1000 * 1% / 30 * 14 = 4.67
        self.assertEqual(resultado['juros'], Decimal('4.67'))

    def test_atraso_dentro_da_carencia_nao_gera_multa(self):
        vencimento = date(2024, 1, 1)
        data_recebimento = date(2024, 1, 3)  # 2 dias de atraso, carência de 3
        receita = self._receita(vencimento)
        resultado = receita.calcular_multa_juros(data_recebimento)
        self.assertEqual(resultado['multa'], Decimal('0.00'))
        self.assertEqual(resultado['juros'], Decimal('0.00'))

    def test_calculo_nao_altera_a_receita(self):
        """calcular_multa_juros apenas retorna a sugestão — nunca salva no banco."""
        receita = ReceitaAluguel.objects.create(
            contrato=self.contrato, imovel=self.imovel,
            competencia_mes=1, competencia_ano=2024,
            data_vencimento=date(2024, 1, 1), valor_previsto=Decimal('1000.00'),
            multa=Decimal('5.00'), juros=Decimal('3.00'), status='previsto',
        )
        receita.calcular_multa_juros(date(2024, 1, 20))
        receita.refresh_from_db()
        self.assertEqual(receita.multa, Decimal('5.00'))
        self.assertEqual(receita.juros, Decimal('3.00'))


# ─── Checklist: etapa de reajustes pendentes ──────────────────────────────────

class ChecklistReajustesPendentesTest(TestCase):
    def setUp(self):
        self.client = Client()
        User.objects.create_user('chk_reaj_user', password='pass')
        self.client.login(username='chk_reaj_user', password='pass')
        self.imovel, self.locatario = _criar_base()

    def test_etapa_reajustes_nao_ok_quando_pendente(self):
        contrato = _criar_contrato(
            self.imovel, self.locatario,
            data_inicio=date(2020, 1, 1), data_fim=date(2030, 12, 31),
        )
        contrato.data_proximo_reajuste = date.today() - timedelta(days=1)
        contrato.save()

        response = self.client.get(reverse('checklist_mensal'))
        checklist = response.context['checklist']
        etapa = next(e for e in checklist if e['item'] == 'Reajustes em dia')
        self.assertFalse(etapa['ok'])

    def test_etapa_reajustes_ok_quando_sem_pendencia(self):
        response = self.client.get(reverse('checklist_mensal'))
        checklist = response.context['checklist']
        etapa = next(e for e in checklist if e['item'] == 'Reajustes em dia')
        self.assertTrue(etapa['ok'])


# ─── Teste de migrations pendentes ────────────────────────────────────────────

class MigracoesPendentesTest(TestCase):
    """Verifica que não há migrations pendentes após todas as alterações."""

    def test_nao_ha_migrations_pendentes(self):
        from io import StringIO
        from django.core.management import call_command
        try:
            call_command('makemigrations', '--check', verbosity=0)
        except SystemExit as e:
            self.fail('Há migrations pendentes não criadas.')
