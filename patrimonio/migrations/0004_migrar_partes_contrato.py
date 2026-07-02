from django.db import migrations


def migrar_partes(apps, schema_editor):
    """Cria ContratoParte a partir dos campos legados locatario/fiador/imobiliaria."""
    Contrato = apps.get_model('patrimonio', 'Contrato')
    ContratoParte = apps.get_model('patrimonio', 'ContratoParte')

    for contrato in Contrato.objects.all():
        if contrato.locatario_id:
            ContratoParte.objects.get_or_create(
                contrato=contrato, pessoa_id=contrato.locatario_id, papel='locatario',
                defaults={'principal': True},
            )
        if contrato.fiador_id:
            ContratoParte.objects.get_or_create(
                contrato=contrato, pessoa_id=contrato.fiador_id, papel='fiador',
                defaults={'principal': True},
            )
        if contrato.imobiliaria_id:
            ContratoParte.objects.get_or_create(
                contrato=contrato, pessoa_id=contrato.imobiliaria_id, papel='imobiliaria',
                defaults={'principal': True},
            )


def reverter_partes(apps, schema_editor):
    ContratoParte = apps.get_model('patrimonio', 'ContratoParte')
    ContratoParte.objects.filter(papel__in=['locatario', 'fiador', 'imobiliaria']).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("patrimonio", "0003_contrato_data_encerramento_real_and_more"),
    ]

    operations = [
        migrations.RunPython(migrar_partes, reverter_partes),
    ]
