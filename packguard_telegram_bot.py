import asyncio
import json
import logging
import random
import sqlite3
import string
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Optional

from telegram import Document, ReplyKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# =========================
# EDIT THESE 2 VALUES ONLY
# =========================
BOT_TOKEN = "8647197714:AAHC2cSqKt8tWZpVB1j5OtXkbNkB1PnQJ8Q"
ADMIN_IDS = {5641978909}  # replace with your Telegram numeric ID

# =========================
# BOT SETTINGS
# =========================
BOT_NAME = "PackGuardBot"
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
MASTER_PACKS_DIR = DATA_DIR / "master_packs"
BUILT_PACKS_DIR = DATA_DIR / "built_packs"
TMP_DIR = DATA_DIR / "tmp"
DB_PATH = DATA_DIR / "packguard.sqlite3"
SETTINGS_PATH = DATA_DIR / "settings.json"
DEFAULT_HIDDEN_FILENAME = ".cache"
VISIBLE_NOTICE_FILENAME = "READ_ME.txt"

(
    ADD_PACK_NAME,
    ADD_PACK_FILE,
    DELIVER_PACK_ID,
    DELIVER_BUYER_USERNAME,
    INSPECT_FILE,
) = range(5)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(BOT_NAME)


def ensure_dirs() -> None:
    for path in [DATA_DIR, MASTER_PACKS_DIR, BUILT_PACKS_DIR, TMP_DIR]:
        path.mkdir(parents=True, exist_ok=True)


ensure_dirs()


