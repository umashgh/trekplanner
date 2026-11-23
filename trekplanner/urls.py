from django.contrib import admin
from django.urls import include, path

from treks.views import robots_txt, sitemap_xml

urlpatterns = [
    path("robots.txt", robots_txt),
    path("sitemap.xml", sitemap_xml),
    path("admin/", admin.site.urls),
    path("", include("treks.urls")),
]
