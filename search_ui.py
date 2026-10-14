"""Short-lived Next buttons; authorization, channel checks and duplicate-click guard."""
import asyncio
import logging
import time

import nextcord

logger = logging.getLogger("gelbooru_bot.ui")
NEXT_TIMEOUT = 600.0


class NextView(nextcord.ui.View):
    def __init__(self, owner_id: int, channel_id: int, advance):
        super().__init__(timeout=NEXT_TIMEOUT, auto_defer=False)
        self.owner_id = owner_id
        self.channel_id = channel_id
        self.advance = advance
        self.message = None
        self.lock = asyncio.Lock()
        self.consumed = False
        self.expires_at = time.monotonic() + NEXT_TIMEOUT

    async def interaction_check(self, interaction):
        if interaction.user.id != self.owner_id or interaction.channel_id != self.channel_id:
            await interaction.response.send_message(
                "Эта кнопка — для автора запроса в исходном канале. Запусти свой поиск.",
                ephemeral=True)
            return False
        return True

    async def disable(self):
        for child in self.children:
            child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except nextcord.HTTPException:
                logger.debug("Could not disable an expired/deleted Next message")

    async def on_timeout(self):
        self.consumed = True
        await self.disable()

    @nextcord.ui.button(label="Next", emoji="➡️", style=nextcord.ButtonStyle.primary)
    async def next_button(self, button, interaction):
        # Also verify here so direct callback calls cannot bypass the checks.
        if not await self.interaction_check(interaction):
            return
        if time.monotonic() >= self.expires_at:
            self.consumed = True
            await self.disable()
            self.stop()
        if self.consumed or self.is_finished():
            return await interaction.response.send_message(
                "Кнопка уже использована или истекла. Используй новую кнопку или повтори команду.",
                ephemeral=True)
        if self.lock.locked():
            return await interaction.response.send_message("⏳ Уже ищу следующий арт.", ephemeral=True)
        async with self.lock:
            try:
                advanced = await self.advance(interaction)
            except Exception:
                logger.exception("Next failed")
                if interaction.response.is_done():
                    await interaction.followup.send("Не удалось продолжить поиск. Попробуй позже.",
                                                    ephemeral=True)
                else:
                    await interaction.response.send_message("Не удалось продолжить поиск.",
                                                            ephemeral=True)
                return
            if advanced:
                self.consumed = True
                self.message = getattr(interaction, "message", None) or self.message
                await self.disable()
                self.stop()