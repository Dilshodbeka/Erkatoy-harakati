import os
import asyncio
import gspread
import time
from datetime import datetime
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
GSA_KEY = os.getenv("GSA_KEY")
SHEET_ID = os.getenv("SHEET_ID")
NOTIFY_CHAT_ID = os.getenv("NOTIFY_CHAT_ID")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

PRODUCTS_PAGE_SIZE = 20
PRODUCTS_PER_ROW = 2

# ─────────────────────────────────────────────
# ОДНО ГЛОБАЛЬНОЕ ПОДКЛЮЧЕНИЕ К GOOGLE SHEETS
# Создаётся один раз при старте, переиспользуется всегда
# ─────────────────────────────────────────────
_gc: gspread.Client | None = None
_sh = None  # открытая таблица

def _get_sh():
    """Возвращает объект таблицы, переподключается если сессия истекла."""
    global _gc, _sh
    if _gc is None or _sh is None:
        _gc = gspread.service_account(filename=GSA_KEY)
        _sh = _gc.open_by_key(SHEET_ID)
    return _sh

def _worksheet(name: str):
    """Получить лист по имени с автоматическим переподключением."""
    try:
        return _get_sh().worksheet(name)
    except Exception:
        # Сбрасываем соединение и пробуем ещё раз
        global _gc, _sh
        _gc = None
        _sh = None
        return _get_sh().worksheet(name)


# ─────────────────────────────────────────────
# КЭШ ТОВАРОВ (обновляется раз в 5 минут)
# Товары меняются редко — незачем каждый раз дёргать Sheets
# ─────────────────────────────────────────────
_products_cache: dict = {}
_products_cache_time: float = 0
PRODUCTS_CACHE_TTL = 300  # секунд (5 минут)

def _load_products_sync() -> dict:
    """Загрузить товары из Sheets (синхронно, вызывается через asyncio.to_thread)."""
    ws = _worksheet("Config")
    all_values = ws.get_all_values()
    products = {}
    for row in all_values[2:]:
        if len(row) >= 2 and row[0] and row[1]:
            name = str(row[0]).strip()
            try:
                clean = str(row[1]).replace(" ", "").replace("\xa0", "").replace(",", ".")
                products[name] = float(clean)
            except ValueError:
                continue
    return products

async def get_products() -> dict:
    """Возвращает товары из кэша или обновляет его если кэш устарел."""
    global _products_cache, _products_cache_time
    now = time.monotonic()
    if now - _products_cache_time > PRODUCTS_CACHE_TTL or not _products_cache:
        try:
            _products_cache = await asyncio.to_thread(_load_products_sync)
            _products_cache_time = now
        except Exception as e:
            print(f"Error loading products: {e}")
    return _products_cache

def invalidate_products_cache():
    """Сбросить кэш товаров принудительно (если товары изменились)."""
    global _products_cache_time
    _products_cache_time = 0


# ─────────────────────────────────────────────
# ФУНКЦИИ SHEETS — все через asyncio.to_thread
# Не блокируют event loop бота
# ─────────────────────────────────────────────

def _get_balance_sync() -> float:
    ws = _worksheet("Config")
    val = ws.cell(1, 2).value
    return float(val) if val else 0.0

async def get_balance() -> float:
    try:
        return await asyncio.to_thread(_get_balance_sync)
    except Exception as e:
        print(f"Error getting balance: {e}")
        return 0.0


def _update_balance_sync(new_balance: float) -> bool:
    ws = _worksheet("Config")
    ws.update_cell(1, 2, new_balance)
    return True

async def update_balance(new_balance: float) -> bool:
    try:
        return await asyncio.to_thread(_update_balance_sync, new_balance)
    except Exception as e:
        print(f"Error updating balance: {e}")
        return False


def _add_log_sync(user_type, operation_type, description, amount, comment, balance_after):
    ws = _worksheet("Log")
    date_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ws.append_row([date_time, user_type, operation_type, description, amount, balance_after, comment])

async def add_log(user_type, operation_type, description, amount, comment="", balance_after=0.0):
    try:
        await asyncio.to_thread(_add_log_sync, user_type, operation_type, description, amount, comment, balance_after)
    except Exception as e:
        print(f"Error adding log: {e}")


def _get_users_sync() -> list:
    ws = _worksheet("Users")
    all_values = ws.get_all_values()
    return [str(row[0]).strip() for row in all_values if row and row[0]]

async def get_users() -> list:
    try:
        return await asyncio.to_thread(_get_users_sync)
    except Exception as e:
        print(f"Error getting users: {e}")
        return []


