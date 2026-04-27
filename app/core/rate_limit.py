from slowapi import Limiter

from app.core.middleware import get_trusted_client_ip


def get_rate_limit_key(request):
    return get_trusted_client_ip(request)


limiter = Limiter(
    key_func=get_rate_limit_key,
    default_limits=[],
)
