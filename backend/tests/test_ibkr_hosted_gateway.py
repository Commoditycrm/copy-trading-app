"""Hosted IBKR gateway helpers: proxy rewriting and login tokens (pure)."""
from __future__ import annotations

import uuid

from app.services import ibkr_hosted_gateway as H

P = "/api/ibkr-gateway/3"
UP = "https://ibkr-gw-3:5000"


def test_rewrite_location_root_relative_and_absolute_upstream():
    assert H.rewrite_location("/sso/Login?forwardTo=22", P, UP) == f"{P}/sso/Login?forwardTo=22"
    assert H.rewrite_location(f"{UP}/sso/Dispatcher", P, UP) == f"{P}/sso/Dispatcher"
    assert H.rewrite_location(f"{P}/sso/Login", P, UP) == f"{P}/sso/Login"      # idempotent
    assert H.rewrite_location("https://www.interactivebrokers.com/x", P, UP) == "https://www.interactivebrokers.com/x"
    assert H.rewrite_location("//cdn.example.com/a.js", P, UP) == "//cdn.example.com/a.js"


def test_rewrite_set_cookie_scopes_path_and_drops_domain():
    c = "JSESSIONID=abc; Path=/sso; HttpOnly;Secure;SameSite=None"
    assert H.rewrite_set_cookie(c, P) == f"JSESSIONID=abc; Path={P}/sso; HttpOnly;Secure;SameSite=None"
    c = 'partnerID=""; Domain=.ibkr.com; Expires=Thu, 01 Jan 1970 00:00:10 GMT; Path=/;Secure;SameSite=None'
    out = H.rewrite_set_cookie(c, P)
    assert "Domain=" not in out and f"Path={P}/" in out
    assert H.rewrite_set_cookie("x-sess-uuid=1; secure; HttpOnly", P).endswith(f"; Path={P}/")


def test_rewrite_body_prefixes_root_relative_urls_only():
    html = ('<form action="/sso/Login"><a href="/sso/Dispatcher">x</a>'
            '<script src="//cdn/x.js"></script><img src="https://ibkr.com/i.png">'
            '<script>fetch("/v1/api/one"); go(\'/sso/two\');</script>'
            '<style>a{background:url(/img/b.png)}</style>')
    out = H.rewrite_body(html, P)
    assert f'action="{P}/sso/Login"' in out
    assert f'href="{P}/sso/Dispatcher"' in out
    assert 'src="//cdn/x.js"' in out and 'src="https://ibkr.com/i.png"' in out
    assert f'fetch("{P}/v1/api/one")' in out and f"go('{P}/sso/two')" in out
    assert f"url({P}/img/b.png)" in out
    assert H.rewrite_body(out, P) == out  # idempotent


def test_login_token_is_bound_to_user_and_slot():
    uid = uuid.uuid4()
    tok = H.login_token(uid, 3)
    assert H.verify_login_token(tok, 3) == uid
    assert H.verify_login_token(tok, 4) is None
    assert H.verify_login_token("garbage", 3) is None
    assert H.verify_login_token(None, 3) is None


def test_is_rewritable():
    assert H.is_rewritable("text/html; charset=utf-8")
    assert H.is_rewritable("application/javascript")
    assert not H.is_rewritable("image/png")
    assert not H.is_rewritable(None)


def test_rewrite_body_fixes_ibkr_login_script_base_url():
    js = 'let n=document.location.protocol+"//"+document.location.host+"/"+document.location.pathname.split("/")[1]+"/";'
    out = H.rewrite_body(js, P)
    assert 'split("/")[1]' not in out
    assert 'replace(/sso\\/.*$/,"sso/")' in out
    # Evaluate the replacement the way a browser would, for a proxied login URL.
    import re as _re
    pathname = "/api/ibkr-gateway/3/sso/Login"
    base = _re.sub(r"sso/.*$", "sso/", _re.sub(r"^/+", "", pathname))
    assert base == "api/ibkr-gateway/3/sso/"
