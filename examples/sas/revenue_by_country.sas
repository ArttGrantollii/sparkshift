/* Revenue by customer country with PROC SQL. Order 106's customer has no
   country: GROUP BY keeps missing values as a group. */
proc sql;
    create table revenue_by_country as
    select c.country, count(*) as orders, sum(o.amount) as revenue
    from orders o
    inner join customers c on o.customer_id = c.customer_id
    group by c.country
    order by revenue desc;
quit;