def _get_monthly_report_sync(products: dict):
    ws = _worksheet("Log")
    all_values = ws.get_all_values()
    current_month = datetime.now().strftime("%Y-%m")

    income_data = {}
    expense_data = {}
    total_income = 0.0
    total_expense = 0.0

    for row in all_values[1:]:
        if len(row) < 5:
            continue
        date_str = str(row[0])
        op_type = str(row[2])
        desc = str(row[3])
        try:
            amount = float(str(row[4]).replace(" ", "").replace(",", "."))
        except ValueError:
            continue

        if not date_str.startswith(current_month):
            continue

        if op_type == "Приход" and "Отгружено:" in desc:
            items_str = desc.replace("Отгружено:", "").strip()
            row_total = 0.0
            parsed_items = []

            for item in [i.strip() for i in items_str.split(",")]:
                parts = item.rsplit(" x", 1)
                if len(parts) == 2:
                    p_name = parts[0].strip()
                    try:
                        qty = int(parts[1])
                    except ValueError:
                        qty = 1
                    price = products.get(p_name, 0.0)
                    item_sum = price * qty
                    row_total += item_sum
                    parsed_items.append((p_name, qty, item_sum))

            for p_name, qty, item_sum in parsed_items:
                if p_name not in income_data:
                    income_data[p_name] = {"qty": 0, "total": 0.0}
                income_data[p_name]["qty"] += qty
                income_data[p_name]["total"] += item_sum if row_total > 0 else amount

            total_income += amount

        elif op_type == "Расход" and "Оплата от:" in desc:
            person = desc.replace("Оплата от:", "").strip()
            expense_data[person] = expense_data.get(person, 0.0) + amount
            total_expense += amount

    return income_data, expense_data, total_income, total_expense

async def get_monthly_report():
    try:
        products = await get_products()
        return await asyncio.to_thread(_get_monthly_report_sync, products)
    except Exception as e:
        print(f"Error generating report: {e}")
        return {}, {}, 0.0, 0.0


# ─────────────────────────────────────────────
# УВЕДОМЛЕНИЯ
# ─────────────────────────────────────────────
async def send_notification(text: str):
    if not NOTIFY_CHAT_ID:
        return
    try:
        await bot.send_message(chat_id=NOTIFY_CHAT_ID, text=text, parse_mode="Markdown")
    except Exception as e:
        print(f"Error sending notification: {e}")


# ─────────────────────────────────────────────
# FSM
# ─────────────────────────────────────────────
class UserState(StatesGroup):
    role_selection = State()
    producer_add_item = State()
    producer_add_qty = State()
    buyer_select_person = State()
    buyer_enter_amount = State()
    buyer_enter_comment = State()

async def safe_exit_state(state: FSMContext):
    await state.set_state(None)


# ─────────────────────────────────────────────
# КЛАВИАТУРЫ
# ─────────────────────────────────────────────
def get_role_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏭 Men - Aliya mebel", callback_data="set_role_producer")],
        [InlineKeyboardButton(text="💰 Men - Erkatoy", callback_data="set_role_buyer")],
    ])

def get_producer_menu_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📦 Tovar qo'shish", callback_data="prod_add_page_0")],
        [InlineKeyboardButton(text="🛒 Korzina / Jami", callback_data="prod_show_cart")],
        [InlineKeyboardButton(text="📊 Balans", callback_data="show_balance")],
        [InlineKeyboardButton(text="📈 Oylik hisobot", callback_data="monthly_report")],
    ])

def get_buyer_menu_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💸 To'lov qilish", callback_data="buy_start")],
        [InlineKeyboardButton(text="📊 Balans", callback_data="show_balance")],
        [InlineKeyboardButton(text="📈 Oylik hisobot", callback_data="monthly_report")],
    ])

def get_products_keyboard(products: dict, page: int = 0):
    items = list(products.items())
    total = len(items)
    start = page * PRODUCTS_PAGE_SIZE
    end = min(start + PRODUCTS_PAGE_SIZE, total)
    page_items = items[start:end]

    buttons = []
    row = []
    for name, price in page_items:
        row.append(InlineKeyboardButton(
            text=f"{name} ({price:,.0f})",
            callback_data=f"cart_add_{name}"
        ))
        if len(row) == PRODUCTS_PER_ROW:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️ Oldingi.", callback_data=f"prod_add_page_{page - 1}"))
    if end < total:
        nav.append(InlineKeyboardButton(text="➡️ Keyingi.", callback_data=f"prod_add_page_{page + 1}"))
    if nav:
        buttons.append(nav)

    total_pages = (total + PRODUCTS_PAGE_SIZE - 1) // PRODUCTS_PAGE_SIZE
    buttons.append([InlineKeyboardButton(
        text=f"❌ Bekor qilish  ({page + 1}/{total_pages}, всего {total})",
        callback_data="producer_menu"
    )])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_cart_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ tasdiqlash", callback_data="cart_confirm")],
        [InlineKeyboardButton(text="➕ Yana tovar qo'shish", callback_data="prod_add_page_0")],
        [InlineKeyboardButton(text="🗑 Korzina tozalash", callback_data="cart_clear")],
        [InlineKeyboardButton(text="❌ Bekor qilish", callback_data="producer_menu")],
    ])

