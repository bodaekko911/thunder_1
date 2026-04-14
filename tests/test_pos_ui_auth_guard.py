from app.routers.pos import pos_ui


def test_pos_ui_uses_cookie_auth_guard_without_stale_token_checks() -> None:
    html = pos_ui()

    assert "_hasAuthCookie()" in html
    assert "if(!token)" not in html
