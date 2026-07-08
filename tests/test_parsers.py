import datetime as dt

import pytest

from fra_bot.mc.errors import ParseError
from fra_bot.mc.parsers.applications import parse_applications_page
from fra_bot.mc.parsers.common import (
    infer_expense_event_ats,
    normalize_mc_timestamp,
    parse_int,
    parse_percent,
)
from fra_bot.mc.parsers.logs import classify_action, parse_logs_page
from fra_bot.mc.parsers.members import parse_members_page, validate_members_page
from fra_bot.mc.parsers.treasury import (
    parse_expenses_page,
    parse_income_table,
    parse_last_page_number,
    parse_total_funds,
)

UTC = dt.timezone.utc


# ----------------------------------------------------------------------
# common
# ----------------------------------------------------------------------

def test_parse_int():
    assert parse_int("1,234,567 Credits") == 1234567
    assert parse_int("42") == 42
    assert parse_int("no numbers") is None
    assert parse_int("") is None
    assert parse_int(None) is None


def test_parse_percent():
    assert parse_percent("5%") == 5.0
    assert parse_percent(" 12.5 % ") == 12.5
    assert parse_percent("nothing") is None


def test_normalize_absolute_timestamp():
    result = normalize_mc_timestamp("July 06, 2026 14:23")
    assert result is not None
    parsed = dt.datetime.fromisoformat(result)
    assert parsed.utcoffset() == dt.timedelta(0)
    # 14:23 EDT == 18:23 UTC
    assert (parsed.hour, parsed.minute) == (18, 23)


def test_normalize_yearless_timestamp_recent():
    reference = dt.datetime(2026, 7, 6, 12, 0, tzinfo=UTC)
    result = normalize_mc_timestamp("05 Jul 14:23", reference=reference)
    assert result is not None
    assert result.startswith("2026-07-05T18:23")


def test_normalize_yearless_timestamp_year_rollover():
    # Scraping on Jan 1st, a "31 Dec" row belongs to the previous year.
    reference = dt.datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    result = normalize_mc_timestamp("31 Dec 20:00", reference=reference)
    assert result is not None
    assert result.startswith("2026-01-01T01:00")  # 20:00 EST = 01:00 UTC next day


def test_normalize_yearless_timestamp_too_old_is_none():
    reference = dt.datetime(2026, 7, 6, 12, 0, tzinfo=UTC)
    assert normalize_mc_timestamp("01 Jan 10:00", reference=reference) is None


def test_normalize_garbage_is_none():
    assert normalize_mc_timestamp("not a date") is None
    assert normalize_mc_timestamp("") is None


# ----------------------------------------------------------------------
# members
# ----------------------------------------------------------------------

MEMBERS_HTML = """
<html><body>
<a href="/users/sign_out">Logout</a>
<table class="table">
  <thead><tr>
    <th>Name</th><th>Role</th><th>Earned Credits</th>
    <th>Discount</th><th>Alliance contribution rate</th><th>Member since</th>
  </tr></thead>
  <tbody>
    <tr>
      <td><a href="/users/101">Chief Alice</a></td>
      <td>Admin</td>
      <td>12,345,678 Credits</td>
      <td>10%</td>
      <td>5%</td>
      <td>07/05/2024</td>
    </tr>
    <tr>
      <td><a href="/users/102">Bob</a></td>
      <td></td>
      <td>987 Credits</td>
      <td>0%</td>
      <td>2.5%</td>
      <td>01/02/2025</td>
    </tr>
  </tbody>
</table>
</body></html>
"""


def test_parse_members_page_by_headers():
    page = parse_members_page(MEMBERS_HTML)
    assert page.has_table
    assert len(page.members) == 2

    alice = page.members[0]
    assert alice["mc_user_id"] == 101
    assert alice["name"] == "Chief Alice"
    assert alice["role"] == "Admin"
    assert alice["earned_credits"] == 12345678
    # Must be the CONTRIBUTION column (5%), not the discount column (10%).
    assert alice["contribution_rate"] == 5.0
    assert alice["raw_member_since"] == "07/05/2024"

    bob = page.members[1]
    assert bob["contribution_rate"] == 2.5


def test_members_page_without_table_fails_validation():
    page = parse_members_page("<html><body><p>maintenance</p></body></html>")
    assert not page.has_table
    with pytest.raises(ParseError):
        validate_members_page(page, page_number=1)
    # Deeper pages may legitimately be empty.
    validate_members_page(page, page_number=5)


# ----------------------------------------------------------------------
# applications
# ----------------------------------------------------------------------

APPLICATIONS_HTML = """
<table class="table">
  <tr>
    <td><a href="/profile/555">NewGuy</a></td>
    <td>1,234 Credits</td>
    <td>
      <a href="/verband/bewerbungen/annehmen/9001" class="btn">Accept</a>
      <a href="/verband/bewerbungen/ablehnen/9001" class="btn">Deny</a>
    </td>
  </tr>
  <tr><td>Some other row without accept link</td></tr>
</table>
"""


def test_parse_applications():
    apps = parse_applications_page(APPLICATIONS_HTML)
    assert len(apps) == 1
    assert apps[0]["application_id"] == 9001
    assert apps[0]["applicant_name"] == "NewGuy"
    assert apps[0]["mc_user_id"] == 555


def test_parse_applications_empty_page():
    assert parse_applications_page("<html><body></body></html>") == []


# ----------------------------------------------------------------------
# alliance logs
# ----------------------------------------------------------------------

