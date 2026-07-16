"""FAQ commands (reference bot: faqmanager).

Members: ``!faq <vraag>`` fuzzy-searches the FAQ (game-terminology
synonyms included) and posts the best answer; below the reference
suggestion threshold it lists "did you mean" options instead.
``!faq list`` shows everything.

Admins: ``!faq add "Vraag" antwoord``, ``!faq keywords <id> woord,…``,
``!faq edit <id> nieuw antwoord``, ``!faq remove <id>``.
"""

from __future__ import annotations

import logging

import discord
from discord.ext import commands

from ..db.repos import FaqRepo
from ..services.faq_search import SUGGESTION_THRESHOLD, rank_faqs
from .admin import is_fra_admin

log = logging.getLogger(__name__)


class FaqCog(commands.Cog):
    def __init__(self, bot) -> None:
        self.bot = bot
        self.repo = FaqRepo(bot.db)

    @commands.group(name="faq", invoke_without_command=True)
    async def faq(self, ctx: commands.Context, *, query: str = "") -> None:
        """`!faq <vraag>` — search the FAQ."""
        if not query:
            await ctx.send(
                "Ask me something: `!faq how do coins work` — or `!faq list` "
                "for every entry."
            )
            return
        rows = await self.repo.all_active()
        ranked = rank_faqs(query, rows)
        if not ranked:
            await ctx.send(
                "🤷 Nothing in the FAQ matches that. An admin can add it with "
                '`!faq add "Vraag" antwoord`.'
            )
            return
        best = ranked[0]
        if best.score < SUGGESTION_THRESHOLD:
            options = "\n".join(
                f"- `!faq #{s.faq_id}` — {s.question}" for s in ranked[:5]
            )
            await ctx.send(f"🔎 Did you mean:\n{options}")
            return
        await self._post_answer(ctx, best.faq_id)

    async def _post_answer(self, ctx: commands.Context, faq_id: int) -> None:
        row = await self.repo.get(faq_id)
        if row is None:
            await ctx.send(f"⚠️ FAQ #{faq_id} does not exist.")
            return
        embed = discord.Embed(
            title=f"❓ {row['question']}"[:256],
            description=row["answer"][:4096],
            colour=discord.Colour.blurple(),
        )
        embed.set_footer(text=f"FAQ #{row['id']}")
        await ctx.send(embed=embed)

    @faq.command(name="list")
    async def faq_list(self, ctx: commands.Context) -> None:
        rows = await self.repo.all_active()
        if not rows:
            await ctx.send("The FAQ is empty.")
            return
        lines = [f"`#{r['id']}` {r['question']}" for r in rows]
        await ctx.send("📚 FAQ:\n" + "\n".join(lines)[:1800])

    @faq.command(name="show")
    async def faq_show(self, ctx: commands.Context, faq_id: int) -> None:
        await self._post_answer(ctx, faq_id)

    # -- admin management ---------------------------------------------------

    @faq.command(name="add")
    @is_fra_admin()
    async def faq_add(
        self, ctx: commands.Context, question: str, *, answer: str
    ) -> None:
        """`!faq add "How do coins work?" Coins are earned by…` (quote the
        question)."""
        faq_id = await self.repo.add(
            question=question.strip(), answer=answer.strip(),
            created_by=ctx.author.display_name,
        )
        await ctx.send(f"✅ FAQ **#{faq_id}** added: {question}")

    @faq.command(name="edit")
    @is_fra_admin()
    async def faq_edit(
        self, ctx: commands.Context, faq_id: int, *, answer: str
    ) -> None:
        if await self.repo.update(faq_id, answer=answer.strip()):
            await ctx.send(f"✏️ FAQ **#{faq_id}** updated.")
        else:
            await ctx.send(f"⚠️ FAQ #{faq_id} does not exist.")

    @faq.command(name="keywords")
    @is_fra_admin()
    async def faq_keywords(
        self, ctx: commands.Context, faq_id: int, *, keywords: str
    ) -> None:
        """Extra search terms for an entry: `!faq keywords 3 arr, alarm rules`."""
        if await self.repo.update(faq_id, keywords=keywords.strip()):
            await ctx.send(f"🔑 FAQ **#{faq_id}** keywords set: {keywords}")
        else:
            await ctx.send(f"⚠️ FAQ #{faq_id} does not exist.")

    @faq.command(name="remove")
    @is_fra_admin()
    async def faq_remove(self, ctx: commands.Context, faq_id: int) -> None:
        if await self.repo.remove(faq_id):
            await ctx.send(f"🗑️ FAQ **#{faq_id}** removed.")
        else:
            await ctx.send(f"⚠️ FAQ #{faq_id} does not exist.")


async def setup(bot) -> None:
    await bot.add_cog(FaqCog(bot))
