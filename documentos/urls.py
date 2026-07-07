from django.urls import path
from . import views

urlpatterns = [
    path('', views.documento_list, name='documento_list'),
    path('<int:pk>/download/', views.documento_download, name='documento_download'),
]
