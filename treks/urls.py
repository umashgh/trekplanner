from django.urls import path

from . import views

app_name = "treks"

urlpatterns = [
    path("", views.explore_view, name="explore"),
    path("calendar/", views.calendar_view, name="calendar"),
    path("refresh/", views.refresh_view, name="refresh"),
    path("runs/", views.runs_view, name="runs"),
    path("batches/<int:pk>/plan/", views.toggle_plan, name="toggle_plan"),
    path("metrics/", views.metrics_view, name="metrics"),
    path("treks/", views.trek_form_list, name="trek_form_list"),
    path("treks/new/", views.trek_form_new, name="trek_form_new"),
    path("treks/<int:pk>/edit/", views.trek_form_edit, name="trek_form_edit"),
]