LOGS_HTML = """
<table class="table">
  <tbody>
    <tr>
      <td>06 Jul 14:23</td>
      <td><a href="/profile/42">AdminAlice</a></td>
      <td>NewGuy added to the alliance</td>
      <td><a href="/profile/555">NewGuy</a></td>
    </tr>
    <tr>
      <td>06 Jul 13:00</td>
      <td><a href="/users/43">RichBob</a></td>
      <td>Contributed to the alliance <span class="label label-success">+5000 Credits</span></td>
      <td></td>
    </tr>
    <tr>
      <td>06 Jul 12:30</td>
      <td><a href="/profile/44">Builder</a></td>
      <td>Building constructed</td>
      <td><a href="/buildings/777">Hospital North</a></td>
    </tr>
  </tbody>
</table>
"""


def test_parse_logs_page():
    page = parse_logs_page(LOGS_HTML)
    assert page.has_table
    assert len(page.rows) == 3

    join = page.rows[0]
    assert join["action_key"] == "added_to_alliance"
    assert join["executed_name"] == "AdminAlice"
    assert join["executed_mc_id"] == 42
    assert join["affected_name"] == "NewGuy"
    assert join["affected_type"] == "user"
    assert join["affected_mc_id"] == 555

    contrib = page.rows[1]
    assert contrib["action_key"] == "contributed_to_alliance"
    assert contrib["contribution_amount"] == 5000
    assert "label" not in contrib["description"].lower()

    build = page.rows[2]
    assert build["action_key"] == "building_constructed"
    assert build["affected_type"] == "building"
    assert build["affected_mc_id"] == 777


def test_identical_log_rows_get_same_signature():
    html = LOGS_HTML.replace("13:00", "14:23")
    page = parse_logs_page(html)
    assert len({row["signature"] for row in page.rows}) == 3  # still distinct rows


def test_classify_action_guards():
    assert classify_action("X removed co-admin") == "removed_co_admin"
    assert classify_action("X removed admin") == "removed_admin"
    assert classify_action("Somebody set as transport admin") == "set_transport_admin"
    assert classify_action("weird new log line") == "unknown"


def test_logs_page_without_table():
    page = parse_logs_page("<html><body>x</body></html>")
    assert not page.has_table
    assert page.rows == []


# ----------------------------------------------------------------------
# treasury
# ----------------------------------------------------------------------

KASSE_HTML = """
<html><body>
<h3>Alliance funds: 12,345,678 Credits</h3>
<table>
  <thead><tr><th>Name</th><th>Credits</th></tr></thead>
  <tbody>
    <tr><td><a href="/users/101">Alice</a></td><td>50,000</td></tr>
    <tr><td><a href="/users/102">Bob</a></td><td>25,000</td></tr>
  </tbody>
</table>
<table>
  <thead><tr><th>Credits</th><th>Name</th><th>Description</th><th>Date</th></tr></thead>
  <tbody>
    <tr><td>1,000</td><td><a href="/users/103">Carl</a></td><td>Course Lessons</td><td>06 Jul 14:23</td></tr>
    <tr><td>1,000</td><td><a href="/users/103">Carl</a></td><td>Course Lessons</td><td>06 Jul 14:23</td></tr>
    <tr><td>500,000</td><td>System</td><td>Hospital extension</td><td>06 Jul 12:00</td></tr>
  </tbody>
</table>
<ul class="pagination">
  <li><a href="/verband/kasse?page=2">2</a></li>
  <li><a href="/verband/kasse?page=3157">Last</a></li>
</ul>
</body></html>
"""


def test_parse_total_funds():
    assert parse_total_funds(KASSE_HTML) == 12345678
    assert parse_total_funds("<html><body>nothing here</body></html>") is None


def test_parse_income_table_skips_expense_table():
    entries = parse_income_table(KASSE_HTML)
    assert [e["username"] for e in entries] == ["Alice", "Bob"]
    assert entries[0]["amount"] == 50000
    assert entries[0]["mc_user_id"] == 101


def test_parse_expenses_keeps_identical_rows():
    page = parse_expenses_page(KASSE_HTML)
    assert page.has_table
    assert len(page.rows) == 3
    # Identical rows are kept and share a signature.
    assert page.rows[0]["signature"] == page.rows[1]["signature"]
    assert page.rows[0]["amount"] == 1000
    assert page.rows[2]["username"] == "System"
    assert page.rows[2]["amount"] == 500000


def test_parse_expenses_small_amounts_kept():
    html = KASSE_HTML.replace(">1,000<", ">50<", 1)
    page = parse_expenses_page(html)
    assert page.rows[0]["amount"] == 50  # old bot dropped < 100; we keep everything


def test_parse_last_page_number():
    assert parse_last_page_number(KASSE_HTML) == 3157
    assert parse_last_page_number("<html></html>") is None


def test_infer_expense_event_ats_rolls_year_back():
    # Newest -> oldest; noon times so the UTC offset can't shift the date.
    raws = ["08 Jul 12:00", "02 Jan 12:00", "31 Dec 12:00", "15 Nov 12:00"]
    out = infer_expense_event_ats(raws, current_year=2026)
    assert out[0].startswith("2026-07-08")
    assert out[1].startswith("2026-01-02")
    assert out[2].startswith("2025-12-31")  # rolled back past New Year
    assert out[3].startswith("2025-11-15")


def test_infer_expense_event_ats_absolute_reanchors():
    raws = ["08 Jul 12:00", "July 06, 2023 12:00", "02 Jun 12:00"]
    out = infer_expense_event_ats(raws, current_year=2026)
    assert out[0].startswith("2026-07-08")
    assert out[1].startswith("2023-07-06")   # explicit year wins
    assert out[2].startswith("2023-06-02")   # cursor re-anchored to 2023


def test_infer_expense_event_ats_handles_unparseable():
    out = infer_expense_event_ats(["not a date", "08 Jul 12:00"], current_year=2026)
    assert out[0] is None
    assert out[1].startswith("2026-07-08")
