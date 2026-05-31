import sqlite3
import pandas as pd
from sqlalchemy import create_engine
import os
import re

sqlite_path = '/app/data/stroy_analytics.db'
sqlite_conn = sqlite3.connect(sqlite_path)

pg_host = os.getenv('DB_HOST', 'localhost')
pg_engine = create_engine(f'postgresql://postgres:postgres@{pg_host}:5432/stroy_db')

def clean_column_name(name):
    name = re.sub(r'["\']', '', name)
    name = re.sub(r'\s+', '_', name)
    name = re.sub(r'[^a-zA-Z0-9_]', '_', name)
    name = re.sub(r'_+', '_', name)
    name = name.strip('_')
    return name.lower() if name else 'column'

tables = ['dim_expenses', 'dim_objects', 'fact_transactions', 'dim_objects_sqm', 'cluster_assignments', 'cluster_cost_stats']

for table in tables:
    df = pd.read_sql_query(f"SELECT * FROM {table}", sqlite_conn)
    df.columns = [clean_column_name(col) for col in df.columns]
    df.to_sql(table, pg_engine, if_exists='replace', index=False)
    print(f"✓ {table}: {len(df)} rows")

print("Migration done.")