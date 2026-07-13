from fra_bot.mc.parsers.academy import (
    infer_discipline,
    parse_academy_page,
    parse_alliance_buildings_page,
)
from fra_bot.mc.parsers.board import parse_board_thread_page
from fra_bot.mc.trainings_catalog import ambiguous_names, match_trainings
from fra_bot.services.buildings import detect_building_type

BOARD_HTML = """
<html><body>
<script>var user_id = 999;</script>
<div id="post-on-page-1" class="post">
  <a href="/profile/555">MemberOne</a>
  <span title="Mon, 06 Jul 2026 14:23">2 hours ago</span>
  <a href="/alliance_posts/88001">permalink</a>
  <div class="col-md-11">Please open a HazMat training,<br>thanks!</div>
</div>
<div id="post-on-page-2" class="post">
  <a href="/profile/999">TheBot</a>
  <span title="Mon, 06 Jul 2026 14:25">1 hour ago</span>
  <a href="/alliance_posts/88002">permalink</a>
  <div class="col-md-11">[FRA] Training request processed</div>
</div>
<ul class="pagination">
  <li><a href="?page=1">1</a></li>
  <li class="active">2</li>
</ul>
<form id="new_alliance_post" action="/alliance_posts?alliance_thread_id=5935">
  <input name="authenticity_token" value="tok-abc"/>
</form>
</body></html>
"""


def test_parse_board_thread_page():
    page = parse_board_thread_page(BOARD_HTML)
    assert page.current_user_id == 999
    assert page.last_page == 2
    assert page.reply_token == "tok-abc"
    assert page.reply_action == "/alliance_posts?alliance_thread_id=5935"
    assert len(page.posts) == 2

    first = page.posts[0]
    assert first.post_id == 88001
    assert first.author_mc_id == 555
    assert first.author_name == "MemberOne"
    assert "HazMat training" in first.content
    assert first.raw_timestamp == "Mon, 06 Jul 2026 14:23"


def test_board_content_multiline():
    page = parse_board_thread_page(BOARD_HTML)
    assert "\n" in page.posts[0].content  # <br> became newline


# ---------------------------------------------------------------------
# Training catalog matching
# ---------------------------------------------------------------------

def test_match_exact_training():
    matches, ambiguous = match_trainings("Please open a HazMat training")
    assert not ambiguous
    assert len(matches) == 1
    assert matches[0].name == "HazMat"
    assert matches[0].discipline == "fire"
    assert matches[0].duration_days == 3


def test_match_multiple_trainings():
    matches, _ = match_trainings("SWAT and K-9 please")
    names = {m.name for m in matches}
    assert names == {"SWAT", "K-9"}
    assert all(m.discipline == "police" for m in matches)


def test_ambiguous_training_requires_prefix():
    # Lifeguard Training exists in fire, ems and coastal.
    matches, ambiguous = match_trainings("Lifeguard Training")
    assert not matches
    assert len(ambiguous) == 1
    assert ambiguous[0].name == "Lifeguard Training"
    assert len(ambiguous[0].disciplines) >= 2


def test_ambiguous_resolved_by_prefix():
    matches, ambiguous = match_trainings("Water Rescue - Lifeguard Training")
    assert not ambiguous
    assert len(matches) == 1
    assert matches[0].discipline == "coastal"


def test_ambiguous_names_catalog():
    ambiguous = ambiguous_names()
    assert "lifeguard training" in ambiguous
    assert "ocean navigation" in ambiguous


def test_no_match_for_chatter():
    matches, ambiguous = match_trainings("thanks everyone, great work today!")
    assert not matches
    assert not ambiguous


# ---------------------------------------------------------------------
# Academy parsing
# ---------------------------------------------------------------------

ACADEMY_LIST_HTML = """
<table>
  <tr search_attribute="Fire Academy North">
    <td><img building_id="4951748" src="/img/fire_academy.png" alt="Fire Academy"/></td>
    <td><a href="/buildings/4951748" class="btn btn-success">Start a new training course</a></td>
  </tr>
  <tr search_attribute="Police Academy">
    <td><img building_id="4951746" src="/img/police.png" alt="Police"/></td>
    <td><a href="/buildings/4951746" class="btn btn-default">View</a></td>
  </tr>
</table>
"""


