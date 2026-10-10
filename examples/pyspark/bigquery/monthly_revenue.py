from pyspark.sql import functions as F

subquery = (
    spark.table("orders")
    .select(
        F.trunc(F.col("order_date"), "month").alias("month"),
        F.col("amount"),
        F.col("customer_id"),
    )
)

result = (
    subquery
    .groupBy(F.col("month"))
    .agg(
        F.sum(F.col("amount")).alias("revenue"),
        F.count_distinct(F.col("customer_id")).alias("customers"),
    )
    .orderBy(F.col("month").asc())
)
