"""
Comando opcional para popular o banco com dados fictícios de demonstração.
Uso: python manage.py popular_banco
"""
from datetime import date
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.contrib.auth.models import User

from patrimonio.models import Imovel, Pessoa, Contrato, Manutencao
from financeiro.models import ReceitaAluguel, Despesa


class Command(BaseCommand):
    help = 'Popula o banco com dados de demonstração'

    def handle(self, *args, **options):
        self.stdout.write('Criando dados de demonstração...')

        # Imóveis
        i1, _ = Imovel.objects.get_or_create(
            nome='Apartamento Centro',
            defaults=dict(
                endereco='Rua das Flores, 100, Apto 301',
                cidade='São Paulo', estado='SP', cep='01310-100',
                matricula='12345', inscricao_iptu='001.002.003-4',
                proprietario='Família Oliveira',
                status='alugado',
                data_aquisicao=date(2015, 3, 10),
                valor_aquisicao=Decimal('380000.00'),
                valor_estimado=Decimal('620000.00'),
            )
        )
        i2, _ = Imovel.objects.get_or_create(
            nome='Casa Jardins',
            defaults=dict(
                endereco='Alameda Santos, 450',
                cidade='São Paulo', estado='SP', cep='01418-000',
                matricula='67890', inscricao_iptu='005.006.007-8',
                proprietario='Família Oliveira',
                status='alugado',
                data_aquisicao=date(2010, 7, 20),
                valor_aquisicao=Decimal('850000.00'),
                valor_estimado=Decimal('1400000.00'),
            )
        )
        i3, _ = Imovel.objects.get_or_create(
            nome='Sala Comercial Vila Olímpia',
            defaults=dict(
                endereco='Av. Brigadeiro Faria Lima, 2000, Cj. 52',
                cidade='São Paulo', estado='SP', cep='04538-132',
                matricula='11111', inscricao_iptu='009.010.011-2',
                proprietario='Família Oliveira',
                status='vago',
                data_aquisicao=date(2018, 11, 5),
                valor_aquisicao=Decimal('500000.00'),
                valor_estimado=Decimal('680000.00'),
            )
        )
        self.stdout.write(f'  ✓ {Imovel.objects.count()} imóveis')

        # Pessoas
        p1, _ = Pessoa.objects.get_or_create(
            cpf_cnpj='123.456.789-00',
            defaults=dict(
                nome='João Carlos Mendes',
                tipo='locatario',
                email='joao.mendes@email.com',
                telefone='(11) 99999-1111',
                endereco='Rua das Flores, 100, Apto 301 — SP',
            )
        )
        p2, _ = Pessoa.objects.get_or_create(
            cpf_cnpj='987.654.321-00',
            defaults=dict(
                nome='Ana Paula Ferreira',
                tipo='locatario',
                email='ana.ferreira@email.com',
                telefone='(11) 99999-2222',
                endereco='Alameda Santos, 450 — SP',
            )
        )
        p3, _ = Pessoa.objects.get_or_create(
            cpf_cnpj='11.222.333/0001-44',
            defaults=dict(
                nome='Imobiliária Central Ltda',
                tipo='imobiliaria',
                email='contato@imobiliariacentral.com.br',
                telefone='(11) 3333-4444',
            )
        )
        self.stdout.write(f'  ✓ {Pessoa.objects.count()} pessoas')

        # Contratos
        c1, _ = Contrato.objects.get_or_create(
            imovel=i1, locatario=p1,
            defaults=dict(
                imobiliaria=p3,
                data_inicio=date(2023, 1, 1),
                data_fim=date(2025, 12, 31),
                valor_aluguel=Decimal('3500.00'),
                dia_vencimento=5,
                indice_reajuste='igpm',
                data_proximo_reajuste=date(2025, 1, 1),
                tipo_garantia='seguro_fianca',
                comissao_imobiliaria_percentual=Decimal('8.00'),
                status='ativo',
            )
        )
        c2, _ = Contrato.objects.get_or_create(
            imovel=i2, locatario=p2,
            defaults=dict(
                data_inicio=date(2022, 6, 1),
                data_fim=date(2024, 5, 31),
                valor_aluguel=Decimal('7200.00'),
                dia_vencimento=10,
                indice_reajuste='ipca',
                data_proximo_reajuste=date(2024, 6, 1),
                tipo_garantia='fiador',
                status='ativo',
            )
        )
        self.stdout.write(f'  ✓ {Contrato.objects.count()} contratos')

        # Receitas (últimos 3 meses para cada contrato)
        hoje = date.today()
        for contrato, imovel in [(c1, i1), (c2, i2)]:
            for delta in range(3):
                mes = (hoje.month - delta - 1) % 12 + 1
                ano = hoje.year if (hoje.month - delta - 1) >= 0 else hoje.year - 1
                ReceitaAluguel.objects.get_or_create(
                    contrato=contrato,
                    competencia_mes=mes,
                    competencia_ano=ano,
                    defaults=dict(
                        imovel=imovel,
                        data_vencimento=date(ano, mes, contrato.dia_vencimento),
                        valor_previsto=contrato.valor_aluguel,
                        valor_recebido=contrato.valor_aluguel if delta > 0 else None,
                        data_recebimento=date(ano, mes, contrato.dia_vencimento + 2) if delta > 0 else None,
                        status='recebido' if delta > 0 else 'previsto',
                    )
                )
        self.stdout.write(f'  ✓ {ReceitaAluguel.objects.count()} receitas')

        # Despesas
        despesas_demo = [
            dict(imovel=i1, categoria='iptu', descricao='IPTU 2024 — Apartamento Centro',
                 competencia_mes=1, competencia_ano=2024,
                 data_vencimento=date(2024, 2, 10), valor=Decimal('1800.00'), status='paga',
                 data_pagamento=date(2024, 2, 8)),
            dict(imovel=i2, categoria='condominio', descricao='Condomínio Jan/2024 — Casa Jardins',
                 competencia_mes=1, competencia_ano=2024,
                 data_vencimento=date(2024, 1, 20), valor=Decimal('950.00'), status='paga',
                 data_pagamento=date(2024, 1, 18)),
            dict(imovel=i3, categoria='iptu', descricao='IPTU 2024 — Sala Comercial',
                 competencia_mes=1, competencia_ano=2024,
                 data_vencimento=date(2024, 3, 15), valor=Decimal('2200.00'), status='prevista'),
        ]
        for d in despesas_demo:
            Despesa.objects.get_or_create(
                imovel=d['imovel'], descricao=d['descricao'],
                defaults={k: v for k, v in d.items() if k not in ('imovel', 'descricao')}
            )
        self.stdout.write(f'  ✓ {Despesa.objects.count()} despesas')

        # Manutenções
        Manutencao.objects.get_or_create(
            imovel=i1, descricao='Troca de torneira na cozinha',
            defaults=dict(
                categoria='hidraulica', data_solicitacao=date(2024, 1, 10),
                data_conclusao=date(2024, 1, 12), valor_estimado=Decimal('300.00'),
                valor_final=Decimal('280.00'), status='concluida',
            )
        )
        Manutencao.objects.get_or_create(
            imovel=i3, descricao='Pintura geral do escritório',
            defaults=dict(
                categoria='pintura', data_solicitacao=date(2024, 2, 1),
                valor_estimado=Decimal('4000.00'), status='orcamento_recebido',
            )
        )
        self.stdout.write(f'  ✓ {Manutencao.objects.count()} manutenções')

        # Superuser padrão (apenas se não existir)
        if not User.objects.filter(username='admin').exists():
            User.objects.create_superuser('admin', 'admin@holding.local', 'admin123')
            self.stdout.write('  ✓ Superuser criado: admin / admin123')

        self.stdout.write(self.style.SUCCESS('\nDados de demonstração criados com sucesso!'))
