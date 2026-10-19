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
    assert 'src="data:image/png;base64,' in response.text
    assert 'href="pulsara://oauth/return"' in response.text
    assert 'aria-labelledby="callback-title"' in response.text
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Referrer-Policy"] == "no-referrer"


def test_callback_page_uses_desktop_logo_and_only_result_copy():
    response = oauth_callback_response("授权成功", "", outcome="success")
    assert "授权成功" in response.text
    assert "返回 Pulsara" in response.text
    assert "本机连接 · 浏览器授权" not in response.text
    assert "可以关闭此标签页" not in response.text
    assert "<svg" not in response.text
