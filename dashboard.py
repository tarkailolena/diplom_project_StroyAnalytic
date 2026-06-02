import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from sqlalchemy import create_engine
import os
import joblib
import numpy as np
import tempfile
from pathlib import Path

st.set_page_config(layout="wide")
st.title("📊 Дашборд строительных объектов")

# -------------------- ФУНКЦИЯ WATERFALL (ПЛАН-ФАКТ) --------------------
def plot_waterfall_profit(obj_id):
    query = """
        SELECT 
            o.profit_plan, o.profit_fact,
            o.contract_price_plan, o.contract_price_fact,
            e.group_name,
            SUM(f.value_plan) as value_plan,
            SUM(f.value_fact) as value_fact
        FROM dim_objects o
        JOIN fact_transactions f ON o.object_id = f.object_id
        JOIN dim_expenses e ON f.expense_id = e.expense_id
        WHERE o.object_id = :obj_id
        GROUP BY o.profit_plan, o.profit_fact, o.contract_price_plan, o.contract_price_fact, e.group_name
    """
    df = pd.read_sql(query, engine, params={"obj_id": obj_id})
    if df.empty:
        return None, "Нет данных для построения Waterfall"
    
    profit_plan = df['profit_plan'].iloc[0]
    profit_fact = df['profit_fact'].iloc[0]
    revenue_plan = df['contract_price_plan'].iloc[0]
    revenue_fact = df['contract_price_fact'].iloc[0]
    revenue_change = revenue_fact - revenue_plan

    df['cost_change'] = df['value_fact'] - df['value_plan']
    df['profit_impact'] = -df['cost_change']

    df_pos = df[df['profit_impact'] > 0].copy()
    df_neg = df[df['profit_impact'] < 0].copy()
    df_pos['abs_impact'] = df_pos['profit_impact']
    df_neg['abs_impact'] = df_neg['profit_impact'].abs()
    df_pos = df_pos.sort_values('abs_impact', ascending=False)
    df_neg = df_neg.sort_values('abs_impact', ascending=False)
    df_sorted = pd.concat([df_pos, df_neg], ignore_index=True)

    groups = df_sorted['group_name'].tolist()
    impacts = df_sorted['profit_impact'].tolist()

    labels = ['Плановая прибыль', 'Изменение выручки'] + groups + ['Фактическая прибыль']
    measures = ['absolute'] + ['relative'] + ['relative'] * len(groups) + ['total']
    values = [profit_plan, revenue_change] + impacts + [profit_fact]
    text = [f"{profit_plan/1e6:.1f} млн", f"{revenue_change/1e6:+.1f} млн"] + \
           [f"{v/1e6:+.1f} млн" for v in impacts] + [f"{profit_fact/1e6:.1f} млн"]

    fig = go.Figure(go.Waterfall(
        x=labels,
        measure=measures,
        y=values,
        text=text,
        textposition="outside",
        decreasing={"marker": {"color": "#2ca02c"}},
        increasing={"marker": {"color": "#d62728"}},
        connector={"line": {"color": "rgb(63,63,63)"}}
    ))
    fig.update_layout(
        title=f"Формирование фактической прибыли – объект {obj_id}<br>План: {profit_plan:,.0f} руб. → Факт: {profit_fact:,.0f} руб.",
        xaxis_title="Фактор изменения", yaxis_title="Изменение (млн руб)",
        xaxis_tickangle=-45, height=550
    )
    return fig, None

# -------------------- ПОДКЛЮЧЕНИЕ К БД --------------------
@st.cache_resource
def get_engine():
    db_host = os.getenv("DB_HOST")
    if db_host:
        return create_engine(f'postgresql://postgres:postgres@{db_host}:5432/stroy_db')
    else:
        db_path = Path(__file__).parent / "stroy_analytics.db"
        if not db_path.exists():
            st.error("❌ Файл базы данных stroy_analytics.db не найден.")
            st.stop()
        return create_engine(f'sqlite:///{db_path}')

engine = get_engine()

# -------------------- ФУНКЦИИ ЗАГРУЗКИ ДАННЫХ --------------------
@st.cache_data
def load_objects():
    objects = pd.read_sql("""
        SELECT o.object_id, o.profit_fact, o.cost_price_fact, o.contract_price_fact, c.cluster
        FROM dim_objects o
        LEFT JOIN cluster_assignments c ON o.object_id = c.object_id
    """, engine)
    objects['ros_fact_%'] = objects.apply(
        lambda r: (r['profit_fact'] / r['cost_price_fact'] * 100) if r['cost_price_fact'] and r['cost_price_fact'] > 0 else 0.0,
        axis=1
    )
    return objects

