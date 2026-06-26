from django.urls import path
from . import views

urlpatterns = [
    path('receitas/', views.receitas_list, name='receitas_list'),
    path('despesas/', views.despesas_list, name='despesas_list'),
    path('relatorios/', views.relatorios, name='relatorios'),
    path('export/imoveis/<str:formato>/', views.export_imoveis, name='export_imoveis'),
    path('export/contratos/<str:formato>/', views.export_contratos, name='export_contratos'),
    path('export/receitas/<str:formato>/', views.export_receitas, name='export_receitas'),
    path('export/despesas/<str:formato>/', views.export_despesas, name='export_despesas'),
    path('export/inadimplencia/<str:formato>/', views.export_inadimplencia, name='export_inadimplencia'),
    path('export/relatorio-mensal/<str:formato>/', views.export_relatorio_mensal, name='export_relatorio_mensal'),
]
