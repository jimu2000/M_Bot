"""v2.14 regression checks that do not require cloud SDK/network access."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_iam_reset_button_is_not_filtered_by_current_user_or_capability():
    js = (ROOT / "web/static/js/accounts.js").read_text(encoding="utf-8")
    assert 'var pwdBtn = el("button", "重置密码", "btn btn-sm btn-primary");' in js
    assert "var canReset" not in js
    assert "currentApiUserId" not in js


def test_iam_reset_route_delegates_permission_to_oci():
    source = (ROOT / "web/routes/iam.py").read_text(encoding="utf-8")
    start = source.index('def reset_user_password(')
    end = source.find('\n@router.', start + 1)
    body = source[start:end if end > 0 else None]
    assert "create_or_reset_ui_password(cli, user_id)" in body
    assert "can-use-console-password" not in body
    assert "_is_current_api_user(body.profile, user_id)" not in body


def test_new_telegram_tools_are_registered_in_menu_and_dispatch():
    menu = (ROOT / "tgbot/keyboards.py").read_text(encoding="utf-8")
    handlers = (ROOT / "tgbot/handlers_extra.py").read_text(encoding="utf-8")
    assert '("oracleping", "45. Oracle 全球区域测速")' in menu
    assert '("cfip", "46. Cloudflare 优选 IP")' in menu
    assert '"oracleping": handle_oracleping' in handlers
    assert '"cfip": handle_cfip' in handlers


def test_mobile_ui_polish_rules_exist():
    css = (ROOT / "web/static/css/base.css").read_text(encoding="utf-8")
    assert "@media (max-width: 560px)" in css
    assert "prefers-reduced-motion" in css
    assert ".iam-create-row" in css