@st.cache_data
def load_cluster_cost_stats():
    return pd.read_sql("SELECT * FROM cluster_cost_stats", engine)

@st.cache_data
def load_object_transactions(object_id):
    query = f"""
        SELECT e.expense_id, e.group_name, f.value_fact
        FROM fact_transactions f
        JOIN dim_expenses e ON f.expense_id = e.expense_id
        WHERE f.object_id = '{object_id}'
    """
    return pd.read_sql(query, engine)

@st.cache_data
def load_all_transactions_for_corr():
    return pd.read_sql("""
        SELECT f.object_id, e.group_name, f.value_fact
        FROM fact_transactions f
        JOIN dim_expenses e ON f.expense_id = e.expense_id
    """, engine)

# -------------------- ОБРАБОТКА EXCEL --------------------
def process_transactions_file(file):
    try:
        fact = pd.read_excel(file, sheet_name='fact_transactions')
        expenses = pd.read_excel(file, sheet_name='dim_expenses')
        fact.columns = fact.columns.str.lower().str.strip()
        expenses.columns = expenses.columns.str.lower().str.strip()
        fact['expense_id'] = fact['expense_id'].astype(str).str.strip()
        expenses['expense_id'] = expenses['expense_id'].astype(str).str.strip()
        fact['value_fact'] = pd.to_numeric(fact['value_fact'], errors='coerce').fillna(0)
        
        expenses_unique = expenses.drop_duplicates(subset=['expense_id'], keep='first')
        total_cost = fact['value_fact'].sum()
        if total_cost == 0:
            return None, None, None, "Общая сумма расходов равна нулю."
        
        # Группировка по кодам
        code_totals = fact.groupby('expense_id')['value_fact'].sum().reset_index()
        code_summary = code_totals.merge(expenses_unique[['expense_id', 'group_name']], on='expense_id', how='left')
        code_summary['group_name'] = code_summary['group_name'].fillna('Прочие')
        code_summary = code_summary.sort_values('value_fact', ascending=False)
        code_summary['pct'] = code_summary['value_fact'] / total_cost * 100
        
        # Группировка по группам (все группы)
        fact_with_group = fact.merge(expenses_unique[['expense_id', 'group_name']], on='expense_id', how='left')
        fact_with_group['group_name'] = fact_with_group['group_name'].fillna('Прочие')
        group_totals_all = fact_with_group.groupby('group_name')['value_fact'].sum().sort_values(ascending=False)
        
        # Только 4 группы для модели
        needed = ['материалы', 'офисные затраты', 'строительные часы', 'субподряд']
        shares_model = {}
        for grp in needed:
            shares_model[grp] = group_totals_all.get(grp, 0) / total_cost
        
        return shares_model, code_summary, group_totals_all, total_cost
    except Exception as e:
        return None, None, None, f"Ошибка обработки файла: {e}"

# -------------------- ЗАГРУЗКА ОСНОВНЫХ ДАННЫХ --------------------
objects = load_objects()
cost_stats = load_cluster_cost_stats()
all_trans = load_all_transactions_for_corr()

# -------------------- БОКОВАЯ ПАНЕЛЬ --------------------
st.sidebar.header("Фильтры")
selected_clusters = st.sidebar.multiselect(
    "Кластер",
    options=sorted(objects['cluster'].dropna().unique()),
    default=[]
)

st.sidebar.subheader("Выбор объекта")
available_objects_all = objects['object_id'].unique()
selected_object_global = st.sidebar.selectbox(
    "Объект (режим детального анализа)",
    options=["Не выбран"] + list(available_objects_all),
    index=0
)

