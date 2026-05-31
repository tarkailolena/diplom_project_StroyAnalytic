import os
import sqlalchemy as sa
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from dotenv import load_dotenv
from openai import OpenAI
import pandas as pd
import asyncio
import re
import joblib
import sys

sys.stdout.reconfigure(line_buffering=True)

load_dotenv()
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENROUTER_KEY = os.getenv("OPENROUTER_API_KEY")

print("🚀 Бот запускается...")
print(f"DB_HOST = {os.getenv('DB_HOST', 'localhost')}")
print(f"TOKEN = {'*' * 10 if TOKEN else 'MISSING'}")
print(f"OPENROUTER_KEY = {'*' * 10 if OPENROUTER_KEY else 'MISSING'}")

bot = Bot(token=TOKEN)
dp = Dispatcher()

db_host = os.getenv('DB_HOST', 'localhost')
engine = sa.create_engine(f'postgresql://postgres:postgres@{db_host}:5432/stroy_db')

client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_KEY,
)

try:
    model = joblib.load('data/gradient_boosting_model.pkl')
    model_loaded = True
    print("✅ Модель ML загружена (4 признака)")
except Exception as e:
    model_loaded = False
    print(f"⚠️ Модель не загружена: {e}")

# Базовые команды

@dp.message(Command("start"))
async def start(message: types.Message):
    await message.answer(
        "🏗 Бот строительной аналитики (RAG)\n"
        "Команды:\n"
        "/profit ID – прибыль и рентабельность\n"
        "/costs ID – все группы затрат (с процентами)\n"
        "/ask вопрос – ответ на основе данных (AI)\n"
        "/top_profit – самый прибыльный объект\n"
        "/bottom_profit – самый убыточный объект\n"
        "/topcodes [ID] [N] – топ-N самых больших кодов затрат\n"
        "/bottomcodes [ID] [N] – топ-N самых маленьких кодов затрат\n"
        "/cluster ID – кластер объекта\n"
        "/predict_new – предсказать по Excel\n"
        "/predict_new_values – предсказать по 4 долям\n"
        "/dashboard – ссылка на дашборд\n\n"
        "📎 Отправьте Excel-файл с листами:\n"
        "- `fact_transactions` (expense_id, value_fact)\n"
        "- `dim_expenses` (expense_id, group_name)\n"
        "- `dim_objects` (contract_price_fact)\n"
        "Бот сам вычислит доли, предскажет кластер и покажет полный отчёт."
    )

@dp.message(Command("dashboard"))
async def dashboard_link(message: types.Message):
    # ЗДЕСЬ ЗАМЕНА ССЫЛКИ НА ОБЛАЧНЫЙ ДАШБОРД
    await message.answer("📊 Интерактивный дашборд по ссылке:\nhttps://diplomprojectstroyanalytic-4ybc87gwgln2ncnvpn6fpn.streamlit.app")

@dp.message(Command("profit"))
async def profit(message: types.Message):
    parts = message.text.split()
    if len(parts) < 2:
        await message.answer("Пример: /profit 210616_1")
        return
    obj_id = parts[1]
    with engine.connect() as conn:
        row = conn.execute(
            sa.text("SELECT profit_plan, profit_fact, cost_price_fact FROM dim_objects WHERE object_id = :oid"),
            {"oid": obj_id}
        ).first()
    if row:
        profit_plan, profit_fact, cost_price_fact = row
        ros = (profit_fact / cost_price_fact * 100) if cost_price_fact > 0 else 0.0
        await message.answer(
            f"Объект {obj_id}\n"
            f"💰 Прибыль план: {profit_plan:,.0f} руб.\n"
            f"💰 Прибыль факт: {profit_fact:,.0f} руб.\n"
            f"📈 Рентабельность (факт): {ros:.2f}%"
        )
    else:
        await message.answer("Объект не найден.")

@dp.message(Command("costs"))
async def costs(message: types.Message):
    parts = message.text.split()
    if len(parts) < 2:
        await message.answer("Пример: /costs 210616_1")
        return
    obj_id = parts[1]
    with engine.connect() as conn:
        total_row = conn.execute(
            sa.text("SELECT SUM(value_fact) as total FROM fact_transactions WHERE object_id = :oid"),
            {"oid": obj_id}
        ).first()
        total = total_row.total if total_row.total else 0
        rows = conn.execute(
            sa.text("""
                SELECT e.group_name, SUM(f.value_fact) as total
                FROM fact_transactions f
                JOIN dim_expenses e ON f.expense_id = e.expense_id
                WHERE f.object_id = :oid
                GROUP BY e.group_name
                ORDER BY total DESC
            """),
            {"oid": obj_id}
        ).fetchall()
    if rows:
        text = f"📋 Все группы затрат для {obj_id}:\n"
        text += f"💰 Общие расходы: {total:,.0f} руб.\n\n"
        for name, amount in rows:
            percent = (amount / total * 100) if total > 0 else 0
            text += f"• {name}: {amount:,.0f} руб. ({percent:.1f}%)\n"
        await message.answer(text)
    else:
        await message.answer("Нет данных.")

@dp.message(Command("top_profit"))
async def top_profit(message: types.Message):
    with engine.connect() as conn:
        row = conn.execute(sa.text("SELECT object_id, profit_fact FROM dim_objects ORDER BY profit_fact DESC LIMIT 1")).first()
        if row:
            await message.answer(f"🏆 Самый прибыльный объект: {row[0]} с прибылью {row[1]:,.0f} руб.")
        else:
            await message.answer("Нет данных.")

@dp.message(Command("bottom_profit"))
async def bottom_profit(message: types.Message):
    with engine.connect() as conn:
        row = conn.execute(sa.text("SELECT object_id, profit_fact FROM dim_objects ORDER BY profit_fact ASC LIMIT 1")).first()
        if row:
            await message.answer(f"📉 Самый убыточный объект: {row[0]} с прибылью {row[1]:,.0f} руб.")
        else:
            await message.answer("Нет данных.")

# КОМАНДА /ask – с поддержкой общих вопросов через LLM + SQL

