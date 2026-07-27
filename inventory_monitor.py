"""
Inventory Monitor — Phase 1 + 2 + 3 + 4
อ่าน Google Sheets → เช็คสต็อก → คำนวณ usage rate & days remaining
→ จัดลำดับความเร่งด่วน → ตรวจจับ anomaly
→ Gemini วิเคราะห์ + ร่าง PO → ส่ง LINE alert + บันทึกไฟล์ + สร้าง Dashboard
"""

import os
import gspread
from google.oauth2.service_account import Credentials
import requests
import json
from datetime import datetime
from collections import defaultdict

# ===== CONFIG — อ่านจาก Environment Variables =====
GOOGLE_CREDENTIALS = os.environ["GOOGLE_CREDENTIALS"]
SPREADSHEET_URL = "https://docs.google.com/spreadsheets/d/1xk-BsFjNUXD-lUDligiev_xIWQOwX3wv2PwFE9LCBtQ/edit"
LINE_CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
LINE_USER_ID = os.environ["LINE_USER_ID"]
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "")

BUYER_INFO = {
    "company": "บริษัทตัวอย่าง",
    "address": "-",
    "payment_terms": "เครดิต 30 วัน",
}


# ===== Google Sheets =====

def connect_sheets(credentials_json, spreadsheet_url):
    """เชื่อมต่อ Google Sheets ด้วย service account (จาก JSON string)"""
    scopes = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
    creds_dict = json.loads(credentials_json)
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    client = gspread.authorize(creds)
    return client.open_by_url(spreadsheet_url)


def get_suppliers(spreadsheet):
    """อ่าน sheet Suppliers → list of dict"""
    worksheet = spreadsheet.worksheet("Suppliers")
    return worksheet.get_all_records()


def get_materials(spreadsheet):
    """อ่าน sheet Materials → list of dict"""
    worksheet = spreadsheet.worksheet("Materials")
    return worksheet.get_all_records()


def get_usage_log(spreadsheet):
    """อ่าน sheet Usage_Log → list of dict"""
    worksheet = spreadsheet.worksheet("Usage_Log")
    return worksheet.get_all_records()


# ===== Usage Analysis (Phase 2) =====

def parse_date(date_str):
    """แปลง string วันที่เป็น datetime — รองรับหลายรูปแบบ"""
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(str(date_str), fmt)
        except ValueError:
            continue
    raise ValueError(f"ไม่รู้จักรูปแบบวันที่: {date_str}")


def calc_daily_usage(usage_log):
    """คำนวณอัตราการใช้เฉลี่ยต่อวันของแต่ละ material (เฉพาะ OUT)
    ใช้ช่วงวันเฉพาะของแต่ละ material"""
    out_records = [r for r in usage_log if r["type"] == "OUT"]

    if not out_records:
        return {}

    by_material = defaultdict(list)
    for r in out_records:
        by_material[r["material_id"]].append(r)

    result = {}
    for mid, records in by_material.items():
        dates = [parse_date(r["date"]) for r in records]
        date_range = (max(dates) - min(dates)).days + 1
        total = sum(r["quantity"] for r in records)
        result[mid] = total / date_range

    return result


def calc_days_remaining(materials, daily_usage):
    """เพิ่ม days_remaining และ urgency ให้แต่ละ material"""
    results = []
    for item in materials:
        mid = item["material_id"]
        usage = daily_usage.get(mid, 0)

        if usage > 0:
            days_left = item["current_stock"] / usage
        else:
            days_left = None

        lead_time = item["lead_time_days"]

        if days_left is None:
            urgency = "⚪ ไม่มีข้อมูล"
        elif days_left < lead_time:
            urgency = "🔴 วิกฤต"
        elif days_left < lead_time * 1.5:
            urgency = "🟡 เร่งด่วน"
        else:
            urgency = "🟢 ปกติ"

        results.append({
            **item,
            "avg_daily_usage": round(usage, 2),
            "days_remaining": round(days_left, 1) if days_left is not None else None,
            "urgency": urgency,
        })

    return results


