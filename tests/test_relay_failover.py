import pytest
from relay import clean_upstream_error_text

def test_clean_upstream_error_text_waf_html():
    waf_html = "<!DOCTYPE html><html><head><title>Blocked</title></head><body>WAF challenge</body></html>"
    cleaned = clean_upstream_error_text(waf_html, 403)
    assert "Upstream WAF/Security Block" in cleaned
    assert "HTTP 403: Blocked" in cleaned
    assert "<html>" not in cleaned

def test_clean_upstream_error_text_normal():
    err = "Invalid API Key provided"
    cleaned = clean_upstream_error_text(err, 401)
    assert cleaned == "Invalid API Key provided"
