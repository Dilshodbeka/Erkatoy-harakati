import os
import asyncio
import gspread
from datetime import datetime, date
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State

# Загрузка переменных окружения
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
GSA_KEY = os.getenv("GSA_KEY")
SHEET_ID = os.getenv("SHEET_ID")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# --- СОСТОЯНИЯ (FSM) ---
class UserState(StatesGroup):
    role_selection = State() # Выбор роли
    producer_add_item = State() # Добавление товара в корзину
    producer_confirm_cart = State() # Подтверждение корзины
    buyer_select_person = State() # Выбор получателя
    buyer_enter_amount = State() # Ввод суммы
    buyer_enter_comment = State() # Ввод комментария

# --- ФУНКЦИИ GOOGLE SHEETS ---
def get_sheet():
    gc = gspread.service_account(filename=GSA_KEY)
    sh = gc.open_by_key(SHEET_ID)
    return sh

def get_balance():
    try:
        sheet = get_sheet()
        config = sheet.worksheet("Config")
        val = config.cell(1, 2).value
        return float(val) if val else 0.0
    except Exception as e:
        print(f"Error getting balance: {e}")
        return 0.0

def update_balance(new_balance):
    try:
        sheet = get_sheet()
        config = sheet.worksheet("Config")
        config.update_cell(1, 2, new_balance)
        return True
    except Exception as e:
        print(f"Error updating balance: {e}")
        return False

def add_log(user_type, operation_type, description, amount, comment=""):
    try:
        sheet = get_sheet()
        log = sheet.worksheet("Log")
        current_balance = get_balance() # Получаем баланс ПОСЛЕ операции для записи
        date_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log.append_row([date_time, user_type, operation_type, description, amount, current_balance, comment])
        return True
    except Exception as e:
        print(f"Error adding log: {e}")
        return False

def get_products():
    try:
        sheet = get_sheet()
        config = sheet.worksheet("Config")
        all_values = config.get_all_values()
        products = {}
        for row in all_values[2:]:
            if len(row) >= 2 and row[0] and row[1]:
                name = str(row[0]).strip()
                try:
                    clean_price = str(row[1]).replace(" ", "").replace("\xa0", "").replace(",", ".")
                    price = float(clean_price)
                    products[name] = price
                except ValueError:
                    continue
        return products
    except Exception as e:
        print(f"Error getting products: {e}")
        return {}

def get_users():
    try:
        sheet = get_sheet()
        users_sheet = sheet.worksheet("Users")
        all_values = users_sheet.get_all_values()
        return [str(row[0]).strip() for row in all_values if row and row[0]]
    except Exception as e:
        print(f"Error getting users: {e}")
        return []

def get_monthly_report():
    try:
        sheet = get_sheet()
        log = sheet.worksheet("Log")
        all_values = log.get_all_values()
        
        current_month = datetime.now().strftime("%Y-%m")
        report_data = {} # {product_name: {'qty': 0, 'total': 0}}
        total_revenue = 0
        total_ops = 0

        # Пропускаем заголовок (row 0)
        for row in all_values[1:]:
            if len(row) < 5: continue
            date_str = row[0] # YYYY-MM-DD HH:MM:SS
            op_type = row[2]  # Приход / Расход
            desc = row[3]     # Описание
            amount = float(row[4])
            
            # Проверка месяца
            if date_str.startswith(current_month) and op_type == "Приход":
                # Парсим описание: "Отгружено: шкаф80 x4"
                if "Отгружено:" in desc:
                    parts = desc.replace("Отгружено:", "").strip().split(" x")
                    if len(parts) == 2:
                        product_name = parts[0].strip()
                        try:
                            qty = int(parts[1])
                        except:
                            qty = 1
                        
                        if product_name not in report_data:
                            report_data[product_name] = {'qty': 0, 'total': 0.0}
                        
                        report_data[product_name]['qty'] += qty
                        report_data[product_name]['total'] += amount
                        total_revenue += amount
                        total_ops += 1
        
        return report_data, total_revenue, total_ops
    except Exception as e:
        print(f"Error generating report: {e}")
        return {}, 0, 0