def test_parse_alliance_buildings_page():
    listings = parse_alliance_buildings_page(ACADEMY_LIST_HTML)
    assert len(listings) == 2
    fire = listings[0]
    assert fire.building_id == 4951748
    assert fire.discipline == "fire"
    assert fire.has_start_button
    police = listings[1]
    assert police.discipline == "police"
    assert not police.has_start_button  # no btn-success


def test_infer_discipline():
    assert infer_discipline("Coastal Rescue School") == "coastal"
    assert infer_discipline("some fire academy") == "fire"
    assert infer_discipline("random building") is None


ACADEMY_PAGE_HTML = """
<form action="/buildings/4951748/education" method="post">
  <input type="hidden" name="authenticity_token" value="tok-xyz"/>
  <select name="building_rooms_use">
    <option value="1">1</option>
    <option value="2">2</option>
  </select>
  <select name="alliance[cost]">
    <option value="0">Free</option>
    <option value="100">100</option>
  </select>
  <select name="education_select">
    <option value="12">HazMat (3 days)</option>
    <option value="15">Truck Driver's License (2 days)</option>
  </select>
  <input type="submit" value="Educate"/>
</form>
"""


def test_parse_academy_page():
    page = parse_academy_page(ACADEMY_PAGE_HTML)
    assert page.action == "/buildings/4951748/education"
    assert page.authenticity_token == "tok-xyz"
    assert page.available_rooms == 2
    assert 0 in page.costs
    assert page.find_course_value("HazMat") == "12"
    assert page.find_course_value("Truck Driver's License") == "15"
    assert page.find_course_value("Nonexistent") is None


# ---------------------------------------------------------------------
# Building type detection
# ---------------------------------------------------------------------

def test_detect_building_type():
    assert detect_building_type("St. Mary's Hospital, Main St", None) == "hospital"
    assert detect_building_type("State Correctional Facility", None) == "prison"
    assert detect_building_type("County Jail", None) == "prison"
    assert detect_building_type("Random Park", None) is None
    # Ambiguous: both terms present -> None (ask the user).
    assert detect_building_type("Prison Hospital Wing", None) is None
    # Auto-detect for !fra testbuild: French "Hospitalier" contains "hospital".
    assert detect_building_type("Centre Hospitalier de Beaune, France", None) == "hospital"


def test_detect_building_type_osm_and_rejects():
    # The OSM feature type is authoritative even for a generic street.
    assert detect_building_type("12 Main St", None, "hospital") == "hospital"
    assert detect_building_type("5 Rue de la Prison", None, "prison") == "prison"
    # Look-alikes are refused by name...
    assert detect_building_type("Downtown Clinic", None) is None
    assert detect_building_type("Central Police Station", None) is None
    # ...unless the OSM tag confirms the real type.
    assert detect_building_type("Downtown Clinic", None, "hospital") == "hospital"
    # Inactive sites are refused even when named like one.
    assert detect_building_type("Old Prison Museum", None) is None


async def test_edit_post_returns_false_when_post_is_gone():
    """A stale guide id (post deleted from the board) must degrade to False —
    ensure_guide_post then forgets the id and re-creates — never raise and
    wedge guide maintenance forever."""
    from fra_bot.mc.board import BoardClient
    from fra_bot.mc.errors import FetchError

    class GoneClient:
        def url(self, path):
            return path

        async def fetch_page(self, path, *, referer=None):
            raise FetchError(path, 404)

        async def post_form(self, path, data, **kwargs):
            raise AssertionError("must not POST when the edit page is gone")

    board = BoardClient(GoneClient())
    assert await board.edit_post(12345, "new text") is False


