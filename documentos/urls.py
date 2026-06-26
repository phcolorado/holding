from django.urls import path
from . import views

urlpatterns = [
    path('', views.documento_list, name='documento_list'),
]