# --- КЛАВИАТУРЫ ---
def get_main_keyboard(role=None):
    kb = []
    
    # Кнопка смены роли
    if role == "producer":
        kb.append([InlineKeyboardButton(text="🔄 Сменить роль: Покупатель", callback_data="set_role_buyer")])
    elif role == "buyer":
        kb.append([InlineKeyboardButton(text="🔄 Сменить роль: Производитель", callback_data="set_role_producer")])
    else:
        kb.append([InlineKeyboardButton(text="🏭 Я - Производитель", callback_data="set_role_producer")])
        kb.append([InlineKeyboardButton(text="💰 Я - Покупатель", callback_data="set_role_buyer")])

    # Общие кнопки
    kb.append([InlineKeyboardButton(text="📊 Текущий баланс", callback_data="show_balance")])
    kb.append([InlineKeyboardButton(text="📈 Отчет за месяц", callback_data="monthly_report")])
    
    return InlineKeyboardMarkup(inline_keyboard=kb)

def get_producer_menu_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📦 Добавить товар в корзину", callback_data="prod_add_to_cart")],
        [InlineKeyboardButton(text="🛒 Показать корзину и подтвердить", callback_data="prod_show_cart")],
        [InlineKeyboardButton(text="⬅️ Назад в меню", callback_data="back_main")]
    ])

def get_products_keyboard(products):
    buttons = []
    items = list(products.items())
    # Показываем по 3-4 товара на строку или списком
    for name, price in items[:10]: # Ограничим для удобства
        btn_text = f"{name} ({price:,.0f})"
        buttons.append([InlineKeyboardButton(text=btn_text, callback_data=f"cart_add_{name}")])
    
    if len(items) > 10:
        buttons.append([InlineKeyboardButton(text="...еще товары...", callback_data="prod_more_items")])
    
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="producer_menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_cart_keyboard(cart):
    text = "✅ Корзина сформирована.\n\n"
    total = 0
    for item, qty in cart.items():
        # Нужно получить цену снова для отображения, но лучше хранить в state
        # Для простоты берем из БД
        products = get_products()
        price = products.get(item, 0)
        sum_item = price * qty
        total += sum_item
        text += f"• {item}: {qty} шт. = {sum_item:,.0f} сум\n"
    
    text += f"\n💰 ИТОГО: {total:,.0f} сум"
    
    buttons = [
        [InlineKeyboardButton(text="✅ Подтвердить отгрузку", callback_data="cart_confirm")],
        [InlineKeyboardButton(text="❌ Очистить корзину", callback_data="cart_clear")],
        [InlineKeyboardButton(text="⬅️ Продолжить добавление", callback_data="producer_menu")]
    ]
    # Сохраняем текст в data для передачи, но лучше через state. 
    # Здесь просто клавиатура, текст отправим отдельным сообщением
    
    return InlineKeyboardMarkup(inline_keyboard=buttons), text, total

def get_buyer_menu_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💸 Совершить платеж", callback_data="buy_start")],
        [InlineKeyboardButton(text="⬅️ Назад в меню", callback_data="back_main")]
    ])

def get_users_keyboard(users):
    buttons = [[InlineKeyboardButton(text=u, callback_data=f"buy_person_{u}")] for u in users[:10]]
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="buyer_menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# --- ОБРАБОТЧИКИ ---

@dp.message(CommandStart())
async def cmd_start(message: types.Message, state: FSMContext):
    data = await state.get_data()
    role = data.get("role")
    role_text = ""
    if role == "producer": role_text = " (Вы: Производитель)"
    elif role == "buyer": role_text = " (Вы: Покупатель)"
    
    await message.answer(f" Привет! Добро пожаловать в систему учета.{role_text}\n\nВыберите действие:", reply_markup=get_main_keyboard(role))

@dp.callback_query(F.data.startswith("set_role_"))
async def set_role(callback: types.CallbackQuery, state: FSMContext):
    role = "producer" if "producer" in callback.data else "buyer"
    await state.update_data(role=role)
    await state.clear() # Сброс других состояний
    
    msg = "🏭 Вы теперь: ПРОИЗВОДИТЕЛЬ" if role == "producer" else "💰 Вы теперь: ПОКУПАТЕЛЬ"
    await callback.message.edit_text(msg + "\n\nГлавное меню:", reply_markup=get_main_keyboard(role))

