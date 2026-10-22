/* Accounts and transactions from data lines; PROC SQL with a LEFT JOIN,
   CASE inside SUM, and CALCULATED columns; then a DATA step and PROC PRINT. */
data accounts;
    input account_id customer_id balance account_type $;
    datalines;
1001 101 15000 SAVINGS
1002 102 25000 CHECKING
1003 103 8000 SAVINGS
1004 104 120000 BUSINESS
1005 105 45000 CHECKING
;
run;

data transactions;
    input transaction_id account_id amount transaction_type $;
    datalines;
1 1001 500 DEPOSIT
2 1001 200 WITHDRAW
3 1002 1500 DEPOSIT
4 1002 300 WITHDRAW
5 1003 700 DEPOSIT
6 1004 5000 WITHDRAW
7 1004 10000 DEPOSIT
8 1005 250 DEPOSIT
9 1005 100 WITHDRAW
10 1001 1200 DEPOSIT
;
run;

proc sql;
    create table transaction_summary as
    select
        a.account_id,
        a.customer_id,
        a.account_type,
        a.balance,
        count(t.transaction_id) as transaction_count,

        sum(case
            when t.transaction_type = "DEPOSIT"
            then t.amount
            else 0
        end) as total_deposits,

        sum(case
            when t.transaction_type = "WITHDRAW"
            then t.amount
            else 0
        end) as total_withdrawals,

        calculated total_deposits -
        calculated total_withdrawals as net_change,

        a.balance + calculated net_change as final_balance

    from accounts a
    left join transactions t
        on a.account_id = t.account_id

    group by
        a.account_id,
        a.customer_id,
        a.account_type,
        a.balance

    order by a.account_id;
quit;

data account_risk;
    set transaction_summary;

    length risk_level $10;

    if final_balance < 10000 then risk_level = "HIGH";
    else if final_balance < 30000 then risk_level = "MEDIUM";
    else risk_level = "LOW";

    if final_balance > 50000 then vip_flag = 1;
    else vip_flag = 0;
run;

proc print data=account_risk;
run;
