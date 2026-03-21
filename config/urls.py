from functools import partial

from django.contrib import admin
from django.contrib.staticfiles.views import serve
from django.urls import include, path, re_path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", include("audiobook.urls")),
    re_path(r"^static/(?P<path>.*)$", partial(serve, insecure=True)),
]