# -------------------- РЕЖИМ ОДНОГО ОБЪЕКТА --------------------
if selected_object_global != "Не выбран":
    st.info(f"🔍 Детальный анализ: объект **{selected_object_global}**")
    trans = load_object_transactions(selected_object_global)
    if trans.empty:
        st.error("Нет транзакций для выбранного объекта.")
    else:
        obj_row = objects[objects['object_id'] == selected_object_global].iloc[0]
        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Прибыль", f"{obj_row['profit_fact']:,.0f} ₽")
        col2.metric("Рентабельность", f"{obj_row['ros_fact_%']:.2f}%")
        col3.metric("Общие расходы", f"{obj_row['cost_price_fact']:,.0f} ₽")
        col4.metric("Кластер", obj_row['cluster'] if pd.notna(obj_row['cluster']) else "не определён")

        group_sum = trans.groupby('group_name')['value_fact'].sum().reset_index()
        fig_pie = px.pie(group_sum, values='value_fact', names='group_name', title="Распределение затрат по группам")
        st.plotly_chart(fig_pie, use_container_width=True)

        st.subheader("Анализ изменения прибыли (Waterfall)")
        fig_wf, err = plot_waterfall_profit(selected_object_global)
        if fig_wf:
            st.plotly_chart(fig_wf, use_container_width=True)
        else:
            st.info(err)

        st.subheader(f"Анализ кодов затрат для объекта {selected_object_global}")
        n = st.slider("Количество кодов для отображения (N)", 1, 20, 10, 1)
        code_sum = trans.groupby(['expense_id', 'group_name'])['value_fact'].sum().reset_index()
        code_sum = code_sum.sort_values('value_fact', ascending=False)
        total_object_cost = code_sum['value_fact'].sum()
        code_sum['pct'] = code_sum['value_fact'] / total_object_cost * 100

        top_n = code_sum.head(n).copy()
        st.write(f"**🔻 Топ-{n} самых больших кодов затрат**")
        st.dataframe(
            top_n[['expense_id', 'group_name', 'value_fact', 'pct']].rename(
                columns={'expense_id': 'Код', 'group_name': 'Группа', 'value_fact': 'Сумма (руб)', 'pct': 'Доля (%)'}
            ),
            use_container_width=True
        )

        positive = code_sum[code_sum['value_fact'] > 0]
        smallest_n = positive.tail(n).sort_values('value_fact', ascending=True).copy()
        if not smallest_n.empty:
            st.write(f"**🟢 Топ-{n} самых маленьких положительных кодов затрат**")
            st.dataframe(
                smallest_n[['expense_id', 'group_name', 'value_fact', 'pct']].rename(
                    columns={'expense_id': 'Код', 'group_name': 'Группа', 'value_fact': 'Сумма (руб)', 'pct': 'Доля (%)'}
                ),
                use_container_width=True
            )
        else:
            st.info("Нет положительных кодов затрат для этого объекта.")
