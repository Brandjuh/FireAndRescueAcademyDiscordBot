"""Regression tests for login/session verification (_looks_logged_in).

The live bug: a valid login redirected to /buildings but the HTML did
not contain a 'logout'/'sign out' marker (MissionChief loads it in a
dropdown), so verification wrongly failed with "Login verification
failed: check request ended on .../buildings".
"""

from fra_bot.mc.client import MissionChiefClient

_logged_in = MissionChiefClient._looks_logged_in


def test_success_url_without_marker_is_logged_in():
    # The exact regression: landed on /buildings, no logout marker.
    html = "<html><body><h1>Buildings</h1><div class='map'></div></body></html>"
    assert _logged_in("https://www.missionchief.com/buildings", html) is True


def test_sign_in_url_is_not_logged_in():
    html = "<html><body><form action='/users/sign_in'>login</form></body></html>"
    assert _logged_in("https://www.missionchief.com/users/sign_in", html) is False


def test_marker_without_known_url_is_logged_in():
    # A page we don't recognize by URL but with a logout link still counts.
    html = "<a href='/users/sign_out'>Logout</a>"
    assert _logged_in("https://www.missionchief.com/some/other/page", html) is True


def test_unknown_url_without_marker_is_not_logged_in():
    html = "<html><body>please sign in</body></html>"
    assert _logged_in("https://www.missionchief.com/", html) is False


def test_verband_pages_are_logged_in():
    # Applications, members, kasse all live under /verband and must pass.
    for path in ("/verband/bewerbungen", "/verband/mitglieder/1621", "/verband/kasse"):
        assert _logged_in(f"https://www.missionchief.com{path}", "<html></html>") is True


def test_sign_in_takes_priority_over_authenticated_fragment():
    # A redirect back to sign-in with a query wins over any fragment.
    url = "https://www.missionchief.com/users/sign_in?return=/buildings"
    assert _logged_in(url, "<html></html>") is False