@dp.message(Command("ask"))
async def ask(message: types.Message):
    question = message.text.replace("/ask", "").strip()
    if not question:
        await message.answer("Задайте вопрос после команды. Например: /ask какая прибыль у объекта 210616_1?")
        return

    # Поиск ID объекта в вопросе
    obj_match = re.search(r'\b(\d{6}(?:_\d+)?)\b', question)
    if obj_match:
        obj_id = obj_match.group(1)

        # Проверяем, не спрашивают ли про самую большую/маленькую статью (код) или группу
        q_lower = question.lower()
        if ('самая большая статья' in q_lower or 'самая маленькая статья' in q_lower or
            'максимальная статья' in q_lower or 'минимальная статья' in q_lower or
            'самый большой код' in q_lower or 'самый маленький код' in q_lower):
            with engine.connect() as conn:
                max_row = conn.execute(
                    sa.text("""
                        SELECT e.expense_id, e.group_name, SUM(f.value_fact) as total
                        FROM fact_transactions f
                        JOIN dim_expenses e ON f.expense_id = e.expense_id
                        WHERE f.object_id = :oid
                        GROUP BY e.expense_id, e.group_name
                        HAVING SUM(f.value_fact) > 1
                        ORDER BY total DESC
                        LIMIT 1
                    """),
                    {"oid": obj_id}
                ).first()
                min_row = conn.execute(
                    sa.text("""
                        SELECT e.expense_id, e.group_name, SUM(f.value_fact) as total
                        FROM fact_transactions f
                        JOIN dim_expenses e ON f.expense_id = e.expense_id
                        WHERE f.object_id = :oid
                        GROUP BY e.expense_id, e.group_name
                        HAVING SUM(f.value_fact) > 1
                        ORDER BY total ASC
                        LIMIT 1
                    """),
                    {"oid": obj_id}
                ).first()
                total_cost_row = conn.execute(
                    sa.text("SELECT SUM(value_fact) as total FROM fact_transactions WHERE object_id = :oid"),
                    {"oid": obj_id}
                ).first()
                total_cost = total_cost_row.total if total_cost_row.total else 0

            if max_row and min_row:
                max_pct = (max_row.total / total_cost) * 100 if total_cost > 0 else 0
                min_pct = (min_row.total / total_cost) * 100 if total_cost > 0 else 0
                answer = (
                    f"📊 По объекту {obj_id}:"
                    f"🔺 Самая большая статья (код):"
                    f"   Код {max_row.expense_id} ({max_row.group_name}) – {max_row.total:,.0f} руб. ({max_pct:.2f}%)\n\n"
                    f"🔻 Самая маленькая статья (код, >1 руб):"
                    f"   Код {min_row.expense_id} ({min_row.group_name}) – {min_row.total:,.0f} руб. ({min_pct:.2f}%)"
                )
                await message.answer(answer)
                return
            else:
                await message.answer(f"Не найдено данных по кодам для объекта {obj_id} с суммой >1 руб.")
                return

        if ('самая большая группа' in q_lower or 'самая маленькая группа' in q_lower or
            'максимальная группа' in q_lower or 'минимальная группа' in q_lower):
            with engine.connect() as conn:
                max_group = conn.execute(
                    sa.text("""
                        SELECT e.group_name, SUM(f.value_fact) as total
                        FROM fact_transactions f
                        JOIN dim_expenses e ON f.expense_id = e.expense_id
                        WHERE f.object_id = :oid
                        GROUP BY e.group_name
                        ORDER BY total DESC
                        LIMIT 1
                    """),
                    {"oid": obj_id}
                ).first()
                min_group = conn.execute(
                    sa.text("""
                        SELECT e.group_name, SUM(f.value_fact) as total
                        FROM fact_transactions f
                        JOIN dim_expenses e ON f.expense_id = e.expense_id
                        WHERE f.object_id = :oid
                        GROUP BY e.group_name
                        HAVING SUM(f.value_fact) > 1
                        ORDER BY total ASC
                        LIMIT 1
                    """),
                    {"oid": obj_id}
                ).first()
                total_cost_row = conn.execute(
                    sa.text("SELECT SUM(value_fact) as total FROM fact_transactions WHERE object_id = :oid"),
                    {"oid": obj_id}
                ).first()
                total_cost = total_cost_row.total if total_cost_row.total else 0

            if max_group and min_group:
                max_pct = (max_group.total / total_cost) * 100 if total_cost > 0 else 0
                min_pct = (min_group.total / total_cost) * 100 if total_cost > 0 else 0
                answer = (
                    f"📊 По объекту {obj_id}:\n\n"
                    f"🔺 Самая большая группа затрат:"
                    f"   {max_group.group_name} – {max_group.total:,.0f} руб. ({max_pct:.2f}%)\n\n"
                    f"🔻 Самая маленькая группа затрат (>1 руб):"
                    f"   {min_group.group_name} – {min_group.total:,.0f} руб. ({min_pct:.2f}%)"
                )
                await message.answer(answer)
                return
            else:
                await message.answer(f"Не найдено данных по группам для объекта {obj_id} с суммой >1 руб.")
                return

        #  Обычный вопрос с ID: собираем контекст и отправляем в LLM
        with engine.connect() as conn:
            obj_data = conn.execute(
                sa.text("SELECT object_id, profit_fact, cost_price_fact, contract_price_fact FROM dim_objects WHERE object_id = :oid"),
                {"oid": obj_id}
            ).first()
            if not obj_data:
                await message.answer(f"Объект {obj_id} не найден.")
                return
            revenue = obj_data.contract_price_fact
            profit = obj_data.profit_fact
            cost_price = obj_data.cost_price_fact
            ros = (profit / cost_price * 100) if cost_price > 0 else 0.0

            groups = conn.execute(
                sa.text("""
                    SELECT e.group_name, SUM(f.value_fact) as total
                    FROM fact_transactions f
                    JOIN dim_expenses e ON f.expense_id = e.expense_id
                    WHERE f.object_id = :oid
                    GROUP BY e.group_name
                    ORDER BY total DESC
                """),
                {"oid": obj_id}
            ).fetchall()

            codes = conn.execute(
                sa.text("""
                    SELECT e.expense_id, e.group_name, SUM(f.value_fact) as total
                    FROM fact_transactions f
                    JOIN dim_expenses e ON f.expense_id = e.expense_id
                    WHERE f.object_id = :oid
                    GROUP BY e.expense_id, e.group_name
                    ORDER BY total DESC
                """),
                {"oid": obj_id}
            ).fetchall()

            cluster_row = conn.execute(
                sa.text("SELECT cluster FROM cluster_assignments WHERE object_id = :oid"),
                {"oid": obj_id}
            ).first()
            cluster = cluster_row.cluster if cluster_row else None

        total_cost = sum(c.total for c in codes) if codes else 0
        context = f"Данные по объекту {obj_id}:\n"
        context += f"Выручка: {revenue:,.0f} руб.\n"
        context += f"Расходы (общие): {total_cost:,.0f} руб.\n"
        context += f"Прибыль: {profit:,.0f} руб.\n"
        context += f"Рентабельность: {ros:.2f}%\n"
        if cluster is not None:
            cluster_desc = "собственные работы" if cluster == 0 else "субподряд и прочие"
            context += f"Кластер: {cluster} ({cluster_desc})\n"
        context += "\n"

        if groups:
            context += "Распределение затрат по группам (руб. и %):\n"
            for name, amount in groups:
                pct = (amount / total_cost * 100) if total_cost > 0 else 0
                context += f"- {name}: {amount:,.0f} руб. ({pct:.1f}%)\n"
        else:
            context += "Нет данных о затратах по группам.\n"

        if codes:
            context += "Затраты по всем кодам (сумма и % от общих затрат):"
            for exp_id, group_name, amount in codes:
                pct = (amount / total_cost * 100) if total_cost > 0 else 0
                context += f"- Код {exp_id} ({group_name}): {amount:,.0f} руб. ({pct:.2f}%)\n"
        else:
            context += "\nНет данных о кодах затрат.\n"

        try:
            response = client.chat.completions.create(
                model="openrouter/free",
                messages=[
                    {"role": "system", "content": "Ты – аналитик по строительным данным. Отвечай только на основе предоставленного контекста."},
                    {"role": "user", "content": f"Контекст:\n{context}\n\nВопрос пользователя: {question}"}
                ],
                temperature=0.3,
            )
            if response is None or not response.choices:
                await message.answer("❌ API вернул пустой ответ. Попробуйте позже или смените модель.")
                return
            answer = response.choices[0].message.content
            if not answer:
                await message.answer("❌ Модель не дала ответа.")
                return
            if len(answer) > 4000:
                answer = answer[:4000] + "..."
            await message.answer(answer)
        except Exception as e:
            await message.answer(f"Ошибка OpenRouter: {type(e).__name__}: {e}")
        return

    # Общие вопросы без ID (сначала быстрые SQL-шаблоны)
    q_lower = question.lower()

    # 0. Прибыль по кластеру (приоритет)
    cluster_match = re.search(r'кластер[ае]?\s*(\d+)', q_lower)
    if cluster_match and ('прибыль' in q_lower or 'рентабельность' in q_lower):
        cluster_num = int(cluster_match.group(1))
        with engine.connect() as conn:
            rows = conn.execute(
                sa.text("""
                    SELECT o.object_id, o.profit_fact, o.cost_price_fact
                    FROM dim_objects o
                    JOIN cluster_assignments c ON o.object_id = c.object_id
                    WHERE c.cluster = :cluster
                    ORDER BY o.object_id
                """),
                {"cluster": cluster_num}
            ).fetchall()
            if rows:
                answer = f"📊 Прибыль и рентабельность объектов кластера {cluster_num}:\n\n"
                for row in rows:
                    profit = row.profit_fact if row.profit_fact is not None else 0
                    cost = row.cost_price_fact if row.cost_price_fact is not None else 0
                    ros = (profit / cost * 100) if cost and cost > 0 else 0.0
                    answer += f"• {row.object_id}: прибыль {profit:,.0f} руб., рентабельность {ros:.2f}%\n"
                await message.answer(answer)
            else:
                await message.answer(f"Нет объектов в кластере {cluster_num} или нет данных о прибыли.")
        return

    # 0b. Перечислить объекты кластера (без прибыли)
    cluster_objects_match = re.search(r'(?:какие объекты|объекты).*?кластер[ае]?\s*(\d+)', q_lower)
    if not cluster_objects_match:
        cluster_objects_match = re.search(r'кластер[ае]?\s*(\d+).*?(?:объекты|попали)', q_lower)
    if cluster_objects_match:
        cluster_num = int(cluster_objects_match.group(1))
        with engine.connect() as conn:
            rows = conn.execute(
                sa.text("""
                    SELECT o.object_id
                    FROM dim_objects o
                    JOIN cluster_assignments c ON o.object_id = c.object_id
                    WHERE c.cluster = :cluster
                    ORDER BY o.object_id
                """),
                {"cluster": cluster_num}
            ).fetchall()
            if rows:
                objects_list = "\n".join([row.object_id for row in rows])
                await message.answer(f"📋 Объекты, попавшие в кластер {cluster_num}:\n{objects_list}")
            else:
                await message.answer(f"Нет объектов в кластере {cluster_num}.")
        return

    # 1. Список всех объектов
    if any(phrase in q_lower for phrase in ['список всех объектов', 'все объекты', 'какие объекты', 'выведи объекты', 'выведи все объекты']):
        with engine.connect() as conn:
            rows = conn.execute(sa.text("SELECT object_id FROM dim_objects ORDER BY object_id")).fetchall()
            if rows:
                objects_list = "\n".join([row.object_id for row in rows])
                await message.answer(f"📋 Список всех объектов:\n{objects_list}")
            else:
                await message.answer("Нет данных об объектах.")
        return

    # 2. Максимальные затраты по группе
    group_max_match = re.search(r'по группе\s+([а-яё\s]+).*(?:больше(?: всего)?|наибольшие|максимальные|самые большие|высокие)', q_lower)
    if not group_max_match:
        group_max_match = re.search(r'(?:больше(?: всего)?|наибольшие|максимальные|самые большие|высокие).*по группе\s+([а-яё\s]+)', q_lower)
    if group_max_match:
        group_name = group_max_match.group(1).strip()
        with engine.connect() as conn:
            row = conn.execute(
                sa.text("""
                    SELECT f.object_id, SUM(f.value_fact) as total
                    FROM fact_transactions f
                    JOIN dim_expenses e ON f.expense_id = e.expense_id
                    WHERE e.group_name = :group
                    GROUP BY f.object_id
                    ORDER BY total DESC
                    LIMIT 1
                """),
                {"group": group_name}
            ).first()
            if row:
                await message.answer(f"Наибольшие затраты по группе '{group_name}' у объекта {row.object_id}: {row.total:,.0f} руб.")
            else:
                await message.answer(f"Не найдено данных по группе '{group_name}'.")
        return

    # 3. Минимальные положительные затраты по группе
    group_min_match = re.search(r'по группе\s+([а-яё\s]+).*(?:меньше(?: всего)?|наименьшие|минимальные|самые маленькие|низкие)', q_lower)
    if not group_min_match:
        group_min_match = re.search(r'(?:меньше(?: всего)?|наименьшие|минимальные|самые маленькие|низкие).*по группе\s+([а-яё\s]+)', q_lower)
    if group_min_match:
        group_name = group_min_match.group(1).strip()
        with engine.connect() as conn:
            row = conn.execute(
                sa.text("""
                    SELECT f.object_id, SUM(f.value_fact) as total
                    FROM fact_transactions f
                    JOIN dim_expenses e ON f.expense_id = e.expense_id
                    WHERE e.group_name = :group
                    GROUP BY f.object_id
                    HAVING SUM(f.value_fact) > 0
                    ORDER BY total ASC
                    LIMIT 1
                """),
                {"group": group_name}
            ).first()
            if row:
                await message.answer(f"Наименьшие положительные затраты по группе '{group_name}' у объекта {row.object_id}: {row.total:,.0f} руб.")
            else:
                zero_objects = conn.execute(
                    sa.text("""
                        SELECT COUNT(DISTINCT f.object_id)
                        FROM fact_transactions f
                        JOIN dim_expenses e ON f.expense_id = e.expense_id
                        WHERE e.group_name = :group AND f.value_fact = 0
                    """),
                    {"group": group_name}
                ).scalar()
                if zero_objects > 0:
                    await message.answer(f"По группе '{group_name}' есть объекты с нулевыми затратами, но положительные затраты отсутствуют.")
                else:
                    await message.answer(f"Не найдено данных по группе '{group_name}'.")
        return

    # 4. Максимальные затраты по коду
    code_max_match = re.search(r'(?:код(?:у)?|стать(?:е|и)?)\s+(\d+\.\d+).*(?:больше(?: всего)?|наибольшие|максимальные|самые большие|высокие)', q_lower)
    if not code_max_match:
        code_max_match = re.search(r'(?:больше(?: всего)?|наибольшие|максимальные|самые большие|высокие).*(?:код(?:у)?|стать(?:е|и)?)\s+(\d+\.\d+)', q_lower)
    if code_max_match:
        code = code_max_match.group(1)
        with engine.connect() as conn:
            row = conn.execute(
                sa.text("""
                    SELECT f.object_id, SUM(f.value_fact) as total
                    FROM fact_transactions f
                    WHERE f.expense_id = :code
                    GROUP BY f.object_id
                    ORDER BY total DESC
                    LIMIT 1
                """),
                {"code": code}
            ).first()
            if row:
                grp_row = conn.execute(sa.text("SELECT group_name FROM dim_expenses WHERE expense_id = :code"), {"code": code}).first()
                group = f" ({grp_row.group_name})" if grp_row else ""
                await message.answer(f"Наибольшие затраты по коду {code}{group} у объекта {row.object_id}: {row.total:,.0f} руб.")
            else:
                await message.answer(f"Не найдено данных по коду {code}.")
        return

    # 5. Минимальные положительные затраты по коду
    code_min_match = re.search(r'(?:код(?:у)?|стать(?:е|и)?)\s+(\d+\.\d+).*(?:меньше(?: всего)?|наименьшие|минимальные|самые маленькие|низкие)', q_lower)
    if not code_min_match:
        code_min_match = re.search(r'(?:меньше(?: всего)?|наименьшие|минимальные|самые маленькие|низкие).*(?:код(?:у)?|стать(?:е|и)?)\s+(\d+\.\d+)', q_lower)
    if code_min_match:
        code = code_min_match.group(1)
        with engine.connect() as conn:
            row = conn.execute(
                sa.text("""
                    SELECT f.object_id, SUM(f.value_fact) as total
                    FROM fact_transactions f
                    WHERE f.expense_id = :code
                    GROUP BY f.object_id
                    HAVING SUM(f.value_fact) > 0
                    ORDER BY total ASC
                    LIMIT 1
                """),
                {"code": code}
            ).first()
            if row:
                grp_row = conn.execute(sa.text("SELECT group_name FROM dim_expenses WHERE expense_id = :code"), {"code": code}).first()
                group = f" ({grp_row.group_name})" if grp_row else ""
                await message.answer(f"Наименьшие положительные затраты по коду {code}{group} у объекта {row.object_id}: {row.total:,.0f} руб.")
            else:
                zero_count = conn.execute(sa.text("SELECT COUNT(*) FROM fact_transactions WHERE expense_id = :code AND value_fact = 0"), {"code": code}).scalar()
                if zero_count > 0:
                    await message.answer(f"По коду {code} есть объекты с нулевыми затратами, но положительные затраты отсутствуют.")
                else:
                    await message.answer(f"Не найдено данных по коду {code}.")
        return

    # 6. Самый прибыльный объект
    if 'самый прибыльный' in q_lower or 'наибольшая прибыль' in q_lower:
        with engine.connect() as conn:
            row = conn.execute(sa.text("SELECT object_id, profit_fact FROM dim_objects ORDER BY profit_fact DESC LIMIT 1")).first()
            if row:
                await message.answer(f"🏆 Самый прибыльный объект: {row.object_id} с прибылью {row.profit_fact:,.0f} руб.")
            else:
                await message.answer("Нет данных.")
        return

    # 7. Самый убыточный объект
    if 'самый убыточный' in q_lower or 'наибольший убыток' in q_lower:
        with engine.connect() as conn:
            row = conn.execute(sa.text("SELECT object_id, profit_fact FROM dim_objects ORDER BY profit_fact ASC LIMIT 1")).first()
            if row:
                await message.answer(f"📉 Самый убыточный объект: {row.object_id} с прибылью {row.profit_fact:,.0f} руб.")
            else:
                await message.answer("Нет данных.")
        return

    # 8. Количество объектов в кластере
    cluster_match = re.search(r'кластере\s*(\d+)', q_lower)
    if cluster_match:
        cluster_num = int(cluster_match.group(1))
        with engine.connect() as conn:
            count = conn.execute(sa.text("SELECT COUNT(*) FROM cluster_assignments WHERE cluster = :c"), {"c": cluster_num}).scalar()
            await message.answer(f"В кластере {cluster_num} находится {count} объектов.")
        return

    # 9. Средняя прибыль/рентабельность по кластеру
    avg_match = re.search(r'(?:средняя прибыль|средняя рентабельность).*кластера\s*(\d+)', q_lower)
    if avg_match:
        cluster_num = int(avg_match.group(1))
        with engine.connect() as conn:
            stats = conn.execute(
                sa.text("""
                    SELECT AVG(o.profit_fact) as avg_profit, AVG(o.profit_fact / NULLIF(o.cost_price_fact, 0) * 100) as avg_ros
                    FROM dim_objects o
                    JOIN cluster_assignments c ON o.object_id = c.object_id
                    WHERE c.cluster = :cluster
                """),
                {"cluster": cluster_num}
            ).first()
            if stats and stats.avg_profit is not None:
                await message.answer(f"📊 По кластеру {cluster_num}:\n• Средняя прибыль: {stats.avg_profit:,.0f} руб.\n• Средняя рентабельность: {stats.avg_ros:.2f}%")
            else:
                await message.answer(f"Нет данных по кластеру {cluster_num}.")
        return

    # 10. Топ-N объектов по прибыли
    top_n_match = re.search(r'(?:топ|первые)\s*(\d+)\s*(?:объекта|объектов).*прибыли', q_lower)
    if top_n_match:
        n = min(int(top_n_match.group(1)), 10)
        with engine.connect() as conn:
            rows = conn.execute(
                sa.text("""
                    SELECT object_id, profit_fact
                    FROM dim_objects
                    ORDER BY profit_fact DESC
                    LIMIT :n
                """),
                {"n": n}
            ).fetchall()
            if rows:
                answer = f"🏆 Топ-{n} объектов по прибыли:\n"
                for row in rows:
                    answer += f"• {row.object_id}: {row.profit_fact:,.0f} руб.\n"
                await message.answer(answer)
            else:
                await message.answer("Нет данных.")
        return

    # 11. Объекты с убытком
    if 'убыток' in q_lower or 'отрицательная прибыль' in q_lower:
        with engine.connect() as conn:
            rows = conn.execute(
                sa.text("SELECT object_id, profit_fact FROM dim_objects WHERE profit_fact < 0 ORDER BY profit_fact ASC")
            ).fetchall()
            if rows:
                answer = "📉 Объекты с убытком:\n"
                for row in rows:
                    answer += f"• {row.object_id}: {row.profit_fact:,.0f} руб.\n"
                await message.answer(answer)
            else:
                await message.answer("Нет объектов с убытком.")
        return

    # 12. Объекты с рентабельностью выше заданного порога
    ros_threshold_match = re.search(r'рентабельность.*(?:выше|больше)\s*(\d+(?:\.\d+)?)\s*%', q_lower)
    if ros_threshold_match:
        threshold = float(ros_threshold_match.group(1))
        with engine.connect() as conn:
            rows = conn.execute(
                sa.text("""
                    SELECT object_id, profit_fact, cost_price_fact
                    FROM dim_objects
                    WHERE cost_price_fact > 0 AND (profit_fact / cost_price_fact * 100) > :thr
                    ORDER BY (profit_fact / cost_price_fact * 100) DESC
                """),
                {"thr": threshold}
            ).fetchall()
            if rows:
                answer = f"📈 Объекты с рентабельностью выше {threshold}%:\n"
                for row in rows:
                    ros = (row.profit_fact / row.cost_price_fact * 100)
                    answer += f"• {row.object_id}: {ros:.2f}%\n"
                await message.answer(answer)
            else:
                await message.answer(f"Нет объектов с рентабельностью выше {threshold}%.")
        return

    # Если ни один шаблон не подошёл – формируем общую сводку по базе
    # и отправляем в LLM
    with engine.connect() as conn:
        objects = conn.execute(
            sa.text("""
                SELECT o.object_id, o.profit_fact, c.cluster
                FROM dim_objects o
                LEFT JOIN cluster_assignments c ON o.object_id = c.object_id
            """)
        ).fetchall()
        top10_codes = conn.execute(
            sa.text("""
                SELECT e.expense_id, e.group_name, SUM(f.value_fact) as total
                FROM fact_transactions f
                JOIN dim_expenses e ON f.expense_id = e.expense_id
                GROUP BY e.expense_id, e.group_name
                ORDER BY total DESC
                LIMIT 10
            """)
        ).fetchall()
        top5_groups = conn.execute(
            sa.text("""
                SELECT e.group_name, SUM(f.value_fact) as total
                FROM fact_transactions f
                JOIN dim_expenses e ON f.expense_id = e.expense_id
                GROUP BY e.group_name
                ORDER BY total DESC
                LIMIT 5
            """)
        ).fetchall()
        clusters = conn.execute(
            sa.text("""
                SELECT c.cluster, COUNT(*) as cnt, AVG(o.profit_fact) as avg_profit
                FROM cluster_assignments c
                JOIN dim_objects o ON c.object_id = o.object_id
                GROUP BY c.cluster
                ORDER BY c.cluster
            """)
        ).fetchall()

    summary = "Общая информация по всем объектам:\n"
    summary += f"Всего объектов: {len(objects)}\n\n"
    for obj in objects[:5]:
        summary += f"Объект {obj.object_id}: прибыль {obj.profit_fact:,.0f} руб., кластер {obj.cluster if obj.cluster else 'не определён'}\n"
    if len(objects) > 5:
        summary += f"... и ещё {len(objects)-5} объектов.\n"
    summary += "\nТоп-10 кодов затрат:\n"
    for code, group, total in top10_codes:
        summary += f"- {code} ({group}): {total:,.0f} руб.\n"
    summary += "\nТоп-5 групп затрат:\n"
    for group, total in top5_groups:
        summary += f"- {group}: {total:,.0f} руб.\n"
    summary += "\nИнформация по кластерам:\n"
    for cl in clusters:
        summary += f"Кластер {cl.cluster}: {cl.cnt} объектов, средняя прибыль {cl.avg_profit:,.0f} руб.\n"

    prompt = f"{summary}\n\nВопрос пользователя: {question}\n\nОтветь на основе данных выше. Если данных недостаточно, скажи об этом."

    try:
        response = client.chat.completions.create(
            model="openrouter/free",
            messages=[
                {"role": "system", "content": "Ты – аналитик по строительным данным. Отвечай только на основе предоставленной информации."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.3,
        )
        if response is None or not response.choices:
            await message.answer("❌ API вернул пустой ответ. Попробуйте позже или смените модель.")
            return
        answer = response.choices[0].message.content
        if not answer:
            await message.answer("❌ Модель не дала ответа.")
            return
        if len(answer) > 4000:
            answer = answer[:4000] + "..."
        await message.answer(answer)
    except Exception as e:
        await message.answer(f"Ошибка OpenRouter: {type(e).__name__}: {e}")

# Команды topcodes, bottomcodes, cluster, predict_new_values, predict_new

@dp.message(Command("topcodes"))
async def top_codes(message: types.Message):
    parts = message.text.split()
    obj_id = None
    limit = 5
    for p in parts[1:]:
        if re.match(r'^\d{6}(?:_\d+)?$', p):
            obj_id = p
        elif p.isdigit():
            limit = int(p)
        else:
            obj_id = p
    if limit > 20:
        limit = 20
    if obj_id is None:
        query = """
            SELECT e.expense_id, e.group_name, SUM(f.value_fact) as total
            FROM fact_transactions f
            JOIN dim_expenses e ON f.expense_id = e.expense_id
            GROUP BY e.expense_id, e.group_name
            HAVING SUM(f.value_fact) > 0
            ORDER BY total DESC
            LIMIT :limit
        """
        with engine.connect() as conn:
            rows = conn.execute(sa.text(query), {"limit": limit}).fetchall()
        if rows:
            text = f"🏆 Топ-{limit} самых затратных кодов (только >0) ПО ВСЕМ ОБЪЕКТАМ:\n"
            for code, group, total in rows:
                text += f"- Код {code} ({group}): {total:,.0f} руб.\n"
            await message.answer(text)
        else:
            await message.answer("Нет положительных кодов затрат.")
    else:
        with engine.connect() as conn:
            total_row = conn.execute(
                sa.text("SELECT SUM(value_fact) as total FROM fact_transactions WHERE object_id = :oid"),
                {"oid": obj_id}
            ).first()
            total_cost = total_row.total if total_row.total else 0
            rows = conn.execute(
                sa.text("""
                    SELECT e.expense_id, e.group_name, SUM(f.value_fact) as total
                    FROM fact_transactions f
                    JOIN dim_expenses e ON f.expense_id = e.expense_id
                    WHERE f.object_id = :oid
                    GROUP BY e.expense_id, e.group_name
                    HAVING SUM(f.value_fact) > 0
                    ORDER BY total DESC
                    LIMIT :limit
                """),
                {"oid": obj_id, "limit": limit}
            ).fetchall()
        if rows:
            text = f"🏆 Топ-{limit} самых затратных кодов (только >0) для {obj_id}:\n"
            for code, group, amount in rows:
                pct = (amount / total_cost * 100) if total_cost > 0 else 0
                text += f"- Код {code} ({group}): {amount:,.0f} руб. ({pct:.2f}% от общих расходов)\n"
            await message.answer(text)
        else:
            await message.answer(f"Нет положительных кодов затрат для {obj_id}.")

@dp.message(Command("bottomcodes"))
async def bottom_codes(message: types.Message):
    parts = message.text.split()
    obj_id = None
    limit = 5
    for p in parts[1:]:
        if re.match(r'^\d{6}(?:_\d+)?$', p):
            obj_id = p
        elif p.isdigit():
            limit = int(p)
        else:
            obj_id = p
    if limit > 20:
        limit = 20
    if obj_id is None:
        query = """
            SELECT e.expense_id, e.group_name, SUM(f.value_fact) as total
            FROM fact_transactions f
            JOIN dim_expenses e ON f.expense_id = e.expense_id
            GROUP BY e.expense_id, e.group_name
            HAVING SUM(f.value_fact) > 0
            ORDER BY total ASC
            LIMIT :limit
        """
        with engine.connect() as conn:
            rows = conn.execute(sa.text(query), {"limit": limit}).fetchall()
        if rows:
            text = f"🔻 Топ-{limit} наименьших положительных кодов затрат ПО ВСЕМ ОБЪЕКТАМ:\n"
            for code, group, total in rows:
                text += f"- Код {code} ({group}): {total:,.0f} руб.\n"
            await message.answer(text)
        else:
            await message.answer("Нет положительных кодов затрат.")
    else:
        with engine.connect() as conn:
            total_row = conn.execute(
                sa.text("SELECT SUM(value_fact) as total FROM fact_transactions WHERE object_id = :oid"),
                {"oid": obj_id}
            ).first()
            total_cost = total_row.total if total_row.total else 0
            rows = conn.execute(
                sa.text("""
                    SELECT e.expense_id, e.group_name, SUM(f.value_fact) as total
                    FROM fact_transactions f
                    JOIN dim_expenses e ON f.expense_id = e.expense_id
                    WHERE f.object_id = :oid
                    GROUP BY e.expense_id, e.group_name
                    HAVING SUM(f.value_fact) > 0
                    ORDER BY total ASC
                    LIMIT :limit
                """),
                {"oid": obj_id, "limit": limit}
            ).fetchall()
        if rows:
            text = f"🔻 Топ-{limit} наименьших положительных кодов затрат для {obj_id}:\n"
            for code, group, amount in rows:
                pct = (amount / total_cost * 100) if total_cost > 0 else 0
                text += f"- Код {code} ({group}): {amount:,.0f} руб. ({pct:.2f}% от общих расходов)\n"
            await message.answer(text)
        else:
            await message.answer(f"Нет положительных кодов затрат для {obj_id}.")

@dp.message(Command("cluster"))
async def cluster_info(message: types.Message):
    parts = message.text.split()
    if len(parts) < 2:
        await message.answer("Пример: /cluster 210616_1")
        return
    obj_id = parts[1]
    with engine.connect() as conn:
        row = conn.execute(
            sa.text("SELECT cluster FROM cluster_assignments WHERE object_id = :oid"),
            {"oid": obj_id}
        ).first()
    if row:
        cluster = row[0]
        desc = "собственные работы (>70%)" if cluster == 0 else "субподряд и прочие (>50%)"
        await message.answer(f"📊 Объект {obj_id} относится к кластеру **{cluster}** – {desc}", parse_mode="Markdown")
    else:
        await message.answer(f"❌ Для объекта {obj_id} нет информации о кластере.")

@dp.message(Command("predict_new_values"))
async def predict_new_values(message: types.Message):
    if not model_loaded:
        await message.answer("❌ Модель не загружена.")
        return
    parts = message.text.split()
    if len(parts) != 6:
        await message.answer(
            "Формат: /predict_new_values obj_id доля_материалы доля_офисные доля_строительные_часы доля_субподряд\n"
            "Пример: /predict_new_values new_001 0.4 0.1 0.3 0.2"
        )
        return
    obj_id = parts[1]
    try:
        share_mat = float(parts[2])
        share_office = float(parts[3])
        share_hours = float(parts[4])
        share_sub = float(parts[5])
    except ValueError:
        await message.answer("Ошибка: все доли должны быть числами (с точкой).")
        return
    feature_values = [share_mat, share_office, share_hours, share_sub]
    X = [feature_values]
    pred_cluster = int(model.predict(X)[0])
    with engine.connect() as conn:
        big = conn.execute(
            sa.text("SELECT expense_id, group_name, pct_of_cluster as pct FROM cluster_cost_stats WHERE cluster_simple = :c ORDER BY pct_of_cluster DESC LIMIT 5"),
            {"c": pred_cluster}
        ).fetchall()
        small_raw = conn.execute(
            sa.text("SELECT expense_id, group_name, pct_of_cluster as pct FROM cluster_cost_stats WHERE cluster_simple = :c AND pct_of_cluster > 0 ORDER BY pct_of_cluster ASC LIMIT 100"),
            {"c": pred_cluster}
        ).fetchall()
        small = [r for r in small_raw if r.pct > 0.01]
    answer = f"🔮 **Предсказанный кластер для {obj_id}:** {pred_cluster}\n"
    answer += f"📌 *{('собственные работы (>70%)' if pred_cluster == 0 else 'субподряд и прочие (>50%)')}*\n\n"
    if big:
        answer += "💰 **Наибольшие доли затрат (исторические данные):**\n"
        for code, group, pct in big:
            answer += f"• Код {code} ({group}): {pct:.2f}% от всех затрат кластера\n"
    if small:
        answer += "\n🟢 **Наименьшие положительные доли затрат (>0.01%):**\n"
        for code, group, pct in small[:5]:
            answer += f"• Код {code} ({group}): {pct:.2f}% от всех затрат кластера\n"
    else:
        answer += "\n🟢 **Наименьшие положительные доли затрат:** нет долей >0.01%.\n"
    answer += "\n💡 **Рекомендация:** оптимизируйте крупные статьи затрат."
    await message.answer(answer, parse_mode="Markdown")

@dp.message(Command("predict_new"))
async def predict_new_command(message: types.Message):
    await message.answer(
        "📎 Отправьте Excel-файл (.xlsx) с данными нового объекта.\n\n"
        "**Вариант 1 (транзакции):** файл с листами `fact_transactions`, `dim_expenses` (и опционально `dim_objects`).\n"
        "**Вариант 2 (готовые доли):** файл с колонками share_материалы, share_офисные затраты, share_строительные часы, share_субподряд.\n\n"
        "Бот сам определит формат."
    )

# Добавляем синоним /predict для удобства
@dp.message(Command("predict"))
async def predict_command(message: types.Message):
    await predict_new_command(message)

# ОБРАБОТЧИК ДОКУМЕНТОВ

@dp.message(lambda message: message.document is not None)
async def handle_document(message: types.Message):
    document = message.document
    if not document.file_name.lower().endswith(('.xlsx', '.xls')):
        await message.answer("Пожалуйста, отправьте файл Excel (.xlsx или .xls).")
        return
    file = await bot.get_file(document.file_id)
    file_path = f"/tmp/{document.file_name}"
    await bot.download_file(file.file_path, file_path)
    try:
        xl = pd.ExcelFile(file_path)
        sheets = xl.sheet_names
        if 'fact_transactions' in sheets and 'dim_expenses' in sheets:
            await process_transactions_file(file_path, message)
        else:
            await process_shares_file(file_path, message)
    except Exception as e:
        await message.answer(f"Ошибка при обработке файла: {e}")
    finally:
        os.remove(file_path)

async def process_transactions_file(file_path: str, message: types.Message):
    """Обработка транзакционного файла – точный расчёт по данным из файла"""
    try:
        fact = pd.read_excel(file_path, sheet_name='fact_transactions')
        expenses = pd.read_excel(file_path, sheet_name='dim_expenses')
        
        fact.columns = fact.columns.str.lower().str.strip()
        expenses.columns = expenses.columns.str.lower().str.strip()
        
        if 'expense_id' not in fact.columns or 'value_fact' not in fact.columns:
            await message.answer("❌ В листе fact_transactions нет колонок expense_id или value_fact.")
            return
        if 'expense_id' not in expenses.columns or 'group_name' not in expenses.columns:
            await message.answer("❌ В листе dim_expenses нет колонок expense_id или group_name.")
            return
        
        fact['expense_id'] = fact['expense_id'].astype(str).str.strip()
        expenses['expense_id'] = expenses['expense_id'].astype(str).str.strip()
        fact['value_fact'] = pd.to_numeric(fact['value_fact'], errors='coerce').fillna(0)
        
        expenses = expenses.drop_duplicates(subset=['expense_id'], keep='first')
        
        merged = fact.merge(expenses, on='expense_id', how='left')
        merged['group_name'] = merged['group_name'].fillna('Прочие')
        
        total_cost = fact['value_fact'].sum()
        if total_cost == 0:
            await message.answer("❌ Общая сумма расходов равна нулю.")
            return
        
        merged_sum = merged['value_fact'].sum()
        if abs(merged_sum - total_cost) > 0.01:
            await message.answer(f"⚠️ Внимание: обнаружены дубликаты в справочнике затрат. Сумма после объединения ({merged_sum:,.0f}) не равна исходной ({total_cost:,.0f}). Используем исходную сумму.")
        
        revenue = None
        try:
            xl = pd.ExcelFile(file_path)
            if 'dim_objects' in xl.sheet_names:
                dim_objects = pd.read_excel(file_path, sheet_name='dim_objects')
                dim_objects.columns = dim_objects.columns.str.lower().str.strip()
                if 'contract_price_fact' in dim_objects.columns:
                    revenue = dim_objects['contract_price_fact'].iloc[0]
        except:
            pass
        
        profit = None
        ros = None
        if revenue is not None:
            profit = revenue - total_cost
            ros = (profit / total_cost * 100) if total_cost > 0 else 0
        
        group_totals = merged.groupby('group_name')['value_fact'].sum().sort_values(ascending=False)
        code_totals = fact.groupby('expense_id')['value_fact'].sum().sort_values(ascending=False)
        top5_codes_obj = code_totals.head(5)
        
        needed = ['материалы', 'офисные затраты', 'строительные часы', 'субподряд']
        shares = {}
        for grp in needed:
            amount = group_totals.get(grp, 0)
            shares[grp] = (amount / total_cost) * 100
        
        if not model_loaded:
            await message.answer("❌ Модель не загружена, предсказание невозможно.")
            return
        feature_values = [shares['материалы']/100, shares['офисные затраты']/100,
                          shares['строительные часы']/100, shares['субподряд']/100]
        X = [feature_values]
        pred_cluster = int(model.predict(X)[0])
        cluster_desc = "собственные работы (>70%)" if pred_cluster == 0 else "субподряд и прочие (>50%)"
        
        answer = "📊 **Анализ загруженного объекта**\n\n"
        if revenue is not None:
            answer += f"💰 **Выручка:** {revenue:,.0f} руб.\n"
        answer += f"📉 **Общие расходы:** {total_cost:,.0f} руб.\n"
        if profit is not None:
            answer += f"💵 **Прибыль:** {profit:,.0f} руб.\n"
            answer += f"📈 **Рентабельность затрат (ROM):** {ros:.2f}%\n"
        answer += "\n📊 **Распределение затрат по группам (сумма и % от всех расходов):**\n"
        for group, amount in group_totals.items():
            pct = (amount / total_cost) * 100
            answer += f"• {group}: {amount:,.0f} руб. ({pct:.2f}%)\n"
        answer += "\n💰 **Топ-5 кодов затрат (по данному объекту):**\n"
        for code, amount in top5_codes_obj.items():
            pct = (amount / total_cost) * 100
            grp_name = expenses[expenses['expense_id'] == code]['group_name'].values
            grp = grp_name[0] if len(grp_name) > 0 else "неизвестно"
            answer += f"• Код {code} ({grp}): {amount:,.0f} руб. ({pct:.2f}%)\n"
        answer += "\n---\n🔮 **Предсказанный кластер:** {} – {}\n".format(pred_cluster, cluster_desc)
        
        answer += "\n📈 **Статистика по предсказанному кластеру (исторические данные)**\n\n"
        with engine.connect() as conn:
            group_stats = conn.execute(
                sa.text("""
                    SELECT e.group_name, SUM(f.value_fact) as total
                    FROM fact_transactions f
                    JOIN dim_expenses e ON f.expense_id = e.expense_id
                    JOIN cluster_assignments c ON f.object_id = c.object_id
                    WHERE c.cluster = :cluster
                    GROUP BY e.group_name
                    ORDER BY total DESC
                """),
                {"cluster": pred_cluster}
            ).fetchall()
            total_cluster_cost = sum(row.total for row in group_stats) if group_stats else 0
            if group_stats and total_cluster_cost > 0:
                answer += "📊 **Распределение затрат по группам в этом кластере (сумма и %):**\n"
                for row in group_stats:
                    pct = (row.total / total_cluster_cost) * 100
                    answer += f"• {row.group_name}: {row.total:,.0f} руб. ({pct:.2f}%)\n"
            else:
                answer += "📊 Нет данных о распределении по группам для этого кластера.\n"
            top_codes_cluster = conn.execute(
                sa.text("""
                    SELECT e.expense_id, e.group_name, SUM(f.value_fact) as total
                    FROM fact_transactions f
                    JOIN dim_expenses e ON f.expense_id = e.expense_id
                    JOIN cluster_assignments c ON f.object_id = c.object_id
                    WHERE c.cluster = :cluster
                    GROUP BY e.expense_id, e.group_name
                    ORDER BY total DESC
                    LIMIT 5
                """),
                {"cluster": pred_cluster}
            ).fetchall()
            if top_codes_cluster:
                answer += "\n💰 **Топ-5 кодов затрат в этом кластере:**\n"
                for code, group, total in top_codes_cluster:
                    pct = (total / total_cluster_cost) * 100 if total_cluster_cost > 0 else 0
                    answer += f"• Код {code} ({group}): {pct:.2f}% от всех затрат кластера\n"
            else:
                answer += "\n💰 Нет данных о кодах затрат для этого кластера.\n"
        answer += "\n💡 **Рекомендация:** оптимизируйте крупные статьи затрат, особенно те, которые входят в топ-5 кодов вашего объекта и топ-5 кодов кластера."
        await message.answer(answer, parse_mode="Markdown")
    except Exception as e:
        await message.answer(f"❌ Ошибка при обработке транзакционного файла: {e}")

async def process_shares_file(file_path: str, message: types.Message):
    """Обработка файла с готовыми долями (share_материалы и т.д.)"""
    df = pd.read_excel(file_path)
    df.columns = df.columns.str.strip().str.lower().str.replace(' ', '_')
    column_mapping = {
        'object_id': ['object_id', 'objectid', 'id'],
        'share_mat': ['share_материалы', 'share_materials', 'share_materialy'],
        'share_office': ['share_офисные_затраты', 'share_office', 'share_офисные_затраты'],
        'share_hours': ['share_строительные_часы', 'share_hours', 'share_строительные_часы'],
        'share_sub': ['share_субподряд', 'share_subcontractor', 'share_sub']
    }
    found = {}
    for key, variants in column_mapping.items():
        found_col = None
        for col in df.columns:
            if col in variants or any(v in col for v in variants):
                found_col = col
                break
        if found_col is None:
            await message.answer(f"❌ Не найдена колонка для '{key}'. Доступные колонки: {list(df.columns)}")
            return
        found[key] = found_col
    row = df.iloc[0]
    if not model_loaded:
        await message.answer("❌ Модель не загружена.")
        return
    try:
        share_mat = float(row[found['share_mat']])
        share_office = float(row[found['share_office']])
        share_hours = float(row[found['share_hours']])
        share_sub = float(row[found['share_sub']])
    except Exception as e:
        await message.answer(f"Ошибка чтения числовых значений: {e}. Убедитесь, что числа с точкой (не запятой).")
        return
    feature_values = [share_mat, share_office, share_hours, share_sub]
    X = [feature_values]
    pred_cluster = int(model.predict(X)[0])
    with engine.connect() as conn:
        big = conn.execute(
            sa.text("SELECT expense_id, group_name, pct_of_cluster as pct FROM cluster_cost_stats WHERE cluster_simple = :c ORDER BY pct_of_cluster DESC LIMIT 5"),
            {"c": pred_cluster}
        ).fetchall()
        small_raw = conn.execute(
            sa.text("SELECT expense_id, group_name, pct_of_cluster as pct FROM cluster_cost_stats WHERE cluster_simple = :c AND pct_of_cluster > 0 ORDER BY pct_of_cluster ASC LIMIT 100"),
            {"c": pred_cluster}
        ).fetchall()
        small = [r for r in small_raw if r.pct > 0.01]
    answer = f"🔮 **Предсказанный кластер:** {pred_cluster}\n"
    answer += f"📌 *{('собственные работы (>70%)' if pred_cluster == 0 else 'субподряд и прочие (>50%)')}*\n\n"
    if big:
        answer += "💰 **Наибольшие доли затрат (исторические данные):**\n"
        for code, group, pct in big:
            answer += f"• Код {code} ({group}): {pct:.2f}% от всех затрат кластера\n"
    if small:
        answer += "\n🟢 **Наименьшие положительные доли затрат (>0.01%):**\n"
        for code, group, pct in small[:5]:
            answer += f"• Код {code} ({group}): {pct:.2f}% от всех затрат кластера\n"
    answer += "\n💡 **Рекомендация:** оптимизируйте крупные статьи затрат."
    await message.answer(answer, parse_mode="Markdown")

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())