else:
    # -------------------- РЕЖИМ ВСЕХ ОБЪЕКТОВ --------------------
    if selected_clusters:
        filtered_objects = objects[objects['cluster'].isin(selected_clusters)]
        filtered_cost_stats = cost_stats[cost_stats['cluster_simple'].isin(selected_clusters)]
    else:
        filtered_objects = objects
        filtered_cost_stats = cost_stats

    st.subheader("💰 Прибыль по объектам (факт)")
    fig1 = px.bar(
        filtered_objects,
        x="object_id",
        y="profit_fact",
        color="cluster",
        title="Фактическая прибыль объектов",
        labels={"profit_fact": "Прибыль (руб)", "object_id": "Объект"}
    )
    fig1.update_layout(xaxis_tickangle=-45)
    fig1.update_xaxes(type='category')
    st.plotly_chart(fig1, use_container_width=True)

    st.subheader("📊 Структура затрат по кластерам (доли в %)")
    cost_agg = filtered_cost_stats.groupby(['cluster_simple', 'group_name'])['pct_of_cluster'].sum().reset_index()
    if not cost_agg.empty:
        fig2 = px.bar(
            cost_agg,
            x="pct_of_cluster",
            y="group_name",
            color="cluster_simple",
            orientation='h',
            title="Доли групп затрат по кластерам",
            labels={"pct_of_cluster": "Доля от общих затрат кластера (%)", "group_name": "Группа затрат"}
        )
        st.plotly_chart(fig2, use_container_width=True)
    else:
        st.info("Нет данных для выбранных кластеров")

    st.subheader("🏷️ Топ-5 кодов затрат в кластере")
    if not filtered_cost_stats.empty:
        cluster_choice = st.selectbox("Выберите кластер", sorted(filtered_cost_stats['cluster_simple'].unique()))
        top_codes = filtered_cost_stats[filtered_cost_stats['cluster_simple'] == cluster_choice].nlargest(5, "pct_of_cluster")
        code_col = next((col for col in ['expense_id', 'expenseid', 'code'] if col in top_codes.columns), None)
        if code_col:
            st.dataframe(top_codes[[code_col, 'group_name', 'pct_of_cluster']].rename(
                columns={code_col: 'Код', 'group_name': 'Группа', 'pct_of_cluster': 'Доля (%)'}
            ), use_container_width=True)
        else:
            st.warning(f"Не найдена колонка с кодом затрат. Доступные колонки: {list(top_codes.columns)}")
    else:
        st.info("Нет данных для выбранных кластеров")

    st.subheader("🔍 Детальный анализ объекта")
    available_objects = filtered_objects['object_id'].unique()
    if len(available_objects) > 0:
        selected_object = st.selectbox("Выберите объект для детального анализа", options=available_objects)
        if selected_object:
            obj_row = filtered_objects[filtered_objects['object_id'] == selected_object].iloc[0]
            col1, col2, col3, col4 = st.columns(4)
            col1.metric("Прибыль", f"{obj_row['profit_fact']:,.0f} ₽")
            col2.metric("Рентабельность", f"{obj_row['ros_fact_%']:.2f}%")
            col3.metric("Общие расходы", f"{obj_row['cost_price_fact']:,.0f} ₽")
            col4.metric("Кластер", obj_row['cluster'] if pd.notna(obj_row['cluster']) else "не определён")

            trans = load_object_transactions(selected_object)
            if not trans.empty:
                group_sum = trans.groupby('group_name')['value_fact'].sum().reset_index()
                fig_pie = px.pie(group_sum, values='value_fact', names='group_name', title="Распределение затрат по группам")
                st.plotly_chart(fig_pie, use_container_width=True)

                st.subheader("Анализ изменения прибыли (Waterfall)")
                fig_wf, err = plot_waterfall_profit(selected_object)
                if fig_wf:
                    st.plotly_chart(fig_wf, use_container_width=True)
                else:
                    st.info(err)

                st.subheader(f"Анализ кодов затрат для объекта {selected_object}")
                n = st.slider("Количество кодов для отображения (N)", 1, 20, 10, 1)
                code_sum = trans.groupby(['expense_id', 'group_name'])['value_fact'].sum().reset_index()
                code_sum = code_sum.sort_values('value_fact', ascending=False)
                total_object_cost = code_sum['value_fact'].sum()
                code_sum['pct'] = code_sum['value_fact'] / total_object_cost * 100

                top_n = code_sum.head(n).copy()
                st.write(f"**🔻 Топ-{n} самых больших кодов затрат**")
                st.dataframe(
                    top_n[['expense_id', 'group_name', 'value_fact', 'pct']].rename(
                        columns={'expense_id': 'Код', 'group_name': 'Группа', 'value_fact': 'Сумма (руб)', 'pct': 'Доля (%)'}
                    ),
                    use_container_width=True
                )

                positive = code_sum[code_sum['value_fact'] > 0]
                smallest_n = positive.tail(n).sort_values('value_fact', ascending=True).copy()
                if not smallest_n.empty:
                    st.write(f"**🟢 Топ-{n} самых маленьких положительных кодов затрат**")
                    st.dataframe(
                        smallest_n[['expense_id', 'group_name', 'value_fact', 'pct']].rename(
                            columns={'expense_id': 'Код', 'group_name': 'Группа', 'value_fact': 'Сумма (руб)', 'pct': 'Доля (%)'}
                        ),
                        use_container_width=True
                    )
                else:
                    st.info("Нет положительных кодов затрат для этого объекта.")
            else:
                st.info("Нет транзакций для выбранного объекта")
    else:
        st.warning("Нет объектов для отображения")

    st.subheader("⚖️ Сравнение двух объектов")
    if len(available_objects) >= 2:
        obj1 = st.selectbox("Первый объект", options=available_objects, key="obj1")
        obj2 = st.selectbox("Второй объект", options=available_objects, key="obj2")
        if obj1 and obj2 and obj1 != obj2:
            row1 = filtered_objects[filtered_objects['object_id'] == obj1].iloc[0]
            row2 = filtered_objects[filtered_objects['object_id'] == obj2].iloc[0]
            comp_df = pd.DataFrame({
                'Показатель': ['Прибыль (руб)', 'Рентабельность (%)', 'Общие расходы (руб)', 'Кластер'],
                obj1: [f"{row1['profit_fact']:,.0f}", f"{row1['ros_fact_%']:.2f}", f"{row1['cost_price_fact']:,.0f}", row1['cluster']],
                obj2: [f"{row2['profit_fact']:,.0f}", f"{row2['ros_fact_%']:.2f}", f"{row2['cost_price_fact']:,.0f}", row2['cluster']]
            })
            st.table(comp_df)
    else:
        st.info("Недостаточно объектов для сравнения (нужно хотя бы два)")

    st.subheader("📈 Корреляция между группами затрат по выбранному кластеру")
    if not all_trans.empty:
        if selected_clusters:
            objects_in_clusters = objects[objects['cluster'].isin(selected_clusters)]['object_id']
            all_trans_filtered = all_trans[all_trans['object_id'].isin(objects_in_clusters)]
        else:
            all_trans_filtered = all_trans
        pivot = all_trans_filtered.pivot_table(
            index='object_id', 
            columns='group_name', 
            values='value_fact', 
            aggfunc='sum', 
            fill_value=0
        )
        if pivot.shape[1] > 1:
            pivot_pct = pivot.div(pivot.sum(axis=1), axis=0) * 100
            corr_matrix = pivot_pct.corr()
            fig_corr = px.imshow(
                corr_matrix, 
                text_auto=True, 
                aspect="auto", 
                title="Корреляция между группами затрат (по долям, %)",
                labels=dict(x="Группа затрат", y="Группа затрат", color="Корреляция")
            )
            st.plotly_chart(fig_corr, use_container_width=True)
            with st.expander("📋 Таблица корреляций"):
                st.dataframe(corr_matrix, use_container_width=True)
        else:
            st.info("Недостаточно групп затрат для построения корреляционной матрицы (нужно хотя бы 2 группы).")
    else:
        st.info("Нет данных для расчёта корреляций.")

    if 'selected_object' in locals() and selected_object:
        obj_cluster = filtered_objects[filtered_objects['object_id'] == selected_object]['cluster'].values[0]
        if pd.notna(obj_cluster):
            st.subheader("💡 Рекомендации по оптимизации")
            if obj_cluster == 0:
                st.markdown("""
                **Кластер 0 – объекты с преобладанием собственных работ (>70%)**  
                • Оптимизируйте использование **строительных часов**, **техники** и **материалов**.  
                • Следите за кодами затрат, которые входят в топ-5 кластера:  
                """)
            else:
                st.markdown("""
                **Кластер 1 – объекты со значительной долей субподряда (>50%)**  
                • Пересмотрите договоры с **субподрядчиками**, особенно по кодам с наибольшими расходами.  
                • Контролируйте стоимость **материалов** и **строительных часов**, переданных на субподряд.  
                """)
        else:
            st.info("Кластер объекта не определён, рекомендации недоступны")