def build_cart_text(cart: dict, products: dict):
    text = "🛒 *Sizning korzina:*\n\n"
    total = 0.0
    for item, qty in cart.items():
        price = products.get(item, 0)
        sum_item = price * qty
        total += sum_item
        text += f"• {item}: {qty} shtk. × {price:,.0f} = *{sum_item:,.0f} sum*\n"
    text += f"\n*Jami: {total:,.0f} sum*"
    return text, total

def get_users_keyboard(users: list):
    buttons = [[InlineKeyboardButton(text=u, callback_data=f"buy_person_{u}")] for u in users]
    buttons.append([InlineKeyboardButton(text="❌ Bekor qilish", callback_data="buyer_menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_cancel_keyboard(back_callback: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Bekor qilish", callback_data=back_callback)]
    ])


# ─────────────────────────────────────────────
# ОБРАБОТЧИКИ
# ─────────────────────────────────────────────

@dp.message(CommandStart())
async def cmd_start(message: types.Message, state: FSMContext):
    data = await state.get_data()
    role = data.get("role")
    if role == "producer":
        await message.answer("🏭 Menya aliya mebel:", reply_markup=get_producer_menu_keyboard())
    elif role == "buyer":
        await message.answer("💰 Menya Erkatoy:", reply_markup=get_buyer_menu_keyboard())
    else:
        await message.answer("Xush kelibsiz! Tanlang:", reply_markup=get_role_keyboard())
        await state.set_state(UserState.role_selection)


@dp.callback_query(F.data.startswith("set_role_"), UserState.role_selection)
async def set_role(callback: types.CallbackQuery, state: FSMContext):
    role = "producer" if "producer" in callback.data else "buyer"
    await state.update_data(role=role, cart={})
    await state.set_state(None)
    if role == "producer":
        await callback.message.edit_text(
            "🏭 Siz: *Aliya mebel*\n\nМеню:", parse_mode="Markdown",
            reply_markup=get_producer_menu_keyboard()
        )
    else:
        await callback.message.edit_text(
            "💰 Siz: *Erkatoy*\n\nМеню:", parse_mode="Markdown",
            reply_markup=get_buyer_menu_keyboard()
        )


@dp.callback_query(F.data == "show_balance")
async def show_balance(callback: types.CallbackQuery):
    bal = await get_balance()
    await callback.answer(f"Hozirgi balans: {bal:,.0f} sum", show_alert=True)


@dp.callback_query(F.data == "monthly_report")
async def show_report(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer("Формирую отчёт...", show_alert=False)
    income_data, expense_data, total_income, total_expense = await get_monthly_report()
    month_label = datetime.now().strftime("%B %Y")
    balance = await get_balance()

    text = f"📊 *Hisobot {month_label} oyi*\n\n"
    if income_data:
        text += "📦 *Отгрузки (приход):*\n"
        for name, d in income_data.items():
            text += f"  ▫️ {name}: {d['qty']} шт. — {d['total']:,.0f} sum\n"
        text += f"  *Umumiy kirim: {total_income:,.0f} sum*\n\n"
    else:
        text += "📦 Отгрузок за месяц не было.\n\n"

    if expense_data:
        text += "💸 *Платежи (расход):*\n"
        for person, total in expense_data.items():
            text += f"  👤 {person}: {total:,.0f} сум\n"
        text += f"  *Итого расход: {total_expense:,.0f} сум*\n\n"
    else:
        text += "💸 Платежей за месяц не было.\n\n"

    net = total_income - total_expense
    text += f"💹 *Чистый оборот: {net:,.0f} сум*\n"
    text += f"📈 *Текущий баланс: {balance:,.0f} сум*"

    data = await state.get_data()
    role = data.get("role", "producer")
    back_kb = get_producer_menu_keyboard() if role == "producer" else get_buyer_menu_keyboard()
    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=back_kb)


# --- ПРОИЗВОДИТЕЛЬ ---

