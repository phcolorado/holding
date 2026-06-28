import os
import zipfile
from datetime import datetime

from django.conf import settings
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Cria backup ZIP do banco de dados SQLite e dos arquivos de mídia'

    def add_arguments(self, parser):
        parser.add_argument(
            '--destino',
            type=str,
            default='',
            help='Pasta de destino do backup (padrão: backups/ na raiz do projeto)',
        )
        parser.add_argument(
            '--manter',
            type=int,
            default=0,
            help='Manter apenas os N backups mais recentes (0 = manter todos)',
        )

    def handle(self, *args, **options):
        destino = options['destino'] or os.path.join(settings.BASE_DIR, 'backups')
        manter = options['manter']

        os.makedirs(destino, exist_ok=True)

        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        zip_path = os.path.join(destino, f'backup_{timestamp}.zip')

        db_cfg = settings.DATABASES.get('default', {})
        db_name = db_cfg.get('NAME', '')
        media_root = getattr(settings, 'MEDIA_ROOT', '')

        media_count = 0
        db_ok = db_cfg.get('ENGINE', '').endswith('sqlite3') and db_name and os.path.exists(db_name)

        with zipfile.ZipFile(zip_path, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
            if db_ok:
                zf.write(db_name, 'db.sqlite3')

            if media_root and os.path.isdir(media_root):
                for dirpath, _, filenames in os.walk(media_root):
                    for filename in filenames:
                        filepath = os.path.join(dirpath, filename)
                        arcname = os.path.join('media', os.path.relpath(filepath, media_root))
                        zf.write(filepath, arcname)
                        media_count += 1

            manifest_lines = [
                f'Data/hora: {datetime.now().isoformat()}',
                f'Banco de dados: {db_name if db_ok else "não encontrado"}',
                f'Arquivos de mídia: {media_count}',
                f'Projeto: Gestão Patrimonial Familiar',
                '',
                'IMPORTANTE: Teste a restauração deste backup periodicamente.',
            ]
            zf.writestr('manifest.txt', '\n'.join(manifest_lines))

        if not db_ok:
            self.stdout.write(self.style.WARNING('Banco SQLite não encontrado — omitido do backup.'))
        if media_count == 0:
            self.stdout.write(self.style.WARNING('Nenhum arquivo de mídia encontrado — omitido do backup.'))

        self.stdout.write(self.style.SUCCESS(f'Backup criado: {zip_path}'))

        if manter:
            zips = sorted(
                f for f in os.listdir(destino)
                if f.startswith('backup_') and f.endswith('.zip')
            )
            while len(zips) > manter:
                antigo = os.path.join(destino, zips.pop(0))
                os.remove(antigo)
                self.stdout.write(f'Backup antigo removido: {antigo}')