# -------------------- ПРЕДСКАЗАНИЕ КЛАСТЕРА (ИСПРАВЛЕННОЕ) --------------------
st.subheader("🔮 Предсказать кластер для нового объекта")

CLUSTER_PROFILES = {
    0: {'материалы': 0.308, 'офисные затраты': 0.103, 'строительные часы': 0.122, 'субподряд': 0.299},
    1: {'материалы': 0.006, 'офисные затраты': 0.000, 'строительные часы': 0.063, 'субподряд': 0.417}
}

if "share_mat" not in st.session_state:
    st.session_state.share_mat = 0.4
    st.session_state.share_office = 0.1
    st.session_state.share_hours = 0.3
    st.session_state.share_sub = 0.2

col_btn1, col_btn2 = st.columns(2)
with col_btn1:
    if st.button("📌 Заполнить профиль кластера 0"):
        st.session_state.share_mat = CLUSTER_PROFILES[0]['материалы']
        st.session_state.share_office = CLUSTER_PROFILES[0]['офисные затраты']
        st.session_state.share_hours = CLUSTER_PROFILES[0]['строительные часы']
        st.session_state.share_sub = CLUSTER_PROFILES[0]['субподряд']
        st.rerun()
with col_btn2:
    if st.button("📌 Заполнить профиль кластера 1"):
        st.session_state.share_mat = CLUSTER_PROFILES[1]['материалы']
        st.session_state.share_office = CLUSTER_PROFILES[1]['офисные затраты']
        st.session_state.share_hours = CLUSTER_PROFILES[1]['строительные часы']
        st.session_state.share_sub = CLUSTER_PROFILES[1]['субподряд']
        st.rerun()