def detect_anomalies(usage_log, daily_usage):
    """ตรวจหาวันที่ใช้ผิดปกติ — เทียบกับค่าเฉลี่ยต่อครั้งที่เบิก + ต้องมีอย่างน้อย 3 ครั้ง"""
    out_records = [r for r in usage_log if r["type"] == "OUT"]

    daily_by_material = defaultdict(lambda: defaultdict(float))
    for r in out_records:
        daily_by_material[r["material_id"]][r["date"]] += r["quantity"]

    anomalies = []
    for mid, dates in daily_by_material.items():
        if len(dates) < 3:
            continue

        quantities = list(dates.values())
        avg_per_day_used = sum(quantities) / len(quantities)

        for date, qty in dates.items():
            if qty > avg_per_day_used * 2:
                anomalies.append({
                    "material_id": mid,
                    "date": date,
                    "quantity": qty,
                    "avg": round(avg_per_day_used, 2),
                    "ratio": round(qty / avg_per_day_used, 1),
                })

    return anomalies


# ===== Phase 3: Gemini Analysis =====

def build_gemini_prompt(results, anomalies, suppliers_lookup):
    """สร้าง prompt สำหรับ Gemini"""
    today = datetime.now().strftime("%Y-%m-%d")

    stock_lines = []
    for r in sorted(results, key=lambda x: x["days_remaining"] if x["days_remaining"] is not None else 999):
        supplier_name = suppliers_lookup.get(r["supplier_id"], "ไม่ทราบ")
        stock_lines.append(
            f"- {r['material_id']} {r['name']}: "
            f"คงเหลือ {r['current_stock']} {r['unit']}, "
            f"ใช้เฉลี่ย {r['avg_daily_usage']} {r['unit']}/วัน, "
            f"เหลือใช้อีก {r['days_remaining']} วัน, "
            f"lead time {r['lead_time_days']} วัน, "
            f"สถานะ {r['urgency']}, "
            f"ราคาต่อหน่วย {r['cost_per_unit']} บาท, "
            f"ซัพพลายเออร์ {supplier_name}"
        )

    anomaly_section = "ไม่พบการใช้ผิดปกติ"
    if anomalies:
        anom_lines = []
        for a in anomalies:
            anom_lines.append(
                f"- {a['material_id']} วันที่ {a['date']}: "
                f"ใช้ {a['quantity']} (เฉลี่ย {a['avg']}, มากกว่า {a['ratio']}x)"
            )
        anomaly_section = "พบการใช้ผิดปกติ:\n" + "\n".join(anom_lines)

    prompt = f"""คุณเป็นนักวิเคราะห์สต็อกวัตถุดิบของ {BUYER_INFO['company']} (ขายครีม/อาหารเสริมออนไลน์)
วันที่วิเคราะห์: {today}

ข้อมูลสถานะสต็อก:
{chr(10).join(stock_lines)}

{anomaly_section}

ให้ทำ 2 ส่วน คั่นด้วยบรรทัด ===PO===

ส่วนที่ 1 — สรุปสถานการณ์ (ส่งผ่าน LINE ต้องกระชับ ไม่เกิน 1500 ตัวอักษร):
- สรุปสถานการณ์สต็อกโดยรวม
- เน้นรายการวิกฤต/เร่งด่วน ระบุเหตุผลที่ต้องสั่งซื้อ
- ถ้ามี anomaly ให้วิเคราะห์สาเหตุที่เป็นไปได้
- ปิดท้ายด้วยจำนวน PO ที่ร่างไว้และมูลค่ารวมโดยประมาณ
- รายงานเฉพาะข้อเท็จจริงจากข้อมูลที่ให้ ห้ามอ้างว่าได้ดำเนินการใดๆ แล้ว
- ใช้ plain text ไม่มี markdown

ส่วนที่ 2 — ร่างใบสั่งซื้อ (PO):
- ออก PO เฉพาะรายการที่สถานะ 🔴 วิกฤต หรือ 🟡 เร่งด่วน
- แยก 1 ใบต่อ 1 ซัพพลายเออร์
- กำหนดจำนวนสั่งซื้อที่เหมาะสม โดยพิจารณาจาก usage rate, lead time, และ reorder point
- แต่ละใบระบุ: เลขที่ PO (PO-2026-07-xxx), วันที่ออก ({today}), ซัพพลายเออร์, รายการ, จำนวน, หน่วย, ราคาต่อหน่วย, รวมเงิน
- ท้ายใบ: รวมเงิน, VAT 7%, ยอดสุทธิ
- กำหนดส่งมอบ = วันที่ออก + lead time ของรายการนั้น
- เงื่อนไขชำระเงิน: {BUYER_INFO['payment_terms']}

ตอบเป็นภาษาไทย ใช้ plain text ไม่มี markdown"""

    return prompt


