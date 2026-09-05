# Security audit patch (2026-09-05)

## Apply `app/main.py` changes

From the repository root:

```bash
patch -p1 < patches/security-audit-20260905-main.patch
```

This removes the UTF-8 BOM, adds timing-safe secret compares, replaces VK `SETUP_API_KEY` in OAuth URLs with short-lived bind tokens, and requires a real `TELEGRAM_SECRET_TOKEN` for webhooks.

## Already applied on this branch (no patch needed)

- `proxy.php` — block `/api/../` traversal
- `app/services/vk_bind.py` — short-lived VK bind secrets
- `app/services/miniapp.py` — require `auth_date`
- `app/services/agent_pairing.py` — timing-safe global agent key
- `.env.example` — webhook secret documentation
- `tests/test_vk_bind.py`, `tests/test_php_agent_proxy.py`
