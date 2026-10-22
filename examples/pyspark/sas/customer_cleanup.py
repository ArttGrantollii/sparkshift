import datetime

from pyspark.sql import functions as F

customers_raw = spark.createDataFrame(
    [
        (101.0, "John", "USA", "john@test.com", datetime.date(2024, 1, 1), 25.0),
        (102.0, "Sarah", "UK", "sarah@test.com", datetime.date(2024, 2, 15), 32.0),
        (103.0, "Michael", "USA", "michael@test.com", datetime.date(2024, 3, 20), None),
        (104.0, "Emma", "Germany", "emma@test.com", datetime.date(2024, 4, 5), 17.0),
        (105.0, "David", "France", "david@test.com", datetime.date(2024, 5, 12), 45.0),
        (106.0, "Lisa", "USA", "lisa@test.com", datetime.date(2024, 6, 15), 29.0),
        (107.0, "James", "UK", "james@test.com", datetime.date(2024, 7, 1), 16.0),
        (108.0, "Anna", "Germany", "anna@test.com", datetime.date(2024, 8, 10), 35.0),
    ],
    "`customer_id` double, `name` string, `country` string, `email` string, "
    "`signup_date` date, `age` double",
)

customers_clean = (
    customers_raw
    .withColumn(
        "name",
        F.regexp_replace(
            F.initcap(F.regexp_replace(F.col("name"), "([-/(.\t])", "$1 ")),
            "([-/(.\t]) ",
            "$1",
        ),
    )
    .withColumn("country", F.upper(F.col("country")))
    .withColumn("age", F.when(F.col("age").isNull(), F.lit(0)).otherwise(F.col("age")))
    .withColumn(
        "customer_status",
        F.when(F.col("age").isNotNull() & (F.col("age") >= F.lit(18)), F.lit("ADULT"))
        .otherwise(F.lit("MINOR")),
    )
    .withColumn(
        "age_group",
        F.when(F.col("age").isNotNull() & (F.col("age") >= F.lit(30)), F.lit("30_PLUS"))
        .otherwise(F.lit("UNDER_3")),
    )
    .withColumn("signup_year", F.year(F.col("signup_date")))
    .withColumn("signup_month", F.month(F.col("signup_date")))
    .withColumn(
        "region",
        F.when(
            (F.coalesce(F.rtrim(F.col("country")), F.lit("")) == F.lit("USA"))
            | (F.coalesce(F.rtrim(F.col("country")), F.lit("")) == F.lit("UK")),
            F.lit("ANGLO"),
        )
        .otherwise(F.lit("EUROP")),
    )
)

customers_clean_2 = (
    customers_clean
    .orderBy(
        F.col("country").asc(),
        F.col("customer_id").asc(),
    )
)

result = customers_clean_2