with st.form("predict_form"):
    st.markdown("**Введите доли затрат (4 признака):**")
    share_mat = st.slider("Доля материалов", 0.0, 1.0, st.session_state.share_mat, 0.01)
    share_office = st.slider("Доля офисных затрат", 0.0, 1.0, st.session_state.share_office, 0.01)
    share_hours = st.slider("Доля строительных часов", 0.0, 1.0, st.session_state.share_hours, 0.01)
    share_sub = st.slider("Доля субподряда", 0.0, 1.0, st.session_state.share_sub, 0.01)
    
    submitted = st.form_submit_button("🔮 Предсказать")
    
    if submitted:
        total = share_mat + share_office + share_hours + share_sub
        if total == 0:
            st.error("Сумма долей не может быть нулевой.")
        else:
            norm_mat = share_mat / total
            norm_office = share_office / total
            norm_hours = share_hours / total
            norm_sub = share_sub / total
            
            fig_pie_pred = px.pie(
                names=['Материалы', 'Офисные затраты', 'Строительные часы', 'Субподряд'],
                values=[norm_mat, norm_office, norm_hours, norm_sub],
                title="Введённое распределение затрат (после нормализации)"
            )
            st.plotly_chart(fig_pie_pred, use_container_width=True)
            
            compare_df = pd.DataFrame({
                'Признак': ['Материалы', 'Офисные затраты', 'Строительные часы', 'Субподряд'],
                'Введено': [norm_mat, norm_office, norm_hours, norm_sub],
                'Среднее по кластеру 0': [CLUSTER_PROFILES[0]['материалы'], CLUSTER_PROFILES[0]['офисные затраты'],
                                           CLUSTER_PROFILES[0]['строительные часы'], CLUSTER_PROFILES[0]['субподряд']],
                'Среднее по кластеру 1': [CLUSTER_PROFILES[1]['материалы'], CLUSTER_PROFILES[1]['офисные затраты'],
                                           CLUSTER_PROFILES[1]['строительные часы'], CLUSTER_PROFILES[1]['субподряд']]
            })
            st.subheader("📊 Сравнение с эталонными профилями кластеров")
            st.dataframe(compare_df, use_container_width=True)
            
            try:
                model = joblib.load('gradient_boosting_model.pkl')
                X = [[norm_mat, norm_office, norm_hours, norm_sub]]
                pred = int(model.predict(X)[0])
                try:
                    proba = model.predict_proba(X)[0]
                    prob_cluster0 = proba[0] if len(proba) > 0 else None
                    prob_cluster1 = proba[1] if len(proba) > 1 else None
                except:
                    prob_cluster0 = prob_cluster1 = None
                desc = "собственные работы (>70%)" if pred == 0 else "субподряд и прочие (>50%)"
                result = f"**Предсказанный кластер: {pred} – {desc}**"
                if prob_cluster0 is not None:
                    result += f"\n\nВероятность кластера 0: {prob_cluster0:.2%}\nВероятность кластера 1: {prob_cluster1:.2%}"
                st.success(result)
            except Exception as e:
                st.error(f"Ошибка загрузки модели: {e}")

