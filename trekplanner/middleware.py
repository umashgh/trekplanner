import re

from django.conf import settings


class CSPMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if hasattr(settings, "CSP_HEADER"):
            response["Content-Security-Policy"] = settings.CSP_HEADER
        return response


class PageViewMiddleware:
    SKIP_PATTERN = re.compile(r"^/(admin|static|__debug__|metrics)/")

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if self.SKIP_PATTERN.match(request.path):
            return response

        from treks.models import PageView

        ip = request.META.get("HTTP_CF_CONNECTING_IP") or request.META.get("REMOTE_ADDR")
        PageView.objects.create(
            path=request.path[:500],
            method=request.method,
            status_code=response.status_code,
            user_agent=request.META.get("HTTP_USER_AGENT", "")[:1000],
            referrer=request.META.get("HTTP_REFERER", "")[:1000],
            ip=ip,
            session_key=request.session.session_key or "",
        )
        return response
