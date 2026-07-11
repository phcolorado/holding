from django.urls import path
from . import views

urlpatterns = [
    path('', views.extrato_list, name='extrato_list'),
    path('<int:pk>/', views.conciliar_extrato, name='conciliar_extrato'),
    path('<int:pk>/download/', views.extrato_download, name='extrato_download'),
]