@dp.callback_query(F.data == "producer_menu")
async def producer_menu(callback: types.CallbackQuery, state: FSMContext):
    await safe_exit_state(state)
    await callback.message.edit_text("🏭 Меню производителя:", reply_markup=get_producer_menu_keyboard())


@dp.callback_query(F.data.startswith("prod_add_page_"))
async def add_to_cart_page(callback: types.CallbackQuery, state: FSMContext):
    page = int(callback.data.split("_")[-1])
    products = await get_products()
    if not products:
        await callback.answer("Список товаров пуст!", show_alert=True)
        return
    kb = get_products_keyboard(products, page)
    try:
        await callback.message.edit_text(f"📦 Выберите товар (всего {len(products)}):", reply_markup=kb)
    except Exception:
        await callback.answer()
    await state.set_state(UserState.producer_add_item)


@dp.callback_query(F.data.startswith("cart_add_"), UserState.producer_add_item)
async def select_product(callback: types.CallbackQuery, state: FSMContext):
    product_name = callback.data.split("_", 2)[2]
    products = await get_products()
    price = products.get(product_name, 0)
    await state.update_data(selected_product=product_name)
    await callback.message.edit_text(
        f"📦 Выбран: *{product_name}*\n"
        f"💰 Цена: {price:,.0f} сум/шт.\n\n"
        f"Введите количество штук:",
        parse_mode="Markdown",
        reply_markup=get_cancel_keyboard("producer_menu")
    )
    await state.set_state(UserState.producer_add_qty)


@dp.message(UserState.producer_add_qty)
async def process_qty_for_cart(message: types.Message, state: FSMContext):
    try:
        qty = int(message.text.strip())
        if qty <= 0:
            await message.answer("Количество должно быть > 0.", reply_markup=get_cancel_keyboard("producer_menu"))
            return

        data = await state.get_data()
        product = data.get("selected_product")
        cart = data.get("cart", {})
        products = await get_products()
        price = products.get(product, 0)
        sum_val = price * qty

        cart[product] = cart.get(product, 0) + qty
        await state.update_data(cart=cart)
        await safe_exit_state(state)

        await message.answer(
            f"✅ Добавлено в корзину:\n"
            f"📦 *{product}* × {qty} шт.\n"
            f"💰 {price:,.0f} × {qty} = *{sum_val:,.0f} сум*\n\n"
            f"Что делаем дальше?",
            parse_mode="Markdown",
            reply_markup=get_producer_menu_keyboard()
        )
    except ValueError:
        await message.answer("Введите целое число.", reply_markup=get_cancel_keyboard("producer_menu"))


