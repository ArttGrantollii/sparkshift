from pyspark.sql import Window
from pyspark.sql import functions as F

window = (
    Window.partitionBy(F.col("customer_id"))
    .orderBy(
        F.col("order_date").desc_nulls_first(),
        F.col("order_id").desc_nulls_first(),
    )
)

numbered = (
    spark.table("orders")
    .select(
        F.col("customer_id"),
        F.col("order_id"),
        F.col("order_date"),
        F.col("amount"),
        F.row_number().over(window).alias("recency"),
    )
)

result = (
    numbered
    .where(F.col("recency") == F.lit(1))
    .select(
        F.col("customer_id"),
        F.col("order_id"),
        F.col("order_date"),
        F.col("amount"),
    )
    .orderBy(F.col("amount").asc_nulls_last())
)