def call_gemini(prompt):
    """เรียก Gemini API — return response text หรือ raise Exception"""
    from google import genai

    client = genai.Client(api_key=GEMINI_API_KEY)
    response = client.models.generate_content(
        model="gemini-3.5-flash",
        contents=prompt,
    )
    return response.text


def parse_gemini_response(response_text):
    """แยก response เป็น (LINE summary, PO details)"""
    if "===PO===" in response_text:
        parts = response_text.split("===PO===", 1)
        return parts[0].strip(), parts[1].strip()
    return response_text.strip(), ""


def save_po_file(summary, po_details):
    """บันทึกร่าง PO เป็น markdown"""
    today = datetime.now().strftime("%Y-%m-%d")
    filename = "PO_draft.md"

    with open(filename, "w", encoding="utf-8") as f:
        f.write(f"# ร่างใบสั่งซื้อ — {today}\n\n")
        f.write(f"## สรุปสถานการณ์\n\n{summary}\n\n")
        if po_details:
            f.write(f"## ร่าง PO\n\n{po_details}\n")

    return filename


# ===== Phase 4: Dashboard =====

def generate_dashboard(results, anomalies, suppliers_lookup, po_filename=None):
    """สร้าง HTML dashboard จากผลวิเคราะห์"""
    today = datetime.now().strftime("%Y-%m-%d")

    def urgency_key(r):
        if "วิกฤต" in r["urgency"]: return "critical"
        if "เร่งด่วน" in r["urgency"]: return "urgent"
        return "normal"

    table_data = sorted([{
        "id": r["material_id"],
        "name": r["name"],
        "stock": r["current_stock"],
        "unit": r["unit"],
        "reorder": r["reorder_point"],
        "avg": r["avg_daily_usage"],
        "days": r["days_remaining"],
        "lead": r["lead_time_days"],
        "supplier": suppliers_lookup.get(r["supplier_id"], "ไม่ทราบ"),
        "urgency": urgency_key(r),
        "belowReorder": r["current_stock"] <= r["reorder_point"],
    } for r in results], key=lambda x: x["id"])

    anomaly_data = [{
        "id": a["material_id"], "date": str(a["date"]),
        "qty": a["quantity"], "avg": a["avg"], "ratio": a["ratio"],
    } for a in anomalies]

    critical = sum(1 for r in results if "วิกฤต" in r["urgency"])
    urgent = sum(1 for r in results if "เร่งด่วน" in r["urgency"])
    normal = sum(1 for r in results if "ปกติ" in r["urgency"])

    data_json = json.dumps({
        "date": today, "company": BUYER_INFO["company"],
        "items": table_data, "anomalies": anomaly_data,
        "kpi": {"total": len(results), "critical": critical, "urgent": urgent, "normal": normal},
        "poFile": po_filename,
    }, ensure_ascii=False)

    html = f"""<!DOCTYPE html>
<html lang="th">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Inventory Monitor Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ font-family:'Segoe UI',Tahoma,sans-serif; background:#F8FAFC; color:#1F2937; }}
.header {{ background:#1E293B; color:#fff; padding:24px 32px; display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:8px; }}
.header h1 {{ font-size:20px; font-weight:700; letter-spacing:-0.02em; }}
.header .sub {{ font-size:13px; color:#94A3B8; margin-top:2px; }}
.header .date {{ text-align:right; }}
.header .date-label {{ font-size:13px; color:#94A3B8; }}
.header .date-value {{ font-size:15px; font-weight:600; }}
.container {{ max-width:1100px; margin:0 auto; padding:24px 16px; }}
.kpi-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(200px,1fr)); gap:16px; margin-bottom:20px; }}
.kpi {{ background:#fff; border-radius:8px; padding:20px 24px; box-shadow:0 1px 3px rgba(0,0,0,0.08); }}
.kpi-label {{ font-size:13px; color:#6B7280; margin-bottom:4px; }}
.kpi-value {{ font-size:32px; font-weight:700; line-height:1.1; }}
.kpi-sub {{ font-size:12px; color:#9CA3AF; margin-top:4px; }}
.card {{ background:#fff; border-radius:8px; padding:24px; box-shadow:0 1px 3px rgba(0,0,0,0.08); margin-bottom:20px; }}
.card-title {{ font-size:15px; font-weight:600; margin-bottom:16px; }}
.anomaly {{ background:#FFFBEB; border-radius:8px; padding:20px; border:1px solid #FDE68A; margin-bottom:20px; }}
.anomaly-title {{ font-size:15px; font-weight:600; color:#92400E; margin-bottom:12px; }}
.anomaly-item {{ font-size:13px; color:#78350F; line-height:1.6; }}
table {{ width:100%; border-collapse:collapse; font-size:13px; }}
th {{ padding:10px 12px; text-align:left; color:#6B7280; font-weight:600; white-space:nowrap; border-bottom:2px solid #E5E7EB; }}
td {{ padding:10px 12px; border-bottom:1px solid #F3F4F6; }}
tr.critical {{ background:#FEF2F2; }}
.badge {{ display:inline-block; padding:2px 10px; border-radius:99px; font-size:12px; font-weight:600; }}
.badge-critical {{ color:#DC2626; background:#FEF2F2; border:1px solid #FECACA; }}
.badge-urgent {{ color:#D97706; background:#FFFBEB; border:1px solid #FDE68A; }}
.badge-normal {{ color:#059669; background:#F0FDF4; border:1px solid #BBF7D0; }}
.below-reorder {{ color:#DC2626; font-weight:600; }}
.below-tag {{ font-size:11px; color:#DC2626; margin-left:6px; }}
.po-note {{ background:#EFF6FF; border:1px solid #BFDBFE; border-radius:8px; padding:16px 20px; margin-bottom:20px; font-size:14px; color:#1E40AF; }}
.footer {{ text-align:center; font-size:12px; color:#9CA3AF; padding:8px 0 16px; }}
</style>
</head>
<body>

<div class="header">
  <div>
    <h1>Inventory Monitor</h1>
    <div class="sub" id="company"></div>
  </div>
  <div class="date">
    <div class="date-label">วันที่วิเคราะห์</div>
    <div class="date-value" id="report-date"></div>
  </div>
</div>

<div class="container">
  <div class="kpi-grid" id="kpi-grid"></div>
  <div class="card">
    <div class="card-title">วันคงเหลือ vs Lead Time (เรียงตาม Material ID)</div>
    <canvas id="stockChart" height="300"></canvas>
  </div>
  <div id="anomaly-section"></div>
  <div id="po-section"></div>
  <div class="card">
    <div class="card-title">รายละเอียดสต็อกทั้งหมด</div>
    <div style="overflow-x:auto">
      <table>
        <thead><tr>
          <th>สถานะ</th><th>รหัส</th><th>ชื่อ</th><th>คงเหลือ</th>
          <th>ใช้/วัน</th><th>เหลือ (วัน)</th><th>Lead Time</th><th>ซัพพลายเออร์</th>
        </tr></thead>
        <tbody id="stock-table"></tbody>
      </table>
    </div>
  </div>
  <div class="footer">สร้างโดย Inventory Monitor — ข้อมูลจาก Google Sheets + วิเคราะห์โดย Gemini AI</div>
</div>

<script>
const D = {data_json};

document.getElementById('company').textContent = D.company;
document.getElementById('report-date').textContent = D.date;

// KPI Cards
const kpiDefs = [
  {{label:'รายการทั้งหมด', value:D.kpi.total, color:'#1F2937', border:'#94A3B8'}},
  {{label:'วิกฤต', value:D.kpi.critical, color:'#DC2626', border:'#DC2626', sub:'ต้องสั่งซื้อทันที'}},
  {{label:'เร่งด่วน', value:D.kpi.urgent, color:'#D97706', border:'#D97706', sub:'เฝ้าระวัง'}},
  {{label:'ปกติ', value:D.kpi.normal, color:'#059669', border:'#059669'}},
];
const kpiGrid = document.getElementById('kpi-grid');
kpiDefs.forEach(k => {{
  const div = document.createElement('div');
  div.className = 'kpi';
  div.style.borderLeft = '4px solid ' + k.border;
  div.innerHTML = '<div class="kpi-label">' + k.label + '</div>'
    + '<div class="kpi-value" style="color:' + k.color + '">' + k.value + '</div>'
    + (k.sub ? '<div class="kpi-sub">' + k.sub + '</div>' : '');
  kpiGrid.appendChild(div);
}});

// Chart
const ctx = document.getElementById('stockChart').getContext('2d');
const items = D.items;
new Chart(ctx, {{
  type: 'bar',
  data: {{
    labels: items.map(i => i.id),
    datasets: [
      {{
        label: 'เหลือใช้ (วัน)',
        data: items.map(i => i.days),
        backgroundColor: items.map(i => i.urgency === 'critical' ? '#DC2626' : i.urgency === 'urgent' ? '#D97706' : '#3B82F6'),
        borderRadius: 3,
        barPercentage: 0.6,
      }},
      {{
        label: 'Lead Time (วัน)',
        data: items.map(i => i.lead),
        backgroundColor: '#CBD5E1',
        borderRadius: 3,
        barPercentage: 0.6,
      }}
    ]
  }},
  options: {{
    indexAxis: 'y',
    responsive: true,
    maintainAspectRatio: false,
    plugins: {{
      legend: {{ labels: {{ font: {{ size: 12 }} }} }},
      tooltip: {{
        callbacks: {{
          afterBody: function(ctx) {{
            const idx = ctx[0].dataIndex;
            const item = items[idx];
            const gap = item.days - item.lead;
            return gap < 0
              ? '\\nขาดมือ ' + Math.abs(gap).toFixed(1) + ' วัน'
              : '\\nเพียงพอ +' + gap.toFixed(1) + ' วัน';
          }}
        }}
      }}
    }},
    scales: {{
      x: {{ grid: {{ display: false }}, ticks: {{ font: {{ size: 12 }}, callback: v => v + ' วัน' }} }},
      y: {{ grid: {{ display: false }}, ticks: {{ font: {{ size: 12 }} }} }}
    }}
  }}
}});

// Anomaly
if (D.anomalies.length > 0) {{
  const sec = document.getElementById('anomaly-section');
  let html = '<div class="anomaly"><div class="anomaly-title">⚠️ การใช้ผิดปกติ (Anomaly)</div>';
  D.anomalies.forEach(a => {{
    html += '<div class="anomaly-item"><strong>' + a.id + '</strong> — วันที่ ' + a.date
      + ' ใช้ ' + a.qty + ' (เฉลี่ย ' + a.avg + ', มากกว่า ' + a.ratio + 'x)</div>';
  }});
  html += '</div>';
  sec.innerHTML = html;
}}

// PO Note
if (D.poFile) {{
  document.getElementById('po-section').innerHTML =
    '<div class="po-note">📄 ร่างใบสั่งซื้อบันทึกไว้ที่ <strong>' + D.poFile + '</strong></div>';
}}

// Table
const urgencyMap = {{
  critical: {{icon:'🔴', label:'วิกฤต', cls:'badge-critical'}},
  urgent: {{icon:'🟡', label:'เร่งด่วน', cls:'badge-urgent'}},
  normal: {{icon:'🟢', label:'ปกติ', cls:'badge-normal'}},
}};
const tbody = document.getElementById('stock-table');
items.forEach(m => {{
  const u = urgencyMap[m.urgency];
  const tr = document.createElement('tr');
  if (m.urgency === 'critical') tr.className = 'critical';
  const stockClass = m.belowReorder ? ' class="below-reorder"' : '';
  const belowTag = m.belowReorder ? '<span class="below-tag">▼ ต่ำกว่า reorder</span>' : '';
  tr.innerHTML =
    '<td><span class="badge ' + u.cls + '">' + u.icon + ' ' + u.label + '</span></td>'
    + '<td style="font-family:monospace;font-weight:500">' + m.id + '</td>'
    + '<td style="font-weight:500">' + m.name + '</td>'
    + '<td' + stockClass + '>' + m.stock.toLocaleString() + ' ' + m.unit + belowTag + '</td>'
    + '<td>' + m.avg + ' ' + m.unit + '</td>'
    + '<td style="font-weight:600;color:' + (m.days < m.lead ? '#DC2626' : '#1F2937') + '">' + m.days + '</td>'
    + '<td style="color:#6B7280">' + m.lead + ' วัน</td>'
    + '<td style="color:#6B7280;font-size:12px">' + m.supplier + '</td>';
  tbody.appendChild(tr);
}});
</script>
</body>
</html>"""

    filename = "index.html"
    with open(filename, "w", encoding="utf-8") as f:
        f.write(html)
    return filename


