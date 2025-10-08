import calendar
import os
import re
from datetime import datetime, timedelta

import mysql.connector
from flask import Flask, jsonify, render_template, request
from flask_cors import CORS

try:
    from openai import OpenAI  # type: ignore
except Exception:  # pragma: no cover
    OpenAI = None  # Fallback if package is not installed

# Configure DeepSeek via environment variable 'DEEPSEEK_API_KEY'.
# If you prefer to hardcode for a quick test (not recommended for production),
# replace the placeholder below with your real key.
if not os.environ.get('DEEPSEEK_API_KEY'):
    os.environ['DEEPSEEK_API_KEY'] = 'sk-1399f482e2fa45a4bf86a508b9ede22c'

app = Flask(__name__)
CORS(app)  # Enable CORS for all routes

# Database configuration - update with your credentials
db_config = {
    'host': '192.168.0.103',
    'user': 'root',  # Your MySQL username
    'password': '',  # Your MySQL password
    'database': 'U99U_TEST',  # Your database name
    # Some older MySQL/MariaDB servers do not support utf8mb4. Default to utf8
    # and allow override via environment variables if the server supports utf8mb4.
    'charset': os.environ.get('MYSQL_CHARSET', 'utf8'),
    'collation': os.environ.get('MYSQL_COLLATION', 'utf8_general_ci')
}

def get_db_connection():
    return mysql.connector.connect(**db_config)

def get_database_schema_snapshot():
    """Return a lightweight schema description for the assistant prompt."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT table_name, column_name, data_type
            FROM information_schema.columns
            WHERE table_schema = DATABASE()
            ORDER BY table_name, ordinal_position
            """
        )
        rows = cursor.fetchall()
        cursor.close()
        conn.close()
        schema_map = {}
        for table_name, column_name, data_type in rows:
            schema_map.setdefault(table_name, []).append(f"{column_name} {data_type}")
        lines = []
        for table_name, cols in schema_map.items():
            cols_str = ", ".join(cols)
            lines.append(f"- {table_name}({cols_str})")
        return "\n".join(lines)
    except Exception:
        return "(schema unavailable)"

SAFE_SQL_PATTERN = re.compile(r"^\s*SELECT\b[\s\S]*", re.IGNORECASE)
FORBIDDEN_SQL_PATTERN = re.compile(r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|CREATE|REPLACE|RENAME|GRANT|REVOKE|MERGE)\b", re.IGNORECASE)

