from pyspark.sql import Window
from pyspark.sql import functions as F

window = (
    Window.partitionBy(F.col("customer_id"))
    .orderBy(
        F.col("order_date").desc_nulls_first(),
        F.col("order_id").desc_nulls_first(),
    )
)

result = (
    spark.table("orders")
    .select(
        F.col("customer_id"),
        F.col("order_id"),
        F.col("order_date"),
        F.col("amount"),
        F.row_number().over(window).alias("_qualify_1"),
    )
    .where(F.col("_qualify_1") == F.lit(1))
    .drop("_qualify_1")
    .orderBy(F.col("customer_id").asc_nulls_last())
)
