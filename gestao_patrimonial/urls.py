from django.contrib import admin
from django.urls import path, include
from django.contrib.auth import views as auth_views

# NÃO servimos MEDIA_URL como rota pública (nem via django.conf.urls.static.
# static(), que já é um no-op quando DEBUG=False, mas em DEBUG=True serviria
# TODO o /media/ sem autenticação). Documentos e extratos bancários são
# potencialmente sensíveis — o único jeito de baixá-los é pelas views
# autenticadas e permissionadas (documento_download, extrato_download), que
# leem o arquivo direto do storage (não dependem desta rota) e funcionam
# igualmente em desenvolvimento e produção. Ver seção "Proteção de arquivos
# privados" do README.
urlpatterns = [
    path('admin/', admin.site.urls),
    path('login/', auth_views.LoginView.as_view(template_name='registration/login.html'), name='login'),
    path('logout/', auth_views.LogoutView.as_view(), name='logout'),
    path('', include('core.urls')),
    path('patrimonio/', include('patrimonio.urls')),
    path('financeiro/', include('financeiro.urls')),
    path('financeiro/conciliacao/', include('conciliacao.urls')),
    path('documentos/', include('documentos.urls')),
]