def is_safe_select_sql(sql: str) -> bool:
    if not SAFE_SQL_PATTERN.match(sql or ""):
        return False
    if FORBIDDEN_SQL_PATTERN.search(sql or ""):
        return False
    # Prevent semicolons to chain multiple statements
    return ";" not in (sql or "").strip()

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/chat', methods=['POST'])
def chat():
    try:
        payload = request.get_json(force=True) or {}
        user_message = (payload.get('message') or '').strip()
        branch_code = (payload.get('branch_code') or '').strip()
        if not user_message:
            return jsonify({'error': 'Message is required'}), 400

        # Compose a system prompt to request a single safe SELECT over the schema
        schema_snapshot = get_database_schema_snapshot()
        system_prompt = (
            "You are a helpful data analyst assistant. "
            "Generate ONE single-line SQL query that is strictly READ-ONLY (SELECT only), "
            "safe for MySQL/MariaDB. Do not include comments or explanations. "
            "Do not include semicolons. Use existing table/column names. "
            "Prefer aggregations and limits for readability."
        )
        user_prompt = (
            f"Schema:\n{schema_snapshot}\n\n"
            f"User question: {user_message}\n"
            f"If a branch filter is needed, the provided branch code is: '{branch_code}'. "
            "Return only the SQL."
        )

        api_key = os.getenv('DEEPSEEK_API_KEY')
        if OpenAI is None or not api_key:
            # Fallback: naive canned response without LLM
            return jsonify({
                'answer': 'AI is not configured. Set DEEPSEEK_API_KEY to enable SQL generation.',
                'rows': [],
                'sql': ''
            })

        client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")
        try:
            chat_completion = client.chat.completions.create(
                model='deepseek-chat',
                messages=[
                    { 'role': 'system', 'content': system_prompt },
                    { 'role': 'user', 'content': user_prompt }
                ],
                temperature=0
            )
        except Exception as api_err:
            err_text = str(api_err)
            if 'Insufficient Balance' in err_text or '402' in err_text:
                return jsonify({
                    'answer': 'The AI service reports insufficient balance. Please top up your DeepSeek account or set a valid API key, then try again.',
                    'rows': [],
                    'sql': ''
                }), 402
            raise
        sql = (chat_completion.choices[0].message.content or '').strip().strip('`')

        if not is_safe_select_sql(sql):
            return jsonify({'error': 'Generated SQL was deemed unsafe', 'sql': sql}), 400

        # Execute the safe SELECT
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(sql)
        rows = cursor.fetchall()
        cursor.close()
        conn.close()

        # Optionally summarize results with the model
        preview_values = rows[:20]
        summarization_prompt = (
            "Summarize the following tabular result succinctly for a business user. "
            "Keep it under 3 sentences.\n\n" + str(preview_values)
        )
        try:
            summary_completion = client.chat.completions.create(
                model='deepseek-chat',
                messages=[
                    { 'role': 'system', 'content': 'You summarize analytical query results briefly.' },
                    { 'role': 'user', 'content': summarization_prompt }
                ],
                temperature=0
            )
        except Exception:
            # If summarization fails (e.g., balance), still return raw rows without summary
            return jsonify({ 'answer': 'Results returned. AI summarization unavailable (insufficient balance).', 'rows': rows, 'sql': sql })
        answer = (summary_completion.choices[0].message.content or '').strip()

        return jsonify({ 'answer': answer, 'rows': rows, 'sql': sql })

    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/get_sales_recommendations')
def get_sales_recommendations():
    try:
        branch_code = request.args.get('branch_code')
        limit_param = request.args.get('limit', default='10')
        stock_origin = (request.args.get('stock_origin') or 'ALL').upper()
        if not branch_code:
            return jsonify({'error': 'Branch code is required'})
        try:
            limit = max(1, min(int(limit_param), 50))
        except Exception:
            limit = 10
        
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        # Validate branch exists in Stock_Item_Branch_Reorder by sir_branch_code only
        cursor.execute("SELECT 1 FROM Stock_Item_Branch_Reorder WHERE sir_branch_code = %s LIMIT 1", (branch_code,))
        branch_exists = cursor.fetchone()
        if not branch_exists:
            cursor.close()
            conn.close()
            return jsonify({'error': 'Branch Code Not Found'})
        
        # First, find the most recent sale date across both invoice tables
        cursor.execute("""
            SELECT MAX(recent_date) as latest_sale_date FROM (
                SELECT MAX(inv_date) as recent_date FROM Csinvoice
                UNION ALL
                SELECT MAX(inv_date) as recent_date FROM invoice
            ) as all_dates
        """)
        result = cursor.fetchone()
        latest_sale_date = result['latest_sale_date'] if result else datetime.now().date()
        
        # Optimized query rewritten without CTEs for broader MySQL/MariaDB compatibility
        # Optional origin filter clause on Stock_Item.stock_imported
        origin_clause = ""
        if stock_origin == 'IMPORTED':
            origin_clause = " AND si.stock_imported = 1"
        elif stock_origin == 'LOCAL':
            origin_clause = " AND si.stock_imported = 0"

        query = f"""
        SELECT 
            si.stock_code AS product_id,
            si.stock_desc AS product_name,
            COALESCE(asales.sales_last_30_days, 0) AS sales_last_30_days,
            COALESCE(MAX(ws_sum.total_quantity), 0) AS current_stock,
            COALESCE(MAX(sir.sir_min_threshold), 10) AS min_stock_level,
            COALESCE(MAX(sir.sir_reorder_qty), 0) AS recommended_order
        FROM Stock_Item si
        LEFT JOIN (
            SELECT 
                stock_code,
                SUM(total_sold) AS sales_last_30_days
            FROM (
                SELECT 
                    cl.invl_stock_code AS stock_code,
                    SUM(cl.invl_quantity) AS total_sold
                FROM Csinvoice_Line cl
                JOIN Csinvoice c ON cl.invl_number = c.inv_number
                WHERE c.inv_date >= DATE_SUB(%s, INTERVAL 30 DAY)
                GROUP BY cl.invl_stock_code
                UNION ALL
                SELECT 
                    il.invl_stock_code AS stock_code,
                    SUM(il.invl_quantity) AS total_sold
                FROM invoice_line il
                JOIN invoice i ON il.invl_number = i.inv_number
                WHERE i.inv_date >= DATE_SUB(%s, INTERVAL 30 DAY)
                GROUP BY il.invl_stock_code
            ) sd
            GROUP BY stock_code
            HAVING sales_last_30_days > 0
        ) AS asales ON si.stock_code = asales.stock_code
        LEFT JOIN (
            SELECT whse_stock_code, SUM(whse_stock_quantity) AS total_quantity
            FROM WareHouse_Stock
            GROUP BY whse_stock_code
        ) AS ws_sum ON si.stock_code = ws_sum.whse_stock_code
        LEFT JOIN Stock_Item_Branch_Reorder sir 
            ON si.stock_code = sir.sir_stock_code AND sir.sir_branch_code = %s
        WHERE asales.sales_last_30_days IS NOT NULL{origin_clause}
        GROUP BY si.stock_code, si.stock_desc, asales.sales_last_30_days
        ORDER BY asales.sales_last_30_days DESC
        LIMIT %s
        """
        
        cursor.execute(query, (latest_sale_date, latest_sale_date, branch_code, limit))
        products = cursor.fetchall()
        
        cursor.close()
        conn.close()
        
        return jsonify(products)
    
    except Exception as e:
        return jsonify({'error': str(e)})