# -------------------- ЗАГРУЗКА EXCEL (СО ВСЕМИ ГРУППАМИ И СРАВНЕНИЕМ) --------------------
st.subheader("📁 Анализ нового объекта по Excel-файлу")
st.markdown("Загрузите Excel-файл с листами `fact_transactions` и `dim_expenses` (как в исходных данных).")
uploaded_file = st.file_uploader("Выберите Excel-файл", type=["xlsx", "xls"])
if uploaded_file is not None:
    with st.spinner("Обработка файла..."):
        shares_model, code_summary, group_totals_all, total_cost = process_transactions_file(uploaded_file)
        if shares_model is None:
            st.error(total_cost)
        else:
            st.success(f"Общая сумма расходов: {total_cost:,.0f} руб.")
            
            # --- ВЫРУЧКА, ПРИБЫЛЬ (если есть в файле) ---
            revenue = None
            profit = None
            ros = None
            try:
                xl = pd.ExcelFile(uploaded_file)
                if 'dim_objects' in xl.sheet_names:
                    dim_objects = pd.read_excel(uploaded_file, sheet_name='dim_objects')
                    dim_objects.columns = dim_objects.columns.str.lower().str.strip()
                    if 'contract_price_fact' in dim_objects.columns:
                        revenue = dim_objects['contract_price_fact'].iloc[0]
                        profit = revenue - total_cost
                        ros = (profit / total_cost * 100) if total_cost > 0 else 0
            except:
                pass
            
            if revenue is not None:
                col1, col2, col3 = st.columns(3)
                col1.metric("💰 Выручка", f"{revenue:,.0f} ₽")
                col2.metric("💵 Прибыль", f"{profit:,.0f} ₽")
                col3.metric("📈 Рентабельность затрат (ROM)", f"{ros:.2f}%")
                st.divider()
            
            st.subheader("📊 Распределение затрат по группам (все группы)")
            group_df_all = pd.DataFrame({
                'Группа': group_totals_all.index,
                'Сумма (руб)': group_totals_all.values,
                'Доля (%)': (group_totals_all.values / total_cost) * 100
            })
            group_df_all = group_df_all.sort_values('Сумма (руб)', ascending=False)
            st.dataframe(group_df_all, use_container_width=True)
            
            # Также показываем барплот по всем группам
            fig_all_groups = px.bar(group_df_all, x='Группа', y='Доля (%)', title="Доли групп затрат (все группы)")
            st.plotly_chart(fig_all_groups, use_container_width=True)
            
            # --- Нормировка для модели (только 4 группы) ---
            norm_mat = shares_model.get('материалы', 0)
            norm_office = shares_model.get('офисные затраты', 0)
            norm_hours = shares_model.get('строительные часы', 0)
            norm_sub = shares_model.get('субподряд', 0)
            total_norm = norm_mat + norm_office + norm_hours + norm_sub
            if total_norm > 0:
                norm_mat /= total_norm
                norm_office /= total_norm
                norm_hours /= total_norm
                norm_sub /= total_norm
            
            # --- СРАВНЕНИЕ С ЭТАЛОННЫМИ ПРОФИЛЯМИ ---
            st.subheader("📊 Сравнение с эталонными профилями кластеров (4 группы)")
            compare_excel_df = pd.DataFrame({
                'Признак': ['Материалы', 'Офисные затраты', 'Строительные часы', 'Субподряд'],
                'Из файла': [norm_mat, norm_office, norm_hours, norm_sub],
                'Среднее по кластеру 0': [CLUSTER_PROFILES[0]['материалы'], CLUSTER_PROFILES[0]['офисные затраты'],
                                           CLUSTER_PROFILES[0]['строительные часы'], CLUSTER_PROFILES[0]['субподряд']],
                'Среднее по кластеру 1': [CLUSTER_PROFILES[1]['материалы'], CLUSTER_PROFILES[1]['офисные затраты'],
                                           CLUSTER_PROFILES[1]['строительные часы'], CLUSTER_PROFILES[1]['субподряд']]
            })
            st.dataframe(compare_excel_df, use_container_width=True)
            
            try:
                model = joblib.load('gradient_boosting_model.pkl')
                X = [[norm_mat, norm_office, norm_hours, norm_sub]]
                pred = int(model.predict(X)[0])
                desc = "собственные работы (>70%)" if pred == 0 else "субподряд и прочие (>50%)"
                st.subheader(f"🔮 Предсказанный кластер: {pred} – {desc}")
            except Exception as e:
                st.error(f"Ошибка предсказания: {e}")
            
            st.subheader("💰 Топ-10 кодов затрат (из файла)")
            top10 = code_summary.head(10).copy()
            st.dataframe(
                top10[['expense_id', 'group_name', 'value_fact', 'pct']].rename(
                    columns={'expense_id': 'Код', 'group_name': 'Группа', 'value_fact': 'Сумма (руб)', 'pct': 'Доля (%)'}
                ),
                use_container_width=True
            )
            
            st.subheader("🟢 Топ-10 наименьших положительных кодов затрат (из файла)")
            positive = code_summary[code_summary['value_fact'] > 0]
            smallest10 = positive.tail(10).sort_values('value_fact', ascending=True).copy()
            if not smallest10.empty:
                st.dataframe(
                    smallest10[['expense_id', 'group_name', 'value_fact', 'pct']].rename(
                        columns={'expense_id': 'Код', 'group_name': 'Группа', 'value_fact': 'Сумма (руб)', 'pct': 'Доля (%)'}
                    ),
                    use_container_width=True
                )
            else:
                st.info("Нет положительных кодов затрат.")