def test_pos_ui_redirects_unauthenticated_to_login(client) -> None:
    response = client.get(
        "/pos",
        follow_redirects=False,
        headers={"accept": "text/html"},
    )

    assert response.status_code in (302, 307)
    assert "/?next=" in response.headers["location"]
    assert "reason=expired" in response.headers["location"]