@app.route('/get_stock_recommendations')
def get_stock_recommendations():
    try:
        branch_code = request.args.get('branch_code')
        limit_param = request.args.get('limit', default='10')
        stock_origin = (request.args.get('stock_origin') or 'ALL').upper()
        if not branch_code:
            return jsonify({'error': 'Branch code is required'})
        try:
            limit = max(1, min(int(limit_param), 50))
        except Exception:
            limit = 10
        
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        # Validate branch exists in Stock_Item_Branch_Reorder by sir_branch_code only
        cursor.execute("SELECT 1 FROM Stock_Item_Branch_Reorder WHERE sir_branch_code = %s LIMIT 1", (branch_code,))
        branch_exists = cursor.fetchone()
        if not branch_exists:
            cursor.close()
            conn.close()
            return jsonify({'error': 'Branch Code Not Found'})
        
        # Get products with low stock levels using precise columns/tables as requested
        origin_clause = ""
        if stock_origin == 'IMPORTED':
            origin_clause = " AND si.stock_imported = 1"
        elif stock_origin == 'LOCAL':
            origin_clause = " AND si.stock_imported = 0"

        query = f"""
        SELECT 
            si.stock_code AS product_id,
            si.stock_desc AS product_name,
            COALESCE(ws.whse_stock_quantity, 0) AS current_stock,
            sir.sir_min_threshold AS min_stock_level,
            sir.sir_reorder_qty AS recommended_order
        FROM Stock_Item_Branch_Reorder sir
        JOIN Stock_Item si ON si.stock_code = sir.sir_stock_code
        LEFT JOIN WareHouse_Stock ws ON ws.whse_stock_code = sir.sir_stock_code
        WHERE sir.sir_branch_code = %s{origin_clause}
          AND COALESCE(ws.whse_stock_quantity, 0) < sir.sir_min_threshold
        ORDER BY COALESCE(ws.whse_stock_quantity, 0) ASC
        LIMIT %s
        """

        cursor.execute(query, (branch_code, limit))
        products = cursor.fetchall()
        
        cursor.close()
        conn.close()
        
        return jsonify(products)
    
    except Exception as e:
        return jsonify({'error': str(e)})