class DB:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS buyers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER UNIQUE NOT NULL,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    registered_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS packs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT UNIQUE NOT NULL,
                    file_path TEXT NOT NULL,
                    original_filename TEXT,
                    uploaded_at TEXT NOT NULL,
                    uploaded_by_admin_id INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS deliveries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    pack_id INTEGER NOT NULL,
                    buyer_username TEXT NOT NULL,
                    buyer_chat_id INTEGER,
                    fingerprint_id TEXT NOT NULL,
                    output_zip_path TEXT NOT NULL,
                    delivered_at TEXT NOT NULL,
                    delivered_by_admin_id INTEGER NOT NULL,
                    FOREIGN KEY (pack_id) REFERENCES packs (id)
                )
                """
            )

    def upsert_buyer(
        self,
        chat_id: int,
        username: Optional[str],
        first_name: str,
        last_name: Optional[str],
    ) -> None:
        now = datetime.utcnow().isoformat()
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT id FROM buyers WHERE chat_id = ?",
                (chat_id,),
            ).fetchone()

            if existing:
                conn.execute(
                    """
                    UPDATE buyers
                    SET username = ?, first_name = ?, last_name = ?
                    WHERE chat_id = ?
                    """,
                    (username, first_name, last_name, chat_id),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO buyers (chat_id, username, first_name, last_name, registered_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (chat_id, username, first_name, last_name, now),
                )

    def get_buyer_by_username(self, username: str) -> Optional[sqlite3.Row]:
        normalized = username.lstrip("@").strip().lower()
        if not normalized:
            return None
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM buyers WHERE lower(username) = ? LIMIT 1",
                (normalized,),
            ).fetchone()

    def list_buyers(self, limit: int = 30) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM buyers ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()

    def add_or_replace_pack(
        self,
        name: str,
        file_path: Path,
        original_filename: str,
        admin_id: int,
    ) -> None:
        now = datetime.utcnow().isoformat()
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT id FROM packs WHERE name = ?",
                (name,),
            ).fetchone()

            if existing:
                conn.execute(
                    """
                    UPDATE packs
                    SET file_path = ?, original_filename = ?, uploaded_at = ?, uploaded_by_admin_id = ?
                    WHERE name = ?
                    """,
                    (str(file_path), original_filename, now, admin_id, name),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO packs (name, file_path, original_filename, uploaded_at, uploaded_by_admin_id)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (name, str(file_path), original_filename, now, admin_id),
                )

    def list_packs(self) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM packs ORDER BY id ASC"
            ).fetchall()

    def get_pack_by_id(self, pack_id: int) -> Optional[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM packs WHERE id = ? LIMIT 1",
                (pack_id,),
            ).fetchone()

    def save_delivery(
        self,
        pack_id: int,
        buyer_username: str,
        buyer_chat_id: Optional[int],
        fingerprint_id: str,
        output_zip_path: Path,
        admin_id: int,
    ) -> None:
        now = datetime.utcnow().isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO deliveries (pack_id, buyer_username, buyer_chat_id, fingerprint_id, output_zip_path, delivered_at, delivered_by_admin_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pack_id,
                    buyer_username,
                    buyer_chat_id,
                    fingerprint_id,
                    str(output_zip_path),
                    now,
                    admin_id,
                ),
            )

    def find_delivery_by_fingerprint(self, fingerprint_id: str) -> Optional[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM deliveries WHERE fingerprint_id = ? LIMIT 1",
                (fingerprint_id,),
            ).fetchone()


db = DB(DB_PATH)


def load_settings() -> dict:
    settings = {
        "hidden_filename": DEFAULT_HIDDEN_FILENAME,
        "include_visible_notice": False,
    }
    if SETTINGS_PATH.exists():
        try:
            saved = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                settings.update(saved)
        except Exception:
            pass
    return settings


def write_default_settings_if_missing() -> None:
    if not SETTINGS_PATH.exists():
        SETTINGS_PATH.write_text(
            json.dumps(
                {
                    "hidden_filename": DEFAULT_HIDDEN_FILENAME,
                    "include_visible_notice": False,
                },
                indent=2,
            ),
            encoding="utf-8",
        )


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def random_id(length: int = 10) -> str:
    alphabet = string.ascii_uppercase + string.digits
    return "".join(random.SystemRandom().choice(alphabet) for _ in range(length))


def sanitize_name(value: str) -> str:
    cleaned = "".join(
        c for c in value if c.isalnum() or c in ("-", "_", ".", "@")
    ).strip("._")
    return cleaned or "buyer"


def write_watermark(
    pack_root: Path,
    hidden_filename: str,
    buyer_username: str,
    fingerprint_id: str,
) -> Path:
    hidden_filename = hidden_filename.strip() or DEFAULT_HIDDEN_FILENAME
    if not hidden_filename.startswith("."):
        hidden_filename = "." + hidden_filename

    watermark_path = pack_root / hidden_filename
    watermark_path.write_text(
        f"id={fingerprint_id}\n"
        f"buyer={buyer_username}\n"
        f"license=single_user\n"
        f"source=pack_guard_bot\n",
        encoding="utf-8",
    )
    return watermark_path


def write_visible_notice(pack_root: Path, fingerprint_id: str) -> None:
    notice_path = pack_root / VISIBLE_NOTICE_FILENAME
    notice_path.write_text(
        "Single-user license.\n"
        "This pack contains a unique hidden fingerprint ID.\n"
        "Unauthorized redistribution can be traced.\n"
        f"Reference ID: {fingerprint_id}\n",
        encoding="utf-8",
    )


def zip_folder(source_folder: Path, output_zip: Path) -> None:
    with zipfile.ZipFile(output_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for item in source_folder.rglob("*"):
            if item.is_file():
                zf.write(item, item.relative_to(source_folder))


def extract_zip_to(zip_path: Path, out_dir: Path) -> None:
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(out_dir)


def build_buyer_pack(master_zip: Path, buyer_username: str) -> tuple[str, Path]:
    settings = load_settings()
    hidden_filename = settings.get("hidden_filename", DEFAULT_HIDDEN_FILENAME)
    include_visible_notice = bool(settings.get("include_visible_notice", False))

    fingerprint_id = random_id()
    safe_buyer = sanitize_name(buyer_username.lstrip("@"))
    output_zip = BUILT_PACKS_DIR / f"PACK_{fingerprint_id}_{safe_buyer}.zip"

    with tempfile.TemporaryDirectory(dir=str(TMP_DIR)) as tmp:
        tmp_dir = Path(tmp)
        extracted_dir = tmp_dir / "pack"
        extracted_dir.mkdir(parents=True, exist_ok=True)

        extract_zip_to(master_zip, extracted_dir)
        write_watermark(extracted_dir, hidden_filename, buyer_username, fingerprint_id)

        if include_visible_notice:
            write_visible_notice(extracted_dir, fingerprint_id)

        zip_folder(extracted_dir, output_zip)

    return fingerprint_id, output_zip


def inspect_zip_for_watermark(zip_path: Path) -> Optional[dict]:
    settings = load_settings()
    hidden_filename = settings.get("hidden_filename", DEFAULT_HIDDEN_FILENAME)
    if not hidden_filename.startswith("."):
        hidden_filename = "." + hidden_filename

    with zipfile.ZipFile(zip_path, "r") as zf:
        for member in zf.namelist():
            if Path(member).name == hidden_filename:
                with zf.open(member) as f:
                    raw = f.read().decode("utf-8", errors="replace")

                data: dict[str, str] = {}
                for line in raw.splitlines():
                    if "=" in line:
                        key, value = line.split("=", 1)
                        data[key.strip()] = value.strip()
                return data
    return None


def admin_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            ["/addpack", "/listpacks"],
            ["/deliver", "/buyers"],
            ["/inspect", "/settings"],
        ],
        resize_keyboard=True,
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.message
    if not user or not message:
        return

    db.upsert_buyer(
        chat_id=update.effective_chat.id,
        username=user.username,
        first_name=user.first_name or "",
        last_name=user.last_name,
    )

    if is_admin(user.id):
        await message.reply_text(
            "PackGuard admin ready.\n\n"
            "Commands:\n"
            "/addpack - upload a master pack ZIP\n"
            "/listpacks - show saved packs\n"
            "/deliver - build and send a buyer-specific pack\n"
            "/buyers - show saved buyers\n"
            "/inspect - inspect a leaked ZIP\n"
            "/settings - show current settings\n\n"
            "Buyers must start this bot once if you want auto-send by username.",
            reply_markup=admin_keyboard(),
        )
        return

    username = f"@{user.username}" if user.username else "no username set"
    await message.reply_text(
        "You are registered.\n"
        f"Username: {username}\n"
        f"Chat ID: {update.effective_chat.id}\n\n"
        "The seller can auto-send your pack after this.",
        reply_markup=ReplyKeyboardRemove(),
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.message
    if not user or not message:
        return

    if is_admin(user.id):
        await message.reply_text(
            "/start\n/addpack\n/listpacks\n/deliver\n/buyers\n/inspect\n/settings\n/cancel"
        )
    else:
        await message.reply_text(
            "Use /start once so the seller can auto-send your pack later."
        )


async def buyers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.message
    if not user or not message or not is_admin(user.id):
        return

    rows = db.list_buyers(30)
    if not rows:
        await message.reply_text("No buyers registered yet.")
        return

    lines = ["Saved buyers:"]
    for row in rows:
        uname = f"@{row['username']}" if row["username"] else "(no username)"
        lines.append(f"- {uname} | chat_id={row['chat_id']}")
    await message.reply_text("\n".join(lines))


async def listpacks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.message
    if not user or not message or not is_admin(user.id):
        return

    rows = db.list_packs()
    if not rows:
        await message.reply_text("No packs saved yet. Use /addpack.")
        return

    lines = ["Saved packs:"]
    for row in rows:
        lines.append(f"{row['id']}. {row['name']} | file={Path(row['file_path']).name}")
    await message.reply_text("\n".join(lines))


async def addpack_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    message = update.message
    if not user or not message or not is_admin(user.id):
        return ConversationHandler.END

    await message.reply_text("Send the pack name. Example: sngs_pack_v1")
    return ADD_PACK_NAME


async def addpack_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message = update.message
    if not message or not message.text:
        return ADD_PACK_NAME

    context.user_data["new_pack_name"] = message.text.strip()
    await message.reply_text("Now send the master pack as a ZIP document.")
    return ADD_PACK_FILE


async def addpack_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message = update.message
    if not message or not message.document:
        return ADD_PACK_FILE

    doc: Document = message.document
    filename = doc.file_name or "pack.zip"
    if not filename.lower().endswith(".zip"):
        await message.reply_text("Only ZIP files are supported for master packs.")
        return ADD_PACK_FILE

    pack_name = context.user_data.get("new_pack_name")
    if not pack_name:
        await message.reply_text("Pack name missing. Start again with /addpack.")
        return ConversationHandler.END

    safe_name = sanitize_name(pack_name)
    target_path = MASTER_PACKS_DIR / f"{safe_name}.zip"

    telegram_file = await doc.get_file()
    await telegram_file.download_to_drive(custom_path=str(target_path))

    db.add_or_replace_pack(safe_name, target_path, filename, update.effective_user.id)
    context.user_data.pop("new_pack_name", None)

    await message.reply_text(f"Pack saved: {safe_name}")
    return ConversationHandler.END


async def deliver_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    message = update.message
    if not user or not message or not is_admin(user.id):
        return ConversationHandler.END

    rows = db.list_packs()
    if not rows:
        await message.reply_text("No packs saved yet. Use /addpack first.")
        return ConversationHandler.END

    lines = ["Send the pack ID you want to deliver:"]
    for row in rows:
        lines.append(f"{row['id']}. {row['name']}")
    await message.reply_text("\n".join(lines))
    return DELIVER_PACK_ID


async def deliver_pack_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message = update.message
    if not message or not message.text:
        return DELIVER_PACK_ID

    try:
        pack_id = int(message.text.strip())
    except ValueError:
        await message.reply_text("Send a numeric pack ID.")
        return DELIVER_PACK_ID

    pack = db.get_pack_by_id(pack_id)
    if not pack:
        await message.reply_text("Pack ID not found.")
        return DELIVER_PACK_ID

    context.user_data["deliver_pack_id"] = pack_id
    await message.reply_text(
        "Send buyer username. Example: @buyername or buyername\n\n"
        "If that buyer already started the bot, it will auto-send to them too."
    )
    return DELIVER_BUYER_USERNAME


async def deliver_buyer_username(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message = update.message
    if not message or not message.text:
        return DELIVER_BUYER_USERNAME

    buyer_username_input = message.text.strip()
    normalized_username = buyer_username_input.lstrip("@").strip()
    if not normalized_username:
        await message.reply_text("Invalid username.")
        return DELIVER_BUYER_USERNAME

    pack_id = context.user_data.get("deliver_pack_id")
    if not pack_id:
        await message.reply_text("Pack selection missing. Start again with /deliver.")
        return ConversationHandler.END

    pack = db.get_pack_by_id(int(pack_id))
    if not pack:
        await message.reply_text("Pack not found.")
        return ConversationHandler.END

    master_zip = Path(pack["file_path"])
    if not master_zip.exists():
        await message.reply_text("Master ZIP file is missing on disk.")
        return ConversationHandler.END

    buyer_row = db.get_buyer_by_username(normalized_username)
    buyer_chat_id = int(buyer_row["chat_id"]) if buyer_row else None

    await message.chat.send_action(ChatAction.UPLOAD_DOCUMENT)

    try:
        fingerprint_id, output_zip = build_buyer_pack(master_zip, f"@{normalized_username}")
    except zipfile.BadZipFile:
        await message.reply_text("Master ZIP is invalid.")
        return ConversationHandler.END
    except Exception as e:
        logger.exception("Build failed")
        await message.reply_text(f"Build failed: {e}")
        return ConversationHandler.END

    db.save_delivery(
        pack_id=int(pack["id"]),
        buyer_username=normalized_username,
        buyer_chat_id=buyer_chat_id,
        fingerprint_id=fingerprint_id,
        output_zip_path=output_zip,
        admin_id=update.effective_user.id,
    )

    with output_zip.open("rb") as f:
        await message.reply_document(
            document=f,
            filename=output_zip.name,
            caption=(
                f"Pack built.\n"
                f"Pack: {pack['name']}\n"
                f"Buyer: @{normalized_username}\n"
                f"ID: {fingerprint_id}"
            ),
        )

    if buyer_chat_id:
        try:
            with output_zip.open("rb") as f:
                await context.bot.send_document(
                    chat_id=buyer_chat_id,
                    document=f,
                    filename=output_zip.name,
                    caption="Your pack is ready.",
                )
            await message.reply_text("Auto-send to buyer: success.")
        except Exception as e:
            await message.reply_text(f"Pack built, but auto-send failed: {e}")
    else:
        await message.reply_text(
            "Pack built. Buyer has not started the bot yet or username was not found, so auto-send was skipped."
        )

    context.user_data.pop("deliver_pack_id", None)
    return ConversationHandler.END


async def inspect_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    message = update.message
    if not user or not message or not is_admin(user.id):
        return ConversationHandler.END

    await message.reply_text("Send the leaked ZIP document to inspect.")
    return INSPECT_FILE


async def inspect_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message = update.message
    if not message or not message.document:
        return INSPECT_FILE

    doc: Document = message.document
    filename = doc.file_name or "leak.zip"
    if not filename.lower().endswith(".zip"):
        await message.reply_text("Only ZIP files are supported here.")
        return INSPECT_FILE

    target_path = TMP_DIR / f"inspect_{sanitize_name(filename)}"
    telegram_file = await doc.get_file()
    await telegram_file.download_to_drive(custom_path=str(target_path))

    try:
        data = inspect_zip_for_watermark(target_path)
    except zipfile.BadZipFile:
        await message.reply_text("Invalid ZIP file.")
        return ConversationHandler.END
    finally:
        try:
            target_path.unlink(missing_ok=True)
        except Exception:
            pass

    if not data:
        await message.reply_text("No hidden watermark found.")
        return ConversationHandler.END

    fingerprint_id = data.get("id", "unknown")
    delivery = db.find_delivery_by_fingerprint(fingerprint_id)

    lines = [
        "Watermark found:",
        f"ID: {data.get('id', 'unknown')}",
        f"Buyer: {data.get('buyer', 'unknown')}",
        f"License: {data.get('license', 'unknown')}",
        f"Source: {data.get('source', 'unknown')}",
    ]

    if delivery:
        lines.extend(
            [
                "",
                "Database match:",
                f"Pack ID: {delivery['pack_id']}",
                f"Buyer username: @{delivery['buyer_username']}",
                f"Delivered at: {delivery['delivered_at']}",
                f"Output file: {Path(delivery['output_zip_path']).name}",
            ]
        )

    await message.reply_text("\n".join(lines))
    return ConversationHandler.END


async def settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.message
    if not user or not message or not is_admin(user.id):
        return

    settings = load_settings()
    await message.reply_text(
        "Current settings:\n"
        f"hidden_filename = {settings.get('hidden_filename', DEFAULT_HIDDEN_FILENAME)}\n"
        f"include_visible_notice = {settings.get('include_visible_notice', False)}\n\n"
        "Edit data/settings.json while the bot is stopped if you want to change them."
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message:
        await update.message.reply_text("Cancelled.")
    context.user_data.pop("new_pack_name", None)
    context.user_data.pop("deliver_pack_id", None)
    return ConversationHandler.END


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled error", exc_info=context.error)


def build_application() -> Application:
    token = BOT_TOKEN.strip()
    if not token:
        raise RuntimeError("BOT_TOKEN is missing.")
    if not ADMIN_IDS:
        raise RuntimeError("ADMIN_IDS is missing.")

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("buyers", buyers))
    app.add_handler(CommandHandler("listpacks", listpacks))
    app.add_handler(CommandHandler("settings", settings_cmd))

    addpack_conv = ConversationHandler(
        entry_points=[CommandHandler("addpack", addpack_start)],
        states={
            ADD_PACK_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, addpack_name)],
            ADD_PACK_FILE: [MessageHandler(filters.Document.ALL & ~filters.COMMAND, addpack_file)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    deliver_conv = ConversationHandler(
        entry_points=[CommandHandler("deliver", deliver_start)],
        states={
            DELIVER_PACK_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, deliver_pack_id)],
            DELIVER_BUYER_USERNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, deliver_buyer_username)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    inspect_conv = ConversationHandler(
        entry_points=[CommandHandler("inspect", inspect_start)],
        states={
            INSPECT_FILE: [MessageHandler(filters.Document.ALL & ~filters.COMMAND, inspect_file)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(addpack_conv)
    app.add_handler(deliver_conv)
    app.add_handler(inspect_conv)
    app.add_error_handler(on_error)
    return app


async def run_bot() -> None:
    write_default_settings_if_missing()
    app = build_application()

    await app.initialize()
    await app.start()

    if app.updater is None:
        raise RuntimeError("Updater is not available.")

    await app.updater.start_polling(drop_pending_updates=True)
    logger.info("Bot started")

    try:
        await asyncio.Event().wait()
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()


def main() -> None:
    asyncio.run(run_bot())


if __name__ == "__main__":
    main()