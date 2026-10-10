# Examples

Realistic queries in each supported dialect, next to the PySpark code
SparkShift generates for them. The queries run against the tables the test
suite uses (`customers`, `orders`, and `products`).

Every example is verified: tests fail if a generated file differs from what
SparkShift produces today, and each example runs on Apache Spark and must
return the same result as the original SQL (written in Spark SQL where the
dialect differs).

| Dialect | Query | Generated PySpark | What it shows |
|---|---|---|---|
| Generic SQL | [revenue_ranking.sql](sql/generic/revenue_ranking.sql) | [revenue_ranking.py](pyspark/generic/revenue_ranking.py) | A CTE, a join, and `RANK()` over the result |
| PostgreSQL | [latest_order.sql](sql/postgres/latest_order.sql) | [latest_order.py](pyspark/postgres/latest_order.py) | The latest row per group with `ROW_NUMBER()`; PostgreSQL's NULL ordering spelled out |
| T-SQL | [top_customers.sql](sql/tsql/top_customers.sql) | [top_customers.py](pyspark/tsql/top_customers.py) | `TOP 3` as a sort and limit; `LEN`, which ignores trailing spaces |
| MySQL | [name_lengths.sql](sql/mysql/name_lengths.sql) | [name_lengths.py](pyspark/mysql/name_lengths.py) | MySQL's `LENGTH` counts bytes (`octet_length`); `IFNULL` |
| Snowflake | [customers_without_orders.sql](sql/snowflake/customers_without_orders.sql) | [customers_without_orders.py](pyspark/snowflake/customers_without_orders.py) | A correlated `NOT EXISTS`, with `.outer()` |
| BigQuery | [monthly_revenue.sql](sql/bigquery/monthly_revenue.sql) | [monthly_revenue.py](pyspark/bigquery/monthly_revenue.py) | `DATE_TRUNC(d, MONTH)`, which returns a date, in a subquery |
| Oracle | [never_cancelled.sql](sql/oracle/never_cancelled.sql) | [never_cancelled.py](pyspark/oracle/never_cancelled.py) | `MINUS` as `subtract`; `NVL` |

The generated files are produced by the command line, one folder per dialect:

```bash
uv run python examples/regenerate.py
```
