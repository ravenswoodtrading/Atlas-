"""Small additive migration for exports imported before cost detail was retained."""
from sqlalchemy import inspect, text


def migrate_reporting_schema(engine):
    if 'va_sales_lines' in inspect(engine).get_table_names():
        if 'cog' not in {c['name'] for c in inspect(engine).get_columns('va_sales_lines')}:
            with engine.begin() as connection:
                connection.execute(text('ALTER TABLE va_sales_lines ADD COLUMN cog FLOAT'))
