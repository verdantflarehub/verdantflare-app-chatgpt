"""Subscription registration and gateway observations are separate facts."""
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import os
from urllib.parse import urlsplit

from .store import now

DEFAULT_ORIGIN = ''


def origin(value):
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError('invalid_api_url')
    parsed = urlsplit(value)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path.rstrip('/') not in {'', '/v1'}):
        raise ValueError('invalid_api_url')
    port = parsed.port
    canonical = 'https://' + parsed.hostname.lower() + (':' + str(port) if port not in {None, 443} else '')
    allowed = set(filter(None, os.environ.get('CHATGPT_ALLOWED_API_ORIGINS', '').split(',')))
    if canonical not in allowed:
        raise ValueError('api_origin_not_allowed')
    return canonical + '/v1'


def number(value, maximum='1e15'):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)) or len(str(value)) > 32:
        raise ValueError('invalid_amount')
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < 0 or amount > Decimal(maximum) or amount.as_tuple().exponent < -8:
            raise ValueError('invalid_amount')
        return format(amount, 'f')
    except InvalidOperation:
        raise ValueError('invalid_amount') from None


def timestamp(value):
    if not isinstance(value, str) or datetime.fromisoformat(value).tzinfo is None:
        raise ValueError('observation_timezone_required')
    return value


def registration(value):
    if not isinstance(value, dict) or set(value) - {'membership_tier', 'membership_expiry',
            'used_5h_percent', 'reset_5h_at', 'used_7d_percent', 'reset_7d_at',
            'observed_at', 'note', 'budget_limit'}:
        raise ValueError('invalid_registration')
    result = dict(value)
    for field in ('membership_tier', 'note'):
        if field in result and (not isinstance(result[field], str) or len(result[field]) > 2000):
            raise ValueError('invalid_registration')
    expiry = result.get('membership_expiry')
    if expiry is not None and not isinstance(expiry, str):
        raise ValueError('invalid_membership_expiry')
    if expiry:
        if len(expiry) == 10:
            date.fromisoformat(expiry)
        else:
            timestamp(expiry)
    for field in ('used_5h_percent', 'used_7d_percent'):
        if result.get(field) is not None:
            result[field] = number(result[field], '100')
    for field in ('reset_5h_at', 'reset_7d_at', 'observed_at'):
        if result.get(field) is not None and not isinstance(result[field], str):
            raise ValueError('invalid_timestamp')
        if result.get(field):
            timestamp(result[field])
    if result.get('budget_limit') is not None:
        result['budget_limit'] = number(result['budget_limit'])
    if not result.get('observed_at') and any(result.get(key) is not None for key in ('used_5h_percent', 'used_7d_percent')):
        result['observed_at'] = now()
    return result


def usage(value):
    if not isinstance(value, dict):
        raise ValueError('invalid_usage')
    result = {'source': 'gateway_usage', 'observed_at': now()}
    for field in ('balance', 'remaining'):
        if value.get(field) is not None:
            result[field] = number(value[field])
    for field in ('planName', 'mode', 'unit'):
        if value.get(field) is not None:
            if not isinstance(value[field], str) or not 1 <= len(value[field]) <= 120:
                raise ValueError('invalid_usage')
            result[field] = value[field]
    if not any(field in result for field in ('balance', 'remaining')):
        raise ValueError('invalid_usage')
    return result
