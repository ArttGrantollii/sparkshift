from pyspark.sql import functions as F

orders = spark.table("orders")
customers = spark.table("customers")

revenue_by_country = (
    orders.alias("o")
    .join(
        customers.alias("c"),
        F.col("o.customer_id").eqNullSafe(F.col("c.customer_id")),
        "inner",
    )
    .groupBy(F.col("c.country"))
    .agg(
        F.count(F.lit(1)).alias("orders"),
        F.sum(F.col("o.amount")).alias("revenue"),
    )
    .orderBy(F.col("revenue").desc())
)

result = revenue_by_country
