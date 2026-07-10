from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand

APP_LABELS = {'patrimonio', 'financeiro', 'documentos', 'conciliacao'}


class Command(BaseCommand):
    help = (
        'Cria/atualiza os grupos de permissão: administrador, familiar_edicao, '
        'familiar_leitura, socio, advogado e contador.'
    )

    def handle(self, *args, **options):
        todos_perms = list(Permission.objects.all())
        perms_do_sistema = [
            p for p in todos_perms
            if p.content_type.app_label in APP_LABELS
        ]
        perms_leitura = [p for p in perms_do_sistema if p.codename.startswith('view_')]
        perms_edicao = [p for p in perms_do_sistema if p.codename.startswith(('view_', 'add_', 'change_'))]

        def leitura_de(*apps):
            return [p for p in perms_leitura if p.content_type.app_label in apps]

        grupos = {
            # Perfis de gestão da família
            'administrador': todos_perms,
            'familiar_edicao': perms_edicao,
            'familiar_leitura': perms_leitura,
            # Perfis de acompanhamento externo
            'socio': perms_leitura,  # leitura geral: dashboards, painéis, financeiro, contratos, documentos
            'advogado': leitura_de('patrimonio', 'documentos'),  # contratos, imóveis, pessoas e documentos
            'contador': (
                leitura_de('financeiro', 'documentos', 'conciliacao')
                + leitura_de('patrimonio')  # contexto p/ exportações de imóveis/contratos
            ),
        }

        for nome, perms in grupos.items():
            grupo, criado = Group.objects.get_or_create(name=nome)
            grupo.permissions.set(perms)
            self.stdout.write(
                self.style.SUCCESS(f"{'Criado' if criado else 'Atualizado'}: {nome} ({len(perms)} permissões)")
            )

        self.stdout.write(self.style.SUCCESS(
            'Grupos criados com sucesso. Vincule cada usuário ao grupo no Admin → Usuários.'
        ))
