"""The alliance text page: change ONE field, leave every other alliance text
(description, rules, welcome PM, Discord webhook) byte-for-byte intact,
and only call it saved when a re-read shows it."""

import html as html_lib

import pytest

from fra_bot.mc import alliance_text as at
from fra_bot.mc.errors import FetchError

WEBHOOK = "https://ptb.discordapp.com/api/webhooks/1/secret-token"


class FakeServer:
    """MissionChief's /veband/text/edit + save, as Rails renders them:
    textareas get a leading newline and keep the CRLF a browser sent."""

    def __init__(self):
        self.values = {
            "alliance_text[content]": "[b]Welcome![/b]\r\n\r\nLine two",
            "alliance_text[rules]": "1) Be nice\r\n2) Pay 5%\r\n",
            "alliance_text[welcome_subject]": "Welcome to FRA",
            "alliance_text[welcome_text]": "Hi!\r\nRead the rules.",
            "alliance_text[chat_header]": "Old announcement",
            "discord_webhook": WEBHOOK,
        }
        self.posts = []
        self.ignore_posts = False
        self.post_error = None
        self.side_effect = None  # name -> value the server changes on save

    def render(self) -> str:
        v = {k: html_lib.escape(val, quote=True) for k, val in self.values.items()}
        return (
            '<html><body><form class="simple_form" id="edit_alliance_text_1008" '
            'action="/veband/text/speichern" method="post">'
            '<input name="utf8" type="hidden" value="&#x2713;" />'
            '<input type="hidden" name="_method" value="put" />'
            '<input type="hidden" name="authenticity_token" value="tok" />'
            '<textarea name="alliance_text[content]" id="alliance_text_content">\n'
            f'{v["alliance_text[content]"]}</textarea>'
            '<textarea name="alliance_text[rules]">\n'
            f'{v["alliance_text[rules]"]}</textarea>'
            '<input maxlength="200" type="text" '
            f'value="{v["alliance_text[welcome_subject]"]}" '
            'name="alliance_text[welcome_subject]" />'
            '<textarea name="alliance_text[welcome_text]">\n'
            f'{v["alliance_text[welcome_text]"]}</textarea>'
            '<input class="string optional form-control" maxlength="200" '
            f'type="text" value="{v["alliance_text[chat_header]"]}" '
            'name="alliance_text[chat_header]" id="alliance_text_chat_header" />'
            f'<input type="text" value="{v["discord_webhook"]}" '
            'name="discord_webhook" id="discord_webhook" />'
            '<input type="submit" name="commit" value="Save" />'
            "</form></body></html>"
        )

    def url(self, path):
        return "https://www.missionchief.com" + path

    async def fetch_page(self, path, **_kw):
        assert path == at.EDIT_PATH
        return self.render()

    async def post_form(self, path, data, **_kw):
        self.posts.append((path, list(data)))
        if not self.ignore_posts:
            for name, value in data:
                if name in self.values:
                    self.values[name] = value
            if self.side_effect:
                self.values.update(self.side_effect)
        if self.post_error:
            raise self.post_error
        return 200, "<html>Saved</html>", self.url("/verband")


def test_parse_reads_every_field_like_a_browser():
    form = at.parse_alliance_text_form(FakeServer().render())
    assert form.action == "/veband/text/speichern"
    # The newline Rails adds after <textarea> is dropped; CRLF reads as LF.
    assert form.get("description") == "[b]Welcome![/b]\n\nLine two"
    assert form.get("announcements") == "Old announcement"
    assert form.max_chars("announcements") == 200
    assert form.value("discord_webhook") == WEBHOOK
    assert form.submit == ("commit", "Save")


async def test_save_changes_only_the_announcements():
    server = FakeServer()
    untouched = {k: v for k, v in server.values.items()
                 if k != "alliance_text[chat_header]"}

    result = await at.save_alliance_texts(server, {"announcements": "New text"})

    assert result.ok and result.saved and result.collateral == []
    assert result.before["announcements"] == "Old announcement"
    assert result.after["announcements"] == "New text"
    assert server.values["alliance_text[chat_header]"] == "New text"
    # Every other text — and the webhook — byte-for-byte as before.
    for name, value in untouched.items():
        assert server.values[name] == value, name
    path, payload = server.posts[0]
    assert path == "/veband/text/speichern"
    sent = dict(payload)
    assert sent["_method"] == "put" and sent["authenticity_token"] == "tok"
    assert sent["commit"] == "Save"


async def test_repeated_saves_never_grow_the_textareas():
    server = FakeServer()
    rules = server.values["alliance_text[rules]"]
    for text in ("one", "two", "three"):
        result = await at.save_alliance_texts(server, {"announcements": text})
        assert result.ok
    assert server.values["alliance_text[rules]"] == rules


async def test_same_text_is_not_saved_again():
    server = FakeServer()
    result = await at.save_alliance_texts(server, {"announcements": "Old announcement"})
    assert result.ok and not result.saved
    assert server.posts == []


async def test_too_long_is_refused_before_saving():
    server = FakeServer()
    result = await at.save_alliance_texts(server, {"announcements": "x" * 201})
    assert not result.ok and "201/200" in result.detail
    assert server.posts == []


async def test_line_breaks_become_spaces_in_the_single_line_field():
    server = FakeServer()
    result = await at.save_alliance_texts(server, {"announcements": "a\nb\r\n c "})
    assert result.ok
    assert server.values["alliance_text[chat_header]"] == "a b c"


async def test_dry_run_saves_nothing():
    server = FakeServer()
    result = await at.save_alliance_texts(
        server, {"announcements": "New"}, dry_run=True
    )
    assert result.ok and not result.saved and "dry run" in result.detail
    assert server.posts == []


async def test_a_save_the_game_ignored_is_a_failure():
    server = FakeServer()
    server.ignore_posts = True
    result = await at.save_alliance_texts(server, {"announcements": "New"})
    assert not result.ok and result.saved
    assert "not stored" in result.detail


async def test_lost_response_still_counts_when_the_page_shows_it():
    server = FakeServer()
    server.post_error = FetchError("x", message="POST timed out")
    result = await at.save_alliance_texts(server, {"announcements": "New"})
    assert result.ok and result.saved


async def test_collateral_change_is_reported_without_the_secret():
    server = FakeServer()
    server.side_effect = {"discord_webhook": ""}
    result = await at.save_alliance_texts(server, {"announcements": "New"})
    assert result.collateral == ["Discord webhook"]
    assert "secret-token" not in repr(result)


async def test_unknown_field_is_a_programming_error():
    with pytest.raises(KeyError):
        await at.save_alliance_texts(FakeServer(), {"motto": "x"})


async def test_page_without_the_form_fails_cleanly():
    class NoForm(FakeServer):
        async def fetch_page(self, path, **_kw):
            return "<html><body>You may not edit this.</body></html>"

    result = await at.save_alliance_texts(NoForm(), {"announcements": "x"})
    assert not result.ok and "could not read" in result.detail