# ===== LINE Alert =====

def format_alert_message(results, anomalies, suppliers_lookup):
    """จัดรูปแบบข้อความแจ้งเตือน Phase 2 (ใช้เป็น fallback ถ้า Gemini ล้มเหลว)"""
    low_stock = [r for r in results if r["current_stock"] <= r["reorder_point"]]

    if not low_stock and not anomalies:
        return None

    lines = ["📊 รายงานสถานะสต็อก"]
    lines.append(f"พบ {len(low_stock)} รายการต่ำกว่าเกณฑ์")
    lines.append("")

    for item in sorted(low_stock, key=lambda x: x["days_remaining"] if x["days_remaining"] is not None else 999):
        supplier_name = suppliers_lookup.get(item["supplier_id"], "ไม่ทราบ")

        lines.append(f"{item['urgency']} {item['name']} ({item['material_id']})")
        lines.append(f"  คงเหลือ: {item['current_stock']} {item['unit']}")
        lines.append(f"  ใช้เฉลี่ย: {item['avg_daily_usage']} {item['unit']}/วัน")

        if item["days_remaining"] is not None:
            lines.append(f"  เหลือใช้อีก: {item['days_remaining']} วัน")
        else:
            lines.append(f"  เหลือใช้อีก: ไม่มีข้อมูล")

        lines.append(f"  Lead time: {item['lead_time_days']} วัน")
        lines.append(f"  Supplier: {supplier_name}")
        lines.append("")

    if anomalies:
        lines.append("⚠️ พบการใช้ผิดปกติ")
        for a in anomalies:
            lines.append(f"  • {a['material_id']} วันที่ {a['date']}")
            lines.append(f"    ใช้ {a['quantity']} (เฉลี่ย {a['avg']}, มากกว่า {a['ratio']}x)")
        lines.append("")

    return "\n".join(lines)


