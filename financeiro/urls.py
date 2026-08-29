from django.urls import path
from . import views

urlpatterns = [
    path('receitas/', views.receitas_list, name='receitas_list'),
    path('despesas/', views.despesas_list, name='despesas_list'),
    path('relatorios/', views.relatorios, name='relatorios'),
    path('paineis/', views.paineis, name='paineis'),
    path('baixa-receitas/', views.baixa_receitas_mes_view, name='baixa_receitas_mes'),
    path('gerar-receitas/', views.gerar_receitas_mes_view, name='gerar_receitas_mes'),
    path('checklist-mensal/', views.checklist_mensal_view, name='checklist_mensal'),
    path('export/imoveis/<str:formato>/', views.export_imoveis, name='export_imoveis'),
    path('export/contratos/<str:formato>/', views.export_contratos, name='export_contratos'),
    path('export/locatarios/<str:formato>/', views.export_locatarios, name='export_locatarios'),
    path('export/dimob/xlsx/', views.export_dimob, name='export_dimob'),
    path('export/receitas/<str:formato>/', views.export_receitas, name='export_receitas'),
    path('export/despesas/<str:formato>/', views.export_despesas, name='export_despesas'),
    path('export/inadimplencia/<str:formato>/', views.export_inadimplencia, name='export_inadimplencia'),
    path('export/relatorio-mensal/<str:formato>/', views.export_relatorio_mensal, name='export_relatorio_mensal'),
    path('export/relatorio-contabilidade/xlsx/', views.export_relatorio_contabilidade, name='export_relatorio_contabilidade'),
]