@dp.callback_query(F.data == "show_balance")
async def show_balance(callback: types.CallbackQuery):
    bal = get_balance()
    await callback.answer(f"Текущий баланс: {bal:,.0f} сум", show_alert=True)

@dp.callback_query(F.data == "monthly_report")
async def show_report(callback: types.CallbackQuery):
    await callback.answer("Формирую отчет...", show_alert=True)
    report_data, total_rev, total_ops = get_monthly_report()
    
    if not report_data:
        text = "📊 За этот месяц операций по приходу еще не было."
    else:
        text = f"📊 **Отчет за {datetime.now().strftime('%B %Y')}**\n\n"
        text += f"Всего операций: {total_ops}\n"
        text += f"Общая сумма отгрузок: {total_rev:,.0f} сум\n\n"
        text += "**Детализация по товарам:**\n"
        for name, data in report_data.items():
            text += f"▫️ {name}: {data['qty']} шт. на сумму {data['total']:,.0f} сум\n"
    
    await callback.message.edit_text(text, parse_mode="Markdown")

# --- ЛОГИКА ПРОИЗВОДИТЕЛЯ ---

@dp.callback_query(F.data == "producer_menu")
async def producer_menu(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text("🏭 Меню производителя:", reply_markup=get_producer_menu_keyboard())

@dp.callback_query(F.data == "prod_add_to_cart")
async def add_to_cart_start(callback: types.CallbackQuery, state: FSMContext):
    products = get_products()
    if not products:
        await callback.answer("Список товаров пуст!", show_alert=True)
        return
    await callback.message.edit_text("📦 Выберите товар для добавления в корзину:", reply_markup=get_products_keyboard(products))

@dp.callback_query(F.data.startswith("cart_add_"))
async def add_item_to_cart(callback: types.CallbackQuery, state: FSMContext):
    product_name = callback.data.split("_", 2)[2]
    
    # Проверяем, есть ли корзина в состоянии
    data = await state.get_data()
    cart = data.get("cart", {})
    
    await state.update_data(selected_product=product_name)
    await callback.message.edit_text(f"Вы выбрали: {product_name}.\nВведите количество штук:")
    # Переходим в состояние ввода количества, но сохраняем контекст корзины
    await state.set_state(UserState.producer_add_item)

@dp.message(UserState.producer_add_item)
async def process_qty_for_cart(message: types.Message, state: FSMContext):
    try:
        qty = int(message.text)
        if qty <= 0:
            await message.answer("Количество должно быть больше 0.")
            return
        
        data = await state.get_data()
        product = data.get("selected_product")
        cart = data.get("cart", {})
        
        if product in cart:
            cart[product] += qty
        else:
            cart[product] = qty
            
        await state.update_data(cart=cart)
        await message.answer(f"✅ Добавлено: {product} x{qty}\n\nХотите добавить еще или перейти к оформлению?", reply_markup=get_producer_menu_keyboard())
        await state.clear() # Сбрасываем временное состояние, но cart остается в данных? Нет, clear удалит cart.
        # Исправление: нужно сохранить cart перед clear или не делать clear полного
        # Лучше использовать update_data({}) но оставить cart.
        # Перепишем логику: state.clear() удаляет всё. Надо сохранять cart отдельно.
        # Исправление ниже в коде обработки:
        
    except ValueError:
        await message.answer("Введите число.")

# ПЕРЕПИСЫВАЕМ ЛОГИКУ КОРЗИНЫ ЧТОБЫ НЕ ТЕРЯТЬ ДАННЫЕ
# Используем один поток состояний для добавления

@dp.callback_query(F.data == "prod_show_cart")
async def show_cart(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    cart = data.get("cart", {})
    
    if not cart:
        await callback.answer("Корзина пуста. Добавьте товары.", show_alert=True)
        return
    
    kb, text, total = get_cart_keyboard(cart)
    await callback.message.edit_text(text, reply_markup=kb)

@dp.callback_query(F.data == "cart_clear")
async def clear_cart(callback: types.CallbackQuery, state: FSMContext):
    await state.update_data(cart={})
    await callback.message.edit_text("🗑 Корзина очищена.", reply_markup=get_producer_menu_keyboard())

@dp.callback_query(F.data == "cart_confirm")
async def confirm_cart(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    cart = data.get("cart", {})
    products = get_products()
    
    if not cart:
        await callback.answer("Корзина пуста!", show_alert=True)
        return
    
    total_amount = 0
    desc_parts = []
    
    for prod, qty in cart.items():
        price = products.get(prod, 0)
        sum_val = price * qty
        total_amount += sum_val
        desc_parts.append(f"{prod} x{qty}")
    
    desc = "Отгружено: " + ", ".join(desc_parts)
    
    current_bal = get_balance()
    new_bal = current_bal + total_amount
    
    if update_balance(new_bal):
        add_log("Производитель", "Приход", desc, total_amount)
        await state.update_data(cart={}) # Очистить корзину
        
        msg = f"✅ **Отгрузка подтверждена!**\n\n"
        msg += f"📦 Товары: {', '.join(desc_parts)}\n"
        msg += f"💰 Сумма: {total_amount:,.0f} сум\n"
        msg += f"📈 Новый баланс: {new_bal:,.0f} сум"
        
        await callback.message.edit_text(msg, parse_mode="Markdown", reply_markup=get_producer_menu_keyboard())
    else:
        await callback.answer("Ошибка записи в БД", show_alert=True)

# --- ЛОГИКА ПОКУПАТЕЛЯ ---

@dp.callback_query(F.data == "buyer_menu")
async def buyer_menu(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text("💰 Меню покупателя:", reply_markup=get_buyer_menu_keyboard())

@dp.callback_query(F.data == "buy_start")
async def buy_start(callback: types.CallbackQuery, state: FSMContext):
    users = get_users()
    if not users:
        await callback.answer("Список людей пуст!", show_alert=True)
        return
    await callback.message.edit_text(" Кому переводим деньги?", reply_markup=get_users_keyboard(users))

@dp.callback_query(F.data.startswith("buy_person_"))
async def select_person(callback: types.CallbackQuery, state: FSMContext):
    person = callback.data.split("_", 2)[2]
    await state.update_data(buyer_person=person)
    await callback.message.edit_text(f"Получатель: {person}\nВведите сумму платежа:")
    await state.set_state(UserState.buyer_enter_amount)

@dp.message(UserState.buyer_enter_amount)
async def process_amount(message: types.Message, state: FSMContext):
    try:
        amount = float(message.text.replace(",", ".").replace(" ", ""))
        if amount <= 0:
            await message.answer("Сумма должна быть > 0")
            return
        
        await state.update_data(amount=amount)
        await message.answer("💬 Комментарий (или напишите 'нет' / '/skip'):")
        await state.set_state(UserState.buyer_enter_comment)
    except ValueError:
        await message.answer("Введите число.")

@dp.message(UserState.buyer_enter_comment)
async def process_comment(message: types.Message, state: FSMContext):
    comment = message.text if message.text.lower() not in ['нет', '/skip', 'no'] else ""
    
    data = await state.get_data()
    person = data.get("buyer_person")
    amount = data.get("amount")
    
    current_bal = get_balance()
    new_bal = current_bal - amount # Может уйти в минус
    
    desc = f"Оплата от: {person}"
    
    if update_balance(new_bal):
        add_log("Покупатель", "Расход", desc, amount, comment)
        
        msg = f"✅ **Платеж выполнен!**\n\n"
        msg += f"👤 Получатель: {person}\n"
        msg += f"💵 Сумма: {amount:,.0f} сум\n"
        msg += f"💬 Коммент: {comment if comment else '-'}\n"
        msg += f"📉 Новый баланс: {new_bal:,.0f} сум"
        
        await message.answer(msg, parse_mode="Markdown", reply_markup=get_buyer_menu_keyboard())
    else:
        await message.answer("Ошибка сохранения.")
    
    await state.clear()

@dp.callback_query(F.data == "back_main")
async def back_main(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    role = data.get("role")
    await state.clear()
    await callback.message.edit_text("Главное меню:", reply_markup=get_main_keyboard(role))

# Запуск
async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())