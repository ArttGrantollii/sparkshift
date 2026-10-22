# Examples

Realistic queries in each supported dialect, next to the PySpark code
SparkShift generates for them. The queries run against the tables the test
suite uses (`customers`, `orders`, and `products`); two SAS programs bring
their own data, as data lines.

Every example is verified: tests fail if a generated file differs from what
SparkShift produces today, and each example runs on Apache Spark and must
return the same result as the original SQL (written in Spark SQL where the
dialect differs). The SAS programs, which no SAS installation runs here, must
return rows written by hand from SAS's documented rules
([docs/sas.md](../docs/sas.md)).

| Dialect | Query | Generated PySpark | What it shows |
|---|---|---|---|
| Generic SQL | [revenue_ranking.sql](sql/generic/revenue_ranking.sql) | [revenue_ranking.py](pyspark/generic/revenue_ranking.py) | A CTE, a join, and `RANK()` over the result |
| PostgreSQL | [latest_order.sql](sql/postgres/latest_order.sql) | [latest_order.py](pyspark/postgres/latest_order.py) | The latest row per group with `ROW_NUMBER()`; PostgreSQL's NULL ordering spelled out |
| T-SQL | [top_customers.sql](sql/tsql/top_customers.sql) | [top_customers.py](pyspark/tsql/top_customers.py) | `TOP 3` as a sort and limit; `LEN`, which ignores trailing spaces |
| MySQL | [name_lengths.sql](sql/mysql/name_lengths.sql) | [name_lengths.py](pyspark/mysql/name_lengths.py) | MySQL's `LENGTH` counts bytes (`octet_length`); `IFNULL` |
| Snowflake | [customers_without_orders.sql](sql/snowflake/customers_without_orders.sql) | [customers_without_orders.py](pyspark/snowflake/customers_without_orders.py) | A correlated `NOT EXISTS`, with `.outer()` |
| Snowflake | [latest_order_per_customer.sql](sql/snowflake/latest_order_per_customer.sql) | [latest_order_per_customer.py](pyspark/snowflake/latest_order_per_customer.py) | The PostgreSQL example's latest row per group, written with `QUALIFY`: a helper column, filtered and dropped |
| BigQuery | [monthly_revenue.sql](sql/bigquery/monthly_revenue.sql) | [monthly_revenue.py](pyspark/bigquery/monthly_revenue.py) | `DATE_TRUNC(d, MONTH)`, which returns a date, in a subquery |
| Oracle | [never_cancelled.sql](sql/oracle/never_cancelled.sql) | [never_cancelled.py](pyspark/oracle/never_cancelled.py) | `MINUS` as `subtract`; `NVL` |
| SAS | [customer_ratings.sas](sas/customer_ratings.sas) | [customer_ratings.py](pyspark/sas/customer_ratings.py) | A DATA step with `CATX`, an `IF`/`ELSE IF` chain as one `F.when(...)`, then `PROC SORT` |
| SAS | [revenue_by_country.sas](sas/revenue_by_country.sas) | [revenue_by_country.py](pyspark/sas/revenue_by_country.py) | `PROC SQL` with a join that matches missing values (`eqNullSafe`) and `GROUP BY` |
| SAS | [status_counts.sas](sas/status_counts.sas) | [status_counts.py](pyspark/sas/status_counts.py) | `PROC FREQ`: counts, and percentages that leave missing values out |
| SAS | [customer_cleanup.sas](sas/customer_cleanup.sas) | [customer_cleanup.py](pyspark/sas/customer_cleanup.py) | Data lines (`INPUT`, `DATALINES`) as rows in the code; `PROPCASE`, `YEAR`, and the two values SAS truncates; `PROC SORT` and `PROC PRINT` |
| SAS | [account_risk.sas](sas/account_risk.sas) | [account_risk.py](pyspark/sas/account_risk.py) | `PROC SQL` with a `LEFT JOIN`, `CASE` inside `SUM`, and `CALCULATED` columns added after the summaries |

The generated files are produced by the command line, one folder per dialect
and one for the SAS programs:

```bash
uv run python examples/regenerate.py
```

[coverage/](coverage/README.md) is different: a small mixed workload, not all
of it convertible, for trying `sparkshift report`.
