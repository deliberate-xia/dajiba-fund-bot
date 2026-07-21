"""
PushPlus WeChat push integration.
Tries multiple tokens until one succeeds.
"""
import time

import requests

PUSHPLUS_API_URL = "https://www.pushplus.plus/send"
# Match SAIER1234/investment-bot: no custom headers, let requests handle it
HEADERS = {}


def _try_send(payload: dict) -> bool:
    """Single attempt to POST."""
    try:
        resp = requests.post(PUSHPLUS_API_URL, json=payload, headers=HEADERS, timeout=15)
        data = resp.json()
        code = data.get("code", -1)
        if code == 200:
            return True
        print(f"[PushPlus] API error: {data.get('msg', 'unknown')}")
        return False
    except requests.RequestException as e:
        print(f"[PushPlus] Network error: {e}")
        return False


def send_with_tokens(
    tokens: list[str],
    title: str,
    content: str,
    template: str = "markdown",
    topic_token: str = "",
    max_retries: int = 3,
) -> bool:
    """
    Send via PushPlus, trying each token in order.
    Returns True if any token succeeds.
    """
    for token_idx, token in enumerate(tokens):
        if not token:
            continue

        payload = {
            "token": token,
            "title": title,
            "content": content,
            "template": template,
        }
        if topic_token and token_idx == 0:
            payload["topic"] = topic_token

        label = f"token[{token_idx}]"
        print(f"[PushPlus] Trying {label}: {title}")

        for attempt in range(1, max_retries + 1):
            if _try_send(payload):
                print(f"[PushPlus] OK via {label}")
                return True
            if attempt < max_retries:
                time.sleep(2 ** attempt)

        print(f"[PushPlus] FAILED via {label}")

    return False


def send_report(
    user_token: str,
    title: str,
    content: str,
    topic_token: str = "",
    template: str = "markdown",
) -> bool:
    """Send a Markdown report. Tries user_token first, then topic_token as independent fallback."""
    tokens = [t for t in [user_token, topic_token] if t]
    return send_with_tokens(tokens, title, content, template=template)


def send_alert(
    user_token: str,
    title: str,
    content: str,
    topic_token: str = "",
) -> bool:
    """Send a short alert (plain text)."""
    return send_report(user_token, title, content, topic_token, template="txt")
