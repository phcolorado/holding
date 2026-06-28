import os
from calendar import monthrange
from datetime import date, timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone

from patrimonio.models import Imovel, Pessoa, Contrato
from financeiro.models import ReceitaAluguel, Despesa, receitas_inadimplentes_qs
from financeiro.services import gerar_receitas_para_contrato, gerar_receitas_mes


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


def _criar_contrato(imovel, locatario, data_inicio, data_fim, status='ativo', dia_vencimento=10):
    return Contrato.objects.create(
        imovel=imovel,
        locatario=locatario,
        data_inicio=data_inicio,
        data_fim=data_fim,
        valor_aluguel=Decimal('2000.00'),
        dia_vencimento=dia_vencimento,
        status=status,
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
        self.assertIn('Inadimplência', wb.sheetnames)
        ws = wb['Inadimplência']
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
