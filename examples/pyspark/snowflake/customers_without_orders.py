from pyspark.sql import functions as F

subquery = (
    spark.table("orders").alias("o")
    .where(F.col("o.customer_id") == F.col("c.customer_id").outer())
    .select(F.lit(1))
)

result = (
    spark.table("customers").alias("c")
    .where(~subquery.exists())
    .select(
        F.col("c.customer_id"),
        F.col("c.name"),
        F.col("c.country"),
    )
    .orderBy(F.col("name").asc_nulls_last())
)
