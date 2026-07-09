from django.contrib.auth.models import Group, Permission
from django.contrib.contenttypes.models import ContentType
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Cria grupos básicos de permissão: administrador, familiar_edicao, familiar_leitura'

    def handle(self, *args, **options):
        todos_perms = list(Permission.objects.all())
        app_labels = {'patrimonio', 'financeiro', 'documentos', 'conciliacao'}
        perms_do_sistema = [
            p for p in todos_perms
            if p.content_type.app_label in app_labels
        ]
        perms_leitura = [p for p in perms_do_sistema if p.codename.startswith('view_')]
        perms_edicao = [p for p in perms_do_sistema if p.codename.startswith(('view_', 'add_', 'change_'))]

        administrador, criado = Group.objects.get_or_create(name='administrador')
        administrador.permissions.set(todos_perms)
        self.stdout.write(
            self.style.SUCCESS(f"{'Criado' if criado else 'Atualizado'}: administrador ({len(todos_perms)} permissões)")
        )

        familiar_edicao, criado = Group.objects.get_or_create(name='familiar_edicao')
        familiar_edicao.permissions.set(perms_edicao)
        self.stdout.write(
            self.style.SUCCESS(f"{'Criado' if criado else 'Atualizado'}: familiar_edicao ({len(perms_edicao)} permissões)")
        )

        familiar_leitura, criado = Group.objects.get_or_create(name='familiar_leitura')
        familiar_leitura.permissions.set(perms_leitura)
        self.stdout.write(
            self.style.SUCCESS(f"{'Criado' if criado else 'Atualizado'}: familiar_leitura ({len(perms_leitura)} permissões)")
        )

        self.stdout.write(self.style.SUCCESS('Grupos criados com sucesso.'))
