import os
import shutil
from datetime import datetime

from django.conf import settings
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Cria backup local do banco de dados SQLite e dos arquivos de mídia'

    def handle(self, *args, **options):
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        backup_dir = os.path.join(settings.BASE_DIR, 'backups', timestamp)
        os.makedirs(backup_dir, exist_ok=True)

        db_cfg = settings.DATABASES.get('default', {})
        db_name = db_cfg.get('NAME', '')
        if db_cfg.get('ENGINE', '').endswith('sqlite3') and db_name and os.path.exists(db_name):
            dest = os.path.join(backup_dir, 'db.sqlite3')
            shutil.copy2(db_name, dest)
            self.stdout.write(self.style.SUCCESS(f'Banco copiado: {dest}'))
        else:
            self.stdout.write(self.style.WARNING('Banco SQLite não encontrado — pulando.'))

        media_root = getattr(settings, 'MEDIA_ROOT', '')
        if media_root and os.path.isdir(media_root):
            dest = os.path.join(backup_dir, 'media')
            shutil.copytree(media_root, dest)
            self.stdout.write(self.style.SUCCESS(f'Mídia copiada: {dest}'))
        else:
            self.stdout.write(self.style.WARNING('Diretório de mídia não encontrado — pulando.'))

        self.stdout.write(self.style.SUCCESS(f'Backup concluído em: {backup_dir}'))
