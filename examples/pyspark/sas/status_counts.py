from pyspark.sql import Window
from pyspark.sql import functions as F

window = Window.partitionBy()

status_counts = (
    spark.table("orders")
    .groupBy(F.col("status"))
    .agg(F.count(F.lit(1)).alias("COUNT"))
    .withColumn(
        "_counted",
        F.when(
            ~(F.col("status").isNull() | (F.rtrim(F.col("status")) == F.lit(""))),
            F.col("COUNT"),
        ),
    )
    .withColumn(
        "PERCENT",
        F.col("_counted") * F.lit(100) / F.sum(F.col("_counted")).over(window),
    )
    .drop("_counted")
    .orderBy(F.col("status").asc())
)

result = status_counts