def send_line_push(token, user_id, message):
    """ส่ง push message ผ่าน LINE Messaging API"""
    if len(message) > 5000:
        message = message[:4950] + "\n\n...(ข้อความถูกตัด)"

    url = "https://api.line.me/v2/bot/message/push"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }
    payload = {
        "to": user_id,
        "messages": [{"type": "text", "text": message}],
    }
    return requests.post(url, headers=headers, json=payload)


# ===== Main =====

def main():
    # 1. เชื่อมต่อ + อ่านข้อมูล
    print("กำลังเชื่อมต่อ Google Sheets...")
    spreadsheet = connect_sheets(GOOGLE_CREDENTIALS, SPREADSHEET_URL)

    suppliers = get_suppliers(spreadsheet)
    suppliers_lookup = {s["supplier_id"]: s["supplier_name"] for s in suppliers}
    print(f"Suppliers: {len(suppliers)} ราย")

    materials = get_materials(spreadsheet)
    print(f"Materials: {len(materials)} รายการ")

    usage_log = get_usage_log(spreadsheet)
    print(f"Usage Log: {len(usage_log)} รายการ")

    # 2. วิเคราะห์ (Phase 2)
    daily_usage = calc_daily_usage(usage_log)
    results = calc_days_remaining(materials, daily_usage)
    anomalies = detect_anomalies(usage_log, daily_usage)

    # 3. แสดงผลใน terminal
    print(f"\n{'='*50}")
    for r in sorted(results, key=lambda x: x["days_remaining"] if x["days_remaining"] is not None else 999):
        status = "⚠️" if r["current_stock"] <= r["reorder_point"] else "✅"
        days = f"{r['days_remaining']} วัน" if r["days_remaining"] is not None else "N/A"
        print(f"{status} {r['name']:20s} | เหลือ {days:10s} | {r['urgency']}")

    if anomalies:
        print(f"\n⚠️ Anomalies: {len(anomalies)} รายการ")
        for a in anomalies:
            print(f"  {a['material_id']} วันที่ {a['date']} — ใช้ {a['quantity']} (avg {a['avg']}, {a['ratio']}x)")

    print(f"{'='*50}")

    # 4. Phase 3: Gemini วิเคราะห์ + ร่าง PO
    line_message = None
    gemini_ok = False
    po_filename = None

    if GEMINI_API_KEY:
        print("\n🤖 กำลังส่งข้อมูลให้ Gemini วิเคราะห์...")
        try:
            prompt = build_gemini_prompt(results, anomalies, suppliers_lookup)
            response_text = call_gemini(prompt)
            summary, po_details = parse_gemini_response(response_text)

            print("✅ Gemini วิเคราะห์สำเร็จ")
            gemini_ok = True
            line_message = summary

            # บันทึก PO เป็นไฟล์
            if po_details:
                po_filename = save_po_file(summary, po_details)
                print(f"📄 บันทึกร่าง PO: {po_filename}")

        except Exception as e:
            print(f"⚠️ Gemini ล้มเหลว: {e}")
            print("→ ใช้ข้อความ Phase 2 แทน")

    # Fallback: ถ้า Gemini ไม่ได้ใช้หรือล้มเหลว → ข้อความ Phase 2
    if not gemini_ok:
        line_message = format_alert_message(results, anomalies, suppliers_lookup)

    # แปะลิงก์ dashboard ท้ายข้อความ
    if line_message and DASHBOARD_URL:
        line_message += f"\n\n📊 Dashboard: {DASHBOARD_URL}"

    # 5. ส่ง LINE alert
    if line_message:
        print("\nกำลังส่ง LINE alert...")
        response = send_line_push(LINE_CHANNEL_ACCESS_TOKEN, LINE_USER_ID, line_message)
        if response.status_code == 200:
            print("✅ ส่ง LINE alert สำเร็จ")
        else:
            print(f"❌ ส่งไม่สำเร็จ: {response.status_code}")
            print(response.json())
    else:
        print("\n✅ สต็อกปกติ ไม่มี anomaly — ไม่ต้องแจ้งเตือน")

    # 6. Phase 4: สร้าง Dashboard
    dashboard_file = generate_dashboard(results, anomalies, suppliers_lookup, po_filename)
    print(f"📊 สร้าง Dashboard: {dashboard_file}")


if __name__ == "__main__":
    main()
