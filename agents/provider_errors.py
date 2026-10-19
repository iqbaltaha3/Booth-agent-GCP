"""Safe typed provider failures shared by transport, specialists and the API."""
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime


def retry_after_seconds(value):
    if not value:
        return None
    try:
        return max(0, float(value))
    except (ValueError, TypeError):
        try:
            return max(0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return None


class ProviderReply(str):
    """String-compatible transport result with safe HTTP metadata during migration."""
    def __new__(cls, text, status_code=None, retry_after=None):
        value = super().__new__(cls, text)
        value.status_code, value.retry_after = status_code, retry_after
        return value


class ModelFailure(RuntimeError):
    def __init__(self, kind, status_code=None, retry_after=None):
        self.kind, self.status_code, self.retry_after = kind, status_code, retry_after
        super().__init__(f"Model provider failure: {kind}")

    @classmethod
    def from_response(cls, response):
        match = re.search(r"HTTP (\d{3})", str(response))
        return cls(failure_kind(response), int(match[1]) if match else None)


def failure_kind(response):
    text = str(response)
    if any(f"HTTP {code}" in text for code in (400, 401, 403, 404)):
        return "provider_request_invalid"
    if "429" in text:
        return "provider_capacity"
    if "deadline" in text:
        return "deadline_exceeded"
    return "provider_unavailable"


def user_error(kind, hindi=False):
    if kind == "provider_request_invalid":
        return "AI सेवा के कॉन्फ़िगरेशन के कारण अनुरोध पूरा नहीं हुआ। कृपया अनुरोध ID साझा करें।" if hindi else "An AI service configuration error prevented this request. Please share the request ID for support."
    if kind == "provider_capacity":
        return "AI सेवा अभी व्यस्त है। कृपया थोड़ी देर में दोबारा प्रयास करें।" if hindi else "The AI service is temporarily busy. Please retry shortly."
    if kind == "deadline_exceeded":
        return "अनुरोध का समय समाप्त हो गया। कृपया दोबारा प्रयास करें।" if hindi else "The request reached its time limit before a verified answer was ready. Please retry."
    return "AI सेवा अभी उपलब्ध नहीं है। कृपया दोबारा प्रयास करें।" if hindi else "The AI service is temporarily unavailable. Please retry shortly."