@dp.callback_query(F.data == "prod_show_cart")
async def show_cart(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    cart = data.get("cart", {})
    if not cart:
        await callback.answer("Корзина пуста. Добавьте товары.", show_alert=True)
        return
    products = await get_products()
    text, _ = build_cart_text(cart, products)
    await callback.message.edit_text(text, reply_markup=get_cart_keyboard(), parse_mode="Markdown")


@dp.callback_query(F.data == "cart_clear")
async def clear_cart(callback: types.CallbackQuery, state: FSMContext):
    await state.update_data(cart={})
    await callback.message.edit_text("🗑 Корзина очищена.", reply_markup=get_producer_menu_keyboard())


@dp.callback_query(F.data == "cart_confirm")
async def confirm_cart(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    cart = data.get("cart", {})
    if not cart:
        await callback.answer("Корзина пуста!", show_alert=True)
        return

    products = await get_products()
    total_amount = 0.0
    desc_parts = []
    notify_lines = []

    for prod, qty in cart.items():
        price = products.get(prod, 0)
        sum_val = price * qty
        total_amount += sum_val
        desc_parts.append(f"{prod} x{qty}")
        notify_lines.append(f"  📦 {prod}: {qty} шт. × {price:,.0f} = *{sum_val:,.0f} сум*")

    desc = "Отгружено: " + ", ".join(desc_parts)

    # Баланс и лог обновляем параллельно одним запросом
    current_bal = await get_balance()
    new_bal = current_bal + total_amount

    if await update_balance(new_bal):
        await add_log("Производитель", "Приход", desc, total_amount, balance_after=new_bal)
        await state.update_data(cart={})

        now = datetime.now().strftime("%d.%m.%Y %H:%M")
        items_text = "\n".join(notify_lines)

        await callback.message.edit_text(
            f"✅ *Yuklatib yuborish tasdiqlandi!*\n\n"
            f"{items_text}\n\n"
            f"💰 *Jami: {total_amount:,.0f} sum*\n"
            f"📈 Yangi balans: {new_bal:,.0f} sum",
            parse_mode="Markdown",
            reply_markup=get_producer_menu_keyboard()
        )
        await send_notification(
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🏭 *Yuklatilgan tovarlar*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🕐 {now}\n\n"
            f"{items_text}\n\n"
            f"💰 *Jami: {total_amount:,.0f} sum*\n"
            f"📈 Balans: {new_bal:,.0f} sum\n"
            f"━━━━━━━━━━━━━━━━━━━━"
        )
    else:
        await callback.answer("Ошибка записи в БД", show_alert=True)


# --- ПОКУПАТЕЛЬ ---

@dp.callback_query(F.data == "buyer_menu")
async def buyer_menu(callback: types.CallbackQuery, state: FSMContext):
    await safe_exit_state(state)
    await callback.message.edit_text("💰 Меню покупателя:", reply_markup=get_buyer_menu_keyboard())


@dp.callback_query(F.data == "buy_start")
async def buy_start(callback: types.CallbackQuery, state: FSMContext):
    users = await get_users()
    if not users:
        await callback.answer("Список людей пуст!", show_alert=True)
        return
    await callback.message.edit_text("Кому переводим деньги?", reply_markup=get_users_keyboard(users))
    await state.set_state(UserState.buyer_select_person)


@dp.callback_query(F.data.startswith("buy_person_"), UserState.buyer_select_person)
async def select_person(callback: types.CallbackQuery, state: FSMContext):
    person = callback.data.split("_", 2)[2]
    await state.update_data(buyer_person=person)
    await callback.message.edit_text(
        f"👤 Получатель: *{person}*\n\nВведите сумму платежа (сум):",
        parse_mode="Markdown",
        reply_markup=get_cancel_keyboard("buyer_menu")
    )
    await state.set_state(UserState.buyer_enter_amount)


@dp.message(UserState.buyer_enter_amount)
async def process_amount(message: types.Message, state: FSMContext):
    try:
        amount = float(message.text.replace(",", ".").replace(" ", ""))
        if amount <= 0:
            await message.answer("Сумма должна быть > 0", reply_markup=get_cancel_keyboard("buyer_menu"))
            return
        await state.update_data(amount=amount)
        await message.answer(
            "💬 Введите комментарий (или `-` чтобы пропустить):",
            reply_markup=get_cancel_keyboard("buyer_menu")
        )
        await state.set_state(UserState.buyer_enter_comment)
    except ValueError:
        await message.answer("Введите число.", reply_markup=get_cancel_keyboard("buyer_menu"))


@dp.message(UserState.buyer_enter_comment)
async def process_comment(message: types.Message, state: FSMContext):
    skip_words = {"нет", "/skip", "no", "-", "–", "."}
    comment = "" if message.text.strip().lower() in skip_words else message.text.strip()

    data = await state.get_data()
    person = data.get("buyer_person")
    amount = data.get("amount")

    current_bal = await get_balance()
    new_bal = current_bal - amount
    desc = f"Оплата от: {person}"

    if await update_balance(new_bal):
        await add_log("Покупатель", "Расход", desc, amount, comment, balance_after=new_bal)
        await safe_exit_state(state)

        now = datetime.now().strftime("%d.%m.%Y %H:%M")
        await message.answer(
            f"✅ *Платёж выполнен!*\n\n"
            f"👤 Получатель: *{person}*\n"
            f"💵 Сумма: *{amount:,.0f} сум*\n"
            f"💬 Комментарий: {comment if comment else '—'}\n"
            f"📉 Новый баланс: {new_bal:,.0f} сум",
            parse_mode="Markdown",
            reply_markup=get_buyer_menu_keyboard()
        )
        await send_notification(
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"💸 *To'lov bajarildi*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🕐 {now}\n\n"
            f"👤 Qabul qiluvchi: *{person}*\n"
            f"💵 Summa: *{amount:,.0f} sum*\n"
            f"💬 Izoh: {comment if comment else '—'}\n\n"
            f"📉 Balans: {new_bal:,.0f} sum\n"
            f"━━━━━━━━━━━━━━━━━━━━"
        )
    else:
        await message.answer("Ошибка сохранения.", reply_markup=get_buyer_menu_keyboard())
        await safe_exit_state(state)


# ─────────────────────────────────────────────
# ЗАПУСК
# ─────────────────────────────────────────────
async def main():
    # Прогреваем подключение и кэш товаров при старте
    print("Connecting to Google Sheets...")
    try:
        await get_products()
        print(f"✅ Connected. Products loaded: {len(_products_cache)}")
    except Exception as e:
        print(f"⚠️ Sheets connection warning: {e}")

    print("Bot started.")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
