from pyspark.sql import functions as F

result = (
    spark.table("customers")
    .select(F.col("customer_id"))
    .subtract(
        spark.table("orders")
        .where(F.col("status") == F.lit("cancelled"))
        .select(F.coalesce(F.col("customer_id"), F.lit(-1)).alias("customer_id"))
    )
    .orderBy(F.col("customer_id").asc_nulls_last())
)
