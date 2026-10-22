from pyspark.sql import functions as F

accounts = spark.createDataFrame(
    [
        (1001.0, 101.0, 15000.0, "SAVINGS"),
        (1002.0, 102.0, 25000.0, "CHECKING"),
        (1003.0, 103.0, 8000.0, "SAVINGS"),
        (1004.0, 104.0, 120000.0, "BUSINESS"),
        (1005.0, 105.0, 45000.0, "CHECKING"),
    ],
    "`account_id` double, `customer_id` double, `balance` double, "
    "`account_type` string",
)

transactions = spark.createDataFrame(
    [
        (1.0, 1001.0, 500.0, "DEPOSIT"),
        (2.0, 1001.0, 200.0, "WITHDRAW"),
        (3.0, 1002.0, 1500.0, "DEPOSIT"),
        (4.0, 1002.0, 300.0, "WITHDRAW"),
        (5.0, 1003.0, 700.0, "DEPOSIT"),
        (6.0, 1004.0, 5000.0, "WITHDRAW"),
        (7.0, 1004.0, 10000.0, "DEPOSIT"),
        (8.0, 1005.0, 250.0, "DEPOSIT"),
        (9.0, 1005.0, 100.0, "WITHDRAW"),
        (10.0, 1001.0, 1200.0, "DEPOSIT"),
    ],
    "`transaction_id` double, `account_id` double, `amount` double, "
    "`transaction_type` string",
)

transaction_summary = (
    accounts.alias("a")
    .join(
        transactions.alias("t"),
        F.col("a.account_id").eqNullSafe(F.col("t.account_id")),
        "left",
    )
    .groupBy(
        F.col("a.account_id"),
        F.col("a.customer_id"),
        F.col("a.account_type"),
        F.col("a.balance"),
    )
    .agg(
        F.count(F.col("t.transaction_id")).alias("transaction_count"),
        F.sum(
            F.when(
                F.coalesce(F.rtrim(F.col("t.transaction_type")), F.lit(""))
                == F.lit("DEPOSIT"),
                F.col("t.amount"),
            )
            .otherwise(F.lit(0)),
        ).alias("total_deposits"),
        F.sum(
            F.when(
                F.coalesce(F.rtrim(F.col("t.transaction_type")), F.lit(""))
                == F.lit("WITHDRAW"),
                F.col("t.amount"),
            )
            .otherwise(F.lit(0)),
        ).alias("total_withdrawals"),
    )
    .withColumn("net_change", F.col("total_deposits") - F.col("total_withdrawals"))
    .withColumn("final_balance", F.col("balance") + F.col("net_change"))
    .orderBy(F.col("account_id").asc())
)

account_risk = (
    transaction_summary
    .withColumn(
        "risk_level",
        F.when(
            F.col("final_balance").isNull() | (F.col("final_balance") < F.lit(10000)),
            F.lit("HIGH"),
        )
        .when(
            F.col("final_balance").isNull() | (F.col("final_balance") < F.lit(30000)),
            F.lit("MEDIUM"),
        )
        .otherwise(F.lit("LOW")),
    )
    .withColumn(
        "vip_flag",
        F.when(
            F.col("final_balance").isNotNull()
            & (F.col("final_balance") > F.lit(50000)),
            F.lit(1),
        )
        .otherwise(F.lit(0)),
    )
)

result = account_risk