async def test_find_bot_post_matches_despite_emoji_rendering():
    """The forum re-renders emoji (often into images that vanish from the
    text), so marker matching must be a normalized substring check — a
    guide posted with 📋 must still be FOUND when the page returns it
    without the emoji."""
    from fra_bot.mc.board import BoardClient

    html = """
    <html><body>
    <script>var user_id = 999;</script>
    <div id="post-on-page-1">
      <a href="/alliance_posts/501">#1</a>
      <a href="/profile/999">FRA Bot</a>
      <div class="col-md-11">[FRA]  How to request a TRAINING here<br>Post the training name…</div>
    </div>
    <form id="new_alliance_post" action="/alliance_posts?alliance_thread_id=15305">
      <input name="authenticity_token" value="tok"/>
    </form>
    </body></html>
    """

    class OnePage:
        def url(self, path):
            return path

        async def fetch_page(self, path, *, referer=None):
            return html

    board = BoardClient(OnePage())
    found = await board.find_bot_post(15305, "[FRA] 📋 How to request a TRAINING")
    assert found == 501
    # Other markers still don't match.
    assert await board.find_bot_post(15305, "[FRA] 📋 How to request a BUILDING") is None


async def test_post_reply_sets_last_error_reasons():
    """Failures carry a human-readable reason for the guides report."""
    from fra_bot.mc.board import BoardClient

    NO_FORM = """
    <html><body>
    <div id="post-on-page-1"><a href="/alliance_posts/1">#1</a>
      <div class="col-md-11">hello</div></div>
    </body></html>
    """

    class NoFormClient:
        def url(self, path):
            return path

        async def fetch_page(self, path, *, referer=None):
            return NO_FORM

        async def post_form(self, path, data, **kwargs):
            raise AssertionError("must not POST without a token")

    board = BoardClient(NoFormClient())
    assert await board.post_reply(15305, "[FRA] hi") is False
    assert "no reply form/token" in board.last_error

    class RejectClient(NoFormClient):
        async def fetch_page(self, path, *, referer=None):
            return NO_FORM.replace(
                "</body>",
                '<form id="new_alliance_post" action="/alliance_posts">'
                '<input name="authenticity_token" value="tok"/></form></body>',
            )

        async def post_form(self, path, data, **kwargs):
            return (422, "", "")

    board = BoardClient(RejectClient())
    assert await board.post_reply(15305, "[FRA] hi") is False
    assert "HTTP 422" in board.last_error


# -- matcher hardening (the "technical rescue training" incident) ------------

def test_unknown_course_no_longer_fuzzes_onto_the_nearest_name():
    # A course that does NOT exist must not fuzz onto a real one that merely
    # shares a tail (this used to open the WRONG class). "Technical Rescue
    # Training" is a real Fire course now, so use another unknown "… rescue
    # training" the catalog does not contain.
    matches, ambiguous = match_trainings("rope rescue training")
    assert matches == [] and ambiguous == []


def test_live_catalog_matches_courses_the_builtin_list_lacks():
    live = {"fire": {"Technical Rescue Training": 4, "HazMat": 3}}
    matches, _ = match_trainings("Technical rescue training", live)
    assert [(m.discipline, m.name) for m in matches] == [
        ("fire", "Technical Rescue Training")
    ]


def test_copy_counts_parse_and_cap():
    live = {"fire": {"Technical Rescue Training": 4}}
    matches, _ = match_trainings("3x technical rescue training", live)
    assert matches[0].count == 3
    matches, _ = match_trainings("technical rescue training x2", live)
    assert matches[0].count == 2
    # Repeated lines sum, capped at 4.
    matches, _ = match_trainings(
        "3x technical rescue training\n3x technical rescue training", live
    )
    assert matches[0].count == 4


def test_typo_tolerance_survives_the_stricter_fuzz():
    for text, expected in (
        ("HazMta", "HazMat"),
        ("k9", "K-9"),
        ("police avation", "Police Aviation"),
        ("technical rescue trainng", "Technical Rescue Training"),
    ):
        matches, ambiguous = match_trainings(text)
        names = [m.name for m in matches] + [a.name for a in ambiguous]
        assert expected in names, (text, names)
