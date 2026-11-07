"""Owner-authorized Telegram management and inline location selection."""

import asyncio
import hashlib
import html
import logging

from aiogram import BaseMiddleware, Bot, Dispatcher, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from backend.service import Monitor, interval_label


def keyboard(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    """Construct small inline controls with bounded callback data."""
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=text, callback_data=data) for text, data in row] for row in rows])


class OwnerOnly(BaseMiddleware):
    """Authorize both messages and callback queries before any side effects."""

    def __init__(self, owner_id: int):
        """Bind the bot to one private Telegram owner."""
        self.owner_id = owner_id

    async def __call__(self, handler, event, data):
        """Ignore other users and all group chats."""
        message = event.message if isinstance(event, CallbackQuery) else event
        if not event.from_user or event.from_user.id != self.owner_id or not message or message.chat.type != "private" or message.chat.id != self.owner_id:
            if isinstance(event, CallbackQuery):
                await event.answer("Not authorized", show_alert=True)
            return None
        return await handler(event, data)


class TelegramUI:
    """Share search configuration between chat and the web dashboard."""

    def __init__(self, monitor: Monitor, owner_id: int):
        """Register one owner-protected router and short-lived wizard state."""
        self.monitor = monitor
        self.store = monitor.store
        self.draft = None
        self.nodes = {}
        self.router = Router()
        self.router.message.outer_middleware(OwnerOnly(owner_id))
        self.router.callback_query.outer_middleware(OwnerOnly(owner_id))
        self.router.message.register(self.command, Command("start", "help", "add", "searches", "edit", "pause", "resume", "delete", "latest", "status", "cancel"))
        self.router.callback_query.register(self.callback)
        self.router.message.register(self.input)

    async def command(self, message: Message):
        """Implement the PRD commands with validated IDs and safe HTML output."""
        parts = (message.text or "").split(maxsplit=1)
        command = parts[0].split("@", 1)[0]
        argument = parts[1] if len(parts) > 1 else ""
        if command in {"/start", "/help"}:
            if command == "/start":
                self.store.set_setting("telegram_ready", True)
            await message.answer("House Monitor\n\n/add — create a house search\n/searches — manage searches\n/edit ID — edit criteria\n/pause ID — pause a search\n/resume ID — resume a search\n/delete ID — delete with confirmation\n/latest ID — recent matching houses\n/status — monitoring and delivery status\n/cancel — cancel setup\n\nFirst source scans establish a baseline without alerts.")
            return
        if command == "/cancel":
            self.draft = None
            await message.answer("Search setup cancelled.")
            return
        if command == "/searches":
            searches = self.store.searches()
            if not searches:
                await message.answer("No saved searches. Use /add.")
            for search in searches:
                await self.search_message(message, search)
            return
        if command == "/status":
            status = self.monitor.status()
            lines = [f"Monitoring every {interval_label(self.monitor.interval)}", "Scanning now" if status["scanning"] else "Waiting for next cycle"]
            lines += [f'{source}: {state["status"]}' for source, state in status["sources"].items()]
            lines.append("Delivery queue: " + str(status["queue"]))
            await message.answer("\n".join(lines))
            return
        if command == "/add":
            self.draft = {"name": argument[:60] or "Houses", "min": None, "max": None, "locations": [], "active": True, "step": "min", "id": None}
            await message.answer("Houses for sale\nMinimum house area in m²? Send a number or /skip.")
            return
        try:
            search_id = int(argument)
            search = next(search for search in self.store.searches() if search["id"] == search_id)
        except (ValueError, StopIteration):
            await message.answer("Use a valid saved search ID, for example /edit 1. See /searches.")
            return
        await self.action(message, command.removeprefix("/"), search)

    async def search_message(self, message: Message, search: dict):
        """Show a readable search summary with direct management buttons."""
        catalog = self.store.locations()
        places = ", ".join(catalog.get(location, {}).get("label", location) for location in search["locations"]) or "All Latvia"
        area = f'{search["min"] or "Any"} – {search["max"] or "Any"} m²'
        await message.answer(f'<b>#{search["id"]} {html.escape(search["name"])}</b>\n{"Active" if search["active"] else "Paused"}\n{area}\n{html.escape(places)}', parse_mode="HTML", reply_markup=keyboard([
            [("Edit", f'edit:{search["id"]}'), ("Pause" if search["active"] else "Resume", f'{"pause" if search["active"] else "resume"}:{search["id"]}')],
            [("Recent houses", f'latest:{search["id"]}'), ("Delete", f'delete:{search["id"]}')]]))

    async def action(self, message: Message, action: str, search: dict):
        """Apply a validated search operation without dropping history."""
        if action in {"pause", "resume"}:
            self.store.save_search({**search, "active": action == "resume"}, search["id"])
            if action == "resume":
                self.monitor.trigger()
            await message.answer("Search paused." if action == "pause" else "Search resumed.")
        elif action == "edit":
            self.draft = {**search, "step": "min"}
            await message.answer("Edit minimum house area in m². Send a number or /skip to clear it. Listing history is preserved.")
        elif action == "delete":
            await message.answer(f'Delete “{search["name"]}”?', reply_markup=keyboard([[("Delete search", f'confirm:{search["id"]}'), ("Keep search", "cancel-delete")]]))
        elif action == "latest":
            rows = self.store.db.execute("SELECT listing_id FROM search_history WHERE search_id=? ORDER BY discovered_at DESC LIMIT 5", (search["id"],)).fetchall()
            identifiers = {row[0] for row in rows}
            listings = [listing for listing in self.store.listings() if listing["id"] in identifiers]
            if not listings:
                await message.answer("No matching houses discovered yet.")
            for listing in listings:
                await message.answer(f'{html.escape(listing["title"])}\n{html.escape(listing["price"])}\n{html.escape(listing["location"])}\n<a href="{html.escape(listing["url"], quote=True)}">Open original advertisement</a>', parse_mode="HTML")

    async def input(self, message: Message):
        """Validate numeric wizard input; /skip leaves an area limit empty."""
        if not self.draft or self.draft["step"] not in {"min", "max"}:
            await message.answer("Use /searches to manage searches or /add to create one.")
            return
        text = (message.text or "").strip()
        try:
            value = None if text == "/skip" else float(text.replace(",", "."))
            if value is not None and not 0 < value <= 1_000_000:
                raise ValueError()
            if self.draft["step"] == "max" and value is not None and self.draft["min"] is not None and value < self.draft["min"]:
                raise ValueError()
        except ValueError:
            await message.answer("Send a positive area number or /skip. Maximum must be at least minimum.")
            return
        step = self.draft["step"]
        self.draft[step] = value
        if step == "min":
            self.draft["step"] = "max"
            await message.answer("Maximum house area in m²? Send a number or /skip.")
        else:
            self.draft["step"] = "locations"
            await self.location_message(message)

    async def location_message(self, message: Message, parent: str = "", offset: int = 0):
        """Page through actual category nodes with separate select and browse controls."""
        catalog = self.store.locations()
        children = [record for record in catalog.values() if record.get("parent", "") == parent]
        if not children:
            try:
                children = await self.monitor.discover(parent.removeprefix("ss:"))
            except Exception:
                await message.answer("Source locations unavailable. Try again later; your draft is kept.")
                return
        if parent == "" and offset == 0:
            children = [catalog["ss:" + path] for path in ["riga-region/carnikavas-nov", "riga/vecaki", "riga/mangalsala", "riga/vecdaugava", "riga/trisciems", "riga/jaunciems"] if "ss:" + path in catalog] + children
        rows = []
        for record in children[offset:offset + 8]:
            token = hashlib.sha256(record["id"].encode()).hexdigest()[:24]
            self.nodes[token] = record
            selected = record["id"] in self.draft["locations"]
            rows.append([(("✓ " if selected else "") + record["label"], f"select:{token}"), ("Browse ›", f"browse:{token}")])
        if offset + 8 < len(children):
            rows.append([("Next locations ›", f"page:{offset + 8}")])
        rows += [[("All Latvia", "all-latvia"), ("Back to regions", "root")], [("Review search", "review")]]
        self.draft["parent"] = parent
        self.draft["offset"] = offset
        await message.answer(f'Select locations ({len(self.draft["locations"])} selected). Empty means all Latvia.', reply_markup=keyboard(rows))

    async def callback(self, query: CallbackQuery):
        """Handle owner-authorized buttons and reject expired wizard callbacks."""
        await query.answer()
        action, _, argument = (query.data or "").partition(":")
        message = query.message
        if action == "cancel-delete":
            await message.edit_reply_markup(reply_markup=None)
            return
        if action in {"pause", "resume", "edit", "delete", "latest", "confirm"}:
            search = next((search for search in self.store.searches() if str(search["id"]) == argument), None)
            if not search:
                await message.answer("Search no longer exists.")
            elif action == "confirm":
                self.store.delete_search(search["id"])
                await message.edit_reply_markup(reply_markup=None)
                await message.answer("Search deleted. Listing history retained.")
            else:
                await self.action(message, action, search)
            return
        if not self.draft:
            await message.answer("This setup has expired. Use /add or /edit ID.")
            return
        if action in {"select", "browse"}:
            try:
                node = self.nodes[argument]
            except KeyError:
                await message.answer("Location selection expired. Use /add again.")
                return
            if action == "select":
                if node["id"] in self.draft["locations"]:
                    self.draft["locations"].remove(node["id"])
                else:
                    self.draft["locations"].append(node["id"])
                await message.edit_reply_markup(reply_markup=None)
                await self.location_message(message, self.draft["parent"], self.draft["offset"])
            else:
                await message.edit_reply_markup(reply_markup=None)
                await self.location_message(message, node["id"])
        elif action in {"root", "page", "all-latvia"}:
            if action == "all-latvia":
                self.draft["locations"] = []
            await message.edit_reply_markup(reply_markup=None)
            await self.location_message(message, self.draft.get("parent", "") if action == "page" else "", int(argument) if action == "page" else 0)
        elif action == "review":
            catalog = self.store.locations()
            places = ", ".join(catalog[location]["label"] for location in self.draft["locations"]) or "All Latvia"
            await message.answer(f'{self.draft["name"]}\nHouse area: {self.draft["min"] or "Any"} – {self.draft["max"] or "Any"} m²\n{places}\nEvery {interval_label(self.monitor.interval)}', reply_markup=keyboard([[("Save search", "save"), ("Back", "root")]]))
        elif action == "save":
            try:
                self.store.save_search(self.draft, self.draft.get("id"))
                self.draft = None
                self.monitor.trigger()
                await message.edit_reply_markup(reply_markup=None)
                await message.answer("Search saved.")
            except ValueError as error:
                await message.answer(str(error))


async def run_bot(monitor: Monitor, token: str, owner_id: int):
    """Connect the configured bot, retry startup failures, and close its session on exit."""
    if monitor.store.setting("telegram_owner_id") != owner_id:
        monitor.store.set_setting("telegram_ready", False)
        monitor.store.set_setting("telegram_owner_id", owner_id)
    while True:
        bot = None
        try:
            bot = Bot(token)
            identity = await bot.get_me()
            monitor.bot = bot
            monitor.owner_id = owner_id
            monitor.bot_username = identity.username
            monitor.bot_error = None
            dispatcher = Dispatcher()
            dispatcher.include_router(TelegramUI(monitor, owner_id).router)
            await dispatcher.start_polling(bot, handle_signals=False, close_bot_session=False)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            monitor.bot_error = "Telegram connection failed: " + type(error).__name__
            logging.getLogger(__name__).warning("Telegram connection failed: %s", type(error).__name__)
        finally:
            monitor.bot = None
            if bot:
                await bot.session.close()
        await asyncio.sleep(60)