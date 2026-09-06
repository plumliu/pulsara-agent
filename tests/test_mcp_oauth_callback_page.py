from pulsara_agent.conversation_kernel.mcp.oauth_callback_page import (
    oauth_callback_response,
)


def test_callback_page_is_self_contained_and_escapes_text():
    response = oauth_callback_response("<title>", '<script>"test"</script>', status=400)
    assert response.status == 400
    assert response.content_type == "text/html"
    assert response.charset == "utf-8"
    assert '<html lang="zh-CN">' in response.text
    assert '<meta name="viewport"' in response.text
    assert "&lt;title&gt;" in response.text
    assert "&lt;script&gt;" in response.text
    assert "<script" not in response.text
    assert " src=" not in response.text
    assert " href=" not in response.text
    assert 'aria-labelledby="callback-title"' in response.text
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Referrer-Policy"] == "no-referrer"
