from django.urls import path
from . import views

urlpatterns = [
    path('imoveis/', views.imovel_list, name='imovel_list'),
    path('imoveis/<int:pk>/', views.imovel_detail, name='imovel_detail'),
    path('pessoas/', views.pessoa_list, name='pessoa_list'),
    path('contratos/', views.contrato_list, name='contrato_list'),
]
