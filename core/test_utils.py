"""Utilidades compartilhadas pelos testes dos apps."""

APPS_DO_SISTEMA = ('patrimonio', 'financeiro', 'documentos', 'conciliacao')


def com_leitura(user):
    """
    Concede ao usuário todas as permissões de visualização dos apps do sistema
    (equivalente ao grupo "socio"). Usada nos testes de views, já que toda
    tela de leitura passou a exigir a permissão view_* da sua área.
    """
    from django.contrib.auth.models import Permission

    user.user_permissions.add(*Permission.objects.filter(
        codename__startswith='view_',
        content_type__app_label__in=APPS_DO_SISTEMA,
    ))
    return user
