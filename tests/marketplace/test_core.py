import pytest
from marketplace.services.ratings import RatingsService
from marketplace.services.badges import is_popular, is_top_rated, is_top_builder
from marketplace.services.sanitizer import is_username_reserved, sanitize_html, render_and_sanitize_markdown
from marketplace.web.seo import generate_plugin_jsonld

def test_sanitizer_reserved_usernames():
    assert is_username_reserved("admin") is True
    assert is_username_reserved("talos") is True
    assert is_username_reserved("official") is True
    assert is_username_reserved("alexdev") is False

def test_sanitizer_html_strips_scripts():
    raw = '<script>alert("hack")</script><p>Hello <b>World</b></p><iframe src="evil.com"></iframe>'
    clean = sanitize_html(raw)
    assert "<script>" not in clean
    assert "<iframe>" not in clean
    assert "<p>Hello <b>World</b></p>" in clean

def test_bayesian_scoring():
    svc = RatingsService()
    # 0 ratings -> returns global mean
    score_0 = svc.calculate_bayesian_score(0.0, 0, 4.2, 10.0)
    assert score_0 == 4.2

    # 3 ratings with 5.0 -> smoothed down towards 4.2
    # weighted = (3 / 13) * 5.0 + (10 / 13) * 4.2 = 1.1538 + 3.2307 = 4.38
    score_3 = svc.calculate_bayesian_score(5.0, 3, 4.2, 10.0)
    assert 4.3 < score_3 < 4.5

    # 5000 ratings with 4.8 -> converges closely to 4.8
    score_5000 = svc.calculate_bayesian_score(4.8, 5000, 4.2, 10.0)
    assert score_5000 == 4.8

def test_badges_objective_criteria():
    # Popular: >= 1000 installs
    assert is_popular({"install_count": 999}) is False
    assert is_popular({"install_count": 1000}) is True

    # Top Rated: rating >= 4.5 with >= 50 ratings
    assert is_top_rated({"rating_avg": 4.8, "rating_count": 49}) is False
    assert is_top_rated({"rating_avg": 4.49, "rating_count": 100}) is False
    assert is_top_rated({"rating_avg": 4.5, "rating_count": 50}) is True

    # Top Builder: Top rated + >= 3 plugins + >= 5000 installs
    assert is_top_builder({"rating_avg": 4.8, "rating_count": 50, "plugin_count": 2, "install_count": 6000}) is False
    assert is_top_builder({"rating_avg": 4.8, "rating_count": 50, "plugin_count": 3, "install_count": 5000}) is True

def test_jsonld_omits_aggregating_when_zero():
    # When 0 ratings, aggregateRating must NOT be present
    data_0 = generate_plugin_jsonld(
        name="Test",
        description="Desc",
        url="https://talos.ai/test",
        builder_name="Alex",
        rating_avg=None,
        rating_count=0
    )
    assert "aggregateRating" not in data_0

    # When ratings exist, aggregateRating must be included
    data_pos = generate_plugin_jsonld(
        name="Test",
        description="Desc",
        url="https://talos.ai/test",
        builder_name="Alex",
        rating_avg=4.7,
        rating_count=12
    )
    assert "aggregateRating" in data_pos
    assert data_pos["aggregateRating"]["ratingValue"] == 4.7
    assert data_pos["aggregateRating"]["reviewCount"] == 12

if __name__ == "__main__":
    test_sanitizer_reserved_usernames()
    test_sanitizer_html_strips_scripts()
    test_bayesian_scoring()
    test_badges_objective_criteria()
    test_jsonld_omits_aggregating_when_zero()
    print("ALL CORE UNIT TESTS PASSED!")
