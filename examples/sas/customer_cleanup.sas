/* Customers from data lines, cleaned (PROPCASE, UPCASE, IF/ELSE, YEAR),
   sorted, and printed. SAS truncates two of the new values: see the warnings. */
data customers_raw;
    length customer_id 8 name $30 country $20 email $60;
    format signup_date date9.;
    input customer_id name $ country $ age signup_date :date9. email $;
    datalines;
101 John USA 25 01JAN2024 john@test.com
102 Sarah UK 32 15FEB2024 sarah@test.com
103 Michael USA . 20MAR2024 michael@test.com
104 Emma Germany 17 05APR2024 emma@test.com
105 David France 45 12MAY2024 david@test.com
106 Lisa USA 29 15JUN2024 lisa@test.com
107 James UK 16 01JUL2024 james@test.com
108 Anna Germany 35 10AUG2024 anna@test.com
;
run;

data customers_clean;
    set customers_raw;

    name = propcase(name);
    country = upcase(country);

    if missing(age) then age = 0;

    if age >= 18 then customer_status = "ADULT";
    else customer_status = "MINOR";

    if age >= 30 then age_group = "30_PLUS";
    else age_group = "UNDER_30";

    signup_year = year(signup_date);
    signup_month = month(signup_date);

    if country = "USA" or country = "UK" then
        region = "ANGLO";
    else region = "EUROPE";

    format signup_date date9.;
run;

proc sort data=customers_clean;
    by country customer_id;
run;

proc print data=customers_clean;
run;