@app.route('/get_product_sales_pattern')
def get_product_sales_pattern():
    try:
        product_code = request.args.get('product_code')

        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        # Validate product code exists in either Csinvoice_Line or invoice_line
        cursor.execute(
            """
            SELECT 1 FROM (
                SELECT 1 AS ok FROM Csinvoice_Line WHERE invl_stock_code = %s LIMIT 1
                UNION ALL
                SELECT 1 AS ok FROM invoice_line WHERE invl_stock_code = %s LIMIT 1
            ) t
            LIMIT 1
            """,
            (product_code, product_code),
        )
        exists_row = cursor.fetchone()
        if not exists_row:
            cursor.close()
            conn.close()
            return jsonify({'error': 'Product Code Not Found'})

        # Get product name from Stock_Item table (fallback to product code if missing)
        cursor.execute("SELECT stock_desc FROM Stock_Item WHERE stock_code = %s", (product_code,))
        product = cursor.fetchone()
        product_name = product['stock_desc'] if product else product_code

        # Find the most recent sale date for this product across both invoice types
        cursor.execute(
            """
            SELECT MAX(latest_date) AS latest_sale_date FROM (
                SELECT MAX(c.inv_date) AS latest_date
                FROM Csinvoice_Line cl
                JOIN Csinvoice c ON cl.invl_number = c.inv_number
                WHERE cl.invl_stock_code = %s
                UNION ALL
                SELECT MAX(i.inv_date) AS latest_date
                FROM invoice_line il
                JOIN invoice i ON il.invl_number = i.inv_number
                WHERE il.invl_stock_code = %s
            ) t
            """,
            (product_code, product_code),
        )
        result = cursor.fetchone()
        latest_sale_date = result['latest_sale_date'] if result and result['latest_sale_date'] else datetime.now().date()

        # Build a 12-month window ending at latest_sale_date
        months_window = []  # list of (year, month) from oldest to newest
        for offset in range(11, -1, -1):
            month_ref = (latest_sale_date.replace(day=1) - timedelta(days=offset * 30))
            # Normalize to first of real month by reconstructing year-month
            months_window.append((month_ref.year, month_ref.month))

        # Aggregate monthly sales per (year, month) for this product
        sales_by_year_month = {}

        # Csinvoice side
        cursor.execute(
            """
            SELECT YEAR(c.inv_date) AS y, MONTH(c.inv_date) AS m, SUM(cl.invl_quantity) AS total_sold
            FROM Csinvoice_Line cl
            JOIN Csinvoice c ON cl.invl_number = c.inv_number
            WHERE cl.invl_stock_code = %s
              AND c.inv_date BETWEEN DATE_SUB(%s, INTERVAL 11 MONTH) AND %s
            GROUP BY YEAR(c.inv_date), MONTH(c.inv_date)
            """,
            (product_code, latest_sale_date, latest_sale_date),
        )
        for row in cursor.fetchall():
            key = (row['y'], row['m'])
            sales_by_year_month[key] = sales_by_year_month.get(key, 0) + (row['total_sold'] or 0)

        # invoice side
        cursor.execute(
            """
            SELECT YEAR(i.inv_date) AS y, MONTH(i.inv_date) AS m, SUM(il.invl_quantity) AS total_sold
            FROM invoice_line il
            JOIN invoice i ON il.invl_number = i.inv_number
            WHERE il.invl_stock_code = %s
              AND i.inv_date BETWEEN DATE_SUB(%s, INTERVAL 11 MONTH) AND %s
            GROUP BY YEAR(i.inv_date), MONTH(i.inv_date)
            """,
            (product_code, latest_sale_date, latest_sale_date),
        )
        for row in cursor.fetchall():
            key = (row['y'], row['m'])
            sales_by_year_month[key] = sales_by_year_month.get(key, 0) + (row['total_sold'] or 0)

        # Build labels and series in months_window order
        monthly_labels = []
        monthly_sales = []
        for (year_val, month_val) in months_window:
            monthly_labels.append(f"{calendar.month_abbr[month_val]} {year_val}")
            monthly_sales.append(sales_by_year_month.get((year_val, month_val), 0))

        cursor.close()
        conn.close()

        return jsonify({
            'product_name': product_name,
            'monthly_sales': monthly_sales,
            'monthly_labels': monthly_labels
        })
    
    except Exception as e:
        return jsonify({'error': str(e)})

if __name__ == '__main__':
    app.run(debug=True, port=5000